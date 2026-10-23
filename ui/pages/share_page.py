# -*- coding: utf-8 -*-
"""屏幕共享页：共享我的屏幕 / 看别人的屏幕。

引擎在 `screenshare.py`（纯标准库：GDI 抓屏 + TCP 收发 + 协议），本页只做
三件事：

  * 把"共享什么 / 多清晰"变成抓屏线程的参数；
  * 把引擎回调（发生在收发线程里）用 Qt 信号搬回主线程刷界面；
  * 把收到的帧解成图片贴进画面区。

界面全部用 Yuhub 自己的控件与主题色，**不嵌任何浏览器内核** —— 这正是这一版
把 exe 从 270MB 压回 65MB 的关键（上一版内嵌 QtWebEngine，光那个 DLL 就
203MB，还要连带 cloudflared 与 piik-app 一共再多背 100MB）。

两条链路
--------
局域网共享   绑本机局域网 IP，观看码是随机 6 位数，念给朋友就行。
异地共享     绑「异地联机」的虚拟网卡 IP，观看码由「房间码 + 密码」派生 ——
             同一个房间的人算出来必然一致，所以在成员列表里点一下「看屏幕」
             就能直接连上，不用手输。
两条链路走的是**同一套协议**，区别只在绑哪张网卡。

观众侧也要装 Yuhub：这是"不打开浏览器 + 体积压到 100MB 以内"必然的取舍。
换来的是画面完全不经过任何第三方服务器（局域网直连 / EasyTier 打洞与中继）。
"""

import os
import threading
import time
import zlib

from PySide6.QtCore import (
    QBuffer,
    QIODevice,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import lan_share
import screenshare
from .. import motion, theme
from ..widgets import danger_button, ghost_button, primary_button
from .base_page import BasePage

try:                      # H.264 组件可能未内置（见 h264codec 模块注释）
    import h264codec
except Exception:         # noqa: BLE001
    h264codec = None

try:                      # 组件按需下载（与 EasyTier 组件同款机制）
    import ffmpeg_pack
except Exception:         # noqa: BLE001
    ffmpeg_pack = None

#: 抓屏线程两次投递本机预览的最小间隔（秒）。
#: 预览只是给共享者"确认发出去的是什么"，不需要跟着 12fps 一起刷——
#: 每帧都往主线程发一张图，等于把工作线程省下来的时间又花回去。
PREVIEW_INTERVAL = 0.25

#: 显示器几何的缓存时长（秒）。见 `_Sender._resolve`：不缓存的话每帧都要
#: 枚举一次显示器（30fps = 每秒 30 次），而显示器几何只在改分辨率/热插拔时变。
SCREEN_RESOLVE_TTL = 1.0

#: 聊天气泡的最大宽度（像素）。聊天卡大约是底部行的一半宽，360 是
#: "长句能换行、短行不占满"的折中。
CHAT_BUBBLE_MAX_W = 360

#: 头像底色板：按昵称哈希**稳定**取色（同一个人永远同一个色，换设备也一样），
#: 每项是 (浅色主题, 深色主题)。水墨主题本身是单色的，身份色靠这一小组
#: 低饱和色区分——既不会破坏纸墨观感，又让"谁在说话"一眼可辨。
_AVATAR_COLORS = (
    ("#3f6ea8", "#7db2e6"),   # 靛蓝
    ("#2f7d6b", "#6fc7b2"),   # 青绿
    ("#9a6b2f", "#e0b06a"),   # 琥珀
    ("#8a4a6b", "#d99ec0"),   # 玫红
    ("#5a6b3a", "#a8c47a"),   # 橄榄
    ("#5b5aa8", "#a3a2e8"),   # 紫
)

#: 文件卡色块：按扩展名分类取色（浅色, 深色）。压缩包/程序/媒体/文档
#: 各一组，其余归"其他"。色块上写扩展名，扫一眼就知道是什么文件。
_FILE_COLORS = {
    "archive": ("#9a6b2f", "#e0b06a"),
    "app":     ("#3f6ea8", "#7db2e6"),
    "media":   ("#8a4a6b", "#d99ec0"),
    "doc":     ("#2f7d6b", "#6fc7b2"),
    "other":   ("#6b6b66", "#9a9a94"),
}
_FILE_EXTS = {
    "archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"},
    "app": {"exe", "msi", "bat", "cmd", "ps1", "apk", "dmg", "appimage"},
    "media": {"mp4", "mkv", "avi", "mov", "mp3", "flac", "wav",
              "png", "jpg", "jpeg", "gif", "webp", "bmp"},
    "doc": {"txt", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
            "md", "log", "json", "csv"},
}


def _avatar_color(name):
    """按昵称稳定取一个头像底色（同昵称同色）。"""
    idx = zlib.crc32((name or "?").encode("utf-8")) % len(_AVATAR_COLORS)
    return _AVATAR_COLORS[idx][1 if theme.get_theme() == "dark" else 0]


def _file_kind(name):
    """按扩展名判断文件类别（用于色块配色）。"""
    ext = os.path.splitext(str(name or ""))[1].lstrip(".").lower()
    for kind, exts in _FILE_EXTS.items():
        if ext in exts:
            return kind, ext
    return "other", ext


def _chat_time(ts):
    """记录里的时间戳 → 'HH:MM'（没有就空串）。"""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts or 0)))
    except (TypeError, ValueError, OSError):
        return ""


def room_chat_text_max():
    """输入框长度上限：与引擎口径（room_chat.TEXT_MAX）一致。

    本页不直接 import room_chat——聊天消息由联机页转发（见 _lan_page），
    这里只为拿一个常量做本地导入，拿不到就退回 300（与引擎默认值相同）。
    """
    try:
        import room_chat
        return int(room_chat.TEXT_MAX)
    except Exception:
        return 300


class _Bridge(QObject):
    """把工作线程里的事件搬回主线程（跨线程直接碰控件是 Qt 的经典崩法）。

    两个信号分开是有原因的：`event` 走的是**画面数据**（预览的 QImage、
    收到的 JPEG 字节），频率高、负载大；`engine` 走的是**状态事件**
    （谁进来了 / 被拒了 / 断开了），频率低但每一条都会改界面。混在一个
    信号里就得靠负载类型去分辨，容易看错。
    """

    event = Signal(str, object)
    engine = Signal(str, object)


class _Mailbox:
    """只能装一件东西的邮箱：放新的就把旧的顶掉（单槽，不是队列）。

    抓屏比编码慢，正常不会堆积；但编码一旦被系统卡住，**队列**会让画面
    越来越滞后（延迟一直累积），而"看直播"这件事里"最新的那一帧"远比
    "一帧都不丢"重要。所以只留一格。
    """

    __slots__ = ("_cv", "_item", "_rev")

    def __init__(self):
        self._cv = threading.Condition()
        self._item = None
        self._rev = 0

    def put(self, item):
        with self._cv:
            self._item = item
            self._rev += 1
            self._cv.notify()

    def wake(self):
        """叫醒正在等的消费者（停止时用，省得它多睡一个超时）。"""
        with self._cv:
            self._cv.notify_all()

    def get(self, after, timeout):
        """取比 `after` 新的一件；没有更新就等到超时，返回 (None, after)。"""
        with self._cv:
            if self._rev == after:
                self._cv.wait(timeout)
            if self._rev == after:
                return None, after
            return self._item, self._rev


