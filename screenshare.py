# -*- coding: utf-8 -*-
"""屏幕共享引擎：把本机画面用一条 TCP 流推给另一台装了 Yuhub 的电脑。

为什么是自研的轻量流，而不是继续用 WebRTC
----------------------------------------
第一版内嵌的是开源项目 Piik（WebRTC + 内嵌浏览器控制台）。画质和延迟都更好，
但代价是随包多带三样东西：203MB 的 `Qt6WebEngineCore.dll`、54.8MB 的
`cloudflared.exe`、46.4MB 的 `piik-app.exe` —— exe 从 63MB 涨到 270MB，
启动从 1.9 秒变成 8 秒。

而"看别人的屏幕"这件事，在链路已经打通的前提下并不需要 WebRTC：
异地联机（EasyTier）已经给了双方一个能互通的虚拟局域网，局域网模式下双方
本来就在同一个路由器下。两条链路都只需要**一条 TCP 连接**。于是改成：

    发送端 抓屏 → 缩放 → JPEG → 一条 TCP 流广播
    接收端 收流 → 解成图片 → 显示

随包不再多任何一个二进制，界面也回到 Yuhub 自己的主题里。

代价要说清楚：这条路子没有 WebRTC 的自适应码率，异地经中继时带宽有限，
所以走"降分辨率 + 限帧率 + 画面没变就不重发"来省流量（见 `push`）。

协议
----
握手一行文本：   `YUHUB-SHARE/1 <观看码>\n`
服务端回一行：   `OK <主机名>\n`  或  `ERR <原因>\n`
之后是二进制帧，每帧 17 字节头 + 负载（大端）：

    b"YSH1" | type(1) | seq(4) | w(2) | h(2) | len(4)
    type = 1  JPEG 帧（len = 负载长度）
    type = 2  画面没变（心跳，len = 0）

一条连接只传"我的屏幕"，所以不做多路复用。
"""

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import os
import secrets
import select
import socket
import struct
import threading
import time

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------
PROTOCOL = "YUHUB-SHARE/1"
MAGIC = b"YSH1"

#: 4s(魔数) + B(类型) + I(序号) + H(宽) + H(高) + I(负载长度) = 17 字节。
#: 用 ">"（大端标准尺寸）保证跨平台不会有对齐填充。
HEADER = struct.Struct(">4sBIHHI")

TYPE_FRAME = 1
TYPE_IDLE = 2

#: 默认端口。固定下来是为了"异地联机"里能免输入——房间里的人只要知道
#: 对方开了共享，直接连这个端口就行（观看码另算）。
DEFAULT_PORT = 45890

#: 同时观看人数上限。每多一个观看者就多一份发送线程与一份帧缓冲，
#: 轻量路子不做"给几十个人直播"。
MAX_VIEWERS = 8

#: 握手行最长长度，防止有人往这里灌垃圾。
MAX_HELLO = 256

#: 画质挡位：(名称, 最长边, 帧率, JPEG 质量)。
#:
#: 帧率能开到 30，靠的是界面层把「抓屏」和「JPEG 编码」拆成两条线程：
#: 实测抓屏 18.2ms、编码 12.3ms（1600px），串行只能跑 32.8fps（几乎没有
#: 余量，系统稍一忙就掉帧）；流水线后吞吐取两者最大值 → 54~60fps，30 帧
#: 才有真正的冗余。单纯把这里的数字改大是没用的，串行那条路撑不住。
#:
#: 带宽参考（桌面内容，实测量级）：流畅约 1.5MB/s、标准约 3.4MB/s、
#: 清晰约 5.5MB/s、原画可到 9MB/s。异地经中继时上限通常只有几 MB/s，
#: 所以异地共享建议停在「标准」；局域网随便选。
QUALITY_PRESETS = [
    ("流畅", 1280, 20, 58),
    ("标准", 1600, 30, 65),
    ("清晰", 1920, 30, 72),
    ("原画", 2560, 30, 78),
]
DEFAULT_QUALITY = 1

#: 各挡位的带宽提示，界面拿来做 tooltip。数值是桌面内容的实测中位数，
#: 换成游戏/视频会更高 —— 所以文案里写"约"。
QUALITY_BANDWIDTH = ["约 1.5 MB/s", "约 3.4 MB/s", "约 5.5 MB/s", "约 9 MB/s"]

#: 判定"这一帧和上一帧是不是同一个画面"时的采样步长（字节）。
#:
#: 为什么不逐字节比较 —— 实测本机桌面上**总有一个 20×20 左右的小块在闪**
#: （某个动画指示器或输入光标），每帧稳定产生约 390 字节差异，只占画面
#: 的 0.006%。逐字节比较因此**从不命中**，"画面没变就只发心跳"这条省
#: 流量优化等于白写：桌面明明没动，也照样按满帧率把 3.6MB/s 灌出去。
#: 按 1/4 采样之后这种微小抖动会被忽略，而真正的画面变化（窗口滚动、
#: 放视频、拖窗口）面积远大于采样间隔，依然必然命中。
SAMPLE_STEP = 4

#: 就算判定成"画面没变"，也最多隔这么久（秒）强制发一帧真画面。
#: 采样终究是概率性的：万一漏掉了一处真实变化，观看端最多等这么久就
#: 自己纠正回来，不会永久停在旧画面上。
IDLE_REFRESH = 1.0

