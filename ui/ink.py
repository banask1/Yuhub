# -*- coding: utf-8 -*-
"""水墨视觉组件（2026-10-11 UI 重做）。

这一版把 Yuhub 的观感从"极简色块 + 毛玻璃"改成**水墨**：
    * 浅色模式 = 宣纸白底 + 黑墨；深色模式 = 玄墨黑底 + 白墨。
    * 主窗口右侧与侧栏加入**会随风摆动的墨竹**（BambooStrip）。
    * 开机画面是**水墨池塘 + 写实的鱼**（鱼 sprite 从齐白石风格的鱼画里
      抠出，见 resources/ink_fish_1.png / ink_fish_2.png）。

为什么单独开一个模块：竹子动画同时被主窗口（右侧长条 + 侧栏底部）使用，
鱼 sprite 同时被开机画面与聊天占位卡（水印）使用，都得有统一的取色与
缓存入口，所以收拢在这里。本模块只依赖 theme 与 PySide6，不反向依赖
main_window，避免循环导入。
"""

import math
import os
import sys
import time

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QRadialGradient,
    QTransform,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme


def resource_path(rel):
    """与 main_window.resource_path 相同的逻辑（复制一份防循环导入）。"""
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, rel)


# ---------------------------------------------------------------------------
# 取色：水墨只有"纸"与"墨"两个基本色，从主题色板推导
# ---------------------------------------------------------------------------
def paper_color(palette=None):
    """纸色（背景）。浅色=宣纸米白，深色=玄墨黑。"""
    pal = palette or theme.current()
    return QColor(pal.get("window_top", "#f7f6f2") if not is_dark(pal)
                  else pal.get("window_top", "#101012"))


def ink_color(palette=None, alpha=255):
    """墨色（前景笔画）。浅色=浓黑，深色=月白。"""
    pal = palette or theme.current()
    c = QColor(pal.get("text", "#1d1d1b"))
    c.setAlpha(int(alpha))
    return c


def is_dark(palette=None):
    from .glass import is_dark as _is_dark
    return _is_dark(palette or theme.current())


# ---------------------------------------------------------------------------
# 鱼 sprite：从鱼画抠出的写实墨鱼（透明底 PNG）
# ---------------------------------------------------------------------------
_FISH_FILES = ("ink_fish_1.png", "ink_fish_2.png")
_fish_cache = {}          # kind -> [QPixmap, ...]  kind: 'normal' / 'inverted'
_BAND_CACHE = {}          # pixmap.cacheKey() -> (y0, y1) 鱼身实际像素带


def _align_body_axis(img):
    """把斜着画的鱼摆平：身体主轴转到水平、**头朝右**。

    sprite 是从齐白石风格的鱼画里抠出来的，鱼在画里本来就是斜游的
    （头朝右上、尾在左下）。之前整块贴图直接用问题不大——顶多鱼看着
    比航向翘一点；但身体行波必须**沿体轴**切片，斜着切会把鱼切碎。

    角度与头尾全部自动判别：
      * 主轴 = alpha 加权的 PCA 第一主成分；
      * 头尾 = 沿主轴分两半比"横向厚度"——鲤科鱼头比尾深，厚的那头是头。
    """
    w, h = img.width(), img.height()
    sw = swx = swy = swxx = swyy = swxy = 0.0
    pts = []
    for y in range(h):
        for x in range(w):
            a = img.pixelColor(x, y).alphaF()
            if a <= 0.02:
                continue
            sw += a
            swx += a * x
            swy += a * y
            swxx += a * x * x
            swyy += a * y * y
            swxy += a * x * y
            pts.append((x, y, a))
    if sw <= 0 or len(pts) < 10:
        return img
    cx, cy = swx / sw, swy / sw
    cxx = swxx / sw - cx * cx
    cyy = swyy / sw - cy * cy
    cxy = swxy / sw - cx * cy
    theta = 0.5 * math.atan2(2 * cxy, cxx - cyy)
    ax, ay = math.cos(theta), math.sin(theta)   # 主轴方向（方差最大）

    # 头尾判别：沿主轴取中点分两半，各自累计"垂直厚度"（|偏距|×alpha）
    s_mid_lo = s_mid_hi = None
    thick = {1: 0.0, -1: 0.0}
    for x, y, a in pts:
        dx, dy = x - cx, y - cy
        s = dx * ax + dy * ay
        if s_mid_lo is None or s < s_mid_lo:
            s_mid_lo = s
        if s_mid_hi is None or s > s_mid_hi:
            s_mid_hi = s
    s_mid = (s_mid_lo + s_mid_hi) / 2
    for x, y, a in pts:
        dx, dy = x - cx, y - cy
        s = dx * ax + dy * ay
        d = abs(dx * -ay + dy * ax)
        thick[1 if s >= s_mid else -1] += d * a
    head_sign = 1.0 if thick[1] >= thick[-1] else -1.0   # 头在主轴哪一侧

    # 把"头方向"旋到 +x（右）：内容点 p → R(a)·p，要 R(a)·(hd) = (1,0)
    hd = (ax * head_sign, ay * head_sign)
    ang = -math.degrees(math.atan2(hd[1], hd[0]))
    return img.transformed(QTransform().rotate(ang), Qt.SmoothTransformation)