class _Sender:
    """抓屏 → 缩放 → JPEG → 交给服务端广播，跑在两条工作线程上。

    为什么是**两条**线程而不是一条 —— 这是 30 帧能不能成立的关键：
    实测 1600px 时抓屏 18.2ms、JPEG 编码 12.3ms，串行相加 30.5ms，
    理论上限只有 32.8fps：数字上够 30，实际上系统随便忙一下就开始掉帧。
    把两段拆开之后，同一台机器上吞吐取两者最大值（54~60fps），30 帧才有
    真正的余量。顺带另一个好处：本机预览不再被编码阻塞，画面更跟手。

    顺带一提为什么不用 Qt 的 `QScreen.grabWindow`：那个必须在主线程调用，
    整屏单帧 46.7ms，共享期间界面肉眼可见地卡；GDI 这条路只要 18ms，
    而且能完全离开主线程，界面一帧都不占。
    """

    def __init__(self, server, bridge):
        self.server = server
        self.bridge = bridge
        self.size = (0, 0)
        self.error = ""
        self._mail = _Mailbox()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._target = ("screen", 0)
        self._quality = screenshare.DEFAULT_QUALITY
        self._geom_cache = None      # (key, ts, rect)：显示器几何缓存（见 _resolve）
        # ---- 编码与自适应（2026-10-11）----
        #: 用不用 H.264（由页面按"组件可用 + 对端都会解"设置）
        self._use_h264 = False
        #: 给编码器的帧率提示（**不是**抓屏节奏：帧率不再设上限）
        self._fps_hint = 30
        #: 编码后的实际格式名（界面显示用）
        self.codec_name = "jpeg"
        #: 自适应码率控制器：按送达速率调 target，编码线程每秒读一次
        preset = screenshare.QUALITY_PRESETS[
            min(screenshare.DEFAULT_QUALITY,
                len(screenshare.QUALITY_PRESETS) - 1)]
        self.adaptive = screenshare.AdaptiveBitrate(
            preset[2], log=lambda m: self.bridge.event.emit(
                "codec_log", {"msg": m}))
        self._cap = threading.Thread(target=self._capture_loop,
                                     name="share-capture", daemon=True)
        self._enc = threading.Thread(target=self._encode_loop,
                                     name="share-encode", daemon=True)

    # ------------------------------------------------------------ 外部接口
    def set_target(self, target):
        with self._lock:
            self._target = target

    def set_use_h264(self, flag):
        """页面决定这一轮用不用 H.264（对端能力会变，所以要能动态切）。"""
        with self._lock:
            self._use_h264 = bool(flag)

    def set_preset(self, index):
        """换画质挡位：目标码率跟着换，并把自适应控制器重置到新基准。"""
        index = max(0, min(index, len(screenshare.QUALITY_PRESETS) - 1))
        with self._lock:
            self._quality = index
        target = screenshare.QUALITY_PRESETS[index][2]
        self.adaptive.reset(target)

    def start(self):
        # 先起编码线程：抓屏线程一开跑就会往邮箱里塞东西，消费者得在位。
        self._enc.start()
        self._cap.start()

    def stop(self):
        self._stop.set()
        self._mail.wake()            # 让编码线程立刻从等待里醒过来
        for t in (self._cap, self._enc):
            if t.is_alive():
                t.join(2.0)

    # -------------------------------------------------------------- 抓屏线程
    def _resolve(self, target):
        """把"共享什么"解析成屏幕坐标上的一个矩形。"""
        kind, key = target
        if kind == "screen":
            # 显示器几何极少变化（改分辨率 / 热插拔），没必要每帧都枚举一遍：
            # 30fps 就是每秒 30 次 EnumDisplayMonitors。缓存 1 秒，既省掉这些
            # 无用调用，改了分辨率也能在 1 秒内跟上。
            now = time.monotonic()
            cached = self._geom_cache
            if cached is not None and cached[0] == key \
                    and now - cached[1] < SCREEN_RESOLVE_TTL:
                return cached[2]
            screens = screenshare.list_screens()
            if not screens:
                return None
            s = screens[min(key, len(screens) - 1)]
            geom = (s["x"], s["y"], s["w"], s["h"])
            self._geom_cache = (key, now, geom)
            return geom
        # 窗口目标**不缓存**：窗口随时会被拖动/缩放，缓存一帧的代价就是
        # 画面上跟着慢半拍（拖窗口时肉眼可见）。
        return screenshare.window_rect(key)

    def h264_ready(self):
        """本机能不能用 H.264（组件在 + 编解码器可用）。"""
        if h264codec is None:
            return False
        try:
            return bool(h264codec.available())
        except Exception:
            return False

    def _ensure_encoder(self, enc, w, h):
        """按需创建/重建 H.264 编码器（分辨率变了就得重建）。"""
        if not self.h264_ready():
            return None
        if enc is not None and enc.w == w and enc.h == h:
            return enc
        if enc is not None:
            try:
                enc.close()
            except Exception:
                pass
        try:
            enc = h264codec.H264Encoder(
                w, h, fps=max(1, int(self._fps_hint)),
                bitrate=int(self.adaptive.target), threads=2,
                log=lambda m: self.bridge.event.emit(
                    "codec_log", {"msg": m}))
            self.codec_name = enc.name
        except Exception as exc:                        # noqa: BLE001
            self.bridge.event.emit("codec_log",
                                   {"msg": "H.264 编码器不可用：%s" % exc})
            return None
        return enc

    def _capture_loop(self):
        cap = screenshare.GdiCapture()
        nxt = time.monotonic()
        last_preview = 0.0
        last_beat = 0.0            # 抓屏失败时的保活心跳（见下）
        misses = 0
        miss_since = 0.0
        try:
            while not self._stop.is_set():
                with self._lock:
                    target = self._target
                    index = self._quality
                index = max(0, min(index, len(screenshare.QUALITY_PRESETS) - 1))
                _name, max_edge, _target_bps = \
                    screenshare.QUALITY_PRESETS[index]
                # 帧率**不设上限**（用户要求）：抓屏与编码能跑多快就多快，
                # 真正限制帧率的只有链路带宽（H.264 由自适应码率控制）。
                # 循环节奏交给抓屏+编码自身的耗时。
                interval = 0.0
                self._fps_hint = 30        # 只作为编码器的时基提示

                rect = self._resolve(target)
                if rect is None or rect[2] <= 0 or rect[3] <= 0:
                    misses += 1
                    if not miss_since:
                        miss_since = time.monotonic()
                    # 共享的窗口被关掉了：连着几秒都解析不出矩形就认输，
                    # 继续空转只会让用户以为"还在共享"。
                    # （不能再用"帧数"判：帧率不限之后每帧耗时可快可慢，
                    #   按帧数算会一会儿就误判或等很久，所以改用时间。）
                    if misses >= 3 and time.monotonic() - miss_since >= 3.0:
                        if (target[0] == "window"
                                and screenshare.window_minimized(target[1])):
                            # 最小化要给**准确的说法**：否则用户看到的是
                            # "共享的内容不见了（窗口被关闭了？）"，而窗口
                            # 明明还在任务栏里躺着。
                            self.error = ("被共享的窗口已最小化，抓不到画面"
                                          "（请还原窗口，或改共享整个屏幕）")
                        else:
                            self.error = "共享的内容不见了（窗口被关闭了？）"
                        break
                else:
                    misses = 0
                    miss_since = 0.0
                    out_w, out_h = cap.fit(rect[2], rect[3], max_edge)
                    raw = cap.grab(rect, out_w, out_h)
                    if raw:
                        self.size = (out_w, out_h)
                        last_beat = 0.0
                        # 有人在看才把帧交给编码线程。没人看的时候抓屏照跑
                        # （本机预览要用它），但没必要白占编码线程的 CPU。
                        if self.server.viewers:
                            self._mail.put((raw, out_w, out_h))
                        now = time.monotonic()
                        if now - last_preview >= PREVIEW_INTERVAL:
                            last_preview = now
                            img = QImage(raw, out_w, out_h, out_w * 4,
                                         QImage.Format.Format_RGB32)
                            # 必须 copy()：这个 QImage 只是包着 raw 那块内存，
                            # 而 raw 在下一轮循环就被换掉了。Qt 信号是隐式
                            # 共享，排进主线程队列的仍是同一块内存 —— 不拷贝
                            # 的话界面会去读已经释放的内存（表现为随机花屏或
                            # 直接崩）。预览只有 4 次/秒，拷一次的代价可忽略。
                            self.bridge.event.emit("preview", img.copy())
                    elif self.server.viewers:
                        # 抓屏拿不到画面（锁屏 / 切到安全桌面 / 显示器休眠 /
                        # 独占全屏）：**什么都不发**的话，对端要等
                        # RECV_TIMEOUT×3 才判死并报"连接已断开"，用户看到的
                        # 是"画面卡住，然后莫名其妙断开"。发心跳保活，
                        # 让对方知道"连接还在，只是暂时没画面"。
                        now = time.monotonic()
                        if now - last_beat >= screenshare.HEARTBEAT_INTERVAL:
                            last_beat = now
                            try:
                                self.server.push(None, *self.size)
                            except Exception:
                                pass

                nxt += interval
                delay = nxt - time.monotonic()
                if interval > 0 and delay < -interval:   # 落后超过一帧就重新对表
                    nxt = time.monotonic()
                    delay = 0.0
                if delay > 0:
                    self._stop.wait(delay)
                elif not self.server.viewers:
                    # 没人观看时**不空转**：预览本来只要 4 次/秒，抓屏（18ms）
                    # 全速跑纯属白烧 CPU。给 100ms 的节奏（约 10fps），
                    # 有人连进来立刻恢复全速。
                    self._stop.wait(0.1)
        except Exception as exc:               # 抓屏线程绝不能静默死掉
            self.error = "抓屏中断（%s）" % exc
        finally:
            cap.close()
            self.bridge.event.emit("sender_stopped", {"error": self.error})

    # -------------------------------------------------------------- 编码线程
    def _encode_loop(self):
        """编码线程：**只有 H.264 一条路**（2026-10-11 起 JPEG 编码已删除）。

        生命周期：
          * 组件未就绪（首次共享时还在下载）→ 只发心跳保活，装好后下一帧
            自动开始编码（`_use_h264` 由 pack_done 事件置位）。
          * 分辨率变了 → 重建编码器（重建自带关键帧，解码端自动恢复）。
          * 编码失败（驱动重置/显存耗尽…）→ 关掉编码器重试，**连续 3 次**
            失败才停共享 —— 一次瞬时的驱动抖动不该终结整场共享。
        """
        rev = 0
        last_sig = None
        last_real = 0.0
        last_beat = 0.0
        encoder = None
        encode_fails = 0
        try:
            while not self._stop.is_set():
                item, rev = self._mail.get(rev, 0.3)
                if item is None:
                    continue
                raw, out_w, out_h = item
                now = time.monotonic()
                # 采样签名判断画面变了没有。为什么不逐字节比较，见
                # `screenshare.SAMPLE_STEP` 的注释：真实桌面上总有个 20×20
                # 的小块在闪，逐字节比**从不命中**，这条优化等于白写。
                sig = screenshare.frame_signature(raw)
                unchanged = (sig == last_sig
                             and now - last_real < screenshare.IDLE_REFRESH)
                if unchanged or not self._use_h264:
                    # 画面没变 / 组件还没就绪（正在下载）：只发心跳保活。
                    # H.264 的帧间预测下"没变"本来就该是零成本，跳过编码
                    # 既省 CPU 也省带宽；心跳按固定间隔发就够，跟着满帧率
                    # 发只会把待发帧挤出邮箱。
                    if now - last_beat >= screenshare.HEARTBEAT_INTERVAL:
                        last_beat = now
                        self.server.push(None, out_w, out_h, kind="idle")
                    continue
                last_sig = sig
                last_real = now

                encoder = self._ensure_encoder(encoder, out_w, out_h)
                if encoder is None:
                    # 组件异常且重试也没建成：发心跳撑着，等下载/重试完成
                    if now - last_beat >= screenshare.HEARTBEAT_INTERVAL:
                        last_beat = now
                        self.server.push(None, out_w, out_h, kind="idle")
                    continue
                self.codec_name = encoder.name
                # 自适应：目标码率变了就立刻应用（不必重建编码器）
                want = int(self.adaptive.target)
                if encoder.bitrate != want:
                    encoder.set_bitrate(want)
                try:
                    for pkt, is_key in encoder.encode(raw, out_w * 4):
                        self.server.push(pkt, out_w, out_h,
                                         kind="h264_key" if is_key else "h264")
                    encode_fails = 0            # 成功一帧就清零
                except Exception as exc:        # noqa: BLE001
                    encode_fails += 1
                    self.bridge.event.emit(
                        "codec_log",
                        {"msg": "H.264 编码失败（%d/3）：%s"
                                % (encode_fails, exc)})
                    try:
                        encoder.close()
                    except Exception:
                        pass
                    encoder = None
                    if encode_fails >= 3:
                        # 连续三次重建/编码都失败：如实停掉，别让用户对着
                        # 一个永远黑屏的"共享中"发呆
                        self.error = ("H.264 编码器连续失败，共享已停止"
                                      "（最后一次错误：%s）" % exc)
                        self._stop.set()
                        break
        except Exception as exc:
            self.error = "画面编码中断（%s）" % exc
            self._stop.set()
        finally:
            if encoder is not None:
                try:
                    encoder.close()
                except Exception:
                    pass



