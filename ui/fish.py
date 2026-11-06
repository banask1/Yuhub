# -*- coding: utf-8 -*-
"""齐白石水墨鲤鱼 · **俯视**（从上往下看鱼背）· 矢量绘制。

为什么是这个视角
----------------
用户要的是"鱼在水里游泳"的感觉，而且是齐白石那种水墨。俯视图正好对上两件事：
    * 俯视看到的**就是鱼背**——一整片墨，最像"一笔画下来的鱼"；
    * 侧向摆尾在俯视里是**画面内的左右弯**，整条鱼像一片叶子在扭，而侧视
      只能看到上下起伏，很容易画成"分片平移"。
所以这里把坐标定成：头在 +x、尾在 -x，`y` 是**鱼身横向**（画面里就是上下）。
脊柱曲线的 y 偏移直接就是"鱼往哪边弯"。

画法（齐白石的三个要点）
------------------------
1. **一笔到底的鱼身**：沿脊柱扫出一条变宽闭包路径，不是切片拼的。
2. **墨分浓淡**：横向渐变（背脊浓、两侧淡）+ 纵向渐变（头浓、尾淡）。
   白石画鱼从不勾轮廓线，全靠墨色块面立住形体。
3. **软边**：路径本身是硬边，另外用同色低透明描 2~3 圈做"洇"，边缘才不
   像贴纸。尾鳍更淡（枯笔），鳍上补两三道"飞白"笔触当鳍条。

坐标与比例
----------
所有几何都以**体长 L** 为单位，`u` 是"头→尾"的归一化弧长（u=0 吻端、
u=1 尾柄末端）。因此在任何尺寸下同一套参数都成立。

⚠️ 踩过的坑（改几何前必读）
1. **别把鳍锚在 u=0/u=1**：两端宽度被端帽收到 0，根边会收成一点，
   整片鳍退化成一根针（第一版尾鳍就是这么"消失"的）。
2. **法线符号**：切线沿 u 递增（朝尾），`n = (-ty, tx)`。俯视里它就是
   "横向"，`+n` 与 `-n` 是左右两侧，没有背腹之分 —— 但侧视版曾经把
   胸鳍/腹鳍全画到背上去，改视角时别再照抄旧符号。
3. **体长要扣掉尾鳍**：鱼身 1.0L + 尾鳍 0.3L，直接拿 rect 宽当 L 会把
   尾巴裁到框外。
"""

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

TAU = math.tau

# ---------------------------------------------------------------------------
# 体型剖面（半宽 / 体长）
# ---------------------------------------------------------------------------
# 俯视鲤鱼：吻端窄 → 鳃盖后最宽 → 尾柄收细。最大半宽 0.125 → 全长:体宽
# ≈ 4:1（俯视比侧视更"宽"一点，这是从上面看鱼背的真实比例）。
_WIDTH_KEYS = (
    (0.00, 0.016),
    (0.06, 0.052),
    (0.15, 0.088),
    (0.25, 0.112),
    (0.38, 0.125),
    (0.50, 0.116),
    (0.62, 0.096),
    (0.74, 0.068),
    (0.85, 0.038),
    (0.93, 0.026),
    (1.00, 0.020),
)


def _catmull(p0, p1, p2, p3, t):
    """一维 Catmull-Rom：过 p1→p2，p0/p3 只提供切线。"""
    t2 = t * t
    t3 = t2 * t
    return 0.5 * ((2 * p1)
                  + (-p0 + p2) * t
                  + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * t3)


def _width(u):
    """u 处的半宽（相对体长）。Catmull-Rom 过关键点，保证没有折角。"""
    u = max(0.0, min(1.0, u))
    keys = _WIDTH_KEYS
    for i in range(len(keys) - 1):
        u0, w0 = keys[i]
        u1, w1 = keys[i + 1]
        if u <= u1 or i == len(keys) - 2:
            t = (u - u0) / (u1 - u0)
            p0 = keys[max(0, i - 1)][1]
            p3 = keys[min(len(keys) - 1, i + 2)][1]
            return max(0.0, _catmull(p0, w0, w1, p3, t))
    return keys[-1][1]


