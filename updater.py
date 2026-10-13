"""Yuhub 自动更新核心模块（纯逻辑，不依赖 Qt，可独立测试）。

设计要点（详见 docs/自动更新方案.md）：

  * 更新源用 **GitHub Releases**：tag 当版本号、body 当更新说明、assets 当下载地址。
  * 替换手法用 **两步改名**，不是覆盖写 —— Windows 上运行中的 exe
    禁止改写内容（映像节），但**改目录项（改名）是允许的**。
  * 替换器不是独立 exe，而是**把自己拷一份到临时目录**，
    用 `Yuhub.exe --apply-update <base64-json>` 早返回模式启动。
    原因：替换器不能是它要替换的那个文件（自己锁着自己）。

自检入口见 update_selftest.py，用本地 http.server 伪装更新源跑全流程。
"""

import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 常量

# GitHub API 强制要求 User-Agent，不带会被 403
_UA = "Yuhub-Updater"

# 仓库坐标（更新源必须是公开仓库——把 token 放进客户端 = 泄露）
REPO_OWNER = "banask1"
REPO_NAME = "Yuhub"

# 单次网络请求超时（秒）。静默检查绝不能拖慢启动。
HTTP_TIMEOUT = 8

# 下载完成后、启动新 exe 后的观察窗口：这么久内退出且非 0 → 判定新版有问题
NEW_EXE_GRACE = 5.0

# 等旧进程退出的上限，超时就强杀
WAIT_OLD_PID_TIMEOUT = 30.0

_CREATE_NO_WINDOW = 0x08000000


# ================================================================ 版本号

def parse_version(s):
    """把版本字符串解析成可比较的元组。

    规则（宽松，能容纳常见的几种写法）：
      "v0.8beta"   -> (0, 8, -1)       # beta < 正式，用 -1 占位
      "0.8.1"      -> (0, 8, 1)
      "v1.0"       -> (1, 0)
      "1.0.0-rc1"  -> (1, 0, 0, -1)
      ""           -> ()

    尾部数字个数不齐时短的补 0 比较（见 is_newer）。
    """
    if not s:
        return ()
    t = str(s).strip().lstrip("vV")
    # 预发布后缀（beta / rc / alpha / preview）→ 标记为 -1，低于同号正式版
    pre = -1 if re.search(r"(alpha|beta|rc|preview|dev)", t, re.I) else 0
    out = []
    for part in re.split(r"[.\-_+]", t):
        m = re.match(r"^(\d+)", part.strip())
        if m:
            out.append(int(m.group(1)))
    if not out:
        return ()
    # 有预发布标记时，末尾追加 -1 让 (0,8,-1) < (0,8,0)
    if pre == -1:
        out.append(-1)
    return tuple(out)


def is_newer(remote, local):
    """remote 是否比 local 新。

    长度不齐时补 0：("0.8", ) vs ("0.8", 0) 视为相等。
    """
    r = parse_version(remote)
    l = parse_version(local)
    if not r:
        return False
    if not l:
        return True
    n = max(len(r), len(l))
    r = r + (0,) * (n - len(r))
    l = l + (0,) * (n - len(l))
    return r > l


# ================================================================ 清单获取

class ReleaseInfo:
    """一次 Release 的解析结果。"""

    def __init__(self, tag, name, notes, asset_name, asset_url,
                 asset_size=0, sha256="", published=""):
        self.tag = tag                  # "v0.8beta"
        self.name = name                # "Yuhub v0.8beta"
        self.notes = notes              # 更新说明（Markdown）
        self.asset_name = asset_name    # "Yuhub.exe"
        self.asset_url = asset_url      # 直链
        self.asset_size = int(asset_size or 0)
        self.sha256 = (sha256 or "").lower().replace("sha256:", "").strip()
        self.published = published

    @property
    def version(self):
        return self.tag

    def __repr__(self):
        return "<ReleaseInfo %s asset=%s size=%d>" % (
            self.tag, self.asset_name, self.asset_size)

    def to_dict(self):
        return {
            "tag": self.tag, "name": self.name, "notes": self.notes,
            "asset_name": self.asset_name, "asset_url": self.asset_url,
            "asset_size": self.asset_size, "sha256": self.sha256,
            "published": self.published,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("tag", ""), d.get("name", ""), d.get("notes", ""),
                   d.get("asset_name", ""), d.get("asset_url", ""),
                   d.get("asset_size", 0), d.get("sha256", ""),
                   d.get("published", ""))


