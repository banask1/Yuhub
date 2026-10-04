"""Yuhub 动效令牌（motion tokens）——一处定义，全项目取用。

规则来源
--------
Emil Kowalski 的设计工程技能库 <https://github.com/emilkowalski/skills>
（emil-design-eng / apple-design / review-animations）给出"该不该动、动多久"
的判据；Kyant0/AndroidLiquidGlass
<https://github.com/Kyant0/AndroidLiquidGlass> 给出"怎么动才弹"的手感。
落到 Qt 上要改写的只有"载体"，判据本身不变：

1. **内置缓动太弱。** Qt 的 `OutCubic` / `InOutQuad` 就是 CSS 里那套被点名
   说"不够劲"的内置曲线，缺少让动效显得"有意为之"的收尾。统一换成自定义
   立方贝塞尔。
2. **进入/退出用强 ease-out，屏幕上的位移用 ease-in-out。**
   `ease-in` 起步慢 —— 恰好把用户盯得最紧的那一瞬间拖住了，UI 里永不使用。
3. **UI 动效一律 < 300ms。** 180ms 的下拉比 400ms 的"感觉更快"，即便实际
   耗时一样。感知速度就是速度。
4. **退出必须比进入快。** 慢的地方留给"用户在决策"（按住不放、二次确认），
   快的地方留给"系统在响应"。提示气泡进场 260ms、退场 170ms 就是这个道理。
5. **永远不要从 scale(0) / 零位移进场。** 现实里没有东西是从"无"里长出来的，
   起点留一点形状（0.95 左右）或一点位移，进场才像"到来"而不是"凭空出现"。
6. **只动 pos / geometry / opacity。** 动画 width / height / margin 会让每一帧
   都重跑布局，代价比位移高一个量级。
7. **果冻感 = 过冲一次再收回。** 直线到达的东西像"被搬过去"，冲过目标、
   弹回一点、再停稳的东西才像"有质量"。见下面的弹簧曲线。

Qt 侧的一个硬约束：QSS **不支持** `transition` / `animation`，所有过渡都得用
QPropertyAnimation 或逐帧自绘。所以"按钮按下去要有反馈"在 Qt 里的正解是 QSS
的 `:pressed` 伪态（它是同步立即重绘的，正好满足"按下瞬间就要反馈"），而
"移动 / 淡入淡出 / 弹性"这类才走这里的动画助手。

动效强度固定为「完整」
----------------------
v0.15beta 起**不再提供强度调节**（原设置页的「完整 / 减弱 / 跟随系统」三档
已移除）。`reduced()` 恒返回 False，保留这个查询口子是因为全项目有多处
`if motion.reduced(): ...` 的守卫——留着它，以后想加回来只改这一个函数。
"""

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
)

# ---------------------------------------------------------------------------
# 缓动曲线
# ---------------------------------------------------------------------------
# 注意 `addCubicBezierSegment` 只需要给两个控制点和终点，起点隐含为 (0, 0)，
# 正好对上 CSS 的 `cubic-bezier(x1, y1, x2, y2)` 四个参数。
_CURVES = {
    # 进入 / 退出：起步快、收尾干脆，最"跟手"
    "out": (0.23, 1.0, 0.32, 1.0),
    # 屏幕上的位移 / 形变：自然加减速
    "in_out": (0.77, 0.0, 0.175, 1.0),
    # 抽屉 / 侧栏这类"整块面板挪位"（取自 Ionic Framework 的 iOS 曲线）
    "drawer": (0.32, 0.72, 0.0, 1.0),
    # hover / 颜色变化：比内置 ease 更有弹性，但仍是两端对称的
    "standard": (0.4, 0.0, 0.2, 1.0),
}

CURVE_OUT = "out"
CURVE_IN_OUT = "in_out"
CURVE_DRAWER = "drawer"
CURVE_STANDARD = "standard"

