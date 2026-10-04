"""Yuhub 毛玻璃（frosted glass）绘制工具。

为什么不能只写 QSS
-------------------
参考实现（gitee greyd097/yzrt「纯CSS液态玻璃」）靠三样东西做出玻璃感：

    backdrop-filter: blur(2px)                     ← 背景模糊
    inset 2px -2px 1px -1px rgba(255,255,255,.9)   ← 多层边缘高光（玻璃厚度）
    ::after { 45° 白色渐变; filter: blur(3px) }     ← 表面反光

Qt 的 QSS **一样都没有**：没有 `backdrop-filter`，没有 `box-shadow`，
连 `filter` 也只有有限的几个枚举值。所以这里全部用 QPainter 手绘等效：

    1) 背景模糊   -> 抓取玻璃「背后那一层」的渲染结果，降采样再平滑放大。
                     Qt 的 QGraphicsBlurEffect 必须塞进 QGraphicsScene 才能用，
                     开销大；降采样-放大在「只要糊掉纹理」的场景足够，
                     而且是纯像素操作，代价极低。
    2) 边缘高光   -> 手绘 1px 亮边 + 顶部高光线 + 一圈内描边。
    3) 表面反光   -> QLinearGradient 沿对角线，左上 / 右下最亮、中段透明
                     （与参考实现 ::after 的 stop 位置一一对应）。

「背后那一层」的约定
--------------------
玻璃层必须盖在一个**静态且不含自己**的兄弟控件上，模糊才有内容可糊。
所以窗口里铺了一个 `BackdropLayer`（纹理层），各玻璃控件都从它取背景。
如果盖在纯色上，模糊后还是纯色 —— 毛玻璃就白做了。

坐标与缓存
----------
`BackdropSource` 负责把「某控件在背后层里的矩形」抓成图并缓存；
缓存键含 (背板版本, 尺寸, 圆角)，背板重绘或窗口尺寸变化时自动失效。
"""

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
    QRadialGradient,
)


# ---------------------------------------------------------------------------
# 颜色小工具
# ---------------------------------------------------------------------------
def qcolor(value, fallback="#000000"):
    """宽松地把主题色板里的值转成 QColor（色板可能给 'rgba(...)' 字符串）。"""
    c = QColor(value) if value else QColor()
    if not c.isValid():
        c = QColor(fallback)
    return c


def rgba(color, alpha):
    """把颜色（QColor / '#rrggbb' / 'rgba(...)'）转成带 alpha 的 QColor。

    alpha 取 0-255 整数。用 setAlpha 而不是 setAlphaF，避免主题给的是
    rgba 字符串时浮点精度掉色。
    """
    c = QColor(color) if not isinstance(color, QColor) else QColor(color)
    if not c.isValid():
        c = QColor("#ffffff")
    c.setAlpha(max(0, min(255, int(alpha))))
    return c


def mix(c1, c2, t):
    """两色线性混合：t=0 取 c1，t=1 取 c2。解析不了就返回 c1。

    用来从主题的 accent 推导"浅蓝"——直接写死一个浅蓝会在用户换主题包时
    变成一块跟整体配色无关的色块。
    """
    a = qcolor(c1, "#3b82f6")
    b = qcolor(c2, "#ffffff")
    k = max(0.0, min(1.0, float(t)))
    return QColor(
        int(round(a.red() + (b.red() - a.red()) * k)),
        int(round(a.green() + (b.green() - a.green()) * k)),
        int(round(a.blue() + (b.blue() - a.blue()) * k)),
    )


# ---------------------------------------------------------------------------
# 液态玻璃
# ---------------------------------------------------------------------------
# 与「毛玻璃」的区别在**光学层次**：毛玻璃的核心是"背后的东西被糊掉了"，
# 液态玻璃的核心是**边缘**——一圈有厚度的光带 + 极淡的色散，看起来像一块
# 能把光掰弯的实体。
#
# 参考实现 Kyant0/AndroidLiquidGlass 的 drawBackdrop 一共叠了这些东西：
#   vibrancy()            提饱和     -> 本项目不做（逐像素，代价不值）
#   blur(2dp)             轻模糊     -> 复用 blur_pixmap
#   lens(12dp, 24dp)      边缘折射   -> 见 paint_liquid_refract
#   Highlight.Ambient     环境高光   -> 对角反光 + 顶部高光线
#   InnerShadow(4dp)      内阴影     -> 下/右内侧暗带（让玻璃"鼓"起来）
#   chromaticAberration   色散       -> 青 / 品红各一条细边
#
# 缩放值（1.04 折射、13px 光带、54 色散）都是按本项目控件尺寸调的：侧栏
# 指示条高 34px 左右，光带超过 15px 就会把整块染成一片白。

