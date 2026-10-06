"""Yuhub 主窗口：极简色块（Pixel-UI）风格 + 自定义标题栏 + 侧栏 + 主题切换。"""

import os
import sys
import threading

from PySide6.QtCore import (
    Qt, QEvent, QObject, QPoint, QPropertyAnimation, QRect,
    QRectF, QSettings, QTimer, QVariantAnimation, QAbstractAnimation, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QGuiApplication, QIcon, QPainter, QPainterPath, QPixmap,
)
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QCheckBox,
    QStackedWidget,
    QApplication,
    QFrame,
    QButtonGroup,
)

from . import glass
from . import motion
from . import theme
from . import VERSION, VERSION_LABEL
from .tray import TrayIcon, tray_available
from .widgets import kb_focus, show_toast
from .update_ui import UpdateChecker, UpdateAvailableDialog, UpdateProgressDialog
from .pages.home_page import HomePage
from .pages.cleaner_page import CleanerPage
from .pages.memory_page import MemoryPage
from .pages.download_page import DownloadPage
from .pages.lan_page import LanPage
from .pages.share_page import SharePage
from .pages.uninstall_page import UninstallPage
from .pages.accel_page import AccelPage
from .pages.settings_page import SettingsPage

RESIZE_MARGIN = 5
WINDOW_MARGIN = 6           # 圆角窗口四周的透明留白（极简风格：留白收窄）
SIDEBAR_WIDTH = 216
SIDEBAR_COLLAPSED = 76
PILL_RADIUS = 8.0           # 侧栏选中指示条的圆角（玻璃片形状，见 GlassPill）
PILL_INSET_X = 2            # 选中片距按钮左右各内缩多少（右多留 2px 给描边）
PILL_INSET_Y = 1            # 选中片距按钮上下各内缩多少（越小越"厚"）

# ---------------------------------------------------------------------------
# 侧栏按钮的 hover 反馈（v0.15.4beta 起自绘）
# ---------------------------------------------------------------------------
# 为什么不用 QSS 的 `:hover`：**它在这个控件上根本不生效**。实测
# `QStyleOptionButton.state & State_MouseOver` 恒为 False（鼠标确实在按钮上、
# `underMouse()` 为 True、`WA_Hover` 也为 True，就是不给 MouseOver 位），
# 真机截屏 hover 前后逐像素零差异 —— 也就是用户报的「鼠标移上去没有动画」。
# 同一个窗口里，标题栏按钮只要不叫 `SidebarButton` 就是好的，改名成
# `SidebarButton` 立刻变坏，属于 Qt QSS 侧的坑，不深挖。
#
# 于是 hover 反馈改成**自绘**：一层比选中片淡得多的浅蓝液态玻璃膜，
# 从两侧"收着"弹簧铺开（果冻感），同时文字由 dim 提到正常亮度。
#   HOVER_TINT_ALPHA —— 膜的实心度。选中片是 196，这里只给 92：
#                       hover 是"鼠标在这里"，选中是"你在这页"，两者不能一样重。
#   HOVER_RADIUS     —— 与选中片同半径，让两个片是同一套形状语言。
#   HOVER_SPREAD     —— 起点比终点窄的像素数（弹簧铺开的行程）。
HOVER_TINT_ALPHA = 92
HOVER_RADIUS = 8.0
HOVER_SPREAD = 7.0
# 液态玻璃的两个观感参数（v0.15beta 起选中指示条改液态玻璃）：
#   LIQUID_REFRACT    —— 背景快照放大倍率，做"透过厚玻璃看"的透镜位移。
#                        1.05 在 200px 宽的指示条上是边缘偏 5px，看得出来但不
#                        夸张；再大背后的纹理就会明显错位。
#   LIQUID_TINT_ALPHA —— 浅蓝底的实心度。196(77%) 是"够亮、能压住深色侧栏，
#                        又还剩 23% 背景透出来"的平衡点，也是文字对比度
#                        能到 4.5:1 的前提（见 theme._LIQUID_TEXT_T 的说明）。
LIQUID_REFRACT = 1.05
LIQUID_TINT_ALPHA = 196

#: 切页时页面从下方弹入的起始偏移（px）。给 12px 而不是 40px：侧栏导航是
#: "一天几十次"的频次档，位移大了几十次之后就变烦。这一点位移只是让切换
#: "有落点"，不是表演。
PAGE_ENTER_DY = 12

NAV_ITEMS = [
    ("home", "🏠", "首页"),
    ("cleaner", "🧹", "C盘清理"),
    ("memory", "🧠", "内存优化"),
    ("uninstall", "🗑️", "软件卸载"),
    ("download", "⬇️", "多线程下载"),
    ("share", "📡", "屏幕共享"),
    ("lan", "🌐", "异地联机"),
    ("accel", "🚀", "Steam加速"),
    ("settings", "⚙️", "设置中心"),
]