def fish_sprites(inverted=False):
    """加载鱼 sprite（带缓存）。`inverted` 用于深色模式（墨鱼翻成白鱼）。

    QImage.invertPixels(InvertRgb) 只反转 RGB、保留 alpha —— 正好把
    "黑墨鱼"翻成"白墨鱼"，轮廓与浓淡关系不变。
    加载后自动把鱼摆平（体轴水平、头朝右，见 _align_body_axis）：
    身体行波按体轴切片，斜着的原素材会把鱼切碎。
    """
    kind = "inverted" if inverted else "normal"
    if kind in _fish_cache:
        return _fish_cache[kind]
    out = []
    for name in _FISH_FILES:
        img = QImage(resource_path(os.path.join("resources", name)))
        if img.isNull():
            continue
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
        if inverted:
            img.invertPixels(QImage.InvertMode.InvertRgb)
            # 纯反色后鱼身（原本的浅色肚）还是中灰，压不亮整条鱼。
            # 按"墨密度"重新映射：反色后越亮的地方（原浓墨笔画）越白，
            # 原来淡的地方给稍暗的白，整体读成"白墨画的鱼"。
            for yy in range(img.height()):
                for xx in range(img.width()):
                    c = img.pixelColor(xx, yy)
                    a = c.alpha()
                    if a == 0:
                        continue
                    lum = (c.red() * 299 + c.green() * 587 + c.blue() * 114) // 1000
                    v = 135 + (lum * 115) // 255
                    img.setPixelColor(xx, yy, QColor(v, v, v, a))
        out.append(QPixmap.fromImage(_align_body_axis(img)))
    _fish_cache[kind] = out
    return out


# ---------------------------------------------------------------------------
# 墨池背景：纸色 + 数团极淡的墨晕（开机画面用，合成一次缓存成 pixmap）
# ---------------------------------------------------------------------------
def paint_pond(painter, size, palette, seed=7):
    """画"水墨池塘"的底：纸色 + 几团淡墨 + 边缘雾带。

    全部用低透明度径向渐变叠出来，不用任何图片资源；同一 seed 的结果
    确定，便于调用方缓存。
    """
    import random
    w, h = int(size.width()), int(size.height())
    if w <= 0 or h <= 0:
        return
    rng = random.Random(seed)
    dark = is_dark(palette)
    paper = QColor(palette.get("window_top", "#101012" if dark else "#f7f6f2"))
    painter.fillRect(QRectF(0, 0, w, h), paper)

    ink = QColor(palette.get("text", "#ecebe6" if dark else "#1d1d1b"))
    painter.setRenderHint(QPainter.Antialiasing, True)
    # 中央一团最淡的"水色"，四角稍重的"岸墨"
    blobs = [
        # (cx比例, cy比例, 半径比例, alpha)
        (0.50, 0.52, 0.62, 14),
        (0.12, 0.10, 0.34, 22),
        (0.90, 0.16, 0.30, 18),
        (0.08, 0.92, 0.30, 20),
        (0.92, 0.90, 0.34, 22),
    ]
    for fx, fy, fr, a in blobs:
        cx, cy, r = fx * w, fy * h, fr * max(w, h)
        g = QRadialGradient(cx, cy, max(1.0, r))
        c = QColor(ink)
        c.setAlpha(a)
        g.setColorAt(0.0, c)
        c2 = QColor(ink)
        c2.setAlpha(int(a * 0.35))
        g.setColorAt(0.6, c2)
        c3 = QColor(ink)
        c3.setAlpha(0)
        g.setColorAt(1.0, c3)
        painter.setPen(Qt.NoPen)
        painter.setBrush(g)
        painter.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
    # 几点苔点（画里的"水草点"意象）
    for _ in range(9):
        cx, cy = rng.uniform(0.04, 0.96) * w, rng.uniform(0.04, 0.96) * h
        r = rng.uniform(3, 9)
        c = QColor(ink)
        c.setAlpha(rng.randint(18, 36))
        painter.setBrush(c)
        painter.drawEllipse(QRectF(cx - r, cy - r * 0.45, r * 2, r * 0.9))


