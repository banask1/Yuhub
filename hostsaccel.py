# -*- coding: utf-8 -*-
"""hosts 网络加速引擎（Steam / GitHub）—— 纯标准库，不含 Qt。

做什么
------
通过 DoH（DNS-over-HTTPS）加密解析 + TCP 443 并发测速，为 Steam 与 GitHub
的每个域名选出"当前连通最快"的 CDN 边缘节点，写进系统 hosts 文件，绕开
国内 UDP 53 端口的 DNS 污染与劫持。方案与 `Chinachani/steam-hosts-tools`
(MIT) 同源。

硬约定（都踩过坑才会写进来）
---------------------------
1. **绝不静态 import 时改全局网络状态**。参考实现曾在 import 时
   `urllib.request.install_opener(无代理 opener)` —— 那会波及同进程里
   自动更新等所有 HTTP 行为。这里改为在 `query_doh` 内部按请求建 opener。
2. **所有写盘操作都显式接受 `hosts_path` 参数**。默认值才是系统 hosts；
   自检必须传临时路径，永远不许碰真实 hosts。
3. **提权只做写盘**。DoH 解析与测速不需要管理员权限，全部留在普通进程；
   需要提权的只有"改 hosts"这一步（复用 cleaner.py 的模式：
   runas 唤起 Yuhub.exe 自己 + base64 JSON 载荷 + 结果走文件）。
4. **域名清洗必须是幂等的**：写两次 = 写一次。靠区块标记
   `# === Yuhub Hosts Acceleration [svc] ===` 做到 —— 清洗先摘掉自己
   上次的区块和所有命中域名的散行，再追加新区块。
5. **只信公网 IPv4**。私网/环回/链路本地/多播/保留地址一律拒绝，
   防止把 127.0.0.1 或路由器地址写进 hosts 造成"域名被劫持到本机"。

对用户的诚实说明（写进 UI，不藏在文档里）
---------------------------------------
- Steam 商店 / 图片 / CDN：hosts 加速**有效**，可免代理直连。
- Steam 社区主站（steamcommunity.com）：国内存在 TLS SNI 阻断，
  hosts 只能提供"最优直连节点"，不保证能打开；完全访问需本地代理工具。
- GitHub：主站 / API / 下载 hosts 有效；`raw.githubusercontent.com`
  等部分子域在部分运营商下有 SNI 干扰，hosts 不保证 100% 生效。
"""

import base64
import concurrent.futures
import ctypes
import datetime as dt
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

IS_WIN = (platform.system().lower() == "windows")

# ---------------------------------------------------------------------------
# 域名清单
# ---------------------------------------------------------------------------
# Steam：商店 / 登录 / API / 结算 / 头像 / 公告 / 各 CDN（Akamai·Cloudflare·Fastly）
# GitHub：主站 / API / 代码下载 / Gist / 页面资源（Fastly）
SERVICE_STEAM = "steam"
SERVICE_GITHUB = "github"
SERVICES = (SERVICE_STEAM, SERVICE_GITHUB)

DOMAINS = {
    SERVICE_STEAM: [
        "store.steampowered.com",
        "login.steampowered.com",
        "help.steampowered.com",
        "api.steampowered.com",
        "checkout.steampowered.com",
        "steamcommunity.com",
        "steam-chat.com",
        "avatars.steamstatic.com",
        "clan.steamstatic.com",
        "community.fastly.steamstatic.com",
        "community.akamai.steamstatic.com",
        "store.fastly.steamstatic.com",
        "store.akamai.steamstatic.com",
        "store.cloudflare.steamstatic.com",
        "cdn.fastly.steamstatic.com",
        "cdn.akamai.steamstatic.com",
        "cdn.cloudflare.steamstatic.com",
        "steamcdn-a.akamaihd.net",
        "steamcommunity-a.akamaihd.net",
        "media.steampowered.com",
        # 国内 Steam CDN（与 Steam++ 的加速清单同源：山海云 / 牛牛云）
        "cdn.st.dl.eccdnx.com",
        "avatars.st.dl.eccdnx.com",
        "media.st.dl.eccdnx.com",
        "downloader.st.dl.eccdnx.com",
        "cdn.st.dl.qnssl.com",
    ],
    SERVICE_GITHUB: [
        "github.com",
        "api.github.com",
        "gist.github.com",
        "alive.github.com",
        "collector.github.com",
        "codeload.github.com",
        "gist.githubusercontent.com",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
        "assets.githubusercontent.com",
        "camo.githubusercontent.com",
        "avatars.githubusercontent.com",
        "favicons.githubusercontent.com",
        "github.githubassets.com",
        "live.github.com",
    ],
}

# 反向代理模式下写进 hosts 的固定地址（本机）。域名仍必须在 DOMAINS 清单里。
PROXY_IP = "127.0.0.1"

# 优选结果（accel_map.json）的有效期。超过这个时间就当"不新鲜"，打开开关
# 时顺带在后台重测一遍；没超过就直接用缓存秒开，一次网络请求都不发。
CACHE_TTL = 6 * 3600