# 侧栏导航的色块标识（极简色块风格的点缀）
NAV_TINTS = {
    "home": "tile_1",
    "cleaner": "tile_3",
    "memory": "tile_7",
    "uninstall": "tile_6",
    "download": "tile_5",
    "share": "tile_8",
    "lan": "tile_2",
    "accel": "tile_5",
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


def _mix_hex(a, b, t):
    """在两个颜色之间线性插值（t=0 取 a、t=1 取 b），返回 #rrggbb。

    用 QColor 解析是为了兼容主题包里可能写的 `rgba(...)` / 颜色名；
    `a` 解析不出来时原样返回（宁可不动，也不要抛异常把绘制打断）。
    """
    ca = QColor(str(a))
    if not ca.isValid():
        return str(a)
    cb = QColor(str(b))
    if not cb.isValid():
        cb = ca
    try:
        k = min(1.0, max(0.0, float(t)))
    except (TypeError, ValueError):
        k = 0.0
    return QColor(
        int(round(ca.red() + (cb.red() - ca.red()) * k)),
        int(round(ca.green() + (cb.green() - ca.green()) * k)),
        int(round(ca.blue() + (cb.blue() - ca.blue()) * k)),
    ).name()


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
        # hover 膜的进度（0=不显示，1=完全铺开）。QSS 的 :hover 在本控件上
        # 无效（见 HOVER_TINT_ALPHA 上方的说明），反馈全靠这个值自绘。
        self._hover_t = 0.0
        # 上一次写进 QLabel 的提亮程度（-1 = 还没写过，保证首次一定生效）
        self._label_tint_k = -1.0
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(motion.HOVER)
        # 弹簧而不是直线：膜"冲过头一点再坐稳"才有果冻感（用户 2026-10-04
        # 确认要 Q 弹手感，且不要强度档位）。用与侧栏选中片**同一条**曲线
        # (CURVE_SPRING，16% 过冲)，两处动作才像同一个系统里的东西。
        # 过冲只会让膜更宽：起点两侧各留 HOVER_SPREAD(7px)，16% 最坏也只吃掉
        # 1.1px，绝不会冒出按钮边界（`_hover_rect` 还有一道最小尺寸兜底）。
        self._hover_anim.setEasingCurve(motion.curve(motion.CURVE_SPRING))
        self._hover_anim.valueChanged.connect(self._on_hover_value)
        theme.bus.changed.connect(lambda _: self._refresh())
        # 选中态变化时也要刷新（玻璃贴片选中时描边换成主题色）
        self.toggled.connect(lambda *_: self._refresh())
        self.set_expanded(True)

    # -- hover 反馈（自绘） --------------------------------------------------
    def enterEvent(self, event):
        super().enterEvent(event)
        self._animate_hover(1.0)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._animate_hover(0.0)

    def changeEvent(self, event):
        """窗口失去焦点时把 hover 淡出。

        不处理的话会残留一层膜：鼠标停在按钮上时 Alt+Tab 切走，Qt 不会再发
        Leave（鼠标没动），回到窗口时鼠标早就不在按钮上了，膜就一直挂在
        那一行上。
        """
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange
                and not self.isActiveWindow() and self._hover_t > 0.0):
            self._animate_hover(0.0)

    def _animate_hover(self, target):
        running = (self._hover_anim.state()
                   == QAbstractAnimation.State.Running)
        if running:
            # 已经在往同一个目标跑就别重启 —— 重启会让弹簧从当前位置
            # 重新起步，连续进出时看着像"抖一下"。
            try:
                if abs(float(self._hover_anim.endValue()) - target) < 0.001:
                    return
            except (TypeError, ValueError):
                pass
        elif abs(self._hover_t - target) < 0.002:
            return                        # 已经在位，别白跑一次动画
        self._hover_anim.stop()
        self._hover_anim.setStartValue(float(self._hover_t))
        self._hover_anim.setEndValue(float(target))
        self._hover_anim.start()

    def _on_hover_value(self, value):
        self._hover_t = float(value)
        self._tint_labels()
        self.update()

    def _tint_labels(self):
        """hover 时把文字从 dim 提到正常亮度。

        文字是 QLabel 子控件，颜色只能自己给 —— QSS 的
        `QPushButton#SidebarButton:hover { color: $text }` 改的是按钮自己的
        文字（本控件 `setText("")`，没有文字），根本轮不到子控件。

        k 回到 0 时**把 color 整个去掉**（而不是设成某个"接近原来的"颜色）：
        不带 color 的样式表会让 QLabel 回落到调色板，跟没 hover 过一样，
        不会出现"hover 一次之后文字颜色就永久变了"。
        """
        if not hasattr(self, "_text_lbl"):
            return
        k = min(1.0, max(0.0, self._hover_t))
        # 变化太小就不重设样式表 —— setStyleSheet 会触发一次 repolish，
        # 动画期间每帧都设一次纯属白烧。
        if abs(k - self._label_tint_k) < 0.03 and not (
                k <= 0.001 and self._label_tint_k > 0.0):
            return
        self._label_tint_k = k
        base = "font-size: 13px; background: transparent;"
        if k <= 0.001:
            self._text_lbl.setStyleSheet(base)
            return
        cur = theme.current()
        color = _mix_hex(cur.get("text_dim", "#8a90a0"),
                         cur.get("text", "#f2f4f8"), k)
        self._text_lbl.setStyleSheet(base + " color: %s;" % color)

    def paintEvent(self, event):
        super().paintEvent(event)
        t = self._hover_t
        # 选中项不画：那一行已经有液态玻璃选中片了，再叠一层是同色加重，
        # 反而看不清"当前在哪一页"。
        if t <= 0.004 or self.isChecked():
            return
        r = self._hover_rect(t)
        if r is None:
            return
        liq = theme.liquid_palette()
        # 弹簧曲线会过冲到 1.08：膜的位置可以让它冲，**透明度不能** ——
        # alpha 超过 255 会被 rgba() 截断成奇怪的颜色。
        k = min(1.0, t)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        glass.paint_liquid_glass(
            p, r, radius=HOVER_RADIUS,
            tint=liq["tint"], edge=liq["edge"],
            tint_alpha=int(HOVER_TINT_ALPHA * k),
            gloss_alpha=int(72 * k),
            top_alpha=int(112 * k),
            border_alpha=int(98 * k),
            # 外发光 / 光晕一律给 0：那是"选中片"的重量，hover 只要一层薄膜。
            glow_alpha=0, glow_thickness=0,
            chroma_alpha=int(38 * k),
            shade_alpha=int(24 * k), shade_thickness=8)
        p.end()

    def _hover_rect(self, t):
        """hover 膜的形状：内缩与选中片一致，起点从两侧再收 HOVER_SPREAD。"""
        g = QRectF(self.rect())
        spread = HOVER_SPREAD * (1.0 - float(t))
        r = g.adjusted(PILL_INSET_X + spread, PILL_INSET_Y,
                       -(PILL_INSET_X + 2.0 + spread), -PILL_INSET_Y)
        if r.width() <= 6.0 or r.height() <= 4.0:
            return None
        return r

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
        # 主题换了 → 文字提亮的基准色也跟着换（`_tint_labels` 会重算）
        self._label_tint_k = -1.0
        self._tint_labels()

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


