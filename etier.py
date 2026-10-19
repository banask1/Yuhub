"""内嵌 EasyTier：跨网组网的"蓝盾体验"，零自建服务器。

## 它在 Yuhub 里的角色

EasyTier（开源 Apache 2.0，Rust）会在本机创建一块**二层虚拟网卡**，
把输入了相同"网络名+密码"的异地电脑拉进同一个虚拟局域网——广播可以
跨网传播，所以 Yuhub 现有的"局域网联机"模式（抓宣告 + 代理端口）
在虚拟网上**原样就能工作**。

分工：
    EasyTier  负责"把两台电脑变成同一个局域网"（打洞 + 社区公共节点中继兜底）
    Yuhub     负责壳：房间码/密码管理、启动/停止、状态显示

## 三个实现要点（都踩过验证过）

1. **提权不可避免**：创建虚拟网卡（wintun）必须管理员权限。Yuhub 主程序
   保持普通权限运行，启动 EasyTier 时用 `ShellExecuteW("runas")` 弹一次 UAC。

2. **提权进程收不到管道输出，普通进程也杀不掉它**。解法是"看门狗"：提权
   启动的不是 easytier 本体，而是 `Yuhub.exe --watchdog` 的一个新实例
   （同一个 GUI 子系统 exe），它用 `CREATE_NO_WINDOW` 标志启动
   easytier-core（避开 conhost 闪烁），再轮询一个**停止信号文件**；
   Yuhub 侧写这个文件，watchdog 看到就 taskkill easytier-core 并自删信号。
   **启动时弹一次 UAC，停止时零弹窗，看门狗期间零窗口闪烁**。

3. **状态靠探测，不靠输出**：提权进程的 stdout 我们拿不到。可靠的就绪判据是
   「系统里出现了虚拟网卡且拿到了 10.126.126.0/24 的地址」——用
   GetIpAddrTable 枚举（见 _ip_addresses）。

## 为什么 watchdog 用 Yuhub.exe，而不是 wscript + VBScript 或 PowerShell

上一版用 wscript.exe 跑 .vbs（`WScript.Shell.Run` 启动 easytier-core），
用户实测看到有窗口反复闪烁，路径指向 `C:\\Windows\\System32`——那是
Windows 给 easytier-core（控制台子系统）分配的 conhost 子进程窗口。
即使窗口模式设 0，**只要创建了 conhost，Windows 在创建/隐藏之间就有
那一瞬的可见窗口**，反复出现就形成"闪烁"。

- PowerShell 也同理，且 `-WindowStyle Hidden` 会让脚本卡住不执行；
- wscript 路径看上去"无控制台"，但子进程窗口层面没法控；
- 让 GUI 子系统的可执行文件（Yuhub.exe 自己）担任 watchdog，再用
  Win32 `CREATE_NO_WINDOW` 标志启动子进程，**根本不让 Windows 创建
  conhost**，从根上消灭闪烁。

参数传递走 base64+JSON：避免命令行引号 + 空格 + 特殊字符的转义陷阱。

## 二进制

resources/easytier/ 内含三个文件（最小依赖集，实测确定）：
    easytier-core.exe  核心（24 MB）
    wintun.dll         虚拟网卡用户态驱动（知名开源，无恶意软件误报风险）
    Packet.dll         core 启动时即加载，缺了直接 0xC0000135

首次使用时释放到 %LOCALAPPDATA%\\Yuhub\\etier\\——**从固定目录运行**，
而不是 PyInstaller 的 _MEIxxxx 临时目录：临时目录随主程序退出被删，
而提权进程可能还引用着那里的文件。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

# Win32 CREATE_NO_WINDOW = 0x08000000：禁止 Windows 给控制台子系统子进程
# 创建 conhost.exe 窗口。我们是 PyInstaller --windowed 打包的 GUI 程序，
# 本身没有控制台，每个 subprocess.run("arp" / "tasklist" / "route" /
# "taskkill" ...) 都会触发 Windows 偷偷创建一个 conhost，在创建/销毁
# 的几百毫秒里那窗口会一闪而过——Yuhub 的 _tick 每 800ms 调一次
# list_members() / core_running()，于是用户看到的就是"C:\Windows\System32
# 路径下的白色窗口反复闪烁"。**所有调控制台子进程的代码**都要带这个 flag。
_CREATE_NO_WINDOW = 0x08000000

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 中继/会合节点。官方公共节点（public.easytier.cn / public.easytier.top /
# public.kkrainbow.top）2026 年起已从 DNS 摘除（NXDomain，实测确认）。
#
# 下面这一批是 v0.9beta 重新实测选出来的：用随包携带的 easytier-core 真起一个
# 实例（--no-listener --no-tun，只连节点不建网卡），把日志里出现
# 「new peer added」当作握手成功——只测 TCP 端口通不通是不够的，
# 端口开着但中继拒绝服务的节点会骗过单纯的连通性检查。
# 每个候选连 3~5 次，只保留**每次都成功**的。
#
# 这次实测淘汰掉的：
#   udp://us01.225284.xyz:11010  海波美国（UDP 口已失效，改成 tcp:// 后 5/5 通过）
#   tcp://et.fpfast.top:48625    域名解析失败
#   tcp://161.33.207.13:51010    风起社公益节点（2026-08 迁到日本后已不可达）
#   tcp://et.gbc.moe:11010       端口不通
#   tcp://8.148.29.206:11010     北京节点，端口已关闭
#   c.oee.icu / weior.top / xhrj.324406.xyz / 47.96.28.190 等一批社区列表里的
#   节点也都已失效——网上流传的节点表普遍过期，必须现测现用。
#
# 2026-10-18 又测了一批（这次是为了排查"异地联机连不上"）：
#   * 官方 `public.easytier.cn / .top / public2.easytier.cn` **DNS 全部 NXDomain**
#     —— README 里那套"连官方公共节点"的新手教程在当前已经走不通了，
#     上面首批实测的结论「官方节点 2026 年起已从 DNS 摘除」依然成立。
#   * 当前表 14 个里 **9 个通、3 个不通**（et-hk.clickor.click 的 tcp/ws 超时、
#     sh.vomiku.com 的 wss 拒绝连接）；固件随时间失效，界面下拉会实时测延迟。
#   * 社区新表里另外筛出 2 个能用的，已加进 PUBLIC_SERVERS：
#     183.230.36.171:11010（42ms）、96.9.229.212:11010（313ms）。
#   * 官方 README 的另一个要点仍然有效：**多节点互为冗余**——只要任一节点
#     把双方都拉进来，peer 信息就会在网络内扩散，不必每台机器都连同一个。
#
# 节点会失效，连接失败会自动换下一个。
PUBLIC_SERVERS = (
    "tcp://easytier.weiai.org.cn:11010",   # 唯爱厦门（十堰），国内最快
    "tcp://47.108.52.1:11012",             # 成都阿里云，IP 直连无 DNS 依赖
    "tcp://225284.xyz:11010",              # 海波中国大陆
    "tcp://ros.scpsl.com.cn:11010",        # 杭州
    "tcp://47.92.208.217:11010",           # 腾讯云
    "tcp://boi.de5.net:11010",             # 张家口阿里云
    "wss://sh.vomiku.com:7912",            # 上海腾讯云（WSS：能穿只放行 443 的网络）
    "tcp://et-hk.clickor.click:11010",     # 中国香港
    "ws://et-hk.clickor.click:11011",      # 中国香港（WS）
    "tcp://dreamlife.indevs.in:11010",     # 海外备用
    "tcp://us01.225284.xyz:11010",         # 海波美国（走 TCP；UDP 口已失效）
    "tcp://38.147.105.178:11010",          # 美国备用（与海波美国同机房）
    # --- 2026-10-18 补测新增（同批筛掉一批已失效的，见下）---
    "tcp://183.230.36.171:11010",          # 国内备线（实测 42ms）
    "tcp://96.9.229.212:11010",            # 海外备线（实测 313ms）
)

# 「中继节点」下拉框的选项（key, 显示名, 地址）。
# 地址为空 = 自动模式：把 PUBLIC_SERVERS 全部传给 EasyTier（-p 可重复），
# 由 EasyTier 自己择优。MCTier 的经验是"节点选择必须具有确定性"——
# 单选节点时不偷换、不叠加其它节点，用户选哪个就连哪个，
# 这样"连不上某个节点"和"房间不通"才不会混在一起难排查。
#
# 下拉里只放**实测最稳、地域最分散**的那些（自动模式另有几个仅作冗余）；
# 名字尽量短，因为每一项后面还要挂上实测延迟。
# 「上海中继（WSS）」「香港中继（WS）」这两条是给受限网络准备的：
# 有些公司网/校园网只放行 WebSocket，走 tcp:// 会被直接掐掉。
NODE_CHOICES = (
    ("auto", "自动（尝试全部节点）", ""),
    ("weiai", "唯爱厦门", "tcp://easytier.weiai.org.cn:11010"),
    ("cn_ip", "国内中继（IP 直连）", "tcp://47.108.52.1:11012"),
    ("cn_bak", "国内备线", "tcp://183.230.36.171:11010"),
    ("haibo_cn", "海波中国大陆", "tcp://225284.xyz:11010"),
    ("hz", "杭州中继", "tcp://ros.scpsl.com.cn:11010"),
    ("txy", "腾讯云中继", "tcp://47.92.208.217:11010"),
    ("zjk", "张家口阿里云", "tcp://boi.de5.net:11010"),
    ("sh_wss", "上海中继（WSS）", "wss://sh.vomiku.com:7912"),
    ("hk", "香港中继", "tcp://et-hk.clickor.click:11010"),
    ("hk_ws", "香港中继（WS）", "ws://et-hk.clickor.click:11011"),
    ("haibo_us", "海波美国", "tcp://us01.225284.xyz:11010"),
)


def node_address(key):
    """按 key 查节点地址；auto / 未知 key 返回空串（= 全部节点）。"""
    for k, _label, addr in NODE_CHOICES:
        if k == key:
            return addr
    return ""

# 虚拟网卡默认网段（EasyTier DHCP 默认从这里分配）
VIRTUAL_NET_PREFIX = "10.126.126."

RESOURCE_DIR_NAME = "easytier"
#: 随包携带的 EasyTier 文件。easytier-cli.exe 是「成员表」这一版新增的：
#: 它和 core 由**同一个 crate** 构建、在官方 zip 里本来就有（构建 workflow
#: 把 easytier-{core,cli,web,web-embed} 四个都打进去），所以我们只是多解压
#: 一个文件——换来的是 core 内部那张**权威成员表**（见 CorePeerFeed）。
BIN_NAMES = ("easytier-core.exe", "easytier-cli.exe", "wintun.dll", "Packet.dll")

#: 缺了也**不拦着启动**的文件：core 照常跑，只是少一条能力（成员表退回
#: ARP + 信标的旁路推断）。老安装包里没有 cli，不能因为这个就把房间堵死。
OPTIONAL_BIN_NAMES = ("easytier-cli.exe",)

#: EasyTier 管理 RPC portal 的端口（core 与 cli 两边都要用同一个）。
#:
#: 🔴 **不能跟着用官方默认的 15888**：core 的 portal 是**无条件启动**的
#: （v2.6.4 的 core.rs 里 `ApiRpcServer::new(...)?.serve().await?` 带 `?`），
#: 绑定失败会让整个 easytier-core 直接退出——用户要是自己装过 EasyTier
#: （或开着它的 GUI），15888 就在别人手里，我们的房间会"起不来"而且看不出
#: 原因。所以启动前先探一个空闲口（pick_rpc_port），再用 --rpc-portal 显式
#: 交给 core，CLI 侧用同一个口，与用户的实例彻底隔离。
RPC_PORT = 15889

# 停止信号文件（普通权限可写，提权看门狗可读）
_STOP_FILE = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub", "etier_stop")


def install_dir():
    """EasyTier 二进制的落地目录（固定，不用 _MEI 临时目录，见模块注释）。"""
    return os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub", "etier")


#: EasyTier 的固定监听端口。
#:
#: 🔴 **这是"异地分屏只有 4 帧"的根因所在**：以前启动参数里带 `--no-listener`
#: （官方含义：**不监听任何端口，只连接到对等节点**；另一页文档补充说它
#: "will affect the establishment of non-hole-punching connections"）。
#: 双方都不监听时，P2P 直连根本没有可打洞的落点，流量只能整体走公共中继
#: —— 而公共节点是社区共享的、常被限速，实测有效吞吐只有约 3.6Mbps，
#: 对应分屏 4fps。改成**固定端口监听**后：端口可预测，UDP/TCP 打洞才有
#: 成立的前提（日志里能看到 `tcp hole punch client start`）。
#: 防火墙不受影响：`_preflight_silence_windows()` 是按**程序**放行的
#: （protocol=any），新端口不会弹窗。
LISTEN_PORT = 11010


def log_dir():
    """EasyTier 自己的日志目录（链路诊断靠解析它）。"""
    return os.path.join(install_dir(), "logs")


def core_args(name, secret, hostname="", ipv4="", servers=(), rpc_port=RPC_PORT):
    """组装 easytier-core 的启动参数。

    抽成独立函数是为了**可测试**：想验证"不许再出现 --no-listener""必须带
    监听端口"这类回归，不该真去起一个需要管理员权限的房间（自检里就断言
    这个返回值，见 lan_selftest）。
    """
    args = [
        "--network-name", name,
        "--network-secret", secret,
        "--latency-first",
        # 管理 RPC portal（成员表的数据源）。core 默认会起在 15888，而那个
        # 口可能被用户自装的 EasyTier 占着——绑定失败会让 core 直接退出，
        # 所以这里**显式**指定一个我们探过的空闲口（见 RPC_PORT / pick_rpc_port）。
        # 绑 127.0.0.1：RPC 只对本机开放，局域网里的别人连不上。
        "--rpc-portal", "127.0.0.1:%d" % int(rpc_port),
        # 固定端口监听（见 LISTEN_PORT 的注释：这是 P2P 直连的前提）
        "-l", "udp://0.0.0.0:%d" % LISTEN_PORT,
        "-l", "tcp://0.0.0.0:%d" % LISTEN_PORT,
        # EasyTier 默认单线程运行（官方 help：default is single-threaded），
        # 单核吞吐就是整条隧道（分屏 + 云盘 + 游戏流量）的天花板。
        # count 必须 > 2。两种写法都带上：官方 help 里是 `--multi-thread`
        # 开关 + `--multi-thread-count` 数字，而 TOML 回显只打印 count，
        # 所以不敢只写一个。
        "--multi-thread", "true",
        "--multi-thread-count", "4",
        # 把隧道里的 TCP 流交给 KCP 承载：官方说明是"提高在 UDP 丢包网络上的
        # 延迟和吞吐量"。异地链路几乎必然丢包，而内层 TCP（分屏、云盘下载）
        # 叠在外层 TCP 上会互相拖累（经典 TCP-over-TCP meltdown）。
        # 对端默认允许 KCP 输入，所以只开我们这侧就够。
        "--enable-kcp-proxy", "true",
        # 捕获物理网卡上的 UDP 广播包并转发给对等节点：让依赖局域网广播发现
        # 房间的游戏（Minecraft 等）能看到彼此。仅 Windows 生效，需要管理员
        # 权限——watchdog 本来就是提权进程。
        "--enable-udp-broadcast-relay", "true",
        # 让 EasyTier 自己写日志：链路诊断（打洞/连接错误）靠解析它，
        # 比往包里再塞一个 easytier-cli.exe（约 10MB）划算得多。
        "--file-log-level", "info",
        "--file-log-dir", log_dir(),
        "--file-log-size", "8",
        "--file-log-count", "2",
    ]
    if hostname:
        # 队友在成员列表里看到的名字（不传就是计算机名 DESKTOP-XXXX）
        args += ["--hostname", hostname]
    if ipv4:
        # 固定虚拟 IP：只给 -i、不加 -d，否则 DHCP 会覆盖它（-d 与 -i 互斥）
        args += ["-i", ipv4]
    else:
        # 不指定 IP 时用 DHCP 自动分配（并自动创建虚拟网卡）
        args += ["-d", "true"]
    for srv in servers:
        args += ["-p", srv]
    return args


#: 链路诊断关心的日志特征（EasyTier 是 Rust tracing，行首带时间戳与模块路径）。
_LINK_PUNCH = ("hole punch", "hole_punch", "打洞")
_LINK_RELAY = ("relay", "relaying", "中继")
_LINK_ERROR = ("connect to peer error", "connecterror", "connection refused",
               "handshake failed", "timeout")


def link_diagnostics(max_lines=4, tail_bytes=262144):
    """读 EasyTier 自己的日志，摘出链路线索。

    返回 {"log": 日志路径, "punch": [...], "relay": [...], "error": [...]}，
    每类最多 max_lines 条（最近优先，已去重）。读不到就全空 —— 调用方据此
    显示"暂无链路日志"，而不是把"没读到"当成"一切正常"。
    """
    out = {"log": "", "punch": [], "relay": [], "error": []}
    try:
        d = log_dir()
        if not os.path.isdir(d):
            return out
        cands = [os.path.join(d, fn) for fn in os.listdir(d)
                 if fn.lower().endswith(".log")]
        if not cands:
            return out
        newest = max(cands, key=lambda p: os.path.getmtime(p))
        out["log"] = newest
        with open(newest, "rb") as f:
            try:
                f.seek(-tail_bytes, os.SEEK_END)
            except OSError:
                f.seek(0)
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return out

    buckets = {"punch": _LINK_PUNCH, "relay": _LINK_RELAY, "error": _LINK_ERROR}
    # 去重集合**必须按分类各一份**：一行同时含 "relay" 和 "connect to peer error"
    # 时两个分类都该收到它。第一版用了全局 seen，第二个分类会因为
    # 「这行已经在 seen 里」被静默丢掉（自检里那条连接错误就是这么漏的）。
    seen = {k: set() for k in buckets}
    for raw in reversed(text.splitlines()):
        low = raw.lower()
        for key, marks in buckets.items():
            if len(out[key]) >= max_lines:
                continue
            if any(m in low for m in marks):
                short = raw.strip()
                if len(short) > 220:                 # 日志行可能极长（带结构体 dump）
                    short = short[:220] + "…"
                if short in seen[key]:
                    continue
                seen[key].add(short)
                out[key].append(short)
    return out



def relay_status(tail_bytes=786432, log_path=None):
    """从 EasyTier 日志统计**当前会话**的中继连接与同伴情况。

    解决的问题：用户反馈"异地联机连不上 / 房间里只有自己"，以前只能靠
    猜。这里直接给出可判定的事实：

        relays_ok      —— 当前会话里**真正握手成功**的中继节点（去重、按
                          `reconnect: <uri> ok, ret: Ok(` 计数）
        relays_failed  —— 超时/被拒的中继节点数
        same_net_peers —— 同网络成员数（new peer added 总数 − foreign_network）
        nat_type       —— 日志里探到的 NAT 类型（若有）

    只看**当前会话**：日志文件是跨会话追加的，上一次房间的连接记录留着会让
    数字虚高，所以从最后一行 "Starting easytier from cli with config" 起算。
    读不到一律返回空（调用方据此显示"暂无日志"，而不是当成"一切正常"）。
    """
    out = {"log": "", "relays_ok": [], "relays_failed": 0,
           "same_net_peers": 0, "foreign_peers": 0, "nat_type": "",
           "session": False}
    try:
        d = log_dir()
        if log_path is None:
            if not os.path.isdir(d):
                return out
            cands = [os.path.join(d, fn) for fn in os.listdir(d)
                     if fn.lower().endswith(".log")]
            if not cands:
                return out
            newest = max(cands, key=lambda p: os.path.getmtime(p))
        else:
            if not os.path.isfile(log_path):
                return out
            newest = log_path
        out["log"] = newest
        with open(newest, "rb") as f:
            try:
                f.seek(-tail_bytes, os.SEEK_END)
            except OSError:
                f.seek(0)
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return out

    # 只保留当前会话（最后一次启动 core 之后的部分）
    marker = "Starting easytier from cli with config"
    pos = text.rfind(marker)
    if pos >= 0:
        text = text[pos:]
        out["session"] = True

    # 成功握手的中继：reconnect: <uri> ok, ret: Ok(...)
    for m in re.finditer(r"reconnect: (\S+) done, ret: Ok\(", text):
        uri = m.group(1).strip().rstrip("/")
        if uri and uri not in out["relays_ok"]:
            out["relays_ok"].append(uri)
    # 失败的中继：connect to peer error
    out["relays_failed"] = len(set(re.findall(
        r'connect to peer error dst="([^"]+)"', text)))
    # 成员：new peer added 总数 vs foreign_network_client（跨网络的旁路连接）
    total_peers = len(re.findall(r"new peer added peer_id=\d+", text))
    out["foreign_peers"] = len(re.findall(
        r"foreign_network_client: add new peer conn", text))
    out["same_net_peers"] = max(0, total_peers - out["foreign_peers"])
    # NAT 类型（EasyTier 启动时会探测，日志里形如 nat_type=...）
    m = re.search(r"nat_type[=:]\s*([A-Za-z0-9_]+)", text)
    if m:
        out["nat_type"] = m.group(1)
    return out


def core_path():
    return os.path.join(install_dir(), "easytier-core.exe")


def cli_path():
    """管理 CLI 的路径（成员表的数据源，见 CorePeerFeed）。

    和 core 同目录、同一个发布包里；没装（或被杀软删了）时成员表退回
    ARP + 信标的旁路推断，不影响联机本身。
    """
    return os.path.join(install_dir(), "easytier-cli.exe")


def cli_health():
    """管理 CLI 是否就位。返回 (ok, detail)。

    ⚠️ 它是**可选件**：缺了只是成员表少一条能力（退回 ARP + 信标），
    联机/云盘/分屏全都不受影响。所以它不参与 core_health()——那条判据
    决定"房间能不能起"，这里只决定"有没有权威成员表"。
    """
    cli = cli_path()
    if not os.path.isfile(cli):
        return False, "缺少 easytier-cli.exe（成员表退回 ARP + 信标）"
    try:
        if os.path.getsize(cli) < 1_000_000:
            return False, "easytier-cli.exe 大小异常（文件可能损坏）"
    except OSError:
        return False, "easytier-cli.exe 读不到"
    return True, cli


def cli_available():
    ok, _ = cli_health()
    return ok


def pick_rpc_port(preferred=RPC_PORT, tries=8):
    """挑一个空闲的本机管理端口（见 RPC_PORT 的注释：默认口会被用户自装的
    EasyTier 占着，而 portal 绑定失败 = core 直接退出）。

    探测方式是对 127.0.0.1:port 做一次 connect：**连得上 = 有人在用 →
    换下一个；连不上 = 可用**。非破坏性（不发任何数据），也足够快。
    """
    for i in range(max(1, int(tries))):
        port = int(preferred) + i
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return port
            finally:
                s.close()
        except OSError:
            return port
    return int(preferred)


# ---------------------------------------------------------------------------
# 资源释放
# ---------------------------------------------------------------------------
def _resource_dir():
    """源码运行时是 Yuhub/resources/easytier；打包后在 _MEIPASS/easytier。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "resources", RESOURCE_DIR_NAME)
    if os.path.isdir(cand):
        return cand
    import sys
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = os.path.join(meipass, RESOURCE_DIR_NAME)
        if os.path.isdir(cand):
            return cand
    return ""


