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
import socket
import threading
import time

from PySide6.QtCore import Qt, QSettings, QTimer, Signal
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
from ..widgets import ghost_button, info_card, primary_button
from .base_page import BasePage


# 生成房间码用的字母表：去掉 0/O、1/I/L 这类肉眼易混的字符
_CODE_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def random_room_code():
    """生成 "XXX-XXX" 形式的可读房间码。"""
    picks = [secrets.choice(_CODE_CHARS) for _ in range(6)]
    return "%s-%s" % ("".join(picks[:3]), "".join(picks[3:]))


def default_nickname():
    """默认昵称 = 计算机名（拿不到就用 PLAYER）。"""
    try:
        name = socket.gethostname().strip()
    except Exception:
        name = ""
    return (name or "PLAYER")[:16]


def sanitize_nickname(text):
    """昵称只允许常见可见字符，避免 EasyTier 参数被注入或显示错乱。

    严禁空格（会成为命令行分隔符）与引号/反斜杠。
    """
    keep = []
    for ch in (text or ""):
        if ch.isalnum() or ch in "-_.\u4e00-\u9fff":
            keep.append(ch)
    out = "".join(keep)[:16].strip("-_.")
    return out



class LanPage(BasePage):
    TINT_KEY = "tile_2"
    BADGE = "异地联机"
    BADGE_STYLE = ""

    # 唯一的引导文案。以前这里有「你是房主 / 你是成员」两版动态文案，
    # 但 EasyTier 是纯 P2P、无中心服务器——所谓"房主"只是先启动的那个人，
    # 技术上与其他人完全等价。保留身份区分只会让人误以为房间有归属、
    # 有人能踢人。统一成一句话：填同一组房间码+密码，就在同一个房间里。
    _MODE_HINT = "填一组房间码 + 密码，点「进入房间」，你和队友就进了同一个虚拟局域网。"

    # 没填昵称时顶替 _MODE_HINT 的提示。抽成常量是为了让
    # `_refresh_start_gate` 能拿它当判据把文案换回来（见那里的注释）。
    _NICK_HINT = "先给自己起个昵称——队友在「在线成员」里看到的就是它。"

    # 引擎回调发生在后台线程，必须经信号排队回主线程再碰控件
    _event = Signal(str, object)

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "异地联机",
            "内嵌 EasyTier，把异地电脑拉进同一个虚拟局域网：大家填同一组房间码+密码即可互通，游戏里直接联机。",
            icon="🌐",
            notify=notify,
            parent=parent,
        )
        self._etier = None            # EasyTier 实例（跨网房间运行中）
        self._etier_busy = False      # 正在启动/停止跨网房间
        self._running = False         # 是否在运行（用于状态显示）
        self._my_ip = ""              # 本机虚拟 IP（运行中）
        self._tracker = None          # etier.MemberTracker：成员在线稳定视图
        self._members_row = []        # 当前渲染出来的成员 IP 顺序（用于增删行）
        self._host_nick = ""          # 本次启动用的昵称（成员列表第一行显示）
        self._started_wall = 0.0      # 本次进房的时间戳（time.time，给温和提示用）
        self._alone_hinted = False    # "房间里只有你"的温和提示只给一次
        self._event.connect(self._on_event)
        self._build_content()

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
        不在这里弹 UAC 打扰用户，留给点「进入房间」时走完整清理
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
                    "残留房间需管理员权限才能清掉，点「进入房间」时会自动处理",
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

        # 不再区分「房主 / 成员」：EasyTier 是纯 P2P、无中心服务器，
        # 所谓"房主"只是先启动的那个人，技术上和成员完全等价。
        # 保留这个区分只会带来两个负面后果：
        #   1. 用户以为"房主"是个特殊角色，进而以为房间归谁所有、谁能踢人；
        #   2. 让"房间码"看起来像房主生成的凭据，其实是双方自己约定的一组字符串。
        # 所以统一成一句话：**填同一组房间码 + 密码，就在同一个房间里**。

        # 昵称：队友在「在线成员」里看到的就是它（比 DESKTOP-XXXX 好认）
        rn = QHBoxLayout()
        rn.setSpacing(8)
        rn.addWidget(self._label("我的昵称"))
        self.nick_edit = QLineEdit()
        self._settings = QSettings("Yuhub", "Yuhub")
        saved_nick = str(self._settings.value("lan_nickname", "") or "").strip()
        self.nick_edit.setText(sanitize_nickname(saved_nick) or default_nickname())
        self.nick_edit.setPlaceholderText("队友看到的名字")
        self.nick_edit.setMaximumWidth(180)
        self.nick_edit.setMaxLength(16)
        rn.addWidget(self.nick_edit)
        btn_nick_reset = ghost_button("恢复默认")
        btn_nick_reset.clicked.connect(self._on_reset_nick)
        rn.addWidget(btn_nick_reset)
        rn.addStretch(1)
        v.addLayout(rn)
        # 昵称实时驱动启动按钮可用态（必须在 btn_start 建好之后再连，
        # 所以放到卡片最后统一接一次信号）

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

        # 唯一的引导文案（不再有身份之分，所以是静态的一句话）
        self.mode_note = self._note(
            "填一组房间码 + 密码，点「进入房间」，你和队友就进了同一个虚拟局域网。"
        )
        v.addWidget(self.mode_note)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.addWidget(self._label("房间密码"))
        self.pass_edit = QLineEdit("")
        self.pass_edit.setPlaceholderText("所有人必须填同一个")
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
            "约定一组房间码 + 密码（谁定都行，点「随机」可自动生成一个），"
            "发给大家，每个人填同一组、点「进入房间」，几台电脑就进了同一个"
            "虚拟局域网——游戏里的「局域网」列表能直接看到彼此开的房间"
            "（无需再填地址）。首次启动会弹一次 UAC 管理员确认"
            "（创建虚拟网卡需要），之后不再弹窗。"
            "\n"
            "昵称是必填项：队友在「在线成员」里看到的就是它，没填不能进入房间。"
            "\n"
            "虚拟 IP 由 EasyTier 自动分配（不固定），所以每次启动可能不一样；"
            "「在线成员」里每个人都带独立的「复制」按钮，直接复制即可，不用记。",
            warn=False
        ))



        brow = QHBoxLayout()
        brow.setSpacing(8)
        self.btn_stop = ghost_button("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        brow.addWidget(self.btn_stop)
        self.btn_start = primary_button(self._start_button_text())
        self.btn_start.clicked.connect(self._on_start)
        brow.addWidget(self.btn_start)
        v.addLayout(brow)

        # 昵称变化实时刷新启动按钮（btn_start 已建好，连接安全）
        self.nick_edit.textChanged.connect(self._refresh_start_gate)
        self._refresh_start_gate()

        return card


    def _build_members_card(self):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("在线成员")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.members_count = QLabel("0 人")
        self.members_count.setObjectName("Muted")
        self.members_count.setStyleSheet("font-size: 11px;")
        head.addWidget(self.members_count)
        v.addLayout(head)

        self.members_hint = QLabel("启动后这里会显示同一房间的成员昵称与虚拟 IP")
        self.members_hint.setObjectName("Faint")
        self.members_hint.setWordWrap(True)
        self.members_hint.setStyleSheet("font-size: 11px;")
        v.addWidget(self.members_hint)

        # 成员行容器：每行是「色块 + 昵称 + IP + 复制」。用容器而不是
        # QLabel 拼字符串，是因为**每个人的 IP 都要能单独复制**——
        # 游戏里通常只需要某一台机器的地址，整段文本复制下来还得手删。
        self.members_box = QWidget()
        self.members_layout = QVBoxLayout(self.members_box)
        self.members_layout.setContentsMargins(0, 0, 0, 0)
        self.members_layout.setSpacing(4)
        v.addWidget(self.members_box)
        return card

    def _make_member_row(self, name, ip, is_self=False):
        """构造一行成员：色块 + 昵称 + IP + 复制按钮。

        is_self 为真时高亮并标注"（我）"，方便用户一眼分清
        "哪个地址是我自己的"——填进游戏别填错了。
        """
        p = theme.current()
        row = QFrame()
        row.setObjectName("MemberRow")
        row.setStyleSheet(
            "QFrame#MemberRow { background: %s; border: 1px solid %s;"
            " border-radius: 5px; }" % (p["surface_sunken"], p["border"])
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 5, 6, 5)
        h.setSpacing(8)

        # 纯色小方块做视觉标识（极简色块风格），自己的用主色、别人的用灰
        dot = QFrame()
        dot.setFixedSize(9, 9)
        dot.setStyleSheet(
            "background: %s; border: none; border-radius: 2px;"
            % (p["accent"] if is_self else p["text_faint"])
        )
        h.addWidget(dot)

        who = QLabel("%s%s" % (name or "（未识别昵称）", "（我）" if is_self else ""))
        who.setStyleSheet(
            "font-size: 12px; font-weight: %s; color: %s; background: transparent;"
            " border: none;" % ("700" if is_self else "400",
                               p["text"] if is_self else p["text_dim"])
        )
        h.addWidget(who)

        ip_lb = QLabel(ip)
        ip_lb.setStyleSheet(
            "font-size: 12px; font-family: Consolas, 'Courier New', monospace;"
            " color: %s; background: transparent; border: none;" % p["text"]
        )
        h.addWidget(ip_lb)
        h.addStretch(1)

        btn = ghost_button("复制")
        btn.setFixedWidth(52)
        btn.clicked.connect(lambda _=False, v=ip: self._copy_text(v, "虚拟 IP"))
        h.addWidget(btn)
        return row

    def _render_members(self, entries):
        """按 entries 重建成员行。entries: [{"ip","name","self"}]。

        每次都整体重建而不是做差量更新：成员数量是个位数，
        重建 5 个小控件的开销可以忽略，换来的是"不会漏删/重影"。
        """
        while self.members_layout.count():
            item = self.members_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for e in entries:
            self.members_layout.addWidget(
                self._make_member_row(e.get("name", ""), e["ip"],
                                      is_self=bool(e.get("self")))
            )
        self.members_box.setVisible(bool(entries))

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
            "1. 先在游戏里点「对局域网开放」（谁开都行，开的那个人就是别人\n"
            "   进游戏时看到的那个房间）。\n"
            "2. 回到这里：点「随机」生成一组房间码，想一个密码，然后点\n"
            "   「进入房间」（首次弹一次 UAC，点「是」）。\n"
            "3. 把房间码和密码发给队友（建议走语音/私聊，别公开）。\n"
            "4. 队友填**同一组**房间码和密码，也点「进入房间」。\n"
            "   进入后会在 15 秒内自动校验：房间码或密码不对会明确报错并断开。\n"
            "5. 大家打开游戏的「多人游戏」，列表里就能直接看到彼此开的房间。\n"
            "\n"
            "所有人都是同一个身份：大家都做同一件事——填同一组房间码+密码、\n"
            "点「进入房间」。房间码不是谁「建」出来的，是一组约定好的暗号，\n"
            "谁先进入都行。\n"
            "\n"
            "昵称：填在「我的昵称」里，**必填**——队友在「在线成员」里\n"
            "看到的就是它。\n"
            "\n"
            "虚拟 IP：由 EasyTier 自动分配，不固定（换房间/重启都可能变）。\n"
            "要某台机器的地址时，在「在线成员」里点那一行的「复制」即可，\n"
            "每个人的 IP 都能单独复制。\n"
            "\n"
            "原理：内嵌 EasyTier（Apache 2.0 开源）创建一块二层虚拟网卡，\n"
            "把填了相同房间码+密码的电脑拉进同一个虚拟局域网。P2P 打洞优先，\n"
            "打洞不通时走社区共享节点中继兜底，无需自建服务器。\n"
            "已开启 UDP 广播中继，依赖局域网广播发现房间的游戏也能看到彼此。\n"
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
        """启动按钮文案。**不再有身份之分**，创建和加入是同一个动作。"""
        return "进入房间"

    def _on_reset_nick(self):
        """把昵称恢复为计算机名。"""
        self.nick_edit.setText(default_nickname())

    def _current_nickname(self):
        """取当前昵称（已清洗）。**空就是空**，不再回落默认值。

        昵称是进入房间的必需项：队友靠它在「在线成员」里认出你，
        没有名字的节点在列表里就是一行裸 IP。所以这里不偷偷兜底，
        由 _on_start() 明确拦下来并提示用户填。
        """
        return sanitize_nickname(self.nick_edit.text())

    def _save_nickname(self, nick):
        try:
            self._settings.setValue("lan_nickname", nick)
        except Exception:
            pass

    def _refresh_start_gate(self, *_args):
        """昵称是否填写决定启动按钮可用态。

        昵称是必填的：每个人都会出现在同一个「在线成员」列表里，
        谁都得有名字，否则列表里就是一行裸 IP，认不出谁是谁。

        只在"没在跑、也不忙"时改按钮：不能把「正在启动…」或「停止」
        途中的按钮状态覆盖掉。
        """
        if self._etier_busy or self._running:
            return
        has_nick = bool(self._current_nickname())
        self.btn_start.setEnabled(has_nick)
        if has_nick:
            self.btn_start.setText(self._start_button_text())
            # 用门禁自己写进去的那段提示做判据。
            # 曾经的 bug：这里判的是按钮文案「请先填昵称」，但门禁往
            # mode_note 里写的是「先给自己起个昵称…」，两者永远不相等，
            # 于是提示一旦被改写就再也回不到正常引导文案。
            if self.mode_note.text() == self._NICK_HINT:
                self.mode_note.setText(self._MODE_HINT)
        else:
            self.btn_start.setText("请先填昵称")
            self.mode_note.setText(self._NICK_HINT)

    def _on_start(self):
        if self._etier_busy or self._running:
            return
        code = self.code_edit.text().strip()
        password = self.pass_edit.text()
        # 昵称必填：每个人都要能被人认出来
        nick = self._current_nickname()
        if not nick:
            self.toast("请先填写昵称，再进入房间")
            self._refresh_start_gate()
            return
        if len(code) < 4:
            self.toast("房间码至少 4 个字符（可点「随机」自动生成）")
            return
        if len(password) < 4:
            self.toast("房间密码至少 4 个字符（所有人填同一个）")
            return
        self._etier_busy = True
        self.btn_start.setEnabled(False)
        self.btn_start.setText("正在启动…")
        self._set_status_note(
            "正在进入房间：释放内置组件 → 等待 UAC 授权 → 拉起虚拟网卡。"
            "首次启动可能要 10~20 秒，请在 UAC 弹窗里点「是」。"
        )
        self._append_log("跨网房间：正在进入（房间码 %s）…" % code)
        # 昵称落盘，下次打开还是它
        self.nick_edit.setText(nick)          # 回填清洗后的结果，让用户看到实际值
        self._save_nickname(nick)
        self._host_nick = nick                # 校验回调（后台线程）要用
        # EasyTier.start 会阻塞最长 45 秒（轮询虚拟网卡就绪），必须丢后台
        threading.Thread(target=self._do_start, args=(code, password, nick),
                         daemon=True).start()

    def _do_start(self, code, password, nick):
        tier = etier.EasyTier(on_log=lambda m: self._event.emit("_etier_log", m))
        # 虚拟 IP 交给 EasyTier 的 DHCP 动态分配（不传 ipv4，走 -d true）：
        # 固定 IP 在"多个房间并存 / 一个房间多人"时会互相抢地址，而 DHCP
        # 遇到冲突会自动改地址。代价是 IP 不再可预测，所以成员侧的准入校验
        # 不能再 ping 固定的 10.126.126.1，改由 pick_host_ip() 动态发现。
        try:
            ok, msg = tier.start(
                "yuhub-" + code.lower().replace(" ", ""), password,
                timeout=20.0, hostname=nick,
            )
        except Exception as exc:
            ok, msg = False, str(exc)
        self._event.emit("_etier_done", {"ok": ok, "msg": msg, "tier": tier,
                                          "ipv4": etier.virtual_adapter_ip()})

    def _on_done(self, payload):
        self._etier_busy = False
        tier = payload.get("tier")
        if payload.get("ok") and tier is not None:
            self._etier = tier
            self._running = True
            self._my_ip = payload.get("ipv4", "") or etier.virtual_adapter_ip()
            self._set_running(True)
            self._refresh_status_style()
            self._started_wall = time.time()
            self._alone_hinted = False
            self._update_ip()

            # 不做"15 秒验证"了（v0.8.2beta）：验证的本意是拦下"密码填错
            # 进了空房间"，但 peer 发现本身依赖 ARP/打洞，偶发地找不到人
            # 就把人踢出房间，队友经常"加入不进来"。现在改为**不阻塞**：
            # 直接进入房间，若 30 秒后成员列表仍然只有自己，给一条温和的
            # 提示（见 _update_ip），由用户自己判断是不是码/密码填错了。
            #
            # 成员的稳定在线视图交给 MemberTracker（后台 ping 保活 + 宽限
            # 判定），它同时把名称解析也挪出了 UI 线程。
            self._stop_tracker()
            self._tracker = etier.MemberTracker(
                self._my_ip,
                on_log=lambda m: self._event.emit("_etier_log", m),
            )
            self._tracker.start()
            self._append_log("已进入房间（虚拟 IP %s）" % self._my_ip)
            self.toast("已进入房间")
        else:
            self._running = False
            self._my_ip = ""
            self._stop_tracker()
            self._refresh_start_gate()
            self._set_status_note("")
            msg = payload.get("msg") or "未知原因"
            self._append_log("跨网房间启动失败：%s" % msg)
            if any(w in msg for w in ("资源缺失", "缺少", "被占用", "大小异常")):
                # 组件损坏/被杀毒软件删除：给出可操作的出路
                msg = (msg + "。组件疑似缺失或损坏，可在「设置中心 → "
                            "异地联机组件」里点「一键修复」重新安装")
            self.toast("启动失败：%s" % msg)

    def _stop_tracker(self):
        """停掉成员跟踪线程（重复调用安全）。"""
        if self._tracker is not None:
            try:
                self._tracker.stop()
            except Exception:
                pass
            self._tracker = None

    def _on_stop(self):
        if self._etier_busy:
            return          # 已有启动/停止在进行，避免重复线程
        if self._etier is None and not self._running:
            # 没有可停的房间：把界面归位即可（别留下"正在停止"的死状态）
            # 注意别在这里无条件 setEnabled(True)——昵称为空时按钮该保持禁用，
            # 交给 gate 统一决定。
            self._refresh_start_gate()
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
        self._host_nick = ""
        self._stop_tracker()
        self._set_running(False)
        self._set_status_note("")
        self._clear_members()
        self._append_log("跨网房间已停止")

    def _set_running(self, flag):
        self.btn_stop.setEnabled(flag)
        # 无论启动成功还是失败，启动按钮文案都必须回到正常值。
        # 曾经的 bug：这里只在 `not flag` 时复位文案，于是**启动成功后**
        # 按钮永远停在「正在启动…」——用户看到的就是"进入房间后一直
        # 显示正在启动"（房间其实已经连上了，只是按钮没恢复）。
        #
        # 运行中：禁用 + 文案复位（恢复文案由本函数负责，不能
        # 交给 gate —— gate 会因为 _running 为真而早退，那样按钮就永远
        # 停在「正在启动…」）。
        # 未运行：整个交给 gate —— 它还要看昵称填没填。
        if flag:
            self.btn_start.setEnabled(False)
            self.btn_start.setText(self._start_button_text())
        else:
            self._refresh_start_gate()
        for w in (self.code_edit, self.pass_edit, self.btn_random):
            w.setEnabled(not flag)
        # IP 显示与复制按钮
        self.btn_copy_ip.setEnabled(flag and bool(self._my_ip))
        if not flag:
            self.ip_label.setText("--")
            self._clear_members()

    def _clear_members(self):
        """清空成员列表显示（回到"未启动"的占位状态）。"""
        self._render_members([])
        self.members_hint.setText("启动后这里会显示同一房间的成员昵称与虚拟 IP")
        self.members_hint.setVisible(True)
        self.members_count.setText("0 人")
        self._members_row = []

    def _set_status_note(self, text):
        try:
            self.status_note.setText(text or "")
        except RuntimeError:
            pass

    def _copy_text(self, text, what="内容"):
        """复制任意文本到剪贴板（成员行 / 本机 IP 共用）。"""
        if not text:
            self.toast("没有可复制的%s" % what)
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self.toast("已复制%s：%s" % (what, text))

    def _on_copy_ip(self):
        """复制本机虚拟 IP 到剪贴板。"""
        ip = self._my_ip or etier.virtual_adapter_ip()
        if not ip:
            self.toast("当前没有虚拟 IP 可复制")
            return
        self._copy_text(ip, "本机虚拟 IP")
        self._append_log("已复制本机虚拟 IP %s" % ip)

    def _update_ip(self):
        """轮询虚拟网卡状态，刷新跨网房间状态行、本机 IP 与成员列表。

        成员列表来自 MemberTracker 的稳定快照（后台线程负责 ARP 扫描、
        ping 保活与名称解析），UI 线程在这里**不起任何子进程**——
        800ms 一次的 tick 再也不会被 arp/gethostbyaddr 拖出卡顿。
        """
        ip = etier.virtual_adapter_ip()
        if ip:
            self._my_ip = ip
            self.ip_label.setText(ip)
            self.btn_copy_ip.setEnabled(True)
            members = self._tracker.snapshot() if self._tracker else []

            # 自己永远排第一行——用户最常要复制的是自己的地址
            entries = [{"ip": ip, "name": self._host_nick or self._current_nickname(),
                        "self": True}]
            for m in members:
                entries.append({
                    "ip": m["ip"],
                    "name": m.get("name") or "",
                    "self": False,
                })
            all_ips = [e["ip"] for e in entries]
            # 只在"行内容真的变了"时重建控件，否则每 800ms 重建一次会闪
            if all_ips != [r["ip"] for r in self._members_row]:
                self._render_members(entries)
                self._members_row = entries

            others = len(entries) - 1
            self.members_count.setText("%d 人" % len(entries))
            # 状态栏文案：有队友就显示常规文案；一直只有自己且超过 30 秒，
            # 才给一次温和提示（取代被移除的"15 秒验证"，见 _on_done 注释）
            normal_note = (
                "虚拟网卡已就绪：本机虚拟 IP %s。把房间码和密码发给队友，"
                "让他们也启动跨网房间即可。" % ip
            )
            if others:
                self.members_hint.setText(
                    "点每行右侧「复制」即可单独复制那个人的 IP。"
                )
                self._set_status_note(normal_note)
            else:
                self.members_hint.setText(
                    "目前只有你自己。把房间码和密码发给队友，"
                    "他们加入后就会出现在这里。"
                )
                if (self._running and not self._alone_hinted
                        and time.time() - self._started_wall > 30.0):
                    self._alone_hinted = True
                    self._set_status_note(
                        "已进入房间 30 秒，成员列表里仍然只有你自己。"
                        "如果队友确实已经进入，请核对面前的房间码和密码"
                        "是否与大家完全一致。"
                    )
                    self._append_log("提示：房间内暂时没有其他成员（可能是"
                                     "队友未进入，也可能是房间码/密码不一致）")
                elif not self._alone_hinted:
                    self._set_status_note(normal_note)
            self.members_hint.setVisible(True)
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