def _http_json(url, timeout=HTTP_TIMEOUT, proxy=None):
    """GET 一个 JSON 接口，返回解析后的 dict。失败抛异常。

    proxy="direct" 时绕过系统代理直连（见 downloader._open 的说明）。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
    })
    if proxy == "direct":
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    else:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _bounded(fn, timeout):
    """带上限地执行 fn()。返回 {"res": 值} 或 {"err": 异常}；超时返回 {}。

    DNS 解析（getaddrinfo）不受 socket 超时控制——「检查更新/下载一直
    卡住」的元凶。把请求放进守护线程，join 到点就放弃（遗弃的 daemon
    线程自生自灭），调用方换下一个候选。
    """
    box = {}

    def _job():
        try:
            box["res"] = fn()
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=_job, daemon=True)
    t.start()
    t.join(timeout)
    return box


def pick_asset(assets, prefer=("Yuhub.exe", ".exe")):
    """从 assets 里挑出主程序。

    优先精确匹配 prefer[0]，否则退到第一个 .exe，再否则退到第一个 asset。
    """
    if not assets:
        return None
    for want in prefer:
        for a in assets:
            if (a.get("name") or "").lower() == want.lower():
                return a
    for a in assets:
        if (a.get("name") or "").lower().endswith(".exe"):
            return a
    return assets[0]


def parse_release(d):
    """把 GitHub releases/latest 或 releases 的单个对象解析成 ReleaseInfo。

    也接受自建的 version.json 结构（字段名不同，做兼容）。
    """
    # 兼容自建清单 {"version":..,"url":..,"sha256":..,"notes":..}
    if "tag_name" not in d and "version" in d:
        return ReleaseInfo(
            tag=d.get("version", ""), name=d.get("name") or d.get("version", ""),
            notes=d.get("notes", ""), asset_name=os.path.basename(d.get("url", "")),
            asset_url=d.get("url", ""), asset_size=d.get("size", 0),
            sha256=d.get("sha256", ""), published=d.get("published", ""),
        )

    assets = d.get("assets") or []
    a = pick_asset(assets) or {}
    digest = a.get("digest") or ""
    return ReleaseInfo(
        tag=d.get("tag_name", ""),
        name=d.get("name") or d.get("tag_name", ""),
        notes=d.get("body", "") or "",
        asset_name=a.get("name", ""),
        asset_url=a.get("browser_download_url", ""),
        asset_size=a.get("size", 0),
        sha256=digest,
        published=d.get("published_at", "") or "",
    )


def fetch_release(owner=REPO_OWNER, repo=REPO_NAME, timeout=HTTP_TIMEOUT,
                  api_base="https://api.github.com"):
    """取最新 Release。返回 (ReleaseInfo | None, error_str)。

    v0.8.5beta：检查阶段也走候选源 + 限时。
      * API 候选：官方 api.github.com → 三个中转镜像 → 无代理直连，
        逐个尝试（镜像前缀同样能代理 api.github.com 的路径）；
      * 每次请求限时 timeout+2 秒，DNS 挂起时放弃换下一个，
        「检查更新卡住」从此不可能超过候选数 × (timeout+2)。

    ⚠️ 关键陷阱（保留）：GitHub 的 `releases/latest` **会跳过 prerelease
    和 draft**。所以单个候选内仍维持「先 latest，404 再退列表」两级策略。
    """
    last_err = ""
    for base, proxy_mode in _api_attempts(api_base):
        latest_url = "%s/repos/%s/%s/releases/latest" % (base, owner, repo)
        box = _bounded(
            lambda u=latest_url, m=proxy_mode: _http_json(
                u, timeout=timeout, proxy=m),
            timeout + 2,
        )
        if "res" in box:
            data = box["res"]
            if isinstance(data, dict) and data.get("tag_name"):
                return parse_release(data), ""
            last_err = "服务响应异常"
            continue

        err = box.get("err")
        if isinstance(err, urllib.error.HTTPError):
            if err.code == 404:
                # ① latest 404 → 同一候选内退到 releases 列表
                list_url = "%s/repos/%s/%s/releases?per_page=10" % (
                    base, owner, repo)
                box2 = _bounded(
                    lambda u=list_url, m=proxy_mode: _http_json(
                        u, timeout=timeout, proxy=m),
                    timeout + 2,
                )
                if "res" in box2:
                    arr = box2["res"]
                    if isinstance(arr, list):
                        for item in arr:
                            if item.get("draft"):
                                continue          # 草稿不该被客户端看到
                            if not item.get("tag_name"):
                                continue
                            return parse_release(item), ""
                        return None, ""           # 仓库里一个 Release 都没有
                    last_err = "服务响应异常"
                else:
                    last_err = _net_err(box2.get("err"))
                continue
            if err.code == 403:
                last_err = "请求过于频繁或网络受限（HTTP 403）"
            else:
                last_err = "服务返回 HTTP %d" % err.code
            continue
        last_err = _net_err(err)
    return None, last_err


def _net_err(err):
    """把网络异常翻译成给用户看的短句。"""
    if err is None:
        return ""
    if isinstance(err, urllib.error.URLError):
        return "网络不可达：%s" % (getattr(err, "reason", err),)
    return "检查失败：%s" % (err,)


def fetch_release_by_tag(tag, owner=REPO_OWNER, repo=REPO_NAME,
                         timeout=HTTP_TIMEOUT, api_base="https://api.github.com"):
    """取**指定 tag** 的 Release（一键修复软件用：重下当前版本）。

    返回 (ReleaseInfo | None, error_str)：
      * 找到：   (info, "")
      * 没有该 tag（确定是版本问题，不是网络问题）： (None, "")
      * 网络失败： (None, "原因")
    候选源与限时策略和 fetch_release 完全一致。
    """
    last_err = ""
    for base, proxy_mode in _api_attempts(api_base):
        url = "%s/repos/%s/%s/releases/tags/%s" % (base, owner, repo, tag)
        box = _bounded(
            lambda u=url, m=proxy_mode: _http_json(u, timeout=timeout, proxy=m),
            timeout + 2,
        )
        if "res" in box:
            data = box["res"]
            if isinstance(data, dict) and data.get("tag_name"):
                return parse_release(data), ""
            last_err = "服务响应异常"
            continue
        err = box.get("err")
        if isinstance(err, urllib.error.HTTPError):
            if err.code == 404:
                return None, ""           # tag 不存在，让调用方走兜底逻辑
            if err.code == 403:
                last_err = "请求过于频繁或网络受限（HTTP 403）"
            else:
                last_err = "服务返回 HTTP %d" % err.code
            continue
        last_err = _net_err(err)
    return None, last_err


def check_for_update(current_version, owner=REPO_OWNER, repo=REPO_NAME,
                     timeout=HTTP_TIMEOUT, api_base="https://api.github.com"):
    """检查是否有新版本。

    返回 (ReleaseInfo | None, error_str)。
    - 有新版： (info, "")
    - 已最新： (None, "")
    - 出错：   (None, "原因")
    """
    info, err = fetch_release(owner, repo, timeout, api_base)
    if err:
        return None, err
    if info is None:
        return None, ""              # 没有 Release，视为已最新

    if not info.asset_url:
        return None, "该版本没有可下载的文件"
    if is_newer(info.tag, current_version):
        return info, ""
    return None, ""


# ================================================================ 下载与校验

def sha256_file(path, chunk=1024 * 1024):
    """流式计算文件 sha256（60MB 文件约 0.2 秒，不必开线程）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_sha256(path, expect):
    """校验文件 sha256。expect 为空时返回 True（无从校验，放行）。

    ⚠️ 安全说明：拿不到摘要意味着无法防御中间人替换下载内容。
    GitHub Releases 的 digest 字段通常可用；确实拿不到时，
    这里选择放行但把情况记进日志，而不是拒绝更新（否则老版本
    客户端会永远无法升级）。
    """
    if not expect:
        return True
    try:
        return sha256_file(path).lower() == str(expect).lower().replace("sha256:", "")
    except OSError:
        return False