def _cap(u, r):
    """两端收圆：宽度乘一个半圆因子，避免首尾被切成平口。"""
    if u < r:
        k = (r - u) / r
        return math.sqrt(max(0.0, 1.0 - k * k))
    if u > 1.0 - r:
        k = (u - (1.0 - r)) / r
        return math.sqrt(max(0.0, 1.0 - k * k))
    return 1.0


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
DEFAULTS = {
    # 行波：振幅（相对体长）、沿身的波数、"头不动尾大动"的包络
    "amp": 0.132,
    "wave_k": 0.62,
    "env_pow": 1.55,
    "env_min": 0.10,
    # 尾鳍：长度、单侧张开、跟随身体的滞后
    "tail_len": 0.32,
    "tail_spread": 0.098,
    "tail_lag": 0.15,
    # 胸鳍：(u, 长度, 外张角)；俯视里它是左右各一片，最显眼的一对。
    # 2026-10-30 用户要求"胸鳍收小"：外张 0.115 → 0.072（约 6 成），
    # 后缘也从 0.205 收到 0.165 —— 原来的三角翼太抢，像翅膀不像鳍。
    "pec": (0.230, 0.165, 0.072),
    # 腹鳍：(u, 长度)
    "pelvic": (0.520, 0.100),
    # 臀鳍：(u, 长度)
    "anal": (0.730, 0.085),
    # 背鳍：俯视只看得到"沿脊的一道深色" —— (起 u, 止 u, 半宽)
    "dorsal": (0.30, 0.64, 0.020),
    # 采样数：96 已经看不出折线
    "samples": 96,
}


