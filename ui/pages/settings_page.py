"""设置中心页：主题切换、启动与托盘开关、异地联机组件、关于。"""

import threading

from PySide6.QtCore import Qt, QSettings, Signal
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QMessageBox)

import autostart
import etier
import sac
import updater
import winadmin
from .. import theme, VERSION, VERSION_LABEL
from ..widgets import (
    ToggleSwitch, setting_row, info_card, SegmentedControl, ghost_button)
from .base_page import BasePage


def small_note(text="", warn=False):
    """卡片内的小号说明文字。"""
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    # 登记身份：颜色是内联写死的（不走 QSS），主题切换时要靠这两个属性
    # 把它们认出来重刷（见 SettingsPage._on_theme_changed_restyle）。
    lbl.setProperty("smallNote", True)
    lbl.setProperty("noteWarn", bool(warn))
    _style_note(lbl, warn)
    return lbl


def _style_note(lbl, warn=False):
    p = theme.current()
    color = p["amber"] if warn else p["text_faint"]
    lbl.setStyleSheet(f"font-size: 11px; color: {color}; background: transparent;")


def _messagebox_qss():
    """给 QMessageBox 算一份**按当前色板**的样式表（弹窗主题的唯一出口）。

    为什么不直接甩 `theme.build_qss()`：那份里有
    `QDialog { background: transparent; }` —— 它是配 `QFrame#DialogFrame`
    用的（真正的底由内层那层 QFrame 画）。QMessageBox 没有这层内框，
    透明背景会把主窗口 / 桌面直接透出来，比不修还难看。所以这里单独按
    色板算一份**全不透明**的样式。

    ⚠️ 教训（v1.2.2 修）：全局 QSS 只在 `MainWindow._apply_theme()` 里
    `setStyleSheet` 在**主窗口自己**身上，**不会**流进任何独立顶层窗口。
    项目里每个自建对话框（update_ui / cleaner / uninstall / monitor）
    都各自带了一份，唯独 SAC 这个确认框漏了 —— 于是它走系统原生渲染，
    浅色应用里弹出一个深色弹窗（用户截图 2026-10-21）。按钮语言与主界面
    的 GhostButton / DangerButton 保持一致：取消=中性描边，确定=红色描边。
    """
    c = theme.current()
    red = c.get("red", "#e5484d")
    vals = dict(c)
    vals["red_pressed"] = theme._pressed_shade(red, theme.get_theme())
    return (
        "QMessageBox { background: %(window_top)s; color: %(text)s; }"
        "QMessageBox QLabel { color: %(text)s; background: transparent; }"
        # 取消：中性描边（= GhostButton）
        "QMessageBox QPushButton {"
        " background: transparent; color: %(text_dim)s;"
        " border: 1px solid %(border_strong)s; border-radius: 6px;"
        " padding: 7px 18px; min-width: 84px; font-weight: 700;"
        "}"
        "QMessageBox QPushButton:hover {"
        " background: %(accent_soft)s; border-color: %(accent)s;"
        " color: %(accent)s;"
        "}"
        "QMessageBox QPushButton:pressed {"
        " background: %(accent)s; border-color: %(accent)s;"
        " color: %(accent_text)s;"
        "}"
        "QMessageBox QPushButton:disabled {"
        " color: %(text_faint)s; border-color: %(border)s;"
        "}"
        # 确定（高危动作）：红色描边（= DangerButton）
        "QMessageBox QPushButton#DangerButton {"
        " background: transparent; color: %(red)s;"
        " border: 1px solid %(red)s;"
        "}"
        "QMessageBox QPushButton#DangerButton:hover {"
        " background: %(red)s; color: #ffffff; border-color: %(red)s;"
        "}"
        "QMessageBox QPushButton#DangerButton:pressed {"
        " background: %(red_pressed)s; border-color: %(red_pressed)s;"
        " color: #ffffff;"
        "}"
        # QMessageBox 在某些样式下会挂一个可缩放手柄，别让它裸着
        "QSizeGrip { background: transparent; }"
    ) % vals


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
            "应用偏好、开机自启与关于 Yuhub。",
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
        # SAC 开关同理：程序性改 checked 时挡信号
        self._sac_guard = False
        # SAC 读取结果的失败分类（KIND_*），_render_sac_state 按它分流提示
        self._sac_kind = sac.KIND_OK
        # 自动检查更新的偏好（读不到时默认开启）
        self._settings = QSettings("Yuhub", "Yuhub")
        raw = self._settings.value("auto_check_update", None)
        self._auto_check = True if raw is None else str(raw).lower() in ("true", "1")
        self._build_content(theme_setting, close_to_tray)

    # ------------------------------------------------------------------ 构建
    def _build_content(self, theme_setting, close_to_tray):
        self._etier_event.connect(self._on_etier_event)
        # 软件修复的进度/结果也走 _etier_event 通道（后台线程 → 主线程），
        # 用不同 kind 区分："repair_prog"（进度）/ "repair_ready"（下载完成）/
        # "repair_done"（结束）。
        self._build_appearance(theme_setting)
        self._build_startup_card(close_to_tray)
        self._build_sac_card()
        self._build_repair_card()
        self._build_accel_card()
        self._build_etier_card()
        self._build_about()
        self.add_stretch()

    # ------------------------------------------------------ 外观
    # 水墨重做后不再提供"主题包"切换；明暗模式在下面分段按钮里选
    # （跟随系统 / 浅色 / 深色），标题栏 🌙/☀️ 按钮也能直接切。
    def _build_appearance(self, theme_setting):
        appearance = QFrame()
        appearance.setObjectName("Card")
        av = QVBoxLayout(appearance)
        av.setContentsMargins(18, 16, 18, 16)
        av.setSpacing(10)

        t = QLabel("外观")
        t.setObjectName("CardTitle")
        av.addWidget(t)

        self.theme_segment = SegmentedControl(
            [("system", "跟随系统"), ("light", "浅色"), ("dark", "深色")],
            current=theme_setting,
        )
        self.theme_segment.changed.connect(self._on_theme_change)
        self.theme_segment.setMaximumWidth(380)
        av.addWidget(self.theme_segment, 0, Qt.AlignLeft)

        note = QLabel("水墨界面：浅色是宣纸黑墨，深色是玄墨白竹。")
        note.setObjectName("Faint")
        av.addWidget(note)

        self.add(appearance)

    def set_theme_setting(self, value):
        """外部（标题栏按钮）切换主题时同步分段按钮选中态。"""
        if getattr(self, "theme_segment", None) is not None:
            self.theme_segment.set_current(value)

    def set_pack_setting(self, _name):
        """旧接口：主题包切换 UI 已移除，这里只是兼容空操作。"""
        return

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

    # ------------------------------------------------------ Windows 安全 / SAC
    def _build_sac_card(self):
        """智能应用控制（SAC）开关。

        背景：部分用户在 Windows 11 上运行**未签名**的 Yuhub.exe 时，会被
        SAC 直接拦下（"智能应用控制已阻止可能不安全的应用"），而且这个机制
        **没有"仍要运行"、也没有按应用例外** —— 要么关掉 SAC，要么给程序
        做代码签名。这里提供一个开关，省得用户去翻 Windows 安全中心。

        实现见 sac.py：改
        HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy\\
        VerifiedAndReputablePolicyState（0关/1开/2评估），再跑
        `CiTool.exe -r` 刷新。**关闭前二次确认**，并明确告知老版 Win11
        关掉后不可逆。
        """
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        title = QLabel("Windows 安全")
        title.setObjectName("CardTitle")
        v.addWidget(title)

        self._sac_elevated = bool(winadmin.is_elevated())
        state, err, kind = sac.read_state_ex()
        self._sac_state = state
        self._sac_error = err or ""
        self._sac_kind = kind

        self.sw_sac = ToggleSwitch(state == sac.STATE_ON)
        self.sw_sac.toggled.connect(self._on_sac_toggled)
        v.addLayout(setting_row("智能应用控制 (SAC)", self.sw_sac))

        self.sac_status = small_note()
        v.addWidget(self.sac_status)

        v.addWidget(small_note(
            "开关「开启」= SAC 保护中（只允许可信 / 已签名程序运行）；"
            "「关闭」= 允许未签名程序运行（Yuhub 就属于未签名）。"
            "关闭会降低整机防护；老版本 Windows 11 关闭后无法直接重新打开"
            "（需重装或重置系统）。Microsoft Defender 与 SmartScreen 仍然有效。",
            warn=True))

        self._render_sac_state(state, err, kind, self._sac_elevated)
        self.add(card)

    def _render_sac_state(self, state, err="", kind=sac.KIND_OK, elevated=True):
        """按读到的状态刷新开关与说明（程序性改开关时用 _sac_guard 挡信号）。

        ⚠️ state 为 None 时有**三种**完全不同的情况，必须按 kind 分流：
        没有 SAC / 需要管理员 / 读取失败。v1.2.1 之前这里只分"含管理员"
        和"其它"两类，读取失败（含写入后回读失败）会被说成"本机没有
        SAC"—— 对着一台刚写成功、明明有 SAC 的机器撒谎，还把开关永久
        置灰。err 是会改的文案，分类不会，所以判据用 kind 不用文本。
        """
        self._sac_guard = True
        try:
            if state is None:
                self.sw_sac.setChecked(False)
                self.sw_sac.setEnabled(False)
                msg = str(err or "")
                if kind == sac.KIND_ABSENT:
                    self.sac_status.setText(
                        "本机没有智能应用控制（SAC）—— 它只存在于 Windows 11 "
                        "新装系统，Windows 10 没有这个功能。")
                elif kind == sac.KIND_DENIED:
                    self.sac_status.setText(
                        "无法读取 SAC 状态：需要管理员权限。"
                        "请以管理员身份运行 Yuhub 后再试。")
                else:
                    # 读取失败 ≠ 没有 SAC：如实摆出错误，别编一个"没有"出来
                    self.sac_status.setText(
                        "读取 SAC 状态失败：%s" % (msg or "未知错误"))
            else:
                self.sw_sac.setChecked(state == sac.STATE_ON)
                self.sw_sac.setEnabled(bool(elevated))
                text = "当前状态：%s" % sac.state_text(state)
                if not elevated:
                    text += "（需要管理员权限才能修改）"
                self.sac_status.setText(text)
        finally:
            self._sac_guard = False

    def _on_sac_toggled(self, checked):
        """拨动开关 → 写入注册表并刷新策略（关闭前二次确认）。"""
        if self._sac_guard:
            return
        if not self._sac_elevated:
            # 没权限：把开关拨回真实状态并说明
            self._render_sac_state(self._sac_state, self._sac_error,
                                   self._sac_kind, False)
            self.toast("需要管理员权限才能修改 SAC")
            return
        if not checked and not self._confirm_disable_sac():
            self._sac_guard = True
            self.sw_sac.setChecked(True)      # 用户取消 → 拨回去
            self._sac_guard = False
            return
        self.sw_sac.setEnabled(False)
        self.sac_status.setText(
            "正在应用…（%s）"
            % ("关闭时可能需要几秒刷新策略" if not checked else "开启后通常立即生效"))
        threading.Thread(
            target=self._do_apply_sac, args=(bool(checked),),
            name="SacToggle", daemon=True).start()

    def _build_sac_confirm_box(self):
        """关闭 SAC 是"降防护 + 老版本不可逆"的高危动作，二次确认框。

        ⚠️ **必须自己带样式表**：全局 QSS 只挂主窗口，流不到这个独立顶层
        窗口（见 `_messagebox_qss` 的教训说明）。按钮按主界面的命名注册
        对象名，样式语言才能对上（DangerButton=红色描边 / 其余=中性描边）。
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("关闭智能应用控制")
        box.setText("确定要关闭智能应用控制（SAC）吗？")
        box.setInformativeText(
            "关闭后将允许运行未签名的程序（包括 Yuhub）。\n\n"
            "· 整机安全防护会降低；\n"
            "· 老版本 Windows 11 关闭后无法直接重新打开，只能重装或重置系统；\n"
            "· Microsoft Defender 实时防护与 SmartScreen 仍然有效。\n\n"
            "确定继续？")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        yes = box.button(QMessageBox.Yes)
        if yes is not None:
            yes.setObjectName("DangerButton")     # 高危动作 → 红色描边
        box.setStyleSheet(_messagebox_qss())
        return box

    def _confirm_disable_sac(self):
        """弹出二次确认框；返回 True 表示用户选了「是」。"""
        return self._build_sac_confirm_box().exec() == QMessageBox.Yes

    def _do_apply_sac(self, enable):
        """后台线程：写注册表 + 刷新策略，再回读真实状态。"""
        target = sac.STATE_ON if enable else sac.STATE_OFF
        ok, msg = sac.set_state(target)
        state, err, kind = sac.read_state_ex()
        self._etier_event.emit("sac_done", {
            "ok": ok, "msg": msg, "state": state, "err": err,
            "kind": kind, "target": target})

    def _on_sac_applied(self, payload):
        """主线程：落地 SAC 写入结果，并按真实状态刷新开关。"""
        ok = bool(payload.get("ok"))
        msg = str(payload.get("msg") or "")
        state = payload.get("state")
        err = str(payload.get("err") or "")
        kind = str(payload.get("kind") or sac.KIND_ERROR)
        target = payload.get("target")
        self._sac_state = state
        self._sac_error = err
        self._sac_kind = kind
        if ok and state is None:
            # 写入成功但回读失败：按「目标态」显示，并如实说明读取失败。
            # ⚠️ 绝不能走 _render_sac_state(None, ...) —— 那会把提示栏写成
            # "本机没有 SAC"（本机明明有、而且刚写成功过），还会把开关永久
            # 置灰，用户再也没法拨。v1.2.1 修的就是这条路径。
            self._sac_guard = True
            try:
                self.sw_sac.setChecked(target == sac.STATE_ON)
                self.sw_sac.setEnabled(bool(self._sac_elevated))
                self.sac_status.setText(
                    "已%s，但读取状态失败：%s（可重启或重新进入本页刷新）"
                    % ("开启" if target == sac.STATE_ON else "关闭",
                       err or "未知错误"))
            finally:
                self._sac_guard = False
            self.toast("已写入设置，但读取状态失败（%s）" % (err or "未知错误"))
            return
        self._render_sac_state(state, err, kind, self._sac_elevated)
        if not ok:
            self.toast("SAC 设置失败：%s" % msg)
            return
        if state == target:
            self.toast("已%s智能应用控制"
                       % ("开启" if target == sac.STATE_ON else "关闭"))
        else:
            # 写入成功但策略还没切过来 → 多半要重启
            self.toast("已写入设置，重启后生效（当前仍为%s）"
                       % sac.state_text(state))

    def _build_about(self):
        self.add(
            info_card(
                "关于 Yuhub",
                f"版本：{VERSION_LABEL}\n"
                "界面为水墨风：明暗模式自动跟随系统，浅色宣纸黑墨、"
                "深色玄墨白竹。\n"
                "技术栈：Python + PySide6 (Qt)\n"
                "已上线：本机配置检测（含逐盘容量）、实时占用监控、C盘清理、"
                "多线程下载、软件卸载与残留清理、跨网联机、系统托盘驻留、"
                "单实例限制、开机自启。",
            )
        )

    # ------------------------------------------------------ 修复软件
    def _build_repair_card(self):
        """一键修复软件：从 GitHub Releases 重新下载**当前版本**并替换自身。

        适用场景：exe 被杀毒软件误删/损坏、更新中断后文件异常、
        手动改动导致程序行为不对——重下同版本覆盖即可，不必等新版本。
        下载走与自动更新完全相同的线路矩阵（官方直连 → 国内镜像），
        sha256 校验通过后才替换；替换失败自动回滚，不会把程序弄坏。
        """
        # 下载中要能取消：同一个按钮从「一键修复软件」切到「取消」。
        # 旧版点下去就把按钮 setEnabled(False) 撒手不管，用户既没取消
        # 入口、也不知道进度——"更新的时候取消不了"就是这么来的。
        self._repair_busy = False
        self._repair_cancel = False

        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        title = QLabel("修复软件")
        title.setObjectName("CardTitle")
        v.addWidget(title)

        self.repair_status = QLabel(
            "当前版本 %s。程序被误删/损坏或行为异常时，"
            "可从 GitHub 重新下载本版本并自动替换。" % VERSION_LABEL)
        self.repair_status.setWordWrap(True)
        self.repair_status.setStyleSheet("font-size: 12px;")
        v.addWidget(self.repair_status)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.btn_repair_app = ghost_button("一键修复软件")
        self.btn_repair_app.clicked.connect(self._on_repair_app_clicked)
        row.addWidget(self.btn_repair_app)
        v.addLayout(row)

        self.add(card)

    def _on_repair_app_clicked(self):
        # 同一个按钮承担两件事：空闲时是「一键修复软件」，下载中变「取消」。
        if self._repair_busy:
            self._request_repair_cancel()
            return
        win = self.window()
        if not hasattr(win, "apply_repair_package"):
            self.notify("当前环境不支持在线修复（源码运行时请直接重跑源码）")
            return
        self._repair_busy = True
        self._repair_cancel = False
        self.btn_repair_app.setText("取消")
        self.btn_repair_app.setEnabled(True)
        self.repair_status.setText("正在查询更新信息…")
        threading.Thread(
            target=self._do_repair_app, name="AppRepair", daemon=True).start()

    def _request_repair_cancel(self):
        """把取消请求交给下载线程（它每 0.2 秒看一次标志）。"""
        if self._repair_cancel:
            return
        self._repair_cancel = True
        self.btn_repair_app.setText("正在取消…")
        self.btn_repair_app.setEnabled(False)
        self.repair_status.setText("正在取消…（等当前连接收尾）")

    def _reset_repair_button(self):
        self._repair_busy = False
        self._repair_cancel = False
        self.btn_repair_app.setText("一键修复软件")
        self.btn_repair_app.setEnabled(True)

    def _do_repair_app(self):
        """后台线程：定位当前版本的 Release → 下载 → 回主线程执行替换。"""
        def progress(payload):
            # ⚠️ 别直接 str(payload)：下载中的回调载荷是 downloader 的
            # snapshot 字典，直接字符串化就是往界面上糊一坨 JSON
            #（用户截图反馈的那串 {"status": "done", ...} 正是这么来的）。
            phase, text = updater.format_progress(payload)
            if text:
                self._etier_event.emit("repair_prog", text)

        # ① 定位与当前版本一致的 Release（先 v 前缀 tag，再裸版本 tag）
        info, err = updater.fetch_release_by_tag("v" + VERSION)
        if info is None and not err:
            info, err = updater.fetch_release_by_tag(VERSION)
        if info is None and not err:
            # 当前版本不在 Release 列表（本地是未发布版本/tag 改过名）→
            # 修复到最新版仍然优于报错退出，明确告知用户即可
            info, err = updater.fetch_release()
            if info is not None:
                self._etier_event.emit(
                    "repair_prog",
                    "未找到 %s 的发布记录，将修复到最新版 %s"
                    % (VERSION_LABEL, info.tag))
        if info is None:
            self._etier_event.emit(
                "app_repair_done",
                {"ok": False, "msg": err or "GitHub 上找不到当前版本的发布记录"})
            return
        if not info.asset_url:
            self._etier_event.emit(
                "app_repair_done", {"ok": False, "msg": "该版本没有可下载的文件"})
            return
        if self._repair_cancel:
            self._etier_event.emit("app_repair_done", {"ok": False, "msg": "已取消"})
            return

        # ② 下载（线路矩阵 + sha256 校验，与自动更新同一条链路）
        self._etier_event.emit(
            "repair_prog", "正在下载 %s（约 %.0f MB）…"
            % (info.tag, info.asset_size / 1048576.0))
        ok, msg, path = updater.download_update(
            info,
            on_progress=progress,
            # 没有这个 cancel 回调，用户在下载中就没法中止——
            # 只能眼睁睁等到 59MB 下完或自己放弃
            cancel=lambda: self._repair_cancel,
        )
        if not ok:
            self._etier_event.emit("app_repair_done", {"ok": False, "msg": msg})
            return

        # ③ 回主线程执行替换（替换器会拉起新 exe，本进程退出）
        self._etier_event.emit(
            "repair_ready", {"info": info.to_dict(), "path": path})

    # ------------------------------------------------------ 网络加速框架
    def _build_accel_card(self):
        """网络加速框架的体检 + 一键修复。

        修复做的事（全部走 ui.pages.accel_page.repair_framework，与加速页
        共用同一份实现）：停掉残留内核 → 清 hosts 加速块 → 用本体里内嵌的
        那份覆盖写回落点并校验 sha256 → 重建/确认根证书（原本信任过且证书
        被重建时自动重新信任）。适用场景：框架 exe 被安全软件删掉、更新中
        断导致文件损坏、加速开启异常退出后 hosts 残留 —— 重装 Yuhub 太重，
        这里一键恢复即可。
        """
        self._accel_repair_busy = False

        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)

        title = QLabel("网络加速框架")
        title.setObjectName("CardTitle")
        v.addWidget(title)

        # 状态：后台检测，不在构造函数里起子进程拖慢页面
        self.accel_status = QLabel("框架检测中…")
        self.accel_status.setWordWrap(True)
        self.accel_status.setStyleSheet("font-size: 12px;")
        v.addWidget(self.accel_status)

        v.addWidget(small_note(
            "加速框架随程序内嵌，进「网络加速」页会自动就位。若它被安全软件"
            "误删、文件损坏，或加速异常退出后残留 hosts 记录，点「修复加速"
            "框架」会用内嵌文件重新写入并校验，同时清理 hosts 与重建证书，"
            "无需重装 Yuhub。"))

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.btn_accel_repair = ghost_button("修复加速框架")
        self.btn_accel_repair.clicked.connect(self._on_accel_repair_clicked)
        row.addWidget(self.btn_accel_repair)
        v.addLayout(row)

        self.add(card)

        threading.Thread(target=self._do_accel_check, daemon=True).start()

    def _do_accel_check(self):
        """后台体检：框架在不在 + 证书状态（不触碰任何 UI）。"""
        try:
            from . import accel_page as ap
            installed = ap.framework_installed()
            status = ap.framework_status() if installed else None
        except Exception as exc:                                   # noqa: BLE001
            installed, status = False, {"error": repr(exc)}
        self._etier_event.emit("accel_check",
                               {"installed": installed, "status": status})

    def _on_accel_repair_clicked(self):
        if self._accel_repair_busy:
            return
        self._accel_repair_busy = True
        self.btn_accel_repair.setEnabled(False)
        self.accel_status.setText("正在修复加速框架…")
        threading.Thread(
            target=self._do_accel_repair, name="AccelRepair", daemon=True).start()

    def _do_accel_repair(self):
        def progress(text):
            self._etier_event.emit("accel_repair_prog", text)

        try:
            from . import accel_page as ap
            ok, msg = ap.repair_framework(progress=progress)
        except Exception as exc:                                   # noqa: BLE001
            ok, msg = False, repr(exc)
        self._etier_event.emit("accel_repair_done", {"ok": ok, "msg": msg})

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
        # 顺带报一下管理 CLI 的状态：它是**可选件**，缺了只是成员表退回
        # ARP + 信标的旁路推断（联机/云盘/分屏全不受影响），所以这里只做
        # 说明，不影响上面的 ok——两个判据混在一起会让"组件正常"变红。
        try:
            cli_ok, cli_detail = etier.cli_health()
        except Exception as exc:
            cli_ok, cli_detail = False, repr(exc)
        self._etier_event.emit("check", {"ok": ok, "detail": detail,
                                         "cli_ok": cli_ok,
                                         "cli_detail": cli_detail})

    def _on_etier_event(self, kind, payload):
        """后台线程 → 主线程的事件落地（体检结果 / 修复进度与结果）。"""
        if kind == "check":
            ok = payload.get("ok")
            detail = str(payload.get("detail") or "")
            cli_ok = bool(payload.get("cli_ok"))
            cli_detail = str(payload.get("cli_detail") or "")
            base = ("组件正常（%s）" % detail) if ok else ("组件异常：%s" % detail)
            if cli_ok:
                base += "；成员表：已接入 core（权威数据）"
            else:
                base += "；成员表：%s" % (cli_detail or "未接入")
            self.etier_status.setText(base)
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
        elif kind == "repair_prog":
            # 软件修复进度（区分于上面的组件修复进度）
            self.repair_status.setText(str(payload or ""))
        elif kind == "app_repair_done":
            # 软件修复失败/取消。**必须与组件修复的 repair_done 分开**：
            # 两者共用一个 kind 时，软件修复失败会把「一键修复」（组件）
            # 的按钮启用，而「一键修复软件」还灰着——状态全乱。
            msg = str(payload.get("msg") or "")
            self._reset_repair_button()
            if msg == "已取消":
                self.repair_status.setText("已取消修复，未做任何改动。")
                self.toast("已取消修复")
            else:
                self.repair_status.setText("修复失败：%s" % msg)
                self.toast("修复失败：%s" % msg)
        elif kind == "repair_ready":
            # 下载完成 → 主线程里启动替换器（它会等本进程退出后替换）
            info = updater.ReleaseInfo.from_dict(payload.get("info") or {})
            path = str(payload.get("path") or "")
            win = self.window()
            if info.tag and path and hasattr(win, "apply_repair_package"):
                self.repair_status.setText("下载完成，正在替换程序…")
                self._reset_repair_button()
                win.apply_repair_package(info, path)
            else:
                self._reset_repair_button()
                self.repair_status.setText("修复失败：下载结果无效")
        elif kind == "accel_check":
            self._render_accel_status(payload.get("installed"),
                                      payload.get("status"))
        elif kind == "accel_repair_prog":
            self.accel_status.setText(str(payload or ""))
        elif kind == "accel_repair_done":
            ok = bool(payload.get("ok"))
            msg = str(payload.get("msg") or "")
            self._accel_repair_busy = False
            self.btn_accel_repair.setEnabled(True)
            if ok:
                self.accel_status.setText(msg)
                self.toast("加速框架已修复：%s" % msg)
                # 加速页可能已经建好并停在旧状态上，通知它重读框架/证书
                self._refresh_accel_page()
            else:
                self.accel_status.setText("修复失败：%s" % msg)
                self.toast("修复失败：%s" % msg)
        elif kind == "sac_done":
            self._on_sac_applied(payload)
        else:
            return

    def _render_accel_status(self, installed, status):
        """把框架体检结果写进状态行。"""
        if not installed:
            self.accel_status.setText(
                "框架尚未就位（进一次「网络加速」页会自动安装，"
                "或直接点下面的「修复加速框架」）。")
            return
        if not isinstance(status, dict) or not status.get("ca"):
            self.accel_status.setText(
                "框架已就位，但根证书状态异常 —— 建议点「修复加速框架」重建。")
            return
        trusted = status.get("trusted")
        days = status.get("expires_in_days")
        bits = ["框架已就位", "根证书"
                + ("已信任" if trusted else "未信任（加速前需在加速页点「信任证书」）")]
        if isinstance(days, int):
            bits.append("证书剩余 %d 天" % days)
        self.accel_status.setText(" · ".join(bits))

    def _refresh_accel_page(self):
        """若「网络加速」页已构造过，让它重读状态（不触发懒构造）。"""
        win = self.window()
        pages = getattr(win, "_pages", None)
        page = pages.get("accel") if hasattr(pages, "get") else None
        if page is not None and hasattr(page, "refresh_state"):
            try:
                page.refresh_state()
            except Exception:                                      # noqa: BLE001
                pass

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
        # 小号说明文字的颜色是**内联**写死的，不会随 QSS 自动更新，主题
        # 切换后必须逐条重刷。先按 small_note 登记的 noteWarn 属性刷一遍，
        # 再单独处理 autostart_hint —— 它的警示态是运行时变的（注册表指向
        # 别处时变琥珀色），属性停留在创建时的值，必须放最后按最新态刷。
        # ⚠️ v1.2.1 之前这里只刷了 autostart_hint / tray_hint 两条，SAC 卡
        #    （以及后面各卡）的提示栏切主题后一直留在旧色板上 —— 浅色模式下
        #    还留着深色的 text_faint，淡得几乎看不见。
        for lbl in self.findChildren(QLabel):
            if lbl.property("smallNote"):
                _style_note(lbl, bool(lbl.property("noteWarn")))
        _style_note(self.autostart_hint, self._autostart_is_warn)

    # set_theme_setting / set_pack_setting 的兼容空操作见 _build_content 上方
    # （外观切换 UI 已随水墨重做移除）。

    # -------------------------------------------------------------- 开机自启
    def _autostart_state(self):
        """返回 (说明文字, 是否属于需要注意的情况)。"""
        if not autostart.is_supported():
            return "当前系统不支持自动启动（本功能仅覆盖 Windows）。", True
        current = autostart.registered_command()
        if not current:
            return (
                "未开启。开启后将在登录 Windows 时自动启动，"
                "并静默驻留系统托盘、不弹出主窗口。"
                "Yuhub 以管理员身份运行，登录时由 Windows 请求一次"
                "管理员权限（点「是」即通过）。",
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
