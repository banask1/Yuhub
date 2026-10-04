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

import threading
import time

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
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import screenshare
from .. import motion, theme
from ..widgets import danger_button, ghost_button, primary_button
from .base_page import BasePage

#: 抓屏线程两次投递本机预览的最小间隔（秒）。
#: 预览只是给共享者"确认发出去的是什么"，不需要跟着 12fps 一起刷——
#: 每帧都往主线程发一张图，等于把工作线程省下来的时间又花回去。
PREVIEW_INTERVAL = 0.25


def _encode_jpeg(img, quality):
    """QImage → JPEG 字节。在工作线程里调用。

    单独把编码拆出来，是为了"没人观看时跳过编码"这条优化（见 `_Sender.run`）。
    """
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPEG", quality)
    data = bytes(buf.data())
    buf.close()
    return data


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
        self._cap = threading.Thread(target=self._capture_loop,
                                     name="share-capture", daemon=True)
        self._enc = threading.Thread(target=self._encode_loop,
                                     name="share-encode", daemon=True)

    # ------------------------------------------------------------ 外部接口
    def set_target(self, target):
        with self._lock:
            self._target = target

    def set_quality(self, index):
        with self._lock:
            self._quality = index

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
            screens = screenshare.list_screens()
            if not screens:
                return None
            s = screens[min(key, len(screens) - 1)]
            return (s["x"], s["y"], s["w"], s["h"])
        return screenshare.window_rect(key)

    def _capture_loop(self):
        cap = screenshare.GdiCapture()
        nxt = time.monotonic()
        last_preview = 0.0
        misses = 0
        try:
            while not self._stop.is_set():
                with self._lock:
                    target = self._target
                    index = self._quality
                index = max(0, min(index, len(screenshare.QUALITY_PRESETS) - 1))
                _name, max_edge, fps, quality = screenshare.QUALITY_PRESETS[index]
                interval = 1.0 / max(1, fps)

                rect = self._resolve(target)
                if rect is None or rect[2] <= 0 or rect[3] <= 0:
                    misses += 1
                    # 共享的窗口被关掉了：连着几秒都解析不出矩形就认输，
                    # 继续空转只会让用户以为"还在共享"。
                    if misses >= max(3, int(3.0 / interval)):
                        self.error = "共享的内容不见了（窗口被关闭了？）"
                        break
                else:
                    misses = 0
                    out_w, out_h = cap.fit(rect[2], rect[3], max_edge)
                    raw = cap.grab(rect, out_w, out_h)
                    if raw:
                        self.size = (out_w, out_h)
                        # 有人在看才把帧交给编码线程。没人看的时候抓屏照跑
                        # （本机预览要用它），但没必要白占编码线程的 CPU。
                        if self.server.viewers:
                            self._mail.put((raw, out_w, out_h, quality))
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

                nxt += interval
                delay = nxt - time.monotonic()
                if delay < -interval:          # 落后超过一整帧就重新对表
                    nxt = time.monotonic()
                    delay = 0.0
                if delay > 0:
                    self._stop.wait(delay)
        except Exception as exc:               # 抓屏线程绝不能静默死掉
            self.error = "抓屏中断（%s）" % exc
        finally:
            cap.close()
            self.bridge.event.emit("sender_stopped", {"error": self.error})

    # -------------------------------------------------------------- 编码线程
    def _encode_loop(self):
        rev = 0
        last_sig = None
        last_real = 0.0
        last_beat = 0.0
        try:
            while not self._stop.is_set():
                item, rev = self._mail.get(rev, 0.3)
                if item is None:
                    continue
                raw, out_w, out_h, quality = item
                now = time.monotonic()
                # 采样签名判断画面变了没有。为什么不逐字节比较，见
                # `screenshare.SAMPLE_STEP` 的注释：真实桌面上总有个 20×20
                # 的小块在闪，逐字节比**从不命中**，这条优化等于白写。
                sig = screenshare.frame_signature(raw)
                if sig == last_sig and now - last_real < screenshare.IDLE_REFRESH:
                    # 画面没变：跳过 JPEG 编码（这一段是整条链上最贵的一步），
                    # 只发一个 17 字节心跳。看文档、看桌面时发送端的 CPU 与
                    # 带宽占用都趋于零，异地走中继时这一条最关键。
                    # 心跳按固定间隔发就够（它的作用只是保活），跟着 30fps
                    # 发不但没意义，还会把待发的画面帧从邮箱里挤掉。
                    if now - last_beat >= screenshare.HEARTBEAT_INTERVAL:
                        last_beat = now
                        self.server.push(None, out_w, out_h)
                    continue
                last_sig = sig
                last_real = now
                img = QImage(raw, out_w, out_h, out_w * 4,
                             QImage.Format.Format_RGB32)
                self.server.push(_encode_jpeg(img, quality), out_w, out_h)
        except Exception as exc:
            self.error = "画面编码中断（%s）" % exc
            self._stop.set()



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
        self.setMinimumHeight(280)
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
        self._full = None            # 全屏观看窗口（懒建，退出只是 hide）
        self._mode = ""
        self._last_pix = None
        self._targets_loaded = False
        self._send_meter = screenshare.RateMeter()
        self._recv_meter = screenshare.RateMeter()

        self._bridge = _Bridge()
        self._bridge.event.connect(self._on_engine)
        self._bridge.engine.connect(self._on_engine)

        # 本页不用 BasePage 的滚动区：画面区要吃掉全部剩余高度，
        # 塞进 QScrollArea 会变成"画面里再套一个滚动条"的怪样子。
        self.scroll.setVisible(False)
        root = self.layout()
        root.addWidget(self._build_host_card())
        root.addWidget(self._build_watch_card())
        root.addWidget(self._build_view_card(), 1)

        self._restyle()
        self._apply_state()
        theme.bus.changed.connect(lambda _: self._restyle())

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------------ 构建
    def _build_host_card(self):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        self._lbl_state = _label("● 未共享")
        top.addWidget(self._lbl_state)
        self._lbl_stats = _label("", "Faint")
        top.addWidget(self._lbl_stats)
        top.addStretch(1)
        # 只有一个"开始共享"：走局域网还是走异地，由下面的「本机地址」
        # 里选中哪块网卡决定（选虚拟网卡就是异地，选真实网卡就是局域网）。
        # 分成两个按钮的时候，用户得自己保证"选的地址"和"点的按钮"是一对，
        # 选错就出现"绑着局域网 IP 却按了异地共享"这种自相矛盾的状态。
        self._btn_start = primary_button("开始共享")
        self._btn_start.clicked.connect(self._start_host)
        self._btn_stop = danger_button("停止共享")
        self._btn_stop.clicked.connect(self._stop_host)
        for b in (self._btn_start, self._btn_stop):
            top.addWidget(b)
        v.addLayout(top)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self._target_combo = QComboBox()
        self._target_combo.setMinimumWidth(220)
        self._btn_targets = ghost_button("刷新")
        self._btn_targets.clicked.connect(self._reload_targets)
        tbox = QHBoxLayout()
        tbox.setSpacing(6)
        tbox.addWidget(self._target_combo, 1)
        tbox.addWidget(self._btn_targets)
        grid.addWidget(_label("共享内容"), 0, 0)
        grid.addLayout(tbox, 0, 1)
        grid.addWidget(_label("画质"), 0, 2)
        self._quality_combo = QComboBox()
        for i, (name, edge, fps, _q) in enumerate(screenshare.QUALITY_PRESETS):
            self._quality_combo.addItem("%s（%dpx · %d 帧）" % (name, edge, fps))
            self._quality_combo.setItemData(
                i,
                "上行带宽约 %s（桌面内容的实测值，放视频/游戏会更高）。\n"
                "异地经中继时建议停在「标准」。" % screenshare.QUALITY_BANDWIDTH[i],
                Qt.ToolTipRole)
        self._quality_combo.setCurrentIndex(screenshare.DEFAULT_QUALITY)
        self._quality_combo.currentIndexChanged.connect(self._on_quality)
        grid.addWidget(self._quality_combo, 0, 3)

        self._addr_combo = QComboBox()
        self._addr_combo.setMinimumWidth(240)
        # 选哪块网卡 = 走哪条链路，所以换地址要立刻把提示文案改过来。
        self._addr_combo.currentIndexChanged.connect(self._on_addr_changed)
        grid.addWidget(_label("本机地址"), 1, 0)
        grid.addWidget(self._addr_combo, 1, 1)
        grid.addWidget(_label("端口"), 1, 2)
        self._lbl_port = _label("--")
        grid.addWidget(self._lbl_port, 1, 3)

        self._token_edit = QLineEdit()
        self._token_edit.setReadOnly(True)
        self._token_edit.setPlaceholderText("开始共享后生成")
        grid.addWidget(_label("观看码"), 2, 0)
        grid.addWidget(self._token_edit, 2, 1)
        self._btn_copy = ghost_button("复制观看码")
        self._btn_copy.clicked.connect(self._copy_token)
        self._btn_copy.setEnabled(False)
        grid.addWidget(self._btn_copy, 2, 2)
        v.addLayout(grid)

        self._host_hint = _label(self._hint_off(), "Faint")
        self._host_hint.setWordWrap(True)
        v.addWidget(self._host_hint)
        return card

    def _build_watch_card(self):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel("共享屏幕")
        title.setObjectName("CardTitle")
        top.addWidget(title)
        top.addStretch(1)
        self._lbl_watch = _label("", "Faint")
        top.addWidget(self._lbl_watch)
        v.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("对方的地址，如 192.168.1.5 或 10.126.126.7:45890")
        self._code_edit = QLineEdit()
        self._code_edit.setPlaceholderText("观看码")
        self._code_edit.setMaximumWidth(220)
        self._btn_watch = primary_button("开始观看")
        self._btn_watch.clicked.connect(self._start_watch)
        self._btn_unwatch = ghost_button("停止观看")
        self._btn_unwatch.clicked.connect(self._stop_watch)
        self._btn_unwatch.setEnabled(False)
        row.addWidget(self._host_edit, 1)
        row.addWidget(self._code_edit)
        row.addWidget(self._btn_watch)
        row.addWidget(self._btn_unwatch)
        v.addLayout(row)

        hint = _label(
            "在「异地联机」里进了同一个房间的话，直接去成员列表点「看屏幕」——"
            "地址和观看码会自动填好。", "Faint")
        hint.setWordWrap(True)
        v.addWidget(hint)
        return card

    def _build_view_card(self):
        holder = QFrame()
        holder.setObjectName("Card")
        box = QVBoxLayout(holder)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(6)

        # 全屏入口做两个：一个按钮（看得见、找得到），一个双击画面
        # （看视频的人的本能动作）。按钮在没在观看时是灰的 —— 全屏看的是
        # 对方的画面，共享端自己的预览全屏没有意义。
        top = QHBoxLayout()
        top.setSpacing(8)
        self._lbl_full_hint = _label("双击画面即可全屏观看", "Faint")
        top.addWidget(self._lbl_full_hint)
        top.addStretch(1)
        self._btn_full = ghost_button("全屏观看")
        self._btn_full.setToolTip(
            "整块屏只放画面。按 Esc 或双击画面退出；只用来观看对方的屏幕。")
        self._btn_full.clicked.connect(self._open_full)
        top.addWidget(self._btn_full)
        box.addLayout(top)

        self._view = _ScreenView(
            "还没开始。\n\n共享时这里显示本机正要发出去的画面；"
            "观看时显示对方的屏幕。")
        self._view.double_clicked.connect(self._open_full)
        box.addWidget(self._view)
        return holder

    # ------------------------------------------------------------------ 主题
    def _restyle(self):
        p = theme.current()
        self._view.setStyleSheet(
            "QLabel#ScreenView { background: %s; border: 1px solid %s;"
            " border-radius: 5px; color: %s; font-size: 12px; }"
            % (p["surface_sunken"], p["border"], p["text_faint"]))

    def _hint_off(self):
        return ("在「本机地址」里选哪块网卡，就决定了走哪条链路 ——\n"
                "· 本地网络地址 → 局域网共享，观看码是随机 6 位数字，念给朋友就行。\n"
                "· 异地联机虚拟网卡 → 异地共享，走 EasyTier 的虚拟局域网，房间成员免输观看码。")

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

    def _tick(self):
        """每秒采样一次计数器，算帧率与码率。"""
        if self._sender is not None and self._server is not None:
            # 统计口径统一在引擎层：`frames_sent` 只数**真正带画面的帧**
            # （心跳不算），`bytes_sent` 含协议头。抓屏线程与编码线程各数
            # 一半的话，两个数对不上还很难查。
            self._send_meter.feed(self._server.frames_sent,
                                  self._server.bytes_sent)
            w, h = self._sender.size
            if self._server.viewers:
                index = min(self._quality_combo.currentIndex(),
                            len(screenshare.QUALITY_PRESETS) - 1)
                cap_fps = screenshare.QUALITY_PRESETS[max(0, index)][2]
                text = ("%d 人正在观看　·　发出 %.0f 帧/秒　·　%.2f MB/s　·　%d×%d"
                        % (self._server.viewers, self._send_meter.fps,
                           self._send_meter.kbps / 1024.0, w, h))
                # 静止画面本来就不重复发，这时帧率掉到挡位以下是对的。
                # 不解释一句的话，"我选了 30 帧怎么只有 2 帧"会被当成故障。
                if self._send_meter.fps < cap_fps * 0.5:
                    text += "　（画面没变就不重发）"
                self._lbl_stats.setText(text)
            else:
                # 没人在看时显示"0 帧/秒"会让人以为坏了。如实说清状态：
                # 画面已经准备好，只是在等人连进来。
                self._lbl_stats.setText("等待观看者接入　·　画面已就绪 %d×%d" % (w, h))
        if self._client is not None:
            self._recv_meter.feed(self._client.frames, self._client.bytes)
            text = ("收到 %.0f 帧/秒　·　%.2f MB/s"
                    % (self._recv_meter.fps, self._recv_meter.kbps / 1024.0))
            # 观看的人看的是这一行，而对方画面静止时这里会很低 —— 解释一句
            # 才不会被当成"卡住了"（发送端那一行也有同样的说明）。
            if self._recv_meter.fps < 5:
                text += "　（对方画面没变，没有重发）"
            self._lbl_watch.setText(text)

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

    def _current_target(self):
        data = self._target_combo.currentData()
        return data if data is not None else ("screen", 0)

    def _on_quality(self, index):
        if self._sender is not None:
            self._sender.set_quality(index)

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
        self._sender.set_quality(self._quality_combo.currentIndex())
        self._sender.start()

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
        client = screenshare.FrameClient(host, port, self._code_edit.text().strip(),
                                         on_frame=self._on_frame,
                                         on_event=self._engine_event)
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
        self._bridge.engine.emit(kind, payload)

    def _on_frame(self, kind, seq, w, h, payload):
        """在接收线程里被调用 —— 只做转发，绝不碰控件。"""
        if kind == screenshare.TYPE_FRAME and payload:
            self._bridge.event.emit("frame", payload)

    def _on_engine(self, kind, payload):
        """在主线程里处理引擎事件。"""
        if kind == "frame":
            pix = QPixmap()
            if pix.loadFromData(payload, "JPEG"):
                self._last_pix = pix
                self._view.show_frame(pix)
                # 全屏窗口开着就同步刷 —— 两个 `_ScreenView` 各自缓存自己
                # 尺寸下的缩放结果，同一张 QPixmap 喂过去没有额外解码成本。
                if self._full is not None and self._full.isVisible():
                    self._full.show_frame(pix)
        elif kind == "preview":
            self._view.show_frame(QPixmap.fromImage(payload))
        elif kind == "viewer_join":
            self.toast("有人开始观看了（%d 人）" % payload.get("total", 1))
        elif kind == "rejected":
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

    def shutdown(self):
        for stop in (self._stop_host, self._stop_watch):
            try:
                stop()
            except Exception:
                pass
        self._close_full()
        self._timer.stop()