# ---------------------------------------------------------------------------
# 弹簧曲线（果冻感）
# ---------------------------------------------------------------------------
# 为什么不用 `QEasingCurve.OutElastic`：内置弹性曲线振幅大、来回振荡三四次，
# 用在按钮/指示条上会显得廉价而且"停不下来"。真实手感（iOS / Android 的
# physics-based animation）是**阻尼弹簧**：冲过目标、小幅回落、迅速收敛。
#
# 为什么不用 `setCustomType(回调)`：那会让每一帧从 C++ 回调进 Python，
# 60fps 下每个动画多 60 次解释器进出；而且 QEasingCurve 会被 Qt 拷贝一份，
# Python 回调的存活期依赖 PySide6 的引用保持机制，是个不必要的风险点。
# `BezierSpline` 支持**多段**三次贝塞尔，纯 C++ 求值，形状足够逼近弹簧。
#
# 每段是 (控制点1, 控制点2, 段终点)，t 在各段之间**均分**。
_SPRINGS = {
    # 果冻：过冲 16% → 回落 5% → 收敛。侧栏指示条、开关滑块用。
    "spring": (
        ((0.32, 1.44), (0.55, 1.22), (0.66, 1.00)),
        ((0.78, 0.90), (0.90, 0.99), (1.00, 1.00)),
    ),
    # 轻弹：过冲 8%、几乎不回弹。页面进场这类"面积大、次数多"的场合用，
    # 大面积上做 16% 的过冲会像画面在抖。
    "spring_soft": (
        ((0.28, 1.34), (0.48, 1.10), (0.62, 1.00)),
        ((0.78, 0.985), (0.90, 1.00), (1.00, 1.00)),
    ),
}

CURVE_SPRING = "spring"
CURVE_SPRING_SOFT = "spring_soft"


def curve(name=CURVE_OUT):
    """取一条 QEasingCurve。名字不认识时退回强 ease-out（永不返回线性）。"""
    segs = _SPRINGS.get(name)
    if segs:
        c = QEasingCurve()
        c.setType(QEasingCurve.BezierSpline)
        for p1, p2, end in segs:
            c.addCubicBezierSegment(
                QPointF(p1[0], p1[1]), QPointF(p2[0], p2[1]),
                QPointF(end[0], end[1]))
        return c
    pts = _CURVES.get(name) or _CURVES[CURVE_OUT]
    c = QEasingCurve()
    c.setType(QEasingCurve.BezierSpline)
    c.addCubicBezierSegment(
        QPointF(pts[0], pts[1]), QPointF(pts[2], pts[3]), QPointF(1.0, 1.0)
    )
    return c


def curve_points(name=CURVE_OUT):
    """取首段的 (x1, y1, x2, y2)。

    单段曲线就是它自己；弹簧取**第一段**的控制点。自检用它来断言
    "用的是自定义曲线而不是内置枚举"，以及"没有一条曲线起步慢"——
    弹簧首段的 y 都 > 1，不会被误判成 ease-in。
    """
    segs = _SPRINGS.get(name)
    if segs:
        (ax, ay), (bx, by), _ = segs[0]
        return (ax, ay, bx, by)
    return _CURVES.get(name) or _CURVES[CURVE_OUT]


def curve_segments(name=CURVE_OUT):
    """取完整的段定义：单段曲线返回 1 段，弹簧返回 2 段。

    形状断言（"这是过冲后回收的弹簧，不是直线"）用它。
    """
    segs = _SPRINGS.get(name)
    if segs:
        return tuple((tuple(p1), tuple(p2), tuple(end)) for p1, p2, end in segs)
    ax, ay, bx, by = _CURVES.get(name) or _CURVES[CURVE_OUT]
    return (((ax, ay), (bx, by), (1.0, 1.0)),)


def curve_names():
    return tuple(_CURVES.keys()) + tuple(_SPRINGS.keys())


def is_spring(name):
    """这条曲线是不是弹簧（过冲型）。自检与绘制逻辑都靠它区分。"""
    return name in _SPRINGS


# ---------------------------------------------------------------------------
# 时长预算（毫秒）
# ---------------------------------------------------------------------------
# 对照技能库的表格：按压 100–160 / 气泡 125–200 / 下拉 150–250 / 弹窗 200–500，
# 且一律不超过 300。启动画面不属于"交互响应"，可以更长。
#
# 弹簧曲线的时长要给足"余韵"：过冲峰值出现在 t≈0.49，也就是一半时长的位置。
# 300ms 的弹簧 = 147ms 冲到最远、再花 150ms 弹回来停稳 —— 手感上仍然是
# "立刻响应"，但多了质量感。
PRESS = 110        # 按钮按下去的形变
HOVER = 140        # hover 染色
TOOLTIP = 160      # 工具提示
POPOVER = 180      # 小浮层
DROPDOWN = 200     # 下拉 / 选择器
DIALOG_IN = 220    # 弹窗进场
DIALOG_OUT = 160   # 弹窗退场（比进场快）
TOAST_IN = 260     # 提示气泡进场（略慢，走 ease 更"从容"）
TOAST_OUT = 170    # 提示气泡退场
TOAST_STACK = 200  # 已有气泡被顶上来的重排
NAV = 180          # 页面淡入
PAGE_IN = 260      # 页面弹入的位移（轻弹，面积大所以只过冲 8%）
PILL = 300         # 侧栏选中指示条的弹簧滑动（果冻，过冲 16%）
SWITCH = 280       # 开关滑块（弹簧，并且滑动中被拉长）
SIDEBAR = 220      # 侧栏展开 / 折叠

