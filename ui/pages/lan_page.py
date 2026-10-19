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
import re
import secrets
import socket
import subprocess
import threading
import time

from PySide6.QtCore import Qt, QSettings, QStandardPaths, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import etier
import lan_share
import node_probe
import room_chat
import screenshare
from .. import theme
from ..widgets import ghost_button, primary_button
from .base_page import BasePage


# ---------------------------------------------------------------------------
# 链路日志的归一化（去重键）
# ---------------------------------------------------------------------------
_DIAG_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+(?:[+-]\d{2}:\d{2}|Z)\s+")


def _normalize_diag(line):
    """把一条 EasyTier 日志行归一化成"去重键"。

    🔴 为什么必须有：EasyTier 对连不上的节点会**每 10 秒重试一次**，每次的
    日志行**只有时间戳不同**。界面原来按"整行"去重（`_link_seen`），于是
    永远命中不了 → 同一条错误被反复打印。用户实测（2026-10-19）进房间后
    满屏 ❌，而当时其实已经连上 10 个中继、链路完全正常——纯属噪音。
    这里剥掉时间戳、把所有数字折叠成 `#`，让"同一件事"归成同一个键。

    纯残片（多行 Rust 错误的续行，如 `connect timeout after 1.99s",`）不带
    时间戳 → 返回空串，调用方直接丢弃，不再当成独立错误刷屏。
    """
    s = (line or "").strip()
    if not s.startswith("20"):          # 只认带时间戳的完整记录
        return ""
    s = _DIAG_TS.sub("", s, count=1)
    s = re.sub(r"\d+", "#", s)
    return s[:200]


# 生成房间码用的字母表：去掉 0/O、1/I/L 这类肉眼易混的字符
_CODE_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

# 「中继节点」下拉的宽度上限。节点名后面要挂实测延迟（「海波中国大陆（209ms）」）
# 甚至嵌进「自动（最快：…）」，比裸名字长不少——但也不能无限宽，否则窄窗口下
# 会把同一行的「测速」按钮和「延迟数据」提示挤出去。360 是两侧都留够的折中：
# 现有全部内置节点的最长一项（含延迟）约 230px。
_NODE_COMBO_MAX_W = 360

# 游戏快连（参考 MCTier 的同名功能）：常见联机游戏的**专用服务器/直连
# 端口**预设，运行中一键拼出「虚拟 IP:端口」复制给队友。
# 注意 Minecraft「对局域网开放」的端口是随机的（看游戏聊天栏提示），
# 25565 是它开专用服务器(dedicated server)的默认端口，所以保留在列表里。
# 神力科莎：acServer 默认 9600（TCP+UDP，赛车主数据）——局域网里可直接
#           用（在线列表的 LAN 标签页会自己发现，或手动填 IP:9600）。
# **其他游戏**：端口填 0 = "没有固定端口/用不上端口"（用户 2026-10-05 要求）。
#           这类游戏（自建服、非标准端口的、或者只是想让队友拿到纯 IP 的）
#           复制出来只给**纯 IP**，不拼 `:端口`，UI 里也不显示端口后缀。
# 以撒的结合：官方不支持局域网联机（只有同屏 + Steam 在线），列出来只会
#           让队友白找地址，已按用户 2026-10-11 要求移除。
GAME_PORTS = (
    ("Minecraft 服务器", 25565),
    ("泰拉瑞亚", 7777),
    ("幻兽帕鲁", 8211),
    ("神力科莎", 9600),
    ("饥荒联机版", 10999),
    ("CS 2 / 起源引擎", 27015),
    ("七日杀", 26900),
    ("其他游戏", 0),
)


def game_label(name, port):
    """游戏下拉的显示文案：有端口才带「（端口）」后缀。

    port=0 是「其他游戏」，显示成纯名字，不能出现刺眼的"（0）"。
    """
    return "%s（%d）" % (name, port) if port else str(name)


def game_addr(ip, port):
    """拼游戏直连地址：有端口 → `IP:端口`；无端口 → **纯 IP**。

    这是全局唯一出口 —— 三个复制入口（成员行「复制」、本机「复制 IP:端口」、
    成员行 tooltip）都走它，避免某处漏判又拼出 `10.x.x.x:None` 这种事。
    """
    if not ip:
        return ""
    return "%s:%s" % (ip, port) if port else str(ip)