def _panel_corner_radius(widget, palette):
    """玻璃面板该用多大的半径去圆自己的外角。

    外壳（#AppFrame）的圆角是 QSS 画的，**Qt 的 QSS 圆角不会裁剪子控件**，
    所以贴着窗口边角的玻璃面板（标题栏、侧栏）必须自己把外角圆掉，
    否则会拿直角把外壳的圆角"补方"——用户看到的就是「软件边角凸出来的角」。

    QSS 的 px 会随设备像素比缩放（实测渲染出来的半径比声明的 8px 小），
    这个值没法反查，所以这里取主题声明值再收 1px（面板贴在外壳 1px 边框里）。
    宁可多裁一点、露出一点外壳底色，也绝不让玻璃凸到圆角外面。
    """
    win = widget.window()
    try:
        if win is not None and win.isMaximized():
            return 0            # 最大化时外壳圆角归零，面板跟着不圆
    except (RuntimeError, AttributeError):
        pass
    r = glass.window_radius(palette)
    return max(0, r - 1) if r > 0 else 0


class GlassTitleBar(QFrame):
    """毛玻璃标题栏。

    和侧栏同一套取景 / 模糊 / 上色（ui/glass.paint_glass_panel），只是更宽更扁，
    所以光泽给得略强一点——一条横贯窗口的玻璃带，是整窗毛玻璃最显眼的那一笔。
    """

    def __init__(self, backdrop, parent=None):
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self._backdrop = backdrop
        self._src = glass.BackdropSource()
        theme.bus.changed.connect(lambda _: self.refresh_glass())

    def refresh_glass(self):
        self._src.invalidate()
        self.update()

    def paintEvent(self, event):
        cur = theme.current()
        tint, gloss, dim = glass.glass_params(cur)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        glass.paint_glass_panel(
            p, self, self._backdrop(), self._src,
            blur=9, tint_alpha=tint, gloss_alpha=gloss, dim_alpha=dim,
            fallback=cur.get("titlebar_bg", "#12141a"),
            # 标题栏横跨窗口顶边，左右两个外角要和外壳一起圆
            corners=("tl", "tr"),
            corner_radius=_panel_corner_radius(self, cur))
        p.end()


# 标题栏版本号：**故意不用玻璃贴片**。
#
# v0.10beta 那版给它单独画了一块玻璃（自己的底色 + 描边 + 投影），本意是
# 「让人看出这是块玻璃」，但贴片终究是浮在标题栏上的一个圆角矩形，边界一眼
# 可见——用户反馈就是「不想要顶部版本号有一个额外的框」。
# 现在它就是普通 QLabel：不画任何底色，直接透出标题栏本身的毛玻璃，和标题栏
# 里其它文字一样"长在玻璃上"，没有任何框；而"那一块有毛玻璃"依然成立，因为
# 标题栏整条自己就是玻璃。
#
# ⚠️ QSS 里 `QLabel#AppSubtitle` 必须保持 `background: transparent`：QLabel::
#    paintEvent 一进来就 drawFrame()，只要刷了底色就把底下的标题栏玻璃盖住，
#    又变成"贴了一张纸"（sky glass 的 surface_hover 是 rgba(255,255,255,225)，
#    几乎全白）。


