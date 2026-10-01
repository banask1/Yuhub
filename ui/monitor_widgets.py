"""Yuhub 实时监控 UI：迷你趋势条 / 监控指标块 / 详情弹窗。

设计要点：
  - 趋势条用自绘（QRectF 折线 + 面积填充），60 个采样点约等于 1 分钟走势。
  - 指标块可点击 → 弹出详情窗（显示生产商、序列号、固件等静态参数 + 当前实时值）。
  - 详情窗的数据按需查询（约 2 秒），弹出时先显示"读取中"，不阻塞主窗口。
"""

from PySide6.QtCore import Qt, QTimer, Signal, QPointF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QBrush
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

import threading

from . import theme
import live_monitor as lm


def _gpu_mem_text(gpu):
    """显卡显存文本："已用 / 总量 MB"。

    两半来自不同数据源（总量读注册表、已用读 PDH 计数器），在部分驱动上
    可能只有一半有值。缺哪半就只显示哪半——直接格式化 None 会抛
    TypeError，把整个实时刷新线程带崩（这是 A 卡用户会碰到的路径）。
    """
    used = gpu.get("mem_used_mb")
    total = gpu.get("mem_total_mb")
    if used is not None and total is not None:
        return "%.0f / %.0f MB" % (used, total)
    if total is not None:
        return "共 %.0f MB" % total
    if used is not None:
        return "已用 %.0f MB" % used
    return ""