#: 交互类动效的硬上限（超过这个数的都算"拖"）
UI_BUDGET = 300


def budget(kind):
    """按用途取时长。`kind` 传下面的常量名（如 'TOAST_OUT'）。"""
    return int(globals().get(kind, 0) or 0)


# ---------------------------------------------------------------------------
# 动效强度
# ---------------------------------------------------------------------------
def reduced():
    """是否应当减弱动效。

    恒为 False —— 本项目固定使用完整动效（v0.15beta 起移除了强度调节入口，
    见模块开头说明）。保留函数是为了让全项目那些
    `if motion.reduced(): ...` 的守卫继续可读，而不是散落一地死分支。
    """
    return False


def dur(ms, kind="move"):
    """按用途取时长。

    `kind` 参数保留是为了兼容调用点（'move' / 'fade'）——以前它决定
    "减弱动效时清零还是压缩"，现在强度固定，两档返回同一个值。
    """
    return int(ms)


def stagger(index, step=40):
    """列表 / 网格成组进场时的错峰延迟。

    30–80ms 之间即可；再长整组就会显得"卡"。注意错峰是装饰性的，
    **绝不能**用它阻塞交互（Qt 里对应"别用 QTimer 排队感"）。
    """
    return max(0, int(index)) * max(0, int(step))


# ---------------------------------------------------------------------------
# 动画助手
# ---------------------------------------------------------------------------
def animate(widget, prop, to, ms, easing=CURVE_OUT, start=None, on_done=None):
    """给 `widget` 的某个 Qt 属性做一次过渡，返回动画对象（调用方需保引用）。

    保留引用是必须的：QPropertyAnimation 一旦被 GC，动画会**静默停在半路**
    （现象是"控件偶尔卡在中间位置"，极难复现）。调用方把它挂到 widget 上
    （如 `w._anim = anim`）最稳。
    """
    ms = dur(ms, "move" if prop != b"opacity" else "fade")
    if ms <= 0:
        try:
            widget.setProperty(prop.decode(), to)
        except Exception:                   # noqa: BLE001
            pass
        if on_done is not None:
            on_done()
        return None

    anim = QPropertyAnimation(widget, prop, widget)
    anim.setDuration(ms)
    anim.setEasingCurve(curve(easing))
    if start is not None:
        anim.setStartValue(start)
    anim.setEndValue(to)
    if on_done is not None:
        anim.finished.connect(on_done)
    anim.start()
    return anim


def fade_effect(widget):
    """取 widget 的不透明度特效，没有就装一个。

    一个 QWidget 只能挂一个 QGraphicsEffect，所以不能各建各的 ——
    Toast 的淡出和"被顶上来"的重排动画曾经就会互相踩。
    """
    from PySide6.QtWidgets import QGraphicsOpacityEffect

    eff = widget.graphicsEffect()
    if isinstance(eff, QGraphicsOpacityEffect):
        return eff
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(1.0)
    widget.setGraphicsEffect(eff)
    return eff


def fade(widget, to, ms, easing=CURVE_STANDARD, on_done=None):
    """淡到指定不透明度。返回动画对象（同样要保引用）。"""
    eff = fade_effect(widget)
    ms = dur(ms, "fade")
    from PySide6.QtCore import QPropertyAnimation as _A

    anim = _A(eff, b"opacity", widget)
    anim.setDuration(max(1, ms))
    anim.setEasingCurve(curve(easing))
    anim.setStartValue(float(eff.opacity()))
    anim.setEndValue(float(to))
    if on_done is not None:
        anim.finished.connect(on_done)
    anim.start()
    return anim
