"""异地联机页：内嵌 EasyTier 的跨网组网，把异地电脑拉进同一个虚拟局域网。

分工：引擎在 `etier.py`（纯 Python、不依赖 Qt），本页只做三件事：
  - 把用户输入的房间码 + 密码变成 EasyTier 的启动/停止调用；
  - 把引擎回调（在后台线程里发生）用 Qt 信号转到主线程刷界面；
  - 把「游戏那边该怎么连」讲清楚——真正容易卡住的不是程序，
    而是玩家不知道接下来在游戏里该点哪里。

背景：局域网直连（UPnP / 地址交换打洞）方案已按需求移除，本页只保留
「跨网房间」这一种异地联机方式。EasyTier 会在本机创建一块二层虚拟网卡，
把输入了相同房间码+密码的电脑拉进同一个虚拟局域网，游戏里的「局域网」
列表能直接互相看到，无需填地址、无需自建服务器。
"""

import secrets
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

import etier
from .. import theme
from ..widgets import SegmentedControl, ghost_button, info_card, primary_button
from .base_page import BasePage


# 生成房间码用的字母表：去掉 0/O、1/I/L 这类肉眼易混的字符
_CODE_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def random_room_code():
    """生成 "XXX-XXX" 形式的可读房间码。"""
    picks = [secrets.choice(_CODE_CHARS) for _ in range(6)]
    return "%s-%s" % ("".join(picks[:3]), "".join(picks[3:]))