# 加密 DoH 解析源（HTTPS 443 传输，避开 UDP 53 的 DNS 投毒）。
# 前两个在国内直连稳定，后两个国际源做交叉验证。
DOH_ENDPOINTS = [
    ("DNSPod", "https://doh.pub/resolve?name={domain}&type=A"),
    ("AliDNS", "https://dns.alidns.com/resolve?name={domain}&type=A"),
    ("Cloudflare", "https://cloudflare-dns.com/dns-query?name={domain}&type=A"),
    ("Google", "https://dns.google/resolve?name={domain}&type=A"),
]

# 兜底 IP 池：所有 DoH 源都不可用时注入（Akamai / Fastly 边缘节点较长期稳定）。
# 只有公网 IP 才会最终写盘（is_valid_public_ipv4 还会再拦一遍）。
FALLBACK_IPS = {
    SERVICE_STEAM: {
        "store.steampowered.com": ["23.15.142.182", "23.49.104.48", "104.89.103.51"],
        "avatars.steamstatic.com": ["151.101.79.52", "151.101.1.52", "23.2.16.11"],
        "cdn.cloudflare.steamstatic.com": ["23.2.16.11", "23.2.16.32", "104.16.29.34"],
        "steamcdn-a.akamaihd.net": ["23.208.12.167", "23.208.12.156", "23.49.104.59"],
        "steamcommunity.com": ["104.89.103.51", "23.49.104.48", "23.33.92.19"],
    },
    SERVICE_GITHUB: {
        "github.com": ["140.82.112.3", "140.82.113.4", "20.205.243.166"],
        "api.github.com": ["140.82.112.5", "140.82.113.5"],
        "codeload.github.com": ["140.82.112.9", "140.82.113.9"],
        "objects.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
        "raw.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
        "assets.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
        "camo.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
        "avatars.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
        "favicons.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
        "gist.githubusercontent.com": ["185.199.108.133", "185.199.109.133"],
    },
}

# ---------------------------------------------------------------------------
# hosts 路径、区块标记与提权载荷
# ---------------------------------------------------------------------------
_BLOCK_BEGIN = "# === Yuhub Hosts Acceleration [{svc}] ==="
_BLOCK_END = "# === End Yuhub Hosts Acceleration [{svc}] ==="
_BEGIN_RE = re.compile(r"^#\s*===\s*Yuhub Hosts Acceleration \[(steam|github)\]",
                       re.IGNORECASE)
_END_RE = re.compile(r"^#\s*===\s*End Yuhub Hosts Acceleration \[(steam|github)\]",
                     re.IGNORECASE)

_ELEVATED_FLAG = "--hosts-elevated"

# 并发提权结果文件序号（配合 PID 保证每次调用的结果文件唯一，见 _result_path）
_result_seq = 0
_result_seq_lock = threading.Lock()


def hosts_path():
    """系统 hosts 文件路径（Windows: %SystemRoot%\\System32\\drivers\\etc\\hosts）。"""
    if IS_WIN:
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return os.path.join(root, "System32", "drivers", "etc", "hosts")
    return "/etc/hosts"


def backup_dir():
    """hosts 备份目录（%LOCALAPPDATA%\\Yuhub\\hosts_backup）。

    放这里而不是文档目录的 Yuhub 文件夹：那里是主题包根目录，
    主题列表会扫子目录 —— 放进去备份目录会以"垃圾主题"的形式出现在列表里。
    """
    d = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%" if IS_WIN else "~"),
                     "Yuhub", "hosts_backup")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = ""
    return d


def _result_path():
    """提权写 hosts 的结果落地路径（提权进程写、普通进程读）。

    路径带 **PID + 递增序号**：同一进程内并发两次提权（比如"写入"和"退出清理"
    几乎同时发生）如果共用同一个结果文件，会互相 os.remove 掉对方的结果，
    导致其中一次永远等不到文件、一路轮询到 timeout 后报"取消了 UAC 或超时"。
    """
    d = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%" if IS_WIN else "~"), "Yuhub")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    global _result_seq
    with _result_seq_lock:
        _result_seq += 1
        seq = _result_seq
    return os.path.join(d, "hosts_result_%d_%d.json" % (os.getpid(), seq))


def is_admin():
    """当前进程是否具有管理员权限（Windows 用 IsUserAnAdmin）。"""
    try:
        if IS_WIN:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def shell_execute_runas(exe, args):
    """以管理员身份启动 exe（弹 UAC），返回 (ok, err)。

    ⚠️ **必须显式声明 restype / argtypes**：`ShellExecuteW` 返回的是
    `HINSTANCE`（**指针宽度**），而 ctypes.windll 默认按 `c_long`（32 位）
    取返回值。虽然大多数情况下返回的是 42 这种小值、截断不发作，但只要
    Windows 返回一个高位非零的句柄，截断后就会变成**负数或 ≤32**，
    于是被本函数下游的 `rc <= 32` 误判成"用户取消了 UAC" —— 表现为
    "点了关掉/开启，开关弹回去、什么也没发生"（用户报的"hosts 关不掉"）。
    显式 `restype = c_void_p` 才是对的写法。

    ok=True 时 err=""；ok=False 时 err 是给用户看的人话（取消 / 启动失败）。
    """
    try:
        fn = ctypes.windll.shell32.ShellExecuteW
        fn.restype = ctypes.c_void_p
        fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                       ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        rc = fn(None, "runas", exe, args, None, 0)
    except Exception as exc:
        return False, "无法发起提权请求：%s" % exc
    # NULL(0) 或 ≤32 = 失败；32 以上才是成功（见 ShellExecute 返回码约定）
    if not rc or int(rc) <= 32:
        code = int(rc or 0)
        if code == 5:
            return False, "提权被系统策略拒绝（返回码 5）"
        return False, "已取消管理员授权，未做任何改动"
    return True, ""


