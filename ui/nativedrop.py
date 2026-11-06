# -*- coding: utf-8 -*-
"""Windows 原生 WM_DROPFILES 拖放通道（提权运行时 Qt 拖放的唯一活路）。

## 为什么必须有这个模块

Yuhub.exe 的清单是 `requireAdministrator`（Yuhub.spec 的 `uac_admin=True`），
跑起来完整性级别（IL）是「高」；资源管理器是「中」。Windows 的 UIPI
（User Interface Privilege Isolation）**禁止低 IL 进程往高 IL 进程发消息**，
而 Qt 的拖放走的是 OLE 的 `IDropTarget` —— 请求在 COM 层就被挡掉了。

症状与用户报告一字不差：从桌面把文件拖到「临时云盘」的投放区，鼠标一直是
"可拖放"的样子，松手却什么都没发生；点一下选文件则完全正常（文件对话框是
本进程自己弹的，不走跨进程消息）。微软博客对这个现象的原话是：提权运行的
记事本，从 Explorer 拖文件进去"什么都不会发生，但指针还装作可以放"。

`ChangeWindowMessageFilter(Ex)` 只放行**窗口消息**，对 OLE 无效 —— 单加它
一点用都没有。能走通的是原生 `WM_DROPFILES`：

  1. `DragAcceptFiles(hwnd, TRUE)` —— 给窗口挂上 `WS_EX_ACCEPTFILES`；
  2. `RevokeDragDrop(hwnd)` —— 注销 Qt 注册的 OLE 拖放目标
     （不注销的话 Explorer 优先走 OLE，原生消息永远收不到）；
  3. `ChangeWindowMessageFilterEx` 放行 `WM_DROPFILES`、`WM_COPYDATA`、
     `WM_COPYGLOBALDATA` 三条消息 —— UIPI 只对窗口消息放行，所以这步有效；
  4. 用 `QAbstractNativeEventFilter` 收 `WM_DROPFILES`，`DragQueryFileW`
     取路径、`DragQueryPoint` 取落点。

⚠️ **顺序不能反**：先 `DragAcceptFiles` 再 `RevokeDragDrop`。反过来的话，
万一 `DragAcceptFiles` 失败，我们就会把 Qt 的 OLE 通道掐掉、而原生通道又没
建起来 —— 两条路同时死，拖拽彻底不可用。这正是本项目反复栽跟头的那类
"静默失效"：不报错、界面毫无变化、用户只看到"拖了没反应"。

## 影响面

全项目只有「临时云盘」一个投放区（`setAcceptDrops` / `dropEvent` 全仓库只
命中 `ui/pages/lan_page.py::_DropZone`），所以注销主窗口的 OLE 拖放目标不会
误伤别的控件。而原生通道在**非提权**时同样可用，所以修完之后两种启动方式
走的是同一条代码路径 —— 不存在"打包机上好、开发机上坏"的分叉。

## 自检

见根目录 `nativedrop_selftest.py`（`Yuhub.exe --nativedrop-selftest <json>`）。
它会在真窗口上走完「造 HDROP → PostMessage → 过滤器 → 路由到投放区」全链路，
而不是只检查几个常量。
"""
import ctypes
import struct
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QPoint, QRect
from PySide6.QtWidgets import QWidget

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
WM_DROPFILES = 0x0233
WM_COPYDATA = 0x004A
WM_COPYGLOBALDATA = 0x0049

MSGFLT_RESET = 0
MSGFLT_ALLOW = 1
MSGFLT_DISALLOW = 2
MSGFLT_ADD = 1            # ChangeWindowMessageFilter（Win7 之前的兜底）

GWL_EXSTYLE = -20
WS_EX_ACCEPTFILES = 0x00000010

# 投放区在 Qt 侧打的标记：原生消息不知道"临时云盘"是什么，只能按落点反查控件。
# 用动态属性而不是 isinstance 判断 —— 免得 nativedrop 反向依赖 lan_page。
SINK_PROPERTY = "yuhubNativeDropSink"

_PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)
if _PTR_SIZE == 8:
    # HWND, UINT(+4 对齐填充), WPARAM, LPARAM, DWORD time, POINT, DWORD
    _MSG_FMT = "<Q I I Q Q I i i I"
    _MSG_SIZE = 48
else:
    _MSG_FMT = "<I I I I I i i I"
    _MSG_SIZE = 32


# ---------------------------------------------------------------------------
# Win32 声明
# ⚠️ restype / argtypes 必须显式写全：64 位句柄不声明会被截成 32 位，
#    OpenProcess 打不开、ShellExecuteW 返回值误判 —— 本项目吃过这个亏。
# ---------------------------------------------------------------------------
def _load():
    """惰性加载 DLL。导入本模块时还没到 Windows 也能 import（自检会判空）。"""
    try:
        ole = ctypes.WinDLL("ole32", use_last_error=True)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        user = ctypes.WinDLL("user32", use_last_error=True)
    except Exception:
        return None

    ole.RevokeDragDrop.argtypes = [wintypes.HWND]
    ole.RevokeDragDrop.restype = ctypes.HRESULT

    shell.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    shell.DragAcceptFiles.restype = None
    shell.DragQueryFileW.argtypes = [
        wintypes.HANDLE, ctypes.c_uint, wintypes.LPWSTR, ctypes.c_uint]
    shell.DragQueryFileW.restype = ctypes.c_uint
    shell.DragQueryPoint.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.POINT)]
    shell.DragQueryPoint.restype = wintypes.BOOL
    shell.DragFinish.argtypes = [wintypes.HANDLE]
    shell.DragFinish.restype = None

    try:
        user.ChangeWindowMessageFilterEx.argtypes = [
            wintypes.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        user.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
    except AttributeError:
        pass
    try:
        user.ChangeWindowMessageFilter.argtypes = [ctypes.c_uint, ctypes.c_uint]
        user.ChangeWindowMessageFilter.restype = wintypes.BOOL
    except AttributeError:
        pass

    try:
        glp = user.GetWindowLongPtrW
    except AttributeError:
        glp = user.GetWindowLongW
    glp.argtypes = [wintypes.HWND, ctypes.c_int]
    glp.restype = ctypes.c_longlong if _PTR_SIZE == 8 else ctypes.c_long
    return {"ole": ole, "shell": shell, "user": user, "get_style": glp}


_API = None


def _api():
    global _API
    if _API is None:
        _API = _load() or False
    return _API or None


def available():
    """本机能不能走原生通道（非 Windows / 缺 API 时返回 False）。"""
    return _api() is not None


# ---------------------------------------------------------------------------
# 窗口侧：切换拖放通道
# ---------------------------------------------------------------------------
def style_accepts_files(hwnd):
    """只读体检：窗口是否真的挂上了 `WS_EX_ACCEPTFILES`。

    自检要拿它当**独立于 enable() 返回值的第二条证据** —— 只信 enable() 的
    True 就是"自洽比较"，API 静默失败时会一路绿灯。
    """
    a = _api()
    if a is None or not hwnd:
        return False
    try:
        style = a["get_style"](int(hwnd), GWL_EXSTYLE)
    except Exception:
        return False
    return bool(int(style) & WS_EX_ACCEPTFILES)


def _allow_message(api, hwnd, msg):
    """放行一条 UIPI 消息。返回 (是否成功, 用的哪个 API)。"""
    try:
        if api["user"].ChangeWindowMessageFilterEx(
                int(hwnd), int(msg), MSGFLT_ALLOW, None):
            return True, "Ex"
    except Exception:
        pass
    try:
        # Win7 之前没有 Ex 版本：退回全局过滤（粒度粗，但总比不放行强）
        if api["user"].ChangeWindowMessageFilter(msg, MSGFLT_ADD):
            return True, "legacy"
    except Exception:
        pass
    return False, "none"


def enable(hwnd, report=None):
    """把 hwnd 从 OLE 拖放切换到原生 WM_DROPFILES。

    `report` 传 dict 的话会写进诊断细节（给自检留真凭实据）。返回 bool。
    """
    if report is None:
        report = {}
    if not hwnd:
        report["reason"] = "hwnd 为空"
        return False
    api = _api()
    if api is None:
        report["reason"] = "Win32 API 不可用（非 Windows？）"
        return False

    hwnd = int(hwnd)
    report["hwnd"] = hwnd

    # ① 先建原生通道（见模块 docstring：顺序反了会两条路同时死）
    try:
        api["shell"].DragAcceptFiles(hwnd, True)
    except Exception as exc:
        report["reason"] = "DragAcceptFiles 失败：%r" % (exc,)
        return False
    report["accept_style"] = style_accepts_files(hwnd)
    if not report["accept_style"]:
        report["reason"] = "DragAcceptFiles 没生效（WS_EX_ACCEPTFILES 未置位）"
        return False

    # ② 再注销 Qt 的 OLE 目标。S_OK(0) 说明**确实注销掉了一个** —— 这本身就
    #    是"Qt 注册过 OLE 拖放"的证据（不注册会返回 DRAGDROP_E_NOTREGISTERED）。
    try:
        hr = int(api["ole"].RevokeDragDrop(hwnd))
    except Exception as exc:
        hr = -1
        report["revoke_error"] = repr(exc)
    report["revoke_hr"] = hr
    report["ole_was_registered"] = (hr == 0)

    # ③ 放行三条消息
    filters = {}
    for msg in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):
        ok, how = _allow_message(api, hwnd, msg)
        filters["0x%04X" % msg] = {"ok": ok, "via": how}
    report["filters"] = filters
    report["dropfiles_allowed"] = filters["0x0233"]["ok"]

    ok = bool(report["accept_style"] and report["dropfiles_allowed"])
    report["reason"] = "ok" if ok else "UIPI 消息未放行"
    return ok