#: 画面没变时，心跳的最小发送间隔（秒）。
#: 心跳唯一的作用是"告诉对端我还活着"，30/s 是浪费：接收端判死要连续 3 次
#: 超时（socket 超时 15 秒），2/s 已经绰绰有余。
HEARTBEAT_INTERVAL = 0.5


def frame_signature(raw):
    """给一帧原始 BGRA 像素算一个"是不是同一个画面"的廉价签名。

    只取 1/SAMPLE_STEP 的字节，所以**有意**放过细小的差异（屏幕上某个
    小指示器在闪、鼠标划过留下的一点残影）——那些变化不值得按满帧率
    把整帧重发一遍。真正成片的变化（滚动、放视频、拖窗口）面积远大于
    采样间隔，一定会在签名里体现出来。

    成本实测约 0.3ms（1600×1000），相对 JPEG 编码的 12ms 可以忽略。
    判定的取舍由 `screenshare_selftest` 用合成数据守着（不依赖屏幕状态）。
    """
    return raw[::SAMPLE_STEP]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def make_token(room_code, password):
    """由「房间码 + 密码」派生观看码。

    刻意和 `lan_share.make_token` 保持同一口径：同一个房间的人算出来必然
    一致（两边输入本来就要求完全相同），虚拟网外的请求拿不到这个值，因此
    房间成员点「看屏幕」不需要手输观看码。

    为什么不去 import 那个函数：屏幕共享和临时云盘是两件独立的事，一个的
    改动不该悄悄改掉另一个的准入规则。两边口径一致这件事由自检守着
    （`screenshare_selftest` 里有一条专门断言两者结果相等）。
    """
    raw = ("%s|%s" % (room_code or "", password or "")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def random_token():
    """局域网共享用的 6 位观看码。

    用 `secrets` 而不是 `random`：观看码是唯一的准入门槛，可预测的随机数
    等于没有门槛。零填充成 6 位，用户念起来方便。
    """
    return "%06d" % secrets.randbelow(1000000)


def _is_usable(ip):
    if not ip or ip.startswith("127.") or ip.startswith("169.254."):
        return False
    return ip != "0.0.0.0"


def primary_lan_ip():
    """默认路由那张网卡的 IPv4。

    靠"UDP connect 到公网地址"拿本机首选源地址——UDP 不会真的发包，
    只是让系统把路由决策做一遍，因此不需要联网、也不会真的连出去。
    """
    for probe in ("8.8.8.8", "1.1.1.1", "223.5.5.5"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.settimeout(0.4)
                s.connect((probe, 53))
                ip = s.getsockname()[0]
            finally:
                s.close()
            if _is_usable(ip):
                return ip
        except OSError:
            continue
    return ""


# `gethostbyname_ex` 拿不到没注册 DNS 的网卡（EasyTier 那种 wintun 网卡就
# 经常不在里面），所以地址表走 iphlpapi 的 GetIpAddrTable。手写
# GetAdaptersAddresses 的链表结构字段极多，偏移错一位就是段错误（实测崩过），
# 这里只要一个 7 字段的 MIB_IPADDRROW，稳。
class _MibIpAddrRow(ctypes.Structure):
    _fields_ = [("dwAddr", wt.DWORD), ("dwIndex", wt.DWORD),
                ("dwMask", wt.DWORD), ("dwBCastAddr", wt.DWORD),
                ("dwReasmSize", wt.DWORD), ("unused1", wt.USHORT),
                ("wType", wt.USHORT)]


def _adapter_ipv4():
    """本机所有网卡 IPv4（含回环与失效地址，由调用方筛）。失败返回 []。"""
    try:
        size = wt.ULONG(0)
        if ctypes.windll.iphlpapi.GetIpAddrTable(None, ctypes.byref(size), 0) != 122:
            return []                            # 122 = ERROR_INSUFFICIENT_BUFFER
        buf = ctypes.create_string_buffer(size.value)
        if ctypes.windll.iphlpapi.GetIpAddrTable(buf, ctypes.byref(size), 0) != 0:
            return []
        count = wt.ULONG.from_buffer_copy(buf, 0).value
        row_size = ctypes.sizeof(_MibIpAddrRow)
        out = []
        for i in range(count):
            row = _MibIpAddrRow.from_buffer_copy(buf, 4 + i * row_size)
            raw = row.dwAddr
            out.append("%d.%d.%d.%d" % (raw & 255, (raw >> 8) & 255,
                                        (raw >> 16) & 255, (raw >> 24) & 255))
    except Exception:
        return []
    return out


def list_ipv4():
    """本机可用的 IPv4 列表：默认路由那张排最前，其余按网卡表补。

    虚拟网卡（异地联机的 10.126.126.x）也会在里面——用户手动选它，就能
    把共享绑到虚拟局域网上。
    """
    found = []
    primary = primary_lan_ip()
    if primary:
        found.append(primary)
    for ip in _adapter_ipv4():
        if _is_usable(ip) and ip not in found:
            found.append(ip)
    if not found:                                # 网卡表也读不到时的兜底
        try:
            _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        except OSError:
            addrs = []
        found = [ip for ip in addrs if _is_usable(ip)]
    return found


def port_free(ip, port):
    """ip:port 现在能不能**独占**绑定（界面提示与 pick_port 都用它）。

    刻意**不设** `SO_REUSEADDR`：Windows 上"这个端口能不能绑"只检查**新
    socket** 的选项 —— 新 socket 一旦自己设了，哪怕端口已经被别的程序
    listen 着，`bind` 也照样成功。换句话说，在这里加 `SO_REUSEADDR` 会让
    本函数永远返回 True，"端口被占"这个提示永远不可能出现（v0.11beta
    实测确认过）。不设它，系统才会如实回 WSAEADDRINUSE。

    探针连 `listen` 也一起做：`bind` 成功≠能用，Windows 上"两个 socket 都
    设了 SO_REUSEADDR 绑同一端口"就是 bind 成功、listen 才失败（10048），
    而我们的 `ShareServer.start()` 正好设了 SO_REUSEADDR。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((ip or "0.0.0.0", port))
        s.listen(1)
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(preferred=DEFAULT_PORT, tries=20, ip=None):
    """挑一个能绑上的端口：先试 preferred，被占就往后顺延。

    `ip` 传**实际要绑的地址**（调用方知道）。传 None 时按 `0.0.0.0` 探测，
    结果会偏宽松：一个只被 `127.0.0.1` 占走的端口会被算成空闲（Windows
    允许通配地址与具体地址共存），所以 `ShareServer.start()` 仍可能回一个
    "端口已被占用"。界面层传真实 IP 就没这个问题。
    """
    probe = ip or "0.0.0.0"
    for i in range(tries):
        port = preferred + i
        if port > 65535:
            break
        if port_free(probe, port):
            return port
    # 全被占了就交给系统随机挑一个（宁可端口随机，也不要打不开）
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((probe, 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 本机窗口列表（给"只共享某个窗口"用）
# ---------------------------------------------------------------------------
class WindowInfo(object):
    """一个可共享的顶层窗口。hwnd 是给 Qt 的 `QScreen.grabWindow()` 用的。"""

    __slots__ = ("hwnd", "title")

    def __init__(self, hwnd, title):
        self.hwnd = hwnd
        self.title = title

    def __repr__(self):
        return "WindowInfo(hwnd=%d, title=%r)" % (self.hwnd, self.title)

    def __eq__(self, other):
        return isinstance(other, WindowInfo) and (self.hwnd, self.title) == \
            (other.hwnd, other.title)


def list_windows(skip_pid=None):
    """本机当前所有"看起来能共享"的顶层窗口。

    过滤掉：不可见的、没标题的、工具窗口（输入法候选框、桌面本身这类
    `WS_EX_TOOLWINDOW`）、被 UWP "幽灵化"的（`DWMWA_CLOAKED`，它们
    `IsWindowVisible` 是 True 但屏幕上根本没有，比如"Windows 输入体验"）、
    **最小化的**，以及自己这个进程的窗口——把自己列进去只会让人困惑。

    最小化的那个必须滤掉：我们抓的是**屏幕上的那块区域**（不用
    `PrintWindow`，那个在窗口未响应时会把抓屏线程一起拖死），而最小化的
    窗口根本没被绘制，抓出来就是一片黑。与其让用户选一个只能看到黑的窗口，
    不如一开始就不列出来。
    """
    if skip_pid is None:
        skip_pid = os.getpid()
    user32 = ctypes.windll.user32
    dwm = ctypes.windll.dwmapi
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    DWMWA_CLOAKED = 14

    # 显式声明 argtypes：HWND 在 64 位下是指针宽，不声明的话 ctypes 按
    # c_int 传，高位会被截掉，查到的就是另一个窗口（或直接失败）。
    # 本项目所有 ctypes 调 Win32 的地方都按这条来。
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsIconic.restype = wt.BOOL
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM),
                                   wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p,
                                          wt.DWORD]
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long

    out = []

    def _cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.IsIconic(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if skip_pid and pid.value == skip_pid:
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        cloaked = wt.DWORD(0)
        try:
            if dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED,
                                         ctypes.byref(cloaked),
                                         ctypes.sizeof(cloaked)) == 0 \
                    and cloaked.value:
                return True
        except Exception:
            pass
        out.append(WindowInfo(int(hwnd), title))
        return True

    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(_cb)
    try:
        user32.EnumWindows(proc, 0)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 当前房间（异地联机页进房/退房时登记，屏幕共享据此免输观看码）
# ---------------------------------------------------------------------------
_ROOM = {"code": "", "password": "", "ip": ""}


def set_active_room(code="", password="", ip=""):
    """登记"我此刻在哪个房间"。异地联机页进房成功后调用，退房时清空。"""
    _ROOM["code"] = code or ""
    _ROOM["password"] = password or ""
    _ROOM["ip"] = ip or ""


def active_room():
    return dict(_ROOM)


def room_view_token():
    """房间内用的观看码：由房间码+密码派生，房间成员算出来必然一致。

    不在房间里就返回空串——这时共享只能走局域网 + 随机观看码那条路。
    """
    if not _ROOM["code"]:
        return ""
    return make_token(_ROOM["code"], _ROOM["password"])


#: 异地联机页进房时登记的"把屏幕共享端口广播出去"的回调。
#: 屏幕共享页**不直接依赖**联机页：它只调 `publish_screenshare()`，
#: 由联机页决定怎么广播（现在是写进昵称信标，队友的成员行据此长出
#: 「看屏幕」按钮）。没人登记时（没在房间里）就安静地什么都不做。
_publish_port = None


def set_port_publisher(fn):
    global _publish_port
    _publish_port = fn


def publish_screenshare(port):
    fn = _publish_port
    if fn is None:
        return
    try:
        fn(int(port or 0))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 抓屏（GDI）
# ---------------------------------------------------------------------------
class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", _Rect),
                ("rcWork", _Rect), ("dwFlags", wt.DWORD)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wt.DWORD * 3)]


MONITORINFOF_PRIMARY = 0x1
DWMWA_EXTENDED_FRAME_BOUNDS = 9
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
HALFTONE = 0x03


def list_screens():
    """本机所有显示器：[{index, x, y, w, h, primary, label}]。

    用 EnumDisplayMonitors 而不是 Qt 的 QGuiApplication.screens()：抓屏这段
    刻意保持不依赖 Qt，才能整个搬到工作线程里跑（Qt 的抓屏只能在主线程）。
    """
    user32 = ctypes.windll.user32
    user32.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.c_void_p,
                                           ctypes.c_void_p, wt.LPARAM]
    user32.EnumDisplayMonitors.restype = wt.BOOL
    user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(_MonitorInfo)]
    user32.GetMonitorInfoW.restype = wt.BOOL

    out = []

    def _cb(hmon, _hdc, _lprc, _data):
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        rc = info.rcMonitor
        out.append({"index": len(out),
                    "x": rc.left, "y": rc.top,
                    "w": rc.right - rc.left, "h": rc.bottom - rc.top,
                    "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY)})
        return True

    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC,
                              ctypes.POINTER(_Rect), wt.LPARAM)(_cb)
    try:
        user32.EnumDisplayMonitors(None, None, proc, 0)
    except Exception:
        pass
    for i, s in enumerate(out):
        s["label"] = "显示器 %d%s" % (i + 1, "（主）" if s["primary"] else "")
    if not out:                                   # 极端兜底：整块虚拟桌面
        w = ctypes.windll.user32.GetSystemMetrics(0)
        h = ctypes.windll.user32.GetSystemMetrics(1)
        out = [{"index": 0, "x": 0, "y": 0, "w": w, "h": h,
                "primary": True, "label": "显示器 1（主）"}]
    return out


def _same_space(a, b):
    """两个矩形是不是在同一个坐标系里（宽高量级相当）。

    DWM 的 `EXTENDED_FRAME_BOUNDS` 在同一坐标系里只会比 `GetWindowRect`
    小一圈（排掉那圈看不见的调整边框，十几像素）；差出一个缩放倍率就说明
    两边量的不是同一件东西。
    """
    for i in (2, 3):
        big, small = max(a[i], b[i]), min(a[i], b[i])
        if big <= 0 or small <= 0:
            return False
        if big - small > max(64, big * 0.2):
            return False
    return True


def window_rect(hwnd):
    """窗口在**抓屏坐标系**里的矩形 (x, y, w, h)。拿不到返回 None。

    坐标系必须和 `GdiCapture` 用的屏幕 DC 一致，否则"只共享某个窗口"抓到的
    是旁边那块区域。而屏幕 DC 的坐标系取决于**进程是不是 DPI 感知的**：

      * 感知的进程（打包后的 exe 默认 PerMonitorV2）：GDI 与 DWM 都是物理像素；
      * 不感知的进程（源码态的 python.exe）：GDI 一路是**逻辑**像素（150%
        缩放下 2560 宽的屏幕报告成 1707），而 DWM **无论进程怎样**都回物理像素。

    所以这里把两个来源对一下：量级一致就用 DWM 的更精确边界（没有 DWM 那圈
    看不见的调整边框，抓出来不会多黑边）；量级差一个缩放倍率就退回
    `GetWindowRect`，宁可多几像素黑边，也不能抓错地方。
    """
    rect = _Rect()
    try:
        u32 = ctypes.windll.user32
        u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(_Rect)]
        u32.GetWindowRect.restype = wt.BOOL
        gr = None
        if u32.GetWindowRect(hwnd, ctypes.byref(rect)):
            gr = (rect.left, rect.top,
                  rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        gr = None
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD,
                                              ctypes.POINTER(_Rect), wt.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        fb = _Rect()
        if dwm.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                     ctypes.byref(fb),
                                     ctypes.sizeof(fb)) == 0:
            dr = (fb.left, fb.top, fb.right - fb.left, fb.bottom - fb.top)
            if gr is None:
                return dr
            if _same_space(dr, gr):
                return dr
    except Exception:
        pass
    return gr


class GdiCapture(object):
    """GDI 抓屏：一步 `StretchBlt` 把屏幕上一块区域缩放成目标尺寸的 BGRA。

    为什么不用 Qt 的 `QScreen.grabWindow`：
      * 它整屏 BitBlt 出来一张 2560x1600 的 QPixmap（16MB 回读，本机实测
        46.7ms），再交给 Qt 缩放，单帧 61~68ms，天花板只有 15fps；
      * 而且它必须跑到主线程上，共享期间界面会明显卡。
    `StretchBlt` 在 GDI 内部一次完成"抓 + 缩"，HALFTONE 下 1280x800 实测
    15.9ms，且纯 ctypes、可以整个搬到工作线程，主线程一帧都不占。

    输出是**自上而下**的 32 位 BGRA（`biHeight` 取负即 top-down），
    与 `QImage.Format_RGB32` 的内存布局一致，接手时不用翻转。

    一个刻意取舍：抓的是**屏幕上的那块区域**，不是窗口自己的绘制结果。
    所以窗口被别的窗口挡住时，看到的就是被挡住的样子——这跟用户自己
    截屏看到的一致，不会出现"我明明看不到它，对方却看得到"的诡异。代价是
    不给用 PrintWindow：那个在窗口"未响应"时会把抓屏线程一起拖死。
    """

    def __init__(self):
        self._mem = None                              # 内存 DC（长期复用）
        self._bmp = None                              # 当前 DIB 位图
        self._old = None                              # 内存 DC 原有的位图
        self._ptr = ctypes.c_void_p()                 # DIB 像素首地址
        self._size = (0, 0)

    # ------------------------------------------------------------------ 内部
    def _setup(self):
        if self._mem is not None:
            return
        u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
        u32.GetDC.argtypes = [wt.HWND]
        u32.GetDC.restype = wt.HDC
        u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        u32.ReleaseDC.restype = ctypes.c_int
        g32.CreateCompatibleDC.argtypes = [wt.HDC]
        g32.CreateCompatibleDC.restype = wt.HDC
        g32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT,
                                         ctypes.c_void_p, wt.HANDLE, wt.DWORD]
        g32.CreateDIBSection.restype = wt.HBITMAP
        g32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        g32.SelectObject.restype = wt.HGDIOBJ
        g32.StretchBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int,
                                   wt.HDC, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.DWORD]
        g32.StretchBlt.restype = wt.BOOL
        g32.SetStretchBltMode.argtypes = [wt.HDC, ctypes.c_int]
        g32.SetStretchBltMode.restype = ctypes.c_int
        g32.SetBrushOrgEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_void_p]
        g32.SetBrushOrgEx.restype = wt.BOOL
        g32.DeleteDC.argtypes = [wt.HDC]
        g32.DeleteObject.argtypes = [wt.HGDIOBJ]
        self._mem = g32.CreateCompatibleDC(None)
        # HALFTONE 是唯一"缩小时不丢字"的模式（COLORONCOLOR 实测更慢、锯齿
        # 也更明显）。设完之后必须重置一次画刷原点，否则 GDI 会把上一次的
        # 偏移带进来，画面出现规律性错位。
        g32.SetStretchBltMode(self._mem, HALFTONE)
        g32.SetBrushOrgEx(self._mem, 0, 0, None)

    def _ensure_bitmap(self, w, h):
        if self._size == (w, h):
            return
        g32 = ctypes.windll.gdi32
        if self._bmp:
            g32.SelectObject(self._mem, self._old)
            g32.DeleteObject(self._bmp)
            self._bmp = None
        bmi = _BitmapInfo()
        head = bmi.bmiHeader
        head.biSize = ctypes.sizeof(_BitmapInfoHeader)
        head.biWidth = w
        head.biHeight = -h                            # 负高度 = top-down
        head.biPlanes = 1
        head.biBitCount = 32
        head.biCompression = 0                        # BI_RGB
        head.biSizeImage = w * h * 4
        self._bmp = g32.CreateDIBSection(self._mem, ctypes.byref(bmi), 0,
                                         ctypes.byref(self._ptr), None, 0)
        self._old = g32.SelectObject(self._mem, self._bmp)
        self._size = (w, h)

    # ------------------------------------------------------------------ 对外
    def fit(self, src_w, src_h, max_edge):
        """按最长边限制算出输出尺寸（保持宽高比，且不放大）。"""
        if src_w <= 0 or src_h <= 0:
            return 0, 0
        if max_edge <= 0 or max(src_w, src_h) <= max_edge:
            return src_w, src_h
        if src_w >= src_h:
            return max_edge, max(2, int(round(src_h * max_edge / float(src_w))))
        return max(2, int(round(src_w * max_edge / float(src_h)))), max_edge

    def grab(self, rect, out_w, out_h):
        """抓 rect=(x,y,w,h) 并缩放到 out_w x out_h。

        返回自上而下的 BGRA 字节（out_w*out_h*4），失败返回 None。
        """
        x, y, w, h = rect
        if w <= 0 or h <= 0 or out_w <= 0 or out_h <= 0:
            return None
        self._setup()
        self._ensure_bitmap(out_w, out_h)
        u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
        screen = u32.GetDC(None)
        if not screen:
            return None
        try:
            ok = g32.StretchBlt(self._mem, 0, 0, out_w, out_h,
                                screen, x, y, w, h,
                                SRCCOPY | CAPTUREBLT)
        finally:
            u32.ReleaseDC(None, screen)
        if not ok:
            return None
        return ctypes.string_at(self._ptr, out_w * out_h * 4)

    def close(self):
        g32 = ctypes.windll.gdi32
        if self._bmp:
            try:
                g32.SelectObject(self._mem, self._old)
                g32.DeleteObject(self._bmp)
            except Exception:
                pass
            self._bmp = None
        if self._mem:
            try:
                g32.DeleteDC(self._mem)
            except Exception:
                pass
            self._mem = None
        self._size = (0, 0)
        self._ptr = ctypes.c_void_p()


# ---------------------------------------------------------------------------
# 发送端
# ---------------------------------------------------------------------------
class _Viewer:
    """一个观看者连接。

    关键设计：**每人只有一格"最新帧"的邮箱**，新帧直接覆盖旧的。
    慢的观看者（比如异地经中继、带宽不够）不会把发送端的内存撑爆，也不会
    把整个流拖慢——他只是掉帧，画面卡一点，但不会连累别人，更不会让
    发送端的 socket 缓冲区越积越多（那是内存泄漏的经典姿势）。
    """

    def __init__(self, conn, addr, name, on_died=None):
        self.conn = conn
        self.addr = addr
        self.name = name
        self.frames = 0
        self.bytes = 0
        self.alive = True
        self.on_died = on_died
        self._lock = threading.Lock()
        self._slot = None
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def label(self):
        return "%s:%d" % (self.addr[0], self.addr[1])

    def offer(self, packet, is_frame):
        """投递一帧。非阻塞：满了就覆盖，绝不等这个观看者。

        唯一的例外是**心跳不许顶掉还没发出去的画面帧**：画面静止时发送端
        只在慢速发心跳，而一帧 100KB 上下的 JPEG 要走完 `sendall` 需要一点
        时间；如果这段时间里来的心跳把那一格顶掉，观看端就会只收到心跳、
        **一帧画面都看不到**。实测就是这么暴露的：刚连上时前 1 秒黑屏，
        随后突然出画 —— 那一帧正是每秒一次的兜底刷新。
        心跳本身不含信息，丢掉它没有任何损失。
        """
        with self._lock:
            if (not is_frame and self._slot is not None and self._slot[1]):
                return                        # 有画面帧还没发出去，心跳让位
            self._slot = (packet, is_frame)
        self._event.set()

    def stop(self):
        self._stop.set()
        self._event.set()
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def join(self, timeout=1.0):
        self._thread.join(timeout)

    def _loop(self):
        while not self._stop.is_set():
            if not self._event.wait(0.5):
                # 没帧可发的时候顺手看一眼对端是不是已经走了。
                # 光靠 sendall 报错是不够的：画面静止时我们只发 17 字节的
                # 心跳，甚至可能连着几秒不发东西，于是界面上会一直挂着一个
                # 早就关掉的"观看者"，人数越堆越多。
                try:
                    ready, _, _ = select.select([self.conn], [], [], 0)
                    if ready and not self.conn.recv(1, socket.MSG_PEEK):
                        break                     # 读到 EOF = 对端关了
                except OSError:
                    break
                continue
            self._event.clear()
            with self._lock:
                slot = self._slot
                self._slot = None
            if slot is None:
                continue
            packet, is_frame = slot
            try:
                self.conn.sendall(packet)
            except OSError:
                break
            if is_frame:
                self.frames += 1
            self.bytes += len(packet)
        self.alive = False
        # 自己退出后必须主动销号。只靠 push() 里顺带清理是不够的：
        # 没人发帧（或画面一直没变）的时候，名单里会一直挂着一个已经关掉的
        # 连接，界面上的"正在观看"人数就再也降不下来。
        if self.on_died is not None:
            try:
                self.on_died(self)
            except Exception:
                pass


class ShareServer(object):
    """把 JPEG 帧广播出去。不抓屏——帧由调用方（界面层）产生后交进来。"""

    def __init__(self, token, on_event=None):
        self.token = token or ""
        self.on_event = on_event or (lambda kind, payload: None)
        self.port = 0
        self.bind_ip = ""
        self.name = ""
        self.running = False
        self.error = ""
        # 统计（单调递增，界面按秒采样算速率）。
        # frames_sent 只数**真正带画面的帧** —— 心跳不算，否则画面静止时
        # 界面会显示一个看着正常、其实什么都没传的帧率。
        self.frames_sent = 0
        self.bytes_sent = 0
        self._viewers = []
        self._lock = threading.Lock()
        self._sock = None
        self._accept_thread = None
        self._stop = threading.Event()
        self._seq = 0
        self._last_frame = None

    # ------------------------------------------------------------------ 生命周期
    def start(self, bind_ip, port=DEFAULT_PORT, name=""):
        """在 bind_ip:port 上起服务。返回 (ok, 端口 或 错误文案)。"""
        if self.running:
            return True, self.port
        ip = bind_ip or "0.0.0.0"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((ip, port))
            sock.listen(MAX_VIEWERS)
        except OSError as exc:
            sock.close()
            # 端口被占是最常见的一种，直接把结论说出来，别让用户去猜
            if getattr(exc, "winerror", None) == 10048:
                msg = "端口 %d 已被占用，换个端口或先停掉占用的程序" % port
            else:
                msg = "无法在 %s:%d 上监听（%s）" % (ip, port, exc)
            self.error = msg
            return False, msg
        self._sock = sock
        self.bind_ip = ip
        self.port = sock.getsockname()[1]
        self.name = name or socket.gethostname()
        self._seq = 0
        self._last_frame = None
        self.frames_sent = 0
        self.bytes_sent = 0
        self._stop.clear()
        self.running = True
        self.error = ""
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return True, self.port

    def stop(self):
        if not self.running:
            return
        self.running = False
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()                      # 关掉 listen socket 让 accept 立刻退出
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(1.5)
            self._accept_thread = None
        with self._lock:
            viewers = list(self._viewers)
            self._viewers = []
        for v in viewers:
            v.stop()
        for v in viewers:
            v.join(1.0)
        self._last_frame = None

    @property
    def viewers(self):
        with self._lock:
            return len(self._viewers)

    def viewer_list(self):
        with self._lock:
            return [v.label for v in self._viewers]

    def viewer_stats(self):
        """[{label, frames, bytes}]，界面拿它显示"谁在看"。"""
        with self._lock:
            return [{"label": v.label, "frames": v.frames, "bytes": v.bytes}
                    for v in self._viewers]

    # ------------------------------------------------------------------ 收发
    def push(self, jpeg, w, h):
        """投递一帧 JPEG。没有观看者时立刻返回（不白干活）。

        **画面没变就不重发**：JPEG 编码是确定性的，屏幕静止时编出来的字节
        与上一帧逐字节相同。这时只发一个 17 字节的心跳头，观看端据此知道
        "连接还活着，画面就是没变"——异地走中继时这一条能省掉绝大部分流量
        （看文档、看桌面时几乎为 0）。
        """
        with self._lock:
            viewers = list(self._viewers)
        if not viewers:
            return False
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        if jpeg is not None and jpeg == self._last_frame:
            packet = HEADER.pack(MAGIC, TYPE_IDLE, self._seq, w, h, 0)
            is_frame = False
        else:
            if jpeg is None:
                packet = HEADER.pack(MAGIC, TYPE_IDLE, self._seq, w, h, 0)
                is_frame = False
            else:
                self._last_frame = jpeg
                packet = HEADER.pack(MAGIC, TYPE_FRAME, self._seq, w, h,
                                     len(jpeg)) + jpeg
                is_frame = True
        if is_frame:
            self.frames_sent += 1
        self.bytes_sent += len(packet)
        dead = []
        for v in viewers:
            if not v.alive:
                dead.append(v)
                continue
            v.offer(packet, is_frame)
        if dead:
            self._drop(dead)
        return is_frame

    # ------------------------------------------------------------------ 内部
    def _drop(self, viewers):
        for v in viewers:
            v.stop()
        self._forget(viewers)

    def _forget(self, viewers):
        """把已经死掉的连接从名单里摘掉，只有真摘掉了才报事件。"""
        gone = []
        with self._lock:
            for v in viewers:
                if v in self._viewers:
                    self._viewers.remove(v)
                    gone.append(v)
        for v in gone:
            self.on_event("viewer_leave", {"label": v.label,
                                           "total": self.viewers})

    def _on_viewer_died(self, viewer):
        """观看线程自己退出时的回调（在观看线程里执行）。"""
        self._forget([viewer])

    def _accept_loop(self):
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                conn, addr = sock.accept()
            except OSError:
                break
            try:
                self._handshake(conn, addr)
            except OSError:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handshake(self, conn, addr):
        conn.settimeout(6.0)
        line = _recv_line(conn, MAX_HELLO)
        if line is None:
            conn.close()
            return
        try:
            hello = line.decode("utf-8", "replace").strip()
        except Exception:
            hello = ""
        parts = hello.split(" ", 1)
        if len(parts) != 2 or parts[0] != PROTOCOL:
            _safe_send(conn, b"ERR protocol\n")
            conn.close()
            return
        got = parts[1].strip()
        if not self.token or not hmac.compare_digest(got, self.token):
            _safe_send(conn, b"ERR token\n")
            conn.close()
            self.on_event("rejected", {"label": "%s:%d" % (addr[0], addr[1])})
            return
        with self._lock:
            full = len(self._viewers) >= MAX_VIEWERS
        if full:
            _safe_send(conn, b"ERR full\n")
            conn.close()
            return
        _safe_send(conn, ("OK %s\n" % self.name).encode("utf-8"))
        conn.settimeout(None)
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        viewer = _Viewer(conn, addr, self.name, on_died=self._on_viewer_died)
        with self._lock:
            self._viewers.append(viewer)
        self.on_event("viewer_join", {"label": viewer.label,
                                      "total": self.viewers})


class FrameClient(object):
    """观看端：连到别人的共享，把收到的帧回调出去。

    `start()` 是**同步**的：握手（含观看码校验）在这一步就做完，所以
    "地址不通""观看码不对"能立刻作为返回值交给界面，而不是过几秒才由一个
    后台线程含糊地报出来。
    """

    def __init__(self, host, port, token, on_frame, on_event=None, timeout=6.0):
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.on_frame = on_frame
        self.on_event = on_event or (lambda kind, payload: None)
        self.timeout = timeout
        self.peer_name = ""
        #: 收到的**画面帧**数（心跳不算）—— 界面上那个"收到 X 帧/秒"就是它，
        #: 口径和发送端的 `frames_sent` 对齐：两边都表示"画面刷新了多少次"。
        self.frames = 0
        #: 收到的**包**总数（含心跳）。心跳也算"链路还活着"，所以判断连接
        #: 是否正常要用这个，不能用 frames —— 画面静止时 frames 会停在 0。
        self.packets = 0
        self.bytes = 0
        self.running = False
        self.error = ""
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        """建立连接并完成握手。返回 (ok, 对端名字 或 错误文案)。"""
        if self.running:
            return True, self.peer_name
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except socket.timeout:
            msg = "连接 %s:%d 超时（对方没开共享，或不在同一网络里）" % (self.host, self.port)
            self.error = msg
            return False, msg
        except OSError as exc:
            msg = "连不上 %s:%d（%s）" % (self.host, self.port, exc)
            self.error = msg
            return False, msg
        sock.settimeout(self.timeout)
        try:
            sock.sendall(("%s %s\n" % (PROTOCOL, self.token)).encode("utf-8"))
            line = _recv_line(sock, MAX_HELLO)
        except OSError as exc:
            sock.close()
            msg = "握手失败（%s）" % exc
            self.error = msg
            return False, msg
        if line is None:
            sock.close()
            msg = "对方没有应答"
            self.error = msg
            return False, msg
        reply = line.decode("utf-8", "replace").strip()
        if reply.startswith("ERR"):
            sock.close()
            reason = reply[3:].strip()
            if reason == "token":
                msg = "观看码不对"
            elif reason == "full":
                msg = "对方观看人数已满（上限 %d 人）" % MAX_VIEWERS
            elif reason == "protocol":
                msg = "对方版本不匹配，双方都要升级到同一版 Yuhub"
            else:
                msg = "对方拒绝了连接（%s）" % (reason or "未知原因")
            self.error = msg
            return False, msg
        if not reply.startswith("OK"):
            sock.close()
            msg = "对方回了看不懂的应答：%s" % reply[:60]
            self.error = msg
            return False, msg
        self.peer_name = reply[2:].strip()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.settimeout(15.0)
        self._sock = sock
        self.running = True
        self.error = ""
        self.frames = 0
        self.packets = 0
        self.bytes = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True, self.peer_name

    def stop(self):
        if not self.running and self._sock is None:
            return
        self.running = False
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(1.5)
            self._thread = None

    def _loop(self):
        stale = 0
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                head = _recv_exact(sock, HEADER.size)
            except socket.timeout:
                stale += 1
                # 画面静止时发送端也会每帧发一个心跳，所以长时间收不到东西
                # 就是真的断了。给足 3 次机会再判死，避免误杀网络抖动。
                if stale >= 3:
                    self.error = "连接已断开（长时间没有收到数据）"
                    break
                continue
            except OSError as exc:
                self.error = "连接中断（%s）" % exc
                break
            if head is None:
                self.error = "对方结束了共享"
                break
            stale = 0
            magic, typ, seq, w, h, length = HEADER.unpack(head)
            if magic != MAGIC:
                self.error = "数据流损坏（版本不匹配？）"
                break
            payload = b""
            if length:
                try:
                    payload = _recv_exact(sock, length)
                except OSError as exc:
                    self.error = "读取画面失败（%s）" % exc
                    break
                if payload is None:
                    self.error = "对方结束了共享"
                    break
            if typ == TYPE_FRAME:
                self.frames += 1
            self.packets += 1
            self.bytes += len(head) + len(payload)
            try:
                self.on_frame(typ, seq, w, h, payload)
            except Exception:
                pass                              # 界面层的锅不该弄死接收线程
        self.running = False
        self.on_event("closed", {"error": self.error})


# ---------------------------------------------------------------------------
# socket 读写助手
# ---------------------------------------------------------------------------
def _safe_send(conn, data):
    try:
        conn.sendall(data)
    except OSError:
        pass


def _recv_exact(sock, n):
    """读满 n 字节。对端正常关闭返回 None；超时抛 socket.timeout。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _recv_line(sock, limit):
    """读一行（到 \\n 为止，最多 limit 字节）。对端提前关闭返回 None。"""
    buf = bytearray()
    while len(buf) < limit:
        ch = sock.recv(1)
        if not ch:
            return None
        if ch == b"\n":
            break
        buf += ch
    return bytes(buf)


# ---------------------------------------------------------------------------
# 统计小工具（界面与自检共用，放这里免得两处各写一遍）
# ---------------------------------------------------------------------------
class RateMeter(object):
    """按秒采样单调计数器，算出帧率与字节率。

    发送端和接收端都可以用：喂进累计值，读 fps / KB/s。
    """

    def __init__(self, window=3.0):
        self.window = window
        self._samples = []                        # [(t, frames, bytes)]
        self.fps = 0.0
        self.kbps = 0.0

    def feed(self, frames, nbytes, now=None):
        now = time.monotonic() if now is None else now
        self._samples.append((now, frames, nbytes))
        while len(self._samples) > 1 and now - self._samples[0][0] > self.window:
            self._samples.pop(0)
        if len(self._samples) < 2:
            self.fps = 0.0
            self.kbps = 0.0
            return
        t0, f0, b0 = self._samples[0]
        t1, f1, b1 = self._samples[-1]
        span = t1 - t0
        if span <= 0:
            return
        self.fps = (f1 - f0) / span
        self.kbps = (b1 - b0) / 1024.0 / span

    def reset(self):
        self._samples = []
        self.fps = 0.0
        self.kbps = 0.0