def sys_executable():
    """定位"能被 runas 唤起并解析 --hosts-elevated 的 exe"。

    源码模式下 sys.executable 是 python.exe —— 把它 runas 起来后
    `--hosts-elevated <b64>` 会被 python 当成**脚本名**，黑窗一闪
    （showCmd=0 隐藏）然后报错退出，上层只能干等到超时。所以源码模式必须
    显式去找同目录的 Yuhub.exe / dist\\Yuhub.exe。
    """
    if getattr(sys, "frozen", False):
        return sys.executable
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "Yuhub.exe"),
                 os.path.join(here, "dist", "Yuhub.exe")):
        if os.path.isfile(cand):
            return cand
    return ""


def elevation_capable():
    """能否走提权写盘。返回 (ok, 原因)。

    提前拦截"源码运行且没有打包 exe"的情况，给一句人话，而不是让用户
    白等 120 秒后收到"取消了 UAC 或超时"这种驴唇不对马嘴的提示。
    """
    if not IS_WIN:
        return False, "当前系统不支持提权写 hosts"
    if is_admin():
        return True, ""                              # 已经是管理员，直接写
    if sys_executable():
        return True, ""
    return False, ("当前是源码运行且未找到打包好的 Yuhub.exe，无法请求管理员权限。"
                   "请先运行 build.bat 构建，或直接使用打包后的 Yuhub.exe。")


def try_write_direct(mode, service, entries=None, proxy=False):
    """不弹 UAC，直接在当前进程里写 / 清 hosts。

    仅在**当前已是管理员**时调用（或用于"本来就不需要权限"的清理）。
    返回 (handled, result)：handled=False 表示"权限不够，请走提权"，此时
    result 为 None。这样上层就能做到"能直写就直写，不能才弹 UAC"，
    而不是每次操作都强制弹一遍。
    """
    if not is_admin():
        return False, None
    if mode == "write":
        clean_entries = []
        for item in entries or []:
            try:
                ip, domain = str(item[0]), str(item[1])
            except Exception:
                return True, {"ok": False, "removed": 0, "backup": "",
                              "error": "条目格式非法"}
            if domain not in DOMAINS[service]:
                return True, {"ok": False, "removed": 0, "backup": "",
                              "error": "域名不在白名单：%s" % domain}
            if proxy:
                if ip != PROXY_IP:
                    return True, {"ok": False, "removed": 0, "backup": "",
                                  "error": "代理模式只允许写入 127.0.0.1"}
            elif not is_valid_public_ipv4(ip):
                return True, {"ok": False, "removed": 0, "backup": "",
                              "error": "不是合法公网 IP：%s" % ip}
            clean_entries.append((ip, domain))
        return True, write_service_block(service, clean_entries, proxy=proxy)
    return True, clean_service(service)


# ---------------------------------------------------------------------------
# 解析与测速
# ---------------------------------------------------------------------------
# stdlib 的 is_private 不覆盖的"非公网"网段（CGNAT / 基准测试 / 文档专用段）。
# 这些地址写进 hosts 会让域名解析到一个不可路由的地址，必须显式拒绝。
_EXTRA_NON_PUBLIC = [
    ipaddress.ip_network(n)
    for n in ("100.64.0.0/10", "198.18.0.0/15", "192.0.0.0/24",
              "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
]


def is_valid_public_ipv4(ip_str):
    """严格校验公网 IPv4（拒绝私网 / 环回 / 链路本地 / 多播 / 保留 / CGNAT 等）。"""
    try:
        ip = ipaddress.IPv4Address(str(ip_str).strip())
    except Exception:
        return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return False
    return not any(ip in net for net in _EXTRA_NON_PUBLIC)


def parse_doh_answer(data):
    """从 DoH 的 JSON 应答里取全部合法公网 A 记录（纯函数，方便自检打桩）。"""
    try:
        answers = (data or {}).get("Answer", []) or []
    except AttributeError:
        return []
    ips = []
    for ans in answers:
        try:
            # type 1 = A 记录；CNAME(type 5) 等一律跳过
            if ans.get("type") != 1:
                continue
            raw = str(ans.get("data", "")).strip()
        except AttributeError:
            continue
        if is_valid_public_ipv4(raw) and raw not in ips:
            ips.append(raw)
    return ips


def query_doh(domain, url_template, timeout=2.5):
    """单源 DoH 解析。**按请求**建无代理 opener（绝不动全局网络状态）。"""
    url = url_template.format(domain=domain)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/dns-json",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return []
            return parse_doh_answer(json.loads(resp.read().decode("utf-8", "ignore")))
    except Exception:
        return []


def _fallback_for(domain, service):
    """兜底池命中（先精确、再包含匹配）。"""
    pool = FALLBACK_IPS.get(service, {})
    if domain in pool:
        return list(pool[domain])
    for key, ips in pool.items():
        if key in domain or domain in key:
            return list(ips)
    return []


def resolve_candidates(domain, service, doh_timeout=2.5):
    """多源并发 DoH 解析 + 兜底池，返回去重后的候选公网 IP 列表。"""
    cands = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(DOH_ENDPOINTS)) as ex:
        for ips in ex.map(lambda t: query_doh(domain, t[1], doh_timeout),
                          DOH_ENDPOINTS):
            for ip in ips:
                if ip not in cands:
                    cands.append(ip)
    if not cands:
        cands = _fallback_for(domain, service)
    return cands


