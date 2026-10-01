"""Yuhub 主窗口：极简色块（Pixel-UI）风格 + 自定义标题栏 + 侧栏 + 主题切换。"""

import os
import sys
import threading

from PySide6.QtCore import Qt, QEvent, QObject, QSettings, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QApplication,
    QFrame,
    QButtonGroup,
)

from . import theme
from . import VERSION, VERSION_LABEL
from .tray import TrayIcon, tray_available
from .widgets import show_toast
from .update_ui import UpdateChecker, UpdateAvailableDialog, UpdateProgressDialog
from .pages.home_page import HomePage
from .pages.cleaner_page import CleanerPage
from .pages.download_page import DownloadPage
from .pages.lan_page import LanPage
from .pages.uninstall_page import UninstallPage
from .pages.settings_page import SettingsPage

RESIZE_MARGIN = 5
WINDOW_MARGIN = 6           # 圆角窗口四周的透明留白（极简风格：留白收窄）
SIDEBAR_WIDTH = 216
SIDEBAR_COLLAPSED = 76

NAV_ITEMS = [
    ("home", "🏠", "首页"),
    ("cleaner", "🧹", "C盘清理"),
    ("uninstall", "🗑️", "软件卸载"),
    ("download", "⬇️", "多线程下载"),
    ("lan", "🌐", "异地联机"),
    ("settings", "⚙️", "设置中心"),
]

# 侧栏导航的色块标识（极简色块风格的点缀）
NAV_TINTS = {
    "home": "tile_1",
    "cleaner": "tile_3",
    "uninstall": "tile_6",
    "download": "tile_5",
    "lan": "tile_2",
    "settings": "tile_4",
}


