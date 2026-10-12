# -*- coding: utf-8 -*-
"""Yuhub 启动动画（水墨版）：一方墨池，几条写实的鱼游来游去。

2026-10-11 UI 重做：原来的"毛玻璃 + 图标 + 加载环"换成了**水墨池塘**——
纸色池底、几团淡墨、数条从齐白石风格鱼画里抠出的写实墨鱼沿随机轨迹游动
（深色模式下鱼自动翻成"白墨鱼"，与主窗口的深浅规则一致）。

2026-10-12：池子与鱼抽成可复用控件 `ink.InkPond`（首页横幅也在用），
本文件只剩启动窗口的外壳：无边框窗口 + 圆形裁切 + 池边状态文字。
父窗口的 paintEvent 先于子控件绘制，所以描边/文字/墨圈都放在一个
盖在鱼池之上的透明叠加层里画（见 `_SplashOverlay`）。

流程（由 main.py 驱动，接口与旧版完全一致）：
  1. show()              —— 立即显示启动屏；
  2. 首页硬件数据就绪     —— main.py 调 finish()；
  3. finish()            —— 不足最短展示时长则先补足，然后 close() 并发出 finished。

鱼的游法（随机但要有"活气"）：
  * 每条鱼有自己的速度 / 大小 / 浓淡 / 相位；
  * 航向 = 巡游目标点模型：直线巡航为主、接近才缓转、到达另挑目标；
  * 身体朝向恒等于前进方向（不叠加刚体拧转，那会看着像"左右甩"）；
  * 偶尔从鱼尾漾开一圈涟漪。
"""

import time

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import ink, theme, VERSION_LABEL

#: 池塘窗口的逻辑尺寸
POND_W, POND_H = 640, 420

#: 鱼的数量。 sprites 只有 2 个形状，靠镜像/大小/浓淡/速度差拉开个体感
FISH_COUNT = 6


class _SplashOverlay(QWidget):
    """启动画面最上层：淡墨描边 + 墨圈加载指示 + 状态文字 + 版本号。

    必须是独立子控件：父窗口的 paintEvent 画完才会画子控件，直接画在
    SplashScreen 上会被 InkPond 盖住。透明背景，只画笔墨。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._status = "正在准备…"

    def set_status(self, text):
        if text != self._status:
            self._status = text
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = theme.current()
        ink_strong = ink.ink_color(c, 210)
        ink_dim = ink.ink_color(c, 140)
        ink_faint = ink.ink_color(c, 100)
        w = self.width()

        # ---- 窗口描边（淡墨一线）----
        try:
            radius = float(c.get("window_radius", 10) or 10)
        except (TypeError, ValueError):
            radius = 10.0
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(c.get("window_border", "#d9d6cc")), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(body, radius, radius)

        # ---- 小墨环加载指示（替代旧的彩色加载环：一支"未干的墨圈"）----
        cx, cy, r = w / 2.0, self.height() - 86.0, 11.0
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)
        p.setPen(QPen(QColor(ink_faint), 2.4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rect)
        angle = (time.monotonic() * 220.0) % 360
        pen = QPen(QColor(ink_strong), 2.4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, -int(angle * 16), -95 * 16)

        p.setPen(QColor(ink_dim))
        f = p.font()
        f.setPixelSize(13)
        p.setFont(f)
        p.drawText(QRectF(0, self.height() - 58, w, 22),
                   Qt.AlignHCenter, self._status)

        p.setPen(QColor(ink_faint))
        f2 = p.font()
        f2.setPixelSize(11)
        p.setFont(f2)
        p.drawText(QRectF(0, self.height() - 32, w, 18),
                   Qt.AlignHCenter, "Yuhub " + VERSION_LABEL)
        p.end()


class SplashScreen(QWidget):
    """无边框水墨启动画面。用法：

        splash = SplashScreen()
        splash.show()
        ...  数据就绪后：
        splash.finish()          # close → finished 信号
    """

    finished = Signal()

    def __init__(self, min_duration_ms=2000, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(POND_W, POND_H)

        self._min_duration_ms = int(min_duration_ms)
        self._shown_at = time.monotonic()

        # 鱼池（子控件，自带 60fps 动画）与盖在其上的笔墨叠加层
        self._pond = ink.InkPond(fish_count=FISH_COUNT, parent=self)
        self._pond.setGeometry(0, 0, POND_W, POND_H)
        self._overlay = _SplashOverlay(self)
        self._overlay.setGeometry(0, 0, POND_W, POND_H)

        self._center_on_screen()

        # 叠加层的墨圈要一直转（角度取自墙钟），用定时器推着重画
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._overlay.update)
        self._timer.start(16)

    def _center_on_screen(self):
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen is not None:
            g = screen.availableGeometry()
            self.move(g.center() - self.rect().center())

    # ---------------------------------------------------------------- 状态
    def set_status(self, text):
        """更新池边的状态文字。"""
        self._overlay.set_status(text)

    def _backdrop_pixmap(self):
        """兼容旧自检/调用方的接口：池底 pixmap（现在由 InkPond 持有缓存）。"""
        return self._pond._backdrop()

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