# ---------------------------------------------------------------- 包体检
#
# 为什么要在"启动新版"之前自己看一眼文件：
#
# 把不是一个合法 64 位 PE 的东西交给 CreateProcess，Windows 不会安静地失败，
# 而是弹一个**模态系统对话框**：
#
#     不支持的 16 位应用程序
#     由于与 64 位版本的 Windows 不兼容，此程序或功能
#     "C:\...\Target.exe" 无法启动或运行。
#     请联系软件供应商询问是否有与 64 位 Windows 兼容的版本。
#
# 用户看到的是"更新完电脑坏了、要联系软件商"，而不是"更新包损坏，已回滚"。
# 这个场景并不罕见：下载被中途掐断会留下半截文件；被代理/镜像劫持时，
# 拿回来的可能是一个 HTML 错误页被原样存成了 .exe。
#
# 所以在改名换文件**之前**先体检，不合格就原地返回、一个文件都不动。

# 本机 Windows 能执行的 PE Machine。任何 Windows 都跑得了 32 位；
# 64 位 x64 额外接受 x64；ARM64 额外接受 ARM64 与 x64（系统带模拟）。
_PE_MACHINE = {"X86": 0x14C, "AMD64": 0x8664, "ARM64": 0xAA64, "ARM": 0x1C0}

_SEM_FAILCRITICALERRORS = 0x0001


def silence_loader_dialogs():
    """关掉本进程加载器弹系统错误框的行为（兜底，不是主防线）。

    主防线是 check_package_runnable()——正常情况下根本不会把坏文件交给
    Windows。这里再兜一层：万一是别的原因触发载入器报错（杀软拦了、
    文件刚好在改名和启动之间被删），也不要弹框吓人。
    """
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetErrorMode(_SEM_FAILCRITICALERRORS)
    except Exception:
        pass


def loader_dialogs_silenced():
    """本进程是否已关掉载入器系统弹窗（自检 / 诊断用）。"""
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.kernel32.GetErrorMode()
                    & _SEM_FAILCRITICALERRORS)
    except Exception:
        return False