class _ScreenView(QLabel):
    """画面区：按比例缩放贴图（不变形），没有画面时显示提示文案。

    缩放结果按控件尺寸缓存 —— 尺寸没变就不重复做 Smooth 缩放，
    否则每来一帧都要在主线程上重算一次，白白吃掉工作线程省下来的时间。
    """

    #: 双击画面 = 请求全屏（页面内的那份与全屏窗口里的那份都发这个信号，
    #: 只是后者接到的是"退出全屏"）
    double_clicked = Signal()

    def __init__(self, hint=""):
        super().__init__()
        self.setObjectName("ScreenView")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 最小高度给小：三区块布局下画面区与底部两张卡要能同时放下，
        # 小窗口时宁可画面矮一点（纵向布局交给拉伸，不再强制 280）。
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self._pix = None
        self._scaled = None
        self._scaled_for = None
        self._hint = hint
        self.setText(hint)

    def show_frame(self, pix):
        self._pix = pix
        self._scaled_for = None
        self._refresh()

    def set_hint(self, hint):
        self._hint = hint
        if self._pix is None:
            self.setText(hint)

    def clear(self, hint=None):
        self._pix = None
        self._scaled = None
        self._scaled_for = None
        if hint is not None:
            self._hint = hint
        self.setPixmap(QPixmap())
        self.setText(self._hint)

    def _refresh(self):
        if self._pix is None or self._pix.isNull():
            return
        size = self.size()
        key = (size.width(), size.height())
        if self._scaled is not None and self._scaled_for == key:
            self.setPixmap(self._scaled)       # 尺寸没变，直接复用上次的缩放结果
            return
        self._scaled = self._pix.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
        self._scaled_for = key
        self.setPixmap(self._scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scaled_for = None
        self._refresh()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class FullScreenViewer(QWidget):
    """全屏观看：一块纯黑的顶层无边框窗口，整块屏只放画面。

    为什么不直接 `MainWindow.showFullScreen()` —— 主窗口有标题栏、侧栏和
    卡片留白，最大化以后画面周围仍有一圈非画面区域；而且用户全屏看完想回
    列表时，主窗口的窗口状态不该被程序改掉。

    交互刻意做得很薄（全屏里不该有控件挡着画面）：
      * `Esc` / `F11` / 双击画面 → 退出全屏
      * 鼠标一动浮出「怎么退出」的提示，`TIP_MS` 后淡出（不抢焦点、不吃鼠标）

    做成顶层窗口而不是内嵌到页面里，还有个副作用是好的：它**不属于主窗口
    的布局**，所以主窗口缩放、切页都不会带着它一起动。
    """

    #: 退出全屏时发一次，页面据此把引用清掉
    closed = Signal()

    TIP_TEXT = "按 Esc 或双击画面退出全屏"
    TIP_MS = 2500

    def __init__(self, hint=""):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.FramelessWindowHint)
        # 不随 close 销毁：页面会把同一个实例留着复用（退出全屏只是 hide），
        # 省掉"每次全屏都重建窗口 + 重算样式"的开销。
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet("QWidget { background: #000000; }")
        # ⚠️ 两个都不能省：QWidget 默认 FocusPolicy 是 NoFocus，键盘事件**根本
        # 不会送到这个窗口**（Esc 会失效）；默认也不跟踪鼠标移动（MouseMove
        # 只在按住键时才发），提示条就永远浮不出来。
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        self._view = _ScreenView(hint)
        # 画面区铺满整个窗口，鼠标其实一直在它上面 —— 它不转发移动事件的话，
        # 上一条 mouseTracking 就等于没开。
        self._view.setMouseTracking(True)
        # 全屏窗口是独立顶层窗口，**不继承 SharePage 的样式表**，得自己写
        # 一份：没有画面时要能看清提示文字，所以给一个灰蓝而不是纯白。
        self._view.setStyleSheet(
            "QLabel#ScreenView { background: transparent; border: none;"
            " color: #97a3b4; font-size: 13px; }")
        self._view.double_clicked.connect(self.close)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        box.addWidget(self._view)

        self._tip = QLabel(self.TIP_TEXT, self)
        self._tip.setStyleSheet(
            "QLabel { background: rgba(0, 0, 0, 190); color: #ffffff;"
            " padding: 6px 12px; border-radius: 5px; font-size: 12px; }")
        # 提示条只负责看，不能被点到，也不能把键盘焦点抢走
        self._tip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._eff = QGraphicsOpacityEffect(self._tip)
        self._tip.setGraphicsEffect(self._eff)
        self._fade = QPropertyAnimation(self._eff, b"opacity", self)
        self._fade.setDuration(motion.PAGE_IN)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._tip.hide)
        self._tip.hide()

    # ------------------------------------------------------------------ 对外
    def show_frame(self, pix):
        self._view.show_frame(pix)

    def set_hint(self, hint):
        self._view.set_hint(hint)

    def clear(self, hint=None):
        self._view.clear(hint)

    def open_on(self, screen=None):
        """铺满 `screen`（默认交给 Qt 按窗口当前位置决定）。"""
        if screen is not None:
            # 先挪到目标屏，`showFullScreen()` 才会铺满那一块 —— 多显示器
            # 下不这么做会跑到主屏上，让人以为窗口"跳走了"。
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self._flash_tip()

    # ------------------------------------------------------------------ 内部
    def _flash_tip(self):
        """浮出提示再自动淡出（鼠标每动一下就重来一遍）。"""
        self._place_tip()
        self._tip.show()
        self._eff.setOpacity(1.0)
        self._fade.stop()
        self._fade.start()

    def _place_tip(self):
        self._tip.adjustSize()
        margin = 24
        x = max(margin, self.width() - self._tip.width() - margin)
        self._tip.move(x, margin)

    # ------------------------------------------------------------------ 事件
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_tip()

    def mouseMoveEvent(self, event):
        self._flash_tip()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        # 点一下就把焦点收回来 —— 全屏期间焦点可能被别的窗口抢走（比如弹了
        # 一条系统通知），不抢回来的话 Esc 会失灵，而用户第一反应就是按 Esc。
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.close()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        # Esc 是所有人的第一直觉；F11 是"全屏"在老软件里的通用键，顺手也认了。
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_F11):
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # 先停动画：淡出回调会在窗口隐藏后继续跑，碰到已隐藏的提示条虽然不会
        # 崩，但会让下次全屏时提示条的初始透明度不确定。
        self._fade.stop()
        self._tip.hide()
        super().closeEvent(event)
        self.closed.emit()


def _label(text, obj="Muted"):
    lbl = QLabel(text)
    lbl.setObjectName(obj)
    return lbl