# ---------------------------------------------------------------------------
# HDROP 解析
# ---------------------------------------------------------------------------
def query_paths(hdrop):
    """从 HDROP 取出拖拽的文件路径列表。"""
    api = _api()
    if api is None or not hdrop:
        return []
    shell = api["shell"]
    try:
        count = int(shell.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0))
    except Exception:
        return []
    out = []
    for i in range(count):
        try:
            need = int(shell.DragQueryFileW(hdrop, i, None, 0))
            buf = ctypes.create_unicode_buffer(need + 1)
            shell.DragQueryFileW(hdrop, i, buf, need + 1)
            val = buf.value
        except Exception:
            continue
        if val:
            out.append(val)
    return out


def query_point(hdrop):
    """从 HDROP 取出落点（**接收窗口的客户区坐标**）。取不到返回 None。"""
    api = _api()
    if api is None or not hdrop:
        return None
    pt = wintypes.POINT()
    try:
        if not api["shell"].DragQueryPoint(hdrop, ctypes.byref(pt)):
            return None
    except Exception:
        return None
    return (int(pt.x), int(pt.y))


def finish(hdrop):
    """释放 HDROP。**必须**在取完数据后调用一次，否则壳侧资源泄漏。"""
    api = _api()
    if api is None or not hdrop:
        return
    try:
        api["shell"].DragFinish(hdrop)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 落点 → 控件
