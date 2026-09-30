"""系统托盘：让 Yuhub 关掉窗口后仍驻留在通知区域。

## 为什么菜单要单独套一份样式

主窗口的 QSS 是 `window.setStyleSheet(...)` 设上去的，样式表只会级联到
**该控件的后代**。而托盘菜单是独立顶层弹窗、parent 不指向主窗口，
拿不到那份样式表 → 菜单会退回系统默认外观，在深色主题下白底刺眼、
浅色主题下又和界面完全不搭。

所以这里显式给菜单设一套 QMenu 样式，并挂在主题总线上跟随切换。

## 顺带复习一个坑（v0.5.1 踩过）

全局 QSS 里有 `QWidget { background: transparent }` 这条通配规则，
它会**一起作用到 QMenu**。QMenu 是原生弹窗，背景透明导致它自己的底不绘制，
于是漏出窗口默认黑底，而文字又跟着主题走 → 浅色主题下"近黑文字压黑底"完全看不见。
而且条目必须写在 `QMenu::item` 上：只给 QMenu 设 background，菜单项仍是系统白底。
"""

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import VERSION_LABEL, theme

# 留驻托盘时给一次气泡提示，告诉用户程序没关掉、去哪找
HINT_TITLE = "Yuhub"
HINT_TEXT = "已留在系统托盘，点击托盘图标可恢复窗口"


def tray_available():
    """系统是否支持通知区域。

    极少数环境（精简版系统、部分远程会话）下没有通知区域，
    此时要禁用「关闭到托盘」开关，否则用户点了 ✕ 就真的找不回窗口了。
    """
    try:
        return QSystemTrayIcon.isSystemTrayAvailable()
    except Exception:  # noqa: BLE001
        return False


def menu_qss():
    """托盘菜单样式（与主界面同一套色板，跟随主题）。"""
    p = theme.current()
    return (
        "QMenu {"
        f" background: {p['card_top']};"
        f" border: 1px solid {p['border_strong']};"
        " border-radius: 6px; padding: 5px;"
        f" color: {p['text']};"
        " }"
        "QMenu::item {"
        " background: transparent;"
        f" color: {p['text']};"
        " padding: 7px 28px 7px 14px; border-radius: 4px;"
        " }"
        "QMenu::item:selected {"
        f" background: {p['accent']}; color: {p['accent_text']};"
        " }"
        "QMenu::separator {"
        f" height: 1px; background: {p['border']}; margin: 5px 8px;"
        " }"
    )


class TrayIcon(QSystemTrayIcon):
    """托盘图标 + 右键菜单。

    行为约定（Windows 习惯）：
      单击 / 双击 → 窗口不可见就显示，可见但不在前台就提到前台，
                   已经在前台就收起来（来回切换，省得再找一次菜单）
      右键        → 菜单（显示 / 隐藏 / 退出），由 Qt 自动处理
    """

    def __init__(self, window, icon=None, parent=None):
        super().__init__(icon or QIcon(), parent or window)
        self._window = window
        self._hint_shown = False
        self.setToolTip(f"Yuhub {VERSION_LABEL}")

        # 菜单不挂 parent，避免继承主窗口那些只为窗口布局准备的规则
        self.menu = QMenu()
        self.act_show = self.menu.addAction("显示主界面")
        self.act_hide = self.menu.addAction("隐藏到托盘")
        self.menu.addSeparator()
        self.act_quit = self.menu.addAction("退出 Yuhub")
        self.setContextMenu(self.menu)

        self.act_show.triggered.connect(self.show_window)
        self.act_hide.triggered.connect(self.hide_window)
        self.act_quit.triggered.connect(self._window.request_quit)
        self.activated.connect(self._on_activated)

        # 注意必须连**绑定方法**而不是 lambda：用 lambda 时 Qt 不知道接收者是谁，
        # 对象析构后仍会收到主题信号（v0.5.x 在 DriveRow 上踩过这个）。
        theme.bus.changed.connect(self.refresh_style)
        self.refresh_style()

    def refresh_style(self, *_args):
        """主题切换时重刷菜单配色。"""
        try:
            self.menu.setStyleSheet(menu_qss())
        except RuntimeError:
            pass

    # -------------------------------------------------------------- 行为
    def _on_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if not self._window.isVisible() or self._window.isMinimized():
                self.show_window()
            elif not self._window.isActiveWindow():
                self._window.activate_window()
            else:
                self.hide_window()

    def show_window(self):
        """把窗口显示出来并提到前台。"""
        self._window.activate_window()

    def hide_window(self):
        """收进托盘（并首次给一次气泡提示）。"""
        self._window.hide()
        self.notify_hidden()

    def notify_hidden(self):
        """首次隐藏时提示一次，避免用户以为程序被关掉了。"""
        if self._hint_shown:
            return
        self._hint_shown = True
        try:
            self.showMessage(HINT_TITLE, HINT_TEXT, self.icon(), 4000)
        except Exception:  # noqa: BLE001
            pass