def pe_machine(path):
    """读 PE 头里的 Machine 字段；不是 PE 就返回 None。

    只读前几百字节，60MB 的包也是瞬间完成，不用整文件读。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if len(head) < 64 or head[:2] != b"MZ":
                return None
            off = int.from_bytes(head[0x3C:0x40], "little")
            if not 0 < off < 16 * 1024 * 1024:
                return None                # e_lfanew 离谱 → 不是真 PE
            f.seek(off)
            sig = f.read(6)
            if len(sig) < 6 or sig[:4] != b"PE\0\0":
                return None
            return int.from_bytes(sig[4:6], "little")
    except (OSError, ValueError):
        return None


def runnable_machines():
    """本机 Windows 能跑哪些 Machine（决定体检放行范围）。"""
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    ok = {_PE_MACHINE["X86"]}              # 32 位在任何 Windows 上都能跑
    if arch == "AMD64":
        ok.add(_PE_MACHINE["AMD64"])
    elif arch == "ARM64":
        ok.add(_PE_MACHINE["ARM64"])
        ok.add(_PE_MACHINE["AMD64"])       # ARM64 Windows 自带 x64 模拟
    elif arch == "ARM":
        ok.add(_PE_MACHINE["ARM"])
    return ok


def check_package_runnable(path):
    """更新包能不能在本机跑起来。返回 (ok, 给用户看的原因)。

    只判"能不能启动"，不判内容对不对——内容由 sha256 负责。
    """
    if not path or not os.path.isfile(path):
        return False, "更新包不存在"
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, "读不到更新包：%s" % e
    if size < 4096:
        # 真 Yuhub.exe 是几十 MB。几 KB 的东西只可能是错误页或半截文件。
        return False, "更新包只有 %d 字节，明显没下载完整" % size
    machine = pe_machine(path)
    if machine is None:
        return False, "更新包不是有效的 Windows 程序（多半是下载中断或被抓成了错误页）"
    if os.name == "nt" and machine not in runnable_machines():
        return False, "更新包的架构与你的系统不匹配（不兼容本机 64 位 Windows）"
    return True, ""


def update_dir():
    """更新工作目录：%LOCALAPPDATA%\\Yuhub\\update。

    放 LOCALAPPDATA 而不是 exe 同目录 —— 后者可能在 Program Files（不可写）。
    """
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    d = os.path.join(base, "Yuhub", "update")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def package_path(info):
    """更新包在本地的工作路径（带版本号，便于识别）。"""
    safe = re.sub(r"[^\w.\-]", "_", info.tag or "latest")
    return os.path.join(update_dir(), "Yuhub-%s.exe" % safe)


def _url_allowed(url):
    """更新地址是否允许下载。

    默认**只允许 https**（防中间人把更新包降级到明文）。
    唯一例外：`http://127.0.0.1` / `localhost` 回环地址——
    自检用本地 http.server 伪装更新源时必须走这条路，
    而回环流量不出本机，不存在被劫持的问题。
    """
    u = (url or "").lower()
    if u.startswith("https://"):
        return True
    if u.startswith("http://127.0.0.1") or u.startswith("http://localhost"):
        return True
    return False


# ---------------------------------------------------------------- 下载源与探测
# 国内直连 github.com（下载还要再跳 objects.githubusercontent.com）经常
# 连不上或 DNS 挂起。下载源按顺序尝试：官方直连 → 中转镜像。
# 镜像只是给原 URL 加前缀，文件字节完全一致——info.sha256 校验仍然生效，
# 被篡改的镜像过不了校验，所以这里不存在安全降级。
#
# 镜像列表为 2026-10-01 实测存活的（Range 探测返回 206）；
# 社区镜像会周期性失效，失效的源会被限时探测自动跳过。
DOWNLOAD_MIRRORS = ("ghfast.top", "gh-proxy.com", "ghproxy.net")
API_MIRRORS = DOWNLOAD_MIRRORS          # 镜像同样能代理 api.github.com 路径


def _download_candidates(asset_url):
    """生成候选下载地址：官方直连 → 镜像。"""
    urls = [asset_url]
    tail = asset_url.split("://", 1)[1] if "://" in asset_url else ""
    if tail.startswith("github.com/"):
        for m in DOWNLOAD_MIRRORS:
            urls.append("https://%s/https://%s" % (m, tail))
    return urls


def _api_attempts(api_base):
    """生成候选 API 端点：(base, proxy_mode) 序列。

    官方直连 → 镜像（前缀代理）→ 无代理直连。最后那步是给
    「系统代理配置了但代理软件已死」的用户兜底——这种情况下走系统
    代理的所有请求都会卡死，直连反而通。
    """
    base = api_base.rstrip("/")
    attempts = [(base, None)]
    tail = base.split("://", 1)[1] if "://" in base else ""
    if tail.startswith("api.github.com"):
        for m in API_MIRRORS:
            attempts.append(("https://%s/%s" % (m, tail), None))
        attempts.append((base, "direct"))
    else:
        attempts.append((base, "direct"))
    return attempts


def format_progress(payload):
    """把 on_progress 回调的载荷统一成**一句人类可读的进度文本**。

    download_update 的进度回调有两种形态（历史协议，主窗口的
    emit_progress 依赖它，不能随便改）：
      ① 探测 / 换线阶段 → {"status_text": "...", "phase": "probing"}
      ② 下载进行中     → downloader 的 snapshot 字典
    调用方要是直接 str(payload)，界面上就会糊出一坨 JSON——
    用户实际截图反馈的就是这个（设置页「修复软件」里那串大括号）。

    统一在这里转，避免每个调用点各写一遍、各错一遍。
    返回 (phase, text)：phase ∈ {"probing", "download", ""}。
    """
    try:
        import downloader
    except Exception:  # noqa: BLE001
        return "", ""
    if not isinstance(payload, dict):
        return ("probing", str(payload)) if payload else ("", "")

    if payload.get("phase") == "probing":
        return "probing", str(payload.get("status_text") or "正在连接更新源…")

    # 容忍两种包装：裸 snapshot，或 {"snap": {...}}
    snap = payload.get("snap")
    if not isinstance(snap, dict):
        snap = payload
    if not any(k in snap for k in ("status", "downloaded", "percent")):
        text = payload.get("status_text")
        return ("probing", str(text)) if text else ("", "")

    status = snap.get("status") or "running"
    total = snap.get("total") or 0
    got = snap.get("downloaded") or 0

    if status == downloader.STATUS_DONE:
        return "download", "下载完成：%s" % downloader.human_bytes(got)
    if status == downloader.STATUS_ERROR:
        return "download", "下载失败：%s" % (snap.get("error") or "未知错误")
    if status == downloader.STATUS_CANCELLED:
        return "download", "已取消"
    if status == downloader.STATUS_PROBING:
        return "probing", "正在探测下载线路…"

    parts = []
    if total:
        parts.append("%.1f%%" % (got * 100.0 / total))
    parts.append("%s / %s" % (downloader.human_bytes(got),
                              downloader.human_bytes(total) if total else "未知"))
    if snap.get("cancelling"):
        parts.append("正在取消…")
    else:
        speed = snap.get("speed") or 0
        if speed:
            parts.append(downloader.human_speed(speed))
        eta = snap.get("eta")
        if eta:
            parts.append("剩余 " + downloader.human_time(eta))
    msgs = snap.get("messages") or []
    if msgs:
        parts.append(str(msgs[-1]))
    return "download", "　·　".join(parts)


def _probe_line(url, timeout=15.0, proxy=None, cancel=None):
    """探测一条下载线路，**取消以 0.1 秒粒度生效**。

    返回：ProbeResult（探测完成）/ None（超时）/ "CANCELLED"（用户取消）。
    探测放在守护线程里跑（DNS 挂起不受 socket 超时控制，必须可遗弃），
    主流程每 0.1 秒轮询一次取消标志——用户点取消后最多 0.1 秒就响应，
    这是"取消优先级要很高"的关键。
    """
    box = {}

    def _job():
        try:
            import downloader
            box["res"] = downloader.probe(url, proxy=proxy)
        except Exception as exc:  # noqa: BLE001
            try:
                import downloader
                box["res"] = downloader.ProbeResult(url=url, ok=False,
                                                    error=str(exc))
            except Exception:
                pass

    t = threading.Thread(target=_job, name="YuhubUpdProbe", daemon=True)
    t.start()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cancel is not None and cancel():
            return "CANCELLED"
        if not t.is_alive():
            return box.get("res")
        time.sleep(0.1)
    return None


def download_update(info, on_progress=None, cancel=None, threads=None):
    """下载更新包。返回 (ok, message, path)。

    v0.8.5beta：下载线路矩阵 = 官方直连 + 三个中转镜像 × 两种网络路径
    （系统代理 / 无代理直连），每条线路限时探测 15 秒，谁通用谁。
    覆盖两类用户：
      * 纯国内网络（无代理）→ 直连 github 不通，镜像通；
      * 系统代理配置了但代理软件已死 → 走系统代理的全挂，直连兜底。
    取消在每个阶段边界即时生效（v0.8.3beta 行为保留）。
    下载后的 sha256 校验对所有线路同样生效，安全性不打折。
    """
    if not info or not info.asset_url:
        return False, "没有可下载的更新包", ""
    if not _url_allowed(info.asset_url):
        return False, "更新地址不是 https，已拒绝", ""

    try:
        import downloader
    except Exception as e:  # noqa: BLE001
        return False, "下载模块不可用：%s" % (e,), ""

    dest = package_path(info)
    last_err = ""

    # 清掉上次失败 / 崩溃留下的半成品：旧 `.part` 会一直占着几十 MB，
    # 而被句柄占用改名的 `.part.bad` 更是永远没人收拾（用户反馈过
    # 更新目录里躺着两个 59MB 的残包）。
    for stale in (dest + ".part", dest + ".part.bad"):
        try:
            if os.path.exists(stale):
                os.remove(stale)
        except OSError:
            pass

    def cancelled():
        return bool(cancel and cancel())

    def report_status(text):
        # 探测/换线阶段：UI 的进度条转忙碌动画（snapshot 里带 phase 标记）
        if on_progress:
            try:
                on_progress({"status_text": text, "phase": "probing"})
            except Exception:
                pass

    mode_label = {None: "系统代理", "direct": "直连"}
    candidates = _download_candidates(info.asset_url)
    total = len(candidates) * 2
    tries = 0
    for url in candidates:
        for mode in (None, "direct"):
            tries += 1
            if cancelled():
                return False, "已取消", ""
            if tries > 2:
                src = "官方源" if url == candidates[0] else "镜像"
                report_status("自动切换下载线路（%d/%d）：%s · %s…"
                              % (tries - 1, total, src,
                                 mode_label.get(mode, "默认")))
            else:
                report_status("正在连接更新源…")

            # ---- ① 限时探测：拿大小/分块支持，同时筛掉连不上的线路 ----
            pres = _probe_line(url, timeout=15.0, proxy=mode, cancel=cancel)
            if pres == "CANCELLED" or cancelled():
                return False, "已取消", ""
            if pres is None:
                last_err = "连接超时（已自动换线，共 %d 条线路）" % total
                continue
            if not pres.ok:
                last_err = pres.error or "连接失败"
                continue

            # ---- ② 下载（探测结果直接传入，任务内不再重复探测） ----
            task = downloader.DownloadTask(url, dest, threads=threads,
                                           probe_result=pres, proxy=mode)
            task.start()
            asked = False
            while task.running:
                snap = task.snapshot()
                if on_progress:
                    try:
                        on_progress(snap)
                    except Exception:
                        pass
                if cancel and not asked and cancel():
                    asked = True
                    task.cancel()
                time.sleep(0.2)
            snap = task.snapshot()
            if on_progress:
                try:
                    on_progress(snap)
                except Exception:
                    pass

            if snap["status"] == downloader.STATUS_CANCELLED:
                return False, "已取消", ""
            if snap["status"] != downloader.STATUS_DONE:
                last_err = snap["error"] or "下载失败"
                continue                 # 这条线路不行，换下一条

            # ---- ③ 校验（大小 + sha256，镜像下载的字节同样要过这一关） ----
            if not os.path.isfile(dest):
                return False, "下载完成但文件不存在", ""
            if info.asset_size and os.path.getsize(dest) < info.asset_size:
                return False, ("文件不完整（%d / %d 字节）"
                               % (os.path.getsize(dest), info.asset_size)), ""
            if not verify_sha256(dest, info.sha256):
                try:
                    os.remove(dest)
                except OSError:
                    pass
                return False, "校验失败，文件可能已损坏或被篡改，已删除", ""

            # ④ 体检：sha256 拿不到时（老 release 没 digest）这条路是
            #    唯一的把关；拿得到时它也是最后一道，确保交给替换器的
            #    一定是本机能跑的真程序。不合格的当场删掉，免得下次
            #    「继续下载」时又把这个坏文件当成已完成的任务。
            fine, why = check_package_runnable(dest)
            if not fine:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                return False, why, ""
            return True, "下载完成", dest

    return False, last_err or "所有下载线路均不可达，请检查网络后重试", ""


# ================================================================ 进程工具

def _pid_alive(pid):
    """PID 是否还活着（OpenProcess + WaitForSingleObject）。"""
    if not pid:
        return False
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x102
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.WaitForSingleObject.restype = ctypes.c_ulong
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(SYNCHRONIZE, 0, int(pid))
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


def wait_pid_exit(pid, timeout=WAIT_OLD_PID_TIMEOUT):
    """等 PID 退出。超时返回 False（调用方决定是否强杀）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.25)
    return False