# ---------------------------------------------------------------------------
def resolve_sink(window, client_pt):
    """把客户区落点翻译成带 `SINK_PROPERTY` 标记的**可见**投放区。

    特意不用 `QApplication.widgetAt`：它走 `WindowFromPoint` 全局命中测试，
    在"窗口还没真正映射 / 被别的顶层窗口盖住 / 无桌面会话"时会返回 None ——
    而这种"取不到"的表现恰好就是"拖了没反应"，正是我们要修的那个病。

    窗口既然收到了 WM_DROPFILES，落点就一定在它自己的客户区里，所以直接按
    几何找即可：遍历子孙里带标记、且**当前可见**的控件，看落点在不在它的
    矩形里。可见性这一条不能省 —— QStackedWidget 里没在看的那一页也是
    window 的子孙，不判可见就会把落在首页的文件偷偷投给联机页的云盘。
    """
    if client_pt is None or window is None:
        return None
    try:
        pt = QPoint(int(client_pt[0]), int(client_pt[1]))
        candidates = window.findChildren(QWidget)
    except Exception:
        return None
    for w in candidates:
        try:
            if not bool(w.property(SINK_PROPERTY)):
                continue
            if not w.isVisible():
                continue
            top = w.mapTo(window, QPoint(0, 0))
            if QRect(top, w.size()).contains(pt):
                return w
        except Exception:
            continue
    return None


def deliver(sink, paths):
    """把路径交给投放区。返回是否真的送到了。"""
    if sink is None or not paths:
        return False
    fn = getattr(sink, "accept_native_paths", None)
    if fn is None:
        return False
    try:
        fn(list(paths))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 原生事件过滤器
# ---------------------------------------------------------------------------
class NativeDropFilter(QAbstractNativeEventFilter):
    """收 `WM_DROPFILES`，解析后回调 `on_drop(paths, client_pt)`。

    ⚠️ 实例必须被持有（MainWindow 存成属性）：`installNativeEventFilter`
    只是登记了一个裸指针，过滤器被回收后 Qt 会拿着野指针回调。

    ⚠️ 回调跑在 Qt 事件派发线程里，且**晚于** `DragQueryFileW` —— 也就是说
    HDROP 在回调之前就已经 `DragFinish` 了。回调里不要再去碰 hdrop。
    """

    def __init__(self, on_drop=None):
        super().__init__()
        self.on_drop = on_drop
        self.hwnd = 0
        self.report = {}
        self.seen = 0

    def arm(self, hwnd):
        """挂到某个窗口句柄上（窗口重建后要重新 arm，见 WinIdChange）。"""
        if not hwnd:
            return False
        self.hwnd = int(hwnd)
        ok = enable(self.hwnd, self.report)
        self.report["armed"] = ok
        return ok

    # ------------------------------------------------------------ Qt 回调
    def nativeEventFilter(self, eventType, message):
        # PySide6 给的是 bytes（实测 b'windows_generic_MSG'），但不同版本/
        # 不同消息来源也可能走 dispatcher 那条，两条都接。
        try:
            et = eventType.decode("ascii", "ignore") \
                if isinstance(eventType, (bytes, bytearray)) else str(eventType)
        except Exception:
            return False, 0
        if "windows_generic_MSG" not in et and "windows_dispatcher_MSG" not in et:
            return False, 0
        addr = _ptr_value(message)
        if not addr:
            return False, 0
        try:
            raw = ctypes.string_at(addr, _MSG_SIZE)
        except Exception:
            return False, 0
        if len(raw) < _MSG_SIZE:
            return False, 0
        try:
            _hwnd, msg, _pad, wparam, _lparam = struct.unpack_from(_MSG_FMT, raw, 0)[:5]
        except Exception:
            return False, 0
        if int(msg) != WM_DROPFILES:
            return False, 0

        hdrop = wparam & 0xFFFFFFFFFFFFFFFF
        paths = query_paths(hdrop)
        pt = query_point(hdrop)
        finish(hdrop)           # ⚠️ 无论成功失败都要收尾，否则壳侧资源泄漏
        self.seen += 1
        if self.on_drop is not None:
            try:
                self.on_drop(paths, pt)
            except Exception:
                pass
        return True, 0


def _ptr_value(message):
    """`message` 可能是 int，也可能是 sip.voidptr —— 统一取地址。"""
    if isinstance(message, int):
        return message
    for attr in ("__int__", "__index__"):
        try:
            return int(getattr(message, attr)())
        except Exception:
            continue
    try:
        return int(message)
    except Exception:
        return 0
