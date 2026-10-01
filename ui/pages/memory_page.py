# -*- coding: utf-8 -*-
"""内存优化页：一键回收 + 定时回收 + 过载回收。

和 PCL 百宝箱里的「内存优化」是同一类功能（把闲置的物理内存还给系统），
但这里额外钉死四条 PCL 没管的边界：

1. **绝不结束任何进程** —— 只做工作集回收 + 系统级缓存清理，跳过关键
   系统进程和当前最前面的程序（详见 memopt 模块头）。
2. **定时间隔有下限（15 分钟）** —— 更频繁地回收只会让常用程序反复
   重新调页，收益远小于代价。
3. **两类自动触发（定时 / 过载）一律不提权** —— 后台静默弹 UAC 既烦人
   又容易被误拒；系统级清理只挂在"管理员深度优化"这个手动开关上。
4. **自动触发有冷却** —— 过载触发后 10 分钟内不再重复，否则机器长期
   跑在阈值之上时会变成"每几秒回收一次"，那是纯伤害。
"""

import json
import threading
import time

from PySide6.QtCore import Qt, Signal, QTimer, QSettings
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from .. import theme
from ..widgets import ToggleSwitch, primary_button, usage_color
from .base_page import BasePage
import memopt


# 预设挡位（分钟）。**最小 15 分钟**是硬下限：更频繁地回收只会让常用
# 程序反复重新调页，收益远小于代价。
INTERVALS = [
    (15, "15 分"),
    (30, "30 分"),
    (60, "1 小时"),
    (120, "2 小时"),
    (240, "4 小时"),
    (480, "8 小时"),
]
CUSTOM_LABEL = "自定义"
MIN_MINUTES = 15
MAX_MINUTES = 1440            # 24 小时

# 过载触发的可选阈值（百分比）
THRESHOLD_OPTIONS = (70, 75, 80, 85, 90)
DEFAULT_THRESHOLD = 80
THRESHOLD_CHECK_SEC = 5       # 每 5 秒看一次占用率
THRESHOLD_COOLDOWN = 600      # 触发后 10 分钟内不再重复触发
THRESHOLD_GRACE = 60          # 刚打开开关先等 1 分钟，避免立刻触发

# 触发来源（写进优化记录，便于事后分辨是谁干的）
TRIGGER_MANUAL = "手动"
TRIGGER_TIMER = "定时"
TRIGGER_OVERLOAD = "过载"

# 挡位按钮的紧凑内边距。主题默认是 6px 14px，而这里一行要塞 7 个挡位
# 外加一个数字框，在 1000px 以内的窗口就会撑出横向滚动条。
# 用**控件级**样式而不是写进 theme.py：主题包（含用户自制皮肤）自带
# theme.qss 时会整份覆盖内置模板，只有控件级样式才能在所有皮肤下生效。
# 这里只压内边距和字号，背景/选中态仍由主题负责，换肤不会跑偏。
_COMPACT_QSS = "QPushButton { padding: 6px 8px; font-size: 12px; }"

HISTORY_MAX = 6


def _clamp_minutes(value):
    try:
        m = int(value)
    except (TypeError, ValueError):
        m = MIN_MINUTES
    return max(MIN_MINUTES, min(MAX_MINUTES, m))


def _is_preset(minutes):
    return any(v == minutes for v, _ in INTERVALS)