def _lat_bucket(ms):
    """延迟分档（5ms 一档），只用于"要不要重画成员行"的签名比较。

    为什么要分档而不是直接用精确值：成员行是整体重建的，拿精确延迟当签名
    等于"延迟每抖 0.1ms 就重建一次"。分档之后只有跨档（用户能看出差别）才
    重画。没有延迟数据返回 -1（与 0ms 区分开）。
    """
    try:
        return int(float(ms) // 5)
    except (TypeError, ValueError):
        return -1


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
    """临时云盘的文件投放区：拖文件（或文件夹）进来即分享，点一下选文件。

    做成独立控件而不是给卡片挂拖拽事件：拖拽事件要落在"一块明确的区域"
    上，而且得能自己高亮——悬停时把边框换成主色，用户才知道松手会落到
    这里。用 QFrame 是因为要同时控制边框样式和子标签文字。

    点击：两个子标签都设了 WA_TransparentForMouseEvents。它们本来是
    "点上去什么都不发生"的装饰，但鼠标事件会先落到标签上——尽管 Qt 默认
    会把没被处理的按下事件冒泡给父控件，这条路径在带文本选择的标签上并
    不总是成立。让标签对鼠标彻底透明，点在哪块区域都一定落到本控件上。
    """

    dropped = Signal(list)      # 本地绝对路径列表
    clicked = Signal()          # 点击 = 打开文件选择框

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(64)
        self.setToolTip("把文件 / 文件夹拖到这里分享；也可以直接点一下选文件")
        self._active = False
        self._armed = False      # 房间已开启（拖拽/点击才真的能用）

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(3)
        self.title = QLabel("拖文件 / 文件夹到这里分享")
        self.title.setAlignment(Qt.AlignCenter)
        self.sub = QLabel("也可以点这里选文件")
        self.sub.setAlignment(Qt.AlignCenter)
        self.sub.setWordWrap(True)
        for lb in (self.title, self.sub):
            lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        v.addWidget(self.title)
        v.addWidget(self.sub)
        self.restyle()

    def restyle(self, *_):
        """跟随主题重画（拖拽高亮/未进房间的灰态也走这里）。"""
        p = theme.current()
        if self._active:
            line, bg = p["accent"], p["accent_soft"]
        elif self._armed:
            line, bg = p["border_strong"], p["surface_sunken"]
        else:
            line, bg = p["border"], p["surface_sunken"]
        self.setStyleSheet(
            "QFrame { background: %s; border: 1px dashed %s;"
            " border-radius: 6px; }"
            "QLabel { background: transparent; border: none; }" % (bg, line))
        if self._active:
            title_color = p["accent"]
        else:
            title_color = p["text"] if self._armed else p["text_faint"]
        self.title.setStyleSheet(
            "font-size: 12px; font-weight: 700; color: %s;" % title_color)
        self.sub.setStyleSheet("font-size: 11px; color: %s;" % p["text_faint"])

    def set_armed(self, flag):
        """房间开启状态。没进房间时画成灰的，但**照样能点**——点了会给出
        "先点进入房间"的提示。比一个点不动的灰块有用：用户至少知道该干嘛。"""
        if bool(flag) != self._armed:
            self._armed = bool(flag)
            self.restyle()

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

    #: 房间聊天消息（room_chat 的接收线程 → 主线程 → 屏幕共享页渲染）。
    #: payload 是一条消息记录，字段见 room_chat.RoomChat 的 docstring。
    chat_message = Signal(dict)

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
        # etier.CorePeerFeed：借 easytier-cli 读 core 的**权威成员表**
        # （第三条到场信号 + 链路质量）。cli 不在就静默降级，见该类注释。
        self._core_feed = None
        # 房间聊天（room_chat.RoomChat）：进房才创建，退房即销毁。
        # _chat_file_ids 记着"哪个云盘文件对应哪条聊天消息"——取消分享时
        # 靠它把那条消息广播成「已撤回」。
        self._chat = None
        self._chat_file_ids = {}
        # 链路诊断：已写进日志面板的行（去重）、上次扫描时间
        self._link_seen = set()
        self._link_check_at = 0.0
        # 中继连接状态自检（见 _on_relay_status）：最近一次结果 + 两类提示的
        # "只提示一次" 标记（复位后下次孤身一人会重新给出判断）
        self._relay_st = {}
        self._relay_zero_hinted = False
        self._relay_empty_hinted = False
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
        # ip -> 本轮拉清单失败的原因（未开启 / 超时 / 令牌不匹配…）。
        # 为什么要留住它：以前失败是**静默**的——对方文件一个都不显示、
        # 也没有任何提示，用户只能得出"云盘坏了"的结论（反馈原话：
        # "看不到我朋友分享的软件"）。现在把它渲染成一行可见的故障行。
        self._share_errors = {}
        self._share_seen_errors = {}   # 已写进日志的错误（防每 3 秒刷屏）
        self._share_polling = False   # 上一轮清单还没拉完，别叠加
        self._share_peers = {}        # ip -> 昵称（渲染来源用）
        self._share_rev = 0           # 我这份清单的版本号（增删文件就 +1）
        self._share_seen = {}         # ip -> (端口, 版本号)：上一拍看到的状态
        self._refresh_paused = False  # 模态框开着时挂起自动刷新
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
        # 换主题会换字体 → 之前量出来的宽度就不准了，必须重量一次
        theme.bus.changed.connect(self._fit_node_combo)

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
        # 四区块布局（用户 2026-10-12 要求）：
        #   上 1/3 高度   跨网房间设置（房间码 / 密码 / 昵称 / 节点 / 进入房间）
        #   左下 2/3 高度 在线成员
        #   右中 1/3 高度 临时云盘
        #   右下 1/3 高度 运行日志
        # 用 stretch 1:2 分上下；底部行内左侧 2、右侧上下各 1，
        # 于是右侧两块各占 1/4 + 间距，视觉上就是"右中 / 右下"。
        self.add(self._build_mode_card(), 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        bottom.addWidget(self._build_members_card(), 2)

        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(self._build_share_card(), 1)
        right.addWidget(self._build_log_card(), 1)
        bottom.addLayout(right, 1)

        self.add_layout(bottom, 2)

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
        v.setSpacing(8)

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

        # 第一行：我的昵称 + 房间码（合并成一行，四区块布局下省高度）
        rn = QHBoxLayout()
        rn.setSpacing(8)
        rn.addWidget(self._label("我的昵称"))
        self.nick_edit = QLineEdit()
        self._settings = QSettings("Yuhub", "Yuhub")
        saved_nick = str(self._settings.value("lan_nickname", "") or "").strip()
        self.nick_edit.setText(sanitize_nickname(saved_nick) or default_nickname())
        self.nick_edit.setPlaceholderText("队友看到的名字")
        self.nick_edit.setMaximumWidth(150)
        self.nick_edit.setMaxLength(16)
        rn.addWidget(self.nick_edit)
        btn_nick_reset = ghost_button("恢复默认")
        btn_nick_reset.clicked.connect(self._on_reset_nick)
        rn.addWidget(btn_nick_reset)
        rn.addSpacing(16)
        rn.addWidget(self._label("房间码"))
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("点「随机」生成，或自己起一个")
        self.code_edit.setMaximumWidth(150)
        rn.addWidget(self.code_edit)
        self.btn_random = ghost_button("随机")
        self.btn_random.clicked.connect(
            lambda: self.code_edit.setText(random_room_code())
        )
        rn.addWidget(self.btn_random)
        rn.addStretch(1)
        v.addLayout(rn)
        # 昵称实时驱动启动按钮可用态（必须在 btn_start 建好之后再连，
        # 所以放到卡片最后统一接一次信号）

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
        # 宽度必须跟着内容走。QComboBox 默认的宽度策略只在**首次显示**时
        # 量一次文字宽度，之后 _fill_node_combo() 往列表里塞进「自动（最快：
        # 国内中继 36ms）」这类更长的项，它不会自己变宽——于是最长的几项
        # 被切掉尾巴，用户看到的是「海波中国大…」这种半截地名。
        # AdjustToContents 会在每次内容变化时重新量；_fit_node_combo()
        # 再用当前字体实测一遍兜底（字体随主题变，光靠策略不够稳）。
        self.node_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._saved_node = str(self._settings.value("lan_node", "auto") or "auto")
        self._fill_node_combo()
        self.node_combo.currentIndexChanged.connect(self._on_node_changed)
        r3.addWidget(self.node_combo)

        self.btn_node_speed = QPushButton("测速")
        self.btn_node_speed.setObjectName("MiniButton")
        self.btn_node_speed.setCursor(Qt.PointingHandCursor)
        self.btn_node_speed.setFixedHeight(26)
        self.btn_node_speed.setToolTip(
            "并发测一遍所有内置节点的延迟，按快的排前面。\n"
            "每次打开本页会自动测一次；页面开着的时候想更新，\n"
            "点这里就好（不会偷偷定时重测占带宽）。")
        self.btn_node_speed.clicked.connect(lambda: self._start_node_probe(force=True))
        r3.addWidget(self.btn_node_speed)

        self.node_age_label = self._note("")
        self.node_age_label.setStyleSheet("font-size: 11px;")
        r3.addWidget(self.node_age_label)
        r3.addStretch(1)
        v.addLayout(r3)

        self.status_note = self._note("")
        v.addWidget(self.status_note)

        # 运行中的直连信息放同一行：本机虚拟 IP + 游戏快连
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
        rip.addSpacing(16)

        # 游戏快连（参考 MCTier）：常见联机游戏端口预设，
        # 运行中一键拼出「虚拟 IP:端口」复制，省得现查端口表。
        rip.addWidget(self._label("游戏快连"))
        self.game_combo = QComboBox()
        self.game_combo.setMaximumWidth(200)
        for i, (name, port) in enumerate(GAME_PORTS):
            self.game_combo.addItem(game_label(name, port), port)
            # 纯游戏名单独存一份（纯展示用：成员行里的贴片 + 广播给队友）
            self.game_combo.setItemData(i, name, Qt.UserRole + 1)
        self.game_combo.setToolTip(
            "选你这次要玩的游戏：会广播给队友（成员行显示贴片），\n"
            "每行的「复制」也给那个人的「IP:端口」。\n"
            "选「其他游戏」时不带端口，复制的是纯 IP。进房间后也能改。")
        rip.addWidget(self.game_combo)
        self.btn_copy_addr = ghost_button("复制 IP:端口")
        self.btn_copy_addr.setEnabled(False)
        self.btn_copy_addr.clicked.connect(self._on_copy_game_addr)
        rip.addWidget(self.btn_copy_addr)
        rip.addStretch(1)
        v.addLayout(rip)

        # 放在最后连：addItem 填充期间也会发 currentIndexChanged，
        # 那时 _beacon / 成员行还没建起来，提前连会打到空对象上。
        self.game_combo.currentIndexChanged.connect(self._on_game_changed)

        # 用法浓缩（原「怎么用」大卡片的要点，四区块布局后并到这里）
        v.addWidget(self._note(
            "用法：各自填同一组房间码 + 密码，点「进入房间」（首次弹一次 UAC）；"
            "游戏里点「对局域网开放」即可在多人游戏列表互相看到。\n"
            "文件拖进右侧「临时云盘」与队友互传，退房即失效；"
            "虚拟 IP 变了会提示，也可点「刷新」重读。",
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
        card.setMinimumHeight(150)
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(6)

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
        self.btn_members_refresh.setFixedHeight(24)
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
        self.members_layout.setSpacing(5)
        v.addWidget(self.members_box, 1)

        # 卡片吃的是 2/3 高度的 stretch：不在末尾兜一个 stretch，多余空间
        # 会被平均分配到**标题/说明/成员行**之间 —— 标题与说明之间空出
        # 一大块（用户说的"大额头"），成员行也会被纵向拉成一个大长条。
        # 兜底 stretch 之后：所有内容贴顶，空余落在列表下方，成员行保持
        # 固定高度的细长条。
        v.addStretch(1)
        return card

    def _make_member_row(self, name, ip, is_self=False, game="", port=0, ss=0,
                         core=None):
        """构造一行成员：色块 + 昵称 + IP + 游戏快连贴片 + 链路质量 + 看屏幕 + 复制。

        is_self 为真时高亮并标注"（我）"，方便用户一眼分清
        "哪个地址是我自己的"——填进游戏别填错了。

        game / port 是**这个人**在「游戏快连」里选的那款游戏（自己就是
        当前下拉值，队友由昵称信标广播过来）。显示它是为了省掉"你玩的
        是哪个、端口多少"的来回问；点「复制」时也用他的端口拼地址，
        不再受我自己那个下拉的影响。

        ss 是**这个人**在「屏幕共享」页开的服务端口（0 = 没在共享），
        同样由信标广播过来。有它就有「看屏幕」按钮。

        core 是这条链路的**权威质量数据**（来自 EasyTier 自己的成员表，
        见 etier.CorePeerFeed）：延迟/丢包/隧道协议/NAT 类型/是否直连。
        没有它（cli 没装、core 没起）就什么都不显示——这一行照旧能用，
        这是"保留旁路通道"的意思。
        """
        p = theme.current()
        row = QFrame()
        row.setObjectName("MemberRow")
        # 固定高度：成员行是"一行一条"的细长条，绝不能被布局纵向拉伸
        # （之前卡片吃 stretch，单条成员被拉成 130px 的大长条）。
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        row.setFixedHeight(36)
        # 自己的行用主色淡底 + 主色描边，扫一眼就知道哪个地址是自己的；
        # 别人的行用下沉底色，鼠标悬停时描边变主色（样式里出现 :hover 时
        # Qt 会自动给控件打开 WA_Hover）。
        bg = p.get("accent_soft", p["surface_sunken"]) if is_self \
            else p["surface_sunken"]
        bd = p["accent"] if is_self else p["border"]
        row.setStyleSheet(
            "QFrame#MemberRow { background: %s; border: 1px solid %s;"
            " border-radius: 6px; }"
            "QFrame#MemberRow:hover { border-color: %s; }"
            % (bg, bd, p["accent"])
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 0, 8, 0)
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

        # 「中继」标记：core 的成员表说这条链路**没有直连**（多跳/走中继）。
        # 为什么值得单独摆一个贴片：公共中继是社区共享带宽（实测有效吞吐
        # 约 3.6Mbps），异地分屏帧率低十有八九就是它——把这一条摆在成员行
        # 上，用户不用翻日志就知道该换个中继节点、或者让两边别挡着 UDP。
        if core and core.get("relayed") and not is_self:
            relay_tag = QLabel("中继")
            relay_tag.setStyleSheet(
                "font-size: 11px; color: %s; background: transparent;"
                " border: 1px solid %s; border-radius: 4px; padding: 1px 6px;"
                % (p["red"], p["red"]))
            relay_tag.setToolTip(
                "这条链路没有直连（走公共中继或多跳）。中继带宽是社区共享的，"
                "屏幕共享/传文件的速率会被它限制。\n可以试试：\n"
                "· 在「中继节点」里换一个（下拉里显示的是本机实测延迟）；\n"
                "· 确认两边都没被防火墙/路由器挡着 UDP（打洞靠它）。")
            h.addWidget(relay_tag)

        h.addStretch(1)

        # 「看屏幕」：对方正在共享屏幕时才有。点击跳到屏幕共享页并直接连上
        # ——地址用他的虚拟 IP，端口来自他广播的信标，观看码由房间码+密码
        # 两边各自算，所以这里一个都不用用户输。
        if ss and not is_self:
            watch = QPushButton("看屏幕")
            watch.setObjectName("MiniButton")
            watch.setCursor(Qt.PointingHandCursor)
            watch.setFixedHeight(26)
            watch.setMinimumWidth(64)
            watch.setToolTip("%s 正在共享屏幕，点这里看" % (name or ip))
            watch.clicked.connect(
                lambda _=False, a=ip, b=ss, c=name: self._watch_peer(a, b, c))
            h.addWidget(watch)

        # 「复制」用标准 mini 尺寸：以前用 ghost_button + setFixedWidth(52)，
        # 而 ghost 样式自带 18px 左右内边距，52 宽留给文字的只有十几像素，
        # 两个字被挤成"显示不清楚"。MiniButton 的内边距是 2px 12px，够用。
        btn = QPushButton("复制")
        btn.setObjectName("MiniButton")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(26)
        btn.setMinimumWidth(56)
        # 复制「IP:端口」而不是裸 IP：游戏里要填的就是带端口的直连地址
        # （「IP:端口」缺一不可）。端口优先用**这个人的**游戏快连。
        #
        # ⚠️ 这里不能用 `port or 我的端口`：他明确选了「其他游戏」（game 有值、
        # port=0）时，`port or …` 会退回**我的**端口，复制出
        # `10.x.x.x:25565` —— 这正是用户报的"选了其他游戏还是带端口"。
        # 判据看 game 字段而不是 port：
        #   game 非空 + port=0  → 他选了无端口游戏 → 纯 IP
        #   game 为空           → 他还没广播过     → 退回我自己选的，至少能用
        if game:
            use_port = port
        else:
            use_port = port or (self.game_combo.currentData() or 0)
        addr = game_addr(ip, use_port)
        btn.setToolTip("复制直连地址 %s" % addr)
        btn.clicked.connect(lambda _=False, v=addr: self._copy_text(v, "直连地址"))
        h.addWidget(btn)

        # 链路质量详情放 tooltip：行要能一眼扫过去，精确数字按需看。
        # （数据来自 core 的成员表；没接上时 core 为空，这一行照旧可用。）
        if core:
            bits = []
            lat = core.get("lat_ms")
            if lat is not None:
                try:
                    bits.append("延迟 %.1f ms" % float(lat))
                except (TypeError, ValueError):
                    pass
            if core.get("loss_rate"):
                bits.append("丢包 %s" % core["loss_rate"])
            if core.get("tunnel"):
                bits.append("隧道 %s" % core["tunnel"])
            if core.get("nat"):
                bits.append("NAT %s" % core["nat"])
            if core.get("relayed"):
                bits.append("未直连（中继/多跳）")
            if bits:
                row.setToolTip("链路：%s\n（数据来自 EasyTier 自己的成员表）"
                               % " · ".join(bits))
        return row

    def _render_members(self, entries):
        """按 entries 重建成员行。

        entries: [{"ip","name","self","game","port","ss","core"}]。每次都整体重建
        而不是做差量更新：成员数量是个位数，重建几个小控件的开销可以忽略，
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
                                      port=int(e.get("port") or 0),
                                      ss=int(e.get("ss") or 0),
                                      core=e.get("core") or {})
            )
        # 行全部贴顶：成员少的时候多出来的高度落在列表下方，而不是把
        # 每一条行都撑高（重渲染时这个 spacer 会被 takeAt 一起清掉）。
        # 空列表时不加：清空后布局要真的"空"（占位态判断依赖这个）。
        if entries:
            self.members_layout.addStretch(1)
        self.members_box.setVisible(bool(entries))

    def _share_page(self):
        """拿到屏幕共享页实例（两个页面是兄弟，都挂在主窗口的 _pages 上）。

        用间接查找而不是互相持有引用：这两页在功能上没有从属关系，
        为了一次跳转把它们绑死，会让任何一页的构造顺序都变成必须。
        """
        win = self.window()
        pages = getattr(win, "_pages", None) if win is not None else None
        return pages.get("share") if pages else None

    def _watch_peer(self, ip, ss_port, name=""):
        """跳到屏幕共享页并直接看某个成员的屏幕。

        这一跳顺手把观看码也解决了：房间成员两边都用「房间码+密码」派生，
        所以不用问用户要。拿不到屏幕共享页（不该发生）就如实说一声。
        """
        win = self.window()
        page = self._share_page()
        if page is None or not hasattr(page, "watch_peer"):
            self.toast("屏幕共享页不可用")
            return
        switcher = getattr(win, "switch_page", None)
        if callable(switcher):
            switcher("share")
        page.watch_peer(ip, ss_port,
                        token=screenshare.room_view_token(),
                        name=name)

    def _publish_screenshare(self, port):
        """屏幕共享页开/停共享时回调进来：把端口写进昵称信标广播出去。

        队友收到这个键，成员行上就会多出/收起「看屏幕」按钮。
        """
        if self._beacon is not None:
            try:
                self._beacon.set_screenshare(port)
            except Exception:
                pass

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
        card.setMinimumHeight(140)
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
        v.addLayout(head)

        self.drop_zone = _DropZone()
        self.drop_zone.dropped.connect(self._on_share_paths)
        self.drop_zone.clicked.connect(self._on_share_pick)
        v.addWidget(self.drop_zone)

        self.share_hint = QLabel(
            "进入房间后可用：把文件拖进上面的框（或点一下选文件），"
            "队友在左侧「在线成员」里就能看到并下载。\n"
            "文件只在你自己的电脑上，不经任何服务器；"
            "退出房间（或点「停止」）服务立即关闭。"
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
        # 与成员行一致：固定高度的细长条，不被卡片 stretch 拉伸
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        row.setFixedHeight(36)
        row.setStyleSheet(
            "QFrame#MemberRow { background: %s; border: 1px solid %s;"
            " border-radius: 6px; }"
            "QFrame#MemberRow:hover { border-color: %s; }"
            % (p["surface_sunken"], p["border"], p["accent"])
        )
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 0, 8, 0)
        h.setSpacing(8)

        dot = QFrame()
        dot.setFixedSize(9, 9)
        if entry.get("problem"):
            dot_color = p["amber"]
        else:
            dot_color = p["accent"] if entry["mine"] else p["text_faint"]
        dot.setStyleSheet(
            "background: %s; border: none; border-radius: 2px;" % dot_color)
        h.addWidget(dot)

        name = QLabel(entry["name"])
        name.setToolTip(entry["name"])
        if entry.get("problem"):
            name.setStyleSheet(
                "font-size: 12px; font-weight: 400; color: %s;"
                " background: transparent; border: none;" % p["amber"])
        else:
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

        size = QLabel(lan_share.human_size(entry["size"]) if not entry.get("problem")
                      else "—")
        size.setStyleSheet(
            "font-size: 11px; color: %s; background: transparent; border: none;"
            % p["text_faint"])
        h.addWidget(size)

        if entry.get("problem"):
            btn = QPushButton("重试")
            btn.setObjectName("MiniButton")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(26)
            btn.setMinimumWidth(56)
            btn.clicked.connect(lambda _=False: self._poll_shares())
        else:
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
        # 拉清单失败的队友：列一行**看得见**的故障行（带原因 + 重试按钮）。
        # 以前这里是静默的，用户只能看到"一个文件都没有"，无法判断是自己
        # 没分享、还是对方不可达、还是令牌对不上。
        for ip, err in (self._share_errors or {}).items():
            owner = (self._share_peers or {}).get(ip) or ip
            entries.append({"key": "err:" + ip, "fid": "", "name": err,
                            "size": 0, "owner": owner, "mine": False,
                            "ip": ip, "port": 0, "problem": True})
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
        self.share_count.setText(
            "%d 个文件" % sum(1 for e in entries if not e.get("problem")))
        if self._running and self._hub is not None and self._hub.running:
            others = sum(len(f) for f in (self._share_remote or {}).values())
            bad = len(self._share_errors or {})
            extra = ("\n⚠ 有 %d 位队友的云盘暂时连不上（列表里已标出来，"
                     "可点「重试」）。" % bad) if bad else ""
            self.share_hint.setText(
                "云盘已开启：拖文件进来（或点一下这里选文件），队友就能下载；"
                "已看到队友分享的 %d 个文件。\n"
                "下载时每次都会让你选保存位置。队友退出房间或取消分享，"
                "他那些文件会立刻从这里消失。\n"
                "你退出房间后服务立即关闭，之后谁都下不到。%s" % (others, extra))

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
        # 与在线成员一致：文件少时多出来的高度落在列表下方（空列表不加）
        if entries:
            self.share_layout.addStretch(1)
        self.share_box.setVisible(bool(entries))

    # ------------------------------------------------------------ 云盘：交互
    def _pause_refresh(self):
        """打开模态框期间停掉 3 秒一轮的云盘轮询。

        为什么必须停：轮询回来会重建整个文件列表，把用户**此刻正点的那个
        按钮** deleteLater 掉，而模态对话框自己转事件循环，那条"延迟删除"
        会在对话框还开着的时候就真被执行——按钮在信号处理过程中被销毁。
        停表是最省心的规避（列表本来就没人看得到，它在模态框底下）。
        """
        self._refresh_paused = True
        try:
            self._share_timer.stop()
        except (AttributeError, RuntimeError):
            pass

    def _resume_refresh(self):
        """恢复自动轮询，并**立刻补一次**——停表期间对方可能已经动了文件。"""
        self._refresh_paused = False
        try:
            self._share_timer.start()
        except (AttributeError, RuntimeError):
            pass
        self._poll_shares()

    def _last_dir(self, key, fallback):
        """从设置里取上次用过的目录（不存在就退回 fallback）。"""
        try:
            saved = str(self._settings.value(key, "") or "")
        except Exception:
            saved = ""
        if saved and os.path.isdir(saved):
            return saved
        return fallback

    def _ask_save_path(self, name):
        """弹保存对话框，返回用户选的绝对路径；取消返回 ""。

        **每次下载都问一遍**：临时云盘下的是别人电脑上的文件，落地到哪
        只有用户自己清楚（有人要扔桌面、有人要塞 D 盘、有人只想先看一眼）。
        默认目录记住上次选的那个，省得每次都从"文档"翻起。
        """
        default_dir = QStandardPaths.writableLocation(
            QStandardPaths.DownloadLocation) or os.path.expanduser("~")
        start_dir = self._last_dir("share_save_dir", default_dir)
        suggested = os.path.join(start_dir, lan_share.safe_name(name))
        self._pause_refresh()
        try:
            path, _ = QFileDialog.getSaveFileName(
                self, "选择保存位置", suggested, "所有文件 (*)")
        finally:
            self._resume_refresh()
        path = str(path or "")
        if path:
            try:
                self._settings.setValue("share_save_dir", os.path.dirname(path))
            except Exception:
                pass
        return path

    def _on_share_pick(self):
        """点击拖拽区 = 打开文件选择框（可多选）。"""
        if not (self._running and self._hub is not None):
            self.toast("先点「进入房间」，云盘才会开启")
            return
        start_dir = self._last_dir(
            "share_pick_dir", os.path.expanduser("~"))
        self._pause_refresh()
        try:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "选择要分享给房间成员的文件", start_dir, "所有文件 (*)")
        finally:
            self._resume_refresh()
        paths = list(paths or [])
        if paths:
            try:
                self._settings.setValue(
                    "share_pick_dir", os.path.dirname(paths[0]))
            except Exception:
                pass
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
        if added:
            # 分享清单变了：版本号 +1 并立刻广播，再自己重拉一次。
            # 队友的信标缓存是"收到就写"，所以它们大约 1 秒内就会看到版本号
            # 变化并立刻重拉清单——这就是"每次上传文件，所有人都刷新一次"。
            self._bump_share_rev()
            self._poll_shares()
            # 同时，每个新分享的文件在聊天室里留一条可下载的消息
            self._announce_share_files(added)
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
            # 聊天室里那条文件消息同步变成「已撤回」（先撤再广播版本号，
            # 队友那边两条信号到的顺序无所谓：一个改文字、一个撤列表行）
            self._withdraw_chat_file(entry["fid"])
            # 取消分享同样要让所有人立刻刷新（队友那边这一行要马上消失）
            self._bump_share_rev()
            self._poll_shares()
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
        # 先让用户挑保存位置（取消就什么都不做，连请求都不发）
        dest = self._ask_save_path(entry["name"])
        if not dest:
            self._append_log("已取消下载：%s" % entry["name"])
            self.toast("已取消下载")
            return
        key = entry["key"]
        self._download = {"key": key, "name": entry["name"]}
        self._cancel_dl = False
        self._append_log("开始下载 %s（来自 %s）→ %s"
                         % (entry["name"], entry["owner"], dest))
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
                                   timeout=20.0, dest_path=dest)
            self._event.emit("share_done",
                             {"key": key, "ok": ok, "result": res,
                              "name": entry["name"]})

        threading.Thread(target=work, daemon=True, name="ShareDownload").start()
        self._refresh_share_view()

    def _publish_share_port(self):
        """把云盘端口 + 清单版本号写进昵称信标。

        队友的成员列表下一轮就知道我开着云盘；版本号则是"我动过分享清单"
        的信号，对端据此立刻重拉一次（见 etier._NICK_SHARE_REV）。
        """
        if self._beacon is None:
            return
        port = self._hub.port if (self._hub is not None and self._hub.running) else 0
        try:
            self._beacon.set_share(port, self._share_rev)
        except Exception:
            pass

    def _bump_share_rev(self):
        """分享清单变了（上传/取消）：版本号 +1 并立刻广播出去。"""
        self._share_rev = int(self._share_rev) + 1
        self._publish_share_port()

    # ------------------------------------------------------------------ 聊天
    def chat_history(self):
        """聊天历史（屏幕共享页切到前台时补画用）。"""
        return self._chat.history() if self._chat is not None else []

    def chat_active(self):
        """聊天是否可用（在房间里且监听成功）。"""
        return self._chat is not None and self._chat.running

    def send_chat_text(self, text):
        """屏幕共享页的输入框调用：往房间发一条文本消息。

        返回 (成功, 错误说明)。失败原因直接来自引擎（没进房 / 没虚拟 IP /
        发送失败），界面原样 toast 出来。
        """
        if self._chat is None or not self._chat.running:
            return False, "还没进入房间（先在「异地联机」里进房）"
        try:
            rec = self._chat.send_text(text)
        except Exception as exc:
            return False, "发送失败：%s" % exc
        if rec is None:
            return False, "消息是空的"
        if rec.get("error"):
            return False, str(rec.get("error"))
        return True, ""

    def _announce_share_files(self, added):
        """把新分享的文件广播成聊天室里的文件消息（带下载按钮）。

        一条消息一个文件：`_chat_file_ids` 记下 fid → 消息 id，取消分享时
        靠它广播撤回（见 _withdraw_chat_file）。聊天没起来（监听失败等）
        就只走云盘原有的版本号广播——聊天是锦上添花，不拦着传文件。
        """
        if not added or self._chat is None or not self._chat.running:
            return
        port = (self._hub.port if (self._hub is not None
                                   and self._hub.running) else 0)
        for e in added:
            try:
                rec = self._chat.send_file(e["name"], e["size"], e["id"], port)
            except Exception:
                continue
            if rec:
                self._chat_file_ids[e["id"]] = rec["id"]

    def _withdraw_chat_file(self, fid):
        """取消分享 → 广播撤回：队友聊天室里那条文件消息变成「已撤回」。"""
        mid = self._chat_file_ids.pop(fid, "")
        if not mid or self._chat is None:
            return
        try:
            self._chat.withdraw(mid)
        except Exception:
            pass

    def download_chat_file(self, rec):
        """下载聊天室里的文件消息（屏幕共享页的下载图标调用）。

        端口优先用**轮询拿到的最新值**：消息里带的是对方分享那一刻的云盘
        端口，对方换 IP 重绑后会变；`_share_peer_ports` 每 3 秒随清单刷新。
        下载 key 与云盘列表行同一个格式（ip:fid）——于是同一次下载在云盘
        卡片里也能看到进度，两边点的是同一个文件，不该出现两个进度。
        """
        hub = self._hub
        if hub is None or not hub.running:
            self.toast("临时云盘没在运行（先进房间）")
            return
        if self._download is not None:
            self.toast("已有一个下载在进行，先等它结束或点它那行的「取消」")
            return
        rec = rec or {}
        ip = str(rec.get("from_ip") or "")
        fid = str(rec.get("fid") or "")
        name = str(rec.get("name") or "文件")
        port = int((self._share_peer_ports or {}).get(ip)
                   or rec.get("port") or 0)
        if not ip or not fid or not port:
            self.toast("这条文件消息已经下不了了（对方可能退出了房间）")
            return
        dest = self._ask_save_path(name)
        if not dest:
            self._append_log("已取消下载：%s" % name)
            self.toast("已取消下载")
            return
        key = "%s:%s" % (ip, fid)
        self._download = {"key": key, "name": name}
        self._cancel_dl = False
        self._append_log("开始下载 %s（来自 %s，聊天室）→ %s" % (name, ip, dest))
        last = [0.0]

        def prog(done, total):
            now = time.monotonic()
            if done != total and now - last[0] < 0.25:
                return          # 节流：每个 64KB 分块都发信号会把事件队列灌满
            last[0] = now
            self._event.emit("share_prog", {"key": key, "done": done,
                                            "total": total})

        def work():
            ok, res = hub.download(ip, port, fid, progress=prog,
                                   is_cancelled=lambda: self._cancel_dl,
                                   timeout=20.0, dest_path=dest)
            self._event.emit("share_done",
                             {"key": key, "ok": ok, "result": res,
                              "name": name})

        threading.Thread(target=work, daemon=True, name="ChatDownload").start()

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
        self._share_errors = {}
        self._share_seen_errors = {}
        # 云盘没了：fid → 聊天消息 id 的对照表也没用了（聊天记录本身留着，
        # 但那些文件消息点了也下不到了——服务已经停了，如实失败即可）
        self._chat_file_ids = {}
        if hub is not None:
            try:
                hub.stop()
            except Exception:
                pass
        if self._beacon is not None:
            try:
                # 先把"我关了云盘"写进报文，再**同步**广播一次——别指望后台
                # 广播线程：它马上就会被 _stop_room_threads 停掉，很可能一个
                # 包都没发出去。队友收到这条就知道该把我分享的文件撤下来，
                # 不用等它们的 HTTP 轮询超时（那要好几秒）。
                self._beacon.set_share(0)
                self._beacon.announce_now()
            except Exception:
                pass
        self._share_rows = []
        self._share_seen = {}
        try:
            # 关闭路径（退出程序）上控件可能已在销毁中，刷新失败不该再抛
            self._refresh_share_view()
        except RuntimeError:
            pass

    def _room_alive_ips(self):
        """此刻「应该算在场」的成员 IP：跟踪器快照 ∪ 信标新鲜表。

        为什么云盘也要自己并一次（跟踪器内部已经并了信标）：跟踪器是另一条
        线程的快照，慢一拍不要紧，**卡住**也不要紧——但只要这一拍没算他
        在场，`_share_watch` 就会把他分享的文件行撤掉、`_poll_shares` 也不
        会去拉他的清单。用户实测的"好友在房间里放东西我看不到"，就是
        peer 列表单靠跟踪器快照造成的。两条线索取并集，任何一条算数。
        """
        ips = set()
        try:
            if self._tracker is not None:
                ips.update(m.get("ip") or ""
                           for m in self._tracker.snapshot())
        except Exception:
            pass
        try:
            if self._beacon is not None:
                ips.update((self._beacon.live_peers() or {}).keys())
        except Exception:
            pass
        ips.discard("")
        if self._my_ip:
            ips.discard(self._my_ip)
        return ips

    def _poll_shares(self):
        """每 3 秒拉一次各成员的云盘清单（网络操作，必须丢后台线程）。"""
        if self._refresh_paused:
            return                     # 模态框开着，先别重建列表
        if not self._running or self._hub is None or not self._hub.running:
            return
        if self._share_polling:
            return                     # 上一轮还没回来，别叠加请求
        hub = self._hub
        # 端口优先用信标缓存里的：它比成员跟踪器早一拍拿到（跟踪器 3 秒
        # 才回收一次），对方刚开的云盘不用等下一轮跟踪才被发现。
        try:
            live = self._beacon.live_shares() if self._beacon is not None else {}
        except Exception:
            live = {}
        # 跟踪器快照兜底：信标条目有 15 秒新鲜期，偶尔连丢几包就过期了，
        # 这时跟踪器里 sticky 的 share/srev 还能把轮询续上。
        snap = {}
        try:
            if self._tracker is not None:
                snap = {m.get("ip") or "": m
                        for m in self._tracker.snapshot()}
        except Exception:
            snap = {}
        # 🔴 轮询对象 = 跟踪器快照 ∪ 信标新鲜表（见 _room_alive_ips）：
        # 以前只遍历跟踪器快照，ARP 没生成条目的队友永远不进 peers 列表，
        # 他的文件就永远拉不回来——"好友放了文件我看不到"的根因。
        peers, ports, names = [], {}, {}
        for ip in sorted(self._room_alive_ips()):
            m = snap.get(ip) or {}
            if ip in live:
                port = int(live[ip][0] or 0)     # 信标说关了就是关了，别拿旧值
            else:
                port = int(m.get("share") or 0)
            if port:
                ports[ip] = port
                peers.append({"ip": ip, "port": port})
                names[ip] = m.get("name") or ip
        self._share_polling = True

        def work():
            try:
                res = hub.collect(peers, timeout=2.5) if peers else []
            except Exception:
                res = []
            self._event.emit("share_poll",
                             {"res": res, "ports": ports, "names": names})

        threading.Thread(target=work, daemon=True, name="SharePoll").start()

    def _share_watch(self):
        """云盘"现在就该刷新"的判定（每一拍跑一次，不发任何网络请求）。

        两件事，都是为了让列表**当场**跟上现实，而不是等 3 秒的轮询周期：

          1. 对方信标里的版本号变了 → 他刚上传或取消了一个分享文件，
             马上重拉一次他的清单。（"谁传文件/取消分享，所有人都刷新一次"）
          2. 对方的云盘端口变成 0（他关了云盘）、或者两条到场信号都看不到
             他了（真退出房间 / 掉线）→ 当场把他分享的文件行撤下来。
             （"分享的人退出房间，文件直接从分享列表里消失"）
        """
        if not (self._running and self._hub is not None and self._hub.running):
            self._share_seen = {}
            return
        marks = {}
        try:
            for m in (self._tracker.snapshot() if self._tracker else []):
                ip = m.get("ip") or ""
                if ip and ip != self._my_ip:
                    marks[ip] = int(m.get("share") or 0)
        except Exception:
            marks = {}
        # 到场判定同样走并集：只认跟踪器快照的话，ARP 慢一拍就会把队友的
        # 文件行撤掉，下一拍又拉回来——用户看到的就是"文件一闪一闪"，
        # 严重时（跟踪器那拍始终没他）就一直是空的。
        online = self._room_alive_ips()
        try:
            live = self._beacon.live_shares() if self._beacon is not None else {}
        except Exception:
            live = {}

        # ⑵ 掉线 / 关了云盘 → 立刻撤行
        changed = False
        for ip in list(self._share_remote):
            port = live[ip][0] if ip in live else marks.get(ip, 0)
            if ip not in online or not port:
                self._share_remote.pop(ip, None)
                self._share_peer_ports.pop(ip, None)
                self._share_seen.pop(ip, None)
                changed = True
        # ⑴ 版本号变了 → 立刻重拉
        need_poll = False
        for ip in sorted(online):
            state = live.get(ip) or (marks.get(ip, 0), 0)
            if self._share_seen.get(ip) != state:
                need_poll = True
            self._share_seen[ip] = state
        for ip in list(self._share_seen):
            if ip not in online:
                self._share_seen.pop(ip, None)
        if changed:
            self._refresh_share_view()
        if need_poll:
            self._poll_shares()

    def _on_share_poll(self, payload):
        payload = payload or {}
        self._share_polling = False
        self._share_peers = payload.get("names") or {}
        self._share_peer_ports = payload.get("ports") or {}
        remote = {}
        errors = {}
        for r in payload.get("res") or []:
            ip = r.get("ip") or ""
            if not ip:
                continue
            if r.get("ok"):
                remote[ip] = r.get("files") or []
            else:
                err = str(r.get("error") or "未知原因")
                errors[ip] = err
                # ⭐ 本轮失败**不清空**上次拿到的清单：网络抖动、对方一时忙
                # 都会失败一次，清空就会让队友的文件在列表里一闪一闪。
                # 保留旧数据 + 打上"暂时连不上"的标记，才是用户能理解的行为。
                if ip in (self._share_remote or {}):
                    remote[ip] = self._share_remote[ip]
                if err and self._share_seen_errors.get(ip) != err:
                    self._append_log(
                        "拉取 %s 的云盘清单失败：%s" % (self._share_peers.get(ip) or ip, err))
        self._share_seen_errors = dict(errors)
        self._share_remote = remote
        self._share_errors = errors
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
        card.setMinimumHeight(140)
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
        # 四区块布局下日志卡只占右下 1/3：不锁死高度，让日志框随卡片伸缩。
        # 垂直策略用 Ignored——QPlainTextEdit 空文档的 sizeHint 也有上百像素，
        # 默认 Preferred 会把整页的 sizeHint 顶高、拖出滚动条。
        self.log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.log_view.setMinimumHeight(80)
        v.addWidget(self.log_view)
        return card

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
            # 换项不重建列表，悬停提示得跟着换，否则停在旧节点上
            self.node_combo.setToolTip(self.node_combo.currentText())
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
        # keep_stale + TTL_NEVER：数字一直显示，过期与否只由旁边的
        # 「延迟数据：x 分钟前」说明。现在没有定时重测了，若还按 TTL 过滤，
        # 用户在这一页停留 90 秒后整列会突然变回「待测」——像是数据丢了。
        decorated = node_probe.decorate_choices(
            etier.NODE_CHOICES, self._probe.results(now=now, keep_stale=True),
            missing_text=missing, ttl=node_probe.TTL_NEVER)
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
        self._fit_node_combo()

    def _fit_node_combo(self, *_args):
        """把下拉撑到「刚好放得下最长那一项」，一个字都不切。

        为什么不能只靠 Qt 的宽度策略：QComboBox 默认只在首次显示时量一次
        文字宽度，而带延迟的项（尤其「自动（最快：海波中国大陆 44ms）」）
        是测速回来之后才出现的，比首屏的裸名字长一倍。不重新量就会出现
        「海波中国大…」——地名被切掉一半，用户根本分不清是哪个节点。

        这里直接按当前字体逐项实测。上限 _NODE_COMBO_MAX_W 兜住超长节点名，
        *（*_args 是为了能直接挂到主题切换信号上——那个信号带参。）
        """
        try:
            fm = self.node_combo.fontMetrics()
            widest = 0
            for i in range(self.node_combo.count()):
                widest = max(widest,
                             fm.horizontalAdvance(self.node_combo.itemText(i)))
            if not widest:
                return
            # 左右内边距 + 下拉箭头：箭头宽度随主题变，统一多留一点，
            # 宁可宽几像素，也不要最后一两个字被箭头压掉。
            self.node_combo.setMinimumWidth(
                min(widest + 52, _NODE_COMBO_MAX_W))
            self.node_combo.setMaximumWidth(_NODE_COMBO_MAX_W)
            # 万一以后节点名更长顶到上限，全文至少能在悬停提示里看全
            self.node_combo.setToolTip(self.node_combo.currentText())
        except RuntimeError:
            pass                          # 页面正在销毁，别碰控件

    def _start_node_probe(self, force=False):
        """起一轮节点测速（后台线程）。

        现在的触发点只有两个：**点进这一页**（on_shown）和**手点「测速」**，
        两处都走 force=True。force=False 的分支留给"将来想按需补测"的
        调用方（比如数据从未测过时），保留是为了不把这个开关写死。
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
        v0.8.14beta 起不再自动重测，所以数据放旧时要把"怎么更新"一起说出来。
        """
        age = self._probe.age()
        if age is None:
            text = "还没测过，点「测速」"
        elif self._probing:
            text = "正在测速…"
        elif age > node_probe.TTL_SECONDS:
            text = "延迟数据：%s（点「测速」更新）" % node_probe.age_text(age)
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
        # 「其他游戏」没端口：按钮文案也跟着改，别让用户以为复制出来带端口
        try:
            self.btn_copy_addr.setText("复制 IP:端口" if port else "复制 IP")
        except (AttributeError, RuntimeError):
            pass
        if self._running:
            self._members_row = []          # 强制重建：贴片与复制端口都变了
            self._update_ip()
            if port:
                self._append_log("游戏快连已切换为 %s（%d），已广播给队友"
                                 % (name, port))
            else:
                self._append_log("游戏快连已切换为 %s（无端口），已广播给队友"
                                 % name)

    def _on_copy_game_addr(self):
        """游戏快连：复制「虚拟 IP:端口」直连地址（无端口游戏只给纯 IP）。"""
        ip = self._my_ip or etier.virtual_adapter_ip()
        if not ip:
            self.toast("当前没有虚拟 IP 可复制")
            return
        port = self.game_combo.currentData() or 0
        addr = game_addr(ip, port)
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

    def _servers_for_auto(self):
        """auto 模式的中继列表：用**本次测速结果**剔除实测不可用的节点。

        为什么值得做：14 个节点里常有几个是死的（2026-10 实测 3 个连不上），
        EasyTier 会对它们每 10 秒重试一次 —— 既拖慢收敛，又把日志刷满
        "connect to peer error"。测速（node_probe）本来就判出了"端口不通 /
        能 ping 不能握手"（记录里的 `ok` 字段），没理由再把它们交给 EasyTier。

        两条保守规则：
          * **没测过的节点照给** —— 信息不足就不替它下结论；
          * 筛完一个不剩就退回全量 —— 宁可多试，也不让房间起不来。
        """
        try:
            recs = self._probe.results(now=time.time(), keep_stale=True)
        except Exception:
            return list(etier.PUBLIC_SERVERS)
        if not recs:
            return list(etier.PUBLIC_SERVERS)
        bad = {r.get("addr") for r in recs.values()
               if r.get("ok") is False and r.get("error")}
        if not bad:
            return list(etier.PUBLIC_SERVERS)
        kept = [s for s in etier.PUBLIC_SERVERS if s not in bad]
        if not kept:
            return list(etier.PUBLIC_SERVERS)
        # 本方法在后台线程里被调用，不能直接碰控件 —— 走信号转发
        self._event.emit("_etier_log",
                         "已按测速剔除 %d 个不可用中继节点，本轮连 %d 个"
                         % (len(bad), len(kept)))
        return kept

    def _do_start(self, code, password, nick, node_addr=""):
        tier = etier.EasyTier(on_log=lambda m: self._event.emit("_etier_log", m))
        # 虚拟 IP 交给 EasyTier 的 DHCP 动态分配（不传 ipv4，走 -d true）：
        # 固定 IP 在"多个房间并存 / 一个房间多人"时会互相抢地址，而 DHCP
        # 遇到冲突会自动改地址。代价是 IP 不再可预测，所以成员侧的准入校验
        # 不能再 ping 固定的 10.126.126.1，改由 pick_host_ip() 动态发现。
        try:
            # 网络名与密码都走 etier 的**唯一归一化口径**（见 etier.room_key）：
            # 云盘的 token、屏幕共享的观看码都按同一口径派生，避免出现
            # "房间进得去、但云盘/看屏幕互相读不到"。
            ok, msg = tier.start(
                "yuhub-" + etier.room_key(code), etier.room_secret(password),
                timeout=20.0, hostname=nick, node=node_addr,
                # auto 模式：只把测速判定可用的节点交给 EasyTier
                servers=None if node_addr else self._servers_for_auto(),
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
            # 重新开始本轮的"连不上"诊断（新会话：EasyTier 日志从新的
            # Starting easytier 起算，提示也要重新给一遍）
            self._relay_zero_hinted = False
            self._relay_empty_hinted = False
            self._relay_st = {}
            self._link_seen = set()
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
        """停掉成员跟踪、昵称信标、core 成员表与聊天室的后台线程。

        重复调用安全（换 IP 时会先停再起一遍）。"""
        for attr in ("_tracker", "_beacon", "_core_feed"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.stop()
                except Exception:
                    pass
                setattr(self, attr, None)
        chat, self._chat = self._chat, None
        if chat is not None:
            try:
                chat.stop()
            except Exception:
                pass
        self._chat_file_ids = {}
        # 房间没了，屏幕共享那两个登记也一并撤掉（这个函数在换 IP 时也会走，
        # 换完 IP 会在 _start_room_threads 末尾重新登记）。
        screenshare.set_active_room()
        screenshare.set_port_publisher(None)

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
        beacon = self._beacon        # 闭包用局部绑定，避免捕获 self 后取到新实例
        # 🔴 权威成员表：借 easytier-cli 读 core 内部那张表（etier.CorePeerFeed）。
        # 端口用**本次房间探得的那个**——core 的 portal 起在哪儿，cli 就连哪儿
        # （见 RPC_PORT 的注释：刻意避开官方默认的 15888，免得和用户自装的
        # EasyTier 撞车把 core 顶死）。拿不到就退回默认值：自检里的假 tier
        # 没有这个方法，不该因此起不来房间。
        try:
            rpc_port = (int(self._etier.rpc_port())
                        if self._etier is not None else etier.RPC_PORT)
        except Exception:
            rpc_port = etier.RPC_PORT
        self._core_feed = etier.CorePeerFeed(rpc_port=rpc_port, on_log=log)
        self._core_feed.start()
        feed = self._core_feed
        # 成员发现的三条到场信号，**并集**：
        #   a. ARP（跟踪器内部自己扫）
        #   b. 昵称信标新鲜表——应用层广播，入站 UDP 按端口放行
        #   c. core 权威成员表——协议内数据，握手过的人在表里
        # 任何一条坏掉都还有另外两条：用户实测的"看不到好友 / 好友云盘的文件
        # 看不到"根因是只认 a，b 是应用层补丁，c 是根治。名称与链路质量也一并
        # 从 c 兜底/补充（hostname 就是用户昵称）。
        self._tracker = etier.MemberTracker(
            ip, name_lookup=beacon.get_info, on_log=log,
            alive_hints=(lambda: (beacon.live_peers() or {}).keys(),
                         lambda: (feed.by_ip() or {}).keys()),
            name_hint=feed.hostname_of,
            quality_hint=feed.quality_map)
        self._tracker.start()
        # 临时云盘与这一对同生共死（进房起、退房停、IP 变了重绑）
        self._ensure_share_hub(ip)
        # 房间聊天：文本消息 + 云盘文件消息都走虚拟网 UDP 广播（room_chat）。
        # 昵称用进房时填的那个——联机页强制必填，所以聊天室里不会出现裸 IP；
        # 消息记录经 chat_message 信号转到主线程，由屏幕共享页渲染。
        self._chat = room_chat.RoomChat(
            ip, self._host_nick or self._current_nickname(),
            on_message=lambda rec: self.chat_message.emit(rec),
            on_log=lambda m: self._event.emit("_etier_log", m))
        self._chat.start()
        self._chat_file_ids = {}
        # 登记给屏幕共享：异地共享要绑虚拟 IP，观看码由「房间码+密码」派生。
        # 放在这里而不是 _on_done，是因为虚拟 IP 变了也会重走这条路，
        # 每次都要把最新的 IP 重新登记进去。
        screenshare.set_active_room(self.code_edit.text().strip(),
                                    self.pass_edit.text(), ip)
        screenshare.set_port_publisher(self._publish_screenshare)

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
        # 异地共享是绑在虚拟网卡上的：房间一退，那张网卡就没了，共享也就
        # 名存实亡。界面上却还显示"正在共享"，用户会以为队友还能看。
        # 所以退房时顺手把它停掉（只在"共享的就是异地那条链路"时才停，
        # 局域网共享不受影响）。
        page = self._share_page()
        if page is not None and hasattr(page, "stop_room_share"):
            try:
                page.stop_room_share()
            except Exception:
                pass
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
        # 拖进去也无处可分享。这里只换外观（set_armed），**不禁用**——
        # 没进房间点上去会给一句"先点进入房间"，比一个点不动的灰块有用。
        self.drop_zone.set_armed(flag)
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

        ⚠️ 整段包 try：这一拍里任何异常都会让成员列表/云盘视图**停在半路**，
        而打包态（--windowed）没有 stderr，Qt 只在控制台打印 traceback——
        用户看到的就是"列表不动了"，毫无线索。接住并写进日志面板。
        """
        try:
            self._update_ip_inner()
        except Exception as exc:
            self._append_log("界面刷新出错（这一轮的成员/云盘视图已跳过，"
                             "下一拍会重试）：%s" % exc)

    def _update_ip_inner(self):
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
                        "self": True, "game": my_game, "port": my_port,
                        "ss": 0}]          # 自己那行的「看屏幕」没有意义
            for m in members:
                entries.append({
                    "ip": m["ip"],
                    "name": m.get("name") or "",
                    "self": False,
                    "game": m.get("game") or "",
                    "port": int(m.get("port") or 0),
                    "ss": int(m.get("ss") or 0),
                    # 链路质量（core 成员表并进来的，见 MemberTracker
                    # ._apply_quality）：没有就是空字典，行照旧渲染。
                    "core": {"lat_ms": m.get("lat_ms"),
                             "loss_rate": m.get("loss_rate") or "",
                             "tunnel": m.get("tunnel") or "",
                             "nat": m.get("nat") or "",
                             "relayed": bool(m.get("relayed"))},
                })
            # 只在"行内容真的变了"时重建控件，否则每 800ms 重建一次会闪。
            # 签名必须带上昵称与游戏快连：队友中途换了游戏，他那行也得跟着
            # 更新——只比 IP 的话，那一行会一直停在旧游戏上。ss 同理：
            # 队友开始/停止共享屏幕，按钮得跟着出现/消失。
            # v0.13 加上「是否中继」与延迟分档（5ms 一档）：中继态一变就要
            # 重画贴片，延迟在档位之间跳动时也更新（同一档内不动，免得抖动
            # 引起每秒重建）。
            sig = [(e["ip"], e["name"], e.get("game", ""),
                    int(e.get("port") or 0), int(e.get("ss") or 0),
                    bool((e.get("core") or {}).get("relayed")),
                    _lat_bucket((e.get("core") or {}).get("lat_ms")))
                   for e in entries]
            old_sig = [(r.get("ip"), r.get("name"), r.get("game", ""),
                        int(r.get("port") or 0), int(r.get("ss") or 0),
                        bool((r.get("core") or {}).get("relayed")),
                        _lat_bucket((r.get("core") or {}).get("lat_ms")))
                       for r in self._members_row]
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
                    "（端口取自他自己选的游戏；选「其他游戏」则只给纯 IP）。"
                )
                self._set_status_note(normal_note)
            else:
                st = self._relay_st or {}
                n_ok = len(st.get("relays_ok") or [])
                if n_ok:
                    extra = "（本机已连上 %d 个中继节点，链路正常）" % n_ok
                elif st.get("session"):
                    extra = ("（⚠ 一个中继节点都没连上，请在中继节点下拉里"
                             "换一个，或关掉代理/VPN 再试）")
                else:
                    extra = ""
                self.members_hint.setText(
                    "目前只有你自己。把房间码和密码发给队友，"
                    "他们加入后就会出现在这里。" + extra)
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
                    # 诊断分叉：信标到底收没收到人？这是"列表只有自己"时
                    # 最能分开两种原因的一问——收到过人说明发现链路坏了
                    # （跟踪器），没收到说明广播没到（防火墙 / 中继丢包），
                    # 两者的处置完全不同。用户反馈过这个症状，日志里必须
                    # 留得下可判定的线索。
                    try:
                        seen = sorted((self._beacon.live_peers() or {}).keys()) \
                            if self._beacon is not None else []
                    except Exception:
                        seen = []
                    if seen:
                        self._append_log(
                            "诊断：昵称信标其实收到了 %s，但成员列表没显示——"
                            "这是成员跟踪的问题，请把本日志反馈给作者。"
                            % "、".join(seen))
                    else:
                        self._append_log(
                            "诊断：昵称信标也没有收到任何成员广播。请确认队友"
                            "填的房间码/密码与你完全一致；若一致仍收不到，"
                            "可能是中继节点丢广播，换一个「中继节点」试试。")
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
            elif kind == "link_diag":
                self._on_link_diag(payload or {})
            elif kind == "relay_status":
                self._on_relay_status(payload or {})
        except Exception as exc:                     # 界面出错不该拖垮转发
            self._append_log("界面更新出错：%s" % exc)

    # -------------------------------------------------- 中继连接状态自检
    def _on_relay_status(self, st):
        """把「中继到底连上没、房间里到底有几个人」变成可执行的判断。

        这两句话以前只能靠猜，现在有事实：
          * relays_ok == 0  → 一个中继都没握手成功，房间不可能有人，
            "连不上"的原因在本机网络/节点选择上（换节点、看防火墙/代理）。
          * relays_ok > 0  但 same_net_peers == 0 → 本机链路是通的，
            问题在**对方没进来**（房间码/密码不一致、对端网络不通、
            对端 Yuhub 没能启动虚拟网卡）。
        每种只提示一次，避免每 10 秒刷屏。
        """
        self._relay_st = st
        n_ok = len(st.get("relays_ok") or [])
        n_peer = int(st.get("same_net_peers") or 0)
        others = (self._tracker.snapshot() if self._tracker else [])
        alone = not others and n_peer <= 0

        if alone and n_ok == 0 and not self._relay_zero_hinted:
            self._relay_zero_hinted = True
            self._append_log(
                "❌ 诊断：一个中继节点都没连上（%d 个失败）。虚拟局域网"
                "没有它就无法发现彼此——这就是「连不上」的原因。" % st.get("relays_failed", 0))
            self._append_log(
                "  自检：① 在「中继节点」下拉里换一个节点或选「自动」；"
                "② 若用了代理/VPN，试着关掉再进；③ 公司/校园网可能只放行 443，"
                "可选「上海中继（WSS）」或「香港中继（WS）」")
            self._set_status_note(
                "⚠ 没能连上任何中继节点，房间无法发现成员。请在中继节点下拉"
                "里换一个（或选「自动」），代理/VPN 请先关闭。")
        elif alone and n_ok > 0 and not self._relay_empty_hinted:
            self._relay_empty_hinted = True
            self._append_log(
                "诊断：已连上 %d 个中继节点（%s），本机链路正常；"
                "但同网络里还没有其他人。" % (n_ok, "、".join(st["relays_ok"][:4])))
            self._append_log(
                "  这通常是对端没进来，请让对侧确认：① 房间码和密码与"
                "当前房间完全一致（大小写/空格都会被忽略，但字符必须相同）；"
                "② 对端 Yuhub 是否以管理员身份启动并在 UAC 点了「是」；"
                "③ 对端能否打开 https://easytier.weiai.org.cn:11010 这类"
                "中继地址（网络受限时换 WSS/WS 节点）；④ 双方都是较新版本。")
            self._set_status_note(
                "已连上 %d 个中继节点，但房间里只有你自己。请让对端核对"
                "房间码/密码、以管理员启动，并确认它的网络能连上中继。" % n_ok)
        elif not alone:
            # 有人进来了：复位提示，下次再孤身一人时要重新给出判断
            self._relay_zero_hinted = False
            self._relay_empty_hinted = False


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
        # 节点延迟**不再定时重测**（v0.8.14beta）：以前数据一过期就自动重测，
        # 用户明明只是把这一页开着挂机，也会周期性地占带宽。现在只在
        # 「点进这一页」（on_shown）和「手点测速」时刷新。
        #
        # 云盘的状态看门狗在这里：只做字典比较，不发网络请求（见 _share_watch）。
        self._update_node_age()
        try:
            self._share_watch()
        except (RuntimeError, AttributeError):
            pass
        # 链路诊断：房间开着时每 10 秒扫一次 EasyTier 日志，把打洞/中继/连接
        # 错误的新行写进日志面板。异地分屏帧率低时，用户能直接看到链路状况。
        if self._running and not self._etier_busy:
            now = time.monotonic()
            if now - self._link_check_at >= 10.0:
                self._link_check_at = now
                threading.Thread(target=self._do_link_check, daemon=True).start()
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

    # ------------------------------------------------------------ 链路诊断
    def _do_link_check(self):
        """后台读一次中继状态 + EasyTier 日志（文件 IO，不占 GUI 线程）。

        **先算中继状态、先发出去**：日志面板要不要把"连不上某节点"当红叉，
        取决于"这会儿到底连上几个中继"，顺序反了就会先按 0 个渲染成 ❌。
        """
        try:
            st = etier.relay_status()
        except Exception:
            st = None
        if st and st.get("session"):
            self._event.emit("relay_status", st)
        try:
            info = etier.link_diagnostics()
        except Exception:
            info = None
        if info:
            self._event.emit("link_diag", info)

    def _on_link_diag(self, info):
        """把链路线索写进日志面板（**同一件事只写一次**）。

        噪音治理（2026-10-19 用户实测"进房间满屏 ❌"，而当时已连上 10 个
        中继、链路完全正常）：
          ① 去重键改用 `_normalize_diag(line)` —— 剥掉时间戳再比。原来按
             整行去重，而 EasyTier 每 10 秒重试一次、每次只有时间戳不同，
             于是永远命中不了、同一条错误刷不完；
          ② **已经连上中继时**，单个节点连不上只是"冗余节点在重试"，不是
             故障 —— 降级成 ℹ 提示，不再打红叉吓人；
          ③ 多行 Rust 错误的续行残片（不带时间戳）直接丢弃。
        """
        if not isinstance(info, dict):
            return
        n_ok = len((self._relay_st or {}).get("relays_ok") or [])
        labels = (("error", "❌ 连接问题"),
                  ("relay", "🔁 中继相关"),
                  ("punch", "🎯 打洞"))
        for key, label in labels:
            for line in (info.get(key) or []):
                raw = (line or "").strip()
                norm = _normalize_diag(raw)
                if not norm or norm in self._link_seen:
                    continue
                self._link_seen.add(norm)
                display = _DIAG_TS.sub("", raw, count=1)[:160]
                if key == "error" and n_ok > 0:
                    self._append_log(
                        "ℹ 有中继节点连不上（已连上 %d 个，不影响使用）：%s"
                        % (n_ok, display))
                else:
                    self._append_log("%s：%s" % (label, display))

    def on_shown(self):
        """页面被切到前台。

        中继节点延迟**每次进这一页都自动测一次**：延迟取决于你现在在哪、
        网络好不好，隔一会儿就变；进门顺手量一次，用户看到的永远是"现在
        的"数字。之后这一页开着就不再自动重测了（想更新点「测速」），
        免得挂机时白白占带宽。
        """
        self._tick()
        try:
            self._start_node_probe(force=True)
        except (RuntimeError, AttributeError):
            pass

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