# ---------------------------------------------------------------------------
# 迷你趋势条
# ---------------------------------------------------------------------------
class Sparkline(QWidget):
    """极简趋势条：最近 N 个采样点的折线 + 半透明面积填充。

    用 hover 之外的最小交互（不响应鼠标），纯粹作为视觉辅助。
    """

    def __init__(self, capacity=60, tint_key="accent", parent=None):
        super().__init__(parent)
        self._capacity = capacity
        self._tint_key = tint_key
        self._values = []
        self.setMinimumHeight(30)
        self.setMaximumHeight(34)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def push(self, value):
        """追加一个 0~100 的采样值。"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        self._values.append(max(0.0, min(100.0, v)))
        if len(self._values) > self._capacity:
            self._values = self._values[-self._capacity:]
        self.update()

    def clear(self):
        self._values = []
        self.update()

    def paintEvent(self, event):
        p = theme.current()
        color = QColor(p.get(self._tint_key, p["accent"]))
        w = self.width()
        h = self.height()
        if w <= 2 or h <= 2:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 基线（0% 参考线）
        base_pen = QPen(QColor(p["border"]))
        base_pen.setWidth(1)
        painter.setPen(base_pen)
        painter.drawLine(0, h - 1, w, h - 1)

        vals = self._values
        if len(vals) < 2:
            painter.end()
            return

        n = len(vals)
        # 采样点画满整个宽度，靠右对齐：最新点在右侧
        step = w / (self._capacity - 1) if self._capacity > 1 else w
        x0 = w - (n - 1) * step

        pts = []
        for i, v in enumerate(vals):
            x = x0 + i * step
            y = h - 2 - (v / 100.0) * (h - 4)
            pts.append(QPointF(x, y))

        # 面积填充
        area = QPainterPath()
        area.moveTo(pts[0].x(), h - 1)
        for pt in pts:
            area.lineTo(pt)
        area.lineTo(pts[-1].x(), h - 1)
        area.closeSubpath()

        fill = QColor(color)
        fill.setAlpha(38)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(fill))
        painter.drawPath(area)

        # 折线
        line = QPainterPath()
        line.moveTo(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)
        pen = QPen(color)
        pen.setWidth(2)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(line)

        # 最新点高亮
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(color))
        last = pts[-1]
        painter.drawEllipse(last, 2.5, 2.5)

        painter.end()


# ---------------------------------------------------------------------------
# 单个监控指标块
# ---------------------------------------------------------------------------
class MetricTile(QFrame):
    """指标块：图标 + 名称 + 大号百分比 + 副信息 + 趋势条。点击可打开详情。"""

    clicked = Signal(str)

    def __init__(self, key, label, icon, tint_key="tile_1", parent=None):
        super().__init__(parent)
        self.setObjectName("MetricTile")
        self._key = key
        self._tint_key = tint_key
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(132)
        self.setToolTip("点击查看详细参数")

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(6)

        # 第一行：色块图标 + 名称 + 右侧箭头
        top = QHBoxLayout()
        top.setSpacing(8)
        self._tile = QFrame()
        self._tile.setFixedSize(26, 26)
        tl = QVBoxLayout(self._tile)
        tl.setContentsMargins(0, 0, 0, 0)
        ic = QLabel(icon)
        ic.setAlignment(Qt.AlignCenter)
        ic.setStyleSheet("font-size: 13px; background: transparent; color: #ffffff;")
        tl.addWidget(ic)
        top.addWidget(self._tile, 0, Qt.AlignVCenter)

        self._label = QLabel(label)
        self._label.setStyleSheet("font-size: 12px; font-weight: 700;")
        top.addWidget(self._label)
        top.addStretch(1)

        arrow = QLabel("›")
        arrow.setObjectName("Faint")
        arrow.setStyleSheet("font-size: 15px; background: transparent;")
        top.addWidget(arrow, 0, Qt.AlignVCenter)
        root.addLayout(top)

        # 大号数值
        self._value = QLabel("--")
        self._value.setStyleSheet("font-size: 26px; font-weight: 800;")
        root.addWidget(self._value)

        # 副信息
        self._detail = QLabel("")
        self._detail.setObjectName("Faint")
        self._detail.setStyleSheet("font-size: 11px;")
        root.addWidget(self._detail)

        # 趋势条
        self.spark = Sparkline(60, tint_key)
        root.addWidget(self.spark)

        theme.bus.changed.connect(lambda _: self._restyle())
        self._restyle()

    def _restyle(self):
        p = theme.current()
        color = p.get(self._tint_key, p["accent"])
        self._tile.setStyleSheet(
            f"QFrame {{ background: {color}; border: none; border-radius: 4px; }}"
        )
        self.setStyleSheet(
            f"QFrame#MetricTile {{ background: {p['surface_sunken']}; "
            f"border: 1px solid {p['border']}; border-radius: 5px; }}"
            f"QFrame#MetricTile:hover {{ border: 1px solid {color}; }}"
            f"QLabel {{ background: transparent; }}"
        )

    def set_metric(self, percent, detail, suffix="%"):
        """更新数值与副信息。percent 为 None 时显示不可用。"""
        if percent is None:
            self._value.setText("--")
        else:
            self._value.setText(f"{percent:.0f}{suffix}")
            self.spark.push(percent)
        self._detail.setText(detail or "")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._key)
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# 详情弹窗
# ---------------------------------------------------------------------------
class DialogHeader(QFrame):
    """详情弹窗的自定义标题栏：负责拖动移动窗口 + 关闭按钮。"""

    def __init__(self, window, title, icon, tint_key):
        super().__init__(window)
        self.setObjectName("DialogHeader")
        self.setFixedHeight(44)
        self._window = window
        self._drag_offset = None

        p = theme.current()
        color = p.get(tint_key, p["accent"])

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        tile = QFrame()
        tile.setFixedSize(28, 28)
        tl = QVBoxLayout(tile)
        tl.setContentsMargins(0, 0, 0, 0)
        ic = QLabel(icon)
        ic.setAlignment(Qt.AlignCenter)
        ic.setStyleSheet("font-size: 14px; background: transparent; color: #ffffff;")
        tl.addWidget(ic)
        tile.setStyleSheet(
            f"QFrame {{ background: {color}; border: none; border-radius: 4px; }}"
        )
        lay.addWidget(tile, 0, Qt.AlignVCenter)

        t = QLabel(title)
        t.setObjectName("DialogTitle")
        lay.addWidget(t)
        lay.addStretch(1)

        close = QPushButton("✕")
        close.setObjectName("TitleButton")
        close.setProperty("danger", True)
        close.setFixedSize(32, 28)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("关闭")
        close.clicked.connect(self._window.close)
        lay.addWidget(close, 0, Qt.AlignVCenter)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None


class DetailDialog(QDialog):
    """部件详情弹窗：左列为参数名（灰），右列为值（亮）。

    无系统标题栏（去掉 Windows 白框），改用自绘标题栏，可拖动移动。
    数据按需从 PowerShell 拉取（约 2 秒），先渲染骨架再填充，避免卡顿。
    """

    def __init__(self, title, icon, tint_key, rows, live_rows=None, parent=None):
        """rows: [(label, value), ...] 静态参数；live_rows 为实时参数（会随刷新更新）。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.setMinimumSize(560, 460)
        # 去掉系统白框：无边框 + 透明底（圆角与边框由内部 DialogFrame 提供）
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(0)

        self._frame = QFrame()
        self._frame.setObjectName("DialogFrame")
        outer.addWidget(self._frame)

        root = QVBoxLayout(self._frame)
        root.setContentsMargins(16, 10, 16, 14)
        root.setSpacing(12)

        # 自绘标题栏（可拖动）
        root.addWidget(DialogHeader(self, title, icon, tint_key))

        # 可滚动参数区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        holder = QWidget()
        self._grid = QVBoxLayout(holder)
        self._grid.setContentsMargins(0, 0, 6, 0)
        self._grid.setSpacing(0)
        self._grid.setAlignment(Qt.AlignTop)

        self._live_labels = {}
        if live_rows:
            self._add_section("实时状态")
            for label, value in live_rows:
                lbl = self._add_row(label, value, highlight=True)
                self._live_labels[label] = lbl

        self._add_section("规格参数")
        for label, value in rows:
            self._add_row(label, value)

        self._grid.addStretch(1)
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        # 关闭按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("GhostButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.close)
        btn_row.addWidget(close)
        root.addLayout(btn_row)

        self.setStyleSheet(theme.build_qss())
        theme.bus.changed.connect(self._on_theme)

    def _on_theme(self):
        self.setStyleSheet(theme.build_qss())
        self.update()

    def _add_section(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("DialogSection")
        self._grid.addWidget(lbl)

    def _add_row(self, label, value, highlight=False):
        row = QFrame()
        row.setObjectName("DetailRow")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(12)

        k = QLabel(label)
        k.setObjectName("DialogKey")
        k.setStyleSheet("font-size: 12px;")
        k.setFixedWidth(132)
        k.setWordWrap(True)
        lay.addWidget(k, 0, Qt.AlignTop)

        v = QLabel(str(value) if value not in (None, "") else "--")
        v.setObjectName("DialogValStrong" if highlight else "DialogVal")
        v.setWordWrap(True)
        v.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.setStyleSheet(
            "font-size: 12px; font-weight: 700;" if highlight else "font-size: 12px;"
        )
        lay.addWidget(v, 1)

        self._grid.addWidget(row)
        return v

    def update_live(self, values):
        """按 label 更新实时值（供外部定时刷新时调用）。"""
        for label, value in values.items():
            if label in self._live_labels:
                self._live_labels[label].setText(str(value))


# ---------------------------------------------------------------------------
# 监控区（三块指标 + 详情弹窗调度）
# ---------------------------------------------------------------------------
class LiveMonitorPanel(QFrame):
    """实时监控面板：CPU / 内存 / 显卡 三块指标，可点开详情。"""

    _detail_ready = Signal(int, str, object)  # (seq, kind, rows)

    def __init__(self, monitor_factory=None, parent=None):
        super().__init__(parent)
        self.setObjectName("LivePanel")
        self._monitor_factory = monitor_factory
        self._monitor = None
        self._dialog = None
        self._dialog_kind = None
        self._last = {}
        self._pending_seq = 0
        self._detail_ready.connect(self._on_detail_ready)
        self._build()
        theme.bus.changed.connect(lambda _: self._restyle())

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("⚡  实时监控")
        title.setObjectName("CardTitle")
        head.addWidget(title)

        self._pulse = QLabel("● 实时")
        self._pulse.setStyleSheet("font-size: 11px; font-weight: 700;")
        head.addWidget(self._pulse)

        head.addStretch(1)

        self._status = QLabel("启动中")
        self._status.setObjectName("Faint")
        self._status.setStyleSheet("font-size: 11px;")
        head.addWidget(self._status)

        root.addLayout(head)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.tiles = {
            "cpu": MetricTile("cpu", "CPU", "🧠", "tile_1"),
            "mem": MetricTile("mem", "内存", "🧩", "tile_4"),
            "gpu": MetricTile("gpu", "显卡", "🎮", "tile_2"),
        }
        for tile in self.tiles.values():
            tile.clicked.connect(self.open_detail)
        grid.addWidget(self.tiles["cpu"], 0, 0)
        grid.addWidget(self.tiles["mem"], 0, 1)
        grid.addWidget(self.tiles["gpu"], 0, 2)
        root.addLayout(grid)

        self._sub = QLabel("")
        self._sub.setObjectName("Faint")
        self._sub.setStyleSheet("font-size: 11px;")
        self._sub.setWordWrap(True)
        root.addWidget(self._sub)

    # ------------------------------------------------------------ 样式
    def _restyle(self):
        p = theme.current()
        self.setStyleSheet(
            f"QFrame#LivePanel {{ background: {p['card_top']}; "
            f"border: 1px solid {p['border']}; border-radius: 5px; }}"
            f"QLabel {{ background: transparent; }}"
        )
        self._pulse.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {p['green']}; background: transparent;"
        )

    def paintEvent(self, event):
        from PySide6.QtWidgets import QStyleOption, QStyle
        from PySide6.QtGui import QPainter as _P

        opt = QStyleOption()
        opt.initFrom(self)
        pt = _P(self)
        self.style().drawPrimitive(QStyle.PE_Widget, opt, pt, self)

    # ------------------------------------------------------------ 生命周期
    def start(self):
        if self._monitor_factory is None:
            return
        if self._monitor is None:
            self._monitor = self._monitor_factory()
            self._monitor.sample.connect(self._on_sample)
        self._monitor.start()
        self._status.setText("采样中…")

    def stop(self):
        if self._monitor:
            self._monitor.stop()

    def shutdown(self):
        self.stop()
        if self._dialog is not None:
            self._dialog.close()

    # ------------------------------------------------------------ 数据
    def _on_sample(self, data):
        self._last = data
        cpu = data.get("cpu") or {}
        mem = data.get("mem")
        gpu = data.get("gpu")

        # CPU
        if cpu.get("valid"):
            self.tiles["cpu"].set_metric(cpu["percent"], "")
        else:
            self.tiles["cpu"].set_metric(None, "采样不可用")

        # 内存
        if mem:
            detail = f"{lm.fmt_bytes(mem['used_bytes'])} / {lm.fmt_bytes(mem['total_bytes'])}"
            self.tiles["mem"].set_metric(mem["percent"], detail)
        else:
            self.tiles["mem"].set_metric(None, "读取失败")

        # 显卡
        if gpu:
            bits = []
            mem_txt = _gpu_mem_text(gpu)
            if mem_txt:
                bits.append(mem_txt)
            if gpu.get("temp") is not None:
                bits.append(f"{gpu['temp']:.0f}°C")
            self.tiles["gpu"].set_metric(gpu.get("util"), " · ".join(bits))
        else:
            self.tiles["gpu"].set_metric(None, "显卡数据不可用")

        # 底部其他实时参数
        parts = []
        if gpu:
            if gpu.get("power_w") is not None:
                parts.append(f"显卡功耗 {gpu['power_w']:.1f} W")
            if gpu.get("clock_sm_mhz") is not None and gpu.get("clock_max_mhz"):
                parts.append(f"核心 {gpu['clock_sm_mhz']:.0f}/{gpu['clock_max_mhz']:.0f} MHz")
        disk = data.get("disk")
        if disk:
            parts.append(
                f"{disk['drive']} 剩余 {lm.fmt_bytes(disk['free_bytes'])}"
                f" / {lm.fmt_bytes(disk['total_bytes'])}"
            )
        self._sub.setText("　·　".join(parts))

        # 同步刷新已打开的详情窗
        self._refresh_dialog()

    def _refresh_dialog(self):
        if self._dialog is None or not self._dialog.isVisible():
            return
        d = self._last
        kind = self._dialog_kind
        live = {}
        if kind == "cpu" and d.get("cpu", {}).get("valid"):
            live["当前占用率"] = f"{d['cpu']['percent']:.1f} %"
        if kind == "mem" and d.get("mem"):
            m = d["mem"]
            live["当前占用率"] = f"{m['percent']:.1f} %"
            live["已使用"] = lm.fmt_bytes(m["used_bytes"])
            live["可用"] = lm.fmt_bytes(m["avail_bytes"])
        if kind == "gpu" and d.get("gpu"):
            g = d["gpu"]
            if g.get("util") is not None:
                live["当前占用率"] = f"{g['util']:.0f} %"
            mem_txt = _gpu_mem_text(g)
            if mem_txt:
                live["显存占用"] = mem_txt
            if g.get("temp") is not None:
                live["当前温度"] = f"{g['temp']:.0f} °C"
            if g.get("power_w") is not None:
                live["当前功耗"] = f"{g['power_w']:.1f} W"
            if g.get("clock_sm_mhz") is not None:
                live["核心频率"] = (
                    f"{g['clock_sm_mhz']:.0f} MHz"
                    + (f" / 最大 {g['clock_max_mhz']:.0f} MHz" if g.get("clock_max_mhz") else "")
                )
        self._dialog.update_live(live)

    # ------------------------------------------------------------ 详情
    def open_detail(self, kind):
        """打开部件详情窗：先弹空壳，再后台取数填充。"""
        self._dialog_kind = kind
        titles = {
            "cpu": ("处理器详情", "🧠", "tile_1"),
            "mem": ("内存详情", "🧩", "tile_4"),
            "gpu": ("显卡详情", "🎮", "tile_2"),
        }
        title, icon, tint = titles.get(kind, ("详情", "📦", "tile_1"))

        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None

        dlg = DetailDialog(title, icon, tint, [("状态", "读取中")], parent=self.window())
        self._dialog = dlg
        dlg.show()

        # 静态参数要走 PowerShell（约 2 秒），放到子线程查询，避免冻住界面。
        QTimer.singleShot(50, lambda: self._fill_detail(kind))

    def _fill_detail(self, kind):
        dlg = self._dialog
        if dlg is None or not dlg.isVisible():
            return

        # 记忆化缓存命中时直接同步填充（毫秒级）
        if kind in lm._DETAIL_CACHE:
            rows = _build_detail_rows(kind, self._last)
            if self._dialog is not None and self._dialog.isVisible():
                self._open_detail_with_rows(kind, rows)
            return

        self._pending_seq = getattr(self, "_pending_seq", 0) + 1
        seq = self._pending_seq

        def worker():
            rows = _build_detail_rows(kind, self._last)
            self._detail_ready.emit(seq, kind, rows)

        threading.Thread(target=worker, name="YuhubDetailQuery", daemon=True).start()

    def _on_detail_ready(self, seq, kind, rows):
        # 只有最新一次请求才允许落地，避免快速连点导致错配
        if seq != getattr(self, "_pending_seq", 0):
            return
        if self._dialog is None or not self._dialog.isVisible():
            return
        self._open_detail_with_rows(kind, rows)

    def _open_detail_with_rows(self, kind, rows):
        titles = {
            "cpu": ("处理器详情", "🧠", "tile_1"),
            "mem": ("内存详情", "🧩", "tile_4"),
            "gpu": ("显卡详情", "🎮", "tile_2"),
        }
        title, icon, tint = titles.get(kind, ("详情", "📦", "tile_1"))
        live_rows = _build_live_rows(kind, self._last)
        geom = self._dialog.geometry() if self._dialog else None
        if self._dialog is not None:
            self._dialog.close()
        dlg = DetailDialog(title, icon, tint, rows, live_rows, parent=self.window())
        self._dialog = dlg
        if geom is not None:
            dlg.resize(geom.size())
            dlg.move(geom.topLeft())
        dlg.show()