def _fmt_left(sec):
    """把剩余秒数写成 '28 分 12 秒'。"""
    sec = int(max(0, sec))
    if sec >= 3600:
        return "%d 小时 %d 分" % (sec // 3600, (sec % 3600) // 60)
    if sec >= 60:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 秒" % sec


def _fmt_clock(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except (ValueError, OSError):
        return "--:--"


def _read_bool(settings, key, default):
    """按字符串判断布尔值（Windows 上 QSettings 会存成 'true'/'false'，
    直接 value(..., type=bool) 会把 'false' 读成 True）。"""
    raw = settings.value(key, None)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class MemoryBar(QFrame):
    """物理内存占用条（自绘，按占用率变色）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(10)
        self.setMinimumWidth(80)
        self._percent = 0.0
        # 连绑定方法（不是 lambda）：控件销毁后主题信号不会再打进来
        theme.bus.changed.connect(self._restyle)

    def _restyle(self, *args):
        try:
            self.update()
        except RuntimeError:
            pass

    def set_percent(self, percent):
        self._percent = max(0.0, min(100.0, float(percent or 0.0)))
        self.update()

    def paintEvent(self, event):                          # noqa: N802
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # 底槽要跟卡片背景区分开，否则占用率低时整条"看不见"
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p.get("border_strong", p["border"])))
        painter.drawRoundedRect(0, 0, w, h, 4, 4)
        filled = int(w * self._percent / 100.0)
        if filled > 0:
            painter.setBrush(QColor(usage_color(self._percent)))
            painter.drawRoundedRect(0, 0, max(filled, 4), h, 4, 4)
        painter.end()


class IntervalPicker(QFrame):
    """6 个预设挡位 + 1 个「自定义」挡位。

    **只有选中「自定义」时右侧分钟输入框才可编辑**；选预设挡位时输入框
    显示该挡位的值但呈灰态 —— "用预设"和"自己填"变成两个明确动作，
    不会出现"改了数字框却发现被预设覆盖回去"的困惑。
    """

    changed = Signal(int)

    def __init__(self, minutes=30, parent=None):
        super().__init__(parent)
        self._buttons = []
        self._guard = False
        self._custom = False

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        seg = QFrame()
        seg.setObjectName("Segment")
        seg_lay = QHBoxLayout(seg)
        seg_lay.setContentsMargins(3, 3, 3, 3)
        seg_lay.setSpacing(2)
        for value, label in INTERVALS:
            b = QPushButton(label)
            b.setObjectName("SegmentButton")
            b.setStyleSheet(_COMPACT_QSS)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(
                lambda _=False, v=value: self.set_minutes(v, custom=False))
            seg_lay.addWidget(b)
            self._buttons.append((value, b))

        self.btn_custom = QPushButton(CUSTOM_LABEL)
        self.btn_custom.setObjectName("SegmentButton")
        self.btn_custom.setStyleSheet(_COMPACT_QSS)
        self.btn_custom.setCheckable(True)
        self.btn_custom.setCursor(Qt.PointingHandCursor)
        self.btn_custom.setToolTip(
            "自己指定间隔（%d ~ %d 分钟）" % (MIN_MINUTES, MAX_MINUTES))
        self.btn_custom.clicked.connect(self._pick_custom)
        seg_lay.addWidget(self.btn_custom)
        row.addWidget(seg, 1)

        self.spin = QSpinBox()
        self.spin.setObjectName("IntervalSpin")
        self.spin.setRange(MIN_MINUTES, MAX_MINUTES)
        self.spin.setSuffix(" 分钟")
        self.spin.setFixedWidth(104)
        self.spin.setAlignment(Qt.AlignCenter)
        # 极简色块风格不要那对系统小箭头。QSS 里也压了一道 width:0，
        # 但 Qt 对 QSpinBox 子控件的样式匹配不如代码可靠，这里直接关掉。
        self.spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        # 输入过程中不要每次按键都回写：下限 15 会把中途的 "4" 立刻顶成 15，
        # 再接一个字就变成 "155" ——看着就像"输入不进去"。改成失焦/回车
        # 时一次性生效。
        self.spin.setKeyboardTracking(False)
        self.spin.setToolTip(
            "仅在选中「%s」时可编辑。范围 %d ~ %d 分钟；小于 %d 分钟会让"
            "常用程序反复重新调页，反而拖慢系统。"
            % (CUSTOM_LABEL, MIN_MINUTES, MAX_MINUTES, MIN_MINUTES))
        self.spin.valueChanged.connect(self._on_spin_changed)
        row.addWidget(self.spin)

        m = _clamp_minutes(minutes)
        self._apply(m, custom=not _is_preset(m), emit=False)

    # ------------------------------------------------------------ 内部
    def _apply(self, minutes, custom, emit=True):
        m = _clamp_minutes(minutes)
        self._guard = True
        try:
            self._custom = bool(custom)
            if self.spin.value() != m:
                self.spin.setValue(m)
            for value, btn in self._buttons:
                btn.setChecked((not self._custom) and value == m)
            self.btn_custom.setChecked(self._custom)
            self.spin.setEnabled(self._custom)
        finally:
            self._guard = False
        if emit:
            self.changed.emit(m)

    def _pick_custom(self):
        # 点「自定义」= 进入可编辑态，并把光标直接放进输入框
        self._apply(self.spin.value(), custom=True)
        self.spin.setFocus()
        self.spin.selectAll()

    def _on_spin_changed(self, value):
        if self._guard:
            return
        self._apply(value, custom=True)

    # ------------------------------------------------------------ 对外
    def minutes(self):
        return int(self.spin.value())

    def is_custom(self):
        return bool(self._custom)

    def set_minutes(self, minutes, custom=None):
        """custom=None 时自动判定：命中预设就用预设，否则进自定义。"""
        m = _clamp_minutes(minutes)
        if custom is None:
            custom = not _is_preset(m)
        self._apply(m, custom=custom)


class ThresholdPicker(QFrame):
    """内存过载阈值（占用率百分比）选择器。"""

    changed = Signal(int)

    def __init__(self, percent=DEFAULT_THRESHOLD, parent=None):
        super().__init__(parent)
        self._buttons = []
        self._guard = False

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        seg = QFrame()
        seg.setObjectName("Segment")
        seg_lay = QHBoxLayout(seg)
        seg_lay.setContentsMargins(3, 3, 3, 3)
        seg_lay.setSpacing(2)
        for value in THRESHOLD_OPTIONS:
            b = QPushButton("%d%%" % value)
            b.setObjectName("SegmentButton")
            b.setStyleSheet(_COMPACT_QSS)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(
                lambda _=False, v=value: self.set_percent(v))
            seg_lay.addWidget(b)
            self._buttons.append((value, b))
        row.addWidget(seg)
        row.addStretch(1)

        self.set_percent(percent if percent in THRESHOLD_OPTIONS
                         else DEFAULT_THRESHOLD, emit=False)

    def percent(self):
        return int(self._percent)

    def set_percent(self, percent, emit=True):
        try:
            p = int(percent)
        except (TypeError, ValueError):
            p = DEFAULT_THRESHOLD
        if p not in THRESHOLD_OPTIONS:
            # 存量配置落在区间内但不是预设值 → 取最接近的那个
            p = min(THRESHOLD_OPTIONS, key=lambda v: abs(v - p))
        self._percent = p
        self._guard = True
        try:
            for value, btn in self._buttons:
                btn.setChecked(value == p)
        finally:
            self._guard = False
        if emit:
            self.changed.emit(p)


class MemoryPage(BasePage):
    TINT_KEY = "tile_7"
    BADGE = "新功能"

    _prog = Signal(str)
    _done = Signal(object)

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "内存优化",
            "回收闲置的物理内存，让系统更轻快 · 只回收，不结束任何程序",
            icon="🧠",
            notify=notify,
            parent=parent,
        )
        self._settings = QSettings("Yuhub", "Yuhub")
        self._busy = False
        self._trigger = TRIGGER_MANUAL
        self._last_result = None
        self._last_percent = 0.0
        self._next_at = 0.0
        # 定时
        self._auto_on = _read_bool(self._settings, "mem_auto_enabled", False)
        # 过载
        self._thr_on = _read_bool(self._settings, "mem_thr_enabled", False)
        self._threshold = self._read_int("mem_thr_percent", DEFAULT_THRESHOLD)
        self._thr_next_ok = 0.0
        self._thr_grace_until = 0.0
        # 管理员深度优化（只作用于手动优化）
        self._admin_deep = _read_bool(self._settings, "mem_admin_deep", False)
        saved_min = self._read_int("mem_auto_minutes", 30)

        self._build_content(saved_min)

        self._prog.connect(self._on_progress)
        self._done.connect(self._on_done)

        # 1 秒心跳：刷新内存条 / 倒计时 / 阈值状态，到点触发自动优化。
        # 页面被切走后依然在跑（自动优化本来就该在后台生效）。
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()
        self._ui_tick = 0

        self._apply_auto_state(reset_next=True)
        self._apply_thr_state(grace=True)
        self.refresh_memory()
        # 倒计时 / 状态文字带主题色，换主题时要重算一次
        theme.bus.changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, *args):
        try:
            self._apply_auto_state(reset_next=False)
            self._refresh_thr_note()
        except RuntimeError:                              # 控件已销毁
            pass

    # ------------------------------------------------------------ 构建
    def _read_int(self, key, default):
        try:
            return int(self._settings.value(key, default))
        except (TypeError, ValueError):
            return default

    def _card(self, title, right=None):
        """一张卡片。right 非空时放在标题行右侧（开关之类）。"""
        card = QFrame()
        card.setObjectName("Card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        if title:
            t = QLabel(title)
            t.setObjectName("CardTitle")
            if right is None:
                lay.addWidget(t)
            else:
                row = QHBoxLayout()
                row.setSpacing(10)
                row.addWidget(t)
                row.addStretch(1)
                row.addWidget(right)
                lay.addLayout(row)
        return card, lay

    def _dim(self, text, size=13, wrap=True):
        lbl = QLabel(text)
        lbl.setWordWrap(wrap)
        lbl.setStyleSheet("font-size: %dpx; background: transparent;" % size)
        return lbl

    def _faint(self, text, size=11):
        lbl = QLabel(text)
        lbl.setObjectName("Faint")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("font-size: %dpx; background: transparent;" % size)
        return lbl

    def _build_content(self, saved_min):
        # ---------------- 物理内存 ----------------
        card, lay = self._card("物理内存")
        head = QHBoxLayout()
        head.setSpacing(8)
        self._mem_used = self._dim("正在读取…", wrap=False)
        head.addWidget(self._mem_used)
        head.addStretch(1)
        self._mem_pct = QLabel("--%")
        self._mem_pct.setStyleSheet(
            "font-size: 20px; font-weight: 800; background: transparent;")
        head.addWidget(self._mem_pct)
        lay.addLayout(head)

        self.bar = MemoryBar()
        lay.addWidget(self.bar)

        self._mem_detail = self._dim("", 12, wrap=False)
        self._mem_detail.setObjectName("Muted")
        lay.addWidget(self._mem_detail)
        self.add(card)

        # ---------------- 一键优化 ----------------
        card2, lay2 = self._card("一键优化")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.btn_optimize = primary_button("立即优化")
        self.btn_optimize.setMinimumHeight(36)
        self.btn_optimize.setMinimumWidth(120)
        self.btn_optimize.clicked.connect(self._on_optimize_clicked)
        row.addWidget(self.btn_optimize)
        row.addStretch(1)

        # 管理员深度优化 = 一个开关（不再是一个独立按钮）
        self.sw_admin = ToggleSwitch(self._admin_deep)
        self.sw_admin.toggled.connect(self._on_admin_toggled)
        lbl_admin = self._dim("管理员深度优化")
        lbl_admin.setToolTip(
            "开启后，手动点「立即优化」会请求一次管理员授权，额外清空系统"
            "备用列表并收缩文件缓存。定时 / 过载触发不会提权。")
        row.addWidget(lbl_admin)
        row.addWidget(self.sw_admin)
        lay2.addLayout(row)

        self._admin_hint = self._faint("")
        lay2.addWidget(self._admin_hint)

        self._status = self._dim(
            "尚未优化过。点击「立即优化」会回收各程序闲置的物理内存。", 12)
        lay2.addWidget(self._status)

        lay2.addWidget(self._faint(
            "· 不会结束任何程序：只把闲置的内存页交还系统，程序照常运行。\n"
            "· 当前最前面的窗口（你正在用的软件）会被跳过，避免操作时卡顿。\n"
            "· 定时与过载触发一律使用普通权限，不会在后台弹授权窗口。"))
        self.add(card2)

        # ---------------- 定时自动优化 ----------------
        self.sw_auto = ToggleSwitch(self._auto_on)
        self.sw_auto.toggled.connect(self._on_auto_toggled)
        card3, lay3 = self._card("定时自动优化", right=self.sw_auto)

        r2 = QHBoxLayout()
        r2.setSpacing(10)
        lbl2 = self._dim("间隔")
        lbl2.setMinimumWidth(52)
        r2.addWidget(lbl2)
        self.picker = IntervalPicker(saved_min)
        self.picker.changed.connect(self._on_interval_changed)
        r2.addWidget(self.picker, 1)
        lay3.addLayout(r2)

        self._next_label = QLabel("")
        self._next_label.setStyleSheet(
            "font-size: 12px; font-weight: 700; background: transparent;")
        lay3.addWidget(self._next_label)

        lay3.addWidget(self._faint(
            "每隔设定时间执行一次，与内存占用多少无关。间隔下限 %d 分钟："
            "更频繁地回收只会让程序反复重新调页，收益远小于代价。"
            % MIN_MINUTES))
        self.add(card3)

        # ---------------- 内存过载自动优化 ----------------
        self.sw_thr = ToggleSwitch(self._thr_on)
        self.sw_thr.toggled.connect(self._on_thr_toggled)
        card4, lay4 = self._card("内存过载自动优化", right=self.sw_thr)

        r4 = QHBoxLayout()
        r4.setSpacing(10)
        lbl4 = self._dim("占用达到")
        lbl4.setMinimumWidth(52)
        r4.addWidget(lbl4)
        self.thr_picker = ThresholdPicker(self._threshold)
        self.thr_picker.changed.connect(self._on_threshold_changed)
        r4.addWidget(self.thr_picker, 1)
        r4.addWidget(self._dim("时执行"))
        lay4.addLayout(r4)

        self._thr_note = QLabel("")
        self._thr_note.setStyleSheet(
            "font-size: 12px; font-weight: 700; background: transparent;")
        lay4.addWidget(self._thr_note)

        lay4.addWidget(self._faint(
            "与定时是两套独立规则，可同时开启。触发后 %d 分钟内不会重复"
            "执行；刚打开开关会先等 %d 秒再开始判断。"
            % (THRESHOLD_COOLDOWN // 60, THRESHOLD_GRACE)))
        self.add(card4)

        # ---------------- 优化记录 ----------------
        card5, lay5 = self._card("优化记录")
        self._history_box = QVBoxLayout()
        self._history_box.setSpacing(6)
        lay5.addLayout(self._history_box)
        self.add(card5)

        self.add_stretch()
        self._render_history()

    # ------------------------------------------------------------ 内存显示
    def refresh_memory(self):
        st = memopt.memory_status()
        self._last_percent = float(st["percent"])
        self.bar.set_percent(st["percent"])
        self._mem_pct.setText("%.0f%%" % st["percent"])
        self._mem_pct.setStyleSheet(
            "font-size: 20px; font-weight: 800; background: transparent; "
            "color: %s;" % usage_color(st["percent"]))
        self._mem_used.setText(
            "已用 %s / %s" % (memopt.human_bytes(st["used"]),
                             memopt.human_bytes(st["total"])))
        self._mem_detail.setText(
            "可用 %s · 占用率 %.1f%%" % (memopt.human_bytes(st["avail"]),
                                      st["percent"]))
        self._sync_admin_switch()

    # ------------------------------------------------------------ 管理员开关
    def _sync_admin_switch(self):
        """按当前权限 / 运行形态决定开关是否可用，并更新说明文字。"""
        admin = memopt.is_admin()
        packed = bool(memopt.exe_path())
        if admin:
            self.sw_admin.blockSignals(True)
            self.sw_admin.setChecked(False)
            self.sw_admin.blockSignals(False)
            self.sw_admin.setEnabled(False)
            self._admin_hint.setText(
                "当前已是管理员权限：每次优化都会自动包含系统级缓存清理，"
                "这个开关无需开启。")
        elif not packed:
            self.sw_admin.setEnabled(False)
            self._admin_hint.setText(
                "源码运行时无法请求管理员授权；打包成 exe 后这个开关才可用。")
        else:
            self.sw_admin.setEnabled(True)
            if self._admin_deep:
                self._admin_hint.setText(
                    "已开启：点「立即优化」会请求一次授权，额外清空系统备用"
                    "列表并收缩文件缓存。定时 / 过载触发仍然只走普通权限。")
            else:
                self._admin_hint.setText(
                    "关闭中：只做免管理员的逐进程回收。开启后手动优化会额外"
                    "清理系统级缓存（需授权）。")

    def _on_admin_toggled(self, checked):
        self._admin_deep = bool(checked)
        self._settings.setValue(
            "mem_admin_deep", "true" if checked else "false")
        self._sync_admin_switch()

    # ------------------------------------------------------------ 优化流程
    def _want_elevated(self):
        """这次手动优化是否需要提权：开关开着、且还不是管理员、且能提权。"""
        return (self._admin_deep and memopt.exe_path()
                and not memopt.is_admin())

    def _on_optimize_clicked(self):
        self._run_optimize(self._want_elevated(), trigger=TRIGGER_MANUAL)

    def _run_optimize(self, elevated=False, trigger=TRIGGER_MANUAL):
        if self._busy:
            self.toast("正在优化中，请稍候…")
            return
        if elevated and not memopt.exe_path():
            self.toast("源码运行时不支持提权优化，请用打包后的 exe")
            return
        self._busy = True
        self._trigger = trigger
        self.btn_optimize.setEnabled(False)
        self.btn_optimize.setText("正在优化…")
        self.sw_admin.setEnabled(False)
        self._status.setText(
            "正在请求管理员授权…" if elevated else "正在枚举进程…")
        threading.Thread(target=self._work, args=(elevated,),
                         name="MemOpt", daemon=True).start()

    def _work(self, elevated):
        try:
            if elevated:
                res = memopt.run_elevated_optimize(skip_foreground=True)
                if res is None:
                    res = {"ok": False, "error": "已取消管理员授权",
                           "cancelled": True}
            else:
                res = memopt.optimize(
                    progress=lambda text: self._prog.emit(str(text)))
        except Exception as exc:                          # noqa: BLE001
            res = {"ok": False, "error": "优化异常：%s" % (exc,)}
        self._done.emit(res)

    def _on_progress(self, text):
        self._status.setText(text)

    def _on_done(self, res):
        self._busy = False
        self.btn_optimize.setEnabled(True)
        self.btn_optimize.setText("立即优化")
        self._sync_admin_switch()
        self.refresh_memory()
        self._last_result = res

        if not res or not res.get("ok"):
            if (res or {}).get("cancelled"):
                self._status.setText("已取消管理员授权，未做任何改动。")
            else:
                self._status.setText(
                    "优化未完成：%s" % (res or {}).get("error", "未知原因"))
            return

        prefix = "" if self._trigger == TRIGGER_MANUAL else "（%s触发）" % self._trigger
        self._status.setText(
            "%s%s · 用时 %.2fs%s"
            % (prefix, memopt.describe(res), res.get("seconds", 0.0),
               "" if res.get("elevated") else "（系统级缓存需管理员权限）"))
        self._push_history(res, self._trigger)
        self._render_history()

    # ------------------------------------------------------------ 记录
    def _load_history(self):
        raw = self._settings.value("mem_history", "")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)][:HISTORY_MAX]

    def _push_history(self, res, trigger=TRIGGER_MANUAL):
        items = self._load_history()
        items.insert(0, {
            "ts": res.get("ts", time.time()),
            "trigger": str(trigger or TRIGGER_MANUAL),
            "freed_ws": int(res.get("freed_ws", 0)),
            "freed_avail": int(res.get("freed_avail", 0)),
            "emptied": int(res.get("emptied", 0)),
            "seconds": float(res.get("seconds", 0.0)),
            "elevated": bool(res.get("elevated")),
            "skipped": False,
            "percent": float((res.get("before") or {}).get("percent", 0.0)),
        })
        del items[HISTORY_MAX:]
        self._settings.setValue(
            "mem_history", json.dumps(items, ensure_ascii=False))

    def _render_history(self):
        while self._history_box.count():
            item = self._history_box.takeAt(0)
            w = item.widget()
            if w is not None:
                # 只 takeAt 是不够的：widget 仍在父控件里、仍占着原来的
                # 位置显示，要等 DeferredDelete 被事件循环处理才会消失。
                # 列表刷新得快时就会看到新旧两行叠在一起。
                # 先 hide 再摘掉父级，立刻从界面上消失。
                w.hide()
                w.setParent(None)
                w.deleteLater()
        items = self._load_history()
        if not items:
            lbl = self._faint("还没有记录。", 12)
            self._history_box.addWidget(lbl)
            return
        cur = theme.current()
        for it in items:
            bits = [str(it.get("trigger") or TRIGGER_MANUAL)]
            if it.get("skipped"):
                bits.append("内存充足（%.0f%%）已跳过" % it.get("percent", 0.0))
            else:
                bits.append("回收 %s" % memopt.human_bytes(it.get("freed_ws", 0)))
                if it.get("freed_avail", 0) > 0:
                    bits.append("可用 +%s"
                                % memopt.human_bytes(it.get("freed_avail", 0)))
                bits.append("%d 个程序" % it.get("emptied", 0))
                bits.append("%.1fs" % it.get("seconds", 0.0))
                if it.get("elevated"):
                    bits.append("深度")
            text = "%s　%s" % (_fmt_clock(it.get("ts", 0)), " · ".join(bits))
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(
                "font-size: 12px; background: transparent; color: %s;"
                % cur["text_dim"])
            self._history_box.addWidget(lbl)

    # ------------------------------------------------------------ 定时
    def _on_auto_toggled(self, checked):
        self._auto_on = bool(checked)
        self._settings.setValue(
            "mem_auto_enabled", "true" if checked else "false")
        self._apply_auto_state(reset_next=True)
        self.toast("已开启定时自动优化" if checked else "已关闭定时自动优化")

    def _on_interval_changed(self, minutes):
        self._settings.setValue("mem_auto_minutes", str(int(minutes)))
        if self._auto_on:
            self._apply_auto_state(reset_next=True)

    def _apply_auto_state(self, reset_next=False):
        enabled = self._auto_on
        if enabled:
            if reset_next or not self._next_at:
                self._next_at = time.monotonic() + self.picker.minutes() * 60
            self._next_label.setText(
                "距下次执行还有 %s" % _fmt_left(self._next_at - time.monotonic()))
            self._next_label.setStyleSheet(
                "font-size: 12px; font-weight: 700; background: transparent; "
                "color: %s;" % theme.current()["accent"])
        else:
            self._next_at = 0.0
            self._next_label.setText("未开启定时优化")
            self._next_label.setStyleSheet(
                "font-size: 12px; background: transparent; color: %s;"
                % theme.current().get("text_faint", "#8b93a3"))

    def _timer_fire(self):
        # 先排下一次：优化本身可能耗时，不能让它影响节拍
        self._next_at = time.monotonic() + self.picker.minutes() * 60
        if self._busy:
            return
        self._run_optimize(False, trigger=TRIGGER_TIMER)

    # ------------------------------------------------------------ 过载
    def _on_thr_toggled(self, checked):
        self._thr_on = bool(checked)
        self._settings.setValue(
            "mem_thr_enabled", "true" if checked else "false")
        self._apply_thr_state(grace=True)
        self.toast("已开启内存过载自动优化" if checked else "已关闭内存过载自动优化")

    def _on_threshold_changed(self, percent):
        self._threshold = int(percent)
        self._settings.setValue("mem_thr_percent", str(int(percent)))
        self._refresh_thr_note()

    def _apply_thr_state(self, grace=False):
        if not self._thr_on:
            self._thr_next_ok = 0.0
            self._thr_grace_until = 0.0
            self._refresh_thr_note()
            return
        if grace:
            # 刚打开先等一会儿再判阈值，否则"内存本来就高"的机器一开
            # 开关就立刻回收一次，用户会以为它在乱动手。
            self._thr_grace_until = time.monotonic() + THRESHOLD_GRACE
            self._thr_next_ok = 0.0
        self._refresh_thr_note()

    def _refresh_thr_note(self, percent=None):
        if percent is None:
            percent = self._last_percent
        if not self._thr_on:
            self._thr_note.setText("未开启，不会自动执行")
            self._thr_note.setStyleSheet(
                "font-size: 12px; background: transparent; color: %s;"
                % theme.current().get("text_faint", "#8b93a3"))
            return
        now = time.monotonic()
        hot = False
        if now < self._thr_grace_until:
            text = "刚开启 · %s后开始判断" % _fmt_left(
                self._thr_grace_until - now)
        elif now < self._thr_next_ok:
            text = "冷却中 · 还有 %s" % _fmt_left(self._thr_next_ok - now)
        elif percent >= self._threshold:
            text = "当前 %.0f%% · 已达阈值，将执行优化" % percent
            hot = True
        else:
            text = "当前 %.0f%% · 未达阈值" % percent
        self._thr_note.setText(text)
        self._thr_note.setStyleSheet(
            "font-size: 12px; font-weight: 700; background: transparent; "
            "color: %s;" % (theme.current()["accent"] if hot
                            else theme.current().get("text_faint", "#8b93a3")))

    def _check_threshold(self):
        """按当前占用率判断是否该触发一次过载优化。"""
        st = memopt.memory_status()
        self._last_percent = float(st["percent"])
        now = time.monotonic()
        self._refresh_thr_note(st["percent"])
        if (self._busy or now < self._thr_grace_until
                or now < self._thr_next_ok):
            return
        if st["percent"] < self._threshold:
            return
        self._thr_grace_until = 0.0
        self._thr_next_ok = now + THRESHOLD_COOLDOWN
        self._run_optimize(False, trigger=TRIGGER_OVERLOAD)

    # ------------------------------------------------------------ 心跳
    def _on_tick(self):
        # 内存条每 2 秒刷一次就够（也避免频繁读系统计数器）
        self._ui_tick += 1
        if self._ui_tick % 2 == 0 and self.isVisible():
            self.refresh_memory()
        if self._auto_on:
            left = self._next_at - time.monotonic()
            if left <= 0:
                self._timer_fire()
            else:
                self._next_label.setText("距下次执行还有 %s" % _fmt_left(left))
        if self._thr_on:
            if self._ui_tick % THRESHOLD_CHECK_SEC == 0:
                self._check_threshold()
            else:
                self._refresh_thr_note()

    # ------------------------------------------------------------ 生命周期
    def on_shown(self):
        self.refresh_memory()
        if self._auto_on:
            self._apply_auto_state(reset_next=False)
        self._refresh_thr_note()

    def shutdown(self):
        try:
            self._tick_timer.stop()
        except Exception:                                 # noqa: BLE001
            pass