class TitleBar(GlassTitleBar):
    """自定义标题栏：拖动移动、双击最大化、侧栏折叠、主题切换、窗口按钮。"""

    def __init__(self, window):
        super().__init__(lambda: getattr(window, "backdrop", None))
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
        self.version_label = sub
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


class BackdropLayer(QFrame):
    """窗口底部的静态纹理层（毛玻璃的「可模糊内容」来源）。

    毛玻璃成立的前提是背后有东西可糊：玻璃盖在纯色上，模糊前后都是纯色。
    所以窗口最底下铺这一层——极淡方格 + 两团柔光（见 ui/glass.py），
    所有玻璃控件（侧栏、选中指示条、开关）都从这一层取背景。

    纹理是静态的，缓存成一张 QPixmap；只有尺寸变化 / 换主题才重画。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BackdropLayer")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._pm = None
        self._radius = 0
        # 背板版本号：每次重绘 +1。玻璃控件把它当缓存键的一部分，
        # 于是背板一变（换主题 / 尺寸变化）所有模糊缓存自动失效。
        self.version = 0
        # 让出 AppFrame 那一圈 1px 边框：子控件能画到父控件的边框上，
        # 不留出这 1px 的话背板会把窗口描边整个盖掉。
        self._inset = 1
        if parent is not None:
            parent.installEventFilter(self)
        theme.bus.changed.connect(lambda _: self.rebuild())

    def set_radius(self, radius):
        """跟随 AppFrame 的圆角。AppFrame 的 border-radius 不会裁剪子控件，
        直角背板会把圆角"补方"，所以这里自己在绘制时裁圆角。

        色板里 window_radius 是 QSS 用的字符串（如 '8px'），这里容错解析；
        再减去 insetself 让出的边框宽度，保证圆角弧线与外框贴合。
        """
        try:
            radius = int(str(radius).replace("px", "").strip())
        except (TypeError, ValueError):
            radius = 0
        radius = max(0, radius - self._inset)
        if radius != self._radius:
            self._radius = radius
            self.update()

    def eventFilter(self, obj, event):
        # 父壳体（AppFrame）大小变化时跟着铺满（同样让出 1px 边框）
        if obj is self.parent() and event.type() == QEvent.Resize:
            self.setGeometry(self.parent().rect().adjusted(
                self._inset, self._inset, -self._inset, -self._inset))
        return super().eventFilter(obj, event)

    def rebuild(self):
        self.version += 1
        self._pm = None
        self.update()

    def resizeEvent(self, event):
        self.version += 1
        self._pm = None
        super().resizeEvent(event)

    def paintEvent(self, event):
        if self.size().isEmpty():
            return
        if self._pm is None or self._pm.size() != self.size():
            pm = QPixmap(self.size())
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            glass.paint_backdrop(p, self.size(), theme.current())
            p.end()
            self._pm = pm
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._radius > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), self._radius, self._radius)
            p.setClipPath(path)
        p.drawPixmap(0, 0, self._pm)
        p.end()


class GlassSidebar(QFrame):
    """毛玻璃侧边栏：背后纹理先模糊，再叠玻璃面 + 右缘高光。

    背景取的是 `BackdropLayer`（不含自己），所以不会出现"把自己糊进去"
    的递归残影。
    """

    def __init__(self, backdrop, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self._backdrop = backdrop
        self._src = glass.BackdropSource()
        theme.bus.changed.connect(lambda _: self.refresh_glass())

    def refresh_glass(self):
        self._src.invalidate()
        self.update()

    def paintEvent(self, event):
        r = self.rect()
        if r.width() <= 0 or r.height() <= 0:
            return
        cur = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 1) 背后纹理的模糊版（毛玻璃的本体）+ 玻璃面色纱
        tint, gloss, dim = glass.glass_params(cur)
        glass.paint_glass_panel(
            p, self, self._backdrop(), self._src,
            blur=10, tint_alpha=tint, gloss_alpha=gloss, dim_alpha=dim,
            fallback=cur.get("sidebar_bg", "#12141a"),
            # 侧栏贴窗口左边、通到底：只有左下角是窗口的外角（左上角在标题栏下面）
            corners=("bl",),
            corner_radius=_panel_corner_radius(self, cur))

        # 2) 右缘分隔：一条亮线 + 一条暗线，做出"玻璃片边缘"的厚度感
        p.setPen(glass.rgba("#ffffff", 74))
        p.drawLine(r.width() - 2, 0, r.width() - 2, r.height())
        p.setPen(glass.rgba("#000000", 76))
        p.drawLine(r.width() - 1, 0, r.width() - 1, r.height())
        p.end()


class GlassPill(QFrame):
    """侧栏选中项的玻璃指示条。

    它是侧栏的**子控件**、被 `stackUnder` 压到导航按钮下面，所以按钮文字
    压在半透明玻璃上；切换页面时用 geometry 动画从旧位置滑到新位置，
    这是「功能栏切换」那一处毛玻璃的重点。

    单开一个控件（而不是在侧栏 paintEvent 里直接画）是为了能做动画：
    QPropertyAnimation 直接驱动 geometry，比每帧手算插值省事且更顺。
    """

    def __init__(self, backdrop, parent=None):
        super().__init__(parent)
        self.setObjectName("GlassPill")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._backdrop = backdrop
        self._src = glass.BackdropSource()
        self._relayout = None
        self._anim = QPropertyAnimation(self, b"geometry", self)
        # 面板在两个位置之间挪位 —— 用**弹簧曲线**：冲过目标 16%、弹回一点、
        # 停稳。直线滑过去像"被搬过去"，弹簧才像"有质量的东西撞上去"（用户
        # 要的"Q弹果冻感"）。过冲体现在 x 上：起点终点宽度相同，插值里
        # width 恒定，只有 x 会短暂越过目标再回来。
        # 时长见动效令牌（motion.PILL = 300ms，过冲峰值落在 147ms）。
        self._anim.setDuration(motion.PILL)
        self._anim.setEasingCurve(motion.curve(motion.CURVE_SPRING))
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    def set_relayout(self, fn):
        """注入「目标矩形从哪来」。布局就绪/侧栏尺寸变化时用它重算位置。"""
        self._relayout = fn

    def eventFilter(self, obj, event):
        # 侧栏尺寸变化（展开/折叠、窗口缩放）后按钮位置会变，指示条要跟上。
        # 这一处必须"瞬时落位"不做动画：宽度已经在变了，再叠加滑动会打架。
        if obj is self.parent() and event.type() == QEvent.Resize:
            self.refresh_geometry(animate=False)
        return super().eventFilter(obj, event)

    def refresh_geometry(self, animate=True):
        if self._relayout is None:
            return
        rect = self._relayout()
        if rect is None:
            self.hide()
            return
        self.slide_to(rect, animate=animate)

    def refresh_glass(self):
        self._src.invalidate()
        self.update()

    def slide_to(self, rect, animate=True):
        """滑到目标矩形。首次出现（还没有几何）时不动画，直接落位。"""
        self._anim.stop()
        if not animate or self.geometry().width() <= 0:
            self.setGeometry(rect)
            self.show()
            self.update()
            return
        if self.geometry() == rect:
            self.show()
            return
        self.show()
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(rect)
        self._anim.start()

    def paintEvent(self, event):
        r = self.rect()
        if r.width() <= 2 or r.height() <= 2:
            return
        r = self.rect()
        if r.width() <= 2 or r.height() <= 2:
            return
        liq = theme.liquid_palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        face = r.adjusted(1, 1, -2, -2)
        face_path = glass.rounded_path(QRectF(face), PILL_RADIUS)

        # 0) 先把整块绘制裁到"圆角玻璃片"的形状里。
        #    血泪：这个控件原来不裁剪，而背景快照是**整块矩形** drawPixmap 上去的，
        #    于是圆角之外那四个小三角没被玻璃面盖住，露出的是"没染色的裸背板"
        #    ——用户看到的就是「选中效果蓝色四角周围没有毛玻璃」。裁进 path 之后，
        #    角上什么都不画，底下侧栏自己的玻璃自然透出来，四角就接上了。
        p.setClipPath(face_path, Qt.IntersectClip)

        # 1) 背后纹理的模糊版 + 轻微折射（透镜）
        snap = None
        bd = self._backdrop()
        if bd is not None:
            tl = glass.widget_origin(self, bd)
            snap = self._src.snapshot(bd, QRect(tl, self.size()), radius=12,
                                      stamp=getattr(bd, "version", 0))
        glass.draw_snapshot(p, r, snap, refract=LIQUID_REFRACT)

        # 2) 浅蓝液态玻璃面。
        #    v0.15beta：用户要求「选中的特效不要蓝色，要浅蓝的液态玻璃」。
        #    原来是往模糊背景上糊一层 accent 深蓝薄纱（matte=accent），现在整块
        #    换成液态玻璃 —— 浅蓝底 + 强对角反光 + 14px 内侧光带 + 左右色散边。
        #    可以理解为"毛玻璃换成了更厚、会掰光的那块玻璃"。
        glass.paint_liquid_glass(
            p, face, radius=PILL_RADIUS,
            tint=liq["tint"], edge=liq["edge"],
            tint_alpha=LIQUID_TINT_ALPHA,
            gloss_alpha=128, top_alpha=200, border_alpha=152,
            glow_alpha=120, glow_thickness=14,
            chroma_alpha=88, shade_alpha=66, shade_thickness=10)
        # 3) 左缘**不再**画实心 accent 条：浅蓝玻璃自己靠"上/左光带 + 左缘
        #    青色散"指示方向，再压一根深蓝实心条就回到用户否掉的那个样子了。
        p.end()


class _PageHost(QWidget):
    """页面的包装层 —— 让「切页位移」能动起来。

    为什么必须多这一层：`QStackedWidget` 用 `QStackedLayout` 管理子控件，
    布局每次 activate 都会把当前页的 geometry 设回 (0, 0, w, h)。**直接动
    页面自己的 pos，位移会被布局重置** —— 现象是"动画压根没跑"，而代码
    看起来完全正确（这是本项目在毛玻璃那轮踩过的同类坑：Qt 里凡是"布局管着
    的几何"，手动改都只活到下一次 activate）。

    所以加一层：host 由 QStackedLayout 管（老老实实待在 0,0），页面放进 host
    内部的布局里铺满。切页时动 **host 的 pos**，就绕开了布局那关。

    `sizeHint` 不用转发：host 内部有真布局（QVBoxLayout），页面参与布局计算，
    QStackedWidget 的最小尺寸自然会把它算进去。
    """

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(page)


class _LazyPages(dict):
    """惰性页面表：**首次被访问**时才真正构造那一页。

    为什么：九个页面在启动时全量构造要 ~21MB 常驻内存 + ~110ms 构建时间，
    而用户一次只看一页。改成惰性后启动只建首页（首屏要显示的那一页），
    其余等你点进去才建 —— 单页构建 3~20ms，被 300ms 的切页动画完全盖住，
    感知不到；没访问过的页面则一分内存都不占。

    做成 ``dict`` 子类而不是工厂函数，是为了**所有既有调用点都不用改**：
    `switch_page` 的 `self._pages[key]`、自检里的 `win._pages["lan"]`
    都会走 ``__missing__`` 自动触发构造。
    """

    def __init__(self, factories, on_create=None):
        super().__init__()
        self._factories = dict(factories)
        self._on_create = on_create

    def __missing__(self, key):
        factory = self._factories.pop(key, None)
        if factory is None:
            raise KeyError(key)
        page = factory()
        dict.__setitem__(self, key, page)
        if self._on_create is not None:
            self._on_create(key, page)
        return page

    def get(self, key, default=None):
        """``dict.get`` 不会走 ``__missing__`` —— 这里补齐，行为与 ``[]`` 一致。"""
        try:
            return self[key]
        except KeyError:
            return default

    def knows(self, key):
        """这个键是否已被构造**或**还有工厂可以构造（判断"页面存不存在"用）。"""
        return key in self._factories or dict.__contains__(self, key)


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
        self._hosts = {}                # key -> _PageHost（页面外面那层包装）
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

        # 圆角外壳。QSS 里 AppFrame 仍是实心底（保证不透明、圆角正确），
        # 玻璃只是它上面的一层「局部毛玻璃」，不改变窗口整体的不透明性。
        self.frame = QFrame()
        self.frame.setObjectName("AppFrame")
        self.frame.setProperty("rounded", True)
        self._outer.addWidget(self.frame)

        # 背板纹理层：铺满外壳、压在最底，作为所有玻璃控件的"可模糊内容"。
        # 手动定位（不进布局），跟随外壳 resize。
        self.backdrop = BackdropLayer(self.frame)
        self.backdrop.setGeometry(self.frame.rect().adjusted(1, 1, -1, -1))
        self.backdrop.set_radius(theme.current().get("window_radius", 8))

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
        # 侧栏换成毛玻璃：背后纹理先模糊、再叠玻璃面与右缘高光
        sidebar = GlassSidebar(lambda: getattr(self, "backdrop", None))
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

        # 选中项的玻璃指示条。它是侧栏的子控件、但**不进布局**（不占位），
        # 靠 stackUnder 压到导航按钮下面，按钮文字就压在半透明玻璃上。
        #
        # 注意 stackUnder 只保证"在指定控件之下"，逐个调用的话只有最后一次
        # 生效——而最后一次是最后一个按钮（在最上层），结果指示条跑到按钮
        # 上面去了，把选中项的文字整个盖住（玻璃越实越明显）。按钮是按创建
        # 顺序叠起来的，第一个在最底，所以只需要压到第一个按钮下面即可。
        self._nav_pill = GlassPill(lambda: getattr(self, "backdrop", None), sidebar)
        self._nav_pill.set_relayout(self._nav_pill_rect)
        first_btn = next(iter(self._nav_buttons.values()), None)
        if first_btn is not None:
            self._nav_pill.stackUnder(first_btn)

        return sidebar

    def _nav_pill_rect(self):
        """当前导航项对应的指示条矩形（相对侧栏），给不齐时返回 None。"""
        btn = self._nav_buttons.get(getattr(self, "_current_nav", "home"))
        if btn is None or btn.width() <= 8 or btn.height() <= 8:
            return None
        g = btn.geometry()
        # 内缩量刻意很小：用户反馈「左侧工具栏的蓝色选中效果太细了」，所以
        # 选中片要尽量撑满整行（只留 1px 上下、2px 左右给高光边和圆角），
        # 而不是像原来那样上下各让 3px、整体显得一片薄薄的漂浮条。
        return QRect(g.x() + PILL_INSET_X, g.y() + PILL_INSET_Y,
                     max(4, g.width() - PILL_INSET_X - PILL_INSET_X - 2),
                     max(4, g.height() - PILL_INSET_Y - PILL_INSET_Y))

    def _build_pages(self):
        """登记九个页面 —— 但**此刻一页都不建**（见 _LazyPages）。

        只有首页会被立刻构造（首屏就要显示，而且它的「快速跳转」信号要接）。
        其余页面在第一次切换过去时由 _LazyPages.__missing__ 现场构造，
        构造完由 _on_page_created 包上 host 挂进堆栈。
        """
        self._pages = _LazyPages({
            "home": lambda: HomePage(notify=self.notify),
            "cleaner": lambda: CleanerPage(notify=self.notify),
            "memory": lambda: MemoryPage(notify=self.notify),
            "uninstall": lambda: UninstallPage(notify=self.notify),
            "download": lambda: DownloadPage(notify=self.notify),
            "share": lambda: SharePage(notify=self.notify),
            "lan": lambda: LanPage(notify=self.notify),
            "accel": lambda: AccelPage(notify=self.notify),
            "settings": lambda: SettingsPage(
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
        }, on_create=self._on_page_created)

        # 首页立即就绪（_build_ui 末尾会 switch_page("home")）
        self._pages["home"].navigate.connect(self.switch_page)
        self._apply_keyboard_focus()

    def _on_page_created(self, key, page):
        """某个页面**第一次**被构造出来时：包 host、挂堆栈、补键盘焦点环。"""
        host = _PageHost(page)
        self._hosts[key] = host
        self.stack.addWidget(host)
        # 键盘焦点环：只有 Tab 进来才拿焦点（否则鼠标点过的按钮会一直亮着
        # 主题色描边，看起来像"选中了"）。窗口级那次遍历覆盖不到尚未创建的
        # 页面，所以每建一页就补一次 —— 限定在本页内，代价可以忽略。
        for cls in (QPushButton, QCheckBox):
            for w in page.findChildren(cls):
                kb_focus(w)

    def _apply_keyboard_focus(self):
        """全窗按钮 / 复选框统一改成「只有 Tab 进来才拿焦点」。

        为键盘焦点环服务（见 theme_packs.INTERACTION_QSS 与 widgets.kb_focus）：
        不加这一层的话，鼠标点过的按钮会一直亮着主题色描边，看起来像"选中了"。

        放在窗口级做一次遍历，是因为各页面里还有大量就地 new 出来的按钮
        （MiniButton、标题栏三个窗口按钮、设置页的一堆开关行……），
        逐个改容易漏；这里一次全包住，新增页面也自动生效。
        """
        for w in self.findChildren(QPushButton):
            kb_focus(w)
        for w in self.findChildren(QCheckBox):
            kb_focus(w)

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
        # 布局到这一步才真正算出按钮几何，玻璃指示条在这时补一次落位
        QTimer.singleShot(0, self._sync_nav_pill)
        # 开机自启静默进托盘期间发现的更新，等窗口真打开时再提示
        self.show_update_if_pending()

    def changeEvent(self, event):
        """最大化 / 还原时同步外壳圆角。

        双击标题栏走 toggle_max()，但 Win+↑ / 拖到屏幕顶部这类系统手势
        只发 WindowStateChange，不经过它——不在这里补一刀的话，最大化后
        外壳仍带着 8px 圆角、背板也还裁着圆角，四个角会露出没画到的缝隙。
        """
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, lambda: self._set_rounded(
                not self.isMaximized()))

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
        # 注意判据是 knows() 而不是 `key in self._pages` —— 惰性页面表里
        # 尚未构造的键 `in` 是 False，用它判断会把「第一次切过去」直接吞掉，
        # 页面永远建不出来。knows() 同时认"已构造"和"还有工厂"。
        if not self._pages.knows(key):
            return
        page = self._pages[key]         # ← 首次访问在这里现场构造
        host = self._hosts.get(key)
        if (key == getattr(self, "_current_nav", None)
                and host is not None
                and host is self.stack.currentWidget()):
            # 重复点当前页：只补齐选中态，不重放动画（连点会闪）。
            # 注意判据里必须带上 _current_nav —— 首次进入时 QStackedWidget
            # 的 currentWidget() 已经是最先 addWidget 的那一页（首页），
            # 只看 currentWidget() 会把启动这一次也吞掉，
            # 结果是"首页按钮不高亮、首页的 on_shown 不触发"。
            if key in self._nav_buttons:
                self._nav_buttons[key].setChecked(True)
            return
        self.stack.setCurrentWidget(host if host is not None else page)
        self._current_nav = key
        if key in self._nav_buttons:
            self._nav_buttons[key].setChecked(True)
        # 玻璃指示条弹到新位置（切换动画就发生在这里）
        pill = getattr(self, "_nav_pill", None)
        if pill is not None:
            pill.refresh_geometry(animate=True)
        self._animate_page_in(host, page)
        # 通知页面已切到前台（用于惰性初始化 / 自动刷新）
        if hasattr(page, "on_shown"):
            try:
                page.on_shown()
            except Exception:
                pass

    def _animate_page_in(self, host, page):
        """页面切换：整体从下方弹入（轻弹）+ 页面淡入。

        位移只给 PAGE_ENTER_DY(12px)、过冲只 8%（CURVE_SPRING_SOFT）：侧栏
        导航是"一天几十次"的频次档，按 Emil 的动效决策框架应当克制。真正承担
        "我现在在哪一页"这个空间信息的是侧栏那条**弹簧**指示条；内容再大幅晃动
        就是重复表达，几十次之后就变烦了。这一点位移只是让切换"有落点"。

        两个必须做对的点：
          1) 淡入要在**同步**阶段就压到 0。否则 setCurrentWidget 之后那一帧
             页面是全不透明的 —— 用户先看到它整个出现、再看到它淡入，像是闪了
             一下。
          2) 位移必须**延后一拍**再起。QStackedLayout 在 setCurrentWidget 时
             投递了 LayoutRequest，它会在下一轮事件处理里把 host 的 geometry
             设回 (0,0) —— 当场 move 出去的位移会被它覆盖掉。

        一个必须收尾的细节：QGraphicsOpacityEffect 会让整棵子树每帧先渲染到
        离屏缓冲，**装上去就一直在收费**。动画一结束必须摘掉，否则页面日常重绘
        （内存页的曲线、联机页的日志）都会被白扣一笔。
        """
        if motion.reduced():
            return
        try:
            motion.fade_effect(page).setOpacity(0.0)
        except RuntimeError:
            return
        QTimer.singleShot(0, lambda: self._slide_page_in(host, page))

    def _slide_page_in(self, host, page):
        """延后一拍真正起动画（这时布局已经把 host 摆好了）。"""
        if page is None or host is None:
            return
        try:
            anim = motion.fade(page, 1.0, motion.NAV,
                               easing=motion.CURVE_OUT)
            slide = QPropertyAnimation(host, b"pos", host)
            slide.setDuration(motion.PAGE_IN)
            slide.setEasingCurve(motion.curve(motion.CURVE_SPRING_SOFT))
            slide.setStartValue(QPoint(0, PAGE_ENTER_DY))
            slide.setEndValue(QPoint(0, 0))
            host.move(0, PAGE_ENTER_DY)
            slide.start()
            # 两个动画都要保引用：QPropertyAnimation 被 GC 会静默停在半路
            self._page_fade = anim
            self._page_slide = slide
            anim.finished.connect(
                lambda a=anim, p=page: QTimer.singleShot(
                    0, lambda: self._end_page_fade(p, a)))
        except RuntimeError:
            return

    def _end_page_fade(self, page, anim):
        """淡入结束：先停掉并释放动画，再摘掉特效。

        顺序不能反：动画的 target 就是那个特效对象，特效先被删的话，
        动画下一次 tick 会碰到已析构的 C++ 对象（本项目在 Toast 上已经
        抓过三次同类崩溃）。
        """
        try:
            if anim is not None:
                anim.stop()
                anim.deleteLater()
            if page is not None:
                page.setGraphicsEffect(None)
        except RuntimeError:
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
        # 宽度刚改，布局要等这一轮事件处理完才重算；延后一拍让指示条
        # 落在按钮重排后的真实位置上（否则会停在旧坐标上）。
        QTimer.singleShot(0, self._sync_nav_pill)
        for btn in self._nav_buttons.values():
            btn.repaint()

    def _sync_nav_pill(self):
        pill = getattr(self, "_nav_pill", None)
        if pill is not None:
            pill.refresh_geometry(animate=False)

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
        # 毛玻璃背板跟着换色（浅/深主题的网格与柔光强度不同）
        if hasattr(self, "backdrop"):
            self.backdrop.set_radius(
                theme.current().get("window_radius", 8)
                if not self.isMaximized() else 0)
            self.backdrop.rebuild()

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
        # 背板自己裁圆角：AppFrame 的 border-radius 不会裁子控件，
        # 最大化时圆角为 0 才不会在四个角留出没画到的透明区。
        if hasattr(self, "backdrop"):
            self.backdrop.set_radius(
                theme.current().get("window_radius", 8) if flag else 0)

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
        # ⚠️ 先挡掉"光标不在本窗口上"的情况：QComboBox 的下拉列表、右键菜单
        # 都是**独立顶层窗口**，会伸到主窗口底边之外（下拉列表尤其明显：列表
        # 从 combo 下方铺下来，最后几项落在 frameGeometry 的下边缘带里）。
        # 若只用 frameGeometry 判边，这些项会被判成"下边缘"：鼠标一移过去就
        # 变成上下缩放光标，左键按下还被 startSystemResize **吞掉** ——
        # 用户看到的就是"最后一个选项点不动、鼠标变成上下箭头"。
        # topLevelAt 取到的不是主窗口 ⇒ 光标在别的顶层窗口上，不参与边缘缩放。
        try:
            top = QApplication.topLevelAt(global_pos)
        except Exception:
            top = None
        if top is not None and top is not self:
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