# ---------------------------------------------------------------------------
# 详情内容组装
# ---------------------------------------------------------------------------
def _build_live_rows(kind, data):
    """实时参数行（会在详情窗打开期间持续刷新）。"""
    out = []
    if kind == "cpu":
        c = data.get("cpu") or {}
        if c.get("valid"):
            out.append(("当前占用率", f"{c['percent']:.1f} %"))
    elif kind == "mem":
        m = data.get("mem")
        if m:
            out += [
                ("当前占用率", f"{m['percent']:.1f} %"),
                ("已使用", lm.fmt_bytes(m["used_bytes"])),
                ("可用", lm.fmt_bytes(m["avail_bytes"])),
            ]
    elif kind == "gpu":
        g = data.get("gpu")
        if g:
            if g.get("util") is not None:
                out.append(("当前占用率", f"{g['util']:.0f} %"))
            mem_txt = _gpu_mem_text(g)
            if mem_txt:
                out.append(("显存占用", mem_txt))
            if g.get("temp") is not None:
                out.append(("当前温度", f"{g['temp']:.0f} °C"))
            if g.get("power_w") is not None:
                out.append(("当前功耗", f"{g['power_w']:.1f} W"))
    return out


def _build_detail_rows(kind, live):
    """静态规格参数行（点开时查一次）。"""
    rows = []

    def add(k, v):
        if v not in (None, "", "0", 0):
            rows.append((k, v))
        else:
            rows.append((k, "--"))

    if kind == "cpu":
        d = lm.query_component_detail("cpu") or {}
        add("型号", d.get("Name"))
        add("生产商", _cpu_vendor(d.get("Manufacturer")))
        add("核心 / 线程", f"{d.get('NumberOfCores', '--')} 核 / {d.get('NumberOfLogicalProcessors', '--')} 线程")
        add("基准频率", _mhz(d.get("MaxClockSpeed")))
        add("当前频率", _mhz(d.get("CurrentClockSpeed")))
        add("二级缓存", _kb(d.get("L2CacheSize")))
        add("三级缓存", _kb(d.get("L3CacheSize")))
        add("插槽", d.get("SocketDesignation"))
        add("架构", lm.ARCH_MAP.get(d.get("Architecture"), d.get("Architecture")))
        add("位宽", f"{d.get('DataWidth')} 位" if d.get("DataWidth") else None)
        add("处理器 ID", d.get("ProcessorId"))

    elif kind == "mem":
        d = lm.query_component_detail("ram") or {}
        mods = d.get("modules") or []
        if isinstance(mods, dict):
            mods = [mods]
        m = live.get("mem") or {}
        add("总容量", lm.fmt_bytes(m.get("total_bytes")) if m else None)
        add("插槽使用", f"{len(mods)} / {d.get('slots') or '--'} 条（可加装）" if d.get("slots") else f"{len(mods)} 条")
        total_cap = 0
        for i, mod in enumerate(mods, 1):
            cap = mod.get("Capacity") or 0
            total_cap += cap
            typ = lm.SMBIOS_MEM_TYPE.get(mod.get("SMBIOSMemoryType"), "")
            form = lm.FORM_FACTOR.get(mod.get("FormFactor"), "")
            rows.append((f"第 {i} 条 · 位置", mod.get("DeviceLocator") or mod.get("BankLabel") or "--"))
            rows.append((f"第 {i} 条 · 容量", f"{lm.fmt_bytes(cap)}　{typ}　{form}".strip()))
            rows.append((f"第 {i} 条 · 生产商", (mod.get("Manufacturer") or "--").strip()))
            rows.append((f"第 {i} 条 · 型号", (mod.get("PartNumber") or "--").strip()))
            rows.append((f"第 {i} 条 · 频率",
                         f"{mod.get('Speed', '--')} MT/s"
                         + (f"（实际 {mod.get('ConfiguredClockSpeed')} MT/s）"
                            if mod.get("ConfiguredClockSpeed") and mod.get("ConfiguredClockSpeed") != mod.get("Speed")
                            else "")))
            rows.append((f"第 {i} 条 · 序列号", (mod.get("SerialNumber") or "--").strip()))

    elif kind == "gpu":
        d = lm.query_component_detail("gpu") or {}
        ctrls = d.get("controllers") or []
        if isinstance(ctrls, dict):
            ctrls = [ctrls]
        if not ctrls:
            # WMI 没给出显卡（部分 AMD 驱动上 Win32_VideoController 会返回空）
            # → 从驱动注册表兜底，至少把型号 / 驱动版本列出来，别是一片"--"
            ctrls = [{"Name": n, "DriverVersion": v or None}
                     for n, _mem, v in _reg_gpu_rows()]
        for i, c in enumerate(ctrls, 1):
            prefix = f"显卡 {i} · " if len(ctrls) > 1 else ""
            rows.append((prefix + "型号", c.get("Name") or "--"))
            rows.append((prefix + "生产商", c.get("AdapterCompatibility") or "--"))
            rows.append((prefix + "显存", _vram_of(i - 1, c.get("Name"))))
            res = c.get("CurrentHorizontalResolution")
            resv = c.get("CurrentVerticalResolution")
            rows.append((prefix + "当前分辨率",
                         f"{res} × {resv}" if res and resv else "--"))
            rows.append((prefix + "驱动版本", c.get("DriverVersion") or "--"))
            rows.append((prefix + "驱动日期", _ps_date(c.get("DriverDate"))))
            rows.append((prefix + "设备状态", c.get("Status") or "--"))
            rows.append((prefix + "设备 ID", c.get("PNPDeviceID") or "--"))
        # 附加驱动上报的规格（NVIDIA 走 NVML、A 卡走 ADL + 注册表显存）
        g = live.get("gpu")
        if g:
            if g.get("mem_total_mb"):
                rows.append(("显存容量（驱动报告）", f"{g['mem_total_mb']:.0f} MB"))
            if g.get("clock_max_mhz"):
                rows.append(("最大核心频率", f"{g['clock_max_mhz']:.0f} MHz"))
            if g.get("power_limit_w"):
                rows.append(("功耗上限", f"{g['power_limit_w']:.1f} W"))

    return rows


