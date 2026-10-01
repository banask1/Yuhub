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

import os
import secrets
import socket
import subprocess
import threading
import time

from PySide6.QtCore import Qt, QSettings, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import etier
import lan_share
import node_probe
from .. import theme
from ..widgets import ghost_button, info_card, primary_button
from .base_page import BasePage


# 生成房间码用的字母表：去掉 0/O、1/I/L 这类肉眼易混的字符
_CODE_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

# 游戏快连（参考 MCTier 的同名功能）：常见联机游戏的**专用服务器/直连
# 端口**预设，运行中一键拼出「虚拟 IP:端口」复制给队友。
# 注意 Minecraft「对局域网开放」的端口是随机的（看游戏聊天栏提示），
# 25565 是它开专用服务器(dedicated server)的默认端口，所以保留在列表里。
# 神力科莎：acServer 默认 9600（TCP+UDP，赛车主数据）——局域网里可直接
#           用（在线列表的 LAN 标签页会自己发现，或手动填 IP:9600）。
# 以撒的结合：⚠️ 官方明确不支持 LAN/局域网联机（只有同屏合作 + Steam 在线
#           联机/远程同乐串流），虚拟局域网里没有可填的 IP:端口，列出来只为
#           提醒，别让队友白找地址。
GAME_PORTS = (
    ("Minecraft 服务器", 25565),
    ("泰拉瑞亚", 7777),
    ("幻兽帕鲁", 8211),
    ("神力科莎", 9600),
    ("饥荒联机版", 10999),
    ("CS 2 / 起源引擎", 27015),
    ("以撒的结合（无局域网联机）", 27015),
    ("七日杀", 26900),
)


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


# 单次拖拽/选择允许的文件数上限：只是防手滑拖进一整个下载目录，
# 不是技术限制（HTTP 服务对文件数没意见）。
MAX_DROP_FILES = 50