def kill_pid(pid):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                       capture_output=True, creationflags=_CREATE_NO_WINDOW,
                       timeout=15)
    except Exception:
        pass


def current_exe():
    """当前运行的可执行文件路径（打包后就是 Yuhub.exe）。

    源码态用 argv[0] 当"自己"，但 `python -c "..."` 这类调用方式下
    argv[0] 就是字面量 `-c`——一个根本不存在的文件。调用方（拷一份自己
    当替换器、自检里拷一份当"要替换的目标"）拿到这种路径只会
    报「系统找不到指定的文件」，看不出问题出在哪。所以不成立时
    退回解释器路径，与 sys_executable() 同一套判据。
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    arg0 = sys.argv[0] if sys.argv else ""
    if arg0 and os.path.isfile(arg0):
        return os.path.abspath(arg0)
    return os.path.abspath(sys.executable)


def sys_executable():
    """应该被替换 / 被启动的那个 exe。

    打包后就是 Yuhub.exe 本身。源码态没有"exe 可替换"这回事，
    返回当前解释器路径（调用方通常会发现无法替换而放弃）——
    但绝不能返回 `-c` 这种 argv 残留，否则替换器会去改一个不存在的文件。
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    arg0 = sys.argv[0] if sys.argv else ""
    if arg0 and os.path.isfile(arg0):
        return os.path.abspath(arg0)
    return os.path.abspath(sys.executable)


# ================================================================ 替换请求

def build_request(new_exe, target_exe, old_pid, new_version="",
                  launch_args=None, backup=True):
    """构造传给替换器的载荷（会被 base64 编码）。"""
    return {
        "new_exe": os.path.abspath(new_exe),
        "target_exe": os.path.abspath(target_exe),
        "old_pid": int(old_pid or 0),
        "new_version": new_version,
        "launch_args": list(launch_args or []),
        "backup": bool(backup),
    }


def encode_payload(payload):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_payload(b64):
    return json.loads(base64.b64decode(b64.encode("ascii")).decode("utf-8"))


