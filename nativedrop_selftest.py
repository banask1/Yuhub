# -*- coding: utf-8 -*-
"""打包后自检：临时云盘的拖拽投放通道（提权运行时 Qt 拖放的唯一活路）。

用法：`Yuhub.exe --nativedrop-selftest <结果json路径>`

## 为什么必须在 exe 内部自检

这条链路的**失效形态是"什么都不发生"**：鼠标照样显示"可拖放"、松手后没有
任何报错、日志也不会多一行 —— 和用户报的症状完全一致。外部脚本既驱动不了
UIPI 的完整性级别差异，也发不了真正的 `WM_DROPFILES`。

所以这里在 exe 内部做一次**真的**投放：
  1. 建一个真窗口（有真 HWND）+ 一个真的投放区控件；
  2. 走 `nativedrop.enable()` 切通道，拿**独立于其返回值的第二条证据**
     （`WS_EX_ACCEPTFILES` 真的置位）；
  3. 手工造一个真 HDROP（GlobalAlloc + DROPFILES 结构 + UTF-16 路径表），
     用 `PostMessageW` 真的投递一条 `WM_DROPFILES`；
  4. 断言过滤器收到了**完全一致的路径**（含中文与空格），落点解析命中投放区，
     且 `dropped` 信号真的带着这些路径发了出去。

第 3 步是整套检查的关键：只查常量和函数存在性的"自检"在这里毫无价值 ——
`uninstaller.py` 那个 `NameError` 就是在一堆绿灯下活了三个版本。
"""
import ctypes
import json
import os
import struct
import sys
import time
from ctypes import wintypes

GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
WM_USER_TEST = 0x0400 + 777      # 一条我们自己发的无关消息（验证不被误吞）


# ---------------------------------------------------------------------------
# 造 HDROP：这一步不真造，整条链路的验证就是纸面上的
# ---------------------------------------------------------------------------
def _win32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u = ctypes.WinDLL("user32", use_last_error=True)
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.restype = wintypes.BOOL
    u.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                               ctypes.c_size_t, ctypes.c_ssize_t]
    u.PostMessageW.restype = wintypes.BOOL
    return k, u


def make_hdrop(paths, pt):
    """造一个真 HDROP（DROPFILES + 双零结尾的 UTF-16 路径表）。

    结构：`DWORD pFiles; POINT pt; BOOL fNC; BOOL fWide;` 共 20 字节，
    后面接路径表。返回的句柄交给 `WM_DROPFILES` 的 wParam；
    **不要自己 GlobalFree** —— 接收侧 `DragFinish` 会释放它。
    """
    k, _ = _win32()
    blob = ("".join(p + "\x00" for p in paths) + "\x00").encode("utf-16-le")
    total = 20 + len(blob)
    h = k.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, total)
    if not h:
        return 0
    ptr = k.GlobalLock(h)
    if not ptr:
        return 0
    buf = (ctypes.c_char * total).from_address(int(ptr))
    struct.pack_into("<I i i i i", buf, 0, 20, int(pt[0]), int(pt[1]), 0, 1)
    buf[20:20 + len(blob)] = blob
    k.GlobalUnlock(h)
    return int(h)


def post_drop(hwnd, paths, pt):
    """真的往窗口投递一条 WM_DROPFILES。返回是否投递成功。"""
    _, u = _win32()
    hdrop = make_hdrop(paths, pt)
    if not hdrop:
        return False
    return bool(u.PostMessageW(int(hwnd), 0x0233, hdrop, 0))