class Carp:
    """一条水墨鲤鱼的绘制器（无状态；每次 paint 现算形状）。"""

    def __init__(self, **kw):
        self.p = dict(DEFAULTS)
        self.p.update(kw)

    # -- 脊柱 ---------------------------------------------------------------
    def _spine(self):
        n = int(self.p["samples"])
        amp, k, epw = self.p["amp"], self.p["wave_k"], self.p["env_pow"]
        emin, ph = self.p["env_min"], self.p["_phase"]
        raw = []
        for i in range(n + 1):
            u = i / n
            env = emin + (1.0 - emin) * (u ** epw)
            raw.append((0.5 - u, amp * env * math.sin(ph - TAU * k * u)))
        out = []
        for i in range(n + 1):
            x, y = raw[i]
            i0, i1 = max(0, i - 1), min(n, i + 1)
            tx, ty = raw[i1][0] - raw[i0][0], raw[i1][1] - raw[i0][1]
            ln = math.hypot(tx, ty) or 1.0
            tx, ty = tx / ln, ty / ln
            u = i / n
            w = _width(u) * _cap(u, 0.035) * _cap(1.0 - u, 0.018)
            out.append((x, y, -ty, tx, w, u, tx, ty))
        return out

    # -- 主入口 -------------------------------------------------------------
    def paint(self, painter, rect, phase, palette):
        """把鱼画进 rect（头在右）。phase：一个完整摆动周期 = 1.0。"""
        self.p["_phase"] = float(phase) * TAU
        pts = self._spine()
        # 体长扣掉尾鳍：鱼身 1.0L、尾鳍再往后 0.32L；纵向最宽 0.25L + 摆幅
        L = float(min(rect.width() / 1.50, rect.height() / 0.62))
        if L <= 6:
            return
        # 视觉居中：鱼身 -0.5..+0.5、尾鳍到 -0.78 → 整体中心约 -0.14L
        cx = rect.center().x() + 0.14 * L
        cy = rect.center().y()

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        # 鳍先画（压在鱼身下面）：俯视里胸鳍/腹鳍是从身体两侧伸出去的
        self._paint_pectoral(painter, pts, L, cx, cy, palette)
        self._paint_pelvic(painter, pts, L, cx, cy, palette)
        self._paint_anal(painter, pts, L, cx, cy, palette)
        self._paint_tail(painter, pts, L, cx, cy, palette)

        body = self._body_path(pts, L, cx, cy)
        self._paint_body(painter, body, pts, L, cx, cy, palette, rect)

        # 背鳍：俯视只是一道沿脊的深色（画在鱼身之上，才像"背上那条鳍")
        self._paint_dorsal(painter, pts, L, cx, cy, palette)

        # 须（白石画鱼常有）：从吻端向前外侧各一根细弧
        self._paint_barbels(painter, pts, L, cx, cy, palette)

        # ⚠️ 不画眼睛（用户 2026-10-30 明确要求"鱼不要眼睛"）：
        # 俯视水墨鱼一旦点上双眼，观感立刻从"一团墨"变成"卡通鱼"，
        # 反而丢了白石那种"似与不似之间"的味道。别再加回来。

        painter.restore()

    # -- 鱼身 ---------------------------------------------------------------
    def _body_path(self, pts, L, cx, cy):
        body = QPainterPath()
        for i, (x, y, nx, ny, w, _u, _tx, _ty) in enumerate(pts):
            q = QPointF(cx + (x + nx * w) * L, cy + (y + ny * w) * L)
            if i == 0:
                body.moveTo(q)
            else:
                body.lineTo(q)
        for i in range(len(pts) - 1, -1, -1):
            x, y, nx, ny, w, _u, _tx, _ty = pts[i]
            body.lineTo(QPointF(cx + (x - nx * w) * L, cy + (y - ny * w) * L))
        body.closeSubpath()
        return body

    def _paint_body(self, painter, body, pts, L, cx, cy, palette, rect):
        """鱼身：横向渐变（脊浓侧淡）+ 纵向渐变（头浓尾淡）+ 软边。

        为什么要两轴：只看横向会得到一条"均匀的墨带"，看不出头尾；只看
        纵向会得到一条"从头到尾一样宽的实心鱼"。白石那口墨是**脊背最浓、
        往两侧化开、往尾部飞白**，两个方向都得有。
        """
        ink = QColor(palette["body"])
        paper = QColor(palette["rim"])

        def mix(c1, c2, t):
            return QColor(int(c1.red() + (c2.red() - c1.red()) * t),
                          int(c1.green() + (c2.green() - c1.green()) * t),
                          int(c1.blue() + (c2.blue() - c1.blue()) * t))

        # ① 横向：以脊柱为中心，脊线处最浓、两侧化淡
        flank = mix(ink, paper, 0.42)
        mid = QPointF(cx, cy)
        g1 = QLinearGradient(mid.x(), mid.y() - L * 0.30,
                             mid.x(), mid.y() + L * 0.30)
        center = QColor(ink)
        g1.setColorAt(0.0, flank)
        g1.setColorAt(0.5, center)
        g1.setColorAt(1.0, flank)
        painter.setPen(Qt.NoPen)
        painter.setBrush(g1)
        painter.drawPath(body)

        # ② 纵向：头浓 → 尾淡（画完裁进鱼身里，不越界）
        painter.save()
        painter.setClipPath(body, Qt.IntersectClip)
        g2 = QLinearGradient(cx + L * 0.5, cy, cx - L * 0.55, cy)
        head_c = QColor(ink)
        head_c.setAlpha(92)                 # 叠一层淡墨把头压浓（白石画鱼头重墨）
        tail_c = QColor(paper)
        tail_c.setAlpha(52)                 # 尾部提亮 = 枯笔飞白
        g2.setColorAt(0.0, head_c)
        g2.setColorAt(0.32, QColor(0, 0, 0, 0))
        g2.setColorAt(1.0, tail_c)
        painter.setBrush(g2)
        painter.drawRect(rect)
        painter.restore()

        # ③ 软边：路径是硬边，用同色低透明描两三圈做"洇"
        for width_mul, alpha in ((0.030, 26), (0.014, 40)):
            c = QColor(ink)
            c.setAlpha(alpha)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(c, max(1.0, L * width_mul)))
            painter.drawPath(body)

    # -- 鳍 -----------------------------------------------------------------
    @staticmethod
    def _fan(p0, p1, ta, tb, conc=0.42, trail=0.12):
        """扇形鳍：根边 p0-p1，外缘是**一段** ta-tb（不是一个尖点）。

        ⚠️ 用"两根 + 一个尖点"在根窄时会退化成一根针（第一版尾鳍/胸鳍
        就是两根细线飞出画面）。鳍的外缘必须有宽度。
        """
        mx, my = (p0.x() + p1.x()) / 2.0, (p0.y() + p1.y()) / 2.0
        c0 = QPointF(mx + (ta.x() - mx) * conc, my + (ta.y() - my) * conc)
        c1 = QPointF(mx + (tb.x() - mx) * conc, my + (tb.y() - my) * conc)
        mmx, mmy = (ta.x() + tb.x()) / 2.0, (ta.y() + tb.y()) / 2.0
        c2 = QPointF(mmx + (mmx - mx) * trail, mmy + (mmy - my) * trail)
        path = QPainterPath(p0)
        path.quadTo(c0, ta)
        path.quadTo(c2, tb)
        path.quadTo(c1, p1)
        path.closeSubpath()
        return path

    @staticmethod
    def _wet(painter, path, palette, alpha, rays=0, L=200.0, ray_alpha=0.30):
        """湿墨画一片鳍：淡墨填充 +（可选）两三道飞白笔触。

        鳍不能画成实心同色：那是"贴纸". 白石画鳍是**一笔扫过**，根部浓、
        尖端淡，中间留白。这里用淡墨填充 + 沿外缘的短笔画模拟。
        """
        c = QColor(palette["fin"])
        c.setAlphaF(min(1.0, c.alphaF() * alpha))
        painter.setPen(Qt.NoPen)
        painter.setBrush(c)
        painter.drawPath(path)

        if rays > 0:
            rc = QColor(palette["fin"])
            rc.setAlphaF(min(1.0, rc.alphaF() * alpha * ray_alpha))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(rc, max(0.6, L * 0.005), Qt.SolidLine,
                                Qt.RoundCap))
            painter.drawPath(path)

    def _paint_tail(self, painter, pts, L, cx, cy, palette):
        """尾鳍：分叉的整片（根边 → 上叶 → 凹口 → 下叶 → 根边）。"""
        p = self.p
        i = int(0.955 * (len(pts) - 1))
        x, y, nx, ny, w, _u, tx, ty = pts[i]
        w_root = max(w, 0.024)

        swing = math.sin(p["_phase"] - TAU * p["wave_k"]
                         - TAU * p["tail_lag"] * 1.6)
        base = QPointF(cx + x * L, cy + y * L)
        tl = p["tail_len"] * L
        spread = p["tail_spread"] * L * (0.70 + 0.45 * (0.5 + 0.5 * swing))
        swx, swy = nx * swing * L * 0.09, ny * swing * L * 0.09

        def P(along, across):
            k = (along / tl) if tl else 0.0
            return QPointF(base.x() + tx * along + nx * across + swx * k,
                           base.y() + ty * along + ny * across + swy * k)

        root_up = P(0.0, w_root * 1.02)
        root_dn = P(0.0, -w_root * 1.02)
        tip_up = P(tl, spread)
        tip_dn = P(tl, -spread)
        notch = P(tl * 0.50, 0.0)

        path = QPainterPath(root_up)
        path.quadTo(P(tl * 0.42, spread * 0.55 + w_root * 0.4), tip_up)
        path.quadTo(P(tl * 0.60, spread * 0.50), notch)
        path.quadTo(P(tl * 0.60, -spread * 0.50), tip_dn)
        path.quadTo(P(tl * 0.42, -spread * 0.55 - w_root * 0.4), root_dn)
        path.quadTo(P(-tl * 0.05, 0.0), root_up)
        path.closeSubpath()
        self._wet(painter, path, palette, 0.42, rays=1, L=L)

    def _paint_pectoral(self, painter, pts, L, cx, cy, palette):
        """胸鳍：俯视里最显眼的一对。

        根**沿身体**铺开（从鳃后到鳍后端），外缘是一段弧，整体向后外侧扫。
        第一版把根收成一个点、外缘也只给了一小段，结果是一根横着的尖刺。
        """
        u0, ln, out = self.p["pec"]
        i = int(u0 * (len(pts) - 1))
        x, y, nx, ny, w, _u, tx, ty = pts[i]
        flap = math.sin(self.p["_phase"] * 2.0 + 0.6)
        for sgn in (1.0, -1.0):

            def P(dx, dn, _s=sgn):
                return QPointF(cx + (x - dx + nx * dn * _s) * L,
                               cy + (y + ny * dn * _s) * L)

            p0 = P(0.0, w * 1.00)                     # 前根（贴鳃后）
            p1 = P(0.072, w * 0.86)                   # 后根
            ta = P(0.018, w + out * (1.15 + 0.18 * flap))   # 外前
            tb = P(0.165, w * 0.86 + out * (0.55 + 0.15 * flap))  # 外后
            self._wet(painter, self._fan(p0, p1, ta, tb, conc=0.34,
                                         trail=0.10),
                      palette, 0.40, rays=1, L=L)

    def _paint_pelvic(self, painter, pts, L, cx, cy, palette):
        u0, ln = self.p["pelvic"]
        i = int(u0 * (len(pts) - 1))
        x, y, nx, ny, w, _u, tx, ty = pts[i]
        swing = math.sin(self.p["_phase"] + 1.1)
        for sgn in (1.0, -1.0):

            def P(dx, dn, _s=sgn):
                return QPointF(cx + (x - dx + nx * dn * _s) * L,
                               cy + (y + ny * dn * _s) * L)

            p0 = P(0.0, w * 0.92)
            p1 = P(ln * 0.30, w * 0.45)
            ta = P(ln * 0.95, w * 0.92 + ln * (0.55 + 0.18 * swing))
            tb = P(ln * 0.55, w * 0.45 + ln * 0.22)
            self._wet(painter, self._fan(p0, p1, ta, tb, conc=0.36),
                      palette, 0.34, rays=0, L=L)

    def _paint_anal(self, painter, pts, L, cx, cy, palette):
        u0, ln = self.p["anal"]
        i = int(u0 * (len(pts) - 1))
        x, y, nx, ny, w, _u, tx, ty = pts[i]

        def P(dx, dn):
            return QPointF(cx + (x - dx + nx * dn) * L,
                           cy + (y + ny * dn) * L)

        p0 = P(0.0, w * 0.92)
        p1 = P(ln * 0.25, w * 0.40)
        ta = P(ln, w * 0.92 + ln * 0.48)
        tb = P(ln * 0.60, w * 0.40 + ln * 0.20)
        self._wet(painter, self._fan(p0, p1, ta, tb, conc=0.36),
                  palette, 0.32, rays=0, L=L)

    def _paint_dorsal(self, painter, pts, L, cx, cy, palette):
        """背鳍：俯视只看得到沿脊的一道深色，所以画成一条渐隐的墨带。"""
        u0, u1, half = self.p["dorsal"]
        seg = [q for q in pts if u0 <= q[5] <= u1]
        if len(seg) < 3:
            return
        n = max(1, len(seg) - 1)
        upper, lower = [], []
        for k, (x, y, nx, ny, w, _u, _tx, _ty) in enumerate(seg):
            t = k / n
            hh = half * (math.sin(math.pi * t) ** 0.5)
            upper.append(QPointF(cx + (x + nx * hh) * L, cy + (y + ny * hh) * L))
            lower.append(QPointF(cx + (x - nx * hh) * L, cy + (y - ny * hh) * L))
        path = QPainterPath(upper[0])
        for q in upper[1:]:
            path.lineTo(q)
        for q in lower[::-1]:
            path.lineTo(q)
        path.closeSubpath()
        c = QColor(palette["body"])
        c.setAlpha(55)
        painter.setPen(Qt.NoPen)
        painter.setBrush(c)
        painter.drawPath(path)

    def _paint_barbels(self, painter, pts, L, cx, cy, palette):
        """须：吻端向前外侧的两根细弧——白石画鱼的点睛之笔。"""
        x, y, nx, ny, w, _u, tx, ty = pts[int(0.02 * (len(pts) - 1))]
        c = QColor(palette["fin"])
        c.setAlphaF(min(1.0, c.alphaF() * 0.45))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(c, max(0.6, L * 0.0045), Qt.SolidLine, Qt.RoundCap))
        for sgn in (1.0, -1.0):
            p = QPainterPath(QPointF(cx + (x + nx * w * 0.5 * sgn) * L,
                                     cy + (y + ny * w * 0.5 * sgn) * L))
            p.quadTo(QPointF(cx + (x + 0.045) * L,
                             cy + (y + ny * w * 1.6 * sgn) * L),
                     QPointF(cx + (x + 0.085) * L,
                             cy + (y + ny * w * 2.2 * sgn) * L))
            painter.drawPath(p)


