"""多线程下载页。

参考 PCL2「更多 → 百宝箱 → 下载自定义文件」的交互范式：
粘贴链接 → 选保存位置 → 下载 → 实时进度 → 打开文件夹。

在此基础上做了三件 PCL2 没有直给的事：
  - 开始前先**后台探测**一次，拿到服务器给的真实文件名与文件大小
    （很多下载链接形如 /download?id=123，光看 URL 猜不出文件名）；
  - **分块可视化**：每个分块一个色块方块，能直观看出多线程在并行推进；
  - 指标格：已下载 / 总大小 / 瞬时速度 / 剩余时间，四项独立成格。

进度刷新用 200ms 定时器轮询引擎快照，而不是让工作线程发高频信号——
后者会在高速下载时把主线程事件循环冲垮。
"""

import os
import re
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

import downloader as dl
from .. import theme
from ..widgets import SegmentedControl, ghost_button, info_card, primary_button
from .base_page import BasePage

TICK_MS = 200                      # 进度轮询间隔
URL_DEBOUNCE_MS = 260              # 链接输入防抖，避免每敲一个字符就重算文件名


# ---------------------------------------------------------------------------
# 总进度条（自绘，按状态换色）
# ---------------------------------------------------------------------------
class DownloadBar(QFrame):
    """整体进度条。支持"总大小未知"的不确定态。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(12)
        self.setMinimumWidth(120)
        self._percent = 0.0
        self._indeterminate = False
        self._color_key = "accent"

    def set_state(self, percent, indeterminate=False, color_key="accent"):
        self._percent = max(0.0, min(100.0, float(percent or 0.0)))
        self._indeterminate = indeterminate
        self._color_key = color_key
        self.update()

    def paintEvent(self, event):
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # 底槽必须与卡片背景有色差，否则 0% 时整条"隐形"
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p.get("border_strong", p["border"])))
        painter.drawRoundedRect(0, 0, w, h, 3, 3)

        color = QColor(p.get(self._color_key, p["accent"]))
        if self._indeterminate:
            # 未知总大小：画一段中段色块表示"在跑但不知道还有多少"
            seg = max(60, int(w * 0.28))
            x = int((w - seg) * (self._percent / 100.0)) if self._percent else 0
            painter.setBrush(color)
            painter.drawRoundedRect(x, 0, min(seg, w - x), h, 3, 3)
        else:
            filled = int(w * self._percent / 100.0)
            if filled > 0:
                painter.setBrush(color)
                painter.drawRoundedRect(0, 0, max(filled, 4), h, 3, 3)
        painter.end()


# ---------------------------------------------------------------------------
# 分块可视化（一个分块一个方块）
# ---------------------------------------------------------------------------
class BlockStrip(QWidget):
    """把每个下载分块的完成度画成一排色块。

    左→右依次是分块 1..N；方块内部按进度从左填充。
    一眼能看出"是不是所有线程都在跑"，以及哪个块拖了后腿。
    """

    GAP = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(14)
        self.setMinimumWidth(120)
        self._blocks = []
        self._color_key = "accent"

    def set_blocks(self, fractions, color_key="accent"):
        self._blocks = list(fractions or [])
        self._color_key = color_key
        self.update()

    def paintEvent(self, event):
        if not self._blocks:
            return
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        n = len(self._blocks)
        w, h = self.width(), self.height()
        gap = self.GAP if n <= 24 else 1          # 分块多时把间隙压到 1px
        seg = max(2, (w - gap * (n - 1)) / n)
        empty = QColor(p.get("border_strong", p["border"]))
        fill = QColor(p.get(self._color_key, p["accent"]))

        for i, frac in enumerate(self._blocks):
            x = i * (seg + gap)
            painter.setPen(Qt.NoPen)
            painter.setBrush(empty)
            painter.drawRoundedRect(int(x), 0, max(2, int(seg)), h, 2, 2)
            frac = max(0.0, min(1.0, float(frac or 0.0)))
            if frac > 0:
                fw = int(seg * frac)
                if fw < 2 and frac > 0:
                    fw = 2
                painter.setBrush(fill)
                painter.drawRoundedRect(int(x), 0, max(2, fw), h, 2, 2)
        painter.end()


# ---------------------------------------------------------------------------
# 指标格
# ---------------------------------------------------------------------------
class Metric(QFrame):
    """一个小指标格：上方标签 + 下方数值。"""

    def __init__(self, label, value="--", parent=None):
        super().__init__(parent)
        self.setObjectName("DlMetric")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 9, 12, 9)
        v.setSpacing(3)
        self._label = QLabel(label)
        self._label.setObjectName("DlMetricLabel")
        self._value = QLabel(value)
        self._value.setObjectName("DlMetricValue")
        v.addWidget(self._label)
        v.addWidget(self._value)

    def set_value(self, text):
        self._value.setText(text or "--")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
class DownloadPage(BasePage):
    TINT_KEY = "tile_5"
    BADGE = "已上线"

    # 探测在工作线程执行，结果必须用信号投递回主线程
    _probed = Signal(object)

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "多线程下载",
            "粘贴链接即可分块并发下载，实时显示速度与剩余时间。",
            icon="⬇️",
            notify=notify,
            parent=parent,
        )
        self._task = None
        self._name_auto = True          # 文件名是否仍是"自动推断"状态
        self._ui_active = False         # 进度卡片当前是否在跟一个活动任务
        self._probing = False           # 是否处在"获取文件信息"阶段
        self._last_dir = ""             # 完成后「打开文件夹」用
        self._last_path = ""
        self._probed.connect(self._on_probed)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

        self._url_debounce = QTimer(self)
        self._url_debounce.setSingleShot(True)
        self._url_debounce.setInterval(URL_DEBOUNCE_MS)
        self._url_debounce.timeout.connect(self._refresh_auto_name)

        self._build_content()

    # ------------------------------------------------------------------ 构建
    def _build_content(self):
        self.add(self._build_task_card())
        self.add(self._build_progress_card())
        self.add(
            info_card(
                "关于多线程下载",
                "文件会被切成若干分块并行抓取，速度取决于服务器与你的带宽。"
                "线程数按本机的逻辑处理器（硬件线程）数换算：低档 1/4、中档 1/2、"
                "高档全部，所以换一台电脑不用改设置，程序会自动适配。"
                "若服务器不支持分块（未返回 Content-Range），会自动降级为单线程，"
                "此时无法显示百分比。下载中的文件以 .part 结尾，全部完成后再改名为正式文件；"
                "取消或失败时会自动清理半成品，不会留下垃圾。",
            )
        )
        self.add_stretch()

    def _build_task_card(self):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(12)

        title = QLabel("新建下载任务")
        title.setObjectName("CardTitle")
        v.addWidget(title)

        # --- 下载链接 ---
        v.addWidget(self._field_label("下载链接"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.url = QLineEdit()
        self.url.setPlaceholderText("粘贴下载链接，例如 https://example.com/file.zip")
        self.url.setClearButtonEnabled(True)
        self.url.textChanged.connect(lambda _: self._url_debounce.start())
        self.url.returnPressed.connect(self._on_start)
        row.addWidget(self.url, 1)
        btn_paste = ghost_button("粘贴")
        btn_paste.clicked.connect(self._on_paste)
        row.addWidget(btn_paste)
        v.addLayout(row)

        # --- 保存位置 ---
        v.addWidget(self._field_label("保存位置"))
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.folder = QLineEdit(self._default_folder())
        self.folder.setPlaceholderText("选择保存文件夹")
        row2.addWidget(self.folder, 1)
        btn_browse = ghost_button("浏览…")
        btn_browse.clicked.connect(self._on_browse)
        row2.addWidget(btn_browse)
        v.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(8)
        self.filename = QLineEdit()
        self.filename.setPlaceholderText("文件名（留空则用链接推断）")
        # textEdited 只在用户手打时触发；程序 setText 不会触发，
        # 这样"服务器给了更好的文件名就自动采用"和"用户改过就不再覆盖"能共存
        self.filename.textEdited.connect(self._on_name_edited)
        row3.addWidget(self.filename, 1)
        self.btn_open_dir = ghost_button("打开文件夹")
        self.btn_open_dir.setEnabled(False)
        self.btn_open_dir.clicked.connect(self._on_open_folder)
        row3.addWidget(self.btn_open_dir)
        v.addLayout(row3)

        # --- 线程档位 ---
        # 不写死 1/2/4/8/16：同一份程序要在不同电脑上跑。
        # 改成按本机核心数换算的低/中/高，具体线程数在右侧提示里显出来。
        row4 = QHBoxLayout()
        row4.setSpacing(10)
        row4.addWidget(self._field_label("下载线程"))
        self.threads = SegmentedControl(
            [(lv, dl.THREAD_LEVEL_LABELS[lv]) for lv in dl.THREAD_LEVELS],
            current=dl.DEFAULT_LEVEL,
        )
        row4.addWidget(self.threads)
        self.thread_hint = QLabel("")
        self.thread_hint.setObjectName("DlSub")
        row4.addWidget(self.thread_hint)
        row4.addStretch(1)
        v.addLayout(row4)
        self.threads.changed.connect(lambda _: self._update_thread_hint())
        self._update_thread_hint()

        # --- 按钮 ---
        row5 = QHBoxLayout()
        row5.setSpacing(8)
        row5.addStretch(1)
        self.btn_cancel = ghost_button("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        row5.addWidget(self.btn_cancel)
        self.btn_start = primary_button("开始下载")
        self.btn_start.clicked.connect(self._on_start)
        row5.addWidget(self.btn_start)
        v.addLayout(row5)

        return card

    def _build_progress_card(self):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.file_name = QLabel("--")
        self.file_name.setObjectName("DlFileName")
        top.addWidget(self.file_name, 1)
        self.state = QLabel("等待中")
        self.state.setObjectName("DlStateIdle")
        top.addWidget(self.state, 0, Qt.AlignTop)
        v.addLayout(top)

        bar_row = QHBoxLayout()
        bar_row.setSpacing(10)
        self.bar = DownloadBar()
        bar_row.addWidget(self.bar, 1)
        self.percent = QLabel("0.0%")
        self.percent.setObjectName("DlMetricValue")
        self.percent.setMinimumWidth(64)
        self.percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bar_row.addWidget(self.percent)
        v.addLayout(bar_row)

        blk_row = QHBoxLayout()
        blk_row.setSpacing(10)
        blk_label = QLabel("分块")
        blk_label.setObjectName("DlBlockLabel")
        blk_label.setFixedWidth(64)
        blk_row.addWidget(blk_label)
        self.blocks = BlockStrip()
        blk_row.addWidget(self.blocks, 1)
        v.addLayout(blk_row)

        grid = QGridLayout()
        grid.setSpacing(8)
        self.m_done = Metric("已下载")
        self.m_total = Metric("总大小")
        self.m_speed = Metric("速度")
        self.m_eta = Metric("剩余时间")
        grid.addWidget(self.m_done, 0, 0)
        grid.addWidget(self.m_total, 0, 1)
        grid.addWidget(self.m_speed, 0, 2)
        grid.addWidget(self.m_eta, 0, 3)
        v.addLayout(grid)

        self.sub = QLabel("")
        self.sub.setObjectName("DlSub")
        self.sub.setWordWrap(True)
        v.addWidget(self.sub)

        card.setVisible(False)
        self.progress_card = card
        return card

    def _field_label(self, text):
        lb = QLabel(text)
        lb.setObjectName("DlSub")
        return lb

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _default_folder():
        home = os.path.expanduser("~")
        for name in ("Downloads", "下载"):
            p = os.path.join(home, name)
            if os.path.isdir(p):
                return p
        return home

    def _update_thread_hint(self):
        level = self.threads.current() or dl.DEFAULT_LEVEL
        self.thread_hint.setText(dl.describe_level(level))

    def _on_paste(self):
        text = QApplication.clipboard().text().strip()
        if not text:
            self.toast("剪贴板里没有文本")
            return
        self.url.setText(text)
        self._url_debounce.stop()
        self._refresh_auto_name()

    def _refresh_auto_name(self):
        """链接变化时自动更新文件名（用户手动改过就不再覆盖）。"""
        if not self._name_auto:
            return
        url = self.url.text().strip()
        if not url:
            self.filename.clear()
            return
        name = dl.guess_filename(url)
        self.filename.setText("" if name == "download.bin" else name)

    def _on_name_edited(self, _text):
        self._name_auto = False

    def _on_browse(self):
        start = self.folder.text().strip() or self._default_folder()
        chosen = QFileDialog.getExistingDirectory(self, "选择保存文件夹", start)
        if chosen:
            self.folder.setText(os.path.normpath(chosen))

    def _on_open_folder(self):
        target = self._last_path or self._last_dir
        if not target:
            return
        try:
            if os.path.exists(target):
                os.startfile(os.path.dirname(target))     # noqa: S606
            elif os.path.isdir(self._last_dir):
                os.startfile(self._last_dir)
        except Exception as exc:  # noqa: BLE001
            self.toast(f"无法打开文件夹：{exc}")

    @staticmethod
    def _dedupe_path(path):
        """目标已存在时自动加序号，绝不覆盖既有文件。"""
        if not os.path.exists(path):
            return path, False
        base, ext = os.path.splitext(path)
        for i in range(1, 1000):
            cand = f"{base} ({i}){ext}"
            if not os.path.exists(cand):
                return cand, True
        return f"{base} ({os.getpid()}){ext}", True

    # ------------------------------------------------------------------ 启动
    def _on_start(self):
        if self._task is not None and self._task.running:
            self.toast("已有任务正在下载")
            return
        url = self.url.text().strip()
        if not url:
            self.toast("请先粘贴下载链接")
            self.url.setFocus()
            return
        if not re.match(r"^https?://", url, re.I):
            self.toast("链接需要以 http:// 或 https:// 开头")
            return
        folder = self.folder.text().strip()
        if not folder:
            self.toast("请选择保存文件夹")
            return
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception as exc:  # noqa: BLE001
                self.toast(f"保存文件夹不可用：{exc}")
                return

        # 先探测：拿服务器给的真实文件名（URL 里没有文件名时特别有用）
        self._probing = True
        self._set_busy(True)
        self._show_card()
        self.file_name.setText(self.filename.text().strip() or "获取文件信息…")
        self._set_state("probing")
        self.sub.setText("正在向服务器获取文件信息…")
        threading.Thread(
            target=lambda: self._probed.emit(dl.probe(url)),
            name="YuhubProbe", daemon=True,
        ).start()

    def _on_probed(self, result):
        """探测返回（主线程）。"""
        if not self._probing:
            return          # 探测期间用户已取消，别再自作主张开始下载
        self._probing = False
        if not result.ok:
            self._fail(result.error or "无法获取文件信息")
            return
        if self._name_auto and result.filename:
            self.filename.setText(result.filename)
        name = self.filename.text().strip() or result.filename or "download.bin"
        path = os.path.join(self.folder.text().strip(), name)
        path, renamed = self._dedupe_path(path)
        if renamed:
            self.filename.setText(os.path.basename(path))
            self.toast("同名文件已存在，已自动改名避免覆盖")

        self._last_path = path
        self._last_dir = os.path.dirname(path)
        self.file_name.setText(os.path.basename(path))
        self.btn_open_dir.setEnabled(False)

        try:
            threads = dl.threads_for_level(self.threads.current())
        except Exception:  # noqa: BLE001
            threads = dl.threads_for_level(dl.DEFAULT_LEVEL)

        self._task = dl.DownloadTask(self.url.text().strip(), path,
                                     threads=threads, probe_result=result)
        self._task.start()
        self._ui_active = True
        # 按钮文案要跟着阶段走：_set_busy(True) 写的是"获取信息…"，
        # 那是探测阶段的话术，任务真跑起来后必须换掉，否则用户看到的是"一直在获取信息"。
        self.btn_start.setText("下载中…")
        self.sub.setText("正在建立分块连接…")
        self._set_state("running")
        self._timer.start()

    # ------------------------------------------------------------------ 刷新
    def _show_card(self):
        if not self.progress_card.isVisible():
            self.progress_card.setVisible(True)

    def _tick(self):
        if self._task is None:
            self._timer.stop()
            return
        snap = self._task.snapshot()
        self._render(snap)
        if not snap["running"] and self._ui_active:
            self._ui_active = False
            self._timer.stop()
            self._on_finished(snap)

    def _render(self, snap):
        status = snap["status"]
        indeterminate = snap["total"] <= 0
        color_key = {
            "done": "green",
            "error": "red",
            "cancelled": "amber",
        }.get(status, "accent")

        if status == "running" and snap.get("cancelling"):
            color_key = "amber"

        pct = snap["percent"]
        if indeterminate and status == "running":
            # 未知总大小：让色块缓慢游走，表达"在跑但不知道进度"
            pct = (snap["downloaded"] / (20 * 1024 * 1024) * 100.0) % 100.0
        self.bar.set_state(pct, indeterminate=indeterminate, color_key=color_key)
        self.blocks.set_blocks(snap["blocks"], color_key=color_key)

        if indeterminate:
            self.percent.setText("--")
        else:
            self.percent.setText(f"{snap['percent']:.1f}%")

        self.m_done.set_value(dl.human_bytes(snap["downloaded"]))
        self.m_total.set_value(dl.human_bytes(snap["total"]) if snap["total"] else "未知")
        if status == "running":
            self.m_speed.set_value(dl.human_speed(snap["speed"] or snap["avg_speed"]))
            self.m_eta.set_value(dl.human_time(snap["eta"]))
        elif status == "done":
            self.m_speed.set_value(dl.human_speed(snap["avg_speed"]))
            self.m_eta.set_value(dl.human_time(snap["elapsed"]))
        else:
            self.m_speed.set_value("--")
            self.m_eta.set_value("--")

        # 说明行随阶段变化。数字都在指标格里，这里只说"在干什么"，避免重复。
        if status == "running":
            if snap.get("cancelling"):
                self.sub.setText("正在停止分块线程…")
            elif snap["single_thread"]:
                self.sub.setText("服务器不支持分块，已降级为单线程下载；总大小未知，无法显示百分比。")
            else:
                self.sub.setText(f"正在下载　·　{snap['block_count']} 个分块并行")

        self._set_state(status, snap)

    def _set_state(self, status, snap=None):
        text, obj = {
            "probing": ("获取信息", "DlStateIdle"),
            "running": ("下载中", "DlStateWarn"),
            "done": ("已完成", "DlStateOk"),
            "error": ("失败", "DlStateErr"),
            "cancelled": ("已取消", "DlStateIdle"),
            "idle": ("等待中", "DlStateIdle"),
        }.get(status, ("等待中", "DlStateIdle"))
        if status == "running" and snap and snap.get("cancelling"):
            text = "正在取消"
        self.state.setText(text)
        self.state.setObjectName(obj)
        # 换 objectName 后必须重新应用样式表，否则 QSS 不会重算
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)

    def _on_finished(self, snap):
        status = snap["status"]
        self._set_busy(False)
        if status == "done":
            n = snap["block_count"]
            mode = "单线程" if snap["single_thread"] else f"{n} 线程分块"
            self.sub.setText(
                f"已保存到 {self._last_path}　·　{mode}　·　"
                f"耗时 {dl.human_time(snap['elapsed'])}　·　"
                f"平均 {dl.human_speed(snap['avg_speed'])}"
            )
            self.btn_open_dir.setEnabled(True)
            self.toast(f"下载完成：{dl.human_bytes(snap['downloaded'])}，"
                       f"平均 {dl.human_speed(snap['avg_speed'])}")
        elif status == "cancelled":
            self.sub.setText("已取消，临时文件已清理。")
            self.toast("已取消下载")
        else:
            msg = snap["error"] or "下载失败"
            extra = "；".join(snap["messages"][:2])
            self.sub.setText(f"{msg}" + (f"（{extra}）" if extra else ""))
            self.toast(f"下载失败：{msg}")

    def _fail(self, message):
        """探测阶段就失败：任务还没建起来。"""
        self._probing = False
        self._set_busy(False)
        self._set_state("error")
        self.sub.setText(message)
        self.toast(f"下载失败：{message}")

    def _set_busy(self, busy):
        self.btn_start.setEnabled(not busy)
        self.btn_start.setText("获取信息…" if busy else "开始下载")
        self.btn_cancel.setEnabled(busy)
        self.url.setEnabled(not busy)
        self.folder.setEnabled(not busy)
        self.filename.setEnabled(not busy)
        self.threads.setEnabled(not busy)

    def _on_cancel(self):
        if self._task is not None and self._task.running:
            self._task.cancel()
            self.state.setText("正在取消")
            self.state.setObjectName("DlStateWarn")
            self.state.style().unpolish(self.state)
            self.state.style().polish(self.state)
            self.sub.setText("正在停止分块线程…")
            self.btn_cancel.setEnabled(False)
        elif self._probing:
            # 探测阶段取消：把标记清掉，探测线程回来后 _on_probed 会直接返回
            self._probing = False
            self._set_busy(False)
            self._set_state("cancelled")
            self.sub.setText("已取消。")

    # ------------------------------------------------------------------ 生命周期
    def shutdown(self):
        """窗口关闭时调用：停掉定时器与后台任务。"""
        self._timer.stop()
        self._probing = False
        if self._task is not None and self._task.running:
            self._task.cancel()
