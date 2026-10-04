"""Yuhub 通用 UI 组件：开关、卡片、分段选择器、Toast、按钮等。"""

from PySide6.QtCore import (
    Qt,
    Signal,
    QAbstractAnimation,
    QEvent,
    QPoint,
    QPointF,
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
    QWidget,
)

from . import glass
from . import motion
from . import theme


class ToggleSwitch(QAbstractButton):
    """自定义开关（椭圆形轨道 + 圆形滑块），弹簧滑动 + 果冻拉伸。

    动效从 AndroidLiquidGlass 的 LiquidToggle 借来三样：
      1) **弹簧曲线**（冲过目标再弹回）替代直线 ease-out；
      2) **移动中被拉长** —— 滑块像被惯性拽着，尾部拖在后头。这是"果冻感"
         最直观的来源（参考实现里对应 `velocity / 50` 那两行 scaleX/scaleY）；
      3) **按下时放大** —— 对应参考实现的 `pressedScale = 1.5`。

    轨道开态也从"实心主题蓝"换成浅蓝液态玻璃（跟侧栏选中条同一套色）。
    """

    KNOB_MARGIN = 3.0       # 静止时滑块与轨道内缘的间距（上下左右一致 → 正好居中）
    STRETCH_MAX = 0.55      # 拉伸时额外增加的宽度 = 直径 × 这个系数（会被边界钳制）
    STRETCH_REF = 0.08      # 一帧位移达到这个量 = 拉伸拉满（60fps 下的峰值速度）
    PRESS_SCALE = 0.20      # 按下时直径放大的比例

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)
        self._offset = 1.0 if checked else 0.0
        self._stretch = 0.0        # 0~1，移动中被拉长的程度
        self._stretch_dir = 1.0    # +1 向右、-1 向左（只记方向，供自检/调试）
        self._press = 0.0          # 0~1，按下进度

        self._anim = QVariantAnimation(self)
        # 血泪：这里原来只有 setDuration(150)、**没有 setEasingCurve** ——
        # QVariantAnimation 不设曲线时是 Linear（匀速），滑块走得"机械"。
        # 现在是弹簧：冲过目标再弹回，滑块是"撞"到另一头的。
        self._anim.setDuration(motion.SWITCH)
        self._anim.setEasingCurve(motion.curve(motion.CURVE_SPRING))
        self._anim.valueChanged.connect(self._on_anim)

        self._press_anim = QVariantAnimation(self)
        self._press_anim.setDuration(motion.HOVER)
        self._press_anim.setEasingCurve(motion.curve(motion.CURVE_SPRING_SOFT))
        self._press_anim.valueChanged.connect(self._on_press)

        self.toggled.connect(self._on_toggled)
        self.pressed.connect(lambda: self._animate_press(1.0))
        self.released.connect(lambda: self._animate_press(0.0))
        theme.bus.changed.connect(lambda _: self.update())

    # -- 形状 -----------------------------------------------------------------
    # 抽成方法而不是就地写常量，是为了让自检能断言"轨道是椭圆、滑块是圆"
    # （2026-10-03：用户要求把开关从"小圆角方块"改成椭圆形 + 圆形滑块）。
    def track_radius(self):
        """轨道圆角 = 高度一半 → 左右两端是半圆，整体成椭圆形（胶囊）。"""
        return self.height() / 2.0

    def knob_diameter(self):
        """滑块静止时的直径（不含按下放大）。"""
        return max(1.0, self.height() - 2.0 * self.KNOB_MARGIN)

    def knob_rect(self, offset=None, stretch=None):
        """滑块的方框。

        静止时是**正方形**（配 `knob_radius()` 就是个正圆）；移动中被拉长成
        胶囊（果冻拉伸）。

        ⚠️ **滑块必须完整落在控件矩形内**（2026-10-04 修 bug）。轨道是"胶囊"
        —— 左右两端各一个半径 = h/2 的半圆；滑块静止时直径 18、圆心在
        (12,12)/(30,12)，与两端半圆**同心内切**。所以只要滑块越出
        `[0, width]`，戳出去的那截就画在控件背景上（轨道形状之外），
        看起来就是"白圆的角变成了方块/竖条"。

        第一版是**单侧拖尾**（`x -= extra`），起步那一帧 `x` 直接到 −5.4
        （半径才 9），左边被削掉一大块平口 —— 用户报的"白色圆圈变正方形"
        就是这个。现在改成：

          * 位置：圆心在 `[边距 + d/2, 右边距 − d/2]` 之间随 t 插值
            （端点处正好是两端半圆的圆心）；
          * 拉伸：以**圆心为中心对称**加宽（等效参考实现的 `scaleX`，
            单侧拖尾在这个尺寸下必然越界）；
          * 钳制：`w ≤ 2·cx` 且 `w ≤ 2·(width − cx)` —— 拉伸到端点时
            自动收窄到 24（正好与端部半圆内切），中途才拉满到 27.9。

        `offset` / `stretch` 可以显式传入，方便自检把两种形态都量到。
        """
        t = self._offset if offset is None else float(offset)
        t = max(0.0, min(1.0, t))          # 弹簧会过冲，绘制必须夹住
        st = self._stretch if stretch is None else float(stretch)
        # 按下放大围绕中心进行，且不能高过控件（否则上下也会被裁）
        d = self.knob_diameter() * (1.0 + self.PRESS_SCALE * self._press)
        d = min(d, float(self.height()))

        # 圆心行程：两端正好落在轨道两端半圆的圆心上
        lo = self.KNOB_MARGIN + d / 2.0
        hi = self.width() - self.KNOB_MARGIN - d / 2.0
        if hi < lo:                        # 极窄控件（防呆）：居中不动
            lo = hi = self.width() / 2.0
        cx = lo + t * (hi - lo)

        extra = d * self.STRETCH_MAX * max(0.0, min(1.0, st))
        w = d + extra
        w = min(w, 2.0 * cx, 2.0 * (self.width() - cx))   # 不许戳出控件
        w = max(w, d)                      # 但也不能比滑块本体还窄
        x = cx - w / 2.0
        y = (self.height() - d) / 2.0
        return QRectF(x, y, w, d)

    def knob_radius(self):
        """滑块圆角 = 半个高度 → 静止时是正圆、拉伸时是胶囊。"""
        return self.knob_rect().height() / 2.0

    # -- 动画 -----------------------------------------------------------------
    def _on_toggled(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _on_anim(self, value):
        """逐帧驱动。顺带把"这一帧走了多远"换算成拉伸量。

        为什么用帧位移而不是时间导数：QVariantAnimation 给的本来就是离散帧，
        `value - _offset` 就是这一帧的实际位移，直接除一个参考值即可 ——
        不需要再引一阶差分、也不会因为帧率抖动而算飞。
        """
        value = float(value)
        delta = value - self._offset
        self._offset = value
        if abs(delta) > 1e-4:
            self._stretch_dir = 1.0 if delta > 0 else -1.0
            self._stretch = min(1.0, abs(delta) / self.STRETCH_REF)
        else:
            self._stretch = 0.0            # 收尾时自然归零，不会停在"半拉长"
        self.update()

    def _animate_press(self, to):
        self._press_anim.stop()
        self._press_anim.setStartValue(self._press)
        self._press_anim.setEndValue(float(to))
        self._press_anim.start()

    def _on_press(self, value):
        self._press = float(value)
        self.update()

    def paintEvent(self, event):
        """液态玻璃开关。

        视觉分三层：
          1) 轨道底色：关态是沉底色，开态是**浅蓝液态玻璃**，两态之间按滑动
             进度插值；形状是椭圆形（圆角 = 高度一半），左右两端是半圆；
          2) 玻璃光泽：对角反光 + 内侧光带 + 内阴影 + 顶部高光线。
             **开态明显强于关态**——这一处就是「功能开启」的视觉重点：
             点亮时整块轨道像被光穿过，而不是简单换个颜色；
          3) 滑块：白色，静止是**正圆**、移动中被拉成胶囊（果冻），带竖向
             渐变 + 细边 + 下方投影，像一颗厚玻璃珠。
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = theme.current()
        liq = theme.liquid_palette()
        off_bg = QColor(c.get("toggle_off", c["surface_sunken"]))
        # 开态：浅蓝液态玻璃，但**往 accent 里混 30%**。纯 liquid_tint（#a1c3fb）
        # 太亮，白色滑块浮上去对比度只剩 1.5:1，轮廓会"化"进轨道里。
        on_bg = glass.mix(liq["tint"], c.get("accent", "#3b82f6"), 0.30)
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

        # ---- 2) 液态玻璃光泽：开态拉满 ----
        # 色散关掉（chroma_alpha=0）：42x24 的小控件上，"左青右品红"两条 1px
        # 会变成两粒看不出所以然的彩噪，而不是"玻璃在掰光"。
        glass.paint_liquid_glass(
            p, QRect(1, 1, w - 2, h - 2), radius=max(0.5, radius - 0.5),
            tint=col, edge="#ffffff",
            tint_alpha=0,               # 底色已铺，这里只叠光
            gloss_alpha=int(40 + 88 * t),
            top_alpha=int(58 + 106 * t),
            border_alpha=int(52 + 92 * t),
            glow_alpha=int(38 + 76 * t), glow_thickness=7,
            chroma_alpha=0,
            shade_alpha=int(24 + 44 * t), shade_thickness=5)

        # 边框：关态是硬边框，开态被玻璃亮边吃掉，只留一层淡淡的锁边
        border = QColor(c["accent"] if t > 0.5 else c["border_strong"])
        border.setAlpha(int(140 + 60 * t))
        p.setPen(border)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), radius, radius)

        # ---- 3) 滑块：白色（拉伸时成胶囊）+ 竖向渐变 + 细边 + 投影 ----
        kr = self.knob_rect()
        kr_radius = kr.height() / 2.0
        # 投影比改版前更实（offset 1→2、alpha 52→96）：滑块压在浅蓝轨道上，
        # "白 vs 浅蓝"的亮度差本来就小，靠这圈影子和描边把轮廓托出来。
        glass.paint_soft_shadow(p, kr, radius=kr_radius, offset=2, spread=2,
                                alpha=96)

        grad = QLinearGradient(kr.topLeft(), kr.bottomLeft())
        grad.setColorAt(0.0, QColor("#ffffff"))
        grad.setColorAt(1.0, QColor("#e9edf3"))
        p.setPen(glass.rgba("#ffffff", 130))
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


class SegmentSlider(QWidget):
    """分段控件的「滑动液态玻璃选中片」。

    挂在 `QFrame#Segment` 容器上、`lower()` 到**最底层**，按当前选中按钮的
    位置画一块浅蓝液态玻璃；切换时用弹簧从旧位置滑到新位置。

    为什么必须自绘：QSS 的 `:checked` 是**瞬时**换色 —— 既不能插值，也做不出
    "滑块滑过去"这件事。所以 `SegmentButton:checked` 的背景在 QSS 里已经
    改成透明（见 theme.py 与 theme_packs.SKY_QSS），选中态完全由这块玻璃承担。

    为什么监听 `toggled` 而不是 `clicked`：鼠标点、键盘方向键、以及代码里
    `setChecked()` 三条路都得能更新。构造期容器还没 layout，按钮 geometry
    还是 (0,0,0,0)，那时只留 `_pending` 标记，等第一次 Show/Resize 直接落位
    （不做动画 —— 否则一进页面就看见选中片从左上角飞过来）。
    """

    # 玻璃片的圆角。跟着两块 QSS 的按钮圆角取中间值：内置模板 4px、
    # sky glass 主题包 7px —— 差 2px 在这几十像素的片上肉眼分不出，
    # 但比"照抄其中一边"稳（主题包是会换的）。
    RADIUS = 6.0
    TINT_ALPHA = 214
    #: QWidget 的默认尺寸。没跑过 layout 的控件 geometry 就是这个值，
    #: 拿它当"还没布局"的指纹（见 `_target_rect`）。
    _WIDGET_DEFAULT = (640.0, 480.0)

    def __init__(self, host, parent=None, radius=None):
        super().__init__(parent or host)
        # 铺满容器当背景层，绝不抢鼠标 —— 否则整个分段控件都点不动了
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._host = host
        if radius is not None:
            self.RADIUS = float(radius)
        self._buttons = []
        self._hooks = []           # [(btn, callable)]，断开时精确定位自己的连接
        self._owner = None         # 最近一次"变成选中"的按钮 = 本次的目标
        self._from = None          # QRectF，起点
        self._to = None            # QRectF，终点
        self._t = 1.0              # 当前插值进度（弹簧会 >1 一点，= 果冻过冲）
        self._pending = True       # True = 还没落位

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(motion.PAGE_IN)
        self._anim.setEasingCurve(motion.curve(motion.CURVE_SPRING_SOFT))
        self._anim.valueChanged.connect(self._on_value)
        # 跑完再落一次位：动画期间如果容器 resize 过，按钮几何已经变了
        self._anim.finished.connect(lambda: self._place_now())

        host.installEventFilter(self)
        theme.bus.changed.connect(lambda _: self.update())
        # 压到所有按钮**下面**才不会被文字盖住（这是背景层，不是覆盖层）。
        # 血泪：毛玻璃那轮用过 `stackUnder()` 逐个压按钮，那个 API 只有最后
        # 一次调用生效；这里只有一个滑块，`lower()` 一步到位。
        self.lower()
        self.rebuild()

    # -- 装配 ---------------------------------------------------------------
    def rebuild(self):
        """扫描容器里的 SegmentButton 并接上信号（改了结构后可以再调一次）。"""
        for b, hook in self._hooks:
            try:
                b.toggled.disconnect(hook)
            except (RuntimeError, TypeError):
                pass
        self._hooks = []
        self._buttons = [b for b in self._host.findChildren(QAbstractButton)
                         if b.objectName() == "SegmentButton"]
        for b in self._buttons:
            # ⚠️ 必须把按钮**显式**绑进闭包，不能靠槽里 `self.sender()`：
            # 实测在"用别的函数顶替 `_on_toggled`"这类包装下 sender() 会
            # 返回 None，回退成"扫第一个 checked"就又会踩到旧按钮。
            # 闭包必须存进 `_hooks` 留个引用 —— 否则会被 GC 掉，回调失效。
            hook = (lambda checked, btn=b: self._on_toggled(checked, btn))
            b.toggled.connect(hook)
            self._hooks.append((b, hook))
        if self._owner not in self._buttons:
            self._owner = None
        self._pending = True
        self._sync()
        return self

    # -- 几何 ---------------------------------------------------------------
    def _rect_of(self, btn):
        """把某个按钮的 geometry 换算成可用的方框；不可用就返回 None。

        ⚠️ **必须排掉"还没跑过 layout"的按钮**：QWidget 的默认尺寸是
        640x480，而构造期的容器**也是** 640x480 —— 于是 `btn.geometry()`
        就是 (0,0,640,480)，只看"宽高 > 1"会把它当成目标，片一上来就落成
        一个巨框（第一版就是这样，之后还因为 `_pending` 已翻成 False 而
        再也不更新）。

        怎么认"没布局"：**用不了 `QLayout.isActivated()`**（PySide6 没暴露）
        也**用不了 `Qt.WA_LaidOut`**（实测顶层窗口 show 完仍是 False，
        而嵌套容器的子控件在没 show 时就已经是 True，两头都不可信）。
        只能认那个默认尺寸 —— 真实的分段按钮宽高不可能正好 640x480。
        再补一条"框得装在容器里"做第二道闸。
        """
        if btn is None:
            return None
        try:
            if not btn.isChecked():     # 已经不是选中项了
                return None
        except RuntimeError:            # 已 deleteLater 的对象
            return None
        hr = self._host.rect()
        r = QRectF(btn.geometry())
        if r.width() < 1 or r.height() < 1:
            return None
        if (r.width() == self._WIDGET_DEFAULT[0]
                and r.height() == self._WIDGET_DEFAULT[1]):
            return None              # 未布局：QWidget 的默认 640x480
        if r.width() > hr.width() + 1 or r.height() > hr.height() + 1:
            return None
        return r

    def _target_rect(self, btn=None):
        """本次该落到哪个方框上（容器坐标系 = 本控件坐标系，因为铺满）。

        ⚠️ **不能只认"扫描到的第一个 checked 按钮"**（v0.15.2 修）：点一下
        `QPushButton` 时，Qt 是**先** `nextCheckState()` 把自己翻成 checked、
        **再** emit `clicked`（业务代码才在里面把旧按钮取消）。所以在
        `toggled(True)` 这个瞬间，"第一个 checked"很可能还是**旧的**那个，
        算出来的框 == 片的当前位置 → 被下面的"目标没变就不重播"判据吃掉，
        **动画直接不播**（症状：点「自定义」要再点一次才滑过去）。

        所以目标按优先级取：
          1. `btn` —— 刚刚变成选中的那个按钮（`_on_toggled` 用 `sender()` 拿到）；
          2. `self._owner` —— 最近一次变成选中的按钮，给 `_place_now` /
             `_sync` 这些"没有 sender"的路径用（比如 resize 重排后）；
          3. 兜底才扫"第一个 checked"（构造期、或 `_owner` 已失效时）。
        """
        for cand in (btn, self._owner):
            r = self._rect_of(cand)
            if r is not None:
                return r
        for b in self._buttons:
            r = self._rect_of(b)
            if r is not None:
                return r
        return None

    def _place_now(self, r=None):
        """把片直接放到目标上（不做动画）。"""
        if r is None:
            r = self._target_rect()
        if r is None:
            return
        self._from = self._to = r
        self._t = 1.0
        self._pending = False
        self.update()

    def _animating(self):
        return self._anim.state() == QAbstractAnimation.State.Running

    def _sync(self):
        if self.size() != self._host.size():
            self.setGeometry(0, 0, self._host.width(), self._host.height())
        if self._animating():
            return          # 动画中别打断，跑完 finished 会再落一次位
        self._place_now()

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() in (
                QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            self._sync()
        return False

    # -- 动画 ---------------------------------------------------------------
    def _on_toggled(self, checked, btn=None):
        if not checked:
            return
        # `btn` 由 `rebuild()` 里的闭包直接带进来 —— 见 `_target_rect` 的
        # 说明：点击瞬间旧按钮可能还没被取消，光扫第一个 checked 会扫到旧的。
        if btn is None:                       # 兼容"直接手动调用"的写法
            sent = self.sender()
            btn = sent if isinstance(sent, QAbstractButton) else None
        if btn is not None:
            self._owner = btn
        r = self._target_rect(btn)
        if r is None:
            return
        if self._pending or self._from is None:
            self._place_now(r)          # 首次：不播动画，直接落位
            return
        if self._to is not None and r == self._to:
            return
        self._from = self._current()
        self._to = r
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def _on_value(self, value):
        self._t = float(value)
        self.update()

    def _current(self):
        if self._from is None or self._to is None:
            return None
        k = self._t
        # 弹簧那点过冲（CURVE_SPRING_SOFT 峰值 1.083）**不加在位置上**：
        # 选中项常常就是最后一个挡位，片一到终点右缘就贴着容器边了，再过冲
        # 8% 会直接撞墙（Qt 不裁剪子控件绘制，糊到旁边控件上去）。改成把
        # 溢出转成"宽度收一点"—— 片像撞到东西那样顿一下再弹回来，
        # 果冻感一样有，还不会出界。
        over = max(0.0, k - 1.0)
        pk = min(1.0, max(0.0, k))
        r = QRectF(self._from)
        r.setX(self._from.x() + (self._to.x() - self._from.x()) * pk)
        r.setY(self._from.y() + (self._to.y() - self._from.y()) * pk)
        r.setWidth(self._from.width()
                   + (self._to.width() - self._from.width()) * pk)
        r.setHeight(self._from.height()
                    + (self._to.height() - self._from.height()) * pk)
        if over > 0:
            shrink = min(0.06, over * 0.72)
            cx, cy = r.center().x(), r.center().y()
            r.setWidth(r.width() * (1.0 - shrink))
            r.moveCenter(QPointF(cx, cy))
        return r

    # -- 绘制 ---------------------------------------------------------------
    def paintEvent(self, event):
        r = self._current()
        if r is None or r.width() <= 1 or r.height() <= 1:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        liq = theme.liquid_palette()
        # 比侧栏那条选中条更实一点（TINT_ALPHA 214 vs 196）：分段片只有
        # 几十像素宽，太透就看不出"选中"了；色散也压到 44，两条 1px 彩线
        # 在小面积上会变成噪点。
        glass.paint_soft_shadow(p, r, radius=self.RADIUS, offset=1, spread=2,
                                alpha=52)
        glass.paint_liquid_glass(
            p, r, radius=self.RADIUS,
            tint=liq["tint"], edge=liq["edge"],
            tint_alpha=self.TINT_ALPHA,
            gloss_alpha=118, top_alpha=176, border_alpha=150,
            glow_alpha=104, glow_thickness=7,
            chroma_alpha=44,
            shade_alpha=54, shade_thickness=5)
        p.end()


class SegmentedControl(QFrame):
    """分段选择器：互斥选项组（选中片会滑动，见 `SegmentSlider`）。"""

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
            kb_focus(b)
            self._group.addButton(b)
            self._buttons[value] = b
            lay.addWidget(b)
            b.clicked.connect(lambda checked=False, v=value: self.changed.emit(v))

        if current in self._buttons:
            self._buttons[current].setChecked(True)
        # 按钮全部就位后再装滑动片（它靠 findChildren 找按钮）
        self.slider = SegmentSlider(self)

    def set_current(self, value):
        if value in self._buttons:
            self._buttons[value].setChecked(True)

    def current(self):
        for value, b in self._buttons.items():
            if b.isChecked():
                return value
        return None


def kb_focus(widget):
    """把控件设成「只有 Tab 进来才拿焦点」。

    为什么需要：QSS 里给按钮补了键盘焦点环（见 theme_packs.INTERACTION_QSS），
    但 Qt 没有 CSS 的 `:focus-visible` —— `:focus` 在**鼠标点一下**之后也会命中，
    于是"刚点过的按钮一直亮着蓝边"，看起来像是被选中了。
    `Qt.TabFocus` 只接受 Tab / 快捷键来的焦点，鼠标点击不落焦点，
    正好就是 `:focus-visible` 的语义。

    副作用很小：按钮仍然响应单击与空格/回车（键盘用户按 Tab 能定位到），
    只是鼠标点完不再留下焦点。
    """
    try:
        widget.setFocusPolicy(Qt.TabFocus)
    except RuntimeError:
        pass
    return widget


def primary_button(text):
    b = QPushButton(text)
    b.setObjectName("PrimaryButton")
    b.setCursor(Qt.PointingHandCursor)
    return kb_focus(b)


def ghost_button(text):
    b = QPushButton(text)
    b.setObjectName("GhostButton")
    b.setCursor(Qt.PointingHandCursor)
    return kb_focus(b)


def danger_button(text):
    b = QPushButton(text)
    b.setObjectName("DangerButton")
    b.setCursor(Qt.PointingHandCursor)
    return kb_focus(b)


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
#: 被新提示顶上来时的重排时长。统一取自 ui.motion，别再就地写数字。
TOAST_SLIDE_MS = motion.TOAST_STACK

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


def _slide_to(toast, target, ms=None, easing=None, start=None):
    """把一条 Toast 平滑移到新位置。

    三个调用场景共用这一个函数（进场 / 被顶上来 / 退场），**不要**另起
    QPropertyAnimation：同一个 `pos` 属性上挂两条动画会互相抢帧，
    表现是"提示滑动时不时抖一下"，很难查。

    减弱动效时直接落位（位移是这条规则的适用对象），淡入淡出仍然保留。
    """
    if not _alive(toast):
        return
    try:
        if motion.reduced():
            toast.move(target)
            return
        anim = getattr(toast, "_slide_anim", None)
        if anim is not None and _alive(anim):
            anim.stop()
        anim = QPropertyAnimation(toast, b"pos", toast)
        anim.setDuration(int(ms if ms is not None else TOAST_SLIDE_MS))
        anim.setEasingCurve(motion.curve(easing or motion.CURVE_OUT))
        anim.setStartValue(start if start is not None else toast.pos())
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

    # 就位：新的这条落在最底部，已有的整体上移一条
    stack.insert(0, toast)
    _restack(parent, instant=toast)
    final_pos = toast.pos()

    # 进场：从**窗口下缘外面**滑上来 + 淡入。
    #
    # 改之前是"直接瞬移到最终位置，只有 opacity 从 0 到 1" —— 那是典型的
    # 传送式出现：一个没有来处的东西凭空淡出来。改成位移 + 淡入之后，
    # 提示就有了"从下面升起来"的空间来历。
    #
    # 起点取 `parent.height()`：提示的**上边缘正好与窗口下边缘齐平**，
    # 于是它完全在视野之外，再往上滑进画面。行程 = 自身高度 + 距底边留白，
    # 约等于 `translateY(100%)`（技能库里用的就是自身高度的百分比位移）。
    # 退场沿**同一条边、同一个落点**回去（见 _fade_out），进出严格对折。
    #
    # 数值依据：气泡属于"偶发"频次，可以走标准动效；260ms + 两端对称的 ease
    # （不是 ease-out）让它显得从容 —— 快慢要和控件的性格一致，提示是"通知"，
    # 不该像按钮那样弹。
    enter_y = parent.height() + 2
    if not motion.reduced():
        toast.move(QPoint(final_pos.x(), enter_y))
        _slide_to(toast, final_pos, ms=motion.TOAST_IN,
                  easing=motion.CURVE_STANDARD, start=toast.pos())

    # 注意先把不透明度压到 0 再交给 motion.fade —— 否则新控件特效的初值
    # 就是 1.0，1.0 → 1.0 的"淡入"什么都不会发生（这类假动画特别隐蔽：
    # 代码在跑、动画在播，只是看不出任何变化）。
    motion.fade_effect(toast).setOpacity(0.0)
    fade_in = motion.fade(toast, 1.0, motion.TOAST_IN)
    toast._fade_in_anim = fade_in

    def _fade_out():
        # 这条 Toast 可能已经被 _drop_toast 提前删了（同屏超过 TOAST_MAX 时
        # 挤掉最旧的），此时它的动画/特效连 C++ 对象一起没了——整段容错。
        if not _alive(toast):
            return
        try:
            # 退场：沿**同一条边、同一个落点**往下走 + 淡出，并且比进场快。
            # 回到 enter_y 而不是只挪自身高度，是为了让出场的路径与进场严格
            # 重合 —— 进出走两条不同的路线会让人觉得"来和去不是一个东西"。
            # 血泪：原来进场 180ms、退场 240ms —— 正好反了。慢的那一截应该
            # 留给"用户在决策"的时候，而提示退场是系统在对用户的动作收尾，
            # 拖越长越像界面没反应。
            _slide_to(toast, QPoint(final_pos.x(), enter_y),
                      ms=motion.TOAST_OUT, easing=motion.CURVE_OUT)
            out = motion.fade(toast, 0.0, motion.TOAST_OUT)
            out.finished.connect(lambda: _dismiss_toast(parent, toast))
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
