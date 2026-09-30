"""设置中心页：主题切换、启动与托盘开关、异地联机组件、关于。"""

import threading

from PySide6.QtCore import Qt, QSettings, Signal
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QComboBox)

import autostart
import etier

from .. import theme, VERSION_LABEL
from ..widgets import (
    ToggleSwitch, setting_row, info_card, SegmentedControl, ghost_button)
from .base_page import BasePage


def small_note(text="", warn=False):
    """卡片内的小号说明文字。"""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    _style_note(lbl, warn)
    return lbl


def _style_note(lbl, warn=False):
    p = theme.current()
    color = p["amber"] if warn else p["text_faint"]
    lbl.setStyleSheet(f"font-size: 11px; color: {color}; background: transparent;")


class SettingsPage(BasePage):
    TINT_KEY = "tile_1"
    # 页头徽标。基类默认是"功能开发中"，但本页的开关都是真能用的了，
    # 挂着"开发中"自相矛盾（与 C盘清理 / 多线程下载 保持一致口径）。
    BADGE = "已上线"

    # 引擎/修复回调发生在后台线程，必须经信号排队回主线程再碰控件
    _etier_event = Signal(str, object)

    def __init__(
        self,
        notify=None,
        theme_setting="dark",
        on_theme_change=None,
        on_pack_change=None,
        close_to_tray=True,
        on_close_to_tray_change=None,
        tray_available=True,
        parent=None,
    ):
        super().__init__(
            "设置中心",
            "应用偏好、主题、开机自启与关于 Yuhub。",
            icon="⚙️",
            notify=notify,
            parent=parent,
        )
        self._on_theme_change = on_theme_change or (lambda v: None)
        self._on_pack_change = on_pack_change or (lambda v: None)
        self._on_close_to_tray_change = on_close_to_tray_change or (lambda v: None)
        self._tray_available = tray_available
        # 挡住"写入失败→把开关拨回去"时二次触发的信号，避免递归
        self._autostart_guard = False
        # 自动检查更新的偏好（读不到时默认开启）
        self._settings = QSettings("Yuhub", "Yuhub")
        raw = self._settings.value("auto_check_update", None)
        self._auto_check = True if raw is None else str(raw).lower() in ("true", "1")
        self._build_content(theme_setting, close_to_tray)

    # ------------------------------------------------------------------ 构建
    def _build_content(self, theme_setting, close_to_tray):
        self._etier_event.connect(self._on_etier_event)
        self._build_appearance(theme_setting)
        self._build_startup_card(close_to_tray)
        self._build_etier_card()
        self._build_about()
        self.add_stretch()

    def _build_appearance(self, theme_setting):
        appearance = QFrame()
        appearance.setObjectName("Card")
        av = QVBoxLayout(appearance)
        av.setContentsMargins(18, 16, 18, 16)
        av.setSpacing(14)

        t = QLabel("外观")
        t.setObjectName("CardTitle")
        av.addWidget(t)

        # ---- ① 明暗模式 ----
        self.theme_segment = SegmentedControl(
            [("system", "跟随系统"), ("light", "浅色"), ("dark", "深色")],
            current=theme_setting,
        )
        self.theme_segment.changed.connect(self._on_theme_change)
        self.theme_segment.setMaximumWidth(380)
        av.addWidget(self.theme_segment, 0, Qt.AlignLeft)

        # ---- ② 主题包 ----
        pack_lbl = QLabel("主题")
        pack_lbl.setObjectName("GroupTitle")
        av.addWidget(pack_lbl)

        self.pack_combo = QComboBox()
        self.pack_combo.setMinimumWidth(260)
        self.pack_combo.setMaximumWidth(380)
        self.pack_combo.currentTextChanged.connect(self._on_pack_selected)
        av.addWidget(self.pack_combo, 0, Qt.AlignLeft)

        self.pack_desc = QLabel("")
        self.pack_desc.setObjectName("Muted")
        self.pack_desc.setStyleSheet("font-size: 11px;")
        self.pack_desc.setWordWrap(True)
        av.addWidget(self.pack_desc)

        self._reload_pack_combo()
        theme.bus.packs_changed.connect(self._reload_pack_combo)
        self.add(appearance)

    # ------------------------------------------------------ 主题包
    def _reload_pack_combo(self, *_args):
        """把主题列表同步进下拉框（不触发切换信号）。"""
        names = theme.pack_names()
        self.pack_combo.blockSignals(True)
        self.pack_combo.clear()
        for nm in names:
            pk = theme.packs().get(nm)
            self.pack_combo.addItem(pk.label if pk else nm, nm)
        cur = theme.current_pack_name()
        idx = self.pack_combo.findData(cur)
        if idx >= 0:
            self.pack_combo.setCurrentIndex(idx)
        self.pack_combo.blockSignals(False)
        self._update_pack_desc()

    def _update_pack_desc(self):
        nm = self.pack_combo.currentData()
        pk = theme.packs().get(nm) if nm else None
        if pk is None:
            self.pack_desc.setText("")
            return
        bits = []
        if pk.description:
            bits.append(pk.description)
        if pk.author:
            bits.append(f"作者：{pk.author}")
        if pk.version:
            bits.append(f"v{pk.version}")
        self.pack_desc.setText(" · ".join(bits) or "（无描述）")
        self._populate_note_style()

    def _on_pack_selected(self, _label):
        nm = self.pack_combo.currentData()
        if not nm or nm == theme.current_pack_name():
            self._update_pack_desc()
            return
        self._on_pack_change(nm)
        self._update_pack_desc()

    def set_pack_setting(self, name):
        """外部切换主题包时同步下拉框选中态。"""
        idx = self.pack_combo.findData(name)
        if idx >= 0:
            self.pack_combo.blockSignals(True)
            self.pack_combo.setCurrentIndex(idx)
            self.pack_combo.blockSignals(False)
        self._update_pack_desc()

    def _build_startup_card(self, close_to_tray):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        title = QLabel("启动与托盘")
        title.setObjectName("CardTitle")
        v.addWidget(title)

        # ---- 开机自动启动 ----
        self.sw_autostart = ToggleSwitch(autostart.is_enabled())
        self.sw_autostart.toggled.connect(self._on_autostart_toggled)
        if not autostart.is_supported():
            self.sw_autostart.setEnabled(False)
        v.addLayout(setting_row("开机自动启动", self.sw_autostart))

        self.autostart_hint = small_note()
        v.addWidget(self.autostart_hint)
        self._refresh_autostart_hint()

        v.addSpacing(4)

        # ---- 关闭窗口时留驻托盘 ----
        self.sw_tray = ToggleSwitch(bool(close_to_tray) and self._tray_available)
        self.sw_tray.toggled.connect(self._on_tray_toggled)
        if not self._tray_available:
            self.sw_tray.setEnabled(False)
            self.sw_tray.setToolTip("当前系统没有可用的通知区域，无法留驻托盘")
        v.addLayout(setting_row("关闭窗口时最小化到托盘", self.sw_tray))

        self.tray_hint = small_note(
            "关闭窗口后程序会留在系统托盘，单击托盘图标即可恢复；"
            "右键托盘图标可以选择「退出 Yuhub」。"
            if self._tray_available
            else "当前系统没有可用的通知区域，关闭窗口将直接退出程序。",
            warn=not self._tray_available,
        )
        v.addWidget(self.tray_hint)

        v.addSpacing(4)

        # ---- 自动检查更新（真开关）----
        self.sw_update = ToggleSwitch(self._auto_check)
        self.sw_update.toggled.connect(self._on_auto_update_toggled)
        v.addLayout(setting_row("自动检查更新", self.sw_update))
        v.addWidget(small_note(
            "启动后在后台静默检查新版本，发现更新时提示；"
            "检查失败不会打扰你，也不会自动安装。"))

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.btn_check_update = ghost_button("立即检查更新")
        self.btn_check_update.clicked.connect(self._on_check_update_clicked)
        row.addWidget(self.btn_check_update)
        v.addLayout(row)

        self.add(card)

        # 主题切换后小号说明文字的颜色要跟着换（内联色值不会自动更新）
        theme.bus.changed.connect(self._on_theme_changed_restyle)

    def _build_about(self):
        self.add(
            info_card(
                "关于 Yuhub",
                f"版本：{VERSION_LABEL}\n"
                "支持多套主题，主题存放在「文档 / Yuhub」目录下，"
                "每个主题一个文件夹，可自行修改配色。\n"
                "技术栈：Python + PySide6 (Qt)\n"
                "已上线：本机配置检测（含逐盘容量）、实时占用监控、C盘清理、"
                "多线程下载、软件卸载与残留清理、跨网联机、系统托盘驻留、"
                "单实例限制、开机自启。",
            )
        )

    # ------------------------------------------------------ 异地联机组件
    def _build_etier_card(self):
        """EasyTier 组件体检 + 一键修复。

        背景：有用户反馈下载后 easytier-core.exe 被杀毒软件直接删掉，
        联机页报"内置资源缺失"。重新下载 Yuhub 没用（释放出来又被删），
        所以提供单独重装组件的入口。
        """
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        title = QLabel("异地联机组件")
        title.setObjectName("CardTitle")
        v.addWidget(title)

        # 状态：后台检测，不在构造函数里起子进程拖慢页面
        self.etier_status = QLabel("组件检测中…")
        self.etier_status.setWordWrap(True)
        self.etier_status.setStyleSheet("font-size: 12px;")
        v.addWidget(self.etier_status)

        v.addWidget(small_note(
            "跨网房间依赖 EasyTier 虚拟网卡组件（v%s）。若组件丢失或被"
            "安全软件误删，点「一键修复」会从网络重新下载**同版本**组件"
            "并安装（约 32 MB），无需重装 Yuhub。" % etier.EASYTIER_VERSION))

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.btn_repair = ghost_button("一键修复")
        self.btn_repair.clicked.connect(self._on_repair_clicked)
        row.addWidget(self.btn_repair)
        v.addLayout(row)

        self.add(card)

        # 构建完成后异步做一次体检（tasklist/文件检查很快，但规矩是
        # 子进程一律不进 GUI 线程）
        threading.Thread(target=self._do_etier_check, daemon=True).start()

    def _do_etier_check(self):
        try:
            ok, detail = etier.core_health()
        except Exception as exc:
            ok, detail = False, repr(exc)
        self._etier_event.emit("check", {"ok": ok, "detail": detail})

    def _on_etier_event(self, kind, payload):
        """后台线程 → 主线程的事件落地（体检结果 / 修复进度与结果）。"""
        if kind == "check":
            ok = payload.get("ok")
            detail = str(payload.get("detail") or "")
            if ok:
                self.etier_status.setText("组件正常（%s）" % detail)
            else:
                self.etier_status.setText("组件异常：%s" % detail)
        elif kind == "progress":
            self.etier_status.setText(str(payload or ""))
        elif kind == "repair_done":
            ok = payload.get("ok")
            msg = str(payload.get("msg") or "")
            self.btn_repair.setEnabled(True)
            if ok:
                self.etier_status.setText("组件正常（%s）" % msg)
                self.toast("修复完成：%s" % msg)
            else:
                self.etier_status.setText("修复失败：%s" % msg)
                self.toast("修复失败：%s" % msg)

    def _on_repair_clicked(self):
        self.btn_repair.setEnabled(False)
        self.etier_status.setText("正在准备修复…")
        threading.Thread(
            target=self._do_repair, name="EtierRepair", daemon=True).start()

    def _do_repair(self):
        def progress(text):
            self._etier_event.emit("progress", text)

        try:
            ok, msg = etier.repair_binaries(progress=progress)
        except Exception as exc:
            ok, msg = False, repr(exc)
        self._etier_event.emit("repair_done", {"ok": ok, "msg": msg})

    # ------------------------------------------------------------------ 更新
    def _on_auto_update_toggled(self, checked):
        """把「自动检查更新」写进 QSettings。

        注意 Windows 上用原生格式写进去是字符串，读回来要按字符串判断，
        不能 `value(..., type=bool)`——`bool("false")` 是 True。
        """
        self._settings.setValue("auto_check_update", "true" if checked else "false")
        self._settings.sync()
        self._auto_check = bool(checked)

    def _on_check_update_clicked(self):
        """「立即检查更新」：交给主窗口（那里有 UpdateChecker 的信号接线）。"""
        win = self.window()
        if hasattr(win, "check_update_now"):
            self.btn_check_update.setEnabled(False)
            win.check_update_now(interactive=True)
            # 检查很快，给个短冷却避免连点
            from PySide6.QtCore import QTimer
            QTimer.singleShot(4000, lambda: self.btn_check_update.setEnabled(True))
        else:
            self.notify("当前环境不支持检查更新")

    # ------------------------------------------------------------------ 主题
    def _on_theme_changed_restyle(self, *_args):
        _style_note(self.autostart_hint, self._autostart_is_warn)
        _style_note(self.tray_hint, not self._tray_available)
        self._populate_note_style()

    def _populate_note_style(self):
        """主题包描述是内联色值，主题一换要重新染色。"""
        p = theme.current()
        try:
            self.pack_desc.setStyleSheet(
                f"font-size: 11px; color: {p['text_dim']}; background: transparent;")
        except AttributeError:
            pass

    def set_theme_setting(self, value):
        """外部（标题栏按钮）切换主题时同步选中态。"""
        self.theme_segment.set_current(value)

    # -------------------------------------------------------------- 开机自启
    def _autostart_state(self):
        """返回 (说明文字, 是否属于需要注意的情况)。"""
        if not autostart.is_supported():
            return "当前系统不支持自动启动（本功能仅覆盖 Windows）。", True
        current = autostart.registered_command()
        if not current:
            return (
                "未开启。开启后将在登录 Windows 时自动启动，"
                "并静默驻留系统托盘、不弹出主窗口。",
                False,
            )
        if autostart.same_program(current, autostart.launch_command()):
            return f"已开启，注册的命令：{current}", False
        # 程序被移动过 → 注册表还指着老路径，开机时会静默启动失败
        return (
            f"已开启，但注册的是另一个位置（{autostart.program_of(current)}）。"
            "当前程序已被移动或改名，建议关闭再重新开启以更新路径。",
            True,
        )

    def _refresh_autostart_hint(self):
        text, warn = self._autostart_state()
        self._autostart_is_warn = warn
        self.autostart_hint.setText(text)
        _style_note(self.autostart_hint, warn)

    def _on_autostart_toggled(self, checked):
        if self._autostart_guard:
            return
        ok, info = autostart.set_enabled(checked)
        if not ok:
            # 关键：写不进去就把开关拨回去。否则界面显示"已开启"、
            # 实际注册表里什么都没有，用户下次开机发现根本没启动，
            # 却完全不知道是这里失败了。
            self._autostart_guard = True
            self.sw_autostart.setChecked(not checked)
            self._autostart_guard = False
            self.autostart_hint.setText(f"设置失败：{info}")
            self._autostart_is_warn = True
            _style_note(self.autostart_hint, True)
            self.toast("开机自启设置失败")
            return
        self._refresh_autostart_hint()
        self.toast("已开启开机自动启动" if checked else "已关闭开机自动启动")

    # ------------------------------------------------------------------ 托盘
    def _on_tray_toggled(self, flag):
        self._on_close_to_tray_change(flag)
        self.toast(
            "关闭窗口时将留在系统托盘" if flag else "关闭窗口时将直接退出程序"
        )