LIQUID_TINT_FALLBACK = "#9ecbff"
#: 色散用的两条边：左缘偏青、右缘偏品红。固定值 —— 色散是"白光被掰开"
#: 的结果，跟主题色无关（换成绿色主题，玻璃边缘该有色散还是有）。
CHROMA_COOL = QColor("#79e2ff")
CHROMA_WARM = QColor("#ff9ad8")


def liquid_colors(palette):
    """液态玻璃的色组（浅蓝）。

    用户的要求是"选中效果不要原来那种实心蓝，要浅蓝的液态玻璃"。所以这里
    不直接用 accent 本色，而是把它往白里提两档：

        tint   混 52% 白 —— 玻璃底色，浅蓝
        edge   混 74% 白 —— 边缘光带色，接近白

    这样用户换主题包时，选中效果仍然是"同一色系的浅色玻璃"，不会突然冒出
    一块跟整体配色无关的蓝。

    优先读色板里的 `liquid_tint` / `liquid_edge`（theme.build_qss 注入的
    就是这一套，保证 QSS 画的分段按钮和自绘的指示条是同一种浅蓝）；
    色板里没有才现推。

    返回 (tint, edge, chroma_a, chroma_b)。
    """
    p = palette or {}
    accent = p.get("accent") or "#3b82f6"

    def _pick(key, t):
        c = QColor(p.get(key)) if p.get(key) else QColor()
        return c if c.isValid() else mix(accent, "#ffffff", t)

    return (_pick("liquid_tint", 0.52),
            _pick("liquid_edge", 0.74),
            CHROMA_COOL, CHROMA_WARM)


def _inner_band(painter, rect, thickness, side, color, alpha):
    """在 rect 的某条**内缘**画一条渐隐色带（等效 CSS 的 inset shadow）。

    side 取 top / bottom / left / right。色带从边缘向里渐隐，所以画在
    边缘那一侧 alpha 最高。`color` 给白就是高光，给黑就是内阴影。
    """
    if alpha <= 0 or thickness <= 0:
        return
    x, y = float(rect.left()), float(rect.top())
    w, h = float(rect.width()), float(rect.height())
    if w <= 2 or h <= 2:
        return
    t = float(min(thickness, max(1.0, min(w, h) / 3.0)))

    if side == "top":
        g = QLinearGradient(x, y, x, y + t)
        g.setColorAt(0.0, rgba(color, alpha))
        g.setColorAt(1.0, rgba(color, 0))
        box = QRectF(x, y, w, t)
    elif side == "bottom":
        g = QLinearGradient(x, y + h - t, x, y + h)
        g.setColorAt(0.0, rgba(color, 0))
        g.setColorAt(1.0, rgba(color, alpha))
        box = QRectF(x, y + h - t, w, t)
    elif side == "left":
        g = QLinearGradient(x, y, x + t, y)
        g.setColorAt(0.0, rgba(color, alpha))
        g.setColorAt(1.0, rgba(color, 0))
        box = QRectF(x, y, t, h)
    else:                                   # right
        g = QLinearGradient(x + w - t, y, x + w, y)
        g.setColorAt(0.0, rgba(color, 0))
        g.setColorAt(1.0, rgba(color, alpha))
        box = QRectF(x + w - t, y, t, h)

    painter.setPen(Qt.NoPen)
    painter.setBrush(g)
    painter.drawRect(box)