def release_binaries(force=False):
    """把内置的 EasyTier 释放到固定目录。返回 (成功, 说明)。"""
    src = _resource_dir()
    if not src:
        return False, "内置 EasyTier 资源缺失"
    dst = install_dir()
    os.makedirs(dst, exist_ok=True)
    for name in BIN_NAMES:
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if not os.path.exists(s):
            if name in OPTIONAL_BIN_NAMES:
                continue        # 可选件：没有就少一条能力，不拦启动
            return False, f"缺少 {name}"
        if force or not os.path.exists(d) or os.path.getsize(d) != os.path.getsize(s):
            try:
                shutil.copy2(s, d)
            except PermissionError:
                # 目标文件正被运行的 easytier 占用；大小一致就当可用
                if os.path.exists(d) and os.path.getsize(d) == os.path.getsize(s):
                    continue
                return False, f"{name} 被占用且版本不同"
    return True, dst


# ---------------------------------------------------------------------------
# 一键修复：从网上重新下载同版本 EasyTier 并安装
# ---------------------------------------------------------------------------
# 背景：杀毒软件（尤其国内某些）会把刚释放出来的 easytier-core.exe 当风险
# 程序直接删掉，用户下载完 Yuhub 一进联机页就报"内置资源缺失"。重新下载
# Yuhub 本体解决不了（再释放再被删），正确姿势是单独把 EasyTier 组件
# 从官方 GitHub Release 重新拉一份装回去。
#
# 版本必须与内置的完全一致（内置 = 2.6.4-8428a89d，实测 `--version` 确认），
# 否则新旧混跑会出现协议不兼容。下载源按顺序尝试：GitHub 直连 → 两个国内
# 常见的 GitHub 加速镜像（镜像会失效，失败了自然落到下一个）。
EASYTIER_VERSION = "2.6.4"
_EASYTIER_ASSET = ("easytier-windows-x86_64-v%s.zip" % EASYTIER_VERSION)
_EASYTIER_PATH = ("EasyTier/EasyTier/releases/download/v%s/" % EASYTIER_VERSION)
EASYTIER_DOWNLOAD_URLS = (
    "https://github.com/" + _EASYTIER_PATH + _EASYTIER_ASSET,
    "https://ghproxy.net/https://github.com/" + _EASYTIER_PATH + _EASYTIER_ASSET,
    "https://gh-proxy.com/https://github.com/" + _EASYTIER_PATH + _EASYTIER_ASSET,
)


def core_health():
    """检查 EasyTier 组件是否完好。返回 (ok, detail)。

    判据：三个文件都在、easytier-core.exe 大小正常（>1MB，被删一半/截断
    的文件大小会明显异常）。不在这里跑 --version——那是启动路径的职责，
    这里只做"文件级"体检，供设置页轮询显示。
    """
    core = core_path()
    if not os.path.isfile(core):
        return False, "缺少 easytier-core.exe（可能被安全软件删除）"
    if os.path.getsize(core) < 1_000_000:
        return False, "easytier-core.exe 大小异常（文件可能损坏）"
    for name in ("wintun.dll", "Packet.dll"):
        p = os.path.join(install_dir(), name)
        if not os.path.isfile(p):
            return False, "缺少 %s" % name
    return True, core


