"""Yuhub 通用 UI 组件：开关、卡片、分段选择器、Toast、按钮等。"""

from PySide6.QtCore import (
    Qt,
    Signal,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QGraphicsOpacityEffect,
)

from . import glass
from . import theme


class ToggleSwitch(QAbstractButton):
    """自定义开关控件（椭圆形轨道 + 圆形滑块），带滑动动画，随主题自动换色。"""

    KNOB_MARGIN = 3.0       # 滑块与轨道内缘的间距（上下左右一致 → 正好居中）

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._offset = 1.0 if checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.valueChanged.connect(self._on_anim)
        self.toggled.connect(self._on_toggled)
        theme.bus.changed.connect(lambda _: self.update())

    # -- 形状 -----------------------------------------------------------------
    # 抽成方法而不是就地写常量，是为了让自检能断言"轨道是椭圆、滑块是圆"
    # （2026-10-03：用户要求把开关从"小圆角方块"改成椭圆形 + 圆形滑块）。
    def track_radius(self):
        """轨道圆角 = 高度一半 → 左右两端是半圆，整体成椭圆形（胶囊）。"""
        return self.height() / 2.0

    def knob_rect(self, offset=None):
        """滑块的方形外接框。边长 = 高度 - 2×间距，于是滑块正好居中；
        配上 `knob_radius()` 就是个正圆。"""
        t = self._offset if offset is None else float(offset)
        d = max(1.0, self.height() - 2.0 * self.KNOB_MARGIN)
        travel = max(0.0, self.width() - 2.0 * self.KNOB_MARGIN - d)
        return QRectF(self.KNOB_MARGIN + t * travel, self.KNOB_MARGIN, d, d)

    def knob_radius(self):
        """滑块圆角 = 半径 → 正圆。"""
        return self.knob_rect().width() / 2.0

    def _on_toggled(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _on_anim(self, value):
        self._offset = float(value)
        self.update()

    def paintEvent(self, event):
        """毛玻璃开关。

        视觉分三层（对齐参考 CSS 的液态玻璃写法，见 ui/glass.py 顶部注释）：
          1) 轨道底色：关态是沉底色，开态是主题色，两态之间按滑动进度插值；
             形状是**椭圆形**（圆角 = 高度一半），左右两端是半圆；
          2) 玻璃光泽：45° 对角反光 + 顶部高光线 + 1px 亮边。
             **开态明显强于关态**——这一处就是「功能开启」的毛玻璃重点：
             点亮时整块轨道像被光穿过，而不是简单换个颜色；
          3) 滑块：白色**正圆**，带上亮下暗的竖向渐变 + 细边 + 下方淡影，
             做出"一颗厚玻璃珠"的立体感。
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = theme.current()
        off_bg = QColor(c.get("toggle_off", c["surface_sunken"]))
        on_bg = QColor(c["accent"])
        t = max(0.0, min(1.0, self._offset))
        col = QColor(
            int(off_bg.red() + (on_bg.red() - off_bg.red()) * t),
            int(off_bg.green() + (on_bg.green() - off_bg.green()) * t),
            int(off_bg.blue() + (on_bg.blue() - off_bg.blue()) * t),
        )

        w, h = self.width(), self.height()
        radius = self.track_radius()

        # ---- 1) 轨道底色（椭圆：两端半圆）----
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)

        # ---- 2) 玻璃光泽：开态直接拉满 ----
        glass.paint_glass(
            p, QRect(1, 1, w - 2, h - 2), radius=max(0.5, radius - 0.5),
            tint="#ffffff", tint_alpha=0,          # 底色已铺，这里只叠光
            gloss_alpha=int(34 + 96 * t),           # 对角反光
            top_alpha=int(52 + 104 * t),            # 顶部高光线
            border_alpha=int(46 + 84 * t),          # 亮边
            inner_alpha=0,
            matte=None,
        )

        # 边框：关态是硬边框，开态被玻璃亮边吃掉，只留一层淡淡的锁边
        border = QColor(c["accent"] if t > 0.5 else c["border_strong"])
        border.setAlpha(int(140 + 60 * t))
        p.setPen(border)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), radius, radius)

        # ---- 3) 滑块：白色正圆 + 竖向渐变 + 细边 + 淡影 ----
        kr = self.knob_rect()
        kr_radius = kr.width() / 2.0
        glass.paint_soft_shadow(p, kr, radius=kr_radius, offset=1, spread=1,
                                alpha=52)

        grad = QLinearGradient(kr.topLeft(), kr.bottomLeft())
        grad.setColorAt(0.0, QColor("#ffffff"))
        grad.setColorAt(1.0, QColor("#e9edf3"))
        p.setPen(glass.rgba("#ffffff", 110))
        p.setBrush(grad)
        p.drawRoundedRect(kr, kr_radius, kr_radius)
        p.end()


class FeatureCard(QFrame):
    """首页可点击的功能卡片（极简色块：左侧纯色方块标识 + 文字）。"""

    clicked = Signal()

    def __init__(self, icon, title, desc, tint_key="tile_1", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setProperty("clickable", True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(118)
        self.setMinimumWidth(230)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        # 纯色方块：极简色块风格的核心视觉元素
        self._tile = QFrame()
        self._tile.setObjectName("Tile")
        self._tile.setFixedSize(44, 44)
        tl = QVBoxLayout(self._tile)
        tl.setContentsMargins(0, 0, 0, 0)
        ic = QLabel(icon)
        ic.setAlignment(Qt.AlignCenter)
        ic.setStyleSheet("font-size: 20px; background: transparent; color: #ffffff;")
        tl.addWidget(ic)
        lay.addWidget(self._tile, 0, Qt.AlignTop)
        self._apply_tint(tint_key)
        theme.bus.changed.connect(lambda _: self._apply_tint(tint_key))

        box = QVBoxLayout()
        box.setSpacing(4)
        t = QLabel(title)
        t.setStyleSheet("font-size: 15px; font-weight: 700;")
        d = QLabel(desc)
        d.setObjectName("Muted")
        d.setWordWrap(True)
        d.setStyleSheet("font-size: 12px;")
        box.addWidget(t)
        box.addWidget(d)
        lay.addLayout(box, 1)

        arrow = QLabel("→")
        arrow.setObjectName("Faint")
        arrow.setStyleSheet("font-size: 16px; background: transparent;")
        lay.addWidget(arrow, 0, Qt.AlignVCenter)

    def _apply_tint(self, tint_key):
        cur = theme.current()
        color = cur.get(tint_key, cur["accent"])
        if theme.glass_on():
            # 液态玻璃主题：拟物玻璃贴片（对角渐变 + 高光描边）
            self._tile.setStyleSheet(
                "QFrame#Tile { %s }" % theme.glass_tile(color, radius=12))
        else:
            self._tile.setStyleSheet(
                f"QFrame#Tile {{ background: {color}; border: none; border-radius: 4px; }}"
            )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class SegmentedControl(QFrame):
    """分段选择器：互斥选项组。"""

    changed = Signal(str)

    def __init__(self, options, current=None, parent=None):
        """options: [(value, label), ...]"""
        super().__init__(parent)
        self.setObjectName("Segment")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = {}
        for value, label in options:
            b = QPushButton(label)
            b.setObjectName("SegmentButton")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self._group.addButton(b)
            self._buttons[value] = b
            lay.addWidget(b)
            b.clicked.connect(lambda checked=False, v=value: self.changed.emit(v))

        if current in self._buttons:
            self._buttons[current].setChecked(True)

    def set_current(self, value):
        if value in self._buttons:
            self._buttons[value].setChecked(True)

    def current(self):
        for value, b in self._buttons.items():
            if b.isChecked():
                return value
        return None


def primary_button(text):
    b = QPushButton(text)
    b.setObjectName("PrimaryButton")
    b.setCursor(Qt.PointingHandCursor)
    return b


def ghost_button(text):
    b = QPushButton(text)
    b.setObjectName("GhostButton")
    b.setCursor(Qt.PointingHandCursor)
    return b


def danger_button(text):
    b = QPushButton(text)
    b.setObjectName("DangerButton")
    b.setCursor(Qt.PointingHandCursor)
    return b


def setting_row(label_text, control):
    """一行「标签 + 右侧控件」的横向布局。"""
    row = QHBoxLayout()
    row.setSpacing(10)
    lbl = QLabel(label_text)
    lbl.setStyleSheet("font-size: 13px;")
    row.addWidget(lbl)
    row.addStretch(1)
    row.addWidget(control)
    return row


def info_card(title, desc):
    """说明卡片。"""
    card = QFrame()
    card.setObjectName("Card")
    v = QVBoxLayout(card)
    v.setContentsMargins(18, 16, 18, 16)
    v.setSpacing(6)
    t = QLabel(title)
    t.setObjectName("CardTitle")
    d = QLabel(desc)
    d.setObjectName("CardDesc")
    d.setWordWrap(True)
    v.addWidget(t)
    v.addWidget(d)
    return card


TOAST_MAX = 3            # 同屏最多 3 条，再多就把最旧的一条直接挤掉
TOAST_GAP = 8            # 相邻两条的间距
TOAST_BOTTOM = 40        # 最下面那条距窗口底边的高度
TOAST_SLIDE_MS = 180     # 被顶上来的动画时长

# id(父窗口) -> Toast 列表。下标 0 是最新出现、位置最低的那条
_TOAST_STACKS = {}


def _toast_stack(parent):
    """取某个父窗口的 Toast 栈，并顺手剔除已经被销毁的条目。

    不直接连 destroyed 信号是因为 Toast 的销毁路径有三条（淡出结束、
    被挤掉、父窗口关闭），逐个连容易漏；这里用「访问时校验」兜底。
    """
    key = id(parent)
    stack = _TOAST_STACKS.get(key)
    if stack is None:
        stack = []
        _TOAST_STACKS[key] = stack
        try:
            parent.destroyed.connect(lambda *_: _TOAST_STACKS.pop(key, None))
        except Exception:                                 # noqa: BLE001
            pass
        return stack

    alive = []
    for t in stack:
        try:
            t.height()          # C++ 对象已销毁时会抛 RuntimeError
        except RuntimeError:
            continue
        alive.append(t)
    if len(alive) != len(stack):
        stack[:] = alive
    return stack


def _alive(obj):
    """Qt 对象是否还活着（C++ 侧没被销毁）。

    场景（实测崩溃日志抓过三次）：Toast 同屏超限时 `_drop_toast` 会
    `deleteLater()` 掉最旧那条，但它 2.2 秒后的淡出定时器还挂在事件队列里；
    定时器到点时去碰它的子对象（QPropertyAnimation / 图形特效）就会抛
    `RuntimeError: Internal C++ object already deleted`。
    shiboken6 拿不到时保守地返回 True，让调用方自己的 try/except 兜住。
    """
    if obj is None:
        return False
    try:
        from shiboken6 import isValid
    except ImportError:
        return True
    try:
        return bool(isValid(obj))
    except (TypeError, RuntimeError):
        return False


def _slide_to(toast, target):
    """把一条 Toast 平滑移到新位置（被新提示顶上去时用）。"""
    if not _alive(toast):
        return
    try:
        anim = getattr(toast, "_slide_anim", None)
        if anim is not None and _alive(anim):
            anim.stop()
        anim = QPropertyAnimation(toast, b"pos", toast)
        anim.setDuration(TOAST_SLIDE_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(toast.pos())
        anim.setEndValue(target)
        anim.start()
        toast._slide_anim = anim      # 保住引用，别让动画被回收
    except RuntimeError:
        # shiboken6 不在时 _alive 会乐观返回 True，这里再兜一层
        return


def _restack(parent, instant=None):
    """按栈顺序重新摆放所有 Toast 的位置。

    `instant` 指定的那条直接落位 —— 新弹出的提示应该出现在底部，
    而不是从某个角落滑过来；其余的做平滑上移动画。
    """
    stack = _toast_stack(parent)
    y = parent.height() - TOAST_BOTTOM
    for t in stack:
        target_y = y - t.height()
        pos = QPoint((parent.width() - t.width()) // 2, target_y)
        if t is instant:
            t.move(pos)
        else:
            _slide_to(t, pos)
        y = target_y - TOAST_GAP


def _drop_toast(parent, toast):
    """立刻撤掉一条 Toast（被挤掉的），不走淡出。"""
    stack = _toast_stack(parent)
    if toast in stack:
        stack.remove(toast)
    if not _alive(toast):
        return
    try:
        anim = getattr(toast, "_slide_anim", None)
        if anim is not None and _alive(anim):
            anim.stop()
        toast.hide()
        toast.deleteLater()
    except RuntimeError:
        return


def _dismiss_toast(parent, toast):
    """淡出结束后真正移除，并让剩下的补位。"""
    stack = _toast_stack(parent)
    if toast in stack:
        stack.remove(toast)
        _restack(parent)
    toast.deleteLater()


def show_toast(parent, text, duration=2200):
    """在父窗口底部中央弹出 Toast 提示并自动淡出。

    多条提示**自下而上堆叠**：新的一条从底部出现，把已有的往上顶（带动画），
    所以连着开关几个选项时每条都能看清。同屏最多 `TOAST_MAX` 条，
    再多的最旧一条直接消失，避免堆满半个窗口。
    """
    p = theme.current()
    stack = _toast_stack(parent)

    # 先腾位置：把多出来的最旧一条（栈顶，也就是最靠上的那条）挤掉，
    # 保证「加上新的之后」总数不超过上限。
    while len(stack) >= TOAST_MAX:
        _drop_toast(parent, stack[-1])

    toast = QFrame(parent)
    toast.setObjectName("ToastFrame")
    toast.setStyleSheet(
        f"QFrame#ToastFrame {{ background: {p['toast_bg']}; border: 1px solid {p['border_strong']}; "
        f"border-left: 3px solid {p['accent']}; border-radius: 5px; }}"
        f"QLabel {{ color: {p['text']}; font-size: 13px; background: transparent; }}"
    )
    lay = QHBoxLayout(toast)
    lay.setContentsMargins(16, 10, 16, 10)
    lay.setSpacing(8)
    dot = QLabel("●")
    dot.setStyleSheet(f"color: {p['accent']}; font-size: 9px; background: transparent;")
    lay.addWidget(dot)
    lbl = QLabel(text)
    lay.addWidget(lbl)

    toast.adjustSize()
    toast.show()
    toast.raise_()

    # 就位：新的这条直接落在底部，已有的整体上移一条
    stack.insert(0, toast)
    _restack(parent, instant=toast)

    effect = QGraphicsOpacityEffect(toast)
    toast.setGraphicsEffect(effect)
    fade_in = QPropertyAnimation(effect, b"opacity", toast)
    fade_in.setDuration(180)
    fade_in.setStartValue(0.0)
    fade_in.setEndValue(1.0)
    fade_in.start()

    def _fade_out():
        # 这条 Toast 可能已经被 _drop_toast 提前删了（同屏超过 TOAST_MAX 时
        # 挤掉最旧的），此时它的动画/特效连 C++ 对象一起没了——整段容错。
        if not _alive(toast) or not _alive(effect):
            return
        try:
            anim = getattr(toast, "_slide_anim", None)
            if anim is not None and _alive(anim):
                anim.stop()
            out = QPropertyAnimation(effect, b"opacity", toast)
            out.setDuration(240)
            out.setStartValue(1.0)
            out.setEndValue(0.0)
            out.finished.connect(lambda: _dismiss_toast(parent, toast))
            out.start()
            toast._fade_anim = out        # 保住引用
        except RuntimeError:
            return

    QTimer.singleShot(duration, _fade_out)


# ---------------------------------------------------------------------------
# 硬件信息展示组件
# ---------------------------------------------------------------------------
class SpecChip(QFrame):
    """设备形态标签：笔记本 / 台式机 / 一体机 的彩色小块。"""

    def __init__(self, kind="unknown", label="检测中", parent=None):
        super().__init__(parent)
        self.setObjectName("SpecChip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 10, 3)
        lay.setSpacing(6)
        self._dot = QFrame()
        self._dot.setFixedSize(8, 8)
        lay.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._lbl = QLabel(label)
        self._lbl.setStyleSheet("font-size: 12px; font-weight: 700; background: transparent;")
        lay.addWidget(self._lbl)
        theme.bus.changed.connect(lambda _: self._repaint(kind))
        self._repaint(kind)

    def set_kind(self, kind, label):
        self._lbl.setText(label)
        self._repaint(kind)
    def _repaint(self, kind):
        p = theme.current()
        color = {"laptop": p["cyan"], "desktop": p["green"]}.get(kind, p["amber"])
        self.setStyleSheet(
            f"QFrame#SpecChip {{ background: {p['surface_hover']}; "
            f"border: 1px solid {color}; border-radius: 4px; }}"
            f"QLabel {{ color: {color}; }}"
        )
        self._dot.setStyleSheet(f"background: {color}; border-radius: 4px;")


class HardwarePanel(QFrame):
    """首页电脑配置面板：机型 / CPU / 显卡 / 内存 / 硬盘 + 设备分类。

    用法：
        panel = HardwarePanel(refresh_cb)      # 采集函数，返回 profile dict
        panel.refresh()                        # 触发异步采集
    """

    # 采集与展示状态
    STATE_IDLE = 0
    STATE_LOADING = 1
    STATE_READY = 2
    STATE_ERROR = 3

    # 子线程 → 主线程的结果投递（Qt 信号是线程安全的，跨线程会自动排队到主线程事件循环）
    _scanned = Signal(object)
    _failed = Signal(str)

    def __init__(self, scanner_factory=None, on_profile=None, parent=None):
        super().__init__(parent)
        self.setObjectName("HwPanel")
        self._scanner_factory = scanner_factory
        self._on_profile = on_profile
        self._state = self.STATE_IDLE
        self._busy = False
        self._profile = None
        self._build()
        self._scanned.connect(self._on_ready)
        self._failed.connect(self._set_error)
        theme.bus.changed.connect(lambda _: self._restyle())

    # ------------------------------------------------------------------ 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # 头部：标题 + 设备分类标签 + 刷新按钮
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("💻  本机配置")
        title.setObjectName("CardTitle")
        head.addWidget(title)

        self.chip = SpecChip("unknown", "检测中")
        head.addWidget(self.chip)
        head.addStretch(1)

        self._status = QLabel("")
        self._status.setObjectName("Faint")
        self._status.setStyleSheet("font-size: 11px;")
        head.addWidget(self._status)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setObjectName("MiniButton")
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.setFixedHeight(26)
        self.btn_refresh.clicked.connect(self.refresh)
        head.addWidget(self.btn_refresh)
        root.addLayout(head)

        # 机型整行
        self._model_row = self._make_model_row()
        root.addLayout(self._model_row)

        # 4 个规格块：2×2 网格，保证长型号名有足够宽度
        self._spec_grid = QGridLayout()
        self._spec_grid.setSpacing(10)
        self._cells = {
            "cpu": SpecCell("CPU", "🧠", "tile_1"),
            "gpu": SpecCell("显卡", "🎮", "tile_2"),
            "ram": SpecCell("内存", "🧩", "tile_4"),
            "disk": SpecCell("硬盘", "💾", "tile_3"),
        }
        self._spec_grid.addWidget(self._cells["cpu"], 0, 0)
        self._spec_grid.addWidget(self._cells["gpu"], 0, 1)
        self._spec_grid.addWidget(self._cells["ram"], 1, 0)
        self._spec_grid.addWidget(self._cells["disk"], 1, 1)
        root.addLayout(self._spec_grid)

        # 存储空间：逐盘列出容量与占用（标注盘号，如 C: / D: / E:）
        self._drive_section = QFrame()
        self._drive_section.setObjectName("DriveSection")
        ds = QVBoxLayout(self._drive_section)
        ds.setContentsMargins(0, 0, 0, 0)
        ds.setSpacing(6)

        dhead = QHBoxLayout()
        dhead.setSpacing(8)
        dtitle = QLabel("存储空间")
        dtitle.setObjectName("Faint")
        dtitle.setStyleSheet("font-size: 11px; font-weight: 700;")
        dhead.addWidget(dtitle)
        self._drive_sum = QLabel("")
        self._drive_sum.setObjectName("Faint")
        self._drive_sum.setStyleSheet("font-size: 11px;")
        dhead.addWidget(self._drive_sum)
        dhead.addStretch(1)
        ds.addLayout(dhead)

        self._drive_rows = []
        self._drive_box = QVBoxLayout()
        self._drive_box.setContentsMargins(0, 0, 0, 0)
        self._drive_box.setSpacing(5)
        ds.addLayout(self._drive_box)
        root.addWidget(self._drive_section)

        # 次级信息（系统 / 主板 / BIOS）
        self._sub = QLabel("")
        self._sub.setObjectName("Faint")
        self._sub.setStyleSheet("font-size: 11px;")
        self._sub.setWordWrap(True)
        root.addWidget(self._sub)

        self._set_loading()

    def _make_model_row(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        self._model_cell = SpecCell("设备型号", "🔖", "tile_5", large=True)
        row.addWidget(self._model_cell, 1)
        return row

    # ------------------------------------------------------------------ 样式
    def _restyle(self):
        p = theme.current()
        self.setStyleSheet(
            f"QFrame#HwPanel {{ background: {p['card_top']}; "
            f"border: 1px solid {p['border']}; border-radius: 5px; }}"
        )

    def paintEvent(self, event):
        # 让 QSS 的背景生效
        from PySide6.QtWidgets import QStyleOption, QStyle
        from PySide6.QtGui import QPainter as _P

        opt = QStyleOption()
        opt.initFrom(self)
        pt = _P(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, pt, self)

    # ------------------------------------------------------------------ 存储
    def _clear_drives(self, placeholder=""):
        """清空逐盘列表（加载中 / 出错时调用）。"""
        while self._drive_box.count():
            item = self._drive_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._drive_rows = []
        self._drive_sum.setText(placeholder)

    def _set_drives(self, drives, total_text="", free_text=""):
        """渲染逐盘列表：每个逻辑盘一行（盘符 + 占用条 + 容量）。"""
        self._clear_drives()
        if not drives:
            self._drive_section.setVisible(False)
            return
        self._drive_section.setVisible(True)
        n = len(drives)
        extra = f"共 {n} 个盘"
        if total_text:
            extra += f" · 合计 {total_text}"
        if free_text:
            extra += f" · 可用 {free_text}"
        self._drive_sum.setText(extra)
        for d in drives:
            row = DriveRow()
            row.set_drive(d)
            self._drive_box.addWidget(row)
            self._drive_rows.append(row)

    # ------------------------------------------------------------------ 状态
    def _set_loading(self):
        self._state = self.STATE_LOADING
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("刷新")
        self._status.setText("正在读取本机硬件信息…")
        self.chip.set_kind("unknown", "检测中")
        for cell in self._cells.values():
            cell.set_value("读取中", "")
        self._model_cell.set_value("读取中", "")
        self._clear_drives("读取中…")
        self._sub.setText("")

    def _set_error(self, message):
        self._busy = False
        self._state = self.STATE_ERROR
        self.btn_refresh.setEnabled(True)
        self._status.setText(message)
        self.chip.set_kind("unknown", "读取失败")
        for cell in self._cells.values():
            cell.set_value("--", "")
        self._model_cell.set_value("--", "")
        self._clear_drives("读取失败")

    def refresh(self):
        """触发异步采集。scanner_factory 返回一个带 start() 的对象。

        注意：采集在工作线程执行，结果必须通过 Qt 信号投递回主线程，
        绝不能把 QWidget 的方法直接交给子线程调用（会导致状态静默卡死）。
        """
        if self._busy:
            return
        self._busy = True
        self._set_loading()
        if self._scanner_factory is None:
            self._busy = False
            self._set_error("未配置采集器")
            return

        def _ok(profile):
            self._scanned.emit(profile)

        def _err(message):
            self._failed.emit(message)

        self._scanner_factory(_ok, _err).start()

    def load_profile(self, profile):
        """直接注入已缓存的 profile（同步，用于二次启动秒开）。"""
        self._on_ready(profile, from_cache=True)

    def _on_ready(self, profile, from_cache=False):
        if not profile:
            self._set_error("未能读取到硬件信息")
            return
        self._busy = False
        self._state = self.STATE_READY
        self._profile = profile
        self.btn_refresh.setEnabled(True)
        self._status.setText("缓存数据（点击刷新重新读取）" if from_cache else "已读取本机硬件")
        if self._on_profile and not from_cache:
            self._on_profile(profile)
        self.chip.set_kind(profile["kind"], profile["kind_label"])
        self.chip.setToolTip("判定依据：" + profile.get("kind_reason", ""))

        # 机型：制造商 + 型号
        model = profile["model"]
        maker = profile["manufacturer"]
        sub_bits = []
        if maker and maker != "--" and maker.lower() not in model.lower():
            sub_bits.append(maker)
        if profile.get("product_version"):
            sub_bits.append(profile["product_version"])
        self._model_cell.set_value(model, " · ".join(sub_bits))
        self._model_cell.setToolTip(profile.get("kind_reason", ""))

        # CPU
        self._cells["cpu"].set_value(
            _shorten(profile["cpu_name"], 44), profile["cpu_detail"]
        )
        self._cells["cpu"].setToolTip(profile["cpu_name"])

        # 显卡：主显卡名称 + 显存，多卡时标注数量
        gpus = profile["gpus"]
        if gpus:
            main = gpus[0]
            extra = f"（共 %d 个适配器）" % len(gpus) if len(gpus) > 1 else ""
            detail = main["vram"] + extra
            if main.get("resolution"):
                detail = main["vram"] + " · " + main["resolution"] + extra
            self._cells["gpu"].set_value(_shorten(main["name"], 44), detail)
            self._cells["gpu"].setToolTip(
                "\n".join(
                    (g.get("vendor_label") and g["vendor_label"] + " · " or "")
                    + g["name"] + "  " + g["vram"]
                    for g in gpus
                )
            )
        else:
            self._cells["gpu"].set_value("未检测到", "")

        # 内存
        self._cells["ram"].set_value(profile["ram_total"], profile["ram_detail"])

        # 硬盘 / 存储：主值显示盘号，明细逐盘列出容量
        disks = profile.get("disks", [])
        drives = profile.get("drives", [])
        internal = [d for d in disks if not d["external"]]
        if drives:
            letters = " ".join(d["letter"] for d in drives)
            detail_bits = [f"{len(drives)} 个盘"]
            if profile.get("drives_total"):
                detail_bits.append(f"共 {profile['drives_total']}")
            if profile.get("drives_free"):
                detail_bits.append(f"可用 {profile['drives_free']}")
            self._cells["disk"].set_value(_shorten(letters, 44), " · ".join(detail_bits))
        else:
            detail = f"{len(internal)} 块内置" if internal else "--"
            if len(disks) > len(internal):
                detail += f" · {len(disks) - len(internal)} 外置"
            self._cells["disk"].set_value(profile.get("disk_total", "--"), detail)
        # 物理硬盘信息放进 tooltip，避免和逐盘列表重复
        self._cells["disk"].setToolTip(
            "\n".join(
                [f"{d['kind']}　{d['model']}　{d['size']}" for d in disks]
                + [""]
                + [f"{d['letter']}　{d['used']} / {d['total']}　可用 {d['free']}"
                   for d in drives]
            )
        )
        self._set_drives(drives, profile.get("drives_total", ""),
                         profile.get("drives_free", ""))

        # 次级信息
        sub = " · ".join(
            x for x in [
                profile["os"],
                f"Build {profile['os_build']}" if profile["os_build"] != "--" else "",
                profile["os_arch"] if profile["os_arch"] != "--" else "",
                f"主板 {profile['board']}" if profile["board"] != "--" else "",
                f"BIOS {profile['bios']}" if profile["bios"] != "--" else "",
            ] if x
        )
        self._sub.setText(sub)


class SpecCell(QFrame):
    """单个规格块：色块图标 + 标签 + 主值 + 副值。"""

    def __init__(self, label, icon, tint_key="tile_1", large=False, parent=None):
        super().__init__(parent)
        self.setObjectName("SpecCell")
        self._tint_key = tint_key
        self.setMinimumHeight(84 if not large else 62)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        # 色块图标
        self._tile = QFrame()
        self._tile.setFixedSize(34 if not large else 30, 34 if not large else 30)
        tl = QVBoxLayout(self._tile)
        tl.setContentsMargins(0, 0, 0, 0)
        ic = QLabel(icon)
        ic.setAlignment(Qt.AlignCenter)
        ic.setStyleSheet("font-size: 15px; background: transparent; color: #ffffff;")
        tl.addWidget(ic)
        lay.addWidget(self._tile, 0, Qt.AlignVCenter)

        box = QVBoxLayout()
        box.setSpacing(2)
        self._label = QLabel(label)
        self._label.setObjectName("Faint")
        self._label.setStyleSheet("font-size: 11px;")
        self._value = QLabel("读取中")
        self._value.setStyleSheet(
            f"font-size: {'13px' if not large else '14px'}; font-weight: 700;"
        )
        self._value.setWordWrap(False)
        self._detail = QLabel("")
        self._detail.setObjectName("Faint")
        self._detail.setStyleSheet("font-size: 11px;")
        box.addWidget(self._label)
        box.addWidget(self._value)
        box.addWidget(self._detail)
        lay.addLayout(box, 1)

        theme.bus.changed.connect(lambda _: self._apply_tint())
        self._apply_tint()

    def _apply_tint(self):
        p = theme.current()
        color = p.get(self._tint_key, p["accent"])
        self._tile.setStyleSheet(
            f"QFrame {{ background: {color}; border: none; border-radius: 4px; }}"
        )
        self.setStyleSheet(
            f"QFrame#SpecCell {{ background: {p['surface_sunken']}; "
            f"border: 1px solid {p['border']}; border-radius: 4px; }}"
            f"QLabel {{ background: transparent; }}"
        )

    def set_value(self, value, detail=""):
        self._value.setText(value or "--")
        self._detail.setText(detail or "")
        self._detail.setVisible(bool(detail))


def _shorten(text, limit):
    """过长文本截断并加省略号，避免撑破卡片（用 ASCII 省略号避免字体缺字）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def usage_color(percent):
    """按占用率取色：≥90% 红 / ≥75% 琥珀 / 否则绿（与清理页阈值一致）。"""
    p = theme.current()
    if percent >= 90:
        return p["red"]
    if percent >= 75:
        return p.get("amber", "#ffb020")
    return p["green"]


class DriveBar(QFrame):
    """磁盘占用条（自绘，已用部分按占用率变色）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self.setMinimumWidth(60)
        self._percent = 0.0

    def set_percent(self, percent):
        self._percent = max(0.0, min(100.0, float(percent or 0.0)))
        self.update()

    def paintEvent(self, event):
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # 底槽：必须与行背景（surface_sunken）区分开，否则占用率低的盘
        # 整条都是"看不见"的（曾经用同色，E 盘 0% 时看起来根本没有进度条）
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p.get("border_strong", p["border"])))
        painter.drawRoundedRect(0, 0, w, h, 3, 3)
        # 已用
        filled = int(w * self._percent / 100.0)
        if filled > 0:
            painter.setBrush(QColor(usage_color(self._percent)))
            painter.drawRoundedRect(0, 0, max(filled, 3), h, 3, 3)
        painter.end()


class DriveRow(QFrame):
    """单个逻辑盘一行：[盘符] [占用条] 已用/总计 · 可用。

    盘符（"盘号"）用色块徽标突出，一眼能对应上资源管理器里的 C/D/E 盘。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DriveRow")
        self._percent = 0.0          # 先声明，避免样式回调早于 set_drive 触发
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 12, 7)
        lay.setSpacing(10)

        # 盘符徽标
        self._badge = QLabel("C:")
        self._badge.setObjectName("DriveBadge")
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedSize(38, 24)
        lay.addWidget(self._badge, 0, Qt.AlignVCenter)

        # 卷标 / 类型
        self._meta = QLabel("")
        self._meta.setObjectName("Faint")
        self._meta.setStyleSheet("font-size: 11px; background: transparent;")
        self._meta.setFixedWidth(54)
        lay.addWidget(self._meta, 0, Qt.AlignVCenter)

        # 占用条
        self._bar = DriveBar()
        lay.addWidget(self._bar, 1, Qt.AlignVCenter)

        # 容量文字
        self._size = QLabel("")
        self._size.setStyleSheet("font-size: 12px; background: transparent;")
        self._size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._size.setMinimumWidth(196)
        lay.addWidget(self._size, 0, Qt.AlignVCenter)

        # 注意：必须连**绑定方法**（而不是 lambda）。用 lambda 时 Qt 不知道接收者是谁，
        # 控件被 deleteLater() 回收后仍会收到主题信号 → "Internal C++ object already deleted"。
        # 连绑定方法后 PySide 会在对象析构时自动断开。
        theme.bus.changed.connect(self._restyle)

    def set_drive(self, d):
        """填充一个逻辑盘的数据（d 为 profile["drives"] 里的一项）。"""
        self._badge.setText(d["letter"])
        kind = d.get("kind_label", "")
        label = d.get("label") or ""
        self._meta.setText(_shorten(label or kind, 6))
        self._meta.setToolTip(
            f"{d['letter']}　{d.get('fs', '')}　{kind}"
            + (f"　卷标：{label}" if label else "")
        )
        self._percent = d.get("percent", 0.0)
        self._bar.set_percent(self._percent)
        self._size.setText(f"{d['used']} / {d['total']}　·　可用 {d['free']}")
        self._size.setToolTip(
            f"{d['letter']} 总计 {d['total']}　已用 {d['used']}　"
            f"可用 {d['free']}　占用率 {self._percent:.1f}%"
        )
        self._restyle()

    def _restyle(self, *args):
        try:
            self._apply_styles()
        except RuntimeError:
            # 底层 C++ 对象已销毁（主题信号晚于 deleteLater 到达），忽略即可
            pass

    def _apply_styles(self):
        p = theme.current()
        color = usage_color(getattr(self, "_percent", 0.0))
        self.setStyleSheet(
            f"QFrame#DriveRow {{ background: {p['surface_sunken']}; "
            f"border: 1px solid {p['border']}; border-radius: 4px; }}"
            f"QLabel {{ background: transparent; }}"
        )
        self._badge.setStyleSheet(
            f"background: {color}; color: #ffffff; font-size: 12px; "
            f"font-weight: 700; border: none; border-radius: 4px;"
        )
        self._size.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {p['text']}; "
            f"background: transparent;"
        )
        # 占用条是自绘的，不跟着 QSS 走，主题切换时手动重绘
        self._bar.update()