# ---------------------------------------------------------------------------
# 帧表
# ---------------------------------------------------------------------------
class CarpFrames:
    """预烘焙的**逐帧表**：把一条完整摆动周期切成 N 张姿态图。

    为什么要帧表而不是每帧现画：
    * 现画 = 每帧重算 96 点脊柱 + 6 条路径 + 渐变，5 条鱼就是 30 条路径；
      帧表 = 每条鱼每帧一次 `drawPixmap`；
    * 更重要的是**观感**：帧与帧之间不插值（相位直接量化成帧号），
      这才有"24 张画在播"的手绘感，而不是"一个形变在滑"。

    烘焙很贵、播放很便宜 —— 所以形状可以按"好看"来定（96 点采样、
    软边、飞白），不用按"跑得动"来妥协。

    缓存键 = (墨色, 纸色, DPI)。主题切换或换显示器会自动重烘。
    """

    #: 一个周期切成多少帧（24 = 一秒 24 帧，与引擎的 AMBIENT_MS 对齐）
    N = 24
    #: 参考体长（逻辑像素，scale=1.0 时鱼从头到尾的长度）
    REF_LEN = 150.0

    def __init__(self):
        self._key = None
        self._frames = []
        self._w = 0.0
        self._h = 0.0

    def frames(self, palette, dpr=1.0):
        """取帧表（必要时重烘）。返回 (frames, 逻辑宽, 逻辑高)。"""
        dpr = max(0.5, min(3.0, float(dpr)))
        key = (palette["body"].rgba(), palette["rim"].rgba(),
               round(dpr, 2))
        if key == self._key:
            return self._frames, self._w, self._h

        # 一帧要装下：头前（须）到身后（尾鳍）≈ 1.4L，横向 ≈ 0.55L
        w = self.REF_LEN * 1.45
        h = self.REF_LEN * 0.62
        frames = []
        carp = Carp()
        for i in range(self.N):
            pm = QPixmap(int(w * dpr), int(h * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            try:
                # rect 的宽高比正好对上 Carp 内部的取景（1.50 / 0.62），
                # 这样烘出来的鱼身长度 == REF_LEN
                carp.paint(p, QRectF(0, 0, w, h), i / float(self.N), palette)
            finally:
                p.end()
            frames.append(pm)
        self._frames = frames
        self._w = w
        self._h = h
        self._key = key
        return frames, w, h

    def invalidate(self):
        self._key = None
        self._frames = []
