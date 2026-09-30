"""Yuhub 启动动画：纯色背景 + 图标 + 旋转加载环，数据就绪后直接进入主窗口。

流程（由 main.py 驱动）：
  1. show()              —— 立即显示纯色启动屏。背景色取当前主题的
                            window_top，深浅模式由 MainWindow 之前的
                            init_theme_from_settings() 决定。
  2. 首页硬件数据就绪     —— main.py 调 finish()。
  3. finish()            —— 不足最短展示时长则先补足（让加载环至少转几圈），
                            然后 close() 并发出 finished。
"""

import os
import sys
import time

from PySide6.QtCore import Qt, QRect, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from . import theme, VERSION_LABEL
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
        """加载 Yuhub 图标：优先高分辨率 PNG（1920×1920），退回 ico。

        按 devicePixelRatio 缩放：高分屏（150% 等）下物理像素 = 逻辑尺寸
        × DPR，直接给逻辑尺寸的 QPixmap 会被 Qt 拉伸导致模糊。
        """
        dpr = self.devicePixelRatioF() or 1.0
        want = int(ICON_SIZE * dpr)
        for rel in ("resources/app_source.png", "resources/app.ico"):
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

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        c = theme.current()

        # ---- 纯色圆角底（跟随主题深浅色） ----
        try:
            radius = float(c.get("window_radius", 10) or 10)
        except (TypeError, ValueError):
            radius = 10.0
        p.setPen(QPen(QColor(c.get("window_border", "#26262c")), 1))
        p.setBrush(QColor(c.get("window_top", "#0b0b0e")))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), radius, radius)

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
