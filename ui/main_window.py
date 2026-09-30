"""Yuhub 主窗口：极简色块（Pixel-UI）风格 + 自定义标题栏 + 侧栏 + 主题切换。"""

import os
import sys

from PySide6.QtCore import Qt, QEvent, QSettings, QTimer
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
from . import VERSION_LABEL
from .tray import TrayIcon, tray_available
from .widgets import show_toast
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


class NavButton(QPushButton):
    """侧栏导航按钮：左侧一个纯色方块作为功能标识。"""

    def __init__(self, key, icon, text, parent=None):
        super().__init__(parent)
        self.setObjectName("SidebarButton")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._icon = icon
        self._label = text
        self._tint_key = NAV_TINTS.get(key, "tile_1")
        theme.bus.changed.connect(lambda _: self._refresh())
        self.set_expanded(True)

    def _refresh(self):
        color = theme.current().get(self._tint_key, theme.current()["accent"])
        self._dot.setStyleSheet(
            f"background: {color}; border: none; border-radius: 3px;"
        )

    def set_expanded(self, expanded):
        if self.layout() is None:
            lay = QHBoxLayout(self)
            lay.setContentsMargins(14, 0, 12, 0)
            lay.setSpacing(10)
            self._dot = QFrame()
            self._dot.setFixedSize(12, 12)
            lay.addWidget(self._dot, 0, Qt.AlignVCenter)
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
        theme.reload_packs()
        saved_pack = self._settings.value("theme_pack", "")
        if saved_pack and saved_pack in theme.packs():
            theme.set_pack(saved_pack)
        theme.set_theme(theme.resolve_theme(self.theme_setting))

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