def stage_updater(payload):
    """把「当前的自己」拷一份到临时目录当替换器，返回副本路径。

    ⚠️ 必须拷贝，不能直接用自己：替换器不能是它要替换的那个文件
    （自己锁着自己，改名后行为不可预期）。

    PyInstaller onefile 的 exe 拷到别处能独立运行（依赖解到 _MEIxxxx），
    已实测 22/22 自检通过，所以这条路是通的。

    ⚠️ 残留清理（2026-10-13）：每次更新会在 %TEMP% 留一个 ~50MB 的目录。
    替换器成功时自己会删（_cleanup_self），但**启动失败 / 被杀 / 崩**时
    就永远留着 —— 用户机器上已积压 2 个共 100MB。这里在拷贝前先扫
    一遍，把「本进程之外的、看起来没在跑的」旧目录清掉。
    只删名字符合 `Yuhub_upd_<数字>` 的目录，且**跳过自己那个**，
    不碰任何其他临时文件。
    """
    me = current_exe()
    _sweep_stale_updater_dirs(keep_pid=os.getpid())

    d = os.path.join(tempfile.gettempdir(), "Yuhub_upd_%d" % os.getpid())
    os.makedirs(d, exist_ok=True)
    upd = os.path.join(d, "Yuhub_updater.exe")
    try:
        shutil.copy2(me, upd)
    except Exception:
        # 同盘拷贝一般没问题；失败时退回一个更短的名字再试
        upd = os.path.join(tempfile.gettempdir(), "Yuhub_upd_%d.exe" % os.getpid())
        shutil.copy2(me, upd)
    return upd


def _sweep_stale_updater_dirs(keep_pid=None, max_age=1800.0):
    """清理 %TEMP% 下残留的替换器目录（保留 keep_pid 那个）。返回清理个数。

    判据三条同时成立才删，避免误删正在用的：
      * 目录名匹配 `Yuhub_upd_<pid>` 或 `Yuhub_upd_<pid>.exe` 形态；
      * 不是本次要用的（keep_pid）；
      * mtime 超过 max_age 秒（默认 30 分钟，给正在跑的替换器留足时间）。

    删不掉的（文件还被占用）静默跳过 —— 真正的清理由替换器自己
    在成功路径末尾完成，这里只是兜底。
    """
    tmp = tempfile.gettempdir()
    now = time.time()
    removed = 0
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0
    for name in names:
        if not name.startswith("Yuhub_upd_"):
            continue
        tail = name[len("Yuhub_upd_"):].split(".")[0]
        if not tail.isdigit():
            continue
        if keep_pid is not None and int(tail) == int(keep_pid):
            continue
        path = os.path.join(tmp, name)
        try:
            if (now - os.path.getmtime(path)) < max_age:
                continue                  # 太新，可能正在替换
        except OSError:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def launch_updater(updater_path, payload, elevated=False):
    """拉起替换器进程。返回 (ok, message)。

    elevated=True 时走 ShellExecuteW("runas") —— 替换器副本带
    requireAdministrator 清单时**必须**走这条，见 needs_elevation 的说明。

    ⚠️ elevated=False 但启动仍被 Windows 拒绝（WinError 740）时，
    自动回退到提权重试一次。以前这里是直接抛异常 → 消息弹一下 →
    用户看到「窗口关了、版本没变」，自动更新等于静默失效。
    """
    args = ["--apply-update", encode_payload(payload)]

    if elevated:
        return _runas_launch(updater_path, args)

    try:
        subprocess.Popen([updater_path] + args,
                         creationflags=_CREATE_NO_WINDOW,
                         close_fds=True)
        return True, "更新程序已启动"
    except OSError as e:
        # 740 = ERROR_ELEVATION_REQUIRED：exe 自带 requireAdministrator，
        # 非提权 CreateProcess 被拒。这是本项目的常态（uac_admin=True），
        # 所以这里必须自动升级为提权启动，而不是把失败抛给用户。
        if getattr(e, "winerror", None) == 740:
            return _runas_launch(updater_path, args)
        return False, "启动更新程序失败：%s" % (e,)
    except Exception as e:  # noqa: BLE001
        return False, "启动更新程序失败：%s" % (e,)


def exe_requires_elevation(exe_path):
    """这个 exe 自身是否带 requireAdministrator 清单。

    ⚠️ 这与「目标目录能不能写」是**两件事**，混起来会让自动更新 100% 失效
    （2026-10-13 用户实测：下载完成、程序关闭、版本没变）。

    Yuhub.spec 里 `uac_admin=True`，产物清单写死了
    `<requestedExecutionLevel level="requireAdministrator" />`
    —— 也就是**任何进程启动它都必须提权**，哪怕它就躺在可写的桌面目录上。
    原来的 `needs_elevation()` 只探测目录可写性，对桌面返回 False，
    于是走 `subprocess.Popen`（不带 runas）去启动这个 exe，
    Windows 直接拒绝：**WinError 740「请求的操作需要提升」**。
    替换器根本没起来，而主窗口已经打出「正在更新」并开始退出，
    用户看到的就是「下载完了、窗口关了、打开还是旧版」。

    判据：读 PE 的 manifest。做法是搜 `requestedExecutionLevel` 附近的
    `level="..."`，比解析完整清单轻量得多，且对 PyInstaller 产物可靠
    （PyInstaller 把 XML 清单作为 RT_MANIFEST 资源嵌进去）。
    读不到 / 不是 PE 一律返回 True —— 宁可多弹一次 UAC，
    也不能让更新静默失败。
    """
    if not exe_path or not os.path.isfile(exe_path):
        return False
    try:
        with open(exe_path, "rb") as f:
            data = f.read()
    except OSError:
        return True                     # 读不到就按需要提权处理（保守）

    # 快速排除：不是 PE 就不是我们要判断的东西
    if b"requestedExecutionLevel" not in data:
        return False
    idx = data.find(b"requestedExecutionLevel")
    window = data[idx:idx + 200].lower()
    return b"requireadministrator" in window or b"highestavailable" in window