def repair_binaries(progress=None, timeout=600):
    """下载与内置同版本的 EasyTier 并重新安装到固定目录。

    progress: callable(str)，汇报进度文字（供 UI 显示）。可为 None。
    返回 (ok, msg)。ok=True 时 msg 是成功说明，否则是给用户看的原因。

    注意：房间运行中 easytier-core.exe 被占用、覆盖必然失败，所以这里
    会先尝试停掉它（写停止信号 + 兜底 taskkill，均不弹 UAC）；停不下来
    就明确让用户手动停止，绝不硬来。
    """
    report = progress or (lambda s: None)

    if core_running():
        report("检测到跨网房间正在运行，先停止它…")
        tier = EasyTier()
        tier.request_stop()
        if not tier.wait_stopped(5.0):
            force_kill_core()
            time.sleep(1.0)
        if core_running():
            return False, "无法停止正在运行的 easytier-core，请先停止跨网房间再修复"

    tmpdir = os.path.join(install_dir(), "_repair_tmp")
    zpath = os.path.join(tmpdir, "easytier.zip")
    last_err = ""
    try:
        os.makedirs(tmpdir, exist_ok=True)
        import urllib.request
        for i, url in enumerate(EASYTIER_DOWNLOAD_URLS, 1):
            report("正在下载 EasyTier v%s（第 %d/%d 个源，约 32 MB）…"
                   % (EASYTIER_VERSION, i, len(EASYTIER_DOWNLOAD_URLS)))
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Yuhub-Repair",
                    "Accept": "application/octet-stream",
                })
                # urlopen 会遵循系统/环境代理（urllib.request.getproxies），
                # 走系统代理的机器不会因为 GitHub 直连不通而失败。
                with urllib.request.urlopen(req, timeout=60) as resp, \
                        open(zpath, "wb") as f:
                    total = int(resp.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            report("下载中 %d%%（%.1f / %.1f MB）"
                                   % (got * 100 // total,
                                      got / 1048576.0, total / 1048576.0))
                if got < 10_000_000:
                    raise IOError("下载不完整（仅 %d 字节）" % got)
                last_err = ""
                break
            except Exception as exc:
                last_err = str(exc)
                continue
        if last_err:
            return False, "下载失败：%s（请检查网络后重试）" % last_err

        report("下载完成，正在解压安装…")
        import zipfile
        extracted = {}
        with zipfile.ZipFile(zpath) as z:
            for entry in z.namelist():
                base = os.path.basename(entry)
                if base in BIN_NAMES and base not in extracted:
                    z.extract(entry, tmpdir)
                    extracted[base] = os.path.join(tmpdir, entry)
        # 可选件不算"上游资产变更"：老包里没有 cli 时照样修得动 core
        missing = [w for w in BIN_NAMES
                   if w not in extracted and w not in OPTIONAL_BIN_NAMES]
        if missing:
            return False, "压缩包里缺少 %s（上游资产可能已变更）" % "、".join(missing)

        os.makedirs(install_dir(), exist_ok=True)
        for name, src in extracted.items():
            # tmpdir 就在 install_dir 里，同盘 os.replace 是原子改名
            os.replace(src, os.path.join(install_dir(), name))
    except Exception as exc:
        return False, "修复失败：%s" % exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok, detail = core_health()
    if not ok:
        return False, "安装后校验未通过：%s" % detail
    # 最后跑一次 --version 确认组件真的可用（文件在 ≠ 能跑）
    try:
        r = subprocess.run(
            [core_path(), "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
        ver = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        ver = ver[0].strip() if ver else ""
    except Exception as exc:
        return False, "安装后 easytier-core 无法运行：%s" % exc
    return True, ("已重新安装 EasyTier %s" % (ver or EASYTIER_VERSION))


# ---------------------------------------------------------------------------
# 提权启动（UAC）与看门狗
# ---------------------------------------------------------------------------
# 看门狗改用 Yuhub.exe --watchdog 模式（自己当 helper），而不是 wscript + VBScript
# 或 PowerShell。原因（都实测踩过）：
#   1) PowerShell 是控制台程序，ShellExecuteW(runas) 启动时 conhost 窗口会闪；
#      命令行加 -WindowStyle Hidden 又会让它卡住不执行（本环境确定性复现）；
#      ShowWindow / GetConsoleWindow 在分离会话下拿不到句柄，也藏不住。
#   2) wscript.exe 看似"无控制台"，但用 WScript.Shell.Run 启动控制台子系统程序
#      （如 easytier-core.exe）时，Windows 仍会分配一个 conhost 子进程；
#      窗口模式 0 = 隐藏对 conhost 无效，会被用户看到 C:\Windows\System32 路径
#      的窗口一闪而过——反复出现就成了"白窗口闪烁"。
#   3) 让 GUI 子系统的可执行程序（Yuhub.exe 自身）当 watchdog，再在脚本里用
#      Win32 CREATE_NO_WINDOW 标志启动 easytier-core：Windows 不给它分配
#      conhost，从根上消除窗口闪烁。
# 参数走 base64+JSON 序列化，再无命令行引号/空格/特殊字符的转义陷阱。
def _yuhub_exe_path():
    """watchdog 模式要启动的 Yuhub.exe 完整路径。

    这里的返回值会作为 `ShellExecuteW("runas", exe, "--watchdog <payload>")`
    的**可执行文件**，所以它必须是**认识 --watchdog 这个开关的那个程序**。

    坑（实测踩过，是"进入房间后一直显示正在启动"的根因之一）：
      源码运行时 `sys.executable` 是 **python.exe**，不是 Yuhub.exe。
      `python.exe --watchdog <b64>` 会被 Python 当成"要执行名为 --watchdog
      的脚本"，找不到就立刻退出；`core_running()` 在 3 秒宽限期后仍为假，
      于是报出误导性的"EasyTier 进程意外退出（密码含特殊字符或节点不可达？）"。
      而且 python.exe 是**控制台**程序，runas 启动它还会闪一个黑窗口——
      正是 watchdog 方案当初要消灭的现象。

    所以：
      * 打包后（sys.frozen）→ `sys.executable` 就是 Yuhub.exe，直接用；
      * 源码运行 → 必须去找**真正构建出来的 Yuhub.exe**（项目根目录），
        实在找不到才回落到 python.exe 并让调用方给出可读的错误提示。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 打包态：sys.executable 就是 Yuhub.exe
        exe = sys.executable
        if exe and os.path.isfile(exe):
            return exe

    # 源码运行态：从 etier.py 所在目录（项目根）找构建产物
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "Yuhub.exe"),
        os.path.join(here, "dist", "Yuhub.exe"),
    ):
        if os.path.isfile(cand):
            return cand

    # 最后兜底：打包态下 sys.executable 一般可用；源码态下这里会拿到
    # python.exe —— 调用方 `launch_elevated` 会识别并给出明确提示，
    # 而不是让它静默地去启动一个不认识 --watchdog 的程序。
    return sys.executable or ""


def _parent_process_name():
    """当前进程真实的 exe 文件名（供 watchdog 做防 PID 复用校验）。

    优先读**真实映像名**而不是 `sys.executable`：
    源码态下两者可能都不等于 watchdog 眼里的父进程名，
    但用 `QueryFullProcessImageNameW` 拿到的才是权威值。
    取不到时回落到 sys.executable 的 basename，绝不返回空串
    （空串会让 watchdog 跳过 PID 校验）。
    """
    try:
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong),
        ]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, os.getpid())
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value)
            finally:
                k32.CloseHandle(h)
    except Exception:
        pass
    return os.path.basename(sys.executable or "")


def watchdog_capable(exe_path=None):
    """判断给定程序能否充当 watchdog（即它是不是带 --watchdog 的 Yuhub.exe）。

    ⚠️ 这里**绝对不能**靠"文件名正好等于 Yuhub.exe"来判断。用户从
    GitHub Release / 浏览器下载后，文件经常被存成 `Yuhub (1).exe`、
    `Yuhub-v0.8.9beta.exe`，或者自己顺手改了个名。旧逻辑遇到这种情况
    会把**打包好的正式版**误判成"没有构建"，异地联机一进房间就失败，
    还给出"请先运行 build.bat 构建"这种把锅甩给用户的提示——他明明是
    从 Release 下的安装包，看到这句话只会一脸问号。

    真正的判据只有一条：**当前进程是不是打包后的 Yuhub 自己**
    （`sys.frozen`）。是 → 它必然认识 --watchdog，无条件放行，名字随便叫。
    否 → 源码开发态，才需要去目录里找构建产物并按名字挡掉 python.exe。
    """
    path = (exe_path or _yuhub_exe_path()) or ""
    if not path or not os.path.isfile(path):
        return False, "找不到可执行文件（请先构建，或直接运行打包后的 exe）"

    # ---- 打包态：自己就是认识 --watchdog 的那个程序，文件名无关 ----
    if getattr(sys, "frozen", False):
        try:
            same = os.path.samefile(path, sys.executable)
        except OSError:
            same = (os.path.abspath(path).lower()
                    == os.path.abspath(sys.executable or "").lower())
        if same:
            return True, path

    # ---- 源码开发态：按名字挑构建产物 ----
    # （`_yuhub_exe_path()` 在源码态只会返回 Yuhub.exe / dist\Yuhub.exe /
    #   python.exe 三者之一，所以这里不会误放行无关程序；用 endswith 而非
    #   等号，是为了兼容用户给构建产物改过名的情况。）
    base = os.path.basename(path).lower()
    if base.endswith(".exe") and not base.startswith("python"):
        return True, path
    if base.startswith("python"):
        return False, ("当前是源码运行且未找到构建好的 Yuhub.exe，"
                       "无法创建虚拟网卡（提权看门狗需要 Yuhub.exe）。"
                       "请先运行 build.bat 构建，或改用打包后的 Yuhub.exe。")
    return False, "watchdog 可执行文件不可用：%s" % path


def build_watchdog_payload(core, args, stopfile, workdir, parent_pid=0, parent_name=""):
    """把 watchdog 需要的核心参数打包成 base64(JSON)。

    流程：JSON 序列化 → UTF-8 → base64（ASCII）→ 主进程通过
    `Yuhub.exe --watchdog <payload>` 把它交给 watchdog 进程。

    parent_pid / parent_name：主 Yuhub 进程的 PID 与 exe 名。watchdog 会持续
    检查它是否还活着——一旦主进程消失（正常退出 / 被任务管理器强杀 / 崩溃），
    watchdog 自动停掉 easytier-core 并退出。这是"退出 Yuhub 自动关房间"的
    关键：不依赖主进程主动写停止信号，主进程**没了**本身就是停止信号。
    parent_name 用于防 PID 复用（父进程退出后 PID 被别的进程占了的误判）。
    """
    payload = {"core": core, "args": list(args),
               "stop": stopfile, "workdir": workdir,
               "parent_pid": int(parent_pid or 0),
               "parent_name": parent_name or ""}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def launch_elevated(payload_b64):
    """UAC 提权启动 watchdog（Yuhub.exe --watchdog <payload>）。

    用户会在屏幕上看到一次 UAC 确认框（"Yuhub.exe"请求管理员权限），
    取消则返回失败。watchdog 是 GUI 子系统的程序，本进程无控制台窗口；
    它用 CREATE_NO_WINDOW 启动 easytier-core，**彻底不会**让
    `C:\\Windows\\System32\\cmd.exe / conhost.exe` 之类的窗口出现，
    解决"白窗口反复闪烁"问题。
    """
    exe = _yuhub_exe_path()
    # 先确认这个程序真的认识 --watchdog。否则 runas 会把开关交给
    # python.exe / 别的东西，对方立刻退出，用户却只看到"进程意外退出"。
    capable, why = watchdog_capable(exe)
    if not capable:
        return False, why
    # ShellExecuteW 的 lpParameters 是单个命令行字符串；用 base64（仅含
    # [A-Za-z0-9+/=]）传参，**完全无需关心 Windows 命令行解析规则**。
    params = "--watchdog " + payload_b64
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 0,
        )
        if rc > 32:
            return True, "已请求管理员权限"
        return False, f"启动被取消或失败（代码 {rc}）"
    except OSError as exc:
        return False, f"提权启动失败：{exc}"


def kill_core_elevated():
    """提权强制结束残留的 easytier-core（Yuhub.exe --kill-core）。

    场景：watchdog 已经不在了（被强杀 / 异常），但 easytier-core 还在跑，
    普通权限的 taskkill 杀不掉提权进程。这里 runas 走 --kill-core 模式清理。
    **会弹一次 UAC**，只在确实需要时调用。
    返回 (成功, 说明)。
    """
    exe = _yuhub_exe_path()
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, "--kill-core", None, 0,
        )
        if rc > 32:
            return True, "已请求管理员权限清理残留"
        return False, f"清理被取消或失败（代码 {rc}）"
    except OSError as exc:
        return False, f"提权清理失败：{exc}"


# ---------------------------------------------------------------------------
# 状态探测：枚举本机 IPv4 地址，找 EasyTier 虚拟网段
# ---------------------------------------------------------------------------
# 为什么不用 GetAdaptersAddresses：它的 IP_ADAPTER_ADDRESSES 结构体字段
# 极多，手写 ctypes 定义一旦偏移错位就是段错误（实测崩过）。
# GetIpAddrTable 只要一个 MIB_IPADDRROW（7 个字段），稳如老狗；
# 判据只需要"出现 10.126.126.x"，用不着网卡名。
class _MibIpAddrRow(ctypes.Structure):
    _fields_ = [("dwAddr", wt.DWORD), ("dwIndex", wt.DWORD),
                ("dwMask", wt.DWORD), ("dwBCastAddr", wt.DWORD),
                ("dwReasmSize", wt.DWORD), ("unused1", wt.USHORT),
                ("wType", wt.USHORT)]


def _ip_addresses():
    """本机所有 IPv4 地址（点分字符串列表）。失败返回 []。"""
    size = wt.ULONG(0)
    if ctypes.windll.iphlpapi.GetIpAddrTable(None, ctypes.byref(size), 0) != 122:
        return []                                # 122 = ERROR_INSUFFICIENT_BUFFER
    buf = ctypes.create_string_buffer(size.value)
    if ctypes.windll.iphlpapi.GetIpAddrTable(buf, ctypes.byref(size), 0) != 0:
        return []
    count = wt.ULONG.from_buffer_copy(buf, 0).value
    rows = []
    for i in range(count):
        row = _MibIpAddrRow.from_buffer_copy(buf, 4 + i * ctypes.sizeof(_MibIpAddrRow))
        raw = row.dwAddr
        rows.append("%d.%d.%d.%d" % (raw & 255, (raw >> 8) & 255,
                                     (raw >> 16) & 255, (raw >> 24) & 255))
    return rows


def virtual_adapter_ips():
    """本机**所有**落在虚拟网段的地址（正常只有 1 个）。

    为什么要返回列表而不是直接挑一个：程序被强杀/卡死后重启，偶尔会残留
    一块旧的 wintun 网卡没来得及拆除，于是同时存在两个 10.126.126.x。
    这时"哪个才是在用的"从地址表本身判断不出来——默默挑一个显示给用户，
    他会拿着一个连不通的地址去填游戏，而界面上毫无异常。
    所以这里把全部返回，由联机页如实提示，把判断权交还给人。
    """
    return [ip for ip in _ip_addresses() if ip.startswith(VIRTUAL_NET_PREFIX)]


def virtual_adapter_ip():
    """虚拟网卡就绪时返回虚拟 IP，否则空串。

    判据：IPv4 落在 10.126.126.0/24（EasyTier DHCP 默认段）。
    """
    ips = virtual_adapter_ips()
    return ips[0] if ips else ""


def _resolve_name(ip, timeout=0.6):
    """把虚拟 IP 反查成主机名（昵称）。

    EasyTier 的虚拟网卡走的是 NetBIOS/LLMNR 名称解析，`socket.gethostbyaddr`
    在 Windows 上会走系统解析链，多数情况能把设置过 --hostname 的节点查出来。
    查不到返回空串——调用方回落到只显示 IP。
    """
    try:
        socket.setdefaulttimeout(timeout)
        name, _aliases, _addrs = socket.gethostbyaddr(ip)
        return (name or "").strip()
    except Exception:
        return ""
    finally:
        socket.setdefaulttimeout(None)


def list_members_detailed(my_ip="", resolve=True):
    """列出在线成员，尽量带上名称。

    返回 [{"ip": "10.126.126.3", "name": "XiaoYu"}, ...]，不含自己。

    名称来源优先级：
      1. 反查主机名（EasyTier 的 --hostname 会体现在这里）
      2. 查不到就留空，UI 只显示 IP

    反查有超时（每个 0.6 秒），所以成员多时耗时会线性增长——
    调用方（lan_page 的 800ms tick）应传入 resolve=False 做快速刷新，
    只在需要时偶尔做一次带名称的完整刷新。
    """
    ips = list_members(my_ip)
    out = []
    for ip in ips:
        name = _resolve_name(ip) if resolve else ""
        out.append({"ip": ip, "name": name})
    return out


def probe_host(host_ip, timeout=2.0, attempts=2):
    """探测某个虚拟 IP 是否在线（用于成员校验"房主是否真的在房间里"）。

    用 ping 而不是 ARP：ARP 条目可能是陈旧的缓存，ping 是主动探测更可信。
    Windows 的 ping 即使不通也返回 0，所以必须看输出里的 TTL/时间字段。
    """
    for _ in range(max(1, attempts)):
        try:
            r = subprocess.run(
                ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host_ip],
                capture_output=True, text=True, encoding="gbk",
                errors="replace", timeout=timeout + 2,
                creationflags=_CREATE_NO_WINDOW,
            )
            out = (r.stdout or "")
            # 中文系统输出 "TTL=" / 英文 "TTL="；不通时是 "无法访问目标主机"/"timed out"
            if "TTL=" in out.upper() or "ttl=" in out:
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# 权威成员表：借 easytier-cli 读 core 的管理 RPC（第三条到场信号）
# ---------------------------------------------------------------------------
def _to_float(v):
    """宽容转 float：core 的表格值是预格式化字符串，可能是 "-" / "" / None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_peer_list_json(text):
    """解析 `easytier-cli peer list -o json` 的输出。

    输出是**一个数组**：第一行是本机（cost = "Local"），其余每行一个成员。
    v2.6.4 的字段（PeerTableItem）：cidr / ipv4 / hostname / cost / lat_ms /
    loss_rate / rx_bytes / tx_bytes / tunnel_proto / nat_type / id / version。

    ⚠️ 两点很重要：
      1. 值大多是**预格式化的字符串**（"1.23"、"12.5%"、"1.5 MiB"），不是
         数字；这里只把 lat_ms 转成 float（界面要算/要比较），其余原样留着；
      2. EasyTier 升版可能增删字段——所以**宽容解析**：只有 ipv4 是必需的，
         其余缺了就留空。绝不因为多了/少了字段让整块失效（这是这一版最怕的
         回归：接口一变，成员表整块消失，而界面上看不出原因）。
    解析不了（不是 JSON / 不是数组）返回 None，调用方据此走降级。
    """
    try:
        rows = json.loads(text or "")
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    # 多实例时 CLI 会把每个实例的结果包一层：
    #   [{"instance_id":..., "instance_name":..., "result": [...]}, ...]
    # 我们只起一个实例，正常情况下拿到的是裸数组（CLI 在只有一个结果时
    # 直接打印它）。但**顺手支持包装形态**——多一层封装就让成员表整块
    # 失效，正是这里最不该发生的失败模式。
    if rows and all(isinstance(r, dict) and "result" in r for r in rows):
        merged = []
        for r in rows:
            if isinstance(r.get("result"), list):
                merged.extend(r["result"])
        rows = merged
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ip = str(r.get("ipv4") or "").strip()
        if not ip:
            continue
        cost = str(r.get("cost") or "").strip()
        low = cost.lower()
        out.append({
            "peer_id": str(r.get("id") or ""),
            "ipv4": ip,
            "hostname": str(r.get("hostname") or "").strip()[:32],
            "cost": cost,
            # 是不是"没直连"（多跳/走中继）：直连的 cost 是 1 或 Local。
            # 这一条直接回答"异地分屏为什么只有 4 帧"——中继带宽是共享的。
            "relayed": bool(low) and low not in ("local", "1", "direct"),
            "lat_ms": _to_float(r.get("lat_ms")),
            "loss_rate": str(r.get("loss_rate") or "").strip(),
            "tunnel": str(r.get("tunnel_proto") or "").strip(),
            "nat": str(r.get("nat_type") or "").strip(),
            "version": str(r.get("version") or "").strip(),
        })
    return out


class CorePeerFeed:
    """成员表：借 easytier-cli 读 core 内部那张**权威**成员表。

    为什么要它
    ----------
    到 v0.12 为止，成员发现靠两条**旁路**信号（见 MemberTracker）：操作系统
    ARP 表 + 我们自己的应用层昵称广播。两条都能用，但都是推断——ARP 条目是
    出站流量的副作用、会随时间老化；广播是我们自己维护的一套协议、还受
    丢包影响。core 内部本来就有一张权威表：**握手过的人在表里，表里的人都
    握过手**，每条还带虚拟 IP、主机名、延迟、丢包、隧道协议、NAT 类型，
    以及 OSPF 同步来的多跳成员。这些数据一直存在，只是藏在管理 RPC 后面。

    为什么是"起进程"而不是自己调 RPC
    -------------------------------
    那套 RPC 跑在 EasyTier 自己的 TCP 隧道 + prost 编解码上（不是 HTTP 或
    标准 gRPC），自己实现等于把它的私有协议抄一遍，版本一升就碎。官方给的
    客户端就是 easytier-cli.exe——和 core **同一个 crate** 构建、同一个发布
    包里（官方 zip 里本来就有），整包解压时顺手带上即可，约 9.5MB。

    三条纪律
    --------
      1. **只是"多加一条"**：ARP 与信标通道一行不删。cli 不在、core 没起、
         超时、解析失败——任何一步失败都静默降级，成员列表仍有旁路兜底。
      2. **不阻塞、不刷屏**：轮询在后台线程里；失败只在"第一次失败"和
         "从成功转为失败"时各记一条日志，其余静默重试。
      3. **数据带时间戳**：快照有新鲜期，过期就当没有——不给界面看旧数据。
    """

    POLL_INTERVAL = 2.5      # 轮询间隔（秒）。cli 是进程，不能跟着 800ms 的 tick 走
    FRESH_TTL = 8.0          # 记录新鲜期（秒）：超过当这一轮没数据
    CALL_TIMEOUT = 4.0       # 单次 CLI 调用超时（连不上时它退得很快）

    def __init__(self, rpc_port=RPC_PORT, on_log=None):
        self._rpc_port = int(rpc_port or RPC_PORT)
        self._on_log = on_log or (lambda msg: None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._records = {}       # ipv4 -> record（含 at=monotonic）
        self._ok = False         # 上一次调用成功过（成功过才敢说"权威表可用"）
        self._failed = False     # 上一次是失败（用于日志只在状态翻转时打一条）
        self._cli_missing = False

    # ---------------------------------------------------------------- 生命周期
    def start(self):
        """起轮询线程。重复调用无害。"""
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="CorePeerFeed", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """停轮询线程。瞬时返回。"""
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(1.5)

    @property
    def running(self):
        t = self._thread
        return t is not None and t.is_alive()

    def available(self):
        """权威表现在可用吗（cli 在 + 上一次调用成功）。"""
        with self._lock:
            return bool(self._ok) and not self._cli_missing

    def cli_missing(self):
        with self._lock:
            return self._cli_missing

    # ---------------------------------------------------------------- 读快照
    def _fresh_locked(self, now):
        ttl = self.FRESH_TTL
        return {ip: dict(r) for ip, r in self._records.items()
                if now - float(r.get("at") or 0.0) <= ttl}

    def snapshot(self):
        """当前成员记录（新鲜期内）。UI/跟踪器调用，零阻塞。"""
        now = time.monotonic()
        with self._lock:
            return list(self._fresh_locked(now).values())

    def by_ip(self):
        """{ipv4: record}（新鲜期内）。"""
        now = time.monotonic()
        with self._lock:
            return self._fresh_locked(now)

    def quality_map(self):
        """{ipv4: 链路质量}：给 MemberTracker 并进成员记录用。"""
        now = time.monotonic()
        out = {}
        with self._lock:
            for ip, r in self._fresh_locked(now).items():
                out[ip] = {"lat_ms": r.get("lat_ms"),
                           "loss_rate": r.get("loss_rate") or "",
                           "tunnel": r.get("tunnel") or "",
                           "nat": r.get("nat") or "",
                           "relayed": bool(r.get("relayed")),
                           "peer_id": r.get("peer_id") or ""}
        return out

    def hostname_of(self, ip):
        """某个虚拟 IP 的主机名（= 我们传给 core 的 --hostname）。

        它就是用户填的昵称——所以信标万一没到，昵称还有权威表兜底。
        """
        rec = self.by_ip().get(ip or "")
        return (rec or {}).get("hostname") or ""

    # ---------------------------------------------------------------- 轮询
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                pass                # 任何意外都不该弄死这条辅助通道
            self._stop.wait(self.POLL_INTERVAL)

    def _poll_once(self):
        cli = cli_path()
        if not (os.path.isfile(cli) and os.path.getsize(cli) > 1_000_000):
            # 源码态 / 老安装包里没有 cli：只记一次，之后安静降级
            with self._lock:
                first = not self._cli_missing
                self._cli_missing = True
            if first:
                self._log("没找到 easytier-cli.exe：成员表退回 ARP + 信标"
                          "（联机本身不受影响）")
            return False
        cmd = [cli,
               "-p", "127.0.0.1:%d" % self._rpc_port,
               "-o", "json",
               "peer", "list"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self.CALL_TIMEOUT,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            self._mark_fail("调用 easytier-cli 失败")
            return False
        rows = parse_peer_list_json(proc.stdout or "")
        if rows is None:
            if (proc.returncode or 0) != 0:
                err = (proc.stderr or "").strip().splitlines()
                self._mark_fail(err[0] if err else
                                "easytier-cli 返回码 %s" % proc.returncode)
            else:
                self._mark_fail("easytier-cli 输出无法解析")
            return False
        now = time.monotonic()
        with self._lock:
            self._records = {r["ipv4"]: dict(r, at=now) for r in rows}
            was_fail = self._failed
            self._ok = True
            self._failed = False
        if was_fail:
            self._log("成员表已连上 core（权威表可用）")
        return True

    def _mark_fail(self, why):
        """失败：只在"第一次失败"或"从成功转为失败"时记一条日志。"""
        with self._lock:
            first = not self._failed
            was_ok = self._ok
            self._failed = True
        if first or was_ok:
            self._log("读取成员表失败（不影响联机，继续用 ARP + 信标）：%s"
                      % why)

    def _log(self, msg):
        try:
            self._on_log(msg)
        except Exception:
            pass



# ---------------------------------------------------------------------------
# 成员在线跟踪：解决"在线人数断断续续"
# ---------------------------------------------------------------------------
# 根因：以前直接拿 `arp -a` 的结果当在线名单。Windows 的 ARP 条目受
# "可达性超时"控制——一段时间没有流量，条目就从表里消失（哪怕对方还在线）；
# EasyTier 节点之间没有持续大流量时这几乎是必然发生的。于是成员列表每隔
# 十几秒就"闪没"又"闪回来"。
#
# 解法 = 保活 + 宽限：
#   1. **保活**：后台线程每 3 秒对已知成员逐个 ping（1 次、0.5s 超时）。
#      ICMP 流量本身会刷新 ARP 条目（条目不会过期），ping 通同时是对端
#      活性的主动确认，比被动等 ARP 靠谱。
#   2. **宽限**：离线判定要求**两个信号同时失效**——ARP 里连续 12 秒
#      没见到、且连续 4 次 ping 都不通。任何一个信号单独抖动都不会把人
#      从列表里闪没。
# 名称解析（gethostbyaddr，每个最长 0.8s）也挪进这个后台线程，解析一次
# 就缓存——UI 线程从此只读快照，一个子进程都不起、零阻塞。
#
# v0.13 起「到场信号」变成三条并集（见 _hint_ips / _round）：ARP、昵称信标、
# core 权威成员表（CorePeerFeed）。前两条各有硬伤（ARP 要出站流量才生成条目
# 且会老化；信标是应用层广播、会丢），第三条是协议内权威数据——任一条算数，
# 于是"看不到好友"这类问题从"靠推断"变成"有权威来源 + 旁路兜底"。
class MemberTracker:
    """一个跨网房间对应一个实例。UI 通过 snapshot() 拿稳定视图。"""

    PING_INTERVAL = 3.0      # 每轮 ping 的间隔（秒）
    PING_TIMEOUT = 0.5       # 单次 ping 超时（秒）
    PING_FAIL_EVICT = 4      # 连续 ping 失败次数达到该值才可能判离线
    CONFIRM_GRACE = 12.0     # 最后一次确认（ARP 见到 / ping 通）后的宽限（秒）

    def __init__(self, my_ip, on_log=None, name_lookup=None, alive_hints=(),
                 name_hint=None, quality_hint=None):
        self._my_ip = my_ip
        self._on_log = on_log or (lambda msg: None)
        # 可选的「成员信息」查询回调（NickBeacon.get_info）：一次拿到
        # 昵称 + 游戏快连。信标查得到就不用走 gethostbyaddr——后者在
        # EasyTier 虚拟网里基本解析不出 --hostname。回调允许只返回昵称
        # 字符串（旧写法），本类会兼容。
        self._name_lookup = name_lookup
        #: 「第三方到场线索」列表：每个回调返回一组"此刻该算在场"的 IP。
        #: 联机页接了两条——昵称信标的新鲜表、core 权威成员表
        #: （CorePeerFeed）。为什么必须有它们：只认 ARP 会让新成员永远
        #: 进不了列表（ARP 条目要本机先有出站流量才生成），ICMP 被防火墙
        #: 挡着时还会把老成员陆续误判成离线——用户实测过这两个症状。
        self._alive_hints = tuple(h for h in (alive_hints or ()) if h)
        #: 昵称兜底：ip -> 名字。接的是 core 的 hostname（= 我们传给 core 的
        #: --hostname，也就是用户昵称）。信标没到、gethostbyaddr 也查不出时
        #: 靠它兜底——比"（未识别昵称）"体面得多。
        self._name_hint = name_hint
        #: 链路质量：返回 {ip: {"lat_ms","loss_rate","tunnel","nat","relayed",
        #: "peer_id"}}，并进成员记录给界面显示（core 权威表独有）。
        self._quality_hint = quality_hint
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        # refresh_now() 用它打断 PING_INTERVAL 的等待，立刻再跑一轮
        self._wake = threading.Event()
        self._thread = None
        # ip -> {"confirmed": monotonic, "ping_fail": int, "name": str,
        #        "game": str, "port": int, "share": int, "srev": int, "ss": int,
        #        "lat_ms": float|None, "loss_rate": str, "tunnel": str,
        #        "nat": str, "relayed": bool, "peer_id": str}
        self._peers = {}

    # ------------------------------------------------ 对外接口（UI 用）
    def start(self):
        """启动后台跟踪线程。重复调用无害。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._loop, name="MemberTracker", daemon=True)
        self._thread.start()

    def stop(self):
        """停止后台线程（停止房间 / 回滚时调用）。瞬时返回。"""
        self._stop_evt.set()
        self._wake.set()             # 让 wait 立刻返回，线程尽快退出

    def refresh_now(self):
        """请求后台线程**立刻**再跑一轮，不等 PING_INTERVAL（点「刷新」时用）。

        用户点刷新说明"现在列表不对"，再用最多 3 秒的间隔等下一轮
        在体感上就是"点了没反应"。这里直接把等待打断。
        """
        self._wake.set()

    def snapshot(self):
        """当前在线成员，按 IP 排序。UI 线程调用，零阻塞。

        返回 [{"ip", "name", "game", "port", "share", "srev", "ss",
              "lat_ms", "loss_rate", "tunnel", "nat", "relayed", "peer_id"}]。
        game / port 是对端在「游戏快连」里选的那款游戏（由昵称信标一起
        广播过来），队友的成员行据此显示"他在玩什么"，复制按钮也用**他的**
        端口拼地址。
        share 是对端「临时云盘」的服务端口（0 = 没开 / 旧版本），拉文件
        清单时直接拿它拼地址，不用先猜再试；srev 是对端云盘清单的版本号，
        一变就说明对方增删了分享文件（见 NickBeacon.live_shares）。
        ss 是对端「屏幕共享」的服务端口（0 = 没在共享），成员行据此决定
        要不要显示「看屏幕」按钮。
        lat_ms / loss_rate / tunnel / nat / relayed 是**链路质量**，来源是
        core 的权威成员表（CorePeerFeed，见 _apply_quality）：没接上那份
        数据时这些字段为空/False，界面照常工作（这就是"保留旁路通道"）。
        """
        with self._lock:
            return [{"ip": ip,
                     "name": (p.get("name") or ""),
                     "game": (p.get("game") or ""),
                     "port": int(p.get("port") or 0),
                     "share": int(p.get("share") or 0),
                     "srev": int(p.get("srev") or 0),
                     "ss": int(p.get("ss") or 0),
                     "lat_ms": p.get("lat_ms"),
                     "loss_rate": (p.get("loss_rate") or ""),
                     "tunnel": (p.get("tunnel") or ""),
                     "nat": (p.get("nat") or ""),
                     "relayed": bool(p.get("relayed")),
                     "peer_id": (p.get("peer_id") or "")}
                    for ip, p in sorted(self._peers.items())]

    # ------------------------------------------------ 后台线程主体
    def _loop(self):
        while not self._stop_evt.is_set():
            # 先清唤醒标记再干活：这样"干活期间"进来的 refresh_now()
            # 会留到下面的 wait 上，immediate 生效，不会被吞掉。
            self._wake.clear()
            try:
                self._round()
            except Exception:
                pass                    # 单轮失败不影响下一轮
            self._wake.wait(self.PING_INTERVAL)

    def _hint_ips(self):
        """把所有「第三方到场线索」合成一个 IP 集合（见 _round 的注释）。

        单个线索抛异常只丢它自己——不能让一条辅助通道把整轮扫描带崩。
        """
        ips = set()
        for hint in self._alive_hints:
            try:
                ips.update(hint() or ())
            except Exception:
                pass
        ips.discard(self._my_ip)
        return ips

    def _apply_quality(self):
        """把链路质量并进成员记录（core 权威表提供；没接上就保持原样）。

        只对**已经在表里**的成员赋值：质量数据不负责决定"谁在场"，那是
        `_round` 里三条到场信号的职责——两者分开，免得某次质量查询失败
        就把成员弄丢。
        """
        if not self._quality_hint:
            return
        try:
            q = self._quality_hint() or {}
        except Exception:
            return
        if not q:
            return
        with self._lock:
            for ip, info in q.items():
                p = self._peers.get(ip)
                if p is None or not isinstance(info, dict):
                    continue
                for key in ("lat_ms", "loss_rate", "tunnel", "nat",
                            "relayed", "peer_id"):
                    if key in info:
                        p[key] = info[key]

    def _round(self):
        now = time.monotonic()
        # ① 到场信号有三条，**任一即算**：
        #   a. ARP 扫描（老机制）。硬伤：条目要本机先有出站流量才生成——
        #      刚进房时谁也不认识谁，只 ping 已知成员等于新成员永远进不了
        #      列表；条目还会随时间老化，而 ping 要对方应答 ICMP，wintun
        #      网卡默认「公用网络」入站 ICMP 是被防火墙拦着的。
        #   b. 昵称信标新鲜表（应用层广播，入站 UDP 按端口放行，与 ICMP、
        #      与有没有出站流量都无关；房间里每 3 秒一轮，再闲也在响）。
        #   c. **core 权威成员表**（CorePeerFeed 读 easytier-cli 的管理 RPC）：
        #      握手过的人在表里、表里的人都握过手，还带虚拟 IP / 主机名 /
        #      延迟 / 丢包 / 隧道 / NAT。协议内的数据，不靠推断。
        # 🔴 用户实测"成员列表看不到好友 / 好友云盘的文件看不到"两条症状，
        #    根因都是只认 a：云盘轮询的 peer 列表同样来自本跟踪器，跟踪器
        #    看不见人，文件就永远拉不回来。b 是应用层补丁，c 是根治。
        #    三条并集：任何一条算数，任何一条坏掉都还有另外两条。
        try:
            arp = set(list_members(self._my_ip))
        except Exception:
            arp = set()
        hinted = self._hint_ips()
        with self._lock:
            for ip in (arp | hinted):
                p = self._peers.setdefault(
                    ip, {"confirmed": now, "ping_fail": 0,
                         "name": "", "game": "", "port": 0, "share": 0,
                         "srev": 0, "ss": 0, "lat_ms": None, "loss_rate": "",
                         "tunnel": "", "nat": "", "relayed": False,
                         "peer_id": ""})
                p["confirmed"] = now
                p["ping_fail"] = 0
        # ② 逐个 ping：保活 ARP + 主动确认活性
        with self._lock:
            targets = list(self._peers.keys())
        for ip in targets:
            if self._stop_evt.is_set():
                return
            alive = probe_host(ip, timeout=self.PING_TIMEOUT, attempts=1)
            self._on_ping_result(ip, alive, now)
        # ③ 离线判定（双信号失效才移除）
        self._sweep(now)
        # ④ 链路质量（延迟/丢包/隧道/是否中继）：core 权威表独有，有就并进去
        self._apply_quality()
        # ⑤ 昵称 + 游戏快连：信标里随名字一起广播过来
        self._resolve_info()

    def _on_ping_result(self, ip, alive, now=None):
        """记录一次 ping 结果（抽出来是为了可测）。"""
        now = now if now is not None else time.monotonic()
        with self._lock:
            p = self._peers.get(ip)
            if p is None:
                return
            if alive:
                p["confirmed"] = now
                p["ping_fail"] = 0
            else:
                p["ping_fail"] += 1

    def _sweep(self, now=None):
        """移除确认离线的成员：连续 ping 失败 **且** ARP 长时间未见。"""
        now = now if now is not None else time.monotonic()
        with self._lock:
            gone = [ip for ip, p in self._peers.items()
                    if p["ping_fail"] >= self.PING_FAIL_EVICT
                    and now - p["confirmed"] > self.CONFIRM_GRACE]
            for ip in gone:
                del self._peers[ip]
        for ip in gone:
            self._on_log("成员 %s 已离线" % ip)

    def _resolve_info(self):
        """补齐每个成员的昵称与「游戏快连」。

        信标查得到就一次拿全（昵称 + 游戏名 + 端口）。信标里没有昵称时
        才走 gethostbyaddr 兜底——**且只在确实还没名字时**才查，因为
        反查有 0.8 秒超时，每轮都查会把后台线程拖住。

        与旧版的区别：以前只在 name 为空时查一次就永久缓存；现在每轮都
        问一次信标（命中缓存时是一次字典读，几乎零成本），这样运行中
        队友切换了「游戏快连」，我们这边最多 3 秒就跟着变。
        """
        with self._lock:
            peers = list(self._peers.items())
        for ip, p in peers:
            if self._stop_evt.is_set():
                return
            info = {}
            if self._name_lookup:
                try:
                    got = self._name_lookup(ip)
                except Exception:
                    got = None
                if isinstance(got, dict):
                    info = got
                elif got:                       # 兼容只回昵称字符串的回调
                    info = {"name": str(got)}
            name = (info.get("name") or "").strip()
            if not name:
                name = p.get("name") or ""
            if not name and self._name_hint:
                # core 的 hostname = 我们传给 core 的 --hostname = 用户昵称。
                # 信标没到（丢包/被防火墙拦）时，这条比 gethostbyaddr 靠谱得多
                # ——后者在 EasyTier 虚拟网里基本查不出东西。
                try:
                    name = (self._name_hint(ip) or "").strip()
                except Exception:
                    name = ""
            if not name:
                name = _resolve_name(ip, timeout=0.8)
            with self._lock:
                cur = self._peers.get(ip)
                if cur is None:
                    continue
                if name:
                    cur["name"] = name
                game = (info.get("game") or "").strip()
                if game:
                    cur["game"] = game
                try:
                    port = int(info.get("port") or 0)
                except (TypeError, ValueError):
                    port = 0
                if port:
                    cur["port"] = port
                try:
                    share = int(info.get("share") or 0)
                except (TypeError, ValueError):
                    share = 0
                try:
                    srev = int(info.get("srev") or 0)
                except (TypeError, ValueError):
                    srev = 0
                # 信标里带 share 键才更新：旧版客户端不广播这个键，
                # 此时保持原值（0），不会把"已知道对方开着云盘"给冲掉。
                if "share" in info:
                    cur["share"] = share
                    # 版本号同理：老对端可能只发 share 不发 srev，那就保持原值，
                    # 免得把已知的版本号冲成 0 造成一次假的重拉。
                    if "srev" in info:
                        cur["srev"] = srev
                try:
                    ss = int(info.get("ss") or 0)
                except (TypeError, ValueError):
                    ss = 0
                # 屏幕共享端口这里**直接赋值**，不像 share 那样"带键才更新"。
                # 因为共享是可以停的：停掉之后 `_payload` 就不再带这个键，
                # 对端必须据此把端口归零，否则成员行上的「看屏幕」按钮会
                # 一直挂着，点过去必然连不上。旧版客户端从不发这个键，
                # 归零对它们也是对的（它们确实没有屏幕共享）。
                cur["ss"] = ss


# ---------------------------------------------------------------------------
# 昵称信标：解决"成员列表总是显示不了昵称"
# ---------------------------------------------------------------------------
# 根因：EasyTier 的 --hostname 只存在于它自己的对等协议里，**不进
# DNS/NetBIOS**——socket.gethostbyaddr 在虚拟网卡上基本查不出对方昵称。
# 双方又都是 Yuhub，所以直接做应用层信标：
#
#   * 每 3 秒向 10.126.126.255:41234 **UDP 广播**自己的昵称。
#     启动 EasyTier 时已开 --enable-udp-broadcast-relay，广播能跨到
#     所有节点（这正是异地"局域网"广播联机能工作的同一机制）。
#   * 同时监听虚拟 IP 的同端口，收到广播就记下 ip → 昵称（15 秒新鲜期）。
#   * 广播偶发丢包的兜底：向 peer 的 41234 端口发起 **TCP 查询**，
#     对方立即回一行昵称后关闭。出站连接不受防火墙入站规则限制；
#     监听方向由 watchdog 提权时统一加防火墙入站规则（见
#     _preflight_silence_windows，只放行 41234 端口 + Yuhub.exe）。
#
# 套接字全部绑定在**虚拟 IP** 上（而不是 0.0.0.0）：昵称只在房间内
# 传播，不往物理局域网泄漏一个字节。
NICK_PORT = 41234
_NICK_MAGIC = "yuhub-nick-v1"
# 房间聊天（room_chat.py）的广播端口。与昵称信标同一机制：UDP 广播，
# 靠 EasyTier 的 --enable-udp-broadcast-relay 跨到房间里所有节点。
# 为什么不复用 41234：信标是 3 秒一轮的结构化小报文，聊天是低频突发报文，
# 混在一个口上就要在同一路解析里分岔，边界情况变多；代价只是防火墙多放行
# 一个端口（见 _preflight_silence_windows，watchdog 提权时一起加）。
CHAT_PORT = 41235
# 「游戏快连」跟昵称搭同一班车广播：队友的「在线成员」里就能直接看到
# 每个人选的是哪款游戏，点「复制」也用**对方**的端口拼地址。
# 报文是 JSON 字典，多带两个键对旧版是透明的（旧版只读 _NICK_MAGIC），
# 所以不用升协议版本——新旧客户端可以混在同一个房间里。
_NICK_GAME = "game"
_NICK_PORT = "port"
# 「临时云盘」的服务端口。0 / 缺省 = 这个人没开云盘（或用了旧版本），
# 对端就不用白试一次连接。同样不升协议版本——多一个键对旧版透明。
_NICK_SHARE = "share"
# 云盘清单的**版本号**：分享者对「我分享的文件」每做一次增删就 +1。
# 为什么要它：HTTP 接口是只读的，对端没法被"推"一把，只能轮询；有了
# 版本号，对端一看数字变了就知道"对方刚动了文件"，立刻重拉一次清单，
# 而不是傻等下一个 3 秒周期——"谁上传/取消分享，所有人都刷新一次"。
_NICK_SHARE_REV = "srev"
# 屏幕共享端口（0 = 没在共享）。有这个键，队友的成员行就会多一个
# 「看屏幕」按钮——他知道该连哪个端口，不用来问。同样不升协议版本。
# 不广播观看码：那东西由「房间码 + 密码」两边各自算，广播出去等于把
# 准入凭证贴在广播包里，虚拟网里任何一台机器都能收到。
_NICK_SS = "ss"


# ---------------------------------------------------------------------------
# 房间码 / 密码的归一化（**唯一口径**）
# ---------------------------------------------------------------------------
def room_key(code):
    """房间码 → 送给 EasyTier 的网络名骨架。

    🔴 归一化是**房间判定的一部分**：大小写与空格都不参与。也就是说
    `ABC123`、`abc123`、`abc 123` 是同一个房间、成员列表里互相可见。
    凡是"由房间码派生"的东西（云盘 token、屏幕共享观看码）都**必须**走
    这里，否则会出现"房间进得去、成员看得见，但云盘/看屏幕读不出对方的
    数据"——用户反馈的"临时云盘看不到朋友分享的文件"正是这个。
    """
    return (code or "").strip().lower().replace(" ", "")


def room_secret(password):
    """密码 → secret。只去掉首尾看不见的空白，其余原样（区分大小写）。"""
    return (password or "").strip()


class NickBeacon:
    """一个跨网房间对应一个实例。线程安全。"""

    ANNOUNCE_INTERVAL = 3.0      # 广播间隔（秒）
    PEER_FRESH = 15.0            # 信标记录的新鲜期（秒），过期走 TCP 查询
    TCP_QUERY_TIMEOUT = 1.0      # TCP 查询超时
    TCP_FAIL_BACKOFF = 30.0      # TCP 查询失败后的重试间隔（秒）

    def __init__(self, my_ip, nick, on_log=None, game="", port=0, share=0,
                 share_rev=0, ss=0):
        self._my_ip = my_ip
        self._nick = (nick or "").strip()[:32]
        self._game = (game or "").strip()[:24]
        self._port = int(port or 0)
        self._share = int(share or 0)
        self._share_rev = int(share_rev or 0)
        self._ss = int(ss or 0)
        self._on_log = on_log or (lambda msg: None)
        self._stop_evt = threading.Event()
        self._wake = threading.Event()      # 打断广播间隔（切换游戏后立刻广播）
        self._lock = threading.Lock()
        self._threads = []
        # ip -> {"info": {"name","game","port"}, "until": monotonic}
        self._peers = {}
        # ip -> 上次 TCP 查询失败的 monotonic（失败节流）
        self._tcp_fail = {}

    # ------------------------------------------------ 对外接口
    def start(self):
        if self._threads:
            return
        self._stop_evt.clear()
        self._wake.clear()
        for target, name in (
            (self._announce_loop, "NickBeaconTx"),
            (self._udp_listen_loop, "NickBeaconRx"),
            (self._tcp_serve_loop, "NickBeaconTcp"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(t)
            t.start()

    def stop(self):
        self._stop_evt.set()
        self._wake.set()

    def set_game(self, game, port=0):
        """更新本机广播的「游戏快连」（运行中切换游戏时调用）。

        立刻踢一次广播，队友最多 1 秒就看到变化，不用等下一个 3 秒周期。
        """
        with self._lock:
            self._game = (game or "").strip()[:24]
            self._port = int(port or 0)
        self._wake.set()

    def set_share(self, port, rev=None):
        """更新本机广播的「临时云盘」端口（0 = 关闭）与清单版本号。

        立刻踢一次广播，队友最多 1 秒就看到变化。rev 就是"我动过分享清单"
        的信号——绑定新端口、每加一个文件、每取消一个文件都会 +1。
        """
        with self._lock:
            self._share = int(port or 0)
            if rev is not None:
                self._share_rev = int(rev)
        self._wake.set()

    def set_screenshare(self, port):
        """更新本机广播的「屏幕共享」端口（0 = 停了）。

        立刻踢一次广播，队友最多 1 秒就看到成员行上的「看屏幕」按钮
        出现或消失。只广播端口，不广播观看码——观看码由房间码+密码派生，
        两边各自算，广播出去等于把准入凭证贴在广播包里。
        """
        with self._lock:
            self._ss = int(port or 0)
        self._wake.set()

    def announce_now(self, times=2):
        """**同步**把当前报文广播出去（不等后台线程的下一个周期）。

        用途是"临别通知"：退出房间前先把 share 置 0 再调它，队友那边立刻
        知道"这个人的云盘关了"，把他分享的文件从列表里撤下来。不这么做就
        只能等对端 HTTP 轮询超时——那要好几秒，用户会以为"人都走了文件还挂着"。
        """
        with self._lock:
            if not self._nick:
                return
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            return
        try:
            payload = self._payload()
            for _ in range(max(1, int(times))):
                try:
                    sock.sendto(payload, (VIRTUAL_NET_PREFIX + "255", NICK_PORT))
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def live_peers(self):
        """{ip: info}：信标缓存里**新鲜期内**的各成员信息。

        与 `live_shares` 是同一份数据的两个视角（那个只取云盘两个字段）。
        单独要一个全量视图，是因为成员发现不能只靠 ARP：

          * ARP 条目要**本机先有出站流量**才生成。刚进房时谁也不认识谁，
            只 ping 已知成员 = 新成员永远进不了列表；
          * ARP 条目会随时间老化，而 ping 需要对方应答 ICMP——wintun 网卡
            默认被归为「公用网络」，入站 ICMP 默认被 Windows 防火墙拦着
            （`enforce_virtual_net_trust` 能修好，但它是 best-effort）。

        信标广播是入站 UDP、按端口放行（见 _preflight_silence_windows），
        房间里每 3 秒一轮，再闲也在响——比 ARP 可靠得多。用户实测的
        "成员列表看不到好友 / 好友云盘的文件看不到"两条症状，根因都是
        只认 ARP（云盘轮询的 peer 列表同样来自 MemberTracker）。
        """
        now = time.monotonic()
        out = {}
        with self._lock:
            for ip, hit in self._peers.items():
                if float(hit.get("until") or 0.0) <= now:
                    continue
                out[ip] = dict(hit.get("info") or {})
        return out

    def live_shares(self):
        """{ip: (云盘端口, 清单版本号)}：信标缓存里各成员此刻的云盘状态。

        只认**新鲜期内**的记录——信标缓存是 UDP 收到就写，比对端的成员跟踪
        快一拍（跟踪器每 3 秒才回收一次），所以界面拿它判"对方刚动了文件"
        最灵敏。端口为 0 表示"对方没开云盘 / 刚关掉"。
        """
        now = time.monotonic()
        out = {}
        with self._lock:
            for ip, hit in self._peers.items():
                if float(hit.get("until") or 0.0) <= now:
                    continue
                info = hit.get("info") or {}
                out[ip] = (int(info.get("share") or 0),
                           int(info.get("srev") or 0))
        return out

    def get_info(self, ip, timeout=None):
        """查某虚拟 IP 的 {"name","game","port","share","srev","ss"}。

        信标缓存 → TCP 查询 → 都没有返回 {}（不是 None，调用方直接 .get）。
        """
        if not ip or ip == self._my_ip:
            return {}
        now = time.monotonic()
        with self._lock:
            hit = self._peers.get(ip)
            if hit and hit.get("until", 0) > now:
                return dict(hit.get("info") or {})
            last_fail = self._tcp_fail.get(ip, 0.0)
        if now - last_fail < self.TCP_FAIL_BACKOFF:
            return {}                       # 刚 TCP 查过失败，别反复试探
        info = self._tcp_query(ip, timeout or self.TCP_QUERY_TIMEOUT)
        if info:
            with self._lock:
                self._peers[ip] = {"info": info,
                                   "until": time.monotonic() + self.PEER_FRESH}
                self._tcp_fail.pop(ip, None)
            return dict(info)
        with self._lock:
            self._tcp_fail[ip] = time.monotonic()
        return {}

    def get_name(self, ip, timeout=None):
        """兼容接口：只要昵称（旧调用方与自检在用）。"""
        return self.get_info(ip, timeout).get("name", "")

    def _payload(self):
        """当前要广播的报文（每次现取，所以切换游戏/开关云盘后立刻生效）。"""
        with self._lock:
            nick, game, port, share = self._nick, self._game, self._port, self._share
            srev = self._share_rev
            ss = self._ss
        d = {_NICK_MAGIC: nick}
        if game:
            d[_NICK_GAME] = game
        if port:
            d[_NICK_PORT] = int(port)
        if share:
            d[_NICK_SHARE] = int(share)
            # 版本号只在开着云盘时有意义：关了就是"没有清单"，对端看键消失
            # 一样会当成"他关云盘了"，不必再传一个 0 去占字节。
            d[_NICK_SHARE_REV] = int(srev)
        if ss:
            d[_NICK_SS] = int(ss)
        return json.dumps(d, ensure_ascii=False).encode("utf-8")

    # ------------------------------------------------ 内部：三个小线程
    def _announce_loop(self):
        """定时向虚拟网广播自己的昵称 + 游戏快连。"""
        baddr = VIRTUAL_NET_PREFIX + "255"
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while not self._stop_evt.is_set():
                self._wake.clear()
                if self._nick:
                    try:
                        sock.sendto(self._payload(), (baddr, NICK_PORT))
                    except Exception:
                        pass
                self._wake.wait(self.ANNOUNCE_INTERVAL)
        except Exception:
            pass
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def _udp_listen_loop(self):
        """收别人的广播，维护 ip → 成员信息 新鲜表。"""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._my_ip, NICK_PORT))
            sock.settimeout(0.5)
            while not self._stop_evt.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError:
                    break
                info = self._parse_info(data)
                if info and addr[0] != self._my_ip:
                    with self._lock:
                        self._peers[addr[0]] = {
                            "info": info,
                            "until": time.monotonic() + self.PEER_FRESH,
                        }
        except OSError as exc:
            # 绑定失败（端口被占 / 虚拟 IP 还没就绪）必须**说出来**：收不到
            # 广播的表现是"昵称不更新、云盘端口永远是 0、聊天没动静"，界面上
            # 却毫无线索——正是最难查的那类静默故障。TCP 那条路一直有日志
            # （见 _tcp_serve_loop），UDP 这条补上。
            self._on_log("昵称信标监听失败（此后收不到队友广播：昵称、云盘"
                         "端口、聊天都会停更）：%s" % exc)
        except Exception:
            pass
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def _tcp_serve_loop(self):
        """TCP 查询兜底：连上就把本机报文写过去，立即关闭。"""
        srv = None
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self._my_ip, NICK_PORT))
            srv.listen(4)
            srv.settimeout(0.5)
            while not self._stop_evt.is_set():
                try:
                    conn, _addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    conn.settimeout(2.0)
                    conn.sendall(self._payload())
                except Exception:
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
        except Exception as exc:
            self._on_log("昵称信标监听失败（不影响联机）：%s" % exc)
        finally:
            if srv:
                try:
                    srv.close()
                except Exception:
                    pass

    def _tcp_query(self, ip, timeout):
        """主动向对方查成员信息（出站连接，无防火墙问题）。"""
        if not self._threads:
            return {}
        try:
            with socket.create_connection((ip, NICK_PORT), timeout=timeout) as c:
                c.settimeout(timeout)
                return self._parse_info(c.recv(1024)) or {}
        except Exception:
            return {}

    @staticmethod
    def _parse_info(data):
        """解析信标报文 → {"name","game","port","share","srev"}；
        不是本协议的报文返回 None。"""
        try:
            d = json.loads((data or b"").decode("utf-8"))
        except Exception:
            return None
        if not isinstance(d, dict) or _NICK_MAGIC not in d:
            return None
        try:
            port = int(d.get(_NICK_PORT) or 0)
        except (TypeError, ValueError):
            port = 0
        try:
            share = int(d.get(_NICK_SHARE) or 0)
        except (TypeError, ValueError):
            share = 0
        try:
            srev = int(d.get(_NICK_SHARE_REV) or 0)
        except (TypeError, ValueError):
            srev = 0
        try:
            ss = int(d.get(_NICK_SS) or 0)
        except (TypeError, ValueError):
            ss = 0
        return {
            "name": str(d.get(_NICK_MAGIC) or "").strip()[:32],
            "game": str(d.get(_NICK_GAME) or "").strip()[:24],
            "port": port,
            "share": share,
            "srev": srev,
            "ss": ss,
        }


# ---------------------------------------------------------------------------
# 虚拟网卡"信任化"：达到 Radmin VPN 的开箱即用体验
# ---------------------------------------------------------------------------
# 用户实测：真实局域网能玩 MC、Radmin VPN 能玩、唯独 EasyTier 不能
# （列表看得到人、游戏列表没人、IP 直连也不通）。原因有两层：
#
#   1. **防火墙网络配置文件**：wintun 虚拟网卡被 Windows 归为
#      「未识别网络 = 公用 profile」，公用默认阻止一切入站——
#      javaw.exe 没有入站白名单，IP 直连都会被丢。而 Radmin 的网卡
#      被归为「专用」，用户当年弹窗点过的"允许 Java(专用)"直接生效。
#      这解释了为什么唯独 Yuhub 不通而 Radmin 通。
#   2. 广播发现依赖中继旁路搬运（不可控），但 IP 直连是稳的——
#      只要防火墙放行。
#
# 修法（本函数必须在**提权**的 watchdog 里调用，主进程没有权限）：
#   ① 等虚拟网卡出现（DHCP 拿到 10.126.126.x）；
#   ② Set-NetConnectionProfile 把它设为「专用」——用户历史点过的
#      Java 放行规则立即生效；
#   ③ 再补一条**按接口别名**的入站放行规则（虚拟网里都是持同密码的
#      伙伴，等同可信局域网）——连当年没点过允许的机器也一并覆盖。
# 每次进房都要重跑：wintun 网卡每次启动都会重建，规则/配置绑定的是
# 新网卡实例。
TRUST_RULE_NAME = "Yuhub 联机游戏放行"

_PS_FIND_ADAPTER = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$ip = Get-NetIPAddress | Where-Object { $_.IPAddress -like '"
    + VIRTUAL_NET_PREFIX + "*' } | Select-Object -First 1;"
    "if ($ip) { Write-Output ('FOUND ' + $ip.InterfaceAlias + ' ' + $ip.IPAddress) }"
    "else { Write-Output 'WAIT' }"
)

_PS_APPLY_TRUST = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$ip = Get-NetIPAddress | Where-Object { $_.IPAddress -like '"
    + VIRTUAL_NET_PREFIX + "*' } | Select-Object -First 1;"
    "if (-not $ip) { Write-Output 'NOADAPTER'; exit };"
    "Set-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex"
    " -NetworkCategory Private;"
    "Remove-NetFirewallRule -DisplayName '" + TRUST_RULE_NAME + "'"
    " -ErrorAction SilentlyContinue;"
    "$null = New-NetFirewallRule -DisplayName '" + TRUST_RULE_NAME + "'"
    " -Direction Inbound -Action Allow"
    " -InterfaceAlias $ip.InterfaceAlias -Profile Any;"
    "Write-Output ('OK ' + $ip.InterfaceAlias + ' ' + $ip.IPAddress)"
)


def _ps_run(command, timeout=45):
    """跑一条 PowerShell（无窗口）。输出按 UTF-8 宽松解码。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", command],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def enforce_virtual_net_trust(on_log=None, timeout=60.0):
    """等虚拟网卡就绪 → 设为专用网络 + 放行入站。返回是否成功。

    由 watchdog（提权进程）在拉起 easytier-core 之后调用；每次进房
    都会执行一遍（网卡是新建的，规则要重新绑定到它的接口别名）。
    """
    on_log = on_log or (lambda msg: None)
    deadline = time.monotonic() + timeout
    alias = ip = ""
    while time.monotonic() < deadline:
        out = _ps_run(_PS_FIND_ADAPTER, timeout=30)
        if out.startswith("FOUND"):
            parts = out.split()
            if len(parts) >= 3:
                alias, ip = parts[1], parts[2]
                break
        time.sleep(3.0)
    if not alias:
        on_log("虚拟网卡信任化：等待网卡超时，本次跳过")
        return False

    out = _ps_run(_PS_APPLY_TRUST, timeout=60)
    if out.startswith("OK"):
        on_log("虚拟网卡已设为专用网络并放行游戏入站（%s）" % alias)
        return True
    on_log("虚拟网卡信任化失败（不影响联机本身，但游戏可能被防火墙拦截）")
    return False


def virtual_net_trust_status():
    """查询当前信任化状态（只读，供自检用）。返回 (profile, rule_exists)。

    profile: 'Private' / 'Public' / ''（查不到网卡）；rule_exists: bool。
    """
    out = _ps_run(
        "$ErrorActionPreference='SilentlyContinue';"
        "$ip = Get-NetIPAddress | Where-Object { $_.IPAddress -like '"
        + VIRTUAL_NET_PREFIX + "*' } | Select-Object -First 1;"
        "if (-not $ip) { Write-Output 'NOADAPTER'; exit };"
        "$p = Get-NetConnectionProfile -InterfaceIndex $ip.InterfaceIndex;"
        "$r = Get-NetFirewallRule -DisplayName '" + TRUST_RULE_NAME + "';"
        "Write-Output ('CAT=' + $p.NetworkCategory + ' RULE=' + [bool]$r)",
        timeout=45,
    )
    cat = ""
    if "CAT=Private" in out:
        cat = "Private"
    elif "CAT=Public" in out:
        cat = "Public"
    return cat, ("RULE=True" in out)


def pick_host_ip(my_ip, exclude_ips=None, hostname="", wait=0.0, interval=0.8):
    """在虚拟网里找出**房主**的 IP（DHCP 动态分配下用）。

    为什么需要它：以前房主固定 10.126.126.1，成员直接 ping 那个地址就能
    判断"房间是否存在"。改用 DHCP 后房主 IP 不再固定，必须动态发现。

    判定顺序（从可信到将就）：
      1. `hostname` 匹配 —— 房主在界面上填的昵称能被反查出来，这是
         **最可信**的判据。房主自己知道自己叫什么，所以自己先试这一条。
      2. 回落到"网段里除了已知成员外，还能 ping 通的那个" —— 新成员
         加入时其他成员是它没见过的，谁通谁就是房间里的老节点。

    为什么要 `exclude_ips`：在**已经**连上房间的成员视角下，网段里会
    同时出现房主和其他成员。准入校验要的是"房间里有没有别人"，对成员来说
    这是天然成立的（它已经连上了），所以这个方法主要用于**新加入者**——
    对新加入者来说，除自己外的一切都是它第一次见到的。

    注意这是"尽力而为"的启发式：它无法 100% 区分"房主"和"另一个成员"。
    真要精确到房主需要应用层信令（引入中心服务器），而准入校验真正要
    回答的问题是"房间码+密码对不对"，这个启发式已经足够。

    my_ip：本机虚拟 IP（必须排除自己）
    返回发现的 IP，找不到返回 ""。
    """
    me = my_ip or virtual_adapter_ip()
    skip = set(exclude_ips or ())
    skip.add(me)
    end = time.monotonic() + wait
    while True:
        candidates = [ip for ip in list_members(me) if ip not in skip]
        if hostname:
            # ① 按昵称精确匹配（房主自报家门，最可信）
            for ip in candidates:
                if _resolve_name(ip, timeout=0.8).lower() == hostname.lower():
                    return ip
        # ② 谁能 ping 通就算数（房间里有活人 = 码和密码是对的）
        for ip in candidates:
            if probe_host(ip, timeout=1.5, attempts=1):
                return ip
        if time.monotonic() >= end:
            return ""
        time.sleep(interval)



def list_members(my_ip=""):
    """列出当前跨网网络里的在线成员（虚拟 IP 列表，不含自己）。

    原理：EasyTier 建连后，对端节点的虚拟 IP 会出现在本机 ARP 表里
    （10.126.126.0/24 网段）。用 `arp -a` 抓取该网段的所有条目，
    过滤掉本机自身 IP，剩下的就是在线成员。比 ping 探测更快更准。
    """
    members = []
    try:
        out = subprocess.run(
            ["arp", "-a"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=8, creationflags=_CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return members
    seen = set()
    for line in out.splitlines():
        # 只抓 10.126.126.x 网段的条目
        if VIRTUAL_NET_PREFIX not in line:
            continue
        parts = line.split()
        for p in parts:
            if p.startswith(VIRTUAL_NET_PREFIX):
                ip = p.strip()
                # 排除自己、广播地址 .255、网络地址 .0、以及网关等非主机地址
                last = ip.rsplit(".", 1)[-1]
                if last in ("0", "255"):
                    continue
                if ip != my_ip and ip not in seen:
                    seen.add(ip)
                    members.append(ip)
    return members


def core_running():
    """easytier-core.exe 是否在运行（tasklist 查询，普通权限即可）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq easytier-core.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=8, creationflags=_CREATE_NO_WINDOW,
        )
        return "easytier-core.exe" in (out.stdout or "")
    except Exception:
        return False


def force_kill_core():
    """强制结束所有 easytier-core.exe（用于清理切换网络后残留的进程）。

    看门狗可能因为网络切换等异常没来得及收尾，留下孤儿进程；再启动时
    core_running() 恒真、虚拟网卡又拿不到 IP，就会一直卡在「已有房间在运行」。
    这里用 taskkill /F 直接清掉（普通权限即可杀自己提权拉起的同用户进程）。
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "easytier-core.exe"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=10, creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        pass
    # 清掉可能残留的停止信号文件
    try:
        os.remove(_STOP_FILE)
    except OSError:
        pass


def _preflight_silence_windows(core_exe_path):
    """启动 easytier-core 之前：消除 Windows 两个常见弹窗。

    1) **Windows Defender 防火墙**：第一次以网络应用身份启动 easytier-core 时
       会弹"已阻止 easytier-core.exe 的部分功能"白弹窗。用 `netsh advfirewall`
       给该 exe 同时加 inbound + outbound 例外规则，**任何端口都允许**。
       这一步要管理员权限——本函数本来就在 watchdog 上下文（已 runas 提权）跑。
       规则已存在就静默跳过，不报错。

    2) **新网络连接通知弹窗**：wintun 第一次创建虚拟网卡时 Windows 会弹
       "选择网络位置（家庭/工作/公共）"——这是注册表控制项：
       `HKLM\\System\\CurrentControlSet\\Control\\Network\\NewNetworkNotify`
       设为 DWORD=0 即可彻底禁用，且不影响其它网络提示。
       也会改 `NlaSvc\\AlwaysAllowApp` 让网络位置感知不再追问。

    这两个修改都是注册表 / 防火墙的标准做法，影响范围限定在 easytier-core.exe
    这一条规则，**不会影响系统的其它行为**。
    """
    # 1) 防火墙例外：inbound + outbound 各一条即可（protocol=any 同时覆盖 TCP+UDP）。
    #    规则名如果已存在，netsh 会报错"对象已存在"——一律 try/except 静默吃掉，
    #    不要中途 continue 掉另一半 inbound/outbound。
    rule_name = "Yuhub EasyTier (允许异机联机)"
    for direction in ("in", "out"):
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule",
             "name=" + rule_name],
            capture_output=True, encoding="gbk", errors="replace",
            timeout=8, creationflags=_CREATE_NO_WINDOW,
        )
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             "name=" + rule_name,
             "dir=" + direction,
             "action=allow",
             "program=" + core_exe_path,
             "protocol=any",
             "enable=yes"],
            capture_output=True, encoding="gbk", errors="replace",
            timeout=10, creationflags=_CREATE_NO_WINDOW,
        )

    # 2) 关闭"新网络连接通知"对话框：写注册表 DWORD=0
    try:
        import winreg
        key_path = r"System\CurrentControlSet\Control\Network\NewNetworkNotify"
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "NewNetworkNotify", 0, winreg.REG_DWORD, 0)
    except Exception:
        pass

    # 3) 网络位置感知 NLA：禁止对这台电脑自动弹位置询问
    #    改 `HKLM\\SYSTEM\\CurrentControlSet\\Services\\NlaSvc\\Parameters\\Internet`
    #    加 ActiveConnections = 0 是不彻底的；正确做法是关闭 NLA 的"主动探测"。
    #    但简单且稳妥的是把 `HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\
    #    NetworkList\\NlaSvc\\NewNetworks` 的 DWORD `NewNetwork` 设为 0：
    #    这个是 NLA 服务自己读的配置，改完服务下次重启生效。
    #    用户场景下，easytier 第一次启动可能仍弹一次，重启后再启动就完全不弹。
    try:
        import winreg
        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE,
            r"Software\Microsoft\Windows NT\CurrentVersion\NetworkList\NlaSvc",
            0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "NewNetworks", 0, winreg.REG_DWORD, 0)
    except Exception:
        pass


    # 4) **昵称信标 + 房间聊天端口**：Yuhub.exe 要在虚拟网卡上收 UDP 广播、
    #   接受 TCP 查询（NickBeacon 端口 NICK_PORT，RoomChat 端口 CHAT_PORT）。
    #   虚拟网卡默认是"公共网络"配置文件，Windows 默认拦入站——没有这条规则，
    #   广播收不到、TCP 查询应不上，昵称和聊天都会**静默失效**（界面正常、
    #   就是收不到别人的东西）。watchdog 正好是提权进程，顺手放行；范围严格
    #   限定"本程序 + 这两个端口"。
    #    旧规则名一并删掉：改名是为了让规则名说得清是干什么的，但只加不删
    #    会在升级用户的机器上留下一条永远没人管的孤儿规则。
    beacon_rule = "Yuhub 联机信标与聊天"
    own_exe = os.path.abspath(sys.executable)
    for stale in ("Yuhub 联机昵称信标", beacon_rule):
        for direction in ("in", "out"):
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "delete", "rule",
                 "name=" + stale],
                capture_output=True, encoding="gbk", errors="replace",
                timeout=8, creationflags=_CREATE_NO_WINDOW,
            )
    for proto in ("udp", "tcp"):
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             "name=" + beacon_rule,
             "dir=in",
             "action=allow",
             "program=" + own_exe,
             "protocol=" + proto,
             "localport=%d,%d" % (NICK_PORT, CHAT_PORT),
             "enable=yes"],
            capture_output=True, encoding="gbk", errors="replace",
            timeout=10, creationflags=_CREATE_NO_WINDOW,
        )