class SharePage(BasePage):
    TINT_KEY = "tile_2"
    BADGE = "内测"

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "屏幕共享",
            "把屏幕或某个窗口实时共享给另一台装了 Yuhub 的电脑——局域网直连，"
            "异地走「异地联机」的虚拟局域网，全程不开浏览器。",
            "🖥️",
            notify,
            parent,
        )
        self._server = None
        self._sender = None
        self._client = None
        self._decoder = None        # h264codec.H264Decoder（接收线程独占）
        self._codec_note = ""       # 当前编码方式说明（界面显示）
        self._auto_downgraded = None  # 已自动降到的挡位（防重复降）
        self._pack_fetching = False  # H.264 组件下载进行中（防重复触发）
        self._full = None            # 全屏观看窗口（懒建，退出只是 hide）
        self._mode = ""
        self._last_pix = None
        self._targets_loaded = False
        # 房间聊天（引擎在 room_chat.py，UI 在这里；消息由联机页转发过来）
        self._chat_enabled = False   # 是否在「异地联机」的房间里
        self._chat_connected = False # 聊天信号是否已接到联机页（只接一次）
        self._chat_rows = {}          # 消息 id -> {"row","name_lbl","btn"}
        # fid -> 该文件消息的气泡控件引用（下载进度要就地改这一行的文字）。
        # 与 _chat_rows 分开：_chat_rows 按**消息 id**索引（撤回用），这里按
        # **文件 id** 索引（下载进度用），同一条消息在两个表里各有一份引用。
        self._file_refs = {}
        self._send_meter = screenshare.RateMeter()
        self._recv_meter = screenshare.RateMeter()
        # 送达口径（per-viewer 在 sendall 成功后才计数）：与"发出"对照，
        # 一眼看出瓶颈是不是链路。见 `_tick` 的注释。
        self._deliver_meter = screenshare.RateMeter()

        self._bridge = _Bridge()
        self._bridge.event.connect(self._on_engine)
        self._bridge.engine.connect(self._on_engine)

        # 本页不用 BasePage 的滚动区（水墨重做后是三区块布局，见下）：
        # 画面区要吃掉全部剩余高度，塞进 QScrollArea 会变成
        # "画面里再套一个滚动条"的怪样子。
        self.scroll.setVisible(False)
        root = self.layout()
        root.addSpacing(2)
        # 上：共享画面（吃掉全部剩余高度）
        root.addWidget(self._build_view_card(), 1)
        root.addSpacing(10)
        # 下：左 = 聊天（预留） ｜ 右 = 共享设置
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        bottom.addWidget(self._build_chat_card(), 1)
        bottom.addWidget(self._build_settings_card())
        root.addLayout(bottom)

        self._restyle()
        self._apply_state()
        self._apply_chat_state()
        theme.bus.changed.connect(lambda _: self._restyle())

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------------ 构建
    def _build_view_card(self):
        """上半区块：共享画面 + 状态行。"""
        holder = QFrame()
        holder.setObjectName("Card")
        box = QVBoxLayout(holder)
        box.setContentsMargins(12, 10, 12, 12)
        box.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._lbl_state = _label("● 未共享")
        top.addWidget(self._lbl_state)
        self._lbl_stats = _label("", "Faint")
        self._lbl_stats.setWordWrap(False)
        top.addWidget(self._lbl_stats, 1)
        # 全屏入口做两个：一个按钮（看得见、找得到），一个双击画面
        # （看视频的人的本能动作）。按钮在没在观看时是灰的 —— 全屏看的是
        # 对方的画面，共享端自己的预览全屏没有意义。
        self._btn_full = ghost_button("全屏观看")
        self._btn_full.setToolTip(
            "整块屏只放画面。按 Esc 或双击画面退出；只用来观看对方的屏幕。")
        self._btn_full.clicked.connect(self._open_full)
        top.addWidget(self._btn_full)
        box.addLayout(top)

        self._view = _ScreenView(
            "还没开始。\n\n共享时这里显示本机正要发出去的画面；"
            "观看时显示对方的屏幕。\n共享与观看的入口在右下方的「共享设置」。")
        self._view.double_clicked.connect(self._open_full)
        box.addWidget(self._view, 1)
        return holder

    def _build_chat_card(self):
        """左下区块：房间聊天（文本消息 + 云盘文件消息）。

        只在「异地联机」的房间里可用：聊天走虚拟网 UDP 广播（room_chat），
        昵称来自进房时必填的那个——联机页强制有昵称才能进房，所以聊天室里
        不会出现裸 IP。文件消息由临时云盘产生：谁分享/取消分享，聊天室里
        就多一条带下载图标的消息 / 把那条标成「已撤回」。
        """
        card = QFrame()
        card.setObjectName("Card")
        card.setMinimumHeight(240)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("聊天")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        self._chat_badge = QLabel("未进入房间")
        self._chat_badge.setObjectName("Badge")
        head.addWidget(self._chat_badge)
        head.addStretch(1)
        v.addLayout(head)

        # 消息列表：滚动区 + 竖向布局。行是懒加载的小控件，撤回时**原地**
        # 改那一行（整表重建会丢滚动位置，还会让正在选的文字断掉）。
        self._chat_scroll = QScrollArea()
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._chat_scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self._chat_list = QVBoxLayout(inner)
        self._chat_list.setContentsMargins(0, 0, 0, 0)
        self._chat_list.setSpacing(6)
        # 空态提示放在最上面，stretch 垫底——消息插到 stretch 之前即可
        self._chat_empty = QLabel()
        self._chat_empty.setObjectName("Faint")
        self._chat_empty.setWordWrap(True)
        self._chat_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chat_list.addWidget(self._chat_empty)
        self._chat_list.addStretch(1)
        self._chat_scroll.setWidget(inner)
        v.addWidget(self._chat_scroll, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("进入异地联机的房间后，这里就能聊天…")
        self._chat_input.setMaxLength(room_chat_text_max())
        self._chat_input.returnPressed.connect(self._send_chat)
        self._chat_send = primary_button("发送")
        self._chat_send.setCursor(Qt.PointingHandCursor)
        self._chat_send.setFixedHeight(30)
        self._chat_send.clicked.connect(self._send_chat)
        row.addWidget(self._chat_input, 1)
        row.addWidget(self._chat_send)
        v.addLayout(row)
        return card

    def _build_settings_card(self):
        """右下区块：开始共享 / 清晰度 / 共享码 / 观看入口。"""
        card = QFrame()
        card.setObjectName("Card")
        card.setFixedWidth(410)
        card.setMinimumHeight(240)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("共享设置")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        port_lbl = QLabel("端口")
        port_lbl.setObjectName("Faint")
        head.addWidget(port_lbl)
        self._lbl_port = _label("--", "Faint")
        head.addWidget(self._lbl_port)
        v.addLayout(head)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        # 只有一个"开始共享"：走局域网还是走异地，由「本机地址」
        # 里选中哪块网卡决定（选虚拟网卡就是异地，选真实网卡就是局域网）。
        # 分成两个按钮的时候，用户得自己保证"选的地址"和"点的按钮"是一对，
        # 选错就出现"绑着局域网 IP 却按了异地共享"这种自相矛盾的状态。
        self._btn_start = primary_button("开始共享")
        self._btn_start.clicked.connect(self._start_host)
        self._btn_stop = danger_button("停止共享")
        self._btn_stop.clicked.connect(self._stop_host)
        btns.addWidget(self._btn_start, 1)
        btns.addWidget(self._btn_stop, 1)
        v.addLayout(btns)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        self._target_combo = QComboBox()
        self._target_combo.setMinimumWidth(200)
        self._btn_targets = ghost_button("刷新")
        self._btn_targets.clicked.connect(self._reload_targets)
        tbox = QHBoxLayout()
        tbox.setSpacing(6)
        tbox.addWidget(self._target_combo, 1)
        tbox.addWidget(self._btn_targets)
        grid.addWidget(_label("共享内容"), 0, 0)
        grid.addLayout(tbox, 0, 1)

        self._quality_combo = QComboBox()
        for i, (name, edge, bitrate) in enumerate(
                screenshare.QUALITY_PRESETS):
            self._quality_combo.addItem("%s（%dpx）" % (name, edge))
            self._quality_combo.setItemData(
                i,
                "目标码率约 %.1f Mbps（自适应会按实测带宽上下浮动）。\n"
                "上行带宽参考：%s。\n"
                "帧率不设上限——能跑多快取决于链路带宽与机器性能。"
                % (bitrate / 1e6, screenshare.QUALITY_BANDWIDTH[i]),
                Qt.ToolTipRole)
        self._quality_combo.setCurrentIndex(screenshare.DEFAULT_QUALITY)
        self._quality_combo.currentIndexChanged.connect(self._on_quality)
        grid.addWidget(_label("清晰度"), 1, 0)
        grid.addWidget(self._quality_combo, 1, 1)

        self._addr_combo = QComboBox()
        # 选哪块网卡 = 走哪条链路，所以换地址要立刻把提示文案改过来。
        self._addr_combo.currentIndexChanged.connect(self._on_addr_changed)
        grid.addWidget(_label("本机地址"), 2, 0)
        grid.addWidget(self._addr_combo, 2, 1)

        self._token_edit = QLineEdit()
        self._token_edit.setReadOnly(True)
        self._token_edit.setPlaceholderText("开始共享后生成")
        self._btn_copy = ghost_button("复制")
        self._btn_copy.clicked.connect(self._copy_token)
        self._btn_copy.setEnabled(False)
        kbox = QHBoxLayout()
        kbox.setSpacing(6)
        kbox.addWidget(self._token_edit, 1)
        kbox.addWidget(self._btn_copy)
        grid.addWidget(_label("观看码"), 3, 0)
        grid.addLayout(kbox, 3, 1)
        v.addLayout(grid)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setObjectName("Separator")
        v.addWidget(sep)

        wrow = QHBoxLayout()
        wrow.setSpacing(6)
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("对方的地址，如 192.168.1.5 或 10.126.126.7:45890")
        wrow.addWidget(self._host_edit, 1)
        v.addLayout(wrow)
        wrow2 = QHBoxLayout()
        wrow2.setSpacing(6)
        self._code_edit = QLineEdit()
        self._code_edit.setPlaceholderText("观看码")
        self._code_edit.setMaximumWidth(140)
        self._btn_watch = primary_button("开始观看")
        self._btn_watch.clicked.connect(self._start_watch)
        self._btn_unwatch = ghost_button("停止观看")
        self._btn_unwatch.clicked.connect(self._stop_watch)
        self._btn_unwatch.setEnabled(False)
        wrow2.addWidget(self._code_edit)
        wrow2.addWidget(self._btn_watch)
        wrow2.addWidget(self._btn_unwatch)
        v.addLayout(wrow2)

        self._lbl_watch = _label("", "Faint")
        v.addWidget(self._lbl_watch)

        self._host_hint = _label(self._hint_off(), "Faint")
        self._host_hint.setWordWrap(True)
        self._host_hint.setStyleSheet("font-size: 11px;")
        v.addWidget(self._host_hint, 1)
        return card

    # ------------------------------------------------------------------ 主题
    def _restyle(self):
        p = theme.current()
        self._view.setStyleSheet(
            "QLabel#ScreenView { background: %s; border: 1px solid %s;"
            " border-radius: 5px; color: %s; font-size: 12px; }"
            % (p["surface_sunken"], p["border"], p["text_faint"]))
        self._restyle_chat()

    def _restyle_chat(self):
        """聊天区的颜色（深浅模式都走这里；气泡是内联样式，不跟全局 QSS）。"""
        p = theme.current()
        self._chat_badge.setStyleSheet(
            "font-size: 11px; font-weight: 700; color: %s;"
            " background: transparent; border: 1px solid %s;"
            " border-radius: 4px; padding: 2px 10px;"
            % (p["green"] if self._chat_enabled else p["text_faint"],
               p["green"] if self._chat_enabled else p["border_strong"]))
        # 空态提示与气泡底色跟着主题走
        self._chat_empty.setStyleSheet("font-size: 11px; color: %s;"
                                       % p["text_faint"])
        for refs in list(self._chat_rows.values()):
            me = bool(refs.get("me"))
            bubble = refs.get("bubble")
            nick = refs.get("nick_lbl")
            av = refs.get("avatar")
            tile = refs.get("tile")
            if bubble is not None:
                bubble.setStyleSheet(
                    "QFrame#ChatBubble { background: %s;"
                    " border: 1px solid %s; border-radius: 10px; }"
                    % (p["accent_soft"] if me else p["surface_sunken"],
                       p["accent"] if me else p["border"]))
            if nick is not None:
                nick.setStyleSheet("font-size: 12px; font-weight: 700;"
                                   " color: %s;" % p["text"])
            if av is not None:
                av.setStyleSheet(
                    "background: %s; color: %s; border: none;"
                    " border-radius: 6px; font-size: 12px; font-weight: 700;"
                    % (p["accent"] if me else _avatar_color(av.text()),
                       p["accent_text"] if me else "#ffffff"))
            if tile is not None and not refs.get("withdrawn"):
                kind = refs.get("tile_kind") or "other"
                tile.setStyleSheet(
                    "background: %s; color: #ffffff; border: none;"
                    " border-radius: 6px; font-size: 9px; font-weight: 700;"
                    % _FILE_COLORS[kind][
                        1 if theme.get_theme() == "dark" else 0])
        # 已撤回的行：灰色斜体（_mark_chat_withdrawn 写的内联样式会被
        # 主题切换覆盖掉，所以撤回态在这里按标记重刷一遍）
        for refs in list(self._chat_rows.values()):
            if refs.get("withdrawn"):
                lbl = refs.get("name_lbl")
                size_lbl = refs.get("size_lbl")
                tile = refs.get("tile")
                if lbl is not None:
                    lbl.setStyleSheet("color: %s; font-style: italic;"
                                      % p["text_faint"])
                if size_lbl is not None:
                    size_lbl.setStyleSheet("font-size: 11px; color: %s;"
                                           % p["text_faint"])
                if tile is not None:
                    tile.setStyleSheet(
                        "background: %s; color: #ffffff; border: none;"
                        " border-radius: 6px; font-size: 9px;"
                        " font-weight: 700;" % p["text_faint"])

    def _hint_off(self):
        return ("在「本机地址」里选哪块网卡，就决定了走哪条链路 ——\n"
                "· 本地网络地址 → 局域网共享，观看码是随机 6 位数字，念给朋友就行。\n"
                "· 异地联机虚拟网卡 → 异地共享，走 EasyTier 的虚拟局域网，房间成员免输观看码。"
                "\n观看别人的屏幕：填对方地址与观看码后点「开始观看」；"
                "在「异地联机」的成员列表里点「看屏幕」会自动填好。")

    # ------------------------------------------------------------------ 状态
    @property
    def sharing(self):
        return self._server is not None

    @property
    def watching(self):
        return self._client is not None

    def _apply_state(self):
        sharing, watching = self.sharing, self.watching
        self._btn_start.setEnabled(not sharing and not watching)
        self._btn_stop.setEnabled(sharing)
        self._btn_watch.setEnabled(not watching and not sharing)
        self._btn_unwatch.setEnabled(watching)
        self._btn_full.setEnabled(watching)          # 全屏看的是对方的画面
        self._target_combo.setEnabled(not sharing and not watching)
        self._btn_targets.setEnabled(not sharing and not watching)
        self._addr_combo.setEnabled(not sharing and not watching)
        self._quality_combo.setEnabled(sharing)      # 共享中允许随时切换画质
        self._btn_copy.setEnabled(sharing)

        if sharing:
            text, obj = ("● 正在共享", "Success")
        elif watching:
            text, obj = ("● 正在观看", "Success")
        else:
            text, obj = ("● 未共享", "Faint")
        self._lbl_state.setText(text)
        self._lbl_state.setObjectName(obj)
        # setObjectName 不会自动重刷 QSS，得手动把样式重新算一遍
        self._lbl_state.style().unpolish(self._lbl_state)
        self._lbl_state.style().polish(self._lbl_state)

        if not sharing:
            self._lbl_stats.setText("")
        if not watching:
            self._lbl_watch.setText("")

    # ------------------------------------------------------------------ 聊天
    def _lan_page(self):
        """拿到「异地联机」页实例（聊天消息由它转发，下载也走它的云盘）。

        用间接查找而不是互相持有引用：这两页在功能上没有从属关系，
        为了一次跳转把它们绑死，会让任何一页的构造顺序都变成必须。
        """
        win = self.window()
        pages = getattr(win, "_pages", None) if win is not None else None
        return pages.get("lan") if pages else None

    def _apply_chat_state(self):
        """聊天可用态 + 空态文案。

        可用性跟着「异地联机」的房间走（_tick 里检测变化）：不在房间里
        时输入框是灰的——没有虚拟网卡，消息发出去也没有人收。
        """
        on = self._chat_enabled
        self._chat_input.setEnabled(on)
        self._chat_send.setEnabled(on)
        self._chat_badge.setText("房间聊天中" if on else "未进入房间")
        if not on:
            # 退房了：这个房间的消息一起退。留着只会让人以为还能接着聊。
            self._clear_chat_rows()
        if not self._chat_rows:
            self._chat_empty.setVisible(True)
            self._chat_empty.setText(
                "还没人说话。\n\n进入「异地联机」的房间后就能和成员聊天；"
                "临时云盘里分享的文件也会出现在这里，点「下载」即可。"
                if on else
                "进入「异地联机」的房间后，这里就能和房间成员聊天。\n"
                "临时云盘里分享的文件也会出现在这里，可直接下载。")
        else:
            self._chat_empty.setVisible(False)
        self._restyle_chat()

    def _connect_chat(self):
        """把聊天信号接到联机页（只接一次；on_shown 会重复触发）。

        接上之后先把房里已有的消息补画出来——用户可能是进房之后才切到
        本页的，前面的对话不该凭空消失。
        """
        if self._chat_connected:
            return
        lan = self._lan_page()
        if lan is None or not hasattr(lan, "chat_message"):
            return
        try:
            lan.chat_message.connect(self._on_chat_message)
            # 聊天区文件下载的进度（联机页单独发的一条通道，见 lan_page
            # 的 chat_download）。少了这一行，聊天区点下载就**完全没有反馈**。
            lan.chat_download.connect(self._on_chat_download)
        except (TypeError, RuntimeError):
            return
        self._chat_connected = True
        try:
            for rec in (lan.chat_history() or []):
                self._on_chat_message(dict(rec, event="new"))
        except Exception:
            pass

    def _send_chat(self):
        """发送按钮 / 回车：把输入框里的文本交给联机页广播出去。"""
        text = self._chat_input.text().strip()
        if not text:
            return
        lan = self._lan_page()
        if lan is None or not hasattr(lan, "send_chat_text"):
            self.toast("聊天不可用（找不到联机页）")
            return
        ok, err = lan.send_chat_text(text)
        if ok:
            self._chat_input.clear()
        else:
            self.toast(err or "发送失败")

    def _download_chat_file(self, rec):
        """文件消息上的下载图标：走联机页的云盘下载（会先问保存位置）。"""
        lan = self._lan_page()
        if lan is None or not hasattr(lan, "download_chat_file"):
            self.toast("下载不可用（找不到联机页）")
            return
        lan.download_chat_file(rec)

    def _on_chat_download(self, payload):
        """聊天区文件下载的进度 / 完成（联机页 `chat_download` → 主线程）。

        就地把气泡里的那一行进度文字改掉，**不重建整行** —— 重建会让用户
        正在看的那条消息闪一下，还可能把滚动位置带跑。

        以前这条通道根本不存在：联机页把进度发到云盘列表的
        `_share_widgets[key]`，而聊天区的文件气泡不在那张表里，于是
        "聊天区点下载完全没有反馈"（用户实测 2026-10-19）。
        """
        payload = dict(payload or {})
        refs = self._file_refs.get(str(payload.get("fid") or ""))
        if refs is None:
            return
        lbl = refs.get("prog_lbl")
        btn = refs.get("btn")
        if lbl is None:
            return
        p = theme.current()

        def _safe_set_button(text, enabled):
            if btn is None:
                return
            try:
                btn.setText(text)
                btn.setEnabled(enabled)
            except RuntimeError:                 # 控件已被销毁（退房竞态）
                pass

        if str(payload.get("state") or "") == "done":
            if payload.get("ok"):
                lbl.setText("✓ 已下载")
                lbl.setStyleSheet("font-size: 11px; color: %s;"
                                  % p.get("green", p["accent"]))
            else:
                lbl.setText("✗ 下载失败：%s"
                            % (payload.get("result") or "未知原因"))
                lbl.setStyleSheet("font-size: 11px; color: %s;" % p["red"])
            lbl.setVisible(True)
            _safe_set_button("下载", True)
            return

        # ---- 进度中 ----
        done = int(payload.get("done") or 0)
        total = int(payload.get("total") or 0)
        pct = int(done * 100 / total) if total else 0
        lbl.setText("下载中 %d%% · %s / %s"
                    % (pct, lan_share.human_size(done),
                       lan_share.human_size(total)))
        lbl.setStyleSheet("font-size: 11px; color: %s;" % p["accent"])
        lbl.setVisible(True)
        # 同一条消息一次只允许一个下载（联机页本来就有全局互斥），
        # 按钮就地变成"下载中"并禁用，避免用户重复点。
        _safe_set_button("下载中", False)

    def _on_chat_message(self, rec):
        """聊天消息到达（主线程）：新消息加一行，撤回改那一行。"""
        rec = dict(rec or {})
        mid = str(rec.get("id") or "")
        if not mid:
            return
        if str(rec.get("event") or "new") == "new":
            if mid in self._chat_rows:
                return                  # 历史补画时的重复
            refs = self._add_chat_row(rec)
            self._chat_rows[mid] = refs
            self._chat_empty.setVisible(False)
            self._scroll_chat_bottom()
        else:
            refs = self._chat_rows.get(mid)
            if refs is not None:
                self._mark_chat_withdrawn(refs)

    def _add_chat_row(self, rec):
        """往聊天列表里加一行，返回该行的控件引用（撤回时原地改它）。

        布局：**头像（昵称首字 + 稳定色）｜气泡（昵称 + 时间 + 内容）**。
        自己的消息靠右、头像在气泡右侧；别人的靠左、头像在左侧 ——
        "谁发的"不再只靠气泡里一行小字，头像 + 颜色 + 左右位置三重编码。

        文件消息在气泡里是一张**文件卡**：扩展名色块 + 文件名 + 体积 +
        下载按钮。色块按扩展名分类取色（压缩包/程序/媒体/文档），
        扫一眼就知道点下去会拿到什么。
        """
        p = theme.current()
        me = bool(rec.get("me"))
        kind = str(rec.get("kind") or "text")
        who = "我" if me else (str(rec.get("nick") or "").strip()
                               or str(rec.get("from_ip") or "队友"))
        stamp = _chat_time(rec.get("ts"))
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(2, 3, 2, 3)
        h.setSpacing(8)
        if me:
            h.addStretch(1)                      # 自己的消息靠右

        # ---- 头像：昵称首字 + 按昵称稳定取色 ----
        # 位置按微信/QQ 的逻辑：**别人的头像在气泡左边，自己的在右边**。
        # （`me` 由引擎按"是不是本机发的"打标：本机记录 me=True、收到的
        #   数据报 me=False —— 所以在任何一台电脑上跑，左右逻辑都一致。）
        av = QLabel(who[:1] or "?")
        av.setFixedSize(28, 28)
        av.setAlignment(Qt.AlignCenter)
        av.setStyleSheet(
            "background: %s; color: %s; border: none; border-radius: 6px;"
            " font-size: 12px; font-weight: 700;"
            % (p["accent"] if me else _avatar_color(who),
               p["accent_text"] if me else "#ffffff"))
        if not me:
            h.addWidget(av, 0, Qt.AlignTop)

        bubble = QFrame()
        bubble.setObjectName("ChatBubble")
        bubble.setMaximumWidth(CHAT_BUBBLE_MAX_W)
        # 自己的气泡用主色淡底 + 主色描边，别人的用中性下沉色：
        # 左右分栏之外再加一层底色区分，扫过去就能分清"我说的/别人说的"。
        bubble.setStyleSheet(
            "QFrame#ChatBubble { background: %s; border: 1px solid %s;"
            " border-radius: 10px; }"
            % (p["accent_soft"] if me else p["surface_sunken"],
               p["accent"] if me else p["border"]))
        bv = QVBoxLayout(bubble)
        bv.setContentsMargins(10, 7, 10, 8)
        bv.setSpacing(4)

        # ---- 头部：昵称（粗）+ 时间（淡）----
        head = QHBoxLayout()
        head.setSpacing(6)
        nick_lbl = QLabel(who)
        nick_lbl.setStyleSheet(
            "font-size: 12px; font-weight: 700; color: %s;"
            % (p["text"] if me else p["text"]))
        head.addWidget(nick_lbl)
        if stamp:
            time_lbl = QLabel(stamp)
            time_lbl.setStyleSheet("font-size: 10px; color: %s;"
                                   % p["text_faint"])
            head.addWidget(time_lbl)
        head.addStretch(1)
        bv.addLayout(head)

        refs = {"row": row, "bubble": bubble, "nick_lbl": nick_lbl,
                "avatar": av, "me": me, "withdrawn": False}
        if kind == "file":
            line = QHBoxLayout()
            line.setSpacing(9)
            # 扩展名色块：分类取色 + 写扩展名（最多 3 个字符）
            fname = str(rec.get("name") or "文件")
            fkind, ext = _file_kind(fname)
            tile = QLabel((ext[:3] or "?").upper())
            tile.setFixedSize(30, 30)
            tile.setAlignment(Qt.AlignCenter)
            tile.setStyleSheet(
                "background: %s; color: #ffffff; border: none;"
                " border-radius: 6px; font-size: 9px; font-weight: 700;"
                % _FILE_COLORS[fkind][1 if theme.get_theme() == "dark" else 0])
            line.addWidget(tile, 0, Qt.AlignVCenter)

            info = QVBoxLayout()
            info.setSpacing(1)
            name_lbl = QLabel(fname)
            name_lbl.setWordWrap(True)
            name_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            name_lbl.setStyleSheet(
                "font-size: 12px; font-weight: 600; color: %s;" % p["text"])
            info.addWidget(name_lbl)
            size_lbl = QLabel(lan_share.human_size(int(rec.get("size") or 0)))
            size_lbl.setStyleSheet("font-size: 11px; color: %s;"
                                   % p["text_faint"])
            info.addWidget(size_lbl)
            # 下载进度：默认不占位，点「下载」后才出现。单独一行而不是
            # 覆盖体积——"文件多大"和"下到哪了"要能同时看到。
            prog_lbl = QLabel("")
            prog_lbl.setVisible(False)
            prog_lbl.setWordWrap(True)
            prog_lbl.setStyleSheet("font-size: 11px; color: %s;" % p["accent"])
            info.addWidget(prog_lbl)
            line.addLayout(info, 1)

            # 「下载」用 MiniButton（内边距 2px 12px）而不是 ghost_button
            # （9px 18px）：气泡最大 360px，长文件名换行后行内剩余宽度不够，
            # ghost 的 36px 左右内边距会把"下载"两个字挤成显示不全。
            # 再给一个最小宽度兜底，任何文件名下都不会被压。
            btn = QPushButton("下载")
            btn.setObjectName("MiniButton")
            btn.setFixedHeight(26)
            btn.setMinimumWidth(56)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip("下载：%s" % fname)
            # lambda 里抓 rec 的副本：行建好后记录不会再变，但闭包抓可变
            # 字典的话，将来若记录被改写（撤回）按钮行为会跟着漂。
            btn.clicked.connect(
                lambda _=False, r=dict(rec): self._download_chat_file(r))
            line.addWidget(btn, 0, Qt.AlignVCenter)
            bv.addLayout(line)
            refs["name_lbl"] = name_lbl
            refs["size_lbl"] = size_lbl
            refs["tile"] = tile
            refs["tile_kind"] = fkind
            refs["btn"] = btn
            refs["prog_lbl"] = prog_lbl
            fid = str(rec.get("fid") or "")
            if fid:
                # 下载进度按 fid 找这一行（同一个文件只会有一条文件消息）
                self._file_refs[fid] = refs
        else:
            body = QLabel(str(rec.get("text") or ""))
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            body.setStyleSheet("font-size: 12px; color: %s;" % p["text"])
            bv.addWidget(body)
        h.addWidget(bubble)
        if me:
            h.addWidget(av, 0, Qt.AlignTop)      # 自己的头像在气泡右侧
        else:
            h.addStretch(1)
        # 插到 stretch 之前：消息自上而下堆积
        self._chat_list.insertWidget(self._chat_list.count() - 1, row)
        row.show()
        return refs

    def _mark_chat_withdrawn(self, refs):
        """把一条文件消息改写成「已撤回」：文字变灰、下载按钮失效。"""
        refs["withdrawn"] = True
        p = theme.current()
        lbl = refs.get("name_lbl")
        btn = refs.get("btn")
        size_lbl = refs.get("size_lbl")
        tile = refs.get("tile")
        if lbl is not None:
            lbl.setText("已撤回")
            lbl.setStyleSheet("color: %s; font-style: italic;"
                              % p["text_faint"])
        if size_lbl is not None:
            size_lbl.setText("对方撤回了这个文件")
            size_lbl.setStyleSheet("font-size: 11px; color: %s;"
                                   % p["text_faint"])
        if tile is not None:
            tile.setStyleSheet(
                "background: %s; color: #ffffff; border: none;"
                " border-radius: 6px; font-size: 9px; font-weight: 700;"
                % p["text_faint"])
        if btn is not None:
            btn.setEnabled(False)
            btn.setToolTip("对方已撤回这个文件")

    def _clear_chat_rows(self):
        """清空所有消息行（退房时用；控件先 hide 再 deleteLater，防重影）。"""
        for refs in list(self._chat_rows.values()):
            w = refs.get("row")
            if w is not None:
                w.hide()
                w.deleteLater()
        self._chat_rows = {}
        # 进度表的引用指向同一批控件，必须一起清 —— 否则退房后残留的
        # refs 指向已 deleteLater 的 QLabel，下次进度回调会碰已销毁对象。
        self._file_refs = {}

    def _scroll_chat_bottom(self):
        bar = self._chat_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _tick(self):
        """每秒采样一次计数器，算帧率与码率。

        ⚠️ 这里有两套口径，别混：
          * `frames_sent` / `bytes_sent` = **投递口径**（交给观看者的那一刻计数）
          * `viewer_stats()` 的 frames/bytes = **送达口径**（sendall 成功之后计数）
        慢链路下投递 30 帧/秒、送达可能只有 4 帧/秒 —— 这正是"异地只有 4 帧"
        的判断依据。以前只显示投递口径，用户看到的是"发出 30 帧/秒"，完全
        看不出瓶颈在链路。
        """
        if self._sender is not None and self._server is not None:
            # 统计口径统一在引擎层：`frames_sent` 只数**真正带画面的帧**
            # （心跳不算），`bytes_sent` 含协议头。抓屏线程与编码线程各数
            # 一半的话，两个数对不上还很难查。
            self._send_meter.feed(self._server.frames_sent,
                                  self._server.bytes_sent)
            w, h = self._sender.size
            if self._server.viewers:
                stats = self._server.viewer_stats()
                d_frames = sum(v["frames"] for v in stats)
                d_bytes = sum(v["bytes"] for v in stats)
                d_drop = sum(v["dropped"] for v in stats)
                self._deliver_meter.feed(d_frames, d_bytes)
                # ---- 自适应码率：把**送达口径**的实测值喂给控制器 ----
                # 用送达而不是投递：投递包含"塞进内核缓冲但还没出去"的部分，
                # 拿它判链路会把拥塞中的链路误判成富余。
                action = self._sender.adaptive.feed(d_bytes, d_frames)
                # "down" 由编码线程按 target 直接生效（H.264 的码率旋钮）；
                # "preset_down" = 码率压到下限仍不够 → 自动降一档分辨率。
                if action == "preset_down":
                    self._auto_lower_preset()
                per_frame = (self._deliver_meter.kbps * 1024.0
                             / max(1.0, self._deliver_meter.fps))
                text = ("%d 人正在观看　·　%s　·　发出 %.0f 帧/秒　·　送达 %.0f 帧/秒"
                        "　·　%.2f MB/s　·　%d×%d"
                        % (self._server.viewers, self._sender.codec_name,
                           self._send_meter.fps, self._deliver_meter.fps,
                           self._deliver_meter.kbps / 1024.0, w, h))
                if self._deliver_meter.fps > 0.5:
                    text += "　·　单帧 %.0f KB" % (per_frame / 1024.0)
                text += "　·　码率目标 %.2f Mbps" % (
                    self._sender.adaptive.target / 1e6)
                # 判定优先级：先看"送达是不是明显低于发出"（=链路受限），
                # 再看"发出本身就低"（=画面静止没有重发）。
                # 以前无脑补一句"（画面没变就不重发）"，在链路受限时会把
                # 4fps 说成正常现象，把真正的瓶颈掩盖掉。
                sent, got = self._send_meter.fps, self._deliver_meter.fps
                if got < 3.0 and sent < 2.0:
                    text += "　（画面没变就不重发）"
                self._lbl_stats.setText(text)
            else:
                # 没人在看时显示"0 帧/秒"会让人以为坏了。如实说清状态：
                # 画面已经准备好，只是在等人连进来。
                hint = "（%s）" % self._codec_note if self._codec_note else ""
                self._lbl_stats.setText(
                    "等待观看者接入　·　画面已就绪 %d×%d %s" % (w, h, hint))
        if self._client is not None:
            self._recv_meter.feed(self._client.frames, self._client.bytes)
            text = ("收到 %.0f 帧/秒　·　%.2f MB/s"
                    % (self._recv_meter.fps, self._recv_meter.kbps / 1024.0))
            # 观看的人看的是这一行，而对方画面静止时这里会很低 —— 解释一句
            # 才不会被当成"卡住了"（发送端那一行也有同样的说明）。
            if self._recv_meter.fps < 5:
                text += "　（对方画面没变，没有重发）"
            self._lbl_watch.setText(text)

        # 聊天可用态跟着「异地联机」的房间走：进房/退房各刷新一次界面。
        # （房间状态是联机页通过 screenshare.set_active_room 登记的，
        #  这里只读快照，不跨页调方法。）
        in_room = bool(screenshare.active_room().get("ip"))
        if in_room != self._chat_enabled:
            self._chat_enabled = in_room
            self._apply_chat_state()

    # ------------------------------------------------------------------ 共享
    def _reload_targets(self):
        """刷新"共享什么"下拉：显示器 + 本机可见窗口。"""
        keep = self._target_combo.currentData()
        self._target_combo.clear()
        for s in screenshare.list_screens():
            self._target_combo.addItem("🖥️ " + s["label"], ("screen", s["index"]))
        for w in screenshare.list_windows():
            title = w.title if len(w.title) <= 42 else w.title[:41] + "…"
            self._target_combo.addItem("🪟 " + title, ("window", w.hwnd))
            # 得说明一声：抓的是**屏幕上的那块区域**（不用 PrintWindow，
            # 那个在窗口"未响应"时会把抓屏线程一起拖死）。所以窗口被别的
            # 窗口挡住时，对方看到的就是被挡住的样子——和用户自己截屏看到的
            # 完全一致，但不提前讲清楚会被当成 bug。
            self._target_combo.setItemData(
                self._target_combo.count() - 1,
                "共享这个窗口在屏幕上的区域。它被别的窗口挡住时，"
                "对方看到的也是被挡住的样子（和你自己截屏一样）。",
                Qt.ToolTipRole)
        if keep is not None:
            for i in range(self._target_combo.count()):
                if self._target_combo.itemData(i) == keep:
                    self._target_combo.setCurrentIndex(i)
                    break
        self._targets_loaded = True

    def _reload_addresses(self):
        """列出本机所有 IPv4，并标出哪些是「异地联机」的虚拟网卡。

        选哪块网卡就决定了走哪条链路，所以每一项都把结论写在标签里 ——
        只写个 IP 让人自己判断哪块是虚拟网卡，太考验用户了。
        """
        keep = self._addr_combo.currentData()
        try:
            from etier import virtual_adapter_ips
            virtual = set(virtual_adapter_ips())
        except Exception:
            virtual = set()
        rows = [(ip, ip in virtual) for ip in screenshare.list_ipv4()]
        # 虚拟网卡排前面：进了房间的人多半就是要用异地共享。
        rows.sort(key=lambda r: (not r[1],))
        self._addr_combo.blockSignals(True)      # 重建期间别触发一串回调
        self._addr_combo.clear()
        for ip, is_virtual in rows:
            label = "%s（%s）" % (ip, "异地联机虚拟网卡" if is_virtual
                                  else "本地网络地址")
            self._addr_combo.addItem(label, (ip, is_virtual))
        if not rows:
            self._addr_combo.addItem("未检测到可用地址", ("", False))
        if keep is not None:
            for i in range(self._addr_combo.count()):
                if self._addr_combo.itemData(i) == keep:
                    self._addr_combo.setCurrentIndex(i)
                    break
        self._addr_combo.blockSignals(False)
        self._on_addr_changed()

    def _addr_mode(self, data=None):
        """当前选中的地址 → (ip, 是否虚拟网卡/异地)。"""
        if data is None:
            data = self._addr_combo.currentData()
        if isinstance(data, tuple):
            return (data[0] or ""), bool(data[1])
        return (data or ""), False

    def _on_addr_changed(self, _index=0):
        """地址换了 → 链路跟着换，提示文案立刻改口。"""
        if self.sharing:
            return                                # 共享中不允许换，别乱改提示
        ip, is_virtual = self._addr_mode()
        if is_virtual:
            self._host_hint.setText(
                "当前链路：异地共享。会绑在虚拟局域网 %s 上，房间成员在"
                "「异地联机」的成员列表里点「看屏幕」就能看，不用输观看码。"
                % ip)
        elif ip:
            self._host_hint.setText(
                "当前链路：局域网共享。会绑在 %s 上，同一个路由器下的电脑拿"
                "这个地址 + 观看码就能看；不在同一个网络的看不到。" % ip)
        else:
            self._host_hint.setText(self._hint_off())

    def _fetch_ffmpeg_pack(self):
        """后台下载/解压 H.264 组件（在网络线程里跑，进度走信号队列）。"""
        try:
            def prog(msg):
                self._bridge.event.emit("pack_progress", {"msg": msg})

            ok, msg = ffmpeg_pack.ensure_installed(
                on_progress=prog, on_stage=prog)
            self._bridge.event.emit("pack_done", {"ok": ok, "msg": msg})
        except Exception as exc:                        # noqa: BLE001
            self._bridge.event.emit("pack_done",
                                    {"ok": False, "msg": repr(exc)})

    def _auto_lower_preset(self):
        """自适应判定"码率压到下限还不够" → 自动降一档分辨率。

        只降一次一档，并记在 `_auto_downgraded` 里：控制器每个窗口都会报
        `preset_down`，不记就会一路降到底。用户手动换挡会清掉这个记录
        （见 `_on_quality`），把主动权还给用户。
        """
        idx = self._quality_combo.currentIndex()
        if idx <= 0:
            return
        if self._auto_downgraded == idx:
            return
        self._auto_downgraded = idx - 1
        self._quality_combo.setCurrentIndex(idx - 1)   # 触发 _on_quality → 重置控制器
        self.toast("链路带宽不足，已自动降到 %s 以保住帧率"
                   % screenshare.QUALITY_PRESETS[idx - 1][0])

    def _on_codec_log(self, payload):
        """编码器/解码器的日志（在后台线程里发出，已经过信号队列）。"""
        msg = str((payload or {}).get("msg") or "")
        if msg:
            self.toast(msg)

    def _current_target(self):
        data = self._target_combo.currentData()
        return data if data is not None else ("screen", 0)

    def _on_quality(self, index):
        """用户手动换挡：目标码率跟着换，并把自适应重置到新基准。

        为什么不用旧接口 `set_quality`：那只是改 JPEG 质量，而现在是
        码率档 —— 用户选了 1080p，自适应就从 5Mbps 起步往上/往下浮动。
        """
        if self._sender is not None:
            self._sender.set_preset(index)
        # 手动换挡也把"自动降挡"的记忆清掉（否则用户的意图会被旧记录盖住）
        self._auto_downgraded = None

    def _start_host(self):
        """开始共享。走哪条链路**完全由「本机地址」里选中的网卡决定**。"""
        if self._server is not None:
            self.toast("已经在共享了")
            return
        if self._client is not None:
            self.toast("请先停止观看，再开始共享")
            return
        self._reload_targets()

        ip, is_virtual = self._addr_mode()
        if not ip:
            self.toast("没找到可用的本机地址")
            return
        if is_virtual:
            mode = "room"
            # 观看码由「房间码 + 密码」派生：同一个房间的人算出来必然一致，
            # 所以队友点「看屏幕」不用手输。没进房间就没有这个码。
            token = screenshare.room_view_token()
            if not screenshare.active_room().get("code") or not token:
                self.toast("异地共享要先进「异地联机」的房间，观看码是按房间派生的")
                return
        else:
            mode = "lan"
            token = screenshare.random_token()

        server = screenshare.ShareServer(token, on_event=self._engine_event)
        # 把真实要绑的 IP 交给 pick_port：不传的话它按 0.0.0.0 探测，会把
        # "已被别的程序绑在具体某张网卡上"的端口算成空闲，随后 start() 才
        # 报"端口已被占用"，用户得多点一次。
        port = screenshare.pick_port(screenshare.DEFAULT_PORT, ip=ip)
        ok, res = server.start(ip, port)
        if not ok:
            self.toast(res)
            return

        self._server = server
        self._mode = mode
        self._send_meter = screenshare.RateMeter()   # 计数器从 0 重新走
        self._token_edit.setText(token)
        self._lbl_port.setText(str(server.port))
        # 在房间里共享的话，把端口广播给队友 —— 他们的成员行会长出
        # 「看屏幕」按钮。不在房间里时这个调用是空操作。
        screenshare.publish_screenshare(server.port)

        self._sender = _Sender(server, self._bridge)
        self._sender.set_target(self._current_target())
        self._sender.set_preset(self._quality_combo.currentIndex())
        # H.264 是唯一的编码路径（JPEG 编码已于 2026-10-11 删除）：组件可用
        # 就发 H.264。对端能力在**握手时**就已经筛掉——不声明 CAP_H264 的
        # 旧客户端直接吃 ERR codec（见 screenshare.ShareServer.offer），
        # 所以这里不用、也没有"每帧判一次要不要退回 JPEG"的逻辑。
        self._sender.set_use_h264(self._sender.h264_ready())
        if self._sender.h264_ready():
            self._codec_note = "H.264（%s）" % h264codec.describe()
        elif ffmpeg_pack is not None:
            self._codec_note = "H.264 组件未就绪（正在后台下载，就绪后自动启用）"
        else:
            self._codec_note = "H.264 组件缺失，画面暂时发不出去（连接会保持）"
        self._sender.start()
        if not self._sender.h264_ready() and ffmpeg_pack is not None \
                and not self._pack_fetching:
            # H.264 组件未装：**后台**按需下载（与 EasyTier 组件同款机制）。
            # 下载完成前连接保持着、心跳照发，但**没有画面**——编码线程在
            # `not self._use_h264` 分支只发心跳保活；装好后下一帧自动切换，
            # 不需要重启共享。
            self._pack_fetching = True
            threading.Thread(target=self._fetch_ffmpeg_pack,
                             daemon=True).start()
            self.toast("H.264 组件未安装，正在后台下载（一次性）…")

        if mode == "room":
            self._host_hint.setText(
                "已在虚拟局域网 %s:%d 上共享。房间成员在「异地联机」的成员列表里"
                "点「看屏幕」即可，不用输观看码（观看码已按房间码自动派生）。"
                % (ip, server.port))
        else:
            self._host_hint.setText(
                "已在 %s:%d 上共享。把「%s」这个地址和下面的观看码发给朋友，"
                "他在下面「共享屏幕」里填上就能看。"
                % (ip, server.port, ip))
        self._view.clear("正在共享（下面是本机预览）")
        self._apply_state()
        self.toast("已开始共享")

    def _stop_host(self):
        if self._server is None:
            return
        sender, self._sender = self._sender, None
        server, self._server = self._server, None
        screenshare.publish_screenshare(0)
        if sender is not None:
            sender.stop()
        if server is not None:
            server.stop()
        self._mode = ""
        self._send_meter = screenshare.RateMeter()   # 下次共享从 0 重新计数
        self._token_edit.clear()
        self._lbl_port.setText("--")
        self._host_hint.setText(self._hint_off())
        self._view.clear()
        self._lbl_stats.setText("")
        self._apply_state()
        self.toast("已停止共享")

    def stop_room_share(self):
        """房间退出时由「异地联机」页回调：停掉绑在虚拟网卡上的共享。

        退房之后虚拟网卡就没了，这条链路已经不通，界面上却还写着"正在
        共享"，队友那边也连不上——不停掉只会误导。局域网共享不受影响
        （它绑的是真实网卡）。
        """
        if self._server is None or self._mode != "room":
            return
        self._stop_host()
        self.toast("已退出房间，异地共享已停止")

    def _copy_token(self):
        text = self._token_edit.text().strip()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.toast("观看码已复制")

    # ------------------------------------------------------------------ 观看
    def watch_peer(self, host, port=0, token="", name=""):
        """供「异地联机」的成员列表调用：预填并直接开始观看。"""
        if self.sharing:
            self.toast("正在共享自己的屏幕，先停止再观看")
            return
        if self.watching:
            self._stop_watch()
        self._host_edit.setText("%s:%d" % (host, port or screenshare.DEFAULT_PORT))
        self._code_edit.setText(token or screenshare.room_view_token())
        self._start_watch()

    def _start_watch(self):
        if self._client is not None:
            self.toast("已经在观看了")
            return
        if self._server is not None:
            self.toast("请先停止共享，再开始观看")
            return
        host = self._host_edit.text().strip()
        if not host:
            self.toast("请填写对方的地址")
            return
        port = screenshare.DEFAULT_PORT
        if ":" in host:
            host, _, raw = host.rpartition(":")
            try:
                port = int(raw)
            except ValueError:
                self.toast("端口看着不对：%s" % raw)
                return
        client = screenshare.FrameClient(
            host, port, self._code_edit.text().strip(),
            on_frame=self._on_frame,
            on_event=self._engine_event,
            # 声明"我会解 H.264"——发送端据此才敢发 3/4 号包。
            # 组件不在时**不声明**：发送端会在握手时回绝（ERR codec）。
            # JPEG 编码已删除，没有"对面继续发 JPEG"这条兼容旧版的路了。
            caps=((screenshare.CAP_H264,) if h264codec is not None
                  and h264codec.available() else ()))
        ok, res = client.start()
        if not ok:
            self.toast(res)
            return
        self._client = client
        self._recv_meter = screenshare.RateMeter()
        self._view.clear("正在连接 %s…" % (res or host))
        self._apply_state()
        self.toast("正在观看 %s 的屏幕" % (res or host))

    def _stop_watch(self):
        if self._client is None:
            return
        # 先退全屏：留着一块黑屏盖住整个桌面、而数据流已经断了，是最难受的
        # 状态（用户会以为电脑卡死）。
        self._close_full()
        client, self._client = self._client, None
        client.stop()
        # 注意：**不在这里关解码器** —— 它归接收线程，主线程关会踩到
        # "线程还在 recv、上下文已被释放"（见 `_close_decoder`）。
        # `client.stop()` 会让接收线程走到 `closed` 事件，那时它自己收尾。
        self._view.clear()
        self._lbl_watch.setText("")
        self._apply_state()
        self.toast("已停止观看")

    # ------------------------------------------------------------------ 全屏观看
    def _target_screen(self):
        """挑"这个页面现在在哪块屏"，而不是主屏。

        多显示器下用主屏全屏会让人以为窗口跳走了；窗口还没 show（拿不到
        windowHandle）时退回主屏。
        """
        handle = self.window().windowHandle()
        if handle is not None and handle.screen() is not None:
            return handle.screen()
        return QGuiApplication.primaryScreen()

    def _open_full(self):
        """进入全屏观看（按钮与"双击画面"两条路都走这里）。"""
        if not self.watching:
            self.toast("先开始观看，再全屏")
            return
        if self._full is None:
            self._full = FullScreenViewer()
        if self._last_pix is not None:
            self._full.show_frame(self._last_pix)   # 立刻有画面，不等下一帧
        else:
            self._full.clear("正在等待对方画面…")
        self._full.open_on(self._target_screen())

    def _close_full(self):
        # 窗口对象**留着复用**（退出全屏只是 hide），所以这里只关不销毁。
        if self._full is not None and self._full.isVisible():
            self._full.close()

    # ------------------------------------------------------------------ 引擎回调
    def _engine_event(self, kind, payload):
        """引擎的事件回调 —— **可能来自任何后台线程**，所以这里只准做一件事：
        把事件丢进 Qt 信号队列，真正的处理在主线程的 `_on_engine` 里。

        为什么必须绕这一道：`ShareServer` 的 `viewer_join` / `rejected` /
        `viewer_leave` 分别是在 accept 线程与每个观看者线程里发出的，
        `FrameClient` 的 `closed` 是在接收线程里发出的。这些回调里但凡碰一下
        控件（哪怕只是弹一条 Toast），就等于**在非 GUI 线程里创建 QWidget**：
        Qt 先警告 `QObject::setParent: Cannot set parent, new parent is in a
        different thread`，随后整个进程直接消失，连 Python 堆栈都留不下来。
        v0.12beta 实测踩过：别人一点「看屏幕」，共享端立刻闪退。
        """
        # 解码器归接收线程所有，所以**它的收尾也必须在接收线程里做**
        # （`closed` 正是接收线程退出前发的最后一个事件）。放在 emit 之前，
        # 所以这段代码确实跑在接收线程上 —— 见 `_close_decoder` 的注释。
        if kind == "closed":
            self._close_decoder()
        self._bridge.engine.emit(kind, payload)

    def _on_frame(self, kind, seq, w, h, payload):
        """在接收线程里被调用 —— 绝不碰控件，但**可以**在这里解码。

        为什么要在这里解（而不是主线程）：1920px 一帧 H.264 解码约 3~8ms，
        30fps 下主线程要被吃掉一两成时间，界面动画（切页、开关、侧栏 hover）
        会被拖累。`QImage` 是纯数据对象，在非 GUI 线程里解码/构造是安全的
        —— 只有 `QPixmap` 必须留在主线程（由主线程那侧 `fromImage` 转一次）。
        """
        img = None
        if kind == screenshare.TYPE_FRAME and payload:
            img = QImage()
            try:
                if not img.loadFromData(payload, "JPEG"):
                    img = None
            except Exception:
                img = None
        elif kind in (screenshare.TYPE_H264_KEY,
                      screenshare.TYPE_H264) and payload:
            img = self._decode_h264(payload)
        if img is not None:
            self._bridge.event.emit("frame", img)

    def _decode_h264(self, payload):
        """解一个 Annex-B 包（解码器实例只在接收线程里用，见模块约定）。"""
        if h264codec is None:
            return None
        dec = self._decoder
        if dec is None:
            try:
                dec = h264codec.H264Decoder(threads=2)
            except Exception as exc:                    # noqa: BLE001
                self._bridge.engine.emit(
                    "codec_log", {"msg": "H.264 解码器不可用：%s" % exc})
                return None
            self._decoder = dec
        try:
            got = dec.decode(payload)
        except Exception as exc:                        # noqa: BLE001
            self._bridge.engine.emit(
                "codec_log", {"msg": "H.264 解码失败：%s" % exc})
            return None
        if not got:
            return None                                 # 还没凑够一帧
        w, h, bgra = got
        img = QImage(bgra, w, h, w * 4, QImage.Format.Format_RGB32)
        # 必须 copy()：bgra 是这一次解码的临时缓冲，下一帧就换掉了
        return img.copy()

    def _close_decoder(self):
        """关闭解码器。**只能在接收线程里调用**（见 `_engine_event`）。

        ⚠️ 为什么不能从主线程关：`FrameClient.stop()` 的 join 只有 1.5 秒，而
        接收线程可能正卡在 5 秒的 recv 里 —— join 超时后主线程继续跑，
        这时若把解码器上下文释放掉，接收线程下一帧就会解码到已释放的内存。
        实测症状：自检全过、**进程退出时访问违例 0xC0000005**。
        """
        dec, self._decoder = self._decoder, None
        if dec is not None:
            try:
                dec.close()
            except Exception:
                pass

    def _on_engine(self, kind, payload):
        """在主线程里处理引擎事件。"""
        if kind == "frame":
            # payload 是接收线程里已经解好的 QImage（见 _on_frame）——
            # 这里只做 QPixmap 转换（必须主线程），不再解码 JPEG。
            pix = QPixmap.fromImage(payload)
            if not pix.isNull():
                self._last_pix = pix
                self._view.show_frame(pix)
                # 全屏窗口开着就同步刷 —— 两个 `_ScreenView` 各自缓存自己
                # 尺寸下的缩放结果，同一张 QPixmap 喂过去没有额外解码成本。
                if self._full is not None and self._full.isVisible():
                    self._full.show_frame(pix)
        elif kind == "preview":
            self._view.show_frame(QPixmap.fromImage(payload))
        elif kind == "codec_log":
            self._on_codec_log(payload)
        elif kind == "pack_progress":
            msg = str((payload or {}).get("msg") or "")
            if msg:
                try:
                    self._host_hint.setText("H.264 组件：%s" % msg)
                except RuntimeError:
                    pass
        elif kind == "pack_done":
            ok = bool((payload or {}).get("ok"))
            msg = str((payload or {}).get("msg") or "")
            self._pack_fetching = False
            if ok and self._sender is not None:
                # 组件装好了：立刻让编码线程切到 H.264（不用重启共享）
                self._sender.set_use_h264(self._sender.h264_ready())
                self._codec_note = "H.264（%s）" % h264codec.describe()
            base = ("H.264 组件已就绪，下一帧起启用" if ok
                    else "H.264 组件下载失败，画面暂时发不出去（连接保持）")
            self.toast("%s（%s）" % (base, msg) if msg else base)
        elif kind == "viewer_join":
            self.toast("有人开始观看了（%d 人）" % payload.get("total", 1))
        elif kind == "rejected":
            if (payload or {}).get("reason") == "codec":
                # 旧版本客户端来观看：JPEG 编码已删除，它解不了 H.264
                self.toast("有旧版本客户端尝试观看，已拒绝（请对方更新 Yuhub）")
            else:
                self.toast("有人用错的观看码来连，已拒绝")
        elif kind == "sender_stopped":
            err = payload.get("error", "")
            if self._server is not None:
                self._stop_host()
                if err:
                    self.toast(err)
        elif kind == "closed":
            if self._client is not None:
                err = payload.get("error", "")
                self._stop_watch()
                self.toast(err or "对方结束了共享")

    # -------------------------------------------------------------- 生命周期
    def on_shown(self):
        self._reload_addresses()
        if not self._targets_loaded:
            self._reload_targets()
        # 聊天：先同步房间状态（徽标/输入框立刻是对的，不等下一秒的 tick），
        # 再接信号、补画房里已有的消息。
        in_room = bool(screenshare.active_room().get("ip"))
        if in_room != self._chat_enabled:
            self._chat_enabled = in_room
            self._apply_chat_state()
        self._connect_chat()

    def shutdown(self):
        for stop in (self._stop_host, self._stop_watch):
            try:
                stop()
            except Exception:
                pass
        self._close_full()
        # 聊天信号摘掉：退出过程中联机页可能还在广播最后几条消息，
        # 排着队发进一个正在拆的页面没有意义。
        lan = self._lan_page()
        if lan is not None and self._chat_connected:
            try:
                lan.chat_message.disconnect(self._on_chat_message)
            except (TypeError, RuntimeError):
                pass
            try:
                lan.chat_download.disconnect(self._on_chat_download)
            except (TypeError, RuntimeError, AttributeError):
                pass
        self._chat_connected = False
        self._clear_chat_rows()
        self._timer.stop()