def needs_elevation(target_exe, updater_exe=None):
    """替换流程是否必须提权。

    两个来源，任一成立就得提权：
      * `updater_exe`（替换器副本）自身带 requireAdministrator 清单
        —— 这是**常态**，因为它就是本程序自己的副本；
      * `target_exe` 所在目录不可写（如装在 Program Files）。

    ⚠️ 旧实现只看第二条，在桌面/下载目录这种可写位置会漏判，
    而恰恰此时替换器副本仍要求提权 —— 于是必撞 740。
    `updater_exe` 传 None 时退回只判目录（保持旧语义，供自检使用）。
    """
    if updater_exe and exe_requires_elevation(updater_exe):
        return True
    d = os.path.dirname(os.path.abspath(target_exe)) or "."
    probe = os.path.join(d, ".yuhub_write_test_%d" % os.getpid())
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return False
    except OSError:
        return True


def _quote_win_arg(a):
    r"""把一个参数编码成 Windows 命令行里可安全传递的形式（加引号）。

    ⚠️ 这个项目的两个参数里都有必须引起来的东西：base64 载荷含
    `+` `/` `=`（尾部 `=` 尤其易被吃掉），而 Windows 路径
    （形如 C:\Users\<用户名>\AppData\Local\...）含空格。
    裸拼给 ShellExecuteW 会被 CommandLineToArgvW 按空格拆词，
    替换器收到残缺载荷 → return 2 → 静默不更新。

    规则（与 CRT / CommandLineToArgvW 一致）：整体加双引号，
    内部出现的 `"` 与紧邻的反斜杠按 Windows 规则成对转义。
    这里参数都是我们自己生成的（不含引号），但仍按完整规则实现，
    避免以后有人往 launch_args 里塞带引号的值时静默出错。

    ⚠️ 本 docstring 用 r 前缀：里面的 C:\Users 不是转义序列。
    """
    a = str(a)
    out = ['"']
    n_bs = 0                      # 连续反斜杠计数
    for ch in a:
        if ch == "\\":
            n_bs += 1
            continue
        if ch == '"':
            # n 个反斜杠 + 一个引号 → 2n 个反斜杠 + 一个转义引号
            out.append("\\" * (n_bs * 2 + 1))
            out.append('"')
            n_bs = 0
            continue
        if n_bs:
            out.append("\\" * n_bs)
            n_bs = 0
        out.append(ch)
    # 收尾：结尾的反斜杠要双写，否则会转义掉闭合引号
    if n_bs:
        out.append("\\" * (n_bs * 2))
    out.append('"')
    return "".join(out)


def _runas_launch(exe_path, args):
    """用 ShellExecuteW(runas) 提权启动 exe。返回 (ok, message)。

    ⚠️ 旧实现把参数用空格裸拼成字符串传给 lpParameters，
    base64 载荷（含 `+` `/` `=`）与带空格的路径都会被拆词，
    替换器收到残缺载荷直接 return 2。现在逐个参数加引号（见
    _quote_win_arg），这是 lpParameters 唯一可行的传参方式。
    """
    SW_SHOWNORMAL = 1
    params = " ".join(_quote_win_arg(a) for a in args)
    try:
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe_path, params, None, SW_SHOWNORMAL)
        if int(r) > 32:
            return True, "已请求管理员权限启动更新"
        # 5 = ERROR_ACCESS_DENIED，最常见的是用户在 UAC 上点了「否」
        return False, "提权启动被拒绝（请在弹出的 UAC 窗口点「是」）"
    except Exception as e:  # noqa: BLE001
        return False, "提权启动异常：%s" % (e,)


# ================================================================ 替换器侧

def updater_log_path():
    """替换器日志路径：%LOCALAPPDATA%\\Yuhub\\update\\replace.log。

    ⚠️ 为什么必须有（2026-10-13 事故）：替换器是**独立的 GUI 子系统进程**，
    它的一举一动对用户完全不可见 —— 窗口关了、版本没变，没有任何提示。
    以前出问题时现场只剩两个 %TEMP% 残留目录，要靠推断才能定位。
    现在每一步都写这里，下次再坏可以直接看这个文件。
    """
    d = update_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(d, "replace.log")


_RC_MEANING = {
    0: "成功：旧版已备份、新版已就位并启动",
    2: "载荷解不开（base64/json 损坏）",
    3: "文件缺失或路径非法（新包或目标 exe 不存在）",
    4: "旧进程不退出，放弃替换（未动任何文件）",
    5: "旧 exe 改名失败（权限不足或被占用）",
    6: "新包就位失败，已回滚",
    7: "新包启动失败，已回滚",
    8: "新版秒退且非 0，已回滚",
    9: "新包体检不合格，未动任何文件",
}


def _ulog(msg):
    """写替换器日志（尽力而为，任何异常都不能影响替换主流程）。"""
    p = updater_log_path()
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s [%d] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                      os.getpid(), msg))
    except OSError:
        pass