def _cpu_vendor(raw):
    if not raw:
        return "--"
    r = str(raw)
    return {"GenuineIntel": "Intel（英特尔）",
            "AuthenticAMD": "AMD（超微半导体）"}.get(r, r)


def _mhz(v):
    try:
        return f"{float(v) / 1000:.2f} GHz"
    except (TypeError, ValueError):
        return "--"


def _kb(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "--"
    if n <= 0:
        return "--"
    return f"{n / 1024:.1f} MB"


def _reg_gpu_rows():
    """注册表里的显示适配器 [(名称, 显存字节, 驱动版本)]。

    详情页的兜底数据源：部分 AMD 驱动上 Win32_VideoController 返回空，
    详情页会是一片"--"。这里复用 hardware 里那份注册表读取实现，
    避免同样的解析逻辑写两遍。
    """
    try:
        import hardware
        return hardware._reg_gpus()
    except Exception:
        return []


def _vram_of(index, name):
    """查注册表拿真实显存（WMI 的 AdapterRAM 32 位会溢出）。"""
    sizes = lm._reg_vram_sizes()
    if sizes and name:
        for rname, rsize in sizes:
            if rname and (rname.lower() in name.lower() or name.lower() in rname.lower()):
                return lm.fmt_bytes(rsize)
    # 名字对不上时：全机只有一项注册表显存就直接用它——A 卡 WMI 名偶尔
    # 带 "(TM)"、厂商后缀，逐字匹配容易漏，漏了就只剩"--"。
    if len(sizes) == 1:
        return lm.fmt_bytes(sizes[0][1])
    return "见下方驱动报告"


def _ps_date(raw):
    """.NET JSON 的 /Date(ms)/ 格式 → 可读日期。"""
    if not raw:
        return "--"
    s = str(raw)
    if s.startswith("/Date(") and s.endswith(")/"):
        try:
            import datetime
            ms = int(s[6:-2])
            return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            return s
    return s