def _physical_network_fingerprint():
    """本机物理网络的指纹：非虚拟网段（排除 10.126.126.x 和常见虚拟网卡段）的
    IPv4 集合 + 默认网关。网络一换（WiFi→热点、换 WiFi）指纹就变，用于检测
    「切换网络导致虚拟局域网失效」这个场景。

    返回一个可比较的元组（排序后的 IP 列表 + 网关），失败返回 None。
    """
    ips = []
    for ip in _ip_addresses():
        # 排除 EasyTier 虚拟网段
        if ip.startswith(VIRTUAL_NET_PREFIX):
            continue
        # 排除回环
        if ip.startswith("127."):
            continue
        ips.append(ip)
    gateway = _default_gateway()
    return (tuple(sorted(ips)), gateway)


def _default_gateway():
    """用 route print 抓默认网关（0.0.0.0 目标）。失败返回空串。"""
    try:
        out = subprocess.run(
            ["route", "print", "0.0.0.0"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=8, creationflags=_CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return ""
    # 取 0.0.0.0 0.0.0.0 那一行的网关（下一跳）列，IPv4 段里
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            gw = parts[2]
            # 只认合法 IPv4
            if gw.count(".") == 3 and all(p.isdigit() for p in gw.split(".")):
                return gw
    return ""



# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
class EasyTier:
    """一个跨网房间对应一个实例。线程安全（内部有锁）。"""

    def __init__(self, on_log=None):
        self._lock = threading.Lock()
        self._on_log = on_log or (lambda msg: None)
        self._started_at = 0.0
        self._base_fingerprint = None    # 启动时的物理网络指纹（用于检测切换网络）
        #: 本次房间的管理 RPC portal 端口（启动时探得的空闲口）。
        #: CorePeerFeed 要用同一个口去连 CLI——两边必须一致。
        self._rpc_port = RPC_PORT

    def rpc_port(self):
        """本次房间的管理 portal 端口（成员表数据源用）。"""
        return int(self._rpc_port)

    def _cleanup_residual(self):
        """清理上次会话残留的 easytier-core 进程。返回是否已清理干净。

        策略从轻到重（每级都比上一级更"重"、体验更差）：
          1. **写停止信号** —— 如果上次的 watchdog 还活着，它会自己在 500ms
             内收尾（零 UAC 弹窗、零窗口闪烁）。这是最常见且最干净的情况。
          2. **普通权限 taskkill** —— watchdog 死了、easytier-core 是普通权限
             起的（少见）时有效。
          3. **提权 --kill-core** —— 最后兜底，会弹一次 UAC。watchdog 死了但
             easytier-core 是提权起的，只有这条路能杀掉。
        """
        # 1. 写停止信号，等 watchdog 自己收（~2 秒）
        self.request_stop()
        if self.wait_stopped(2.0):
            return True
        # 2. 普通权限直接杀
        self._on_log("停止信号未生效，尝试直接结束进程…")
        force_kill_core()
        time.sleep(0.5)
        if not core_running():
            return True
        # 3. 提权兜底（弹一次 UAC）
        self._on_log("需要管理员权限才能结束残留进程，请在 UAC 弹窗点「是」…")
        ok, msg = kill_core_elevated()
        if not ok:
            self._on_log(f"提权清理失败：{msg}")
            return False
        time.sleep(1.5)                 # 给提权进程一点时间跑完 taskkill
        return not core_running()

    def start(self, name, secret, timeout=20.0, ipv4="", hostname="",
              node="", servers=None):
        """创建/加入跨网网络。阻塞直到虚拟网卡就绪或超时。

        name/secret 即 EasyTier 的 network-name / network-secret。
        ipv4：固定虚拟 IP；传空串则走 DHCP 动态分配（推荐）。
              动态分配由 EasyTier 的 DHCP 完成，**遇到 IP 冲突会自动改地址**，
              所以多人/多房间并存时不会互相抢 IP（固定 IP 会）。
              代价是 IP 不固定，不能靠"猜房主 IP"来做准入校验，改由
              `host_ip()` 动态发现（见 lan_page 的成员校验）。
        hostname：本节点在房间里的显示名（队友在成员列表里看到的就是它）；
                  为空则 EasyTier 用系统计算机名（DESKTOP-XXXX 不好认）。
        node：中继节点（MCTier 式确定性选择）。空串 = 自动，
              把 PUBLIC_SERVERS 全部传给 EasyTier；非空 = **只连这一个**，
              不叠加其它节点——否则"用户选的节点有问题"和"房间不通"
              会混在一起没法排查。
        servers：**auto 模式下**由调用方给定的候选节点（可传按测速筛过的
              子集，见 lan_page._servers_for_auto）。None/空则退回全部
              PUBLIC_SERVERS。`node` 非空时忽略本参数。
        返回 (成功, 说明)。成功后 virtual_adapter_ip() 可查虚拟 IP。
        """
        with self._lock:
            # 关键修复：切换网络 / 上次异常退出后可能残留孤儿 easytier-core 进程，
            # 导致 core_running() 恒真、永远提示「已有一个房间在运行」。
            # lan_page 保证了 start() 只在"本会话没启动过"时被调用，所以这里
            # 遇到的 core_running() **一定是上会话的残留**，直接清掉。
            if core_running():
                self._on_log("检测到上次残留的 EasyTier 进程，正在清理…")
                if not self._cleanup_residual():
                    return False, ("无法结束上次残留的 EasyTier 进程，"
                                   "请在任务管理器里手动结束 easytier-core.exe 后重试")
                self._on_log("残留进程已清理，继续启动")
            ok, msg = release_binaries()
            if not ok:
                self._on_log(f"EasyTier 释放失败：{msg}")
                return False, msg
            # 清掉上次可能残留的停止信号
            try:
                os.remove(_STOP_FILE)
            except OSError:
                pass
            # 记录启动时的物理网络指纹，供 run 期间检测网络切换
            self._base_fingerprint = _physical_network_fingerprint()
            # Pre-flight：启动 easytier-core 之前先消除 Windows 自身的两个白弹窗。
            # 这一步要管理员权限（netsh advfirewall + 写注册表），所以放在 watchdog
            # 进程里跑——etier.py 当前是普通权限，写不了。返回 task 给 watchdog。
            self._on_log("准备：watchdog 会先静默配置防火墙例外 + 抑制新网络通知，再启 easytier-core")
            # 把"启动 easytier-core + 轮询停止信号"打包成一个 base64 payload，
            # 通过 `Yuhub.exe --watchdog <payload>` 提权启动。watchdog 是 GUI
            # 子系统进程 + 用 CREATE_NO_WINDOW 启 easytier-core，**不闪窗**。
            #
            # 🔴 参数一律走 core_args() 一个出口。曾经这里手搓过一份 args，
            # 里面带着 `--no-listener`——虽然紧接着就被下面的 core_args 覆盖
            # （死代码），但留着它就是留着雷：LISTEN_PORT 的注释记载着
            # `--no-listener` 是"异地分屏只有 4 帧"的根因（没有监听端口，
            # P2P 打洞没有落点，流量全走限速的公共中继）。谁要是"清理"时
            # 留错那一份， bug 就回来了。组装只认 core_args。
            servers = (node,) if node else tuple(servers or PUBLIC_SERVERS)
            if node:
                # 确定性节点选择（MCTier 实践）：用户选了哪个就只连哪个
                self._on_log("中继节点（手动指定）：%s" % node)
            else:
                self._on_log("中继节点：自动（共 %d 个，失败自动换下一个）"
                             % len(servers))
            # 管理 portal 端口：先探一个空闲的。用户自装的 EasyTier 会占着
            # 官方默认的 15888，而 core 的 portal 绑定失败 = core 直接退出
            # （v2.6.4 core.rs 里那行的 `?`），所以必须显式挑一个空闲口。
            self._rpc_port = pick_rpc_port()
            args = core_args(name, secret, hostname=hostname, ipv4=ipv4,
                             servers=servers, rpc_port=self._rpc_port)
            payload = build_watchdog_payload(
                core_path(), args, _STOP_FILE, install_dir(),
                parent_pid=os.getpid(),
                # 用 watchdog 实际会看到的父进程名做防 PID 复用校验。
                # 注意不能用 sys.executable 的 basename：源码态下那是 python.exe，
                # 而 watchdog 眼里父进程是"拉起它的那个 Yuhub.exe"，对不上就会
                # 误判父进程已死 → 刚起来就自我收尾，表现为"一直正在启动"。
                parent_name=_parent_process_name(),
            )
            ok, msg = launch_elevated(payload)
            if not ok:
                self._on_log(f"EasyTier 启动失败：{msg}")
                return False, msg
            self._started_at = time.monotonic()
            # 轮询等虚拟网卡就绪（首次要装 wintun 适配器，可能十几秒）。
            #
            # "进程意外退出"的判定必须**从 easytier-core 真的出现过之后**才开始算：
            # 用户在 UAC 弹窗上停留的时间是不确定的（可能去接杯水），
            # 这段时间 core 本来就没在跑。若拿"发起提权"当起点，
            # 只要用户点"是"慢了 3 秒，就会误报"进程意外退出"。
            saw_core = False
            core_seen_at = 0.0
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                running = core_running()
                if running and not saw_core:
                    saw_core = True
                    core_seen_at = time.monotonic()
                    self._on_log("easytier-core 已拉起，正在等待虚拟网卡就绪…")
                if not running and saw_core and time.monotonic() - core_seen_at > 6:
                    # 出现过又消失 → 才是真的异常退出
                    return False, ("EasyTier 进程已退出（可能是密码含特殊字符、"
                                   "或公共节点均不可达）")
                ip = virtual_adapter_ip()
                if ip:
                    self._on_log(f"跨网网卡就绪，虚拟 IP {ip}")
                    return True, ip
                time.sleep(0.8)
            # 超时：区分"根本没起来"和"起来了但网卡没就绪"，提示要对得上
            if not saw_core:
                return False, ("%d 秒内未等到 EasyTier 启动。请确认已在 UAC 弹窗里"
                               "点「是」授权；若没有弹窗，可能是被安全软件拦截了。"
                               % int(timeout))
            return False, ("EasyTier 已运行但虚拟网卡未就绪（等待 %d 秒）。"
                           "首次创建虚拟网卡较慢，可稍后在「重新检测」或重试；"
                           "若反复失败，检查是否有其他 VPN 软件占用了网卡驱动。"
                           % int(timeout))

    def request_stop(self):
        """**同步**写出停止信号文件（瞬时完成，不等看门狗收尾）。

        用途：Yuhub 退出（关窗口 / 托盘退出）时调用。**不能**用 daemon
        线程去调 stop()——主进程退出会立刻杀掉 daemon 线程，"写停止信号"
        这一步都可能还没执行，看门狗收不到信号、easytier-core 就永久残留
        （下次打开 Yuhub 会看到"已有一个房间正在运行"）。
        纯文件写入是同步的、微秒级完成，退出路径上一定跑得完。
        """
        try:
            os.makedirs(os.path.dirname(_STOP_FILE), exist_ok=True)
            with open(_STOP_FILE, "w") as f:
                f.write("stop")
            self._base_fingerprint = None
            return True
        except OSError:
            return False

    def wait_stopped(self, timeout=2.0):
        """等 easytier-core 退出，最多 timeout 秒。返回是否已退出。

        退出路径上给个短预算（例如 2 秒）——看门狗轮询间隔 500ms，
        正常情况 1 秒内就收尾了；等不到也**不影响清理**，看门狗会继续
        处理（它是独立进程，不随 Yuhub 退出而消失）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not core_running():
                return True
            time.sleep(0.3)
        return False

    def stop(self):
        """停止跨网网络。写停止信号，看门狗负责清理。零 UAC 弹窗。"""
        with self._lock:
            if not core_running():
                return True, "没有在运行的跨网房间"
            self.request_stop()
            # 等看门狗干活
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if not core_running():
                    self._base_fingerprint = None
                    return True, "已停止"
                time.sleep(0.4)
            # 看门狗没在 8 秒内收掉，直接强杀兜底
            force_kill_core()
            self._base_fingerprint = None
            return True, "已停止（强制清理）"

    def network_changed(self):
        """检测运行期间物理网络是否切换（WiFi→热点 / 换 WiFi）。

        返回 True 表示网络已变、虚拟局域网大概率已失效，应自动关闭。
        仅在启动时成功记录了基线指纹的情况下判断；基线为空则视为未变化。
        """
        if self._base_fingerprint is None:
            return False
        now = _physical_network_fingerprint()
        if now is None:
            return False
        # 网关变了或物理 IP 集合变了，都算切换网络
        return now != self._base_fingerprint

    def status(self):
        """给 UI 轮询的快照。"""
        ip = virtual_adapter_ip()
        return {
            "running": core_running(),
            "ip": ip,
            "uptime": time.monotonic() - self._started_at if self._started_at else 0,
        }

