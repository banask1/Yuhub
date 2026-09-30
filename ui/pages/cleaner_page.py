"""C盘清理页。

参考 Dism++（github.com/Chuyu-Team/Dism-Multi-language）的清理规则集，
提供按分组、可勾选的清理项：扫描 → 勾选 → 二次确认 → 清理 → 结果汇总。

安全设计：
  - 进入页面**自动扫描一次**（只统计体积，不删除任何文件），让用户立刻看到可清理量。
  - 扫描纯只读；清理前必弹二次确认，明确列出项数与体积。
  - 危险项（事件日志等不可恢复的）标注风险色，默认不勾选。
"""

import os
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

import cleaner as cl
from .. import theme
from ..widgets import primary_button, ghost_button, danger_button
from .base_page import BasePage


# ---------------------------------------------------------------------------
# 磁盘占用条（自绘，展示已用/可用比例）
# ---------------------------------------------------------------------------
class DiskBar(QFrame):
    """磁盘占用条：已用部分按占用率变色。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DiskBar")
        self.setFixedHeight(10)
        self._percent = 0.0

    def set_percent(self, p):
        self._percent = max(0.0, min(100.0, float(p)))
        self.update()

    def paintEvent(self, event):
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        ratio = self._percent / 100.0
        # 占用高时转红，中等转琥珀，低为绿
        if self._percent >= 90:
            color = p["red"]
        elif self._percent >= 75:
            color = p.get("amber", "#e0a020")
        else:
            color = p["green"]
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(0, 0, int(w * ratio), h, 3, 3)
        painter.end()


# ---------------------------------------------------------------------------
# 单个清理项行
# ---------------------------------------------------------------------------
class CleanRow(QFrame):
    """一条可勾选的清理项：勾选框 + 名称 + 说明 + 体积。"""

    toggled = Signal(str, bool)

    def __init__(self, rule, parent=None):
        super().__init__(parent)
        self.setObjectName("CleanRow")
        self.rule = rule
        self._dimmed = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 14, 10)
        lay.setSpacing(12)

        self.check = QCheckBox()
        self.check.setChecked(False)
        self.check.setCursor(Qt.PointingHandCursor)
        self.check.stateChanged.connect(
            lambda st: self.toggled.emit(self.rule.key, bool(st))
        )
        lay.addWidget(self.check, 0, Qt.AlignVCenter)

        theme.bus.changed.connect(lambda _: self.set_dim(self._dimmed))

        text = QVBoxLayout()
        text.setSpacing(2)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self.name = QLabel(rule.name)
        self.name.setObjectName("CleanName")
        name_row.addWidget(self.name)
        if rule.admin and not cl.is_admin():
            tag = QLabel("需管理员")
            tag.setObjectName("Warn")
            tag.setStyleSheet("font-size: 10px; font-weight: 700;")
            name_row.addWidget(tag)
        if rule.risky:
            tag = QLabel("不可恢复")
            tag.setObjectName("Danger")
            tag.setStyleSheet("font-size: 10px; font-weight: 700;")
            name_row.addWidget(tag)
        name_row.addStretch(1)
        text.addLayout(name_row)

        self.desc = QLabel(rule.desc)
        self.desc.setObjectName("CleanDesc")
        self.desc.setWordWrap(True)
        text.addWidget(self.desc)
        lay.addLayout(text, 1)

        self.size = QLabel("--")
        self.size.setObjectName("CleanSize")
        self.size.setMinimumWidth(78)
        self.size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.size, 0, Qt.AlignVCenter)

    def set_size(self, text, muted=False):
        self.size.setText(text)
        p = theme.current()
        color = p["text_faint"] if muted else p["text"]
        self.size.setStyleSheet(f"font-size: 13px; font-weight: 700; color: {color};")

    def set_dim(self, dim):
        """置灰整行（用于本机不存在的项）。"""
        self._dimmed = bool(dim)
        p = theme.current()
        if not dim:
            self.setStyleSheet("")
            self.name.setStyleSheet("")
            self.desc.setStyleSheet("")
            self.size.setStyleSheet("")
            return
        faint = p["text_faint"]
        border = p["border"]
        self.setStyleSheet(
            f"QFrame#CleanRow {{ background: transparent; "
            f"border: 1px dashed {border}; border-radius: 4px; }}"
        )
        self.name.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {faint};"
        )
        self.desc.setStyleSheet(f"font-size: 11px; color: {faint};")
        self.size.setStyleSheet(
            f"font-size: 13px; font-weight: 700; color: {faint};"
        )

    def is_checked(self):
        return self.check.isChecked()

    def set_checked(self, v):
        self.check.setChecked(v)


# ---------------------------------------------------------------------------
# 确认弹窗（无系统边框，与详情弹窗风格一致）
# ---------------------------------------------------------------------------
class ConfirmDialog(QDialog):
    """清理前二次确认。列出项数与预估体积，需用户点「确认清理」。"""

    confirmed = Signal()

    def __init__(self, rows, total_bytes, parent=None, explorer_hint=False,
                 admin_count=0):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setWindowTitle("确认清理")
        self._drag = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        frame = QFrame()
        frame.setObjectName("DialogFrame")
        outer.addWidget(frame)

        root = QVBoxLayout(frame)
        root.setContentsMargins(20, 14, 20, 18)
        root.setSpacing(12)

        # 自绘标题栏
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("确认清理")
        title.setObjectName("DialogTitle")
        head.addWidget(title)
        head.addStretch(1)
        close = QPushButton("✕")
        close.setObjectName("TitleButton")
        close.setProperty("danger", True)
        close.setFixedSize(32, 28)
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.close)
        head.addWidget(close)
        root.addLayout(head)

        warn = QLabel(
            f"即将清理 <b>{len(rows)}</b> 项，预计释放 <b>{cl.human_bytes(total_bytes)}</b>。"
            "此操作<b>不可撤销</b>。"
        )
        warn.setTextFormat(Qt.RichText)
        warn.setWordWrap(True)
        warn.setObjectName("CardDesc")
        root.addWidget(warn)

        if explorer_hint:
            hint = QLabel(
                "ℹ️ 选中的项目里有缩略图缓存。如果个别文件正被资源管理器占用，"
                "程序会自动重启资源管理器来释放它们——<b>任务栏会短暂消失 1~3 秒</b>，"
                "随后自动恢复。"
            )
            hint.setTextFormat(Qt.RichText)
            hint.setWordWrap(True)
            hint.setObjectName("Warn")
            hint.setStyleSheet("font-size: 11px;")
            root.addWidget(hint)

        if admin_count:
            hint = QLabel(
                f"ℹ️ 其中 <b>{admin_count}</b> 项需要管理员权限（系统更新缓存、"
                "系统日志等）。清理到它们时会弹一次 <b>UAC 授权框</b>，"
                "点「是」即可继续——期间没有任何额外窗口，清理完自动回到这里。"
            )
            hint.setTextFormat(Qt.RichText)
            hint.setWordWrap(True)
            hint.setObjectName("Warn")
            hint.setStyleSheet("font-size: 11px;")
            root.addWidget(hint)

        box = QVBoxLayout()
        box.setSpacing(4)
        for name, size in rows[:12]:
            r = QHBoxLayout()
            r.setSpacing(8)
            n = QLabel("· " + name)
            n.setObjectName("CleanDesc")
            s = QLabel(cl.human_bytes(size))
            s.setObjectName("CleanDesc")
            r.addWidget(n)
            r.addStretch(1)
            r.addWidget(s)
            box.addLayout(r)
        if len(rows) > 12:
            more = QLabel(f"… 另有 {len(rows) - 12} 项")
            more.setObjectName("Faint")
            box.addWidget(more)
        root.addLayout(box)
        root.addStretch(1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = ghost_button("取消")
        cancel.clicked.connect(self.close)
        btns.addWidget(cancel)
        ok = danger_button("确认清理")
        ok.clicked.connect(self._on_ok)
        btns.addWidget(ok)
        root.addLayout(btns)

        self.setStyleSheet(theme.build_qss())
        theme.bus.changed.connect(lambda _: self.setStyleSheet(theme.build_qss()))
        # 提示条会占高度，按数量动态给窗口尺寸，避免内容被截断
        extra = (62 if explorer_hint else 0) + (78 if admin_count else 0)
        self.resize(520, 420 + extra)

    def _on_ok(self):
        self.close()
        self.confirmed.emit()

    # 拖动窗口（按住顶部标题区）
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() < 56:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag = None


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
class CleanerPage(BasePage):
    TINT_KEY = "tile_3"
    BADGE = "已上线"

    _scanned = Signal(object)           # 单条规则扫描完成
    _scan_done = Signal(object, object)  # (total_bytes, rules) —— 字节数可能超 32 位
    _clean_rule_done = Signal(object)
    _clean_done = Signal(object)
    _clean_status = Signal(str, str)
    _clean_elevated_done = Signal(object)   # 提权清理返回（None = UAC 被取消）

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "C盘清理",
            "参考 Dism++ 规则清理 C 盘上的临时文件、系统缓存与显卡着色器缓存，安全释放空间。",
            icon="🧹",
            notify=notify,
            parent=parent,
        )
        self.rules = cl.build_rules()
        self.rows = {}
        self._scanner = cl.CleanerScanner()
        self._worker = cl.CleanerWorker()
        self._busy = False
        self._clean_total = 0
        self._confirm_dlg = None
        self._results = []
        self._pending_elevated = []       # 待提权清理的规则（普通清理跑完再处理）
        self._elevated_rules = []         # 本次提权清理涉及的规则
        self._auto_scanned = False       # 首次进入自动扫描一次
        self._has_scan_result = False    # 本次进入是否已有扫描数据

        self._scanned.connect(self._on_rule_scanned)
        self._scan_done.connect(self._on_scan_done)
        self._clean_rule_done.connect(self._on_clean_rule_done)
        self._clean_done.connect(self._on_clean_done)
        self._clean_status.connect(self._on_clean_status)
        self._clean_elevated_done.connect(self._on_elevated_done)

        self._build_content()
        self._refresh_disk()
        self._disk_timer = QTimer(self)
        self._disk_timer.setInterval(3000)
        self._disk_timer.timeout.connect(self._refresh_disk)
        self._disk_timer.start()

    # ------------------------------------------------------------ 构建
    def _build_content(self):
        # ---- 磁盘概览 ----
        disk_card = QFrame()
        disk_card.setObjectName("Card")
        dv = QVBoxLayout(disk_card)
        dv.setContentsMargins(18, 14, 18, 16)
        dv.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._disk_title = QLabel("磁盘 C:")
        self._disk_title.setObjectName("CardTitle")
        top.addWidget(self._disk_title)
        top.addStretch(1)
        self._disk_info = QLabel("读取中")
        self._disk_info.setObjectName("CardDesc")
        top.addWidget(self._disk_info)
        dv.addLayout(top)

        self._disk_bar = DiskBar()
        dv.addWidget(self._disk_bar)
        self._disk_free = QLabel("")
        self._disk_free.setObjectName("Faint")
        self._disk_free.setStyleSheet("font-size: 11px;")
        dv.addWidget(self._disk_free)
        self.add(disk_card)

        # ---- 操作栏 ----
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.btn_scan = primary_button("开始扫描")
        self.btn_scan.clicked.connect(self.start_scan)
        bar.addWidget(self.btn_scan)

        self.btn_clean = danger_button("清理选中项")
        self.btn_clean.setEnabled(False)
        self.btn_clean.clicked.connect(self._confirm_clean)
        bar.addWidget(self.btn_clean)

        self.btn_all = ghost_button("全选安全项")
        self.btn_all.clicked.connect(self._select_safe)
        bar.addWidget(self.btn_all)

        self.btn_none = ghost_button("取消全选")
        self.btn_none.clicked.connect(self._select_none)
        bar.addWidget(self.btn_none)

        bar.addStretch(1)
        self._sel_info = QLabel("已选 0 项 · 0 B")
        self._sel_info.setObjectName("Muted")
        self._sel_info.setStyleSheet("font-size: 12px;")
        bar.addWidget(self._sel_info)
        self.add_layout(bar)

        # ---- 进度条 ----
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        self._bar.hide()
        self.add(self._bar)

        self._status = QLabel("")
        self._status.setObjectName("Faint")
        self._status.setStyleSheet("font-size: 11px;")
        self.add(self._status)

        # ---- 分组列表 ----
        self._group_rows = {}
        order = [cl.GROUP_TEMP, cl.GROUP_CACHE, cl.GROUP_GPU,
                 cl.GROUP_SYSTEM, cl.GROUP_DRIVER]
        groups = {}
        for r in self.rules:
            groups.setdefault(r.group, []).append(r)

        for gname in order:
            items = groups.get(gname)
            if not items:
                continue
            head = QFrame()
            hh = QHBoxLayout(head)
            hh.setContentsMargins(2, 6, 2, 2)
            gh = QLabel(gname)
            gh.setObjectName("GroupTitle")
            hh.addWidget(gh)
            hh.addStretch(1)
            gsum = QLabel("")
            gsum.setObjectName("Faint")
            gsum.setStyleSheet("font-size: 11px;")
            hh.addWidget(gsum)
            self.add(head)
            self._group_rows[gname] = (gh, gsum)

            for r in items:
                row = CleanRow(r)
                row.toggled.connect(self._on_row_toggled)
                self.rows[r.key] = row
                self.add(row)

        self.add_stretch()

    # ------------------------------------------------------------ 磁盘
    def _refresh_disk(self):
        d = cl.drive_space("C:")
        self._disk_title.setText(f"磁盘 {d['drive']}")
        self._disk_bar.set_percent(d["percent"])
        self._disk_info.setText(
            f"已用 {cl.human_bytes(d['used_bytes'])} / {cl.human_bytes(d['total_bytes'])}"
        )
        self._disk_free.setText(
            f"{cl.human_bytes(d['free_bytes'])} 可用　·　占用率 {d['percent']:.1f}%"
        )

    # ------------------------------------------------------------ 扫描
    def start_scan(self):
        if self._busy or self._scanner.running or self._worker.running:
            return
        self._busy = True
        self.btn_scan.setEnabled(False)
        self.btn_clean.setEnabled(False)
        self.btn_scan.setText("扫描中")
        self._bar.setRange(0, len(self.rules))
        self._bar.setValue(0)
        self._bar.show()
        for r in self.rules:
            r.size = 0
            r.scanned = False
            row = self.rows.get(r.key)
            if row:
                row.set_size("…", muted=True)
        self._status.setText("正在扫描（只统计体积，不会删除任何文件）")

        ok = self._scanner.start(
            self.rules,
            on_progress=lambda rule: self._scanned.emit(rule),
            on_done=lambda total, rules: self._scan_done.emit(total, rules),
        )
        if not ok:
            self._busy = False

    def _on_rule_scanned(self, rule):
        row = self.rows.get(rule.key)
        if row:
            if rule.size > 0:
                row.set_size(cl.human_bytes(rule.size))
            else:
                row.set_size("0 B", muted=True)
                row.set_dim(True)
                row.check.setEnabled(False)
        self._bar.setValue(self._bar.value() + 1)
        self._status.setText(f"正在扫描　{rule.name}")

    def _on_scan_done(self, total, rules):
        self._busy = False
        self._has_scan_result = True
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("重新扫描")
        self._bar.hide()
        found = sum(1 for r in rules if r.size > 0)
        self._status.setText(
            f"扫描完成：{found} / {len(rules)} 项可清理，共可释放约 {cl.human_bytes(total)}"
        )
        self._update_group_sums()
        self._update_selection()
        self.toast(f"扫描完成，发现约 {cl.human_bytes(total)} 可清理")

    def _update_group_sums(self):
        for gname, (_, gsum) in self._group_rows.items():
            total = sum(r.size for r in self.rules if r.group == gname)
            cnt = sum(1 for r in self.rules if r.group == gname and r.size > 0)
            gsum.setText(f"{cnt} 项可清理 · {cl.human_bytes(total)}")

    # ------------------------------------------------------------ 选择
    def _on_row_toggled(self, key, checked):
        self._update_selection()

    def _select_safe(self):
        """全选"安全"级别（level>=2）且有体积的项。"""
        for r in self.rules:
            row = self.rows.get(r.key)
            if row and r.size > 0 and r.level >= 2:
                row.set_checked(True)
        self._update_selection()

    def _select_none(self):
        for row in self.rows.values():
            row.set_checked(False)
        self._update_selection()

    def _selected_all_incl_risky(self):
        return [r for r in self.rules
                if self.rows.get(r.key) and self.rows[r.key].is_checked()]

    def _update_selection(self):
        sel = self._selected_all_incl_risky()
        total = sum(r.size for r in sel)
        self._sel_info.setText(f"已选 {len(sel)} 项 · {cl.human_bytes(total)}")
        self.btn_clean.setEnabled(
            bool(sel) and not self._busy and not self._worker.running
        )

    # ------------------------------------------------------------ 清理
    def _confirm_clean(self):
        sel = self._selected_all_incl_risky()
        if not sel:
            self.toast("请先勾选要清理的项目")
            return
        rows = [(r.name, r.size) for r in sel]
        total = sum(r.size for r in sel)
        # 提前告知可能重启资源管理器（任务栏会短暂消失）
        explorer_hint = any(cl.may_restart_explorer(r) for r in sel)
        # 提前告知会弹 UAC（有需要管理员权限的项、且当前不是管理员）
        admin_count = 0 if cl.is_admin() else sum(1 for r in sel if r.admin)
        dlg = ConfirmDialog(rows, total, parent=self.window(),
                            explorer_hint=explorer_hint, admin_count=admin_count)
        self._confirm_dlg = dlg
        dlg.confirmed.connect(lambda: self._run_clean(sel))
        dlg.show()
        parent = self.window()
        dlg.move(
            parent.x() + (parent.width() - dlg.width()) // 2,
            parent.y() + (parent.height() - dlg.height()) // 2,
        )

    def _run_clean(self, sel):
        if self._worker.running:
            return
        self._busy = True
        self._clean_total = sum(r.size for r in sel)
        self.btn_scan.setEnabled(False)
        self.btn_clean.setEnabled(False)
        self.btn_all.setEnabled(False)
        self.btn_none.setEnabled(False)
        self._bar.setRange(0, len(sel))
        self._bar.setValue(0)
        self._bar.show()
        self._status.setText("正在清理")
        self._results = []

        # 拆分选中项：
        #   direct   —— 普通权限就能清（非 admin 项；或用户本来就是管理员）
        #   elevated —— 只有管理员权限才清得动，交给提权子进程（弹一次 UAC）
        if cl.is_admin():
            direct, elevated = list(sel), []
        else:
            direct = [r for r in sel if not r.admin]
            elevated = [r for r in sel if r.admin]
        self._pending_elevated = elevated

        if not direct:
            # 全是需要提权的项，不必白跑一趟普通清理
            self._on_clean_done([])
            return
        self._worker.start(
            direct,
            on_rule_done=lambda res: self._clean_rule_done.emit(res),
            on_done=lambda results: self._clean_done.emit(results),
            on_status=lambda rule, text: self._clean_status.emit(rule.name, text),
        )

    # ------------------------------------------------------ 提权清理（UAC）
    def _start_elevated_clean(self, rules):
        """对需要管理员权限的规则走提权清理。

        普通权限的 worker 先把能清的清完，剩下这些交给
        `Yuhub.exe --clean-elevated` 提权子进程（弹一次 UAC、无窗口静默执行），
        结果通过临时 JSON 文件回传。
        """
        self._elevated_rules = list(rules)
        names = "、".join(r.name for r in rules[:3])
        if len(rules) > 3:
            names += f" 等 {len(rules)} 项"
        self._status.setText(f"需管理员权限：{names}　请在 UAC 弹窗点「是」")
        self._clean_status.emit("", "等待管理员授权…")
        keys = [r.key for r in rules]
        threading.Thread(target=self._do_elevated_clean, args=(keys,),
                         daemon=True).start()

    def _do_elevated_clean(self, keys):
        try:
            data = cl.run_elevated_clean(keys)
        except Exception:
            data = None
        self._clean_elevated_done.emit(data)

    def _on_elevated_done(self, data):
        """提权清理返回（主线程）。data 为 None 表示 UAC 被取消或超时。"""
        results = []
        for rule in getattr(self, "_elevated_rules", []):
            d = (data or {}).get(rule.key)
            if d is None:
                res = cl.CleanResult(key=rule.key, name=rule.name)
                res.messages.append(
                    "需要管理员权限，未清理（授权被取消或失败）"
                    if data is None else "未清理")
                results.append(res)
                continue
            res = cl.CleanResult(
                key=d.get("key", rule.key), name=d.get("name", rule.name),
                freed=d.get("freed", 0), estimated=d.get("estimated", 0),
                removed_bytes=d.get("removed_bytes", 0),
                removed_count=d.get("removed_count", 0),
                failed=d.get("failed", 0), verified=d.get("verified", True),
            )
            res.messages.extend(d.get("messages") or [])
            results.append(res)
        self._elevated_rules = []
        # 逐条刷 UI（进度条 / 行文案）——和普通清理走同一条更新路径
        for res in results:
            self._on_clean_rule_done(res)
        self._finish_clean()

    def _on_clean_status(self, name, text):
        if name:
            self._status.setText(f"清理中　{name}　·　{text}")
        else:
            self._status.setText(text)

    def _on_clean_rule_done(self, res):
        self._results.append(res)
        self._bar.setValue(self._bar.value() + 1)
        self._status.setText(f"已清理　{res.name}　释放 {cl.human_bytes(res.freed)}")
        row = self.rows.get(res.key)
        if row:
            if res.removed_count > 0:
                row.set_size(f"已清理 {cl.human_bytes(res.freed)}", muted=True)
            elif res.failed:
                row.set_size("部分被占用", muted=True)
            else:
                row.set_size("无可清理", muted=True)

    def _on_clean_done(self, results):
        """普通权限清理完成。若还有待提权的项，接着走提权流程。"""
        if getattr(self, "_pending_elevated", None):
            rules = self._pending_elevated
            self._pending_elevated = []
            self._start_elevated_clean(rules)
            return
        self._finish_clean()

    def _finish_clean(self):
        self._busy = False
        self._bar.hide()
        self.btn_scan.setEnabled(True)
        self.btn_clean.setEnabled(True)
        self.btn_all.setEnabled(True)
        self.btn_none.setEnabled(True)
        results = self._results
        total = sum(r.freed for r in results)
        count = sum(r.removed_count for r in results)
        failed = sum(r.failed for r in results)
        self._refresh_disk()
        # 如实汇报：数字来自"确认删除成功的对象体积"，不依赖磁盘读数（读数可能不敏感）
        msg = f"清理完成：已删除 {count} 个对象，共 {cl.human_bytes(total)}"
        if failed:
            msg += f"（{failed} 项被占用或权限不足）"
        self._status.setText(msg)
        self.toast(msg)
        # 清理后自动重扫，让用户看到剩余可清理量
        QTimer.singleShot(500, self.start_scan)

    # ------------------------------------------------------------ 生命周期
    def on_shown(self):
        """切到本页时调用：首次进入自动扫描一次，之后沿用已有结果。

        注意用 QTimer 延后，让页面先完成一次绘制，避免扫描线程启动时的
        首帧卡顿；同时 `_auto_scanned` 保证不会每次切入都重扫（重扫会打断
        用户已勾选的状态）。
        """
        if self._auto_scanned:
            return
        self._auto_scanned = True
        self._refresh_disk()
        QTimer.singleShot(180, self._auto_start_scan)

    def _auto_start_scan(self):
        if self._busy or self._scanner.running or self._worker.running:
            return
        self.start_scan()

    def shutdown(self):
        try:
            self._disk_timer.stop()
        except Exception:
            pass
        self._scanner.cancel()
        self._worker.cancel()
        if self._confirm_dlg is not None:
            try:
                self._confirm_dlg.close()
            except Exception:
                pass
