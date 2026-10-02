"""Yuhub 启动动画：毛玻璃背景 + 图标 + 旋转加载环，数据就绪后直接进入主窗口。

流程（由 main.py 驱动）：
  1. show()              —— 立即显示启动屏。背景是**毛玻璃**：合成一层底纹
                            （方格 + 柔光）→ 糊掉 → 叠玻璃薄纱 / 反光 / 边缘光。
                            底色取当前主题的 window_top，深浅模式由 MainWindow
                            之前的 init_theme_from_settings() 决定。
  2. 首页硬件数据就绪     —— main.py 调 finish()。
  3. finish()            —— 不足最短展示时长则先补足（让加载环至少转几圈），
                            然后 close() 并发出 finished。
"""

import os
import sys
import time

from PySide6.QtCore import Qt, QRect, QRectF, QTimer, Signal
from PySide6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

from . import glass, theme, VERSION_LABEL
from .main_window import resource_path

# 启动画面图标的逻辑尺寸。缩放时必须乘 devicePixelRatio，
# 否则 150% 缩放的屏幕上会先缩到 96 物理像素再被拉大 → 模糊。
ICON_SIZE = 104


class SplashScreen(QWidget):
    """无边框启动画面。用法：

        splash = SplashScreen()
        splash.show()
        ...  数据就绪后：
        splash.finish()          # close → finished 信号
    """

    finished = Signal()

    def __init__(self, min_duration_ms=2000, parent=None):
        super().__init__(parent)
        # SplashScreen 工具窗标志：不进任务栏、置顶显示
        self.setWindowFlags(
            Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(380, 380)

        self._angle = 0                # 加载环当前角度
        self._status = "正在准备…"
        self._min_duration_ms = int(min_duration_ms)
        self._shown_at = time.monotonic()

        # 毛玻璃底纹缓存（尺寸 / 主题不变就复用，见 _backdrop_pixmap）
        self._bg = None
        self._bg_key = None

        self._icon_pix = self._load_icon()
        self._center_on_screen()

        # 加载环旋转计时器（~60fps，重绘面积很小，开销可忽略）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

        # 主题切换时立即换色（启动画面存活期间主题不应变化，但防御一下）。
        # 必须用绑定方法连接：PySide6 会在本对象销毁时自动断连；换成 lambda
        # 的话连接会残留，切主题时对已删除对象调 update() 直接 RuntimeError。
        theme.bus.changed.connect(self._on_theme_changed)

    # ---------------------------------------------------------------- 外观
    def _load_icon(self):
        """加载 Yuhub 图标：优先透明底 PNG（1920×1920），退回白底源图 / ico。

        ⚠️ app_source.png 是无透明通道的白底图——浅色模式下白底与启动屏
        融为一体，深色模式下图标周围会有一圈白边。app_source_alpha.png
        是从它抠出来的透明底版本（复用 build_icon._white_to_alpha 的
        边缘洪水填充，锦鲤内部的白色高光不受影响），必须优先使用。
        """
        dpr = self.devicePixelRatioF() or 1.0
        want = int(ICON_SIZE * dpr)
        for rel in ("resources/app_source_alpha.png",
                    "resources/app_source.png", "resources/app.ico"):
            path = resource_path(rel)
            if not os.path.exists(path):
                continue
            if rel.endswith(".ico"):
                pix = QIcon(path).pixmap(want, want)
            else:
                pix = QPixmap(path)
            if pix.isNull():
                continue
            out = pix.scaled(
                want, want, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            out.setDevicePixelRatio(dpr)
            return out
        return None

    def _center_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen is not None:
            g = screen.availableGeometry()
            self.move(g.center() - self.rect().center())

    # ---------------------------------------------------------------- 状态
    def _on_theme_changed(self, _mode):
        self.update()

    def set_status(self, text):
        """更新环下方的状态文字。"""
        if text != self._status:
            self._status = text
            self.update()

    # ---------------------------------------------------------------- 绘制
    def _tick(self):
        self._angle = (self._angle + 5) % 360
        self.update()

    # ------------------------------------------------------------ 毛玻璃背景
    def _backdrop_pixmap(self):
        """合成"玻璃背后那一层"的模糊底纹（带缓存）。

        启动画面是个独立的顶层窗口，**没有 BackdropLayer 可以抓**，所以这里
        直接把 ui.glass.paint_backdrop 画进离屏 pixmap，再糊一次——等效于
        「透过磨砂玻璃看背后的底纹」。底纹本身（方格 + 两团柔光）就是主窗口
        BackdropLayer 用的同一套，所以启动屏和主窗口的玻璃观感是连续的。

        缓存键含主题里的 window_top / accent：换主题后自动重算，不会残留
        上一个主题的深浅。
        """
        cur = theme.current()
        key = (self.width(), self.height(),
               str(cur.get("window_top")), str(cur.get("accent")))
        if self._bg is not None and self._bg_key == key:
            return self._bg

        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        try:
            glass.paint_backdrop(p, self.size(), cur)
        finally:
            p.end()
        # 糊一次：方格交界被抹开，才有"磨砂"的观感（不糊就是一张网格纸）。
        # ⚠️ 半径别调太大：到 12 以上方格就彻底看不见了，整块退化成"一层渐变"，
        #    反而看不出是毛玻璃（试过 12，纹理全没了）。
        self._bg = glass.blur_pixmap(pm, 8.0)
        self._bg_key = key
        return self._bg

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        c = theme.current()

        try:
            radius = float(c.get("window_radius", 10) or 10)
        except (TypeError, ValueError):
            radius = 10.0
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        # ---- 毛玻璃底：模糊底纹 + 玻璃面色纱 + 内侧边缘光 ----
        # 1) 先裁进圆角形状，把"背后那一层"的模糊纹理铺上
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(body, radius, radius)
        p.setClipPath(clip)
        p.drawPixmap(0, 0, self._backdrop_pixmap())
        p.restore()

        # 2) 玻璃面色纱（等效 CSS 的 rgba(255,255,255,.04) + brightness(.9)）
        #    再叠一点 accent 提色，跟主窗口的背板柔光呼应
        tint, gloss, dim = glass.glass_params(c)
        glass.paint_glass(p, body, radius=radius,
                          tint="#ffffff", tint_alpha=tint,
                          gloss_alpha=gloss, top_alpha=136,
                          border_alpha=118, inner_alpha=46,
                          matte=c.get("accent", "#3b82f6"),
                          matte_alpha=30 if glass.is_dark(c) else 22)
        # 3) 内侧边缘光带：1px 描边在 380px 的面板上等于没有，要 16px 宽的
        #    渐变带才看得出"这块玻璃有厚度"（同 ui/glass.py 的说明）
        glass.paint_edge_glow(p, body, radius=radius, thickness=16,
                              top_alpha=104, left_alpha=66, bottom_alpha=26)

        # ---- 窗口描边 ----
        p.setPen(QPen(QColor(c.get("window_border", "#26262c")), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(body, radius, radius)

        w = self.width()

        # ---- 居中图标 ----
        if self._icon_pix is not None:
            p.drawPixmap(
                (w - ICON_SIZE) // 2, 58, self._icon_pix
            )

        # ---- 旋转加载环：淡色轨道 + 主题色弧段 ----
        cx, cy, r = w / 2.0, 240.0, 24.0
        ring_rect = QRect(int(cx - r), int(cy - r), int(r * 2), int(r * 2))
        track = QColor(c.get("border_strong", "#3a3a42"))
        p.setPen(QPen(track, 5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(ring_rect)
        accent = QColor(c.get("accent", "#3b82f6"))
        pen = QPen(accent, 5)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        # Qt 角度单位是 1/16 度；起始角递减 → 视觉上顺时针旋转
        p.drawArc(ring_rect, -self._angle * 16, -100 * 16)

        # ---- 状态文字 ----
        p.setPen(QColor(c.get("text_dim", "#9aa1ad")))
        f = p.font()
        f.setPixelSize(13)
        p.setFont(f)
        p.drawText(QRect(0, 286, w, 24), Qt.AlignHCenter, self._status)

        # ---- 底部版本号 ----
        p.setPen(QColor(c.get("text_faint", "#6b7280")))
        f2 = p.font()
        f2.setPixelSize(11)
        p.setFont(f2)
        p.drawText(
            QRect(0, self.height() - 40, w, 20),
            Qt.AlignHCenter,
            "Yuhub " + VERSION_LABEL,
        )

    # ---------------------------------------------------------------- 退出
    def finish(self):
        """数据就绪后调用：不足最短展示时长则先补足，然后直接关闭。"""
        elapsed = (time.monotonic() - self._shown_at) * 1000.0
        delay = int(self._min_duration_ms - elapsed)
        if delay > 0:
            QTimer.singleShot(delay, self._close_now)
        else:
            self._close_now()

    def _close_now(self):
        self._timer.stop()
        self.finished.emit()
        self.close()
        self.deleteLater()