def test_tcp_latency(ip, port=443, timeout=1.2):
    """TCP 握手延迟（毫秒）；失败返回 None。"""
    try:
        t0 = time.perf_counter()
        sock = socket.create_connection((ip, port), timeout=timeout)
        ms = (time.perf_counter() - t0) * 1000.0
        sock.close()
        return round(ms, 1)
    except Exception:
        return None


def select_best(ips, tcp_timeout=1.2):
    """并发测速选最快 IP。返回 (ip, ms) 或 None（全军覆没）。"""
    if not ips:
        return None
    results = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(ips), 10)) as ex:
        for ms in ex.map(lambda ip: test_tcp_latency(ip, 443, tcp_timeout), ips):
            if ms is not None:
                results.append(ms)
    if not results:
        return None
    best_ms = min(results)
    # ex.map 保序：结果序号 == 输入序号，直接取最快那个的 IP
    best_ip = ips[results.index(best_ms)]
    return best_ip, best_ms


def optimize_domain(domain, service, doh_timeout=2.5, tcp_timeout=1.2):
    """单域名全流程：解析 → 测速 → (ip, ms)；失败返回 None。

    防污染细节：steamcommunity.com 这类域名连 DoH 都可能拿到**投毒 IP**
    （国内 DoH 源的上游缓存被污染、国际源又可能被墙），表现为"候选不为空
    但 TCP 全灭"。所以候选全灭时必须再试兜底池 —— 只靠 TCP 握手测活来
    验证真假，不猜哪个 IP 是毒。
    """
    cands = resolve_candidates(domain, service, doh_timeout)
    best = select_best(cands, tcp_timeout) if cands else None
    if best is None:
        best = select_best(_fallback_for(domain, service), tcp_timeout)
    return best


def optimize_service(service, doh_timeout=2.5, tcp_timeout=1.2, on_domain=None):
    """整个服务全部域名并发优选。

    on_domain(done, total, domain, ip, ms, error) 会在工作线程被逐个调用
    （**UI 侧只能在回调里 emit 信号**，这是本项目的硬规则）。
    返回 [(ip, domain, ms), ...]，按域名清单原顺序排列。
    """
    domains = DOMAINS[service]
    total = len(domains)
    done = [0]

    def work(d):
        try:
            res = optimize_domain(d, service, doh_timeout, tcp_timeout)
        except Exception:
            res = None
        return d, res

    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for d, res in ex.map(work, domains):
            done[0] += 1
            if on_domain is not None:
                if res is None:
                    on_domain(done[0], total, d, "", 0.0, "未找到可用节点")
                else:
                    on_domain(done[0], total, d, res[0], res[1], "")
            if res is not None:
                out[d] = res
    return [(out[d][0], d, out[d][1]) for d in domains if d in out]


# ---------------------------------------------------------------------------
# hosts 内容处理（纯函数 + 显式路径，自检只碰临时文件）
# ---------------------------------------------------------------------------
def _newline():
    return "\r\n" if IS_WIN else "\n"


def clean_hosts_lines(lines, service):
    """摘掉本服务的区块 + 所有命中域名的散行（兼容空格 / 制表符 / 大小写）。

    返回 (cleaned_lines, removed_count)。散行按"域名在行内任何一列"匹配，
    防止用户手写的 `1.2.3.4 store.steampowered.com` 残留膨胀。
    """
    domain_set = set(DOMAINS[service])
    cleaned, removed, in_block = [], 0, False
    for line in lines:
        stripped = line.strip()
        m_begin = _BEGIN_RE.match(stripped)
        if m_begin:
            if m_begin.group(1) == service:
                in_block = True            # 自己的区块：摘开始标记
                removed += 1
            else:
                cleaned.append(line)       # 别的服务的区块：原样保留
            continue
        if in_block:
            m_end = _END_RE.match(stripped)
            if m_end and m_end.group(1) == service:
                in_block = False
            removed += 1                   # 区块内所有行（含结束标记）都摘
            continue
        if not stripped or stripped.startswith("#"):
            cleaned.append(line)
            continue
        parts = re.split(r"\s+", stripped)
        if any(p.lower() in domain_set for p in parts[1:]):
            removed += 1
            continue
        cleaned.append(line)
    return cleaned, removed


def build_block_lines(service, entries, ts=None, proxy=False):
    """把优选结果排成本服务的区块行。entries: [(ip, domain, ms), ...]

    proxy=True 表示是反向代理模式（hosts 指向 127.0.0.1，真实 IP 由本地
    代理按 SNI 转发）—— 模式写在注释里，重启后还能认出来。
    """
    stamp = ts or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = [_BLOCK_BEGIN.format(svc=service),
           "# 由 Yuhub 生成（%s，%s）" % (
               "本地反向代理模式" if proxy else "DoH 防污染解析 + TCP443 测速优选",
               stamp)]
    for ip, domain, ms in entries:
        out.append("%-16s\t%s" % (ip, domain))
    out.append(_BLOCK_END.format(svc=service))
    return out