class _DropZone(QFrame):
    """临时云盘的文件投放区：拖文件（或文件夹）进来即分享。

    做成独立控件而不是给卡片挂拖拽事件：拖拽事件要落在"一块明确的区域"
    上，而且得能自己高亮——悬停时把边框换成主色，用户才知道松手会落到
    这里。用 QFrame 是因为要同时控制边框样式和子标签文字。
    """

    dropped = Signal(list)      # 本地绝对路径列表
    clicked = Signal()          # 点击 = 打开文件选择框

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(70)
        self._active = False

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(3)
        self.title = QLabel("拖文件 / 文件夹到这里分享")
        self.title.setAlignment(Qt.AlignCenter)
        self.sub = QLabel("也可以点这里选文件")
        self.sub.setAlignment(Qt.AlignCenter)
        self.sub.setWordWrap(True)
        v.addWidget(self.title)
        v.addWidget(self.sub)
        self.restyle()

    def restyle(self, *_):
        """跟随主题重画（拖拽高亮也走这里）。"""
        p = theme.current()
        line = p["accent"] if self._active else p["border_strong"]
        bg = p["accent_soft"] if self._active else p["surface_sunken"]
        self.setStyleSheet(
            "QFrame { background: %s; border: 1px dashed %s;"
            " border-radius: 6px; }"
            "QLabel { background: transparent; border: none; }" % (bg, line))
        self.title.setStyleSheet(
            "font-size: 12px; font-weight: 700; color: %s;"
            % (p["accent"] if self._active else p["text"]))
        self.sub.setStyleSheet("font-size: 11px; color: %s;" % p["text_faint"])

    # ------------------------------------------------------------ 拖拽
    def _set_active(self, flag):
        if flag != self._active:
            self._active = flag
            self.restyle()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        # dragMoveEvent 也必须 accept，否则部分平台上 drop 不会触发
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_active(False)

    def dropEvent(self, event):
        self._set_active(False)
        paths = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        if paths:
            self.dropped.emit(paths)
        event.acceptProposedAction()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


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
        self._beacon = None           # etier.NickBeacon：昵称信标（广播+TCP）
        self._members_row = []        # 当前渲染出来的成员 IP 顺序（用于增删行）
        self._host_nick = ""          # 本次启动用的昵称（成员列表第一行显示）
        self._started_wall = 0.0      # 本次进房的时间戳（time.time，给温和提示用）
        self._alone_hinted = False    # "房间里只有你"的温和提示只给一次
        self._multi_ip_hinted = False # "检测到多个虚拟网卡地址"只提示一次
        # 临时云盘（lan_share.ShareHub）：进房才创建，退房即销毁
        self._hub = None
        self._share_rows = []         # 当前渲染出来的文件行签名（避免每轮重建）
        self._share_entries = []      # 当前渲染的条目（与 _share_rows 一一对应）
        self._share_widgets = {}      # key -> {"prog","btn"}：进度只改这两个控件
        self._share_remote = {}       # ip -> [文件]（上一次轮询拉回来的）
        self._share_peer_ports = {}   # ip -> 对方云盘端口
        self._share_polling = False   # 上一轮清单还没拉完，别叠加
        self._share_peers = {}        # ip -> 昵称（渲染来源用）
        self._download = None         # {"key","name"} 正在下载的那一项
        self._cancel_dl = False       # 下载取消标志（后台线程读）
        # 节点测速（方案 A）：结果带时间戳存这里，过期就重测、绝不复用旧数字
        self._probe = node_probe.NodeProbeCache()
        self._probing = False         # 后台测速进行中（别叠加第二轮）
        self._probe_stop = False      # 页面销毁时让测速线程尽快收手
        self._node_age_text = ""      # 上一次写进「延迟数据：xx前」的文案
        self._saved_node = "auto"     # 所选中继节点的 key（重建下拉要用）
        self._event.connect(self._on_event)
        self._build_content()
        self._update_node_age()       # 首屏就把「延迟数据」一栏写上字

        self._timer = QTimer(self)
        self._timer.setInterval(800)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        # 临时云盘：清单轮询比成员列表慢得多（每轮要对每个成员发一次
        # HTTP），单独用 3 秒的定时器，别塞进 800ms 的 _tick 里。
        self._share_timer = QTimer(self)
        self._share_timer.setInterval(3000)
        self._share_timer.timeout.connect(self._poll_shares)
        self._share_timer.start()

        theme.bus.changed.connect(self._refresh_status_style)
        theme.bus.changed.connect(self._restyle_share)

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
        self.add(self._build_share_card())
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

        # 中继节点（参考 MCTier 的节点选择）：自动 = 全部内置节点由
        # EasyTier 择优；手动 = 只连所选节点（确定性，好排查）。
        #
        # v0.8.12beta（方案 A）：每个节点的**实测延迟**直接写进下拉项，
        # 并按延迟从低到高排。「自动」不再是盲选——它后面跟着当前最快的
        # 那个节点，用户一眼就知道"自动会优先打到哪、大概多少毫秒"。
        # 延迟是**本机实测**的：同一个节点在国内是 109ms、在国外可能
        # 266ms，谁快取决于你在哪，程序写不死，只能测。
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        r3.addWidget(self._label("中继节点"))
        self.node_combo = QComboBox()
        self._saved_node = str(self._settings.value("lan_node", "auto") or "auto")
        self._fill_node_combo()
        self.node_combo.setMaximumWidth(280)
        self.node_combo.currentIndexChanged.connect(self._on_node_changed)
        r3.addWidget(self.node_combo)

        self.btn_node_speed = QPushButton("测速")
        self.btn_node_speed.setObjectName("MiniButton")
        self.btn_node_speed.setCursor(Qt.PointingHandCursor)
        self.btn_node_speed.setFixedHeight(26)
        self.btn_node_speed.setToolTip(
            "并发测一遍所有内置节点的延迟，按快的排前面。\n"
            "打开本页会自带一次，之后每 90 秒自动重测——\n"
            "下拉里的数字永远是刚测的，不会拿旧的糊弄你。")
        self.btn_node_speed.clicked.connect(lambda: self._start_node_probe(force=True))
        r3.addWidget(self.btn_node_speed)

        self.node_age_label = self._note("")
        self.node_age_label.setStyleSheet("font-size: 11px;")
        r3.addWidget(self.node_age_label)
        r3.addStretch(1)
        v.addLayout(r3)

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

        # 游戏快连（参考 MCTier）：常见联机游戏端口预设，
        # 运行中一键拼出「虚拟 IP:端口」复制，省得现查端口表。
        qk = QHBoxLayout()
        qk.setSpacing(8)
        qk.addWidget(self._label("游戏快连"))
        self.game_combo = QComboBox()
        self.game_combo.setMaximumWidth(220)
        for i, (name, port) in enumerate(GAME_PORTS):
            self.game_combo.addItem("%s（%d）" % (name, port), port)
            # 纯游戏名单独存一份（纯展示用：成员行里的贴片 + 广播给队友）
            self.game_combo.setItemData(i, name, Qt.UserRole + 1)
        self.game_combo.setToolTip(
            "选你这次要玩的游戏：会广播给队友（成员行显示贴片），\n"
            "每行的「复制」也给那个人的「IP:端口」。进房间后也能改。")
        qk.addWidget(self.game_combo)
        self.btn_copy_addr = ghost_button("复制 IP:端口")
        self.btn_copy_addr.setEnabled(False)
        self.btn_copy_addr.clicked.connect(self._on_copy_game_addr)
        qk.addWidget(self.btn_copy_addr)
        qk.addStretch(1)
        v.addLayout(qk)

        # 放在最后连：addItem 填充期间也会发 currentIndexChanged，
        # 那时 _beacon / 成员行还没建起来，提前连会打到空对象上。
        self.game_combo.currentIndexChanged.connect(self._on_game_changed)

        v.addWidget(self._note(
            "约定一组房间码 + 密码发给大家，各自填好点「进入房间」，"
            "几台电脑就进了同一个虚拟局域网（首次弹一次 UAC，之后不再弹）。"
            "\n"
            "昵称必填，队友靠它在「在线成员」里认你。虚拟 IP 不固定，"
            "变化时这里会提示；也可随时点「刷新」重读本机 IP 并重扫成员，"
            "不用「停止 → 再进入」。",
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
        # 手动刷新：IP 突然变了 / 列表看着不对时，不用"停止再进入"——
        # 点一下立刻重读虚拟 IP、重扫成员，并把变化写进日志。
        self.btn_members_refresh = QPushButton("刷新")
        self.btn_members_refresh.setObjectName("MiniButton")
        self.btn_members_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_members_refresh.setFixedHeight(26)
        self.btn_members_refresh.setToolTip(
            "立刻重读本机虚拟 IP、重新扫描房间成员。\n"
            "中途卡死重进房间后 IP 变了、或列表看着不对时点这里。")
        self.btn_members_refresh.clicked.connect(self._on_refresh)
        head.addWidget(self.btn_members_refresh)
        v.addLayout(head)

        self.members_hint = QLabel("启动后这里会显示同一房间的成员昵称、虚拟 IP"
                                   "与各自的「游戏快连」")
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

    def _make_member_row(self, name, ip, is_self=False, game="", port=0):
        """构造一行成员：色块 + 昵称 + IP + 游戏快连贴片 + 复制按钮。

        is_self 为真时高亮并标注"（我）"，方便用户一眼分清
        "哪个地址是我自己的"——填进游戏别填错了。

        game / port 是**这个人**在「游戏快连」里选的那款游戏（自己就是
        当前下拉值，队友由昵称信标广播过来）。显示它是为了省掉"你玩的
        是哪个、端口多少"的来回问；点「复制」时也用他的端口拼地址，
        不再受我自己那个下拉的影响。
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

        # 游戏快连贴片：他对战/联机用的是哪款游戏。没广播过来（旧版本
        # 队友、或信标还没到）就不显示，宁缺勿假。
        if game:
            tag = QLabel(game)
            tag.setStyleSheet(
                "font-size: 11px; color: %s; background: transparent;"
                " border: 1px solid %s; border-radius: 4px; padding: 1px 6px;"
                % (p["text_dim"], p["border_strong"])
            )
            tag.setToolTip(
                "%s 的游戏快连：%s%s" % (
                    "我" if is_self else (name or ip),
                    game,
                    "（端口 %d）" % port if port else ""))
            h.addWidget(tag)

        h.addStretch(1)

        # 「复制」用标准 mini 尺寸：以前用 ghost_button + setFixedWidth(52)，
        # 而 ghost 样式自带 18px 左右内边距，52 宽留给文字的只有十几像素，
        # 两个字被挤成"显示不清楚"。MiniButton 的内边距是 2px 12px，够用。
        btn = QPushButton("复制")
        btn.setObjectName("MiniButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(26)
        btn.setMinimumWidth(56)
        # 复制「IP:端口」而不是裸 IP：游戏里要填的就是带端口的直连地址
        # （「IP:端口」缺一不可）。端口优先用**这个人的**游戏快连；
        # 他还没广播过来时退回我自己选的那个，至少能用。
        use_port = port or (self.game_combo.currentData() or 0)
        addr = "%s:%s" % (ip, use_port) if use_port else ip
        btn.setToolTip("复制直连地址 %s" % addr)
        btn.clicked.connect(lambda _=False, v=addr: self._copy_text(v, "直连地址"))
        h.addWidget(btn)
        return row

    def _render_members(self, entries):
        """按 entries 重建成员行。

        entries: [{"ip","name","self","game","port"}]。每次都整体重建而不是
        做差量更新：成员数量是个位数，重建几个小控件的开销可以忽略，
        换来的是"不会漏删/重影"。
        """
        while self.members_layout.count():
            item = self.members_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                # 先 hide 再 deleteLater：takeAt 只是把控件移出布局，
                # 它仍然是 members_box 的子控件、仍然"可见"，要等下一轮
                # 事件循环处理 DeferredDelete 才真正消失。中间那一帧
                # 新旧行会叠在一起（就是"重影"）。hide 是立即生效的。
                w.hide()
                w.deleteLater()
        for e in entries:
            self.members_layout.addWidget(
                self._make_member_row(e.get("name", ""), e["ip"],
                                      is_self=bool(e.get("self")),
                                      game=e.get("game", ""),
                                      port=int(e.get("port") or 0))
            )
        self.members_box.setVisible(bool(entries))

    # ------------------------------------------------------------------ 云盘
    def _build_share_card(self):
        """临时云盘：拖文件进来，同房间的人都能下载；退出房间立即失效。

        实现思路抄 MCTier 的「文件夹共享」——每个客户端在自己那块虚拟
        网卡上起 HTTP 服务，成员之间互相拉清单、拉文件。没有中心服务器，
        谁是分享者文件就在谁那儿；退出房间时虚拟网卡一断、服务一停，
        谁也就下不到了。
        """
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("临时云盘")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.share_count = QLabel("0 个文件")
        self.share_count.setObjectName("Muted")
        self.share_count.setStyleSheet("font-size: 11px;")
        head.addWidget(self.share_count)
        self.btn_share_dir = QPushButton("打开文件夹")
        self.btn_share_dir.setObjectName("MiniButton")
        self.btn_share_dir.setCursor(Qt.PointingHandCursor)
        self.btn_share_dir.setFixedHeight(26)
        self.btn_share_dir.setToolTip(
            "打开下载文件的保存位置：\n" + lan_share.download_dir())
        self.btn_share_dir.clicked.connect(self._on_open_share_dir)
        head.addWidget(self.btn_share_dir)
        v.addLayout(head)

        self.drop_zone = _DropZone()
        self.drop_zone.dropped.connect(self._on_share_paths)
        self.drop_zone.clicked.connect(self._on_share_pick)
        self.drop_zone.setEnabled(False)
        v.addWidget(self.drop_zone)

        self.share_hint = QLabel(
            "进入房间后可用：拖进来的文件，队友在「在线成员」里就能看到并下载。\n"
            "文件不会上传到任何服务器——它在你的电脑上，队友直接从你这里取。\n"
            "退出房间（或点「停止」）服务立即关闭，之后谁都下不到。"
        )
        self.share_hint.setObjectName("Faint")
        self.share_hint.setWordWrap(True)
        self.share_hint.setStyleSheet("font-size: 11px;")
        v.addWidget(self.share_hint)

        self.share_box = QWidget()
        self.share_layout = QVBoxLayout(self.share_box)
        self.share_layout.setContentsMargins(0, 0, 0, 0)
        self.share_layout.setSpacing(4)
        self.share_box.setVisible(False)
        v.addWidget(self.share_box)
        return card

    def _make_share_row(self, entry):
        """一行文件：色块 + 文件名 + 大小 + 来源 + 动作按钮。

        自己的行给「移除」，别人的行给「下载」。下载中那一行的按钮变成
        「取消」，旁边多一个进度文本——同一个按钮承担两件事，省得为了
        取消再挤一个控件进去。
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

        dot = QFrame()
        dot.setFixedSize(9, 9)
        dot.setStyleSheet(
            "background: %s; border: none; border-radius: 2px;"
            % (p["accent"] if entry["mine"] else p["text_faint"]))
        h.addWidget(dot)

        name = QLabel(entry["name"])
        name.setToolTip(entry["name"])
        name.setStyleSheet(
            "font-size: 12px; font-weight: %s; color: %s; background: transparent;"
            " border: none;" % ("700" if entry["mine"] else "400",
                               p["text"] if entry["mine"] else p["text_dim"]))
        h.addWidget(name, 1)

        prog = QLabel("")
        prog.setStyleSheet(
            "font-size: 11px; color: %s; background: transparent; border: none;"
            % p["accent"])
        prog.setVisible(False)
        h.addWidget(prog)

        tag = QLabel(entry["owner"])
        tag.setStyleSheet(
            "font-size: 11px; color: %s; background: transparent;"
            " border: 1px solid %s; border-radius: 4px; padding: 1px 6px;"
            % (p["text_dim"], p["border_strong"]))
        h.addWidget(tag)

        size = QLabel(lan_share.human_size(entry["size"]))
        size.setStyleSheet(
            "font-size: 11px; color: %s; background: transparent; border: none;"
            % p["text_faint"])
        h.addWidget(size)

        busy = bool(self._download and self._download.get("key") == entry["key"])
        btn = QPushButton("取消" if busy else ("移除" if entry["mine"] else "下载"))
        btn.setObjectName("MiniButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(26)
        btn.setMinimumWidth(56)
        btn.clicked.connect(lambda _=False, e=entry: self._on_share_action(e))
        h.addWidget(btn)

        self._share_widgets[entry["key"]] = {"prog": prog, "btn": btn}
        return row

    def _refresh_share_view(self):
        """把「我分享的」和「各成员分享的」合成一份列表画出来。

        自己的排前面：拖进来立刻要能看到，否则用户不知道有没有生效。
        """
        entries = []
        if self._hub is not None and self._hub.running:
            for f in self._hub.own_files():
                entries.append({"key": "me:" + f["id"], "fid": f["id"],
                                "name": f["name"], "size": f["size"],
                                "owner": "我", "mine": True, "ip": "", "port": 0})
        for ip, files in (self._share_remote or {}).items():
            owner = (self._share_peers or {}).get(ip) or ip
            port = (self._share_peer_ports or {}).get(ip, 0)
            for f in files:
                entries.append({"key": "%s:%s" % (ip, f["id"]), "fid": f["id"],
                                "name": f["name"], "size": f["size"],
                                "owner": owner, "mine": False,
                                "ip": ip, "port": port})
        sig = [(e["key"], e["name"], e["size"], e["owner"]) for e in entries]
        self._share_entries = entries
        if sig != self._share_rows:
            self._render_share(entries)
            self._share_rows = sig
        else:
            # 结构没变也要把按钮文案拉回正确状态（下载结束/取消后）
            for e in entries:
                w = self._share_widgets.get(e["key"])
                if w:
                    busy = bool(self._download
                                and self._download.get("key") == e["key"])
                    w["btn"].setText(
                        "取消" if busy else ("移除" if e["mine"] else "下载"))
                    if not busy:
                        w["prog"].setVisible(False)
        self.share_count.setText("%d 个文件" % len(entries))
        if self._running and self._hub is not None and self._hub.running:
            others = sum(len(f) for f in (self._share_remote or {}).values())
            self.share_hint.setText(
                "云盘已开启：拖文件进来，队友就能下载。已看到队友分享的 %d 个文件。\n"
                "退出房间后服务立即关闭，之后谁都下不到。" % others)

    def _render_share(self, entries):
        while self.share_layout.count():
            item = self.share_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                # 先 hide 再 deleteLater：takeAt 只把控件移出布局，它仍然
                # 可见，要等下一轮事件循环才真正销毁——中间那一帧新旧行
                # 会叠成"重影"（和成员列表同一个坑）。
                w.hide()
                w.deleteLater()
        self._share_widgets = {}
        for e in entries:
            self.share_layout.addWidget(self._make_share_row(e))
        self.share_box.setVisible(bool(entries))

    # ------------------------------------------------------------ 云盘：交互
    def _on_open_share_dir(self):
        """打开下载目录（没下载过也要能打开，方便用户自己放文件进去看）。"""
        path = lan_share.download_dir()
        try:
            os.makedirs(path, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception as exc:
            self.toast("打不开文件夹：%s" % exc)

    def _on_share_pick(self):
        """点击拖拽区 = 打开文件选择框。"""
        if not (self._running and self._hub is not None):
            self.toast("先点「进入房间」，云盘才会开启")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择要分享给房间成员的文件", "", "所有文件 (*)")
        if paths:
            self._on_share_paths(paths)

    def _on_share_paths(self, paths):
        """拖入/选中的路径：文件直接收，文件夹展开一层。"""
        if not self._running or self._hub is None:
            self.toast("先点「进入房间」，云盘才会开启")
            return
        files = []
        for raw in list(paths)[:MAX_DROP_FILES]:
            if os.path.isdir(raw):
                try:
                    for nm in sorted(os.listdir(raw)):
                        sub = os.path.join(raw, nm)
                        if os.path.isfile(sub):
                            files.append(sub)
                except OSError as exc:
                    self._append_log("读取文件夹失败：%s（%s）" % (raw, exc))
            else:
                files.append(raw)
        if not files:
            self.toast("没有可分享的文件")
            return
        added, skipped = self._hub.add_paths(files[:MAX_DROP_FILES])
        for s in skipped:
            self._append_log("未分享：%s" % s)
        if added:
            names = "、".join(e["name"] for e in added[:3])
            more = "" if len(added) <= 3 else " 等 %d 个" % len(added)
            self.toast("已分享 %d 个文件" % len(added))
            self._append_log("已分享 %d 个文件：%s%s" % (len(added), names, more))
        elif skipped:
            self.toast("没有新增文件：%s" % skipped[0])
        self._publish_share_port()
        self._refresh_share_view()

    def _on_share_action(self, entry):
        """行内按钮：自己的「移除」，别人的「下载」/下载中的「取消」。"""
        if self._hub is None or not self._hub.running:
            return
        if entry["mine"]:
            if self._download and self._download.get("key") == entry["key"]:
                self._cancel_dl = True          # 正在下自己的文件？先取消
                return
            self._hub.remove(entry["fid"])
            self._append_log("已取消分享：%s" % entry["name"])
            self._publish_share_port()
            self._refresh_share_view()
            return

        if self._download is not None:
            if self._download.get("key") == entry["key"]:
                self._cancel_dl = True
                self.toast("正在取消…")
            else:
                self.toast("已有一个下载在进行，先等它结束或点它那行的「取消」")
            return

        if not entry["port"]:
            self.toast("对方未开启云盘（可能是旧版本）")
            return
        self._start_download(entry)

    def _start_download(self, entry):
        hub = self._hub
        if hub is None:
            return
        key = entry["key"]
        self._download = {"key": key, "name": entry["name"]}
        self._cancel_dl = False
        self._append_log("开始下载 %s（来自 %s）…" % (entry["name"], entry["owner"]))
        last = [0.0]
        ip, port, fid = entry["ip"], entry["port"], entry["fid"]

        def prog(done, total):
            now = time.monotonic()
            if done != total and now - last[0] < 0.25:
                return          # 节流：每个 64KB 分块都发信号会把事件队列灌满
            last[0] = now
            self._event.emit("share_prog", {"key": key, "done": done, "total": total})

        def work():
            ok, res = hub.download(ip, port, fid, progress=prog,
                                   is_cancelled=lambda: self._cancel_dl,
                                   timeout=20.0)
            self._event.emit("share_done",
                             {"key": key, "ok": ok, "result": res,
                              "name": entry["name"]})

        threading.Thread(target=work, daemon=True, name="ShareDownload").start()
        self._refresh_share_view()

    def _publish_share_port(self):
        """把云盘端口写进昵称信标，队友的成员列表下一轮就知道我开着云盘。"""
        if self._beacon is None:
            return
        port = self._hub.port if (self._hub is not None and self._hub.running) else 0
        try:
            self._beacon.set_share(port)
        except Exception:
            pass

    def _ensure_share_hub(self, ip):
        """按当前虚拟 IP 起（或重绑）云盘服务。

        和「信标 + 成员跟踪」同一生命周期：进房起、退房停、IP 变了重绑。
        token 用房间码 + 密码派生——只有同房间的人算得出同一个值。
        """
        token = lan_share.make_token(self.code_edit.text().strip(),
                                     self.pass_edit.text())
        nick = self._host_nick or self._current_nickname()
        if self._hub is None:
            self._hub = lan_share.ShareHub(
                token, nick=nick,
                on_log=lambda m: self._event.emit("_etier_log", m))
            ok, msg = self._hub.start(ip)
        else:
            self._hub.set_nick(nick)
            ok, msg = self._hub.rebind(ip)
        if not ok:
            self._append_log("临时云盘开启失败：%s" % msg)
        self._publish_share_port()
        self._refresh_share_view()

    def _stop_share_hub(self):
        """关掉云盘服务并清空清单。

        「退出房间后无法下载」就落在这里：服务一停，队友的连接直接被拒。
        下载到本地的文件**不删**——用户已经拿到手的东西没有理由替他清掉。
        """
        hub, self._hub = self._hub, None
        self._cancel_dl = True          # 正在跑的下载线程据此尽快收尾
        self._download = None
        self._share_remote = {}
        self._share_peer_ports = {}
        self._share_peers = {}
        if hub is not None:
            try:
                hub.stop()
            except Exception:
                pass
        if self._beacon is not None:
            try:
                self._beacon.set_share(0)
            except Exception:
                pass
        self._share_rows = []
        try:
            # 关闭路径（退出程序）上控件可能已在销毁中，刷新失败不该再抛
            self._refresh_share_view()
        except RuntimeError:
            pass

    def _poll_shares(self):
        """每 3 秒拉一次各成员的云盘清单（网络操作，必须丢后台线程）。"""
        if not self._running or self._hub is None or not self._hub.running:
            return
        if self._share_polling:
            return                     # 上一轮还没回来，别叠加请求
        hub = self._hub
        peers, ports, names = [], {}, {}
        for m in (self._tracker.snapshot() if self._tracker else []):
            ip = m.get("ip") or ""
            if not ip or ip == self._my_ip:
                continue
            names[ip] = m.get("name") or ip
            port = int(m.get("share") or 0)
            if port:
                ports[ip] = port
                peers.append({"ip": ip, "port": port})
        self._share_polling = True

        def work():
            try:
                res = hub.collect(peers, timeout=2.5) if peers else []
            except Exception:
                res = []
            self._event.emit("share_poll",
                             {"res": res, "ports": ports, "names": names})

        threading.Thread(target=work, daemon=True, name="SharePoll").start()

    def _on_share_poll(self, payload):
        payload = payload or {}
        self._share_polling = False
        self._share_peers = payload.get("names") or {}
        self._share_peer_ports = payload.get("ports") or {}
        remote = {}
        for r in payload.get("res") or []:
            if r.get("ok") and r.get("files"):
                remote[r.get("ip") or ""] = r["files"]
        self._share_remote = remote
        self._refresh_share_view()

    def _on_share_prog(self, payload):
        """进度回主线程：只改那一行的进度文本，不重建整行（会闪）。"""
        payload = payload or {}
        w = (self._share_widgets or {}).get(payload.get("key"))
        if not w:
            return
        done = int(payload.get("done") or 0)
        total = int(payload.get("total") or 0)
        pct = int(done * 100 / total) if total else 0
        w["prog"].setText("%d%% · %s / %s"
                          % (pct, lan_share.human_size(done),
                             lan_share.human_size(total)))
        w["prog"].setVisible(True)

    def _on_share_done(self, payload):
        payload = payload or {}
        name = payload.get("name") or "文件"
        self._download = None
        self._cancel_dl = False
        if payload.get("ok"):
            path = str(payload.get("result") or "")
            self.toast("已下载：%s" % os.path.basename(path))
            self._append_log("下载完成：%s" % path)
        else:
            self._append_log("下载未完成：%s（%s）" % (name, payload.get("result")))
            self.toast("下载失败：%s" % payload.get("result"))
        self._share_rows = []           # 强制重建，把按钮文案复位
        self._refresh_share_view()

    def _restyle_share(self, *_):
        """主题切换后重画拖拽区（它的边框高亮是内联样式，不跟 QSS 走）。"""
        try:
            self.drop_zone.restyle()
        except (AttributeError, RuntimeError):
            pass

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
            "怎么用",
            "1. 游戏里点「对局域网开放」（以 Minecraft 为例：端口随机，"
            "以聊天栏显示的为准）。\n"
            "2. 这里「随机」生成房间码、设个密码，点「进入房间」"
            "（首次弹一次 UAC，点「是」）。\n"
            "3. 房间码 + 密码发给队友，队友填同一组、也点「进入房间」。\n"
            "4. 各自打开游戏「多人游戏」，列表里就能看到彼此开的房间。\n"
            "\n"
            "房间码不是谁「建」出来的，是大家约定好的一组暗号，谁先进入都行。\n"
            "昵称必填，队友在「在线成员」里看到的就是它。\n"
            "虚拟 IP 由 EasyTier 自动分配，重启或卡死重进后可能变——变了本页会\n"
            "提示，拿不准就点「在线成员」右上角的「刷新」，不用重进房间。\n"
            "「游戏快连」选你这次要玩的游戏，会广播给队友；每行的「复制」拿到的\n"
            "是**那个人的**「IP:端口」，填进游戏即可。中继节点一般保持「自动」——\n"
            "下拉里各节点的毫秒是本机实测、按快慢排序，每 90 秒自动重测；\n"
            "想手动指定更快/更稳的那个，点「测速」看清延迟再选。\n"
            "\n"
            "原理：内嵌 EasyTier（Apache 2.0 开源）建一块虚拟网卡，把填了相同\n"
            "房间码 + 密码的电脑拉进同一个虚拟局域网——P2P 直连优先，打洞不通时\n"
            "走社区节点中继。虚拟网卡驱动已内置，无需提前装任何东西；首次运行\n"
            "Windows 防火墙若弹窗，允许即可，否则隧道建不起来。\n"
            "房间码 + 密码双重校验，密码不落盘、不明文传输，停止后即从内存清除。\n"
            "\n"
            "临时云盘：进房后把文件拖进「临时云盘」的框里，同房间的人就能下载。\n"
            "文件只在你自己的电脑上，不经任何服务器；退出房间服务即关闭，\n"
            "之后谁都下不到（已下载到本地的不受影响）。\n"
            "\n"
            "备注：神力科莎用 acServer 开服后走游戏内 LAN 列表，或直接填 IP:9600；\n"
            "以撒的结合不支持局域网联机（只能同屏或走 Steam 在线），列表里只是提醒。",
        )

    # ------------------------------------------------------------------ 交互
    def _start_button_text(self):
        """启动按钮文案。**不再有身份之分**，创建和加入是同一个动作。"""
        return "进入房间"

    def _on_reset_nick(self):
        """把昵称恢复为计算机名。"""
        self.nick_edit.setText(default_nickname())

    def _on_node_changed(self, _index):
        """记住所选中继节点（下次打开还是它，MCTier 同款行为）。"""
        key = self.node_combo.currentData() or "auto"
        self._saved_node = key
        try:
            self._settings.setValue("lan_node", key)
        except Exception:
            pass
        # 选的时候把这一项的**本次实测值**写进日志：回头"连不上/很卡"要查
        # 原因时，日志里得有当时看到的数字，而不是一句"他选了这个节点"。
        label = next((l for k, l, _a in etier.NODE_CHOICES if k == key), key)
        name = "自动（全部节点）" if key == "auto" else label
        rec = self._probe.get(key) if key != "auto" else None
        if rec is not None:
            self._append_log("中继节点：%s（本机实测 %s，%s）"
                             % (name, node_probe.format_latency(rec.get("ms")),
                                node_probe.age_text(self._probe.age())))
        else:
            self._append_log("中继节点：%s" % name)

    # ---------------------------------------------------- 中继节点测速（方案 A）
    def _fill_node_combo(self):
        """按当前测速结果重建「中继节点」下拉：快的排前面，数字带在标签上。

        重建会换掉整个下拉内容，所以必须 blockSignals——否则清空/重填
        会把用户的选择当成"用户改了节点"再回调一次，写坏设置项。
        选中的 key 在重建后要原样找回来（排序会挪位置）。
        """
        try:
            keep = self.node_combo.currentData() or self._saved_node or "auto"
        except RuntimeError:
            return
        now = time.time()
        addr_of = {k: a for k, _l, a in etier.NODE_CHOICES}
        # 首屏还没测过：保持裸标签，别一开页就是一列「（待测）」
        missing = "待测" if self._probe.has_data() else None
        decorated = node_probe.decorate_choices(
            etier.NODE_CHOICES, self._probe.results(now=now),
            missing_text=missing)
        self.node_combo.blockSignals(True)
        try:
            self.node_combo.clear()
            for key, label, _addr, rec in decorated:
                self.node_combo.addItem(label, key)
                row = self.node_combo.count() - 1
                addr = addr_of.get(key) or ""
                tip = [label,
                       "地址：%s" % (addr or "全部内置节点（由 EasyTier 自行择优）")]
                if rec:
                    tip.append("本机实测：%s（%s）"
                               % (node_probe.format_latency(rec.get("ms")),
                                  node_probe.age_text(
                                      now - float(rec.get("at") or 0.0))))
                elif addr:
                    tip.append("暂无本机实测数据，点「测速」")
                tip.append("手动选中后只连这个节点（连不上时更好排查）。")
                self.node_combo.setItemData(row, "\n".join(tip), Qt.ToolTipRole)
            idx = self.node_combo.findData(keep)
            self.node_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self.node_combo.blockSignals(False)
        self._saved_node = keep

    def _start_node_probe(self, force=False):
        """起一轮节点测速（后台线程）。

        force=False 时只在数据过期 / 从没测过时才真跑——所以它可以被
        800ms 的 _tick 反复调用而不产生任何开销，也就不会出现"忘了刷新"
        这种状态：只要数字变旧，下一拍就会自己去测。
        """
        if self._probing:
            return
        if not force and not self._probe.is_stale():
            return
        self._probing = True
        try:
            self.btn_node_speed.setEnabled(False)
            self.btn_node_speed.setText("测速中…")
        except RuntimeError:
            return
        items = [(k, l, a) for k, l, a in etier.NODE_CHOICES if a]
        self._update_node_age()
        threading.Thread(target=self._do_node_probe, args=(items,),
                         daemon=True).start()

    def _do_node_probe(self, items):
        """后台线程：并发测完所有节点，把结果用信号送回主线程。"""
        try:
            results = node_probe.probe_all(
                items, should_stop=lambda: self._probe_stop)
        except Exception as exc:
            results = {}
            self._safe_emit("_etier_log", "节点测速出错：%s" % exc)
        self._safe_emit("node_speed", results)

    def _safe_emit(self, kind, payload):
        """从后台线程发信号：页面已销毁时静默放弃。

        测速线程是 daemon，退出程序时可能刚好醒来对着一个已经不存在的
        页面发信号（RuntimeError）。这不是错误，只是"太晚了"。
        """
        try:
            self._event.emit(kind, payload)
        except RuntimeError:
            pass

    def _on_node_speed(self, results):
        """测速回到主线程：入缓存 → 重排下拉 → 更新"数据年龄"。"""
        self._probing = False
        try:
            self.btn_node_speed.setEnabled(True)
            self.btn_node_speed.setText("测速")
        except RuntimeError:
            return                        # 页面正在销毁，别碰控件了
        self._probe.update(results or {})
        self._fill_node_combo()
        self._update_node_age()
        if results:
            self._append_log("节点测速完成：" + node_probe.summary_text(results))
        else:
            self._append_log("节点测速没有拿到任何结果（可能网络刚断）")

    def _update_node_age(self):
        """把「这批延迟数据有多新」写在按钮旁边——新鲜度必须是看得见的。

        刻意不做成"测完就完事"：延迟会变（切网、节点拥塞、自己从国内
        飞到国外），用户有权知道眼前的数字是几秒前的还是半小时前的。
        """
        age = self._probe.age()
        if age is None:
            text = "还没测过，点「测速」"
        elif self._probing:
            text = "正在重测…（上次 %s）" % node_probe.age_text(age)
        elif self._probe.is_stale():
            text = "延迟数据已过期（%s），马上重测…" % node_probe.age_text(age)
        else:
            text = "延迟数据：%s" % node_probe.age_text(age)
        if text == self._node_age_text:
            return                        # 每 800ms 都会被叫到，别反复刷控件
        self._node_age_text = text
        try:
            self.node_age_label.setText(text)
        except RuntimeError:
            pass

    def _selected_game(self):
        """当前「游戏快连」选择 → (游戏名, 端口)。取不到就返回 ("", 0)。"""
        try:
            name = self.game_combo.currentData(Qt.UserRole + 1) or ""
        except Exception:
            name = ""
        try:
            port = int(self.game_combo.currentData() or 0)
        except (TypeError, ValueError):
            port = 0
        return str(name), port

    def _on_game_changed(self, _index=0):
        """切换「游戏快连」：广播给队友 + 重画成员行。

        运行中也能改（这个下拉在房间运行期间保持可用）——很多人是进了
        房间才决定玩什么的。改完立刻踢一次信标广播，队友那边 1 秒内
        就能看到；我这边的本机贴片和「复制 IP:端口」也马上跟着变。
        """
        name, port = self._selected_game()
        if self._beacon is not None:
            try:
                self._beacon.set_game(name, port)
            except Exception:
                pass
        if self._running:
            self._members_row = []          # 强制重建：贴片与复制端口都变了
            self._update_ip()
            self._append_log("游戏快连已切换为 %s（%d），已广播给队友"
                             % (name, port))

    def _on_copy_game_addr(self):
        """游戏快连：复制「虚拟 IP:端口」直连地址。"""
        ip = self._my_ip or etier.virtual_adapter_ip()
        if not ip:
            self.toast("当前没有虚拟 IP 可复制")
            return
        port = self.game_combo.currentData()
        addr = "%s:%s" % (ip, port)
        self._copy_text(addr, "直连地址")
        self._append_log("已复制游戏直连地址 %s" % addr)

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
        node_key = self.node_combo.currentData() or "auto"
        node_addr = etier.node_address(node_key)
        if node_key != "auto":
            node_label = next((l for k, l, _a in etier.NODE_CHOICES
                               if k == node_key), node_key)
            rec = self._probe.get(node_key)
            extra = ("（本机实测 %s）" % node_probe.format_latency(rec.get("ms"))
                     if rec else "（尚未测速）")
            self._append_log("指定中继节点：%s%s" % (node_label, extra))
        threading.Thread(target=self._do_start,
                         args=(code, password, nick, node_addr),
                         daemon=True).start()

    def _do_start(self, code, password, nick, node_addr=""):
        tier = etier.EasyTier(on_log=lambda m: self._event.emit("_etier_log", m))
        # 虚拟 IP 交给 EasyTier 的 DHCP 动态分配（不传 ipv4，走 -d true）：
        # 固定 IP 在"多个房间并存 / 一个房间多人"时会互相抢地址，而 DHCP
        # 遇到冲突会自动改地址。代价是 IP 不再可预测，所以成员侧的准入校验
        # 不能再 ping 固定的 10.126.126.1，改由 pick_host_ip() 动态发现。
        try:
            ok, msg = tier.start(
                "yuhub-" + code.lower().replace(" ", ""), password,
                timeout=20.0, hostname=nick, node=node_addr,
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
            # 昵称靠 NickBeacon 应用层信标（UDP 广播 + TCP 查询兜底）：
            # --hostname 不进 DNS/NetBIOS，gethostbyaddr 在虚拟网里查不出
            # 昵称——这就是"成员列表总显示不了昵称"的根因（v0.8.3beta 修）。
            self._stop_room_threads()
            self._start_room_threads(self._my_ip)
            self._append_log("已进入房间（虚拟 IP %s）" % self._my_ip)
            self.toast("已进入房间")
        else:
            self._running = False
            self._my_ip = ""
            self._stop_room_threads()
            self._refresh_start_gate()
            self._set_status_note("")
            msg = payload.get("msg") or "未知原因"
            self._append_log("跨网房间启动失败：%s" % msg)
            if any(w in msg for w in ("资源缺失", "缺少", "被占用", "大小异常")):
                # 组件损坏/被杀毒软件删除：给出可操作的出路
                msg = (msg + "。组件疑似缺失或损坏，可在「设置中心 → "
                            "异地联机组件」里点「一键修复」重新安装")
            self.toast("启动失败：%s" % msg)

    def _stop_room_threads(self):
        """停掉成员跟踪与昵称信标的后台线程（重复调用安全）。"""
        for attr in ("_tracker", "_beacon"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.stop()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _start_room_threads(self, ip):
        """按给定虚拟 IP 起「昵称信标 + 成员跟踪」这对后台线程。

        抽成函数是因为它有**两个**使用场景，而且两处的正确性要求一样高：
          1. 刚进入房间（_on_done）；
          2. 房间存续期内虚拟 IP 变了（_on_ip_changed）。
        第 2 种情况以前根本不存在——信标把 socket 绑在旧 IP 上、跟踪器
        拿旧 IP 当"排除自己"的判据，IP 一变，这一对就静默失效了：
        人还在房间，但成员列表永远空、别人也看不到你。必须整个重建。
        """
        self._stop_room_threads()
        log = lambda m: self._event.emit("_etier_log", m)     # noqa: E731
        game, port = self._selected_game()
        self._beacon = etier.NickBeacon(
            ip,
            self._host_nick or self._current_nickname(),
            on_log=log,
            game=game, port=port,
        )
        self._beacon.start()
        self._tracker = etier.MemberTracker(
            ip, name_lookup=self._beacon.get_info, on_log=log,
        )
        self._tracker.start()
        # 临时云盘与这一对同生共死（进房起、退房停、IP 变了重绑）
        self._ensure_share_hub(ip)

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
        # 先关云盘（此刻信标还在，能把"我关了云盘"广播出去），再停后台线程
        self._stop_share_hub()
        self._stop_room_threads()
        self._set_running(False)
        self._set_status_note("")
        self._clear_members()
        self._append_log("跨网房间已停止（临时云盘已关闭）")

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
        # 房间运行期间允许改「游戏快连」：很多人是进了房间才决定玩什么，
        # 而且切换会立刻广播给队友、成员行的贴片与复制端口同步更新，
        # 所以没必要锁着它逼用户"停止 → 再进入"。
        for w in (self.code_edit, self.pass_edit, self.btn_random,
                  self.node_combo):
            w.setEnabled(not flag)
        self.game_combo.setEnabled(True)
        # 云盘只在房间运行期间可用：没进房间时没有虚拟网卡可绑，
        # 拖进去也无处可分享。
        self.drop_zone.setEnabled(flag)
        # IP 显示与复制按钮
        self.btn_copy_ip.setEnabled(flag and bool(self._my_ip))
        self.btn_copy_addr.setEnabled(flag and bool(self._my_ip))
        if not flag:
            self.ip_label.setText("--")
            self._clear_members()

    def _clear_members(self):
        """清空成员列表显示（回到"未启动"的占位状态）。"""
        self._render_members([])
        self.members_hint.setText("启动后这里会显示同一房间的成员昵称、虚拟 IP"
                                  "与各自的「游戏快连」")
        self.members_hint.setVisible(True)
        self.members_count.setText("0 人")
        self._members_row = []

    def _on_refresh(self):
        """「刷新」按钮：立刻重读虚拟 IP、重扫成员、强制重建列表。

        针对的实际场景：两个人联机中途程序卡死退出，重新进入房间后
        虚拟 IP 变了（DHCP 重新分配），而界面上还是旧地址——拿去填游戏
        当然连不上。以前唯一的办法是「停止 → 再进入」（十几秒 + 一次
        UAC），现在点一下就把最新 IP 和成员列表拉回来，IP 变了会明确说。
        """
        old_ip = self._my_ip
        if self._tracker is not None:
            try:
                self._tracker.refresh_now()   # 让后台线程立刻再扫一轮
            except Exception:
                pass
        self._members_row = []                # 强制重建，清掉可能残留的旧行
        self._update_ip()
        if self._my_ip and old_ip and self._my_ip != old_ip:
            return                            # 变化提示已由 _on_ip_changed 给出
        if self._running:
            self.toast("已刷新：本机虚拟 IP %s（%d 人在线）"
                       % (self._my_ip or "未就绪", len(self._members_row)))
        else:
            self.toast("已刷新")
        self._append_log("已手动刷新（本机虚拟 IP %s）"
                         % (self._my_ip or "未就绪"))

    def _on_ip_changed(self, old_ip, new_ip):
        """本机虚拟 IP 在房间存续期内变了。

        什么时候会变：DHCP 遇到地址冲突会自动改地址；程序卡死/被强杀后
        重进房间，EasyTier 也可能分到一个新地址。**变了必须整对重建**
        昵称信标与成员跟踪——它们把 IP 焊死在 socket 绑定和"排除自己"
        的判据里，不重建就是"人还在房间、成员列表永远空、别人也看不到
        我"，而且界面上一点异常都没有。这正是"IP 变了不知道"的代价。
        """
        self._append_log("⚠ 本机虚拟 IP 已变化：%s → %s（正按新地址重建成员发现…）"
                         % (old_ip, new_ip))
        self.toast("虚拟 IP 已变化：%s → %s" % (old_ip, new_ip))
        self._members_row = []
        self._start_room_threads(new_ip)

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
            # 先记下旧值：进入房间后 IP 还可能变（DHCP 冲突改址 /
            # 卡死重进），变了要主动告知并重建成员发现（见 _on_ip_changed）
            old_ip = self._my_ip
            changed = bool(old_ip) and ip != old_ip
            self._my_ip = ip
            self.ip_label.setText(ip)
            self.btn_copy_ip.setEnabled(True)
            self.btn_copy_addr.setEnabled(True)
            members = self._tracker.snapshot() if self._tracker else []

            # 多个虚拟地址 = 大概率残留了旧 wintun 网卡（卡死重启后常见）。
            # 挑哪个都可能挑错，所以只如实提示一次，让用户有权决定重进房间。
            all_ips = etier.virtual_adapter_ips()
            if len(all_ips) > 1 and not self._multi_ip_hinted:
                self._multi_ip_hinted = True
                self._append_log(
                    "⚠ 检测到多个虚拟网卡地址：%s。界面用的是 %s；"
                    "若连接不正常，建议点「停止」再重新进入房间，"
                    "把残留的旧网卡清掉。" % ("、".join(all_ips), ip))
            elif len(all_ips) <= 1:
                self._multi_ip_hinted = False

            my_game, my_port = self._selected_game()
            # 自己永远排第一行——用户最常要复制的是自己的地址
            entries = [{"ip": ip,
                        "name": self._host_nick or self._current_nickname(),
                        "self": True, "game": my_game, "port": my_port}]
            for m in members:
                entries.append({
                    "ip": m["ip"],
                    "name": m.get("name") or "",
                    "self": False,
                    "game": m.get("game") or "",
                    "port": int(m.get("port") or 0),
                })
            # 只在"行内容真的变了"时重建控件，否则每 800ms 重建一次会闪。
            # 签名必须带上昵称与游戏快连：队友中途换了游戏，他那行也得跟着
            # 更新——只比 IP 的话，那一行会一直停在旧游戏上。
            sig = [(e["ip"], e["name"], e.get("game", ""), int(e.get("port") or 0))
                   for e in entries]
            old_sig = [(r.get("ip"), r.get("name"), r.get("game", ""),
                        int(r.get("port") or 0)) for r in self._members_row]
            if sig != old_sig:
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
                    "点每行的「复制」，复制到的是那个人的「IP:端口」"
                    "（端口取自他自己选的游戏）。"
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
            if changed:
                # 放在最后：先把这一轮的新地址显示出来，再重建后台线程
                self._on_ip_changed(old_ip, ip)
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
            elif kind == "share_poll":
                self._on_share_poll(payload or {})
            elif kind == "share_prog":
                self._on_share_prog(payload or {})
            elif kind == "share_done":
                self._on_share_done(payload or {})
            elif kind == "node_speed":
                self._on_node_speed(payload or {})
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
        # 节点延迟的自愈在这里：数据一过期就被安排重测，不需要谁记得点按钮。
        # 只在页面可见时跑——用户没在看这一页就别占他的带宽。
        try:
            if self.isVisible():
                self._start_node_probe()
        except RuntimeError:
            pass
        self._update_node_age()
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
        # 让还没跑完的测速线程别再把信号发回来（页面正在拆）
        self._probe_stop = True
        # 退出程序也要关云盘：否则服务线程会吊着端口，用户以为"关了软件
        # 别人还能下"。
        try:
            self._stop_share_hub()
        except Exception:
            pass
        if tier is not None:
            tier.request_stop()      # 同步写停止信号（瞬时、必达）
            tier.wait_stopped(1.5)   # 给看门狗一点时间；等不到也无妨
