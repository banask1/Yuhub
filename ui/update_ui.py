"""自动更新的界面：检查更新弹窗 + 下载进度弹窗 + 后台检查线程。

设计取舍：
  * **静默检查**：启动后在后台线程跑，不阻塞界面、失败不弹窗
    （网络不通是常态，不该每次开机都打扰用户）。
  * **手动触发**：只有真的发现新版本才弹窗，用户点「立即更新」才开始下载。
    这既是体验考虑，也是安全底线——绝不静默安装。
  * 下载复用 downloader 的进度回调，文案与下载页保持一致口径。
"""

import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QProgressBar,
    QPushButton, QScrollArea, QWidget,
)

from . import theme
from .widgets import ghost_button, primary_button


# ================================================================ 后台检查

class UpdateChecker(QObject, threading.Thread):
    """启动时的静默检查（后台线程）。

    在子线程里只做纯网络 + 纯逻辑（updater.check_for_update），
    结果通过 Qt 信号回到主线程处理——**绝不在子线程碰 UI**。

    ⚠️ 必须同时继承 QObject：Qt 的 Signal 只有在 QObject 派生类上才会
    被元对象系统接管，否则 `th.found.connect(...)` 会抛
    `AttributeError: 'Signal' object has no attribute 'connect'`。
    mro 里 QObject 必须排在 threading.Thread 前面。
    """

    found = Signal(object)      # 有新版：ReleaseInfo
    failed = Signal(str)        # 出错（多数情况调用方选择忽略）
    finished_ = Signal()        # 无论成败都会发

    def __init__(self, current_version, owner=None, repo=None, timeout=8):
        # ⚠️ 多重继承下 super() 只会走 MRO 第一个（QObject），
        # Thread.__init__ 必须显式调用，否则 start() 时内部状态未初始化。
        QObject.__init__(self)
        threading.Thread.__init__(self, name="YuhubUpdateCheck", daemon=True)
        self._current = current_version
        self._owner = owner
        self._repo = repo
        self._timeout = timeout
        self.result = None
        self.error = ""

    def run(self):
        try:
            import updater
            kw = {"timeout": self._timeout}
            if self._owner:
                kw["owner"] = self._owner
            if self._repo:
                kw["repo"] = self._repo
            info, err = updater.check_for_update(self._current, **kw)
            if err:
                self.error = err
                self.failed.emit(err)
            elif info is not None:
                self.result = info
                self.found.emit(info)
        except Exception as e:
            # 自动更新永远不能把启动流程搞崩
            self.error = str(e)
            self.failed.emit(str(e))
        finally:
            self.finished_.emit()


# ================================================================ 弹窗基类

