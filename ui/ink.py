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

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, Signal
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

from . import motion_engine as anim
from .fish import CarpFrames
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
# 鱼：矢量俯视水墨鲤（实现见 ui/fish.py）
# ---------------------------------------------------------------------------
# 原来这里是"从齐白石鱼画抠出的写实墨鱼 PNG + 切片剪切"（_align_body_axis /
# fish_sprites / _sprite_band / _draw_fish_body）。2026-10-30 起换成矢量绘制的
# 俯视鱼：身体是**一条连续路径**，姿态按相位量化成帧号播放（ui.fish.CarpFrames），
# 不再有"把一张图拆成很多节在滑"的折线观感。
# resources/ink_fish_*.png 两张素材仍在包里（没删），但已不再被引用；
# 想恢复"照片墨鱼"可以从这里接回去。

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

    def __init__(self, rng, w, h, margin, size_factor=1.0, now=None):
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
        #: 涟漪排期用的"现在"。默认墙钟（独立使用 InkPond 的场合），
        #: 由帧时钟驱动时传**累计动画时间** —— 见 InkPond.step 的说明。
        self.ripple_at = (time.monotonic() if now is None else float(now)) \
            + rng.uniform(1.5, 6.0)
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
        #: 累计动画时间（秒）：只在 step() 被真的调用时前进。
        #: 用它而不是墙钟，是为了让"暂停"（窗口隐藏 / 转场让路 / 掉帧跳过）
        #: 表现为**停住**而不是**瞬移** —— 恢复时鱼和涟漪都从原地继续。
        self._t = 0.0

        self._timer = None           # v1.3.0 起不再自带定时器（见下方注册）
        self._last_tick = time.monotonic()

        # v1.3.0：交给共享帧时钟（装饰车道）。原来这里是一个常驻的 33ms
        # 定时器 —— 首页一直开着就一直在跑，连窗口最小化都不停。现在：
        # 只有装饰动画时降到 24fps，窗口不可见/最小化时整层停摆，
        # 交互/转场进行时被预算闸门跳过。
        anim.add_ambient(self)

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
            f = _Fish(self._rng, w, h, m, size_factor=size_factor,
                      now=self._t)
            f.x, f.y = x, y
            self._fish.append(f)
        self._built_size = (w, h)

    # ---------------------------------------------------------------- 动画
    def step(self, dt):
        """帧时钟回调（装饰层）。返回 True = 继续占用车道。

        dt 由时钟统一给出（真实经过时间），掉帧时按时间求值 —— 鱼是"游过去"
        而不是"跳过去"。上限 50ms：长时间卡顿后别让鱼瞬移半屏。
        """
        if not self.isVisible() or not self._fish:
            return True
        dt = min(0.05, float(dt))
        self._t += dt
        now = self._t
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
        # ⚠️ **必须显式返回 True**。帧时钟把 falsy 当成"我已经结束"，
        # 于是第一次 tick 就把鱼池从车道上摘掉 —— 表现是鱼一条不动，
        # 但画面上完全没有任何报错（2026-10-29 真踩过：接引擎后首页鱼池
        # 静止，竹子照摆，因为竹子的 step 写了 return True）。
        return True

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
        # ⚠️ 必须用**同一个**时钟：涟漪的 t0 记的是 `self._t`（累计动画时间），
        # 这里若用墙钟算年龄，k 会是几万倍，`46 * (1 - k)` 直接成负数 ——
        # 症状是满屏 `QColor::setAlpha: invalid value -220745` 的告警，
        # 涟漪画不出来（2026-10-29 接引擎时踩到）。
        now = self._t
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
        """画鱼：姿态来自**帧表**（ui.fish.CarpFrames），位置/朝向实时算。

        v1.3.1（2026-10-30）：原来是"把一张抠出来的墨鱼照片切成 10 片、每片
        单独剪切"——无论怎么调都摆脱不了"分片在滑"的骨架（轮廓是 10 段折线、
        接缝处有整像素台阶）。现在换成**矢量俯视鱼 + 24 帧表**：
          * 身体是一条连续闭合路径（不是切片）；
          * 姿态按相位量化成**帧号**，帧间不插值 → 真正的"24 张画在播"；
          * 位置/朝向/大小/浓淡仍然实时算，所以鱼照样朝任意方向游。

        帧表按 (墨色, 纸色, DPI) 缓存；主题切换或换显示器自动重烘。
        """
        frames, fw, fh = _carp_frames.frames(self._carp_palette(),
                                             self.devicePixelRatioF())
        if not frames:
            return
        n = len(frames)
        # 播放头：池子的动画时钟（累计推进量，暂停时不前进 → 不会瞬移）
        # 相位 = 播放头 + 个体相位偏移 → 同池的鱼不同步，但节奏一致
        head = (self._t % 1.0) if self._t else 0.0
        for f in self._fish:
            phase = (head + f.tail_phase / math.tau) % 1.0
            pm = frames[int(phase * n) % n]

            p.save()
            # 鱼锚在自身位置，身体朝向 = 航向。刚体只负责"朝哪游"，
            # "怎么游"交给帧表里的姿态——两者叠加才是活鱼。
            p.translate(f.x, f.y)
            p.rotate(math.degrees(f.heading))
            p.scale(f.scale, f.scale)
            p.setOpacity(f.alpha / 255.0)
            p.drawPixmap(QPointF(-fw / 2.0, -fh / 2.0), pm)
            p.restore()
        p.setOpacity(1.0)

    def _carp_palette(self):
        """帧表用的四色：鱼身墨、鳍墨、纸色（背脊亮边）、（眼已按要求去掉）。"""
        cur = theme.current()
        body = QColor(cur.get("text", "#1d1d1b"))
        fin = QColor(cur.get("text", "#1d1d1b"))
        rim = QColor(cur.get("window_top", "#f7f6f2"))
        return {"body": body, "fin": fin, "rim": rim, "eye": rim}