def current_entries(service, path=None):
    """读出当前 hosts 里本服务区块的 (ip, domain) 列表（不校验连通）。

    直连模式条目是公网 IP；反向代理模式条目是 127.0.0.1 —— 两种都原样返回。
    """
    try:
        with open(path or hosts_path(), "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out, in_block = [], False
    for line in lines:
        stripped = line.strip()
        if _BEGIN_RE.match(stripped):
            in_block = (_BEGIN_RE.match(stripped).group(1) == service)
            continue
        if _END_RE.match(stripped):
            in_block = False
            continue
        if not in_block or not stripped or stripped.startswith("#"):
            continue
        parts = re.split(r"\s+", stripped)
        if len(parts) >= 2 and (is_valid_public_ipv4(parts[0])
                                or parts[0] == PROXY_IP):
            out.append((parts[0], parts[1]))
    return out


def entries_mode(entries):
    """判断条目属于哪种模式：'proxy'（全 127.0.0.1）/ 'direct'（全公网）/ ''。"""
    if not entries:
        return ""
    if all(ip == PROXY_IP for ip, _d in entries):
        return "proxy"
    if all(is_valid_public_ipv4(ip) for ip, _d in entries):
        return "direct"
    return ""


def service_enabled(service, path=None):
    """当前是否已有本服务的加速区块。"""
    try:
        with open(path or hosts_path(), "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = _BEGIN_RE.match(line.strip())
                if m and m.group(1) == service:
                    return True
    except OSError:
        pass
    return False


def _backup(hosts_file):
    """带时间戳的完整备份，返回备份路径；建不了备份目录返回 ""。"""
    d = backup_dir()
    if not d:
        return ""
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(d, "hosts_%s.bak" % ts)
    try:
        shutil.copy2(hosts_file, dst)
        return dst
    except OSError:
        return ""


def write_service_block(service, entries, hosts_file=None, do_backup=True,
                        proxy=False):
    """清洗旧内容 → 追加本服务新区块 → 写回。

    entries: [(ip, domain), ...]。proxy=True 时条目应为 (127.0.0.1, domain)
    （反向代理模式）。返回 {"ok", "removed", "backup", "error"}。
    只写显式给出的 hosts_file（默认才是系统 hosts）。
    """
    target = hosts_file or hosts_path()
    res = {"ok": False, "removed": 0, "backup": "", "error": ""}
    try:
        with open(target, "r", encoding="utf-8", errors="ignore") as fh:
            raw = fh.read()
    except OSError as exc:
        res["error"] = "读取 hosts 失败：%s" % exc
        return res

    if do_backup:
        res["backup"] = _backup(target)

    lines, res["removed"] = clean_hosts_lines(raw.splitlines(), service)
    lines += [""] + build_block_lines(
        service, [(ip, d, 0.0) for ip, d in entries], proxy=proxy)
    text = _newline().join(lines) + _newline()
    try:
        tmp = target + ".yuhub.tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except OSError as exc:
        res["error"] = "写入 hosts 失败（需要管理员权限）：%s" % exc
        return res
    res["ok"] = True
    return res


def clean_service(service, hosts_file=None, do_backup=True):
    """移除本服务的全部加速条目（恢复默认）。返回 dict 同 write_service_block。"""
    target = hosts_file or hosts_path()
    res = {"ok": False, "removed": 0, "backup": "", "error": ""}
    try:
        with open(target, "r", encoding="utf-8", errors="ignore") as fh:
            raw = fh.read()
    except OSError as exc:
        res["error"] = "读取 hosts 失败：%s" % exc
        return res
    if do_backup:
        res["backup"] = _backup(target)
    lines, res["removed"] = clean_hosts_lines(raw.splitlines(), service)
    if res["removed"] == 0:
        res["ok"] = True
        return res
    text = _newline().join(lines) + _newline()
    try:
        tmp = target + ".yuhub.tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except OSError as exc:
        res["error"] = "写入 hosts 失败（需要管理员权限）：%s" % exc
        return res
    res["ok"] = True
    return res


def flush_dns_cache():
    """刷新系统 DNS 缓存（Windows: ipconfig /flushdns；普通权限即可）。"""
    try:
        if IS_WIN:
            subprocess.run(["ipconfig", "/flushdns"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 提权写 hosts（复用 cleaner.py 的模式：runas 唤起自己的 exe + 结果走文件）
# ---------------------------------------------------------------------------
def elevated_hosts_main(payload_b64):
    """`Yuhub.exe --hosts-elevated <base64-json>` 的入口（提权进程侧）。

    payload: {"mode": "apply"} —— 统一入口，子进程自己读意图文件应用
             （开/关统一走这条，见 elevated_apply_main）
             或 {"mode": "write"|"clean", "service": ..., "entries": ...,
                 "proxy": bool, "result_file": ...} —— 旧的单服务直写模式
    无窗口、不建 UI、不碰网络 —— 只改 hosts 后把结果 JSON 原子写回。
    """
    try:
        payload = json.loads(base64.b64decode(payload_b64.encode("ascii"))
                             .decode("utf-8"))
        mode = payload.get("mode")
        service = payload.get("service")
        entries = payload.get("entries") or []
        result_file = payload.get("result_file") or ""
        proxy = bool(payload.get("proxy"))
    except Exception:
        return 2
    if not result_file:
        return 3
    if mode == "apply":
        # 统一应用入口：子进程自己读意图文件（父进程只负责把意图写好）
        return elevated_apply_main(result_file)
    if service not in SERVICES or mode not in ("write", "clean"):
        return 3
    if mode == "write":
        clean_entries = []
        for item in entries:
            try:
                ip, domain = str(item[0]), str(item[1])
            except Exception:
                return 3
            if domain not in DOMAINS[service]:
                return 3                      # 拒绝写进任何不在清单里的域名
            if proxy:
                if ip != PROXY_IP:
                    return 3                  # 代理模式只允许 127.0.0.1
            elif not is_valid_public_ipv4(ip):
                return 3                      # 直连模式只允许公网 IP
            clean_entries.append((ip, domain))
        out = write_service_block(service, clean_entries, proxy=proxy)
    else:
        out = clean_service(service)
    try:
        tmp = result_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(out, fp, ensure_ascii=False)
        os.replace(tmp, result_file)
    except OSError:
        return 4
    return 0


def run_elevated(mode, service, entries=None, timeout=120.0, proxy=False):
    """弹一次 UAC，在管理员权限下写 / 清 hosts。返回结果 dict 或 None。

    在**后台线程**里调用（轮询等结果文件会阻塞，不能拖主线程）。
    proxy=True：反向代理模式写入（条目应为 127.0.0.1 + 域名）。

    返回约定：
      dict             —— 写/清完成，看 res["ok"]；或失败原因 res["error"]
      {"error": "..."} —— 无法提权 / 用户取消 / 超时，error 里是人话
      None             —— 仅在非 Windows 或建不出结果文件时返回（极少）
    """
    if not IS_WIN:
        return None
    ok, why = elevation_capable()
    if not ok:
        return {"error": why}
    result_file = _result_path()
    if not result_file:
        return {"error": "无法创建提权结果文件（临时目录不可写）"}
    for p in (result_file, result_file + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass
    payload = json.dumps(
        {"mode": mode, "service": service,
         "entries": [[ip, d] for ip, d in (entries or [])],
         "proxy": bool(proxy),
         "result_file": result_file},
        ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    exe = sys_executable()
    if not exe:
        return {"error": "未找到可用于提权的 Yuhub.exe"}
    ok, why = shell_execute_runas(exe, "%s %s" % (_ELEVATED_FLAG, b64))
    if not ok:
        return {"error": why}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(result_file):
            time.sleep(0.08)                         # 等写完（os.replace 已原子化）
            try:
                with open(result_file, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except (OSError, ValueError):
                return None
        time.sleep(0.15)
    return {"error": "等待提权进程超时（%d 秒）——可能是杀软拦下了提权进程" % timeout}


# ---------------------------------------------------------------------------
# 优选映射缓存（域名 → 真实 IP）：本地反向代理按 SNI 查的表
# ---------------------------------------------------------------------------
def _map_cache_path():
    d = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%" if IS_WIN else "~"),
                     "Yuhub")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(d, "accel_map.json")


def _clean_mapping(mapping):
    """把任意映射清洗成 {已知域名: 合法公网 IP}（纯函数，自检直接覆盖）。"""
    out = {}
    for domain, ip in dict(mapping or {}).items():
        try:
            d, ip = str(domain), str(ip)
        except Exception:
            continue
        # 只收已知域名 + 合法 IP（代理模式缓存里不会有 127.0.0.1，但防手改）
        if any(d in DOMAINS[s] for s in SERVICES) and is_valid_public_ipv4(ip):
            out[d] = ip
    return out


def _map_cache_envelope(data):
    """兼容两种落盘格式，取出 (mapping, ts)。

    新格式: {"ts": 1759..., "map": {domain: ip}}
    旧格式: {domain: ip}            （v1.0.2 及以前写的，必须还能读）
    v1.0.3 起带时间戳 —— 有它才能判断"这份缓存还新不新"，进而决定
    打开开关时是直接秒写（缓存新鲜）还是顺带后台重测。
    """
    if not isinstance(data, dict):
        return {}, None
    if isinstance(data.get("map"), dict):
        try:
            ts = float(data.get("ts"))
        except (TypeError, ValueError):
            ts = None
        return _clean_mapping(data["map"]), ts
    return _clean_mapping(data), None


def load_map_cache():
    """读出上次的优选映射 {domain: ip}；坏文件/不存在返回 {}。

    代理模式的真实 IP 全靠它 —— hosts 里只有 127.0.0.1，如果缓存丢了，
    代理会在收到连接时按域名现场重新解析兜底。
    """
    p = _map_cache_path()
    if not p:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return {}
    return _map_cache_envelope(data)[0]


def map_cache_age():
    """缓存写了多久（秒）；没有缓存 / 旧格式无时间戳 / 读不出来 → None。

    None 表示"不知道新不新"，调用方一律当**不新鲜**处理（宁可重测一次，
    也不要拿着一份来历不明的 IP 表去写 hosts）。
    """
    p = _map_cache_path()
    if not p:
        return None
    try:
        with open(p, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return None
    _mapping, ts = _map_cache_envelope(data)
    if ts is None:
        return None
    age = time.time() - ts
    return age if age >= 0 else None


def map_cache_fresh(max_age=None):
    """缓存是否还在有效期内（默认 CACHE_TTL）。"""
    age = map_cache_age()
    if age is None:
        return False
    return age <= (CACHE_TTL if max_age is None else float(max_age))


def save_map_cache(mapping, ts=None):
    """原子写回优选映射。失败返回 False（代理仍有现场解析兜底，不致命）。

    ts 省略时用当前时间 —— 时间戳就是"这份优选结果什么时候测的"。
    """
    p = _map_cache_path()
    if not p:
        return False
    clean = _clean_mapping(mapping)
    payload = {"ts": float(time.time() if ts is None else ts), "map": clean}
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def instant_entries(service, mode, mapping=None):
    """**秒加速**的核心：不联网，立刻给出可以写进 hosts 的条目。

    返回 (entries, missing)：
      entries —— [(ip, domain), ...]，可直接交给 write_service_block
      missing —— 直连模式下"既没缓存也没有兜底 IP"的域名（代理模式恒为空）

    mode == "proxy"
        全部域名都写 127.0.0.1。**一个域名都不用测速** —— 真实 IP 由本地
        代理按 SNI 现场解析（缓存命中就用缓存，未命中就 DoH+测速兜底）。
        这正是 Steam++ 能做到"点一下立刻生效"的原因：启用路径上没有任何
        网络测量，只有一次"改 hosts"。
    mode == "direct"
        先查上次优选缓存，缺失的用内置兜底池补；仍凑不出来的进 missing。
        直连模式必须知道真实 IP，所以缓存为空时无法做到真·秒开 —— 这个
        时候由调用方决定要不要退化成"先测速"（本函数自己绝不联网）。
    """
    if service not in DOMAINS:
        return [], []
    if mode == "proxy":
        return [(PROXY_IP, d) for d in DOMAINS[service]], []
    cached = _clean_mapping(mapping or {})
    entries, missing = [], []
    for d in DOMAINS[service]:
        ip = cached.get(d)
        if not ip:
            pool = _fallback_for(d, service)
            ip = pool[0] if pool else ""
        if ip:
            entries.append((ip, d))
        else:
            missing.append(d)
    return entries, missing


def optimize_service_map(service, on_domain=None):
    """同 optimize_service，但直接返回 {domain: ip}（后台预热缓存的入口）。"""
    return {d: ip for ip, d, _ms in optimize_service(service, on_domain=on_domain)}


# ---------------------------------------------------------------------------
# 意图文件（intent）+ 统一应用入口 —— 修「关不掉」的架构级方案
# ---------------------------------------------------------------------------
# 设计动机（v1.0.4）：以前"开"和"关"是两条独立的提权操作，各弹各的 UAC：
#   * 用户在"开启的 UAC 还没点"时关掉开关 → 旧实现静默早退 + 开关弹回真实
#     状态，用户再点一次就触发**反向操作**，状态机分叉、UAC 接连弹；
#   * "写入途中被关"要补一刀清理 → 第二次 UAC，用户懵了取消掉 → hosts
#     留着刚写的条目，开关弹回"开" → 彻底"关不掉"。
# 现在开/关统一成**一份意图文件**：父进程把"每个服务想开还是想关"写进
# 文件再发起提权；提权子进程应用的是**读文件那一刻的最新意图** —— UAC
# 等待期间改主意，同一次 UAC 就按新意图执行，永不分叉、永不多弹。

def intent_path():
    """意图文件路径（%LOCALAPPDATA%\\Yuhub\\hosts_intent.json）。"""
    d = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%" if IS_WIN else "~"),
                     "Yuhub")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(d, "hosts_intent.json")


def read_intent(path=None):
    """读意图文件，返回 {"seq": int, "steam": bool, "github": bool} 或 None。

    只认 0/1/true/false 布尔值；坏文件、缺 seq、没有任何服务键都算无效。
    """
    p = path or intent_path()
    if not p:
        return None
    try:
        with open(p, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        seq = int(data.get("seq"))
    except (TypeError, ValueError):
        return None
    out = {"seq": seq}
    for s in SERVICES:
        v = data.get(s)
        if isinstance(v, bool):
            out[s] = v
    if len(out) == 1:                        # 只有 seq，没有任何服务键
        return None
    return out


def write_intent(states, path=None):
    """写入（覆盖）意图文件，返回新 seq；失败返回 -1。

    **整文件覆盖**而不是合并：调用方传入的 states 就是"当前想要的完整
    期望态"（UI 侧维护会话内快照，初次 toggle 前用 hosts 真实状态初始化）。
    覆盖式写入杜绝了"上次会话遗留的 steam:True 被这次 github 的提权顺手
    应用"这种陈旧意图污染。
    """
    p = path or intent_path()
    if not p:
        return -1
    old = read_intent(p)
    seq = (old["seq"] + 1) if old else 1
    payload = {"seq": seq}
    for s in SERVICES:
        if s in states:
            payload[s] = bool(states[s])
    if len(payload) == 1:                    # 没有任何服务键，没东西可写
        return -1
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False)
        os.replace(tmp, p)
        return seq
    except OSError:
        return -1


def apply_intent(intent, hosts_file=None, do_backup=True):
    """把意图应用到 hosts：on 的服务写代理区块，off 的清区块。

    幂等：已经处于目标状态的服务**跳过**（不写盘、不备份），所以重复
    应用 = 只动有变化的服务。返回：
      {"ok": 总体是否全部成功, "seq": 意图 seq,
       "services": {svc: {"ok", "action": "write"/"clean"/"skip", "error"}}}
    只动意图里**出现过的**服务键 —— 没提的服务一律不碰。
    """
    seq = intent.get("seq", 0) if isinstance(intent, dict) else 0
    out = {"ok": True, "seq": seq, "services": {}}
    for s in SERVICES:
        if s not in (intent or {}):
            continue
        want = bool(intent[s])
        entries = current_entries(s, hosts_file)
        have = bool(entries) or service_enabled(s, hosts_file)
        sub = {"ok": True, "action": "skip", "error": ""}
        if want:
            # 已是"全量代理区块"就跳过（幂等的关键）
            if (entries_mode(entries) == "proxy"
                    and [d for _ip, d in entries] == list(DOMAINS[s])):
                sub["action"] = "skip"
            else:
                sub["action"] = "write"
                res = write_service_block(
                    s, [(PROXY_IP, d) for d in DOMAINS[s]],
                    hosts_file=hosts_file, do_backup=do_backup, proxy=True)
                sub["ok"] = bool(res.get("ok"))
                sub["error"] = res.get("error") or ""
        else:
            if not have:
                sub["action"] = "skip"
            else:
                sub["action"] = "clean"
                res = clean_service(s, hosts_file=hosts_file,
                                    do_backup=do_backup)
                sub["ok"] = bool(res.get("ok"))
                sub["error"] = res.get("error") or ""
        if not sub["ok"]:
            out["ok"] = False
        out["services"][s] = sub
    return out


_INTENT_STABLE_WINDOW = 0.6     # 子进程"意图没再变"的确认窗口（秒）
_INTENT_MAX_ROUNDS = 8          # 防御性上限：意图疯狂变化时最多应用几轮


def elevated_apply_main(result_file):
    """提权子进程侧的"应用最新意图"入口（apply 模式的实现）。

    循环：读意图 → 应用 → 落结果 → 等稳定窗看意图有没有又变 → 变了就
    再应用一轮（结果文件覆盖重写）。父进程读完结果后比对 seq：seq 落后
    于意图文件就再发起一轮提权（罕见，只在子进程退出后意图又变时发生）。
    """
    last_seq = None
    rounds = 0
    while rounds < _INTENT_MAX_ROUNDS:
        rounds += 1
        intent = read_intent()
        if intent is None:
            out = {"ok": False, "error": "意图文件不可读",
                   "seq": last_seq or 0, "services": {}}
        else:
            out = apply_intent(intent)
            last_seq = intent["seq"]
        if result_file:
            try:
                tmp = result_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fp:
                    json.dump(out, fp, ensure_ascii=False)
                os.replace(tmp, result_file)
            except OSError:
                return 4
        # 稳定窗：窗口内意图没再变才退出（UAC 期间改的主意在这里被吸收）
        deadline = time.monotonic() + _INTENT_STABLE_WINDOW
        changed = False
        while time.monotonic() < deadline:
            cur = read_intent()
            if cur is not None and cur["seq"] != last_seq:
                changed = True
                break
            time.sleep(0.05)
        if not changed:
            return 0
    return 0


def run_elevated_apply(timeout=120.0, path=None):
    """弹一次 UAC，让提权子进程应用**当前意图文件**。返回结果 dict。

    与 run_elevated 同一套结果文件/PID/序号机制；返回 dict 多带一个
    "seq"（子进程实际应用的意图序号），父进程拿它和意图文件的 seq 比对，
    落后了说明"应用完之后意图又变了"，需要再发起一轮。
    """
    if not IS_WIN:
        return None
    ok, why = elevation_capable()
    if not ok:
        return {"ok": False, "error": why}
    result_file = _result_path()
    if not result_file:
        return {"ok": False, "error": "无法创建提权结果文件（临时目录不可写）"}
    for p in (result_file, result_file + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass
    payload = json.dumps({"mode": "apply", "result_file": result_file},
                         ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    exe = sys_executable()
    if not exe:
        return {"ok": False, "error": "未找到可用于提权的 Yuhub.exe"}
    ok, why = shell_execute_runas(exe, "%s %s" % (_ELEVATED_FLAG, b64))
    if not ok:
        return {"ok": False, "error": why}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(result_file):
            time.sleep(0.08)
            try:
                with open(result_file, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except (OSError, ValueError):
                return None
        time.sleep(0.15)
    return {"ok": False,
            "error": "等待提权进程超时（%d 秒）——可能是杀软拦下了提权进程"
                     % timeout}


def entries_match(current, wanted):
    """两组条目是否"实质相同"（顺序无关，只在 ip+域名 集合上比）。

    用途：打开开关时若 hosts 里已经是我们要写的那套内容，就**完全不用
    提权、不弹 UAC、不写盘** —— 只刷一下 DNS 缓存即可。重复拨开关因此
    也是瞬时的。
    """
    try:
        return (sorted(tuple(x) for x in (current or []))
                == sorted(tuple(x) for x in (wanted or [])))
    except Exception:
        return False