# ---------------------------------------------------------------------------
def _sink_center(window, sink):
    from PySide6.QtCore import QPoint
    p = sink.mapTo(window, QPoint(sink.width() // 2, sink.height() // 2))
    return (int(p.x()), int(p.y()))


def _pump(app, seconds=3.0):
    """把 Qt 事件循环推几轮（原生消息是异步投递的）。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def run(out_file):
    """返回进程退出码：0 = 通过，1 = 失败，4 = 结果写盘失败。"""
    result = {"ok": False, "checks": [], "runtime": {}, "logs": []}
    t0 = time.time()

    def mark(name, ok=None, detail=""):
        result["checks"].append({"name": name, "pass": ok, "detail": detail})

    def tlog(msg):
        result["logs"].append({"t": round(time.time() - t0, 3), "msg": msg})

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ui import nativedrop
    except Exception as exc:
        mark("导入 ui.nativedrop", False, repr(exc))
        _dump(result, out_file)
        return 1

    if not nativedrop.available():
        mark("Win32 拖放 API 可用", False, "当前不是 Windows 或缺少 API")
        _dump(result, out_file)
        return 1
    mark("Win32 拖放 API 可用", True,
         "WM_DROPFILES=0x%04X" % nativedrop.WM_DROPFILES)

    # 常量：改错一个就整条链路静默失效，钉死
    mark("消息号常量正确",
         nativedrop.WM_DROPFILES == 0x0233 and nativedrop.WM_COPYDATA == 0x004A
         and nativedrop.WM_COPYGLOBALDATA == 0x0049,
         "033=%s 04A=%s 049=%s" % (hex(nativedrop.WM_DROPFILES),
                                   hex(nativedrop.WM_COPYDATA),
                                   hex(nativedrop.WM_COPYGLOBALDATA)))

    try:
        from PySide6.QtCore import QPoint, Signal
        from PySide6.QtWidgets import QApplication, QFrame, QWidget
    except Exception as exc:
        mark("导入 PySide6", False, repr(exc))
        _dump(result, out_file)
        return 1

    app = QApplication.instance() or QApplication([])

    # ---------------------------------------------------------- 真窗口 + 真投放区
    class _Sink(QFrame):
        """与 lan_page._DropZone 同构的投放区（标记 + dropped 信号）。"""

        dropped = Signal(list)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setProperty(nativedrop.SINK_PROPERTY, True)
            self.got = []

        def accept_native_paths(self, paths):
            self.got.append(list(paths))
            self.dropped.emit(list(paths))

    win = QWidget()
    win.setWindowTitle("Yuhub 拖放自检")
    win.resize(420, 320)
    sink = _Sink(win)
    sink.setGeometry(60, 120, 240, 90)
    win.show()
    _pump(app, 0.8)

    got_from_signal = []
    sink.dropped.connect(lambda paths: got_from_signal.append(list(paths)))

    try:
        hwnd = int(win.winId())
    except Exception:
        hwnd = 0
    result["runtime"]["hwnd"] = hwnd
    result["runtime"]["visible"] = bool(win.isVisible())
    if not hwnd:
        mark("窗口拿到真句柄", False, "winId()=%s（无头环境？）" % hwnd)
        _dump(result, out_file)
        return 1
    mark("窗口拿到真句柄", True, "hwnd=%d visible=%s" % (hwnd, win.isVisible()))

    # ---------------------------------------------------------- ① 切通道
    seen = []
    routed = []

    def _on_drop(paths, pt):
        """完全复刻 MainWindow._on_native_drop 的路由动作。"""
        seen.append((list(paths), pt))
        sink2 = nativedrop.resolve_sink(win, pt)
        if nativedrop.deliver(sink2, paths):
            routed.append(True)

    filt = nativedrop.NativeDropFilter(_on_drop)
    try:
        app.installNativeEventFilter(filt)
        armed = filt.arm(hwnd)
    except Exception as exc:
        mark("安装原生事件过滤器", False, repr(exc))
        _dump(result, out_file)
        return 1

    rep = dict(filt.report or {})
    result["runtime"]["enable_report"] = rep
    tlog("enable report = %r" % (rep,))

    mark("★ 切到原生拖放通道", armed, rep.get("reason", ""))
    # 第二条证据：不读 enable() 的返回值，直接查窗口扩展样式
    mark("★ WS_EX_ACCEPTFILES 真的置位（独立复读，不看 enable 返回值）",
         nativedrop.style_accepts_files(hwnd),
         "style_accepts_files=%s" % nativedrop.style_accepts_files(hwnd))
    mark("★ WM_DROPFILES 已被 UIPI 放行（提权时才收得到）",
         bool(rep.get("dropfiles_allowed")),
         "filters=%s" % (rep.get("filters"),))
    # 诊断项（不参与判定）：hr=0 说明确实注销掉了一个 OLE 目标 —— 它就是
    # "Qt 注册过 OLE 拖放"的证据；若某版本 Qt 不注册，原生通道照样能用，
    # 所以只记不判。
    result["runtime"]["ole_was_registered"] = rep.get("ole_was_registered")
    result["runtime"]["revoke_hr"] = rep.get("revoke_hr")

    # ---------------------------------------------------------- ② 真的投一次
    paths = [r"C:\Users\test\拖放 测试 文件.txt",
             r"D:\游戏\联机\room code.txt"]
    center = _sink_center(win, sink)
    result["runtime"]["drop_point"] = center
    ok_post = post_drop(hwnd, paths, center)
    mark("投递 WM_DROPFILES", ok_post, "落点=%s" % (center,))
    _pump(app, 3.0)

    mark("★ 过滤器收到了 WM_DROPFILES", len(seen) == 1,
         "seen=%d" % len(seen))
    if seen:
        got_paths, got_pt = seen[0]
        mark("★ 取回的路径与投放内容完全一致（含中文与空格）",
             got_paths == paths, "收到 %r" % (got_paths,))
        mark("落点解析到投放区",
             nativedrop.resolve_sink(win, got_pt) is sink,
             "pt=%s 命中=%s" % (got_pt,
                                nativedrop.resolve_sink(win, got_pt) is sink))
    else:
        mark("★ 取回的路径与投放内容完全一致（含中文与空格）", False, "没收到消息")
        mark("落点解析到投放区", False, "没收到消息")

    # ---------------------------------------------------------- ③ 端到端：信号真的发了
    # 上面的回调已经按 MainWindow 的做法把消息路由给投放区了，所以这里直接
    # 验"dropped 信号带着原值发了出去" —— 下游 _on_share_paths 接的就是它。
    mark("★ 真消息端到端：dropped 信号带着原值发出",
         routed == [True] and got_from_signal == [paths] and sink.got == [paths],
         "路由=%r 信号收到 %r" % (routed, got_from_signal))

    # ---------------------------------------------------------- ④ 落在别处不误投
    seen[:] = []
    ok_post2 = post_drop(hwnd, [r"C:\Windows\notepad.exe"], (4, 4))
    _pump(app, 2.0)
    missed = None
    if seen:
        missed = nativedrop.resolve_sink(win, seen[0][1])
    mark("★ 落在窗口空白处不会误投给投放区",
         ok_post2 and seen and missed is None,
         "投递=%s 解析=%r" % (ok_post2, missed))

    # ---------------------------------------------------------- ⑤ 无关消息不被吞
    before = filt.seen
    _, u = _win32()
    try:
        u.PostMessageW(hwnd, WM_USER_TEST, 0, 0)
    except Exception:
        pass
    _pump(app, 1.0)
    mark("无关消息不会被当成投递（不误吞、不误触发）",
         filt.seen == before, "seen %d → %d" % (before, filt.seen))

    # ---------------------------------------------------------- ⑥ 真投放区还在
    # 静态绊线：lan_page._DropZone 一旦丢掉标记或 accept_native_paths，
    # 原生通道就再也找不到落点 —— 那正是"拖了没反应"的原样回归。
    try:
        from ui.pages.lan_page import LanPage, _DropZone
        zone = _DropZone()
        has_prop = bool(zone.property(nativedrop.SINK_PROPERTY))
        has_fn = callable(getattr(zone, "accept_native_paths", None))
        has_sig = hasattr(zone, "dropped")
        has_sink_fn = callable(getattr(LanPage, "_on_share_paths", None))
        zone.deleteLater()
        mark("★ 真投放区带寻址标记 + 原生入口 + dropped 信号",
             has_prop and has_fn and has_sig and has_sink_fn,
             "prop=%s accept_native_paths=%s dropped=%s _on_share_paths=%s"
             % (has_prop, has_fn, has_sig, has_sink_fn))
    except Exception as exc:
        # 导入不了就说清楚，绝不记成 pass —— 记成 pass 等于把洞埋起来
        mark("★ 真投放区带寻址标记 + 原生入口 + dropped 信号", False,
             "导入 ui.pages.lan_page 失败：%r" % (exc,))

    try:
        win.close()
        win.deleteLater()
    except Exception:
        pass

    result["ok"] = all(c["pass"] for c in result["checks"])
    _dump(result, out_file)
    return 0 if result["ok"] else 1


def _dump(result, out_file):
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as exc:
        text = json.dumps({"ok": False, "checks": result.get("checks", []),
                           "dump_error": repr(exc)},
                          ensure_ascii=False, indent=2, default=str)
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return 4
    return 0