class LanPage(BasePage):
    TINT_KEY = "tile_2"
    BADGE = "异地联机"
    BADGE_STYLE = ""

    # 引擎回调发生在后台线程，必须经信号排队回主线程再碰控件
    _event = Signal(str, object)

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "异地联机",
            "内嵌 EasyTier，把异地电脑拉进同一个虚拟局域网：双方填同一组房间码+密码即可互通，游戏里直接联机。",
            icon="🌐",
            notify=notify,
            parent=parent,
        )
        self._etier = None            # EasyTier 实例（跨网房间运行中）
        self._etier_busy = False      # 正在启动/停止跨网房间
        self._running = False         # 是否在运行（用于状态显示）
        self._my_ip = ""              # 本机虚拟 IP（运行中）
        self._event.connect(self._on_event)
        self._build_content()
        self._on_mode_changed("host")

        self._timer = QTimer(self)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        theme.bus.changed.connect(self._refresh_status_style)

        # 启动即清理上次残留（后台线程，不打扰用户、不弹 UAC）
        threading.Thread(target=self._cleanup_orphans_on_launch,
                         daemon=True).start()

    def _cleanup_orphans_on_launch(self):
        """Yuhub 启动时后台清理上次残留的联机房间（轻量级，不弹 UAC）。

        为什么要做：旧版本退出时用 daemon 线程停房间，主进程退出会把线程
        杀掉、停止信号可能没写出去，easytier-core 就残留了——用户下次打开
        会看到"已有一个房间正在运行"、UI 还停不掉它。

        这里启动时静默清一次（写停止信号 + 普通权限 taskkill）。清不掉的
        不在这里弹 UAC 打扰用户，留给点「创建跨网房间」时走完整清理
        （那条路径有提权兜底）。
        """
        try:
            if not etier.core_running():
                return
            self._event.emit("_etier_log", "检测到上次残留的联机房间，正在自动清理…")
            tier = etier.EasyTier()
            tier.request_stop()
            if tier.wait_stopped(2.5):
                self._event.emit("_etier_log", "残留房间已清理")
                return
            etier.force_kill_core()
            time.sleep(0.5)
            if not etier.core_running():
                self._event.emit("_etier_log", "残留房间已清理")
            else:
                self._event.emit(
                    "_etier_log",
                    "残留房间需管理员权限才能清掉，点「创建跨网房间」时会自动处理",
                )
        except Exception:
            pass

    # ------------------------------------------------------------------ 构建
    def _build_content(self):
        self.add(self._build_mode_card())
        self.add(self._build_members_card())
        self.add(self._build_log_card())
        self.add(self._build_help_card())
        self.add_stretch()

    def _label(self, text):
        lb = QLabel(text)
        lb.setObjectName("Muted")
        lb.setStyleSheet("font-size: 12px;")
        return lb

    def _note(self, text, warn=False):
        lb = QLabel(text)
        lb.setObjectName("Warn" if warn else "Faint")
        lb.setWordWrap(True)
        lb.setStyleSheet("font-size: 11px;")
        return lb

    def _build_mode_card(self):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("跨网房间")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.status_label = QLabel("未启动")
        head.addWidget(self.status_label)
        v.addLayout(head)

        # 身份切换：房主创建、成员加入。底层都是同一个 EasyTier（无中心），
        # 只是文案与引导不同，让用户清楚自己是「开房」还是「进房」。
        r0 = QHBoxLayout()
        r0.setSpacing(8)
        r0.addWidget(self._label("身份"))
        self.mode = SegmentedControl(
            [("host", "我是房主"), ("join", "我加入别人")], current="host"
        )
        self.mode.changed.connect(self._on_mode_changed)
        r0.addWidget(self.mode)
        r0.addStretch(1)
        v.addLayout(r0)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        r1.addWidget(self._label("房间码"))
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("点「随机」生成，或自己起一个")
        self.code_edit.setMaximumWidth(160)
        r1.addWidget(self.code_edit)
        self.btn_random = ghost_button("随机")
        self.btn_random.clicked.connect(
            lambda: self.code_edit.setText(random_room_code())
        )
        r1.addWidget(self.btn_random)
        r1.addStretch(1)
        v.addLayout(r1)

        # 成员视角的提示（身份切换后动态更新）
        self.mode_note = self._note("")
        v.addWidget(self.mode_note)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.addWidget(self._label("房间密码"))
        self.pass_edit = QLineEdit("")
        self.pass_edit.setPlaceholderText("房主和成员必须填同一个")
        self.pass_edit.setMaximumWidth(220)
        self.pass_edit.setEchoMode(QLineEdit.Password)
        r2.addWidget(self.pass_edit)
        r2.addStretch(1)
        v.addLayout(r2)

        self.status_note = self._note("")
        v.addWidget(self.status_note)

        # 本机虚拟 IP + 复制（方便游戏里直接输 IP 联机）
        rip = QHBoxLayout()
        rip.setSpacing(8)
        rip.addWidget(self._label("本机虚拟 IP"))
        self.ip_label = QLabel("--")
        self.ip_label.setObjectName("LanIp")
        self.ip_label.setStyleSheet(
            "font-size: 14px; font-weight: 700; font-family: Consolas, 'Courier New', monospace;"
        )
        rip.addWidget(self.ip_label)
        self.btn_copy_ip = ghost_button("复制")
        self.btn_copy_ip.setEnabled(False)
        self.btn_copy_ip.clicked.connect(self._on_copy_ip)
        rip.addWidget(self.btn_copy_ip)
        rip.addStretch(1)
        v.addLayout(rip)

        v.addWidget(self._note(
            "房主点「随机」生成房间码，把房间码和密码发给队友；队友选「我加入别人」、"
            "填同一组码+密码点「加入跨网房间」。两台电脑就进了同一个虚拟局域网——"
            "游戏里的「局域网」列表能直接看到对方的房间（无需再填地址）。"
            "首次启动会弹一次 UAC 管理员确认（创建虚拟网卡需要），之后不再弹窗。",
            warn=False
        ))

        brow = QHBoxLayout()
        brow.setSpacing(8)
        self.btn_stop = ghost_button("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        brow.addWidget(self.btn_stop)
        self.btn_start = primary_button("创建跨网房间")
        self.btn_start.clicked.connect(self._on_start)
        brow.addWidget(self.btn_start)
        v.addLayout(brow)

        return card

    def _build_members_card(self):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("在线成员")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.members_count = QLabel("0 人")
        self.members_count.setObjectName("Muted")
        self.members_count.setStyleSheet("font-size: 11px;")
        head.addWidget(self.members_count)
        v.addLayout(head)

        self.members_list = QLabel("启动后这里会显示同一房间的成员虚拟 IP")
        self.members_list.setObjectName("Faint")
        self.members_list.setWordWrap(True)
        self.members_list.setStyleSheet(
            "font-size: 12px; font-family: Consolas, 'Courier New', monospace;"
        )
        v.addWidget(self.members_list)
        return card

    def _build_log_card(self):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        btn_clear = ghost_button("清空")
        btn_clear.clicked.connect(lambda: self.log_view.clear())
        head.addWidget(btn_clear)
        v.addLayout(head)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LanLog")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)     # 自动丢掉旧行，不会越滚越吃内存
        self.log_view.setFixedHeight(150)
        v.addWidget(self.log_view)
        return card

    def _build_help_card(self):
        return info_card(
            "怎么用（以 Minecraft 为例）",
            "1. 房主：先在游戏里点「对局域网开放」，回到这里选「我是房主」，\n"
            "   点「随机」生成房间码，再点「创建跨网房间」（首次弹一次 UAC，点「是」）。\n"
            "2. 把房间码和密码告诉队友（建议走语音/私聊，别公开）。\n"
            "3. 队友：选「我加入别人」，填同一组房间码和密码，点「加入跨网房间」。\n"
            "4. 队友打开游戏的「多人游戏」，列表里会出现房主的房间，点进去即可。\n"
            "\n"
            "原理：内嵌 EasyTier（Apache 2.0 开源）创建一块二层虚拟网卡，\n"
            "把填了相同房间码+密码的电脑拉进同一个虚拟局域网。P2P 打洞优先，\n"
            "打洞不通时走社区共享节点中继兜底，无需自建服务器。\n"
            "无需提前安装任何东西——虚拟网卡驱动已内置在软件里，首次启动自动装好。\n"
            "\n"
            "安全：房间码 + 密码双重校验，密码不落盘、不通过明文传输，\n"
            "停止后即从内存清除。找不到房间时，检查游戏端口：Minecraft 开完房\n"
            "后聊天栏会显示“Local game hosted on port xxxxx”，那个数字就是它。\n"
            "\n"
            "注意事项：首次启动需要一次管理员授权（创建虚拟网卡需要）。\n"
            "首次运行时 Windows 防火墙可能弹窗，允许即可；否则隧道建不起来。",
        )

    # ------------------------------------------------------------------ 交互
    def _start_button_text(self):
        """根据当前身份返回启动按钮文案。"""
        return "创建跨网房间" if self.mode.current() == "host" else "加入跨网房间"

    def _on_mode_changed(self, value):
        """身份切换：更新按钮文案与成员引导，其余逻辑完全一致。"""
        is_host = value == "host"
        if not self._etier_busy and not self._running:
            self.btn_start.setText(self._start_button_text())
        if is_host:
            self.mode_note.setText(
                "你是房主：点「随机」生成房间码，把码和密码发给队友。"
            )
        else:
            self.mode_note.setText(
                "你是成员：把房主发给你的房间码和密码填进下面，点「加入跨网房间」。"
            )

    def _on_start(self):
        if self._etier_busy or self._running:
            return
        code = self.code_edit.text().strip()
        password = self.pass_edit.text()
        if len(code) < 4:
            self.toast("房间码至少 4 个字符（可点「随机」自动生成）")
            return
        if len(password) < 4:
            self.toast("跨网房间必须设密码，至少 4 个字符")
            return
        self._etier_busy = True
        self.btn_start.setEnabled(False)
        self.btn_start.setText("正在启动…")
        action = "创建" if self.mode.current() == "host" else "加入"
        self._set_status_note(
            "正在%s：释放内置组件 → 等待 UAC 授权 → 拉起虚拟网卡。"
            "首次启动可能要 10~20 秒，请在 UAC 弹窗里点「是」。" % action
        )
        self._append_log("跨网房间：正在%s（房间码 %s）…" % (action, code))
        # EasyTier.start 会阻塞最长 45 秒（轮询虚拟网卡就绪），必须丢后台
        threading.Thread(target=self._do_start, args=(code, password),
                         daemon=True).start()

    def _do_start(self, code, password):
        tier = etier.EasyTier(on_log=lambda m: self._event.emit("_etier_log", m))
        # 房主固定 10.126.126.1，成员固定 10.126.126.2，让 IP 可预期、方便游戏直连
        ipv4 = "10.126.126.1" if self.mode.current() == "host" else "10.126.126.2"
        try:
            ok, msg = tier.start(
                "yuhub-" + code.lower().replace(" ", ""), password,
                timeout=20.0, ipv4=ipv4,
            )
        except Exception as exc:
            ok, msg = False, str(exc)
        self._event.emit("_etier_done", {"ok": ok, "msg": msg, "tier": tier,
                                          "ipv4": ipv4})

    def _on_done(self, payload):
        self._etier_busy = False
        tier = payload.get("tier")
        if payload.get("ok") and tier is not None:
            self._etier = tier
            self._running = True
            self._my_ip = payload.get("ipv4", "") or etier.virtual_adapter_ip()
            self._set_running(True)
            self._refresh_status_style()
            self._update_ip()
            self._append_log("跨网房间已就绪。游戏里的「局域网」列表现在能看到"
                             "同一房间码队友开的房间了。")
        else:
            self._running = False
            self._my_ip = ""
            self.btn_start.setEnabled(True)
            self.btn_start.setText(self._start_button_text())
            self._set_status_note("")
            msg = payload.get("msg") or "未知原因"
            self._append_log("跨网房间启动失败：%s" % msg)
            self.toast("启动失败：%s" % msg)

    def _on_stop(self):
        if self._etier_busy:
            return          # 已有启动/停止在进行，避免重复线程
        if self._etier is None and not self._running:
            # 没有可停的房间：把界面归位即可（别留下"正在停止"的死状态）
            self.btn_start.setText(self._start_button_text())
            self.btn_start.setEnabled(True)
            self._set_status_note("")
            return
        tier, self._etier = self._etier, None
        self._etier_busy = True
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.btn_start.setText("正在停止…")
        self._set_status_note("正在停止跨网房间…")
        threading.Thread(target=self._do_stop, args=(tier,),
                         daemon=True).start()

    def _do_stop(self, tier):
        try:
            if tier is not None:
                tier.stop()
        except Exception:
            pass
        self._event.emit("_etier_stopped", None)

    def _on_stopped(self):
        self._etier_busy = False
        self._running = False
        self._my_ip = ""
        self._set_running(False)
        self._set_status_note("")
        self._clear_members()
        self._append_log("跨网房间已停止")

    def _set_running(self, flag):
        self.btn_stop.setEnabled(flag)
        self.btn_start.setEnabled(not flag)
        # 无论启动成功还是失败，启动按钮文案都必须回到正常值。
        # 曾经的 bug：这里只在 `not flag` 时复位文案，于是**启动成功后**
        # 按钮永远停在「正在启动…」——用户看到的就是"创建跨网房间后一直
        # 显示正在启动"（房间其实已经建好了，只是按钮没恢复）。
        self.btn_start.setText(self._start_button_text())
        for w in (self.code_edit, self.pass_edit, self.btn_random, self.mode):
            w.setEnabled(not flag)
        # IP 显示与复制按钮
        self.btn_copy_ip.setEnabled(flag and bool(self._my_ip))
        if not flag:
            self.ip_label.setText("--")
            self._clear_members()

    def _clear_members(self):
        """清空成员列表显示。"""
        self.members_list.setText("启动后这里会显示同一房间的成员虚拟 IP")
        self.members_count.setText("0 人")

    def _set_status_note(self, text):
        try:
            self.status_note.setText(text or "")
        except RuntimeError:
            pass

    def _on_copy_ip(self):
        """复制本机虚拟 IP 到剪贴板。"""
        ip = self._my_ip or etier.virtual_adapter_ip()
        if not ip:
            self.toast("当前没有虚拟 IP 可复制")
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(ip)
        self.toast("已复制虚拟 IP：%s" % ip)
        self._append_log("已复制本机虚拟 IP %s" % ip)

    def _update_ip(self):
        """轮询虚拟网卡状态，刷新跨网房间状态行、本机 IP 与成员列表。"""
        ip = etier.virtual_adapter_ip()
        if ip:
            self._my_ip = ip
            self.ip_label.setText(ip)
            self.btn_copy_ip.setEnabled(True)
            # 刷新成员列表
            members = etier.list_members(ip)
            if members:
                self.members_list.setText("、".join(members))
                self.members_count.setText("%d 人" % len(members))
            else:
                self.members_list.setText("暂未发现其他成员（等队友加入后会显示）")
                self.members_count.setText("0 人")
            self._set_status_note(
                "虚拟网卡已就绪：本机虚拟 IP %s。把房间码和密码发给队友，"
                "让他们也启动跨网房间即可。" % ip
            )
            return
        # 只有拿不到 IP 时才做 tasklist 探测（每 800ms 起一个进程太重）
        if etier.core_running():
            self._set_status_note("EasyTier 运行中，虚拟网卡还没就绪，再等等…")
        else:
            self._set_status_note("EasyTier 未在运行（可能已退出），请点「停止」后重试")

    # ------------------------------------------------------------------ 事件
    def _on_event(self, kind, payload):
        try:
            if kind == "_etier_log":
                self._append_log("[EasyTier] %s" % payload)
            elif kind == "_etier_done":
                self._on_done(payload or {})
            elif kind == "_etier_stopped":
                self._on_stopped()
        except Exception as exc:                     # 界面出错不该拖垮转发
            self._append_log("界面更新出错：%s" % exc)

    def _refresh_status_style(self, *_args):
        p = theme.current()
        running = getattr(self, "_running", False)
        color = p["green"] if running else p["text_faint"]
        text = "跨网房间运行中" if running else "未启动"
        try:
            self.status_label.setText(text)
            self.status_label.setStyleSheet(
                "font-size: 11px; font-weight: 700; color: %s; background: transparent;"
                "border: 1px solid %s; border-radius: 4px; padding: 2px 10px;"
                % (color, color)
            )
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ 刷新
    def _tick(self):
        if self._etier is not None and self._running:
            # 检测网络切换（WiFi→热点 / 换 WiFi）：物理网络指纹变了说明
            # 虚拟局域网大概率已失效，自动关闭，避免一直卡在「房间开着」。
            if not self._etier_busy and self._etier.network_changed():
                self._append_log("检测到网络已切换（WiFi/热点变化），自动关闭虚拟局域网…")
                self.toast("检测到网络切换，已自动关闭虚拟局域网")
                self._on_stop()
                return
            self._update_ip()

    # ------------------------------------------------------------------ 杂项
    def _append_log(self, text):
        stamp = time.strftime("%H:%M:%S")
        try:
            self.log_view.appendPlainText("[%s] %s" % (stamp, text))
        except RuntimeError:
            pass

    def on_shown(self):
        self._tick()

    def shutdown(self):
        """窗口关闭 / 退出 Yuhub 时停掉跨网房间。

        **必须同步写停止信号**——上一版用 daemon 线程调 stop()，主进程退出
        时会立刻杀掉 daemon 线程，"写停止信号"那一步可能都还没执行，看门狗
        收不到信号、easytier-core 就永久残留（下次打开 Yuhub 会看到
        "已有一个房间正在运行"，而且 UI 上停不掉它）。

        写信号是纯文件操作（微秒级），退出路径上一定跑得完。之后给看门狗
        最多 1.5 秒收尾；**收不掉也不影响**——看门狗是独立提权进程，
        而且它同时盯着主进程 PID，主进程一消失它照样会停掉 easytier-core。
        """
        tier, self._etier = self._etier, None
        self._running = False
        self._etier_busy = False
        if tier is not None:
            tier.request_stop()      # 同步写停止信号（瞬时、必达）
            tier.wait_stopped(1.5)   # 给看门狗一点时间；等不到也无妨