# ---------------------------------------------------------------------------
# 墨池 + 鱼：可嵌进任意页面的水墨池塘（开机画面与首页共用）
# ---------------------------------------------------------------------------
class _Fish:
    """一条鱼的状态。位置/航向以池内像素坐标计。

    按"巡游目标点"模型游（比正弦漫游接近真实鱼类行为）：
      * 大部分时间朝目标点**直线巡航**，头始终指着前进方向；
      * 快到目标才开始缓缓转向——转弯半径大，不会原地甩头；
      * 到达后另挑一个目标，偶尔连着转两个弯像在觅食兜圈；
      * 摆尾推进：尾拍频率是个体属性，推进爆发与摆尾相位同步
        （参考《鲤鱼游动》一类的二维游动循环）。
    """

    __slots__ = ("x", "y", "heading", "speed", "scale", "alpha",
                 "sprite", "flip", "phase", "ripple_at", "target", "margin",
                 "tail_phase", "beat_hz", "bend")

    def __init__(self, rng, w, h, margin, size_factor=1.0):
        self.margin = margin
        self.heading = rng.uniform(0, math.tau)
        self.speed = rng.uniform(18, 36)           # px/s，鱼有快有慢
        # 近大远小；再乘 size_factor——鱼的尺寸按池子大小缩放（开机画面
        # 的 640×420 调出来的比例，直接搬进首页的矮池子会显得鱼比池子还大）
        self.scale = rng.uniform(0.42, 1.05) * size_factor
        self.alpha = rng.randint(140, 235)         # 浓淡不一（墨色分层）
        self.sprite = rng.randrange(2)
        # f.flip（水平镜像变体）已移除：镜像鱼在切片坐标系里头尾颠倒，
        # 曾经导致一半的鱼倒着游；腹背翻转（scale(1,-1)）又看着像翻肚。
        # 个体差异交给大小/浓淡/速度/尾拍频率就够了。
        self.flip = False
        self.phase = rng.uniform(0, math.tau)
        # 摆尾：尾拍是个体属性，1.5~1.95Hz（2026-10-13 用户要求"稍微降低摆动
        # 频率"；鲤鱼慢速巡游本来就在 1.5~2Hz，比原来 1.8~2.4 更贴合）。
        # 小鱼拍得快、大鱼慢悠悠。
        self.tail_phase = rng.uniform(0, math.tau)
        self.beat_hz = rng.uniform(1.5, 1.95) / (0.6 + 0.4 * self.scale)
        self.bend = 0.0                            # 转弯时的静态体弯（-1~1）
        m = margin
        self.x = rng.uniform(m, w - m)
        self.y = rng.uniform(m, h - m)
        self.ripple_at = time.monotonic() + rng.uniform(1.5, 6.0)
        # 第一个巡游目标：偏向自己面朝的方向，开局不会原地掉头
        self.target = self._pick_target(rng, w, h, ahead=True)

    def _pick_target(self, rng, w, h, ahead=False):
        """挑一个池内的巡游目标点。ahead=True 时偏向当前航向前方。"""
        m = self.margin + 16
        for _ in range(20):
            tx = rng.uniform(m, w - m)
            ty = rng.uniform(m, h - m)
            if not ahead:
                return (tx, ty)
            # 目标与航向的夹角 <= 100° 才收（余弦 >= -0.17）
            dx, dy = tx - self.x, ty - self.y
            ln = math.hypot(dx, dy) or 1.0
            if (math.cos(self.heading) * dx + math.sin(self.heading) * dy) \
                    / ln > -0.17:
                return (tx, ty)
        return (w / 2.0, h / 2.0)


def _sprite_band(pix):
    """贴图里"真正有像素"的纵向带 (y0, y1)，按 cacheKey 缓存。

    鱼被转平后，画布上下各留了一大块全透明边角（259×242 的画布里鱼身
    只有 ~100px 高）。切片若按整幅高度画，等于一半以上的 blit 面积在复制
    空气——按内容收紧绘制带，鱼的绘制开销直接砍掉一半多。
    """
    key = pix.cacheKey()
    hit = _BAND_CACHE.get(key)
    if hit is not None:
        return hit
    img = pix.toImage()
    w, h = img.width(), img.height()
    top = bot = None
    step = 2
    for y in range(0, h, step):
        hit_row = False
        for x in range(0, w, step):
            if img.pixelColor(x, y).alpha() > 8:
                hit_row = True
                break
        if hit_row:
            if top is None:
                top = y
            bot = y
    if top is None:                      # 全透明（异常贴图）：退回整幅
        band = (0, h)
    else:
        band = (max(0, top - step), min(h, bot + step + 1))
    _BAND_CACHE[key] = band
    return band