class _BaseDialog(QDialog):
    """与卸载页相同的无边框弹窗骨架（保持全项目视觉一致）。"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setWindowTitle(title)
        self._drag = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        self.frame = QFrame()
        self.frame.setObjectName("DialogFrame")
        outer.addWidget(self.frame)

        self.root = QVBoxLayout(self.frame)
        self.root.setContentsMargins(20, 14, 20, 18)
        self.root.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("DialogTitle")
        head.addWidget(t)
        head.addStretch(1)
        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("TitleButton")
        self.btn_close.setProperty("danger", True)
        self.btn_close.setFixedSize(32, 28)
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self.reject)
        head.addWidget(self.btn_close)
        self.root.addLayout(head)

        self.setStyleSheet(theme.build_qss())
        theme.bus.changed.connect(lambda _: self.setStyleSheet(theme.build_qss()))

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

    def center_on_parent(self):
        parent = self.parent()
        if parent is not None:
            self.move(parent.x() + (parent.width() - self.width()) // 2,
                      parent.y() + (parent.height() - self.height()) // 2)


def _notes_view(text, max_height=190):
    """把 Release notes（Markdown 纯文本）渲染成可滚动区域。

    不引 Markdown 渲染器（项目风格：标准库优先，且更新说明不需要富文本），
    只做最朴素的换行展示 + 代码块/标题的弱化处理。
    """
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setMaximumHeight(max_height)
    area.setFrameShape(QFrame.NoFrame)

    holder = QWidget()
    v = QVBoxLayout(holder)
    v.setContentsMargins(0, 0, 8, 0)
    v.setSpacing(3)

    lines = (text or "（本次更新没有提供说明）").splitlines() or ["（无说明）"]
    for ln in lines:
        s = ln.rstrip()
        lbl = QLabel(s if s.strip() else " ")
        lbl.setWordWrap(True)
        if s.lstrip().startswith("#"):
            lbl.setStyleSheet("font-size: 13px; font-weight: 600;")
        elif s.lstrip().startswith(("-", "*", "+")):
            lbl.setStyleSheet("font-size: 12px;")
        elif s.lstrip().startswith("```"):
            continue
        else:
            lbl.setStyleSheet("font-size: 12px;")
        v.addWidget(lbl)

    v.addStretch(1)
    area.setWidget(holder)
    return area


# ================================================================ 发现新版

class UpdateAvailableDialog(_BaseDialog):
    """发现新版本时弹出：展示版本号 + 更新说明 + 「立即更新」/「稍后」。"""

    def __init__(self, info, current_version, parent=None):
        super().__init__("发现新版本", parent)
        self.info = info
        self.action = "later"            # later | update
        self.setMinimumWidth(460)

        # ---- 版本号对比 ----
        ver = QFrame()
        ver.setObjectName("Card")
        vv = QVBoxLayout(ver)
        vv.setContentsMargins(16, 12, 16, 12)
        vv.setSpacing(4)
        new_lbl = QLabel("新版本  %s" % (info.tag or ""))
        new_lbl.setObjectName("CardTitle")
        vv.addWidget(new_lbl)
        cur_lbl = QLabel("当前版本  %s" % current_version)
        cur_lbl.setObjectName("CardDesc")
        vv.addWidget(cur_lbl)
        self.root.addWidget(ver)

        # ---- 更新说明 ----
        note_lbl = QLabel("更新内容")
        note_lbl.setObjectName("GroupTitle")
        self.root.addWidget(note_lbl)
        self.root.addWidget(_notes_view(info.notes, max_height=170))

        # ---- 大小提示 ----
        if info.asset_size:
            size_lbl = QLabel("更新包大小  %.1f MB" % (info.asset_size / 1048576.0))
            size_lbl.setObjectName("CardDesc")
            self.root.addWidget(size_lbl)

        # ---- 按钮 ----
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        later = ghost_button("稍后提醒")
        later.clicked.connect(self._on_later)
        row.addWidget(later)
        now = primary_button("立即更新")
        now.clicked.connect(self._on_update)
        row.addWidget(now)
        self.root.addLayout(row)

        QTimer.singleShot(0, self.center_on_parent)

    def _on_later(self):
        self.action = "later"
        self.accept()

    def _on_update(self):
        self.action = "update"
        self.accept()


# ================================================================ 下载进度

class UpdateProgressDialog(_BaseDialog):
    """下载更新包：进度条 + 速度 + 剩余时间。"""

    cancelled = Signal()

    def __init__(self, info, parent=None):
        super().__init__("正在下载更新", parent)
        self.setMinimumWidth(440)
        self._final = False

        self.status = QLabel("准备下载…")
        self.status.setObjectName("CardDesc")
        self.status.setWordWrap(True)
        self.root.addWidget(self.status)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.root.addWidget(self.bar)

        self.detail = QLabel(" ")
        self.detail.setObjectName("CardDesc")
        self.root.addWidget(self.detail)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = ghost_button("取消")
        self.btn_cancel.clicked.connect(self._on_cancel)
        row.addWidget(self.btn_cancel)
        self.root.addLayout(row)

        self.btn_close.hide()            # 下载中不给 ✕（与取消按钮语义重复）
        self.setWindowTitle("正在下载更新")
        QTimer.singleShot(0, self.center_on_parent)

    def _on_cancel(self):
        self.cancelled.emit()
        self.status.setText("正在取消…")
        self.btn_cancel.setEnabled(False)

    def set_probing(self, text):
        """线路探测/切换阶段：进度条转来回滚动的忙碌动画。"""
        try:
            self.status.setText(text or "正在连接更新源…")
            self.bar.setRange(0, 0)          # 0,0 = Qt 忙碌指示
            self.detail.setText(" ")
        except RuntimeError:
            pass

    def set_progress(self, snap):
        """由下载线程的进度回调驱动（经 Qt 信号回到主线程后调用）。"""
        try:
            total = snap.get("total") or 0
            got = snap.get("downloaded") or 0
            if self.bar.maximum() == 0:
                self.bar.setRange(0, 1000)   # 从忙碌态恢复正常
            if total:
                self.bar.setValue(int(got * 1000 / total))
            else:
                self.bar.setValue(0)
            speed = snap.get("speed") or 0
            left = snap.get("eta")
            import downloader
            parts = []
            if total:
                parts.append("%.1f%%" % (got * 100.0 / total))
            parts.append("%s / %s" % (downloader.human_bytes(got),
                                      downloader.human_bytes(total)
                                      if total else "未知"))
            if speed:
                parts.append(downloader.human_speed(speed))
            if left:
                parts.append("剩余 " + downloader.human_time(left))
            self.detail.setText("　·　".join(parts))
        except Exception:
            pass

    def set_status(self, text, final=False):
        self.status.setText(text)
        if final:
            self._final = True

    def finish_ok(self, message):
        self._final = True
        self.status.setText(message)
        self.bar.setValue(1000)
        self.detail.setText(" ")
        self.btn_cancel.setText("关闭")
        try:
            self.btn_cancel.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.btn_cancel.clicked.connect(self.accept)
        self.btn_cancel.setEnabled(True)

    def finish_fail(self, message):
        self._final = True
        self.status.setText(message)
        self.detail.setText(" ")
        self.btn_cancel.setText("关闭")
        try:
            self.btn_cancel.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.btn_cancel.clicked.connect(self.accept)
        self.btn_cancel.setEnabled(True)

    def reject(self):
        # 下载进行中按 Esc / 关窗 => 视为取消，而不是直接关掉
        if not self._final:
            self._on_cancel()
            return
        super().reject()