def paint_liquid_glass(painter, rect, radius=8.0,
                       tint=LIQUID_TINT_FALLBACK, edge="#eaf4ff",
                       tint_alpha=176, gloss_alpha=124, top_alpha=192,
                       border_alpha=158, glow_alpha=112,
                       glow_thickness=13, chroma_alpha=56,
                       shade_alpha=62, shade_thickness=10):
    """画一块「液态玻璃」面（**不含背景模糊**，那部分由调用方先铺好）。

    层次自下而上：
      1) 浅蓝底色
      2) 45° 对角反光 —— 玻璃表面那道从左上扫到右下的大高光
      3) 上 / 左内侧光带 —— **厚度感的主要来源**。1px 描边在大面积控件上
         等于没有（本项目在毛玻璃那轮已经吃过这个亏）
      4) 下 / 右内侧暗带（内阴影）—— 让玻璃"鼓"起来而不是像张贴纸
      5) 顶部一条更亮的 1px 高光线
      6) 色散：左缘偏青、右缘偏品红，做得极淡 —— 液态玻璃的签名
      7) 外圈 1px 亮边

    `rect` 与 `radius` 决定形状（胶囊传 radius = 高度一半）。
    """
    if rect.width() <= 0 or rect.height() <= 0:
        return

    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    # 必须 IntersectClip：调用方可能已经设了"裁掉窗口外角"的裁剪，
    # 默认的 ReplaceClip 会把它整个丢掉（毛玻璃那轮踩过，见 paint_glass）。
    painter.setClipPath(path, Qt.IntersectClip)

    # 1) 底色
    painter.fillPath(path, rgba(tint, tint_alpha))

    # 2) 对角反光
    g = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
    g.setColorAt(0.0, rgba("#ffffff", gloss_alpha))
    g.setColorAt(0.22, rgba("#ffffff", gloss_alpha // 4))
    g.setColorAt(0.50, rgba("#ffffff", 0))
    g.setColorAt(0.76, rgba("#ffffff", gloss_alpha // 10))
    g.setColorAt(1.0, rgba("#ffffff", int(gloss_alpha * 0.66)))
    painter.fillPath(path, g)

    # 3) 内侧光带（上强、左次强）
    _inner_band(painter, rect, glow_thickness, "top", edge, glow_alpha)
    _inner_band(painter, rect, glow_thickness, "left", edge,
                int(glow_alpha * 0.72))

    # 4) 内阴影（下暗、右次暗）—— 让玻璃有"凸起"的体积
    _inner_band(painter, rect, shade_thickness, "bottom", "#0b1b30",
                shade_alpha)
    _inner_band(painter, rect, shade_thickness, "right", "#0b1b30",
                int(shade_alpha * 0.55))

    # 5) 顶部高光线
    if top_alpha > 0 and rect.height() > 6:
        pen = QPen(rgba("#ffffff", top_alpha))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        inset = int(min(radius * 1.2, rect.width() / 3))
        painter.drawLine(
            QPoint(int(rect.left() + inset), int(rect.top()) + 1),
            QPoint(int(rect.right() - inset), int(rect.top()) + 1))

    # 6) 色散：左青右品红。宽度给 1px、alpha 很低——它是"一眼觉得像玻璃"
    #    的暗号，一旦看得清颜色就变成廉价滤镜了。
    if chroma_alpha > 0 and rect.width() > 10 and rect.height() > 6:
        top = int(rect.top() + radius * 0.55)
        bot = int(rect.bottom() - radius * 0.55)
        if bot > top:
            pen = QPen(rgba(CHROMA_COOL, chroma_alpha))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawLine(int(rect.left()) + 1, top,
                             int(rect.left()) + 1, bot)
            pen = QPen(rgba(CHROMA_WARM, chroma_alpha))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawLine(int(rect.right()) - 2, top,
                             int(rect.right()) - 2, bot)

    painter.restore()

    # 7) 外圈亮边
    if border_alpha > 0:
        pen = QPen(rgba(edge, border_alpha))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(
            QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


# ---------------------------------------------------------------------------
# 背景模糊
# ---------------------------------------------------------------------------
def blur_pixmap(pm, radius=10.0):
    """近似高斯模糊：降采样 → 再降一半 → 平滑放大回原尺寸。

    radius 越大缩得越狠，糊得越厉害。两级降采样比单级更接近高斯，
    且仍然只是两次 scaled()，比真卷积快一个数量级。
    """
    if pm is None or pm.isNull() or radius <= 0:
        return pm
    w, h = pm.width(), pm.height()
    if w < 4 or h < 4:
        return pm
    factor = max(1.0, float(radius) / 2.0)
    tw = max(1, int(w / factor))
    th = max(1, int(h / factor))
    img = pm.toImage()
    img = img.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    img = img.scaled(max(1, tw // 2), max(1, th // 2),
                     Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    img = img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return QPixmap.fromImage(img)


# ---------------------------------------------------------------------------
# 玻璃面板绘制
# ---------------------------------------------------------------------------
def paint_glass(painter, rect, radius=8.0, tint="#ffffff", tint_alpha=20,
                gloss_alpha=105, top_alpha=150, border_alpha=95,
                inner_alpha=52, matte=None, matte_alpha=110):
    """在 rect 上画一块毛玻璃面板（**不含背景模糊**，那部分由调用方先铺好）。

    参数
    ----
    tint / tint_alpha    底色。浅色主题给白、深色主题给白（低 alpha）都行；
                         `matte` 用来额外叠一层主题色（比如选中项的 accent）。
    gloss_alpha          45° 对角反光的强度（参考实现里的 ::after）。
    top_alpha            顶部那道 1px 高光线，模拟光从左上来。
    border_alpha         外圈 1px 亮边，模拟玻璃边缘的环境光折射。
    inner_alpha          内缩 1px 的第二圈描边，做出「玻璃有厚度」的层次。
    """
    if rect.width() <= 0 or rect.height() <= 0:
        return

    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    # IntersectClip 而不是默认的 ReplaceClip：调用方（paint_glass_panel）可能
    # 已经设了"裁掉窗口外角"的裁剪，这里必须叠加而不是把它丢掉——
    # 否则圆角外会被这层薄纱重新填上色，凸出的角又回来了。
    painter.setClipPath(path, Qt.IntersectClip)

    # 1) 半透明底色
    painter.fillPath(path, rgba(tint, tint_alpha))

    # 2) 主题色薄纱（选中项用 accent 染色）
    if matte is not None and matte_alpha > 0:
        painter.fillPath(path, rgba(matte, matte_alpha))

    # 3) 45° 对角反光：左上、右下最亮，中段完全透明
    #    参考实现用 --tr: 25% 控制高光的"收束点"，这里对应 0.25 / 0.72。
    g = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
    g.setColorAt(0.0, rgba("#ffffff", gloss_alpha))
    g.setColorAt(0.25, rgba("#ffffff", gloss_alpha // 5))
    g.setColorAt(0.50, rgba("#ffffff", 0))
    g.setColorAt(0.74, rgba("#ffffff", gloss_alpha // 8))
    g.setColorAt(1.0, rgba("#ffffff", int(gloss_alpha * 0.72)))
    painter.fillPath(path, g)

    # 4) 顶部高光线（单独一条，比整圈描边更亮）
    if top_alpha > 0 and rect.height() > 6:
        gloss_pen = QPen(rgba("#ffffff", top_alpha))
        gloss_pen.setWidthF(1.0)
        painter.setPen(gloss_pen)
        painter.drawLine(
            QPoint(int(rect.left() + radius * 1.2), int(rect.top()) + 1),
            QPoint(int(rect.right() - radius * 1.2), int(rect.top()) + 1),
        )

    painter.restore()

    # 5) 两圈描边（外圈亮边 + 内圈厚度感）
    if border_alpha > 0:
        pen = QPen(rgba("#ffffff", border_alpha))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(
            QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

    if inner_alpha > 0 and rect.width() > 4 and rect.height() > 4:
        pen = QPen(rgba("#ffffff", inner_alpha))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(
            QRectF(rect).adjusted(1.5, 1.5, -1.5, -1.5),
            max(0.0, radius - 1.0), max(0.0, radius - 1.0))


def paint_soft_shadow(painter, rect, radius=8.0, offset=3, spread=2,
                      alpha=70, color="#000000"):
    """玻璃面下缘投影。等效 CSS 的 `0 4px 8px rgba(0,0,0,.2)`。

    做法：叠三层逐层外扩、逐层变淡的圆角矩形。层数少、无模糊计算，
    但叠出来的灰度梯度足够骗过眼睛（本来就是很淡的一层影）。
    """
    if rect.width() <= 0 or rect.height() <= 0:
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    for i in range(spread, 0, -1):
        a = int(alpha * (1.0 - (i - 1) / float(spread + 1)) / spread)
        if a <= 0:
            continue
        painter.setBrush(rgba(color, a))
        painter.drawRoundedRect(
            QRectF(rect).adjusted(-i, -i + offset, i, i + offset),
            radius + i, radius + i)
    painter.restore()


# ---------------------------------------------------------------------------
# 背板纹理
# ---------------------------------------------------------------------------
GRID_CELL = 55          # 与参考实现的 background-size: 55px 55px 一致


def glass_params(palette):
    """按深浅模式给出玻璃的底色 / 光泽强度。

    对齐参考实现的两条关键写法：

        background: rgba(255, 255, 255, 0.04);   ← 底几乎全透
        filter: brightness(0.9);                 ← 再整体压暗一点

    所以深色主题下玻璃是「略暗的透明白」（dim_alpha 用黑纱等效 brightness），
    不能直接糊一层白上去——那样会变成一块浅灰条，跟深色窗口完全不搭
    （第一版就是这么翻车的）。浅色主题底下本来就是浅灰，玻璃要**比底略亮**
    才看得出来，所以 tint 给得高、也不压暗。

    返回 (tint_alpha, gloss_alpha, dim_alpha)。
    """
    if is_dark(palette):
        return 12, 58, 30
    return 46, 118, 0


def paint_edge_glow(painter, rect, radius=8.0, thickness=14,
                    top_alpha=96, left_alpha=64, bottom_alpha=26):
    """玻璃的内侧边缘光带。

    参考实现在 .box 上挂了四层 inset box-shadow（`inset 2px -2px 1px -1px
    rgba(255,255,255,.9)` 之类），高光有 4~6px 宽，所以 100px 的小玻璃块
    一眼就有"厚度"。只画 1px 描边是远远不够的——在 216px 宽的侧栏上，
    1px 高光几乎看不见（第一版就吃了这个亏）。

    这里用「上/左亮、下/右暗」两组渐变带等效那些 inset 阴影：
    光从左上来，玻璃上缘和左缘反光最强，下缘和右缘只剩下一点点透光。
    """
    if thickness <= 0 or rect.width() <= 4 or rect.height() <= 4:
        return
    thickness = int(min(thickness, rect.width() // 3, rect.height() // 3))
    if thickness <= 0:
        return

    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setClipPath(path, Qt.IntersectClip)

    # 上缘：白 -> 透明
    g = QLinearGradient(rect.left(), rect.top(),
                        rect.left(), rect.top() + thickness)
    g.setColorAt(0.0, rgba("#ffffff", top_alpha))
    g.setColorAt(1.0, rgba("#ffffff", 0))
    painter.setBrush(g)
    painter.drawRect(QRectF(rect.left(), rect.top(), rect.width(), thickness))

    # 左缘：白 -> 透明
    g = QLinearGradient(rect.left(), rect.top(),
                        rect.left() + thickness, rect.top())
    g.setColorAt(0.0, rgba("#ffffff", left_alpha))
    g.setColorAt(1.0, rgba("#ffffff", 0))
    painter.setBrush(g)
    painter.drawRect(QRectF(rect.left(), rect.top(), thickness, rect.height()))

    # 下缘：透明 -> 一层很淡的白（玻璃底部的透光）
    if bottom_alpha > 0:
        g = QLinearGradient(rect.left(), rect.bottom() - thickness,
                            rect.left(), rect.bottom())
        g.setColorAt(0.0, rgba("#ffffff", 0))
        g.setColorAt(1.0, rgba("#ffffff", bottom_alpha))
        painter.setBrush(g)
        painter.drawRect(QRectF(rect.left(), rect.bottom() - thickness,
                                rect.width(), thickness))

    painter.restore()


def draw_snapshot(painter, rect, snap, refract=1.0):
    """把背景快照贴到 rect 上；`refract > 1` 时轻微放大，做出透镜折射。

    AndroidLiquidGlass 的 `lens(12dp, 24dp)` 会把玻璃边缘的背景"掰弯"——
    玻璃下沿看到的是稍微偏外的内容。真折射要逐像素算，这里退一步用
    **整体轻微放大 + 居中裁剪**等效：中心内容基本不动，越靠边缘位移越大，
    看起来就是"透过一块厚玻璃"。1.03~1.05 足够，再大就露馅（文字会明显被拉）。
    """
    if snap is None or snap.isNull():
        return
    k = float(refract or 1.0)
    tw, th = int(rect.width()), int(rect.height())
    if abs(k - 1.0) < 1e-3 or tw <= 2 or th <= 2:
        painter.drawPixmap(rect.topLeft(), snap)
        return
    sw = max(tw, int(round(tw * k)))
    sh = max(th, int(round(th * k)))
    scaled = snap.scaled(sw, sh, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    # 放大后居中裁回去：这样位移量在中心为 0、边缘最大
    src = QRect((sw - tw) // 2, (sh - th) // 2, tw, th)
    painter.drawPixmap(rect, scaled, src)


def rounded_path(rect, radius, corners=("tl", "tr", "br", "bl")):
    """生成"只对指定角做圆角"的矩形路径（其余角保持直角）。

    为什么需要它：Qt 的 QSS `border-radius` **不会裁剪子控件**。窗口外壳
    `#AppFrame` 是圆角的，但贴在它里面的标题栏 / 侧栏都是矩形，于是会把
    外壳的圆角"补方"，在窗口四角露出直角——就是用户看到的"凸出来的角"。
    外壳的圆角半径又是 QSS 算的（跟设备像素比有关，实测比声明值小），
    没法直接查询，所以这里由调用方按主题的 window_radius 给一个略大的
    半径，宁可多裁一点（露出一点外壳底色），也绝不让玻璃凸出去。
    """
    x, y = float(rect.x()), float(rect.y())
    w, h = float(rect.width()), float(rect.height())
    r = max(0.0, float(radius))
    r = min(r, w / 2.0, h / 2.0)
    tl = r > 0 and "tl" in corners
    tr = r > 0 and "tr" in corners
    br = r > 0 and "br" in corners
    bl = r > 0 and "bl" in corners

    path = QPainterPath()
    path.moveTo(x + (r if tl else 0.0), y)
    path.lineTo(x + w - (r if tr else 0.0), y)
    if tr:
        path.arcTo(x + w - 2 * r, y, 2 * r, 2 * r, 90, -90)
    path.lineTo(x + w, y + h - (r if br else 0.0))
    if br:
        path.arcTo(x + w - 2 * r, y + h - 2 * r, 2 * r, 2 * r, 0, -90)
    path.lineTo(x + (r if bl else 0.0), y + h)
    if bl:
        path.arcTo(x, y + h - 2 * r, 2 * r, 2 * r, 270, -90)
    path.lineTo(x, y + (r if tl else 0.0))
    if tl:
        path.arcTo(x, y, 2 * r, 2 * r, 180, -90)
    path.closeSubpath()
    return path


def window_radius(palette, default=8):
    """从色板里取窗口圆角（色板里是 QSS 用的 '8px' 字符串），容错解析为 int。"""
    raw = palette.get("window_radius", default) if palette else default
    try:
        return max(0, int(str(raw).replace("px", "").strip()))
    except (TypeError, ValueError):
        return max(0, int(default))


def widget_origin(widget, backdrop):
    """widget 左上角在 backdrop 坐标系里的位置（取景用）。

    **不能用 `widget.mapTo(backdrop, QPoint(0, 0))`**：QWidget::mapTo 要求
    目标必须是该控件的**祖先**，而 BackdropLayer 跟标题栏/侧栏是兄弟子树，
    Qt 遇到这种情况会直接返回**全局坐标**（实测整体偏 (7,7)，等于外壳 6px
    留白 + 1px 边框），于是每块玻璃取到的都是"往右下挪了 7px"的纹理；
    贴着窗口右/下边缘的玻璃（侧栏底部）还会取到背板之外的像素。
    自己拿全局坐标相减，跟有没有父子关系无关，稳。
    """
    try:
        return (widget.mapToGlobal(QPoint(0, 0))
                - backdrop.mapToGlobal(QPoint(0, 0)))
    except (RuntimeError, TypeError):
        return QPoint(0, 0)


def paint_glass_panel(painter, widget, backdrop, src, blur=10.0,
                      tint_alpha=24, gloss_alpha=76, dim_alpha=0, radius=0.0,
                      edge=True, fallback="#12141a", corners=(),
                      corner_radius=None, border_alpha=0, top_alpha=0,
                      inner_alpha=0, edge_thickness=14, rect=None):
    """给一个控件画毛玻璃底：抓背后纹理 → 模糊 → 盖玻璃面色纱 → 内侧边缘光。

    抽成函数是因为侧栏、标题栏、选中指示条三处的取景/模糊/上色逻辑完全一样，
    只有模糊半径和光泽强度不同。

    `backdrop` 是"背后那一层"（BackdropLayer），**不能是本控件自己或它的子级**，
    否则会把自己已经画好的内容再糊一次，形成残影。取不到时退回实心底色，
    保证文字始终可读。

    `corners` 指定哪几个角要跟着窗口外壳一起圆掉（见 `rounded_path`）。
    贴着窗口边角的玻璃控件必须传，否则它会用直角把外壳的圆角盖掉。
    `corner_radius` 是裁剪用的半径；不给就沿用 `radius`。侧栏这类
    "面本身是直角、但外角要圆"的控件靠它区分。

    `border_alpha` / `top_alpha` / `inner_alpha` / `edge_thickness` 是给
    "小巧的玻璃贴片"（标题栏版本号那种）留的口子：大面板靠内侧边缘光带
    就够，但一块 20px 高的小片必须有一圈清楚的亮描边才看得出是块玻璃。

    `rect` 不给就画满整个控件；给了就按这块矩形画（贴片需要在自己四周留
    2px 画投影 —— 画到控件外面会被 Qt 裁掉，等于没画）。
    """
    rect = QRect(widget.rect()) if rect is None else QRect(rect)
    if rect.width() <= 0 or rect.height() <= 0:
        return None

    cr = radius if corner_radius is None else corner_radius
    face = QRect(rect.adjusted(0, 0, -1, -1))

    painter.save()
    if corners and cr > 0:
        # 裁掉外角：只影响贴窗口边缘的那几个角，内部边缘不动
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setClipPath(
            rounded_path(QRectF(rect), cr, corners), Qt.IntersectClip)
    if radius > 0:
        # 自己也是个圆角片（版本号玻璃贴片这种），就必须裁到自己的圆角形状里。
        # 血泪：背景快照是**整块矩形** drawPixmap 上去的，不裁的话圆角之外会
        # 露出四个"没上色的裸背板"小三角——侧栏选中条就是这么翻车的
        #（用户原话：「蓝色四角周围也没有毛玻璃效果」）。
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setClipPath(
            rounded_path(QRectF(rect), radius), Qt.IntersectClip)

    snap = None
    if backdrop is not None:
        try:
            tl = widget_origin(widget, backdrop) + rect.topLeft()
            snap = src.snapshot(backdrop, QRect(tl, rect.size()),
                                radius=blur,
                                stamp=getattr(backdrop, "version", 0))
        except (RuntimeError, TypeError):
            snap = None
    if snap is not None:
        painter.drawPixmap(rect.topLeft(), snap)
    else:
        painter.fillRect(rect, qcolor(fallback, "#12141a"))

    paint_glass(painter, face, radius=radius,
                tint="#ffffff", tint_alpha=tint_alpha,
                gloss_alpha=gloss_alpha, top_alpha=top_alpha,
                border_alpha=border_alpha, inner_alpha=inner_alpha,
                matte="#000000" if dim_alpha > 0 else None,
                matte_alpha=dim_alpha)
    if edge:
        paint_edge_glow(painter, face, radius=radius, thickness=edge_thickness)
    painter.restore()
    return snap


def paint_backdrop(painter, size, palette, cell=GRID_CELL):
    """画窗口底层纹理：极淡方格 + 两团柔光。

    这层的唯一使命是「让玻璃有东西可模糊」。没有它，玻璃盖在纯色上，
    模糊前后都是纯色，毛玻璃就等于白做。

    强度是调出来的：网格线大约 8% 不透明度，柔光约 17%。远看仍是干净的
    纯色底，但玻璃盖上去之后能看出被糊开的方格与明暗——这才是毛玻璃的
    "证据"。调更低就完全看不出来了（第一版就是调太低，等于没做）。
    """
    w, h = int(size.width()), int(size.height())
    if w <= 0 or h <= 0:
        return
    dark_mode = is_dark(palette)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, False)

    # 底色（保证背板自身就是不透明的，玻璃取到的背景才稳定）
    painter.fillRect(QRect(0, 0, w, h), qcolor(palette.get("window_top"), "#101216"))

    # 方格
    grid_alpha = 15 if dark_mode else 20
    grid_color = "#ffffff" if dark_mode else "#0b1220"
    pen = QPen(rgba(grid_color, grid_alpha))
    pen.setWidth(1)
    painter.setPen(pen)
    x = cell
    while x < w:
        painter.drawLine(x, 0, x, h)
        x += cell
    y = cell
    while y < h:
        painter.drawLine(0, y, w, y)
        y += cell

    # 两团柔光（右上 + 左下，用 accent 提色），给玻璃的模糊提供明暗变化。
    # 强度要克制：太强会把标题栏整条染成浅蓝，一眼看出"这不是玻璃，是块蓝条"。
    accent = qcolor(palette.get("accent"), "#3b82f6")
    glow_alpha = 18 if dark_mode else 15
    for cx, cy, rad in ((int(w * 0.88), int(h * 0.06), int(max(w, h) * 0.42)),
                        (int(w * 0.04), int(h * 0.96), int(max(w, h) * 0.40))):
        g = QRadialGradient(cx, cy, max(1, rad))
        g.setColorAt(0.0, rgba(accent, glow_alpha))
        g.setColorAt(0.55, rgba(accent, int(glow_alpha * 0.35)))
        g.setColorAt(1.0, rgba(accent, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(g)
        painter.drawRect(QRect(0, 0, w, h))

    painter.restore()


def is_dark(palette):
    """按窗口底色亮度判断深浅模式（不依赖主题对象，便于自检独立调用）。"""
    c = qcolor(palette.get("window_top"), "#101216")
    return (c.red() * 299 + c.green() * 587 + c.blue() * 114) / 1000.0 < 128


# ---------------------------------------------------------------------------
# 背板取图 + 缓存
# ---------------------------------------------------------------------------
class BackdropSource:
    """把「某个控件在背板里的矩形」抓出来并缓存。

    玻璃控件的尺寸/内容在运行期基本不变（背板是静态纹理），所以这里
    缓存模糊结果；只有背板重画（尺寸变了 / 换了主题）或矩形变了才重算。
    没有缓存的话，侧栏每次重绘都要 grab + 三轮 scaled()，滚动时会掉帧。
    """

    def __init__(self):
        self._cache = {}
        self._version = 0

    def invalidate(self):
        """背板重绘 / 主题切换时调用。"""
        self._version += 1
        self._cache.clear()

    def snapshot(self, source, rect, radius=10.0, stamp=0):
        """返回 source 控件中 rect 区域的**已模糊**图像（QPixmap）。

        rect 是 source 自己坐标系里的矩形。取不到（控件被销毁、
        尺寸为 0、背板还没建好）时返回 None，调用方应退回纯色绘制。

        `stamp` 是背板的版本号，参与缓存键。**这一步是必须的**：背板换了
        主题会重绘，但玻璃控件不一定收到通知，光靠 invalidate() 会漏，
        结果就是浅色主题下露出一块深色玻璃（实测踩过）。把版本号塞进 key，
        背板一变、缓存自然全部失效。
        """
        if source is None or rect.width() <= 0 or rect.height() <= 0:
            return None
        try:
            if not source.isVisible():
                return None
        except RuntimeError:
            return None

        key = (self._version, int(stamp), int(rect.x()), int(rect.y()),
               int(rect.width()), int(rect.height()), round(float(radius), 2))
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        try:
            pm = source.grab(rect)
        except (RuntimeError, TypeError):
            return None
        if pm is None or pm.isNull():
            return None
        out = blur_pixmap(pm, radius)
        # 缓存别无限涨：同一时间活跃的键本来就只有几个
        if len(self._cache) > 24:
            self._cache.clear()
        self._cache[key] = out
        return out