class InkPond(QWidget):
    """水墨池塘控件：纸色池底 + 淡墨晕 + 几条写实墨鱼沿随机轨迹巡游。

    用法：
        pond = ink.InkPond(fish_count=5)      # 嵌进首页横幅 / 任意页面
        pond.setFixedSize(640, 420)           # 或交给布局拉伸（默认 Expanding）

    要点：
      * 60fps PreciseTimer；位移按实测帧间隔 dt 积分，掉帧不瞬移；
      * 池底按 (尺寸, 主题) 缓存成 QPixmap，只有主题切换才重画；
      * 不可见时跳过 tick（页面切到后台不白烧 CPU）；
      * 深色模式鱼自动翻成"白墨鱼"；圆角自行裁剪，嵌进卡片不露直角；
      * 出生点用拒绝采样彼此拉开（间距随池子缩放），每次构造都不同。
    """

    def __init__(self, fish_count=5, parent=None):
        super().__init__(parent)
        self._fish_count = max(1, int(fish_count))
        self._bg = None
        self._bg_key = None
        self._ripples = []           # (x, y, t0)
        self._fish = None            # 惰性生成：要等拿到实际尺寸
        self._built_size = None
        import random
        self._rng = random.Random()
        self._last_tick = time.monotonic()

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        # 30fps（≈33ms）：鱼是慢速巡游，60fps 与 30fps 肉眼几乎无差，但
        # 实测 CPU 直接差一半（见 2026-10-13 的性能归因：三处动画从 85%
        # 单核降到 ~15%）。
        self._timer.start(33)

        theme.bus.changed.connect(self._on_theme_changed)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ---------------------------------------------------------------- 外观
    def _on_theme_changed(self, _mode):
        self._bg = None            # 纸色变了，背景缓存作废
        self.update()

    def sizeHint(self):
        # 嵌进页面时的默认高度。开机画面用 setFixedSize 不吃这个值；
        # 首页横幅里池子越高，sizeHint 越大——QScrollArea 的内容控件
        # 会按 sizeHint 长个儿，池子 default 420 会把首页顶出滚动条。
        return QSize(640, 300)

    def minimumSizeHint(self):
        return QSize(200, 140)

    # ------------------------------------------------------------ 鱼的初始化
    def _ensure_fish(self):
        w, h = max(1, self.width()), max(1, self.height())
        if self._fish is not None and self._built_size == (w, h):
            return
        self._build_fish(w, h)
        self._bg = None             # 尺寸变了，池底缓存作废

    def _build_fish(self, w, h):
        # 拒绝采样：出生点彼此拉开（间距随池子缩放）——小池子逼太紧会
        # 退化成"摆点阵"的假随机；边距同样按短边比例，鱼不贴边。
        spacing = max(46.0, min(w, h) * 0.16)
        # 边距按摆幅调大了：摆尾幅度加大后，贴着边的鱼摆尾会甩出池外被裁掉
        m = max(30.0, min(w, h) * 0.12)
        # 鱼的大小跟着池子走：以开机画面（640×420）为基准 1.0，
        # 矮池子（首页横幅 ~250px）里鱼缩到 ~0.65，不会鱼比池子还大
        size_factor = max(0.5, min(1.0, min(w, h) / 380.0))
        pts = []
        self._fish = []
        for _ in range(self._fish_count * 24):
            if len(self._fish) >= self._fish_count:
                break
            x = self._rng.uniform(m, w - m)
            y = self._rng.uniform(m, h - m)
            if any((x - px) ** 2 + (y - py) ** 2 < spacing ** 2
                   for px, py in pts):
                continue
            pts.append((x, y))
            f = _Fish(self._rng, w, h, m, size_factor=size_factor)
            f.x, f.y = x, y
            self._fish.append(f)
        self._built_size = (w, h)

    # ---------------------------------------------------------------- 动画
    def _tick(self):
        if not self.isVisible() or not self._fish:
            return
        now = time.monotonic()
        dt = min(0.05, now - self._last_tick)   # 实测帧间隔（掉帧时不瞬移）
        self._last_tick = now
        w, h = self.width(), self.height()
        for f in self._fish:
            # ---- 巡游目标点：直线为主，接近了才缓转 ----
            dx = f.target[0] - f.x
            dy = f.target[1] - f.y
            dist = math.hypot(dx, dy)
            desired = math.atan2(dy, dx)
            diff = math.atan2(math.sin(desired - f.heading),
                              math.cos(desired - f.heading))
            # 限角速度 0.8 rad/s：转弯是缓慢的大弧线，像真鱼调头
            max_turn = 0.8 * dt
            f.heading += max(-max_turn, min(max_turn, diff))
            # 转弯时身体顺势弯向转向侧（行波之上叠加的静态弯曲）：
            # 弯曲量平滑跟随转向需求的强度，直游时归零
            f.bend += (max(-1.0, min(1.0, diff * 2.5)) - f.bend) \
                * min(1.0, 3.0 * dt)

            # ---- 到达目标 → 挑下一个（面朝的方向优先，自然衔接）----
            if dist < 34.0:
                f.target = f._pick_target(self._rng, w, h, ahead=True)

            # ---- 前进：位移方向 = 头的朝向 ----
            # 推进与摆尾同相：扫尾半程轻轻加速——游动的"推进感"来自摆尾本身。
            # 幅度加大后推进起伏**调小**（±11%）：再叠一层明显的加减速就
            # 显出"一鼓一鼓"的机械感了。
            f.tail_phase += math.tau * f.beat_hz * dt
            burst = 0.91 + 0.11 * math.sin(f.tail_phase)
            step = f.speed * burst * dt
            f.x += math.cos(f.heading) * step
            f.y += math.sin(f.heading) * step
            # 越界兜底（避墙失灵时不至于游出画面）
            f.x = max(24.0, min(w - 24.0, f.x))
            f.y = max(24.0, min(h - 24.0, f.y))

            # ---- 涟漪：每条鱼隔一阵子在尾部漾一圈 ----
            if now >= f.ripple_at:
                f.ripple_at = now + self._rng.uniform(2.5, 7.0)
                self._ripples.append(
                    (f.x - math.cos(f.heading) * 26 * f.scale,
                     f.y - math.sin(f.heading) * 26 * f.scale, now))
        # 涟漪 2.4 秒一生
        self._ripples = [r for r in self._ripples if now - r[2] < 2.4]
        self.update()

    # ------------------------------------------------------------ 池底缓存
    def _backdrop(self):
        cur = theme.current()
        key = (self.width(), self.height(), str(cur.get("window_top")))
        if self._bg is not None and self._bg_key == key:
            return self._bg
        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        try:
            paint_pond(p, self.size(), cur, seed=7)
        finally:
            p.end()
        self._bg = pm
        self._bg_key = key
        return self._bg

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, event):
        self._ensure_fish()
        p = QPainter(self)
        # 只开抗锯齿（涟漪是圆/曲线，需要它）。**不开 SmoothPixmapTransform**：
        # 鱼的切片是带 shear 的变换贴图，开滤波后每一片都要重采样，是绘制
        # 侧最贵的一项；鱼小、边缘本来就是柔和墨色，关掉看不出差别。
        # 大鱼的清晰度在 _paint_fish 里按个体单独开（scale > 0.8 才开）。
        p.setRenderHint(QPainter.Antialiasing)
        c = theme.current()

        try:
            radius = float(c.get("window_radius", 10) or 10)
        except (TypeError, ValueError):
            radius = 10.0
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        # 池底裁进圆角（嵌进卡片时四角不露直角）
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(body, radius, radius)
        p.setClipPath(clip)
        p.drawPixmap(0, 0, self._backdrop())
        self._paint_ripples(p)
        self._paint_fish(p)
        p.restore()
        p.end()

    def _paint_ripples(self, p):
        now = time.monotonic()
        c = theme.current()
        base = ink_color(c, 0)
        for x, y, t0 in self._ripples:
            k = (now - t0) / 2.4
            r = 8 + 40 * k
            col = QColor(base)
            col.setAlpha(int(46 * (1 - k)))
            pen = QPen(col, 1.2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            # 俯视池塘的椭圆透视
            p.drawEllipse(QRectF(x - r, y - r * 0.42, r * 2, r * 0.84))

    def _paint_fish(self, p):
        dark = is_dark()
        sprites = fish_sprites(inverted=dark)
        if not sprites:
            return
        for f in self._fish:
            pix = sprites[f.sprite % len(sprites)]

            # 只有"大鱼"才开滤波贴图（清晰度肉眼可辨）；小鱼关掉省一半
            # 的变换 blit 开销。逐鱼设置，全帧不重复设同一个值。
            p.setRenderHint(QPainter.SmoothPixmapTransform, f.scale > 0.8)

            p.save()
            # 鱼锚在自身位置，身体朝向 = 航向。刚体只负责"朝哪游"，
            # "怎么游"交给下面的切片行波——两者叠加才是活鱼。
            p.translate(f.x, f.y)
            p.rotate(math.degrees(f.heading))
            # 缩放只在这里做一次——切片坐标全部用 sprite 的**未缩放**尺寸。
            # 之前把缩放后的 pw 喂进切片循环，源矩形却按未缩放取，
            # scale<1 的鱼只画出了左边一小条（首页的鱼就是这么"消失"的）。
            p.scale(f.scale, f.scale)
            p.setOpacity(f.alpha / 255.0)
            self._draw_fish_body(p, pix, f)
            p.restore()
        p.setOpacity(1.0)

    # 身体切片数：够顺滑即可。屏幕上鱼只有几十像素高，10 片已经看不出
    # 折线（22 片是历史遗留）；每帧 blit 次数直接减半，是动画 CPU 的大头
    BODY_SLICES = 10

    def _fish_dy(self, f, d, pw):
        """身体行波在"离头距离 d"（0=头、1=尾）处的横向偏移。

        参考《鲤鱼游动》一类的二维游动循环的身体波动规律：
          * **行波从头传向尾**：头部几乎不动，波幅向尾部递增
            （carangiform 游泳——头部摆幅只有尾部的 ~15%）；
          * 尾鳍"跟随"身体的弯曲，从不自作主张；
          * 转弯时身体整体顺势弯向转向侧、尾巴甩向外侧（静态弯曲
            叠加在行波上），直游时弯曲量平滑归零。
        振幅以**体长**为基准（pw 的 5.5% / 5%）：转平后的画布高度
        包含大片透明边角，拿它当基准会高估波幅、把鱼折出"断节"。
        """
        env = 0.15 + 0.85 * d ** 1.7               # 头小尾大的振幅包络
        wave = math.sin(f.tail_phase - math.tau * 0.62 * d)
        bend = -f.bend * d ** 1.8
        # 摆幅按体长：行波 7.5%、静态弯 6%（2026-10-13 用户要求加大摆幅，
        # 由 5.5% 提到 7.5%）。波长同时从 0.7 收到 0.62：幅度变大后波长
        # 太短会让 10 片的折线读出"折角"，放长一个摆更连贯。
        return pw * (0.075 * env * wave + 0.06 * bend)

    def _draw_fish_body(self, p, pix, f):
        """把鱼贴图沿体轴切成薄片，按行波逐片**剪切**后画出来。

        全程使用 sprite 的**未缩放**坐标（缩放由调用方在画笔上做一次）。
        关键是用**垂直剪切**（parallelogram）而不是旋转：每片左右两条
        边保持竖直、整体斜切 slope = (dy1 − dy0) / 片宽，于是相邻片的
        分界线完全重合——薄片连成一条连续的、会波动"身体"，没有任何
        缝隙或扇形伪影（旋转方案会让鱼背/鱼腹出现扇贝状缺口）。
        鱼的外观（sprite 像素）一个都不改——改的只是身体怎么动。
        腹背翻转变体由调用方用 scale(1,-1) 做（头始终朝 +x）。
        """
        pw, ph = pix.width(), pix.height()
        # 只画鱼身所在的横带（上下透明边角不画）。余量**按每一片自己的
        # 摆幅**给：头/中段几乎不动（头摆幅只有尾部的 15%），用整条鱼的
        # 最大值去撑就是把空气也画上去了 —— 摆幅加大后这一项差了 10% 开销。
        band0, band1 = _sprite_band(pix)
        n = self.BODY_SLICES
        for i in range(n):
            # 切片边界全部取整：浮点源矩形会让每一片都重采样，鱼身上
            # 浮出一条条淡淡的竖向接缝（深色模式的白鱼上格外明显）。
            # 整数对齐 + 斜率近零的片直接 1:1 贴图 → 中段完全无接缝。
            x0 = round(i * pw / n)
            x1 = round((i + 1) * pw / n)
            u0, u1 = i / n, (i + 1) / n
            # d = 离头距离（0=头、1=尾）：摆平后的贴图头在图像右端
            d0 = 1.0 - u0
            d1 = 1.0 - u1
            dy0 = round(self._fish_dy(f, d0, pw))
            dy1 = round(self._fish_dy(f, d1, pw))
            lx0, lx1 = x0 - pw / 2, x1 - pw / 2
            slope = (dy1 - dy0) / max(1.0, (lx1 - lx0))
            # 余量 = 本片两端的最大横移 + 剪切带来的额外倾斜量
            pad = max(abs(dy0), abs(dy1)) + abs(dy1 - dy0) + 2
            y0 = max(0, band0 - pad)
            y1 = min(ph, band1 + pad)
            p.save()
            p.translate(lx0, dy0)
            if abs(slope) > 0.004:
                p.shear(0.0, slope)
            p.drawPixmap(QRectF(0, -ph / 2 + y0, lx1 - lx0, y1 - y0),
                         pix, QRectF(x0, y0, lx1 - lx0, y1 - y0))
            p.restore()


# ---------------------------------------------------------------------------
# 墨竹：会随风摆动的竹子
# ---------------------------------------------------------------------------
def _wind(t):
    """风力场：几个不同频率的正弦叠加，偶有"阵风"。

    输入秒；输出约 [-1.3, 1.3]。低频是缓慢的摇曳，中频是摆动的层次，
    高频小幅抖动让叶片有"沙沙"的碎动感。
    """
    return (0.55 * math.sin(t * 0.62)
            + 0.32 * math.sin(t * 1.47 + 1.7)
            + 0.22 * math.sin(t * 2.9 + 0.6)
            + 0.18 * math.sin(t * 0.23 + 4.0))


class _Leaf:
    """一片竹叶：由 3 个控制点构成的细长弯叶。"""

    __slots__ = ("length", "width", "angle", "flutter", "alpha")

    def __init__(self, rng):
        self.length = rng.uniform(26, 46)
        self.width = self.length * rng.uniform(0.18, 0.26)
        self.angle = rng.uniform(-1.05, 1.05)     # 相对枝条方向的张角
        self.flutter = rng.uniform(0, math.tau)   # 各叶相位错开
        self.alpha = rng.randint(150, 225)


class _Branch:
    """一根小枝：从竹节斜出，末端挂一簇叶。"""

    __slots__ = ("seg", "side", "angle", "leaves")

    def __init__(self, rng, seg_index, n_segments):
        self.seg = seg_index
        self.side = rng.choice((-1, 1))
        # 越靠上的枝越平；方向随机朝左/朝右
        self.angle = rng.uniform(0.5, 1.05)
        n_leaves = rng.randint(4, 6)
        self.leaves = [_Leaf(rng) for _ in range(n_leaves)]


class _Stalk:
    """一根竹：底部锚点、节数、粗细、倾斜与柔韧度。"""

    def __init__(self, rng, x_ratio, height_ratio, width, depth_alpha):
        self.x_ratio = x_ratio
        self.height_ratio = height_ratio
        self.width = width
        self.alpha = depth_alpha
        self.lean = rng.uniform(-0.05, 0.10)      # 基础倾角（弧度）
        self.flex = rng.uniform(0.75, 1.25)       # 柔韧度：越大摆得越多
        self.phase = rng.uniform(0, math.tau)     # 摆动相位错开
        self.segments = rng.randint(4, 6)
        self.branches = []
        for i in range(self.segments):
            if i >= 1 and rng.random() < 0.85:
                self.branches.append(_Branch(rng, i, self.segments))


class BambooStrip(QWidget):
    """会随风摆动的墨竹条。

    用法：
        BambooStrip(orientation="right")  # 主窗口右侧：竖条，竹从底部向上
        BambooStrip(orientation="mini")   # 侧栏底部：矮丛，两三根小竹

    动画是纯 QPainter 自绘，~30fps；只在可见时重绘。墨色随主题自动换
    （浅色黑竹、深色白竹），动画相位用墙钟，主题切换不跳变。
    """

    #: 风力变化时发出（预留：将来可以让别的元素跟着同一阵风动）
    wind_changed = Signal(float)

    # 24fps：竹子**每个页面都在跑**，实测两根竹子占 ~30% 单核（60fps 全量
    # 抗锯齿矢量重绘）。风本身是低通滤波出来的慢变量，24fps 的摇曳完全
    # 看不出台阶，开销直接砍 60%。
    TICK_MS = 42

    def __init__(self, orientation="right", parent=None):
        super().__init__(parent)
        self._orientation = orientation
        self._stalks = None      # 惰性生成：要等拿到实际尺寸
        self._built_size = None
        import random
        self._rng = random.Random(20261011)
        self._now = time.monotonic()   # 本帧时间（整帧统一，防枝叶抖动不一致）
        self._wind = 0.0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)   # 别让 Qt 把节拍对齐到网格
        self._timer.timeout.connect(self._tick)
        self._timer.start(self.TICK_MS)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    # ------------------------------------------------------------ 布局生成
    def _build_stalks(self):
        rng = self._rng
        w, h = max(1, self.width()), max(1, self.height())
        if self._orientation == "right":
            # 右侧长条：3 根主竹，前浓后淡；底部留出落墨
            spec = [
                (0.30, 1.02, w * 0.085, 205),
                (0.62, 0.86, w * 0.065, 145),
                (0.86, 0.70, w * 0.048, 95),
            ]
        else:
            # 侧栏底部小丛：2-3 根矮竹
            spec = [
                (0.32, 0.92, w * 0.075, 185),
                (0.66, 0.74, w * 0.055, 130),
                (0.88, 0.55, w * 0.042, 90),
            ]
        self._stalks = [_Stalk(rng, xr, hr, wd, a) for xr, hr, wd, a in spec]
        self._built_size = (w, h)

    # ------------------------------------------------------------ 动画
    def _tick(self):
        if not self.isVisible():
            return
        self._now = time.monotonic()
        # 低通滤波：目标风力突变时竹子是"被风带过去"的，不是瞬移
        target = _wind(self._now)
        self._wind += (target - self._wind) * 0.22
        self.wind_changed.emit(self._wind)
        self.update()

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event):
        if (self._stalks is None or self._built_size !=
                (self.width(), self.height())):
            self._build_stalks()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        wind = self._wind
        t = self._now          # 整帧一个时钟：枝与叶的相位必须出自同一刻

        # 底部落墨：一小片淡墨渍，把竹子"种"在地上
        base_c = ink_color(alpha=26)
        g = QRadialGradient(w * 0.55, h, max(30.0, w * 0.9))
        g.setColorAt(0.0, base_c)
        c0 = QColor(base_c); c0.setAlpha(0)
        g.setColorAt(1.0, c0)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QRectF(w * 0.55 - w * 0.9, h - w * 0.32,
                             w * 1.8, w * 0.64))

        # 先画淡的后画浓的（远景竹在下面）
        for stalk in sorted(self._stalks, key=lambda s: s.alpha):
            self._paint_stalk(p, stalk, w, h, wind)
        p.end()

    def _paint_stalk(self, p, stalk, w, h, wind):
        """画一根竹：从底向上逐节推进，风力随高度与柔韧度放大。"""
        total_h = h * stalk.height_ratio
        seg_len = total_h / stalk.segments
        x = w * stalk.x_ratio
        y = float(h)
        angle = stalk.lean
        ink = ink_color(alpha=stalk.alpha)

        joints = []                      # (x, y, angle, seg_index) 记录竹节
        for i in range(stalk.segments):
            # 风力：越高的节摆幅越大（^1.4），乘柔韧度与相位差
            k = (i + 1) / stalk.segments
            sway = (wind * 0.10 * stalk.flex * (k ** 1.4)
                    + 0.02 * math.sin(self._now * 2.2 + stalk.phase))
            seg_angle = angle + sway
            nx = x - math.sin(seg_angle) * seg_len
            ny = y - math.cos(seg_angle) * seg_len
            width = stalk.width * (1.0 - 0.35 * k)
            pen = QPen(ink, max(1.5, width),
                       Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(pen)
            # 节间留 2px 缺口 → 竹节的"节环"意象
            gap = max(1.5, width * 0.28)
            ux, uy = nx - x, ny - y
            ln = math.hypot(ux, uy) or 1.0
            ux, uy = ux / ln, uy / ln
            p.drawLine(QPointF(x, y), QPointF(x + ux * (seg_len - gap),
                                              y + uy * (seg_len - gap)))
            joints.append((nx, ny, seg_angle, i))
            x, y = nx, ny

        # 竹节环：每节末端一条更淡的短横线
        ring = QColor(ink)
        ring.setAlpha(int(stalk.alpha * 0.85))
        for jx, jy, ja, i in joints[:-1]:
            p.setPen(QPen(ring, max(1.0, stalk.width * 0.16),
                          Qt.SolidLine, Qt.RoundCap))
            half = stalk.width * (1.0 - 0.35 * ((i + 1) / stalk.segments)) * 0.62
            p.drawLine(QPointF(jx - half * math.cos(ja),
                               jy + half * math.sin(ja)),
                       QPointF(jx + half * math.cos(ja),
                               jy - half * math.sin(ja)))

        # 枝与叶
        for br in stalk.branches:
            jx, jy, ja, i = joints[br.seg]
            out = br.side
            blen = stalk.width * (2.6 + 1.6 * (1 - br.seg / max(1, stalk.segments - 1)))
            # ⚠️ 角度约定：本文件里有两套，千万别混——
            #   竹秆用 θ（位移 = (-sinθ, -cosθ)，+ = 向左偏）；
            #   叶片 _paint_leaf 用 a（叶尖 = (sin a, -cos a)，+ = 向右偏）。
            # 以前的 bug 就是 bang 按竹秆约定算、却喂给按叶片约定画的东西，
            # out=-1 的枝条被画到右侧、叶子又按左侧算 → 叶子整体朝内。
            # 这里把枝条直接换算到叶片约定：out=+1 朝右上，out=-1 朝左上。
            bang = -ja + out * br.angle
            bx = jx + math.sin(bang) * blen
            by = jy - math.cos(bang) * blen * 0.9
            p.setPen(QPen(QColor(ink), max(1.0, stalk.width * 0.16),
                          Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(jx, jy), QPointF(bx, by))
            for leaf in br.leaves:
                # 叶：以枝条方向为基线，整体向外侧半圆张开（+0.45 下垂偏置，
                # 竹叶是垂下来的，不是平举的），再随风与自身高频颤动。
                # 无论 leaf.angle 取什么值，la 都落在枝条所指的那半边，
                # 不会越过竹秆朝内长。
                la = (bang + out * (0.45 + leaf.angle)
                      + wind * 0.16 * stalk.flex
                      + 0.05 * math.sin(self._now * 3.1 + leaf.flutter))
                self._paint_leaf(p, bx, by, la, leaf, ink)

    def _paint_leaf(self, p, x0, y0, angle, leaf, ink):
        """一片竹叶：细长的两端尖中间鼓的弯月形。"""
        ln, wd = leaf.length, leaf.width
        c = QColor(ink)
        c.setAlpha(int(leaf.alpha * stalk_alpha_scale(leaf.alpha)))
        # 叶尖方向
        x1 = x0 + math.sin(angle) * ln
        y1 = y0 - math.cos(angle) * ln
        # 垂直方向鼓出
        px, py = math.cos(angle), math.sin(angle)
        mx = (x0 + x1) / 2 + px * wd * 0.5
        my = (y0 + y1) / 2 + py * wd * 0.5
        path = QPainterPath()
        path.moveTo(x0, y0)
        path.quadTo(mx, my, x1, y1)
        path.quadTo(mx - px * wd * 0.7, my - py * wd * 0.7, x0, y0)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawPath(path)


def stalk_alpha_scale(_leaf_alpha):
    """叶子透明度的整体缩放（预留调参入口）。"""
    return 0.9