def resource_path(rel):
    """兼容源码运行与 PyInstaller 打包后的资源路径。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, rel)


def read_bool(settings, key, default):
    """从 QSettings 读布尔值。

    不用 `value(key, default, type=bool)`：Windows 上用原生格式存进去是字符串，
    取回来再转 bool 时 `bool("false")` 是 True——「关掉后重启又自己打开了」。
    这里显式按字符串判断，避免这个静默错误。
    """
    raw = settings.value(key, None)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def init_theme_from_settings(settings):
    """按 QSettings 里保存的偏好恢复主题包与深浅模式。

    单独抽出来是因为启动画面（ui/splash.py）必须在**主窗口构建之前**
    就拿到正确的深浅色——用户双击 exe 后启动画面第一个出现，
    而构建全部页面要几百毫秒，不能让浅色用户先看一屏深色。
    MainWindow.__init__ 也会再调一次（幂等：重复 set_pack/set_theme 无副作用）。
    """
    theme.reload_packs()
    saved_pack = settings.value("theme_pack", "")
    if saved_pack and saved_pack in theme.packs():
        theme.set_pack(saved_pack)
    theme.set_theme(theme.resolve_theme(settings.value("theme", "dark")))


class NavButton(QPushButton):
    """侧栏导航按钮：极简色块模式是纯色小方块；玻璃主题是拟物玻璃贴片。"""

    def __init__(self, key, icon, text, parent=None):
        super().__init__(parent)
        self.setObjectName("SidebarButton")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._icon = icon
        self._label = text
        self._tint_key = NAV_TINTS.get(key, "tile_1")
        theme.bus.changed.connect(lambda _: self._refresh())
        # 选中态变化时也要刷新（玻璃贴片选中时描边换成主题色）
        self.toggled.connect(lambda *_: self._refresh())
        self.set_expanded(True)

    def _refresh(self):
        cur = theme.current()
        color = cur.get(self._tint_key, cur["accent"])
        if theme.glass_on():
            # 拟物玻璃模式：emoji 装进 30x30 玻璃贴片（对角渐变 + 高光描边），
            # 选中时描边换成主题色，像玻璃边缘被点亮
            self._dot.setVisible(False)
            self._icon_lbl.setVisible(False)
            self._tile.setVisible(True)
            hl = cur.get("accent", color) if self.isChecked() else None
            self._tile.setStyleSheet(theme.glass_tile(color, radius=9,
                                                      highlight=hl))
        else:
            self._tile.setVisible(False)
            self._icon_lbl.setVisible(True)
            self._dot.setVisible(True)
            self._dot.setStyleSheet(
                f"background: {color}; border: none; border-radius: 3px;"
            )

    def set_expanded(self, expanded):
        if self.layout() is None:
            lay = QHBoxLayout(self)
            lay.setContentsMargins(14, 0, 12, 0)
            lay.setSpacing(10)
            # 极简色块模式的标识：12x12 纯色方块
            self._dot = QFrame()
            self._dot.setFixedSize(12, 12)
            lay.addWidget(self._dot, 0, Qt.AlignVCenter)
            # 玻璃主题的标识：30x30 拟物贴片 + emoji（两种模式各建一份，
            # 按当前主题切显示，切换主题包时无需重建布局）
            self._tile = QFrame()
            self._tile.setFixedSize(30, 30)
            tile_lay = QVBoxLayout(self._tile)
            tile_lay.setContentsMargins(0, 0, 0, 0)
            self._tile_icon = QLabel(self._icon)
            self._tile_icon.setAlignment(Qt.AlignCenter)
            self._tile_icon.setStyleSheet(
                "font-size: 15px; background: transparent;")
            tile_lay.addWidget(self._tile_icon)
            lay.addWidget(self._tile, 0, Qt.AlignVCenter)
            self._icon_lbl = QLabel(self._icon)
            self._icon_lbl.setStyleSheet("font-size: 14px; background: transparent;")
            lay.addWidget(self._icon_lbl, 0, Qt.AlignVCenter)
            self._text_lbl = QLabel(self._label)
            self._text_lbl.setStyleSheet("font-size: 13px; background: transparent;")
            lay.addWidget(self._text_lbl, 1)
            self._refresh()
        self._text_lbl.setVisible(expanded)
        self.setText("")
        self.setToolTip("" if expanded else self._label)


class TitleBar(QFrame):
    """自定义标题栏：拖动移动、双击最大化、侧栏折叠、主题切换、窗口按钮。"""

    def __init__(self, window):
        super().__init__()
        self.setObjectName("TitleBar")
        self.setFixedHeight(46)
        self._window = window
        self._drag_offset = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 8, 0)
        layout.setSpacing(6)

        self.btn_menu = self._make("☰")
        self.btn_menu.setToolTip("折叠/展开侧边栏")
        layout.addWidget(self.btn_menu)

        icon = QLabel("🔷")
        icon.setStyleSheet("font-size: 15px; background: transparent;")
        layout.addWidget(icon)

        title = QLabel("Yuhub")
        title.setObjectName("AppTitle")
        layout.addWidget(title)

        sub = QLabel(VERSION_LABEL)
        sub.setObjectName("AppSubtitle")
        layout.addWidget(sub)

        layout.addStretch(1)

        self.btn_theme = self._make("🌙")
        self.btn_theme.setToolTip("切换浅色 / 深色主题")
        layout.addWidget(self.btn_theme)

        self.btn_min = self._make("—")
        self.btn_max = self._make("□")
        self.btn_close = self._make("✕", danger=True)
        layout.addWidget(self.btn_min)
        layout.addWidget(self.btn_max)
        layout.addWidget(self.btn_close)

        self.btn_menu.clicked.connect(self._window.toggle_sidebar)
        self.btn_theme.clicked.connect(self._window.toggle_theme)
        self.btn_min.clicked.connect(self._window.showMinimized)
        self.btn_max.clicked.connect(self._window.toggle_max)
        self.btn_close.clicked.connect(self._window.close)

    def _make(self, text, danger=False):
        b = QPushButton(text)
        b.setObjectName("TitleButton")
        if danger:
            b.setProperty("danger", True)
        b.setFixedSize(36, 32)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def update_theme_button(self, resolved):
        self.btn_theme.setText("🌙" if resolved == "dark" else "☀️")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._window.toggle_max()


class MainWindow(QWidget):
    def __init__(self, start_minimized=False, parent=None):
        super().__init__(parent)
        self.setObjectName("Root")
        self.setWindowTitle("Yuhub")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(760, 520)

        self._settings = QSettings("Yuhub", "Yuhub")
        self.theme_setting = self._settings.value("theme", "dark")

        # 主题包：先从文档目录扫一遍（首次运行会创建 YuUI），
        # 然后恢复上次选中的那套。主题目录被删/改名时自动落回默认。
        # （main.py 在弹出启动画面之前已经调过一次，这里再调是幂等的。）
        init_theme_from_settings(self._settings)

        # 关闭窗口时是否留在托盘。托盘不可用时这个开关没有意义（藏起来就真没了）。
        self._close_to_tray = read_bool(self._settings, "close_to_tray", True)
        self.tray = None
        self.tray_available = tray_available()

        # 「真的退出」标记：托盘菜单退出、以及关掉托盘留驻时的 ✕ 都会置上，
        # closeEvent 靠它区分「用户想收起来」和「用户想走」。
        self._quitting = False
        self._torn_down = False

        self._override_cursor_active = False
        self._pages = {}
        self._nav_buttons = {}
        self._sidebar_collapsed = False

        self._build_ui()
        self._build_tray()
        self._fit_to_screen()
        self._apply_theme()
        self._install_resize_filter()

        # 开机自启走这条：不弹窗口，直接安静地待在托盘里。
        # 托盘不可用时退回正常显示，否则用户会发现"程序启动了但什么也看不到"。
        self.started_hidden = bool(start_minimized and self.tray is not None)
        if self.started_hidden:
            self.hide()

        # 自动更新：启动后延迟一点在后台静默检查（不阻塞启动、失败不打扰）。
        # 用户可在设置里关掉（"自动检查更新"开关）。
        self._update_busy = False
        self._updater_thread = None
        self._update_pending = None
        self._dl_cancel = None
        if read_bool(self._settings, "auto_check_update", True):
            QTimer.singleShot(2500, self._check_update_silent)

    # ------------------------------------------------------------ 自动更新
    def _check_update_silent(self):
        """启动时的静默检查：只有发现新版本才弹窗，任何失败都不打扰用户。"""
        if self._update_busy:
            return
        self._update_busy = True
        th = UpdateChecker(VERSION)
        th.found.connect(self._on_update_found)
        th.finished_.connect(self._on_update_check_done)
        self._updater_thread = th
        th.start()

    def _on_update_check_done(self):
        self._update_busy = False

    def _on_update_found(self, info):
        """子线程发现新版 → 主线程弹窗。"""
        # 开机自启（静默进托盘）时不弹窗打扰；等用户真的打开窗口再说
        if self.started_hidden and not self.isVisible():
            self._update_pending = info
            return
        self._prompt_update(info)

    def _prompt_update(self, info):
        dlg = UpdateAvailableDialog(info, VERSION_LABEL, parent=self)
        dlg.exec()
        if dlg.action == "update":
            self._start_update_download(info)

    def _start_update_download(self, info):
        """下载更新包（后台线程），完成后询问是否重启并更新。

        ⚠️ v0.8.9beta 修复的根因：旧版在工作线程里用
        `QTimer.singleShot(0, ...)` 把进度/结果抛回主线程——但 0ms
        单发定时器依附于**调用线程**的事件循环，下载线程没有 Qt 事件
        循环，回调永远不触发。表现为：进度条永远 0%（像"无法下载"）、
        下载结束不提示、点取消后界面永远停在「正在取消…」（像"取消
        没用"）。
        正确姿势：工作线程只 emit QObject 信号，Qt 自动把跨线程信号
        排队（QueuedConnection）到接收者所在的主线程执行。
        """
        import updater

        prog = UpdateProgressDialog(info, parent=self)
        self._dl_cancel = False
        self._upd_result = None

        # ---- 信号桥：下载线程 → 主线程的唯一通道 ----
        class _Bridge(QObject):
            probing = Signal(str)
            status = Signal(str)
            progress = Signal(object)
            finished = Signal(bool, str, str)      # ok, msg, path

        bridge = _Bridge()
        bridge.probing.connect(prog.set_probing)
        bridge.status.connect(prog.set_status)
        bridge.progress.connect(prog.set_progress)

        def cancel():
            return bool(self._dl_cancel)

        def emit_progress(snap):
            """下载线程的进度回调 → 只 emit 信号，绝不直接碰 UI。"""
            try:
                s = dict(snap)
                if s.get("phase") == "probing":
                    s.pop("phase", None)
                    bridge.probing.emit(s.pop("status_text", "")
                                        or "正在连接更新源…")
                    return
                text = s.pop("status_text", None)
                if text:
                    bridge.status.emit(text)
                if s:
                    bridge.progress.emit(s)
            except RuntimeError:
                pass

        def work():
            try:
                ok, msg, path = updater.download_update(
                    info, on_progress=emit_progress, cancel=cancel)
            except Exception as e:
                ok, msg, path = False, "下载异常：%s" % (e,), ""
            bridge.finished.emit(bool(ok), str(msg), str(path or ""))

        def on_finished(ok, msg, path):
            # 主线程：落定对话框终态。用户点「关闭」accept 后，
            # 外层 exec() 返回，再由 _start_update_download 收尾。
            self._upd_result = (ok, msg, path)
            if ok:
                prog.finish_ok("更新包已就绪")
            elif msg == "已取消":
                prog.finish_fail("已取消下载")
            else:
                prog.finish_fail("下载失败：%s" % msg)

        def on_cancel():
            self._dl_cancel = True

        bridge.finished.connect(on_finished)
        prog.cancelled.connect(on_cancel)
        threading.Thread(target=work, name="YuhubUpdateDownload",
                         daemon=True).start()
        prog.exec()

        # 对话框关闭后收尾（单层事件循环，不再嵌套 exec）
        r = self._upd_result
        self._upd_result = None
        if r and r[0] and r[2]:
            self._apply_update(info, r[2])

    def _apply_update(self, info, package):
        """把更新交给替换器：拷一份自己做替换器 → 拉起 → 自己退出。"""
        import updater

        # 体检更新包：不合格就**不要**进入替换流程。
        # 因为一旦把替换器拉起来，本进程马上就退出了；替换器再发现包是坏的，
        # 用户看到的就是"点了更新、窗口没了、什么也没发生"，而不是一句原因。
        # 更糟的是坏包会被 Windows 弹成「不支持的 16 位应用程序」系统框。
        fine, why = updater.check_package_runnable(package)
        if not fine:
            show_toast(self, "更新包有问题：%s" % why)
            return

        try:
            payload = updater.build_request(
                package, updater.sys_executable(), os.getpid(),
                new_version=info.tag,
            )
            staged = updater.stage_updater(payload)
            need_admin = updater.needs_elevation(payload["target_exe"])
            ok, msg = updater.launch_updater(staged, payload, elevated=need_admin)
        except Exception as e:
            show_toast(self, "启动更新失败：%s" % (e,))
            return

        if not ok:
            show_toast(self, msg)
            return

        # 更新程序已接手，本进程必须退出，否则它等不到旧 PID 结束
        show_toast(self, "正在更新，程序即将重启…")
        QTimer.singleShot(600, self.request_quit)

    def apply_repair_package(self, info, package):
        """一键修复软件的替换入口（设置中心调用）。

        与 _apply_update 完全同一条替换链路：两步改名 + 失败回滚。
        单独起个公开名字是为了语义清晰：这里替换下来的可能是
        「与当前版本一致的修复包」，不一定是新版本。
        """
        self._apply_update(info, package)

    def show_update_if_pending(self):
        """窗口真正显示出来时，把静默期间挂起的更新提示补上。"""
        info = self._update_pending
        if info is not None:
            self._update_pending = None
            QTimer.singleShot(400, lambda i=info: self._prompt_update(i))

    def check_update_now(self, interactive=True):
        """设置页「检查更新」按钮入口。返回是否发起了检查。"""
        if self._update_busy:
            if interactive:
                show_toast(self, "正在检查更新…")
            return False
        self._update_busy = True
        th = UpdateChecker(VERSION)
        if interactive:
            th.found.connect(self._on_update_found)
            th.failed.connect(lambda e: show_toast(self, "检查失败：%s" % e))
            th.finished_.connect(
                lambda: (setattr(self, "_update_busy", False),
                         show_toast(self, "已是最新版本") if th.result is None
                         and not th.error else None))
        else:
            th.found.connect(self._on_update_found)
            th.finished_.connect(self._on_update_check_done)
        self._updater_thread = th
        th.start()
        return True

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN)
        self._outer.setSpacing(0)

        # 实心圆角外壳（无玻璃模糊）
        self.frame = QFrame()
        self.frame.setObjectName("AppFrame")
        self.frame.setProperty("rounded", True)
        self._outer.addWidget(self.frame)

        frame_layout = QVBoxLayout(self.frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)

        self.titlebar = TitleBar(self)
        frame_layout.addWidget(self.titlebar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        frame_layout.addLayout(body, 1)

        self.sidebar = self._build_sidebar()
        body.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)

        self._build_pages()
        self.switch_page("home")

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)

        self._sidebar_layout = QVBoxLayout(sidebar)
        self._sidebar_layout.setContentsMargins(0, 12, 0, 12)
        self._sidebar_layout.setSpacing(2)

        self._sidebar_section = QLabel("工具")
        self._sidebar_section.setObjectName("SidebarSection")
        self._sidebar_layout.addWidget(self._sidebar_section)
        self._sidebar_layout.addSpacing(4)

        group = QButtonGroup(self)
        group.setExclusive(True)

        for key, icon, text in NAV_ITEMS:
            btn = NavButton(key, icon, text)
            btn.clicked.connect(lambda checked=False, k=key: self.switch_page(k))
            group.addButton(btn)
            self._nav_buttons[key] = btn
            self._sidebar_layout.addWidget(btn)

        self._sidebar_layout.addStretch(1)

        self._sidebar_version = QLabel(f"Yuhub {VERSION_LABEL}")
        self._sidebar_version.setObjectName("SidebarSection")
        self._sidebar_version.setAlignment(Qt.AlignCenter)
        self._sidebar_layout.addWidget(self._sidebar_version)

        return sidebar

    def _build_pages(self):
        def make(page_cls):
            return page_cls(notify=self.notify)

        self._pages = {
            "home": make(HomePage),
            "cleaner": make(CleanerPage),
            "uninstall": make(UninstallPage),
            "download": make(DownloadPage),
            "lan": make(LanPage),
            "settings": SettingsPage(
                notify=self.notify,
                theme_setting=self.theme_setting,
                on_theme_change=self.set_theme_setting,
                on_pack_change=self.set_theme_pack,
                # 传**原始偏好**而不是 close_to_tray 属性：属性里带了
                # "托盘是否存在"的实时判断，而建页面时 _build_tray() 还没跑、
                # self.tray 仍是 None → 属性会算成 False，
                # 于是设置页开关会显示为「关」，明明默认是开的。
                close_to_tray=self._close_to_tray,
                on_close_to_tray_change=self.set_close_to_tray,
                tray_available=self.tray_available,
            ),
        }
        for page in self._pages.values():
            self.stack.addWidget(page)

        self._pages["home"].navigate.connect(self.switch_page)

    def _build_tray(self):
        """建立托盘图标。

        图标取不到就不建托盘（而不是拿个空图标硬撑）——此时
        `self.tray is None` 会让关闭行为自动退回「真退出」，
        不会出现"窗口没了、托盘也没图标"的失联状态。
        """
        if not self.tray_available:
            return
        icon = QIcon(resource_path(os.path.join("resources", "app.ico")))
        if icon.isNull():
            return
        self.tray = TrayIcon(self, icon)
        self.tray.show()

    # ------------------------------------------------------------ 托盘
    @property
    def close_to_tray(self):
        """当前生效的「关窗口时留驻托盘」状态。

        托盘不可用时强制为 False：设置项本身可以存着（换台机器还有效），
        但这次运行不能真把窗口藏进一个不存在的托盘。
        """
        return bool(self._close_to_tray and self.tray is not None)

    def set_close_to_tray(self, flag):
        """设置页开关的回调：写入偏好（存字符串，跨版本读取更稳）。"""
        self._close_to_tray = bool(flag)
        self._settings.setValue("close_to_tray", "true" if flag else "false")

    def activate_window(self):
        """显示并提到前台。托盘唤起、以及第二个实例启动时都走这里。

        带 `Qt.WindowActive` 一起设是为了绕 Windows 的前台窗口限制：
        无边框窗口单靠 `raise_()` + `activateWindow()` 有时只会在任务栏闪一下，
        窗口并不真的到前面来（尤其是由后台进程请求激活时）。
        """
        if not self.isVisible():
            self.show()
        if self.isMinimized():
            self.showNormal()
        state = self.windowState()
        if state & Qt.WindowState.WindowMinimized:
            state &= ~Qt.WindowState.WindowMinimized
        self.setWindowState(state | Qt.WindowState.WindowActive)
        self.raise_()
        self.activateWindow()

    def request_quit(self):
        """真正退出程序（托盘菜单的「退出 Yuhub」）。"""
        self._quitting = True
        self.close()

    def showEvent(self, event):
        """窗口首次显示后再异步采集硬件信息，避免拖慢启动。"""
        super().showEvent(event)
        if not getattr(self, "_hw_kicked", False):
            self._hw_kicked = True
            home = self._pages.get("home")
            if home is not None and hasattr(home, "start_hardware_scan"):
                QTimer.singleShot(120, home.start_hardware_scan)
        # 开机自启静默进托盘期间发现的更新，等窗口真打开时再提示
        self.show_update_if_pending()

    # ---------------------------------------------------- 启动画面联动
    def launch_with_data_wait(self, on_ready, timeout_ms=12000):
        """启动画面模式：先不显示窗口，等首页硬件数据就绪后回调 on_ready。

        与常规启动（show() → showEvent 里触发扫描）的区别：
        这里窗口保持隐藏，直接驱动 HomePage 开始采集；CPU / 显卡 / 硬盘
        等配置读完（或采集失败 / 超时）才调 on_ready —— 由 main.py 在
        on_ready 里显示窗口并让启动画面做模糊淡出。
        """
        self._launch_cb = on_ready
        self._launch_done = False
        home = self._pages.get("home")
        started = False
        if home is not None and hasattr(home, "start_hardware_scan"):
            home.start_hardware_scan()
            hp = getattr(home, "hw_panel", None)
            if hp is not None:
                # 采集成功(_scanned)与失败(_failed)都要放行——启动画面
                # 不能因为一次采集失败就永远转圈。参数签名不同，槽用 *_。
                hp._scanned.connect(self._on_launch_data_ready)
                hp._failed.connect(self._on_launch_data_ready)
                started = True
        if not started:
            QTimer.singleShot(0, self._on_launch_data_ready)
            return
        # 兜底：信号一个都没来（线程卡死等异常情形）也不能把用户关在门外
        QTimer.singleShot(timeout_ms, self._on_launch_data_ready)

    def _on_launch_data_ready(self, *args):
        if getattr(self, "_launch_done", True):
            return
        self._launch_done = True
        cb = getattr(self, "_launch_cb", None)
        self._launch_cb = None
        if cb is not None:
            try:
                cb()
                return
            except Exception:
                pass
        # 回调缺失或抛异常时兜底显示窗口，绝不能出现"没有任何窗口"
        if not self.isVisible() and not self.started_hidden:
            self.show()

    # -------------------------------------------------------------- 行为
    def closeEvent(self, event):
        """点 ✕ 有两种去向：留驻托盘（默认）或真正退出。

        分流靠 `close_to_tray`：托盘不可用、或用户关掉了这个开关时，
        一律走真退出——不然窗口藏进一个不存在的托盘，用户就彻底失联了。
        """
        if self._quitting or not self.close_to_tray:
            self.teardown()
            event.accept()
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return
        # 留驻托盘：拦下这次关闭，只把窗口藏起来
        event.ignore()
        self.hide()
        if self.tray is not None:
            self.tray.notify_hidden()

    def teardown(self):
        """退出前的收尾：停掉后台采样 / 下载线程，撤掉托盘图标。

        做幂等保护：真退出路径可能被走两次（closeEvent 一次、aboutToQuit 一次），
        重复 stop 线程会抛异常、也可能让收尾只做了一半。
        """
        if self._torn_down:
            return
        self._torn_down = True
        # 下载中的更新要能优雅取消（.part 保留，下次续传）
        self._dl_cancel = True
        for page in self._pages.values():
            if hasattr(page, "shutdown"):
                try:
                    page.shutdown()
                except Exception:
                    pass
        if self.tray is not None:
            try:
                self.tray.hide()
            except RuntimeError:
                pass

    def switch_page(self, key):
        if key not in self._pages:
            return
        page = self._pages[key]
        self.stack.setCurrentWidget(page)
        if key in self._nav_buttons:
            self._nav_buttons[key].setChecked(True)
        # 通知页面已切到前台（用于惰性初始化 / 自动刷新）
        if hasattr(page, "on_shown"):
            try:
                page.on_shown()
            except Exception:
                pass

    def notify(self, text):
        show_toast(self, text)

    def toggle_sidebar(self):
        self._sidebar_collapsed = not self._sidebar_collapsed
        if self._sidebar_collapsed:
            self.sidebar.setFixedWidth(SIDEBAR_COLLAPSED)
            self._sidebar_section.hide()
            self._sidebar_version.hide()
            for btn in self._nav_buttons.values():
                btn.set_expanded(False)
        else:
            self.sidebar.setFixedWidth(SIDEBAR_WIDTH)
            self._sidebar_section.show()
            self._sidebar_version.show()
            for btn in self._nav_buttons.values():
                btn.set_expanded(True)

    # -------------------------------------------------------------- 主题
    def set_theme_setting(self, setting):
        """切换深/浅模式（'system' / 'light' / 'dark'）。"""
        self.theme_setting = setting
        self._settings.setValue("theme", setting)
        self._apply_theme()
        if "settings" in self._pages:
            self._pages["settings"].set_theme_setting(setting)

    def set_theme_pack(self, name):
        """切换主题包（文档目录里的某个主题文件夹）。"""
        if not name:
            return
        theme.set_pack(name)
        self._settings.setValue("theme_pack", theme.current_pack_name())
        self._apply_theme()
        if "settings" in self._pages:
            self._pages["settings"].set_pack_setting(
                theme.current_pack_name())

    def toggle_theme(self):
        current = theme.get_theme()
        self.set_theme_setting("light" if current == "dark" else "dark")

    def _apply_theme(self):
        resolved = theme.resolve_theme(self.theme_setting)
        theme.set_theme(resolved)
        self.setStyleSheet(theme.build_qss())
        self.titlebar.update_theme_button(resolved)

    # -------------------------------------------------------------- 窗口
    def _fit_to_screen(self):
        """按主屏可用区域比例初始化窗口尺寸，并限制在合理范围。"""
        screen = QGuiApplication.primaryScreen()
        if screen:
            g = screen.availableGeometry()
            w = int(g.width() * 0.72)
            h = int(g.height() * 0.82)
            w = max(780, min(w, 1440))
            h = max(540, min(h, 960))
            self.resize(w, h)
        else:
            self.resize(1160, 720)

    def _set_rounded(self, flag):
        self.frame.setProperty("rounded", flag)
        self.frame.style().unpolish(self.frame)
        self.frame.style().polish(self.frame)

    def toggle_max(self):
        if self.isMaximized():
            self._outer.setContentsMargins(WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN)
            self._set_rounded(True)
            self.showNormal()
        else:
            self._outer.setContentsMargins(0, 0, 0, 0)
            self._set_rounded(False)
            self.showMaximized()

    # -------------------------------------------------------- 边缘缩放
    def _install_resize_filter(self):
        QApplication.instance().installEventFilter(self)

    def _edge_at(self, global_pos):
        if self.isMaximized():
            return Qt.Edges()
        g = self.frameGeometry()
        m = RESIZE_MARGIN
        left = global_pos.x() <= g.left() + m
        right = global_pos.x() >= g.right() - m
        bottom = global_pos.y() >= g.bottom() - m
        edges = Qt.Edges()
        if left:
            edges |= Qt.Edge.LeftEdge
        if right:
            edges |= Qt.Edge.RightEdge
        if bottom:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _cursor_for(self, edges):
        left = bool(edges & Qt.Edge.LeftEdge)
        right = bool(edges & Qt.Edge.RightEdge)
        bottom = bool(edges & Qt.Edge.BottomEdge)
        if left and bottom:
            return Qt.SizeBDiagCursor
        if right and bottom:
            return Qt.SizeFDiagCursor
        if left or right:
            return Qt.SizeHorCursor
        if bottom:
            return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def eventFilter(self, obj, event):
        if self.isVisible():
            t = event.type()
            if t == QEvent.Type.MouseMove:
                edges = self._edge_at(QCursor.pos())
                if edges:
                    if not self._override_cursor_active:
                        QApplication.setOverrideCursor(self._cursor_for(edges))
                        self._override_cursor_active = True
                elif self._override_cursor_active:
                    QApplication.restoreOverrideCursor()
                    self._override_cursor_active = False
            elif t == QEvent.Type.MouseButtonPress and event.button() == Qt.LeftButton:
                edges = self._edge_at(QCursor.pos())
                if edges:
                    if self._override_cursor_active:
                        QApplication.restoreOverrideCursor()
                        self._override_cursor_active = False
                    self.windowHandle().startSystemResize(edges)
                    return True
            elif t == QEvent.Type.Leave:
                if self._override_cursor_active:
                    QApplication.restoreOverrideCursor()
                    self._override_cursor_active = False
        return super().eventFilter(obj, event)