def apply_update_main(b64_payload):
    """`Yuhub.exe --apply-update <base64-json>` 模式（在 main() 里早返回）。

    职责：体检新包 → 等旧进程退出 → 两步改名替换 → 启动新版 →
    观察是否秒退 → 必要时回滚。

    返回进程退出码：
        0  成功
        2  载荷解不开
        3  文件缺失 / 路径非法
        4  旧进程不退
        5  旧 exe 改名失败（权限/占用）
        6  新包就位失败（已回滚）
        7  新包启动失败（已回滚）
        8  新版秒退且非 0（已回滚）
        9  **新包体检不合格，未动任何文件**（损坏 / 架构不符）
    """
    _ulog("替换器启动：argv 载荷长度=%d" % len(b64_payload or ""))
    try:
        p = decode_payload(b64_payload)
    except Exception as exc:  # noqa: BLE001
        _ulog("载荷解码失败：%r -> rc=2" % (exc,))
        return 2

    new_exe = p.get("new_exe") or ""
    target = p.get("target_exe") or ""
    old_pid = int(p.get("old_pid") or 0)
    do_backup = bool(p.get("backup", True))
    launch_args = p.get("launch_args") or []

    _ulog("目标=%s 新包=%s 旧PID=%s 备份=%s"
          % (target, new_exe, old_pid, do_backup))

    if not new_exe or not target or not os.path.isfile(new_exe):
        _ulog("前置校验失败：新包存在=%s -> rc=3"
              % (os.path.isfile(new_exe) if new_exe else False))
        return 3

    # ---- ⓪ 体检新包：不合格就地返回，一个文件都不动 ----
    # 放在改名之前是关键。若等到第 ④ 步才由 Popen 发现文件是坏的，
    # 旧的 exe 已经被改名、坏的已经就位——那是"先砸了再修"；
    # 而且 Windows 会在此时弹「不支持的 16 位应用程序」系统对话框。
    # 这里提前拦下，用户的机器全程保持原样。
    silence_loader_dialogs()               # 兜底：之后任何失败都不弹框
    fine, why = check_package_runnable(new_exe)
    if not fine:
        _ulog("新包体检不合格：%s -> rc=9" % (why,))
        return 9
    _ulog("新包体检通过：%d 字节" % (os.path.getsize(new_exe),))

    # ---- ① 等旧进程彻底退出 ----
    if old_pid and _pid_alive(old_pid):
        _ulog("旧进程 %d 仍在，等待退出…" % old_pid)
        if not wait_pid_exit(old_pid, WAIT_OLD_PID_TIMEOUT):
            _ulog("等待超时，强杀旧进程 %d" % old_pid)
            kill_pid(old_pid)             # 超时不退就强杀
            if not wait_pid_exit(old_pid, 5.0):
                _ulog("强杀后旧进程仍存活 -> rc=4（不动文件）")
                return 4                  # 还是活着，放弃（不动文件，保证可回退）
    _ulog("旧进程已退出")

    bak = target + ".bak"

    # ---- ② 清理上次可能残留的 .bak ----
    # 上次更新成功后 .bak 可能因为文件句柄没释放而删不掉，这里补删。
    if os.path.exists(bak):
        try:
            os.remove(bak)
            _ulog("删除残留 .bak：成功")
        except OSError:
            # 删不掉就改个名让开位置，避免下面 rename 失败
            try:
                os.replace(bak, bak + ".%d" % int(time.time()))
                _ulog("残留 .bak 改名让位：成功")
            except OSError as e:
                _ulog("残留 .bak 让位也失败 winerror=%s" % (e.winerror,))

    # ---- ③ 两步改名（本方案的核心）----
    # 运行中的 exe 不能覆盖写，但可以改名（只改目录项，不动内容）。
    try:
        if do_backup:
            os.rename(target, bak)        # 旧 → .bak
        else:
            os.remove(target)
        _ulog("③a 旧版改名/删除：成功（备份=%s）" % do_backup)
    except OSError as e:
        _ulog("③a 失败 winerror=%s %s -> rc=5" % (e.winerror, e))
        return 5                          # 连改名都失败：权限/占用，保持原样退出

    try:
        os.replace(new_exe, target)       # 新 → 原路径
        _ulog("③b 新包就位：成功")
    except OSError as e:
        # 新文件就位失败 → 回滚旧版，保证用户至少还能用
        _ulog("③b 失败 winerror=%s %s -> 回滚 -> rc=6" % (e.winerror, e))
        try:
            if do_backup and os.path.exists(bak):
                os.replace(bak, target)
                _ulog("回滚成功")
        except OSError:
            pass
        return 6

    # ---- ④ 启动新版 ----
    try:
        proc = subprocess.Popen([target] + list(launch_args),
                                cwd=os.path.dirname(target) or None,
                                close_fds=True)
        _ulog("④ 新版已启动 pid=%d" % (proc.pid,))
    except Exception as e:
        # 打不开新版 → 回滚
        _ulog("④ 启动新版异常 %r -> 回滚 -> rc=7" % (e,))
        _rollback(bak, target, do_backup)
        return 7

    # ---- ⑤ 观察窗口：新版秒退且非 0 → 判定有问题，自动回滚 ----
    # 这是"更新把自己搞挂"的最后一道保险。
    try:
        code = proc.wait(timeout=NEW_EXE_GRACE)
    except subprocess.TimeoutExpired:
        code = None                       # 活着超过宽限期，认为正常

    if code is not None and code != 0:
        _ulog("⑤ 新版 %s 秒内退出，码=%s -> 回滚 -> rc=8"
              % (NEW_EXE_GRACE, code))
        try:
            proc.kill()
        except Exception:
            pass
        if _rollback(bak, target, do_backup):
            try:
                subprocess.Popen([target] + list(launch_args),
                                 cwd=os.path.dirname(target) or None,
                                 close_fds=True)
            except Exception:
                pass
        return 8

    # ---- ⑥ 成功：删掉 .bak（删不掉也无害，下次启动再删）----
    if do_backup:
        for _ in range(5):
            try:
                os.remove(bak)
                _ulog("⑥ 删除 .bak：成功")
                break
            except OSError:
                time.sleep(0.4)

    # ---- ⑦ 清理自己的临时目录 ----
    _cleanup_self()

    _ulog("替换完成 rc=0 —— %s" % (_RC_MEANING.get(0),))
    return 0


def _rollback(bak, target, do_backup):
    """把 .bak 还原成 target。返回是否成功。"""
    if not do_backup or not os.path.exists(bak):
        return False
    try:
        os.replace(bak, target)
        return True
    except OSError:
        return False


def _cleanup_self():
    """删除替换器自己所在的临时目录（尽力而为）。

    进程自己占着 exe 文件，直接删目录会失败，所以让开一个
    cmd 延时删除；失败也无害，%TEMP% 会被系统回收。
    """
    try:
        me = current_exe()
        d = os.path.dirname(me)
        if os.path.basename(d).startswith("Yuhub_upd_"):
            subprocess.Popen(
                ["cmd", "/c", "ping", "127.0.0.1", "-n", "3", ">nul",
                 "&", "rd", "/s", "/q", d],
                creationflags=_CREATE_NO_WINDOW,
                close_fds=True)
    except Exception:
        pass