# ---------------------------------------------------------------------------
# 墨竹：会随风摆动的竹子
# ---------------------------------------------------------------------------
#: 全进程共用一份帧表（按墨色/纸色/DPR 缓存，主题切换自动重烘）
_carp_frames = CarpFrames()


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

    #: color —— 画叶子用的填充色。**每帧都一样**（只跟主题墨色与叶自身
    #: alpha 有关），所以在建竹时算一次、之后整帧复用（v1.3 性能修复：
    #: 之前每帧为 54 片叶子各 new 一个 QColor + setAlpha，是纯浪费）。
    __slots__ = ("length", "width", "angle", "flutter", "alpha", "color")

    def __init__(self, rng):
        self.length = rng.uniform(26, 46)
        self.width = self.length * rng.uniform(0.18, 0.26)
        self.angle = rng.uniform(-1.05, 1.05)     # 相对枝条方向的张角
        self.flutter = rng.uniform(0, math.tau)   # 各叶相位错开
        self.alpha = rng.randint(150, 225)
        self.color = None                         # 由 _ensure_pens 填


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
        # ---- 画笔缓存（v1.3 性能修复）----
        # 每节的粗细、节环/枝条的笔宽、每片叶的填充色**逐帧完全一致**
        # （只由 width/alpha 与主题墨色决定），所以按"当前墨色"算一次就够。
        # 实测这一坨 Python 侧对象分配占单帧渲染的 ~35%（每帧 ~74 个对象）。
        # _pen_key 存的是墨色签名；主题切换后墨色变了，下一帧自动重建。
        self._pen_key = None
        self.seg_pens = []          # 每节的 QPen（索引同 segments）
        self.ring_pen = None        # 竹节环
        self.branch_pen = None      # 小枝


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
    # v1.3.0 起节奏由 motion_engine 统一（只有装饰动画时用 AMBIENT_MS），
    # 这里保留常量供自检/文档引用，不再自己开定时器。
    TICK_MS = 42

    def __init__(self, orientation="right", parent=None):
        super().__init__(parent)
        self._orientation = orientation
        self._stalks = None      # 惰性生成：要等拿到实际尺寸
        self._built_size = None
        import random
        self._rng = random.Random(20261011)
        # 本帧时间（整帧统一，防枝叶抖动不一致）。用引擎的**累计动画时间**
        # 而不是墙钟：装饰层被暂停（隐藏 / 转场让路 / 掉帧跳过）时相位不前进，
        # 恢复时从原地继续，不会"瞬移"到当前应有的位置。
        self._now = anim.anim_time()
        self._wind = 0.0
        # v1.3.0：不再自带 QTimer，注册到全进程共享的帧时钟（装饰车道）。
        # 三个收益：
        #   ① 只有装饰动画在跑时自动降到 24fps；
        #   ② 窗口最小化/收起托盘/进程挂起时**整个装饰层停摆** —— 旧实现
        #      只查 isVisible()，最小化后 Qt 仍认为控件可见，于是它继续以
        #      24fps 为一块谁也看不见的画面算每一片叶子的摆动；
        #   ③ 用户按开关、切页面时，装饰层会被预算闸门跳过，帧让给交互。
        anim.add_ambient(self)
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
    def step(self, dt):
        """帧时钟回调（装饰层）。返回 True = 继续占用车道。

        dt 由时钟统一给出，同一帧里所有装饰共用一份 —— 各自取
        `time.monotonic()` 会差几微秒，多根竹子之间看不出，但和鱼池叠在
        一起时相位会缓慢漂移。

        风力低通原来写的是"每 tick 吸收 22%"（24fps 下时间常数 ≈170ms）。
        换成按 dt 算才是帧率无关的：掉帧时不会突然"风变大一下"。
        """
        if not self.isVisible():
            return True
        self._now = anim.anim_time()
        target = _wind(self._now)
        self._wind += (target - self._wind) * min(1.0, dt / 0.17)
        self.wind_changed.emit(self._wind)
        self.update()
        return True

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

    def _ensure_pens(self, stalk, ink):
        """按当前墨色备好这根竹的全部画笔/填充色（"每帧零分配"的关键）。

        每节的粗细、节环/枝条的笔宽、每片叶的填充色**逐帧完全一致**，
        只由 width/alpha 与主题墨色决定。实测这坨 Python 对象分配占单帧
        渲染的 ~35%（每帧 ~74 个 QPen/QColor）。用墨色签名当失效键：
        主题换了墨色就自动重建，其余时间一帧都不再 new。
        """
        key = (ink.name(), ink.alpha())
        if stalk._pen_key == key:
            return
        seg_pens = []
        for i in range(stalk.segments):
            k = (i + 1) / stalk.segments
            width = stalk.width * (1.0 - 0.35 * k)
            seg_pens.append(QPen(ink, max(1.5, width),
                                 Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        stalk.seg_pens = seg_pens
        ring = QColor(ink)
        ring.setAlpha(int(stalk.alpha * 0.85))
        stalk.ring_pen = QPen(ring, max(1.0, stalk.width * 0.16),
                              Qt.SolidLine, Qt.RoundCap)
        stalk.branch_pen = QPen(QColor(ink), max(1.0, stalk.width * 0.16),
                                Qt.SolidLine, Qt.RoundCap)
        for br in stalk.branches:
            for leaf in br.leaves:
                c = QColor(ink)
                c.setAlpha(int(leaf.alpha * stalk_alpha_scale(leaf.alpha)))
                leaf.color = c
        stalk._pen_key = key

    def _paint_stalk(self, p, stalk, w, h, wind):
        """画一根竹：从底向上逐节推进，风力随高度与柔韧度放大。"""
        total_h = h * stalk.height_ratio
        seg_len = total_h / stalk.segments
        x = w * stalk.x_ratio
        y = float(h)
        angle = stalk.lean
        self._ensure_pens(stalk, ink_color(alpha=stalk.alpha))
        pens = stalk.seg_pens

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
            p.setPen(pens[i])
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
        for jx, jy, ja, i in joints[:-1]:
            p.setPen(stalk.ring_pen)
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
            p.setPen(stalk.branch_pen)
            p.drawLine(QPointF(jx, jy), QPointF(bx, by))
            for leaf in br.leaves:
                # 叶：以枝条方向为基线，整体向外侧半圆张开（+0.45 下垂偏置，
                # 竹叶是垂下来的，不是平举的），再随风与自身高频颤动。
                # 无论 leaf.angle 取什么值，la 都落在枝条所指的那半边，
                # 不会越过竹秆朝内长。
                la = (bang + out * (0.45 + leaf.angle)
                      + wind * 0.16 * stalk.flex
                      + 0.05 * math.sin(self._now * 3.1 + leaf.flutter))
                self._paint_leaf(p, bx, by, la, leaf)

    def _paint_leaf(self, p, x0, y0, angle, leaf):
        """一片竹叶：细长的两端尖中间鼓的弯月形。

        填充色由 `_ensure_pens` 预算好挂在 `leaf.color` 上（逐帧一致），
        这里不再每帧 new QColor。
        """
        ln, wd = leaf.length, leaf.width
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
        p.setBrush(leaf.color)
        p.drawPath(path)


def stalk_alpha_scale(_leaf_alpha):
    """叶子透明度的整体缩放（预留调参入口）。"""
    return 0.9
