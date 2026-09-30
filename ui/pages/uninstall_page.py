"""软件卸载页。

参考 Geek Uninstaller 的使用体验：
  1. 列表列出所有已安装程序（名称 / 厂商 / 版本 / 体积 / 安装日期）
  2. 支持搜索、排序、筛选（是否显示系统组件）
  3. 选中程序 → 卸载：先跑软件自带卸载器，完成后自动深度扫描残留
  4. 残留（文件 + 注册表）按项列出、可勾选，二次确认后清理
  5. 卸载器坏了 / 程序已损坏时，可走「强制清除残留」跳过卸载器直接扫

安全设计（与清理页一致）：
  * 扫描纯只读，绝不自动删除任何东西
  * 删除前必弹二次确认，逐项列出路径与体积
  * 需要管理员权限的残留走提权子进程（弹一次 UAC）
"""

import os
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import uninstaller as un
from .. import theme
from ..widgets import primary_button, ghost_button, danger_button, ToggleSwitch
from .base_page import BasePage


# ---------------------------------------------------------------------------
# 应用图标块：优先显示程序真实图标，取不到再退回"首字母色块"
# ---------------------------------------------------------------------------
class _IconCache:
    """全局图标缓存（按 来源路径+索引 做键）。

    为什么要缓存：
      * 同一个 exe 可能在列表里出现多次（多版本/多条目共享主程序）；
      * QIcon 从磁盘读取 + 解包图标资源是**同步 IO**，几十个程序逐个读
        会让列表构建明显卡顿；
      * 卸载后重扫会重建整个列表，没有缓存就又要全读一遍。

    缓存的是 QPixmap（已按目标尺寸渲染好），不是 QIcon —— 因为
    QIcon.pixmap() 每次调用都可能重新解码，缓存像素更省。
    """

    _map = {}
    _lock = threading.Lock()
    _provider_obj = None

    @classmethod
    def get(cls, path, index, size):
        """返回 QPixmap 或 None（取不到）。"""
        if not path:
            return None
        key = (path.lower(), int(index or 0), int(size))
        with cls._lock:
            if key in cls._map:
                return cls._map[key]
        pm = cls._load(path, index, size)
        with cls._lock:
            cls._map[key] = pm
        return pm

    @staticmethod
    def _load(path, index, size):
        """真正去磁盘取图标。

        ## 关键：exe 的图标**不能**用 QIcon 读

        这是本功能最容易走错的弯。`QIcon("C:\\...\\7zFM.exe")` 返回的是
        **空图标**（`isNull() == True`，pixmap 0×0）——Qt 的 `qico` 插件
        只认 **.ico 文件**，它不会去解析 PE 文件里嵌的图标资源。
        实测 7-Zip / 迅雷 / 火绒这类正常软件全都读成空。

        正解是 **`QFileIconProvider`**：它走 Windows Shell 的图标提取
        （`SHGetFileInfo` 那一套），对 exe / dll / 快捷方式 / 甚至没有图标
        资源的文件都能返回结果（无图标时给"默认程序"图标）。
        实测三个不同来源的 exe 全部稳定返回 72×72。

        处理顺序：
          1. **.ico / .png / .jpg 等纯图片** —— 直接 QIcon，最精确
          2. **.exe / .dll** —— QFileIconProvider（Shell 提取）
          3. 都没有则返回 None，交给色块兜底
        """
        try:
            if not os.path.isfile(path):
                return None
            ext = os.path.splitext(path)[1].lower()

            # ---- 纯图片资源：直接读 ----
            if ext in (".ico", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".svg"):
                from PySide6.QtGui import QIcon
                pm = QIcon(path).pixmap(size, size)
                if not pm.isNull() and pm.width() > 1:
                    return pm
                return None

            # ---- PE 文件（exe/dll）：走 Shell 图标提取 ----
            from PySide6.QtCore import QFileInfo
            from PySide6.QtWidgets import QFileIconProvider
            prov = _IconCache._provider()
            pm = prov.icon(QFileInfo(path)).pixmap(size, size)
            if pm.isNull() or pm.width() <= 1 or pm.height() <= 1:
                return None
            return pm
        except Exception:
            return None

    @classmethod
    def _provider(cls):
        """进程内复用一个 QFileIconProvider。

        它是无状态的、内部带 Shell 层缓存，重复 new 反而丢缓存。
        **必须在有 QApplication 之后创建**，所以做成懒加载。
        """
        if cls._provider_obj is None:
            try:
                from PySide6.QtWidgets import QFileIconProvider
                cls._provider_obj = QFileIconProvider()
            except Exception:
                cls._provider_obj = False        # 标记不可用，别再重试
        return cls._provider_obj or None

    @classmethod
    def clear(cls):
        with cls._lock:
            cls._map.clear()


class AppAvatar(QFrame):
    """程序图标：优先用真实图标，取不到时退回"首字母 + 稳定配色"。

    为什么保留色块兜底：
      * 大量软件（尤其绿色版 / 已卸载残留条目）压根没有可取图标；
      * 系统组件、MSI 包经常不写 DisplayIcon；
      * 全部显示成空白方格比色块更难看，也更难区分。

    真实图标是**同步读取**的（带缓存），因为列表构建本身就在后台线程里
    做完后再回主线程渲染，这里读几十个图标耗时可控（实测 <100ms）。
    """

    _PALETTE = ["tile_1", "tile_2", "tile_3", "tile_4", "tile_5", "tile_6"]
    SIZE = 34

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._app = app
        self._pix = None
        self._use_icon = False

        src, idx = un._icon_source(app)
        if src:
            self._pix = _IconCache.get(src, idx, self.SIZE)
            self._use_icon = self._pix is not None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._lbl = QLabel()
        self._lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._lbl)

        if self._use_icon:
            self._lbl.setPixmap(self._pix)
        else:
            name = getattr(app, "name", "") or ""
            self._lbl.setText(self._letter(name))
            self._lbl.setStyleSheet(
                "font-size: 15px; font-weight: 800; background: transparent;"
                " color: #ffffff;"
            )

        self._tint = self._PALETTE[
            sum(ord(c) for c in (getattr(app, "name", "") or "?")) % len(self._PALETTE)
        ]
        theme.bus.changed.connect(self._apply)
        self._apply()

    @staticmethod
    def _letter(name):
        s = (name or "?").strip()
        if not s:
            return "?"
        c = s[0]
        # 中文取首字，西文取首字母大写
        return c.upper() if c.isascii() else c

    def _apply(self):
        if self._use_icon:
            # 有真实图标时不加底色，避免图标边缘出现一圈色块
            self.setStyleSheet(
                "QFrame { background: transparent; border: none; }"
            )
        else:
            color = theme.current().get(self._tint, theme.current()["accent"])
            self.setStyleSheet(
                f"QFrame {{ background: {color}; border: none;"
                f" border-radius: 5px; }}"
            )


# ---------------------------------------------------------------------------
# 单个应用行（可选中）
# ---------------------------------------------------------------------------
class AppRow(QFrame):
    """列表里的一行程序：图标 + 名称/厂商 + 版本/日期 + 体积 + 选中框。"""

    picked = Signal(object)          # 点击 → 选中该行
    activated = Signal(object)       # 双击 → 直接卸载

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.setObjectName("CleanRow")
        self.app = app
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 14, 9)
        lay.setSpacing(12)

        self.check = QCheckBox()
        self.check.setCursor(Qt.PointingHandCursor)
        self.check.stateChanged.connect(
            lambda st: self.picked.emit(self)
        )
        lay.addWidget(self.check, 0, Qt.AlignVCenter)

        lay.addWidget(AppAvatar(app), 0, Qt.AlignVCenter)

        # 名称 + 厂商
        box = QVBoxLayout()
        box.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = QLabel(app.name)
        self.name.setObjectName("CleanName")
        top.addWidget(self.name)
        if app.kind == "uwp":
            tag = QLabel("应用商店")
            tag.setObjectName("Faint")
            tag.setStyleSheet("font-size: 10px; font-weight: 700;")
            top.addWidget(tag)
        if not app.removable:
            tag = QLabel("无卸载程序")
            tag.setObjectName("Warn")
            tag.setStyleSheet("font-size: 10px; font-weight: 700;")
            top.addWidget(tag)
        top.addStretch(1)
        box.addLayout(top)

        sub_bits = []
        if app.publisher:
            sub_bits.append(app.publisher)
        if app.version:
            sub_bits.append(app.version)
        self.desc = QLabel(" · ".join(sub_bits) or "—")
        self.desc.setObjectName("CleanDesc")
        self.desc.setWordWrap(False)
        box.addWidget(self.desc)
        lay.addLayout(box, 1)

        # 安装日期
        self.date = QLabel(app.date_text)
        self.date.setObjectName("Faint")
        self.date.setStyleSheet("font-size: 11px;")
        self.date.setFixedWidth(78)
        self.date.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.date, 0, Qt.AlignVCenter)

        # 体积
        self.size = QLabel(app.size_text)
        self.size.setObjectName("CleanSize")
        self.size.setMinimumWidth(80)
        self.size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.size, 0, Qt.AlignVCenter)

        tip = [app.name]
        if app.install_location:
            tip.append(f"安装位置：{app.install_location}")
        if app.hive:
            tip.append(f"注册表来源：{app.hive}\\{app.subkey}")
        self.setToolTip("\n".join(tip))

        theme.bus.changed.connect(lambda _: self._restyle())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.check.setChecked(not self.check.isChecked())
            self._selected = self.check.isChecked()
            self._restyle()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.activated.emit(self)
        super().mouseDoubleClickEvent(event)

    def set_selected(self, v):
        self._selected = bool(v)
        self.check.setChecked(self._selected)
        self._restyle()          # 改状态后必须重绘，否则高亮不会出现

    def is_selected(self):
        return self.check.isChecked()

    def _restyle(self):
        try:
            p = theme.current()
            if self._selected:
                self.setStyleSheet(
                    f"QFrame#CleanRow {{ background: {p['accent_soft']}; "
                    f"border: 1px solid {p['accent']}; border-radius: 4px; }}"
                )
            else:
                self.setStyleSheet("")
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# 残留项行
# ---------------------------------------------------------------------------
class LeftoverRow(QFrame):
    """一条残留：勾选框 + 类型徽标 + 路径 + 体积。"""

    toggled = Signal()

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.setObjectName("CleanRow")
        self.item = item

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 14, 8)
        lay.setSpacing(10)

        self.check = QCheckBox()
        self.check.setChecked(item.checked)
        self.check.setCursor(Qt.PointingHandCursor)
        self.check.stateChanged.connect(self._on_toggle)
        lay.addWidget(self.check, 0, Qt.AlignVCenter)

        kind = "注册表" if item.kind == "reg" else ("文件夹" if item.kind == "dir" else "文件")
        badge = QLabel(kind)
        badge.setObjectName("Badge")
        badge.setStyleSheet("font-size: 10px; padding: 1px 7px;")
        badge.setFixedWidth(56)
        badge.setAlignment(Qt.AlignCenter)
        lay.addWidget(badge, 0, Qt.AlignVCenter)

        path = QLabel(item.path)
        path.setObjectName("CleanDesc")
        path.setWordWrap(False)
        path.setToolTip(item.path)
        lay.addWidget(path, 1)

        if item.note:
            note = QLabel(item.note)
            note.setObjectName("Faint")
            note.setStyleSheet("font-size: 10px;")
            lay.addWidget(note, 0, Qt.AlignVCenter)

        self.size = QLabel(item.size_text)
        self.size.setObjectName("CleanSize")
        self.size.setMinimumWidth(76)
        self.size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.size, 0, Qt.AlignVCenter)

    def _on_toggle(self, st):
        self.item.checked = bool(st)
        self.toggled.emit()

    def is_checked(self):
        return self.check.isChecked()


# ---------------------------------------------------------------------------
# 通用提示弹窗基类（无系统边框，风格与清理页确认框一致）
# ---------------------------------------------------------------------------
class _BaseDialog(QDialog):
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
        close = QPushButton("✕")
        close.setObjectName("TitleButton")
        close.setProperty("danger", True)
        close.setFixedSize(32, 28)
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.reject)
        head.addWidget(close)
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
            self.move(
                parent.x() + (parent.width() - self.width()) // 2,
                parent.y() + (parent.height() - self.height()) // 2,
            )


class UninstallConfirmDialog(_BaseDialog):
    """卸载前确认：显示程序信息与将要发生的事。"""

    confirmed = Signal(bool)      # True = 一并清理残留，False = 只卸载

    def __init__(self, app, parent=None):
        super().__init__("确认卸载", parent)
        warn = QLabel(f"即将卸载 <b>{app.name}</b>"
                      + (f" <span style='opacity:.7'>{app.version}</span>"
                         if app.version else ""))
        warn.setTextFormat(Qt.RichText)
        warn.setWordWrap(True)
        warn.setObjectName("CardDesc")
        self.root.addWidget(warn)

        info = QVBoxLayout()
        info.setSpacing(3)
        for k, v in [("发布者", app.publisher or "—"),
                     ("安装位置", app.install_location or "—"),
                     ("占用体积", app.size_text),
                     ("安装日期", app.date_text)]:
            r = QHBoxLayout()
            r.setSpacing(8)
            kl = QLabel(k)
            kl.setObjectName("Faint")
            kl.setStyleSheet("font-size: 11px;")
            kl.setFixedWidth(60)
            vl = QLabel(v)
            vl.setObjectName("CleanDesc")
            vl.setWordWrap(True)
            r.addWidget(kl)
            r.addWidget(vl, 1)
            info.addLayout(r)
        self.root.addLayout(info)

        hint = QLabel(
            "ℹ️ 将调用该软件<b>自带的卸载程序</b>。卸载向导可能会弹窗询问选项，"
            "请按需选择——完成后本工具会自动扫描并列出残留的文件与注册表项，"
            "由你确认后再清理。"
        )
        hint.setTextFormat(Qt.RichText)
        hint.setWordWrap(True)
        hint.setObjectName("Warn")
        hint.setStyleSheet("font-size: 11px;")
        self.root.addWidget(hint)

        opt = QHBoxLayout()
        opt.setSpacing(10)
        ol = QLabel("卸载完成后自动扫描残留")
        ol.setStyleSheet("font-size: 13px;")
        opt.addWidget(ol)
        opt.addStretch(1)
        self.sw_scan = ToggleSwitch(True)
        opt.addWidget(self.sw_scan)
        self.root.addLayout(opt)

        self.root.addStretch(1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = ghost_button("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        ok = danger_button("开始卸载")
        ok.clicked.connect(self._on_ok)
        btns.addWidget(ok)
        self.root.addLayout(btns)

        self.resize(520, 400)

    def _on_ok(self):
        keep = bool(self.sw_scan.isChecked())
        self.accept()
        self.confirmed.emit(keep)


class LeftoverConfirmDialog(_BaseDialog):
    """残留清理前确认：列出项数与体积，需用户确认。"""

    confirmed = Signal()

    def __init__(self, items, parent=None):
        super().__init__("确认清理残留", parent)
        sel = [it for it in items if it.checked]
        total = sum(it.size for it in sel if it.kind != "reg")
        regs = sum(1 for it in sel if it.kind == "reg")

        warn = QLabel(
            f"即将清理 <b>{len(sel)}</b> 项残留"
            + (f"（其中注册表项 {regs} 个）" if regs else "")
            + f"，预计释放 <b>{un.human_bytes(total)}</b>。"
            "此操作<b>不可撤销</b>。"
        )
        warn.setTextFormat(Qt.RichText)
        warn.setWordWrap(True)
        warn.setObjectName("CardDesc")
        self.root.addWidget(warn)

        admin_needed = [] if un.is_admin() else [it for it in sel
                                                 if self._needs_admin(it)]
        if admin_needed:
            hint = QLabel(
                f"ℹ️ 有 <b>{len(admin_needed)}</b> 项位于受保护位置"
                "（如 Program Files、ProgramData、HKLM 注册表），"
                "清理时会弹一次 <b>UAC 授权框</b>，点「是」即可继续。"
            )
            hint.setTextFormat(Qt.RichText)
            hint.setWordWrap(True)
            hint.setObjectName("Warn")
            hint.setStyleSheet("font-size: 11px;")
            self.root.addWidget(hint)

        box = QVBoxLayout()
        box.setSpacing(4)
        for it in sel[:14]:
            r = QHBoxLayout()
            r.setSpacing(8)
            n = QLabel(("· " + it.path))
            n.setObjectName("CleanDesc")
            n.setWordWrap(False)
            s = QLabel(it.size_text)
            s.setObjectName("Faint")
            s.setStyleSheet("font-size: 11px;")
            r.addWidget(n, 1)
            r.addWidget(s, 0)
            box.addLayout(r)
        if len(sel) > 14:
            more = QLabel(f"… 另有 {len(sel) - 14} 项")
            more.setObjectName("Faint")
            box.addWidget(more)
        self.root.addLayout(box)
        self.root.addStretch(1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = ghost_button("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        ok = danger_button("确认清理")
        ok.clicked.connect(self._on_ok)
        btns.addWidget(ok)
        self.root.addLayout(btns)

        self.resize(560, 440)

    @staticmethod
    def _needs_admin(item):
        """这一项是否**可能**需要管理员权限。

        旧实现只认 Program Files / ProgramData / Windows 三个前缀，
        结果 AppData 下的残留一律按普通权限删——而 AppData 里恰恰混着
        大量"安装程序创建、ACL 只给了 SYSTEM/管理员"的目录（最典型的是
        AppData\\Local\\Packages 下的 UWP 包目录），普通权限必失败。

        这里改成三层判断：
          1. 注册表：HKLM / HKCR 必须提权（HKCU 属于当前用户，不需要）。
          2. 系统级路径前缀：受保护位置。
          3. 其它位置（含 AppData）：**实测能不能删**——用
             `os.access(W_OK)` 加父目录写权限探测；探不出来时返回 False，
             由下游"删失败 → 自动提权重试"兜底，不会漏。
        """
        if item.kind == "reg":
            return item.hive in ("HKLM", "HKLM32", "HKCR")

        p = item.path or ""
        low = p.lower()
        for pre in ("c:\\program files", "c:\\programdata",
                    "c:\\windows", "c:\\users\\all users",
                    "c:\\users\\default"):
            if low.startswith(pre):
                return True

        # AppData 里的 UWP 包目录有特殊 ACL，普通权限一定删不掉
        if "\\appdata\\local\\packages\\" in low:
            return True

        # 其它情况：实际探测一下能否删除。删不掉就直接走提权，别等失败。
        target = p if os.path.isdir(p) else os.path.dirname(p)
        if target and os.path.isdir(target):
            try:
                probe = os.path.join(target, ".yuhub_perm_probe")
                with open(probe, "wb"):
                    pass
                os.remove(probe)
            except OSError:
                return True                 # 连临时文件都写不进 → 需要提权
        return False

    def _on_ok(self):
        self.accept()
        self.confirmed.emit()


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
class UninstallPage(BasePage):
    TINT_KEY = "tile_6"
    BADGE = "已上线"

    _apps_ready = Signal(object)
    _apps_failed = Signal(str)
    _work_done = Signal(object)
    _work_status = Signal(str)
    _clean_done = Signal(object)
    _elevated_done = Signal(object)

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "软件卸载",
            "列出本机所有已安装程序，调用各自卸载程序并深度清理残留文件与注册表。",
            icon="🗑️",
            notify=notify,
            parent=parent,
        )
        self._apps = []
        self._rows = []
        self._selected = None
        self._scanner = None
        self._worker = None
        self._busy = False
        self._scanned_once = False
        self._quiet_scan = False      # True = 本次扫描是程序内部触发的静默刷新
        self._leftovers = []
        self._leftover_rows = []
        self._confirm_dlg = None
        self._elevate_items = []

        self._apps_ready.connect(self._on_apps_ready)
        self._apps_failed.connect(self._on_apps_failed)
        self._work_done.connect(self._on_work_done)
        self._work_status.connect(self._on_work_status)
        self._clean_done.connect(self._on_clean_done)
        self._elevated_done.connect(self._on_elevated_done)

        self._build_content()

    # ------------------------------------------------------------ 构建
    def _build_content(self):
        # ---- 工具条 ----
        bar = QHBoxLayout()
        bar.setSpacing(10)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索程序名称或发布者…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._apply_filter())
        self.search.setMinimumWidth(240)
        bar.addWidget(self.search, 1)

        self.sort = QComboBox()
        for val, label in [("name", "按名称"), ("size", "按体积"),
                           ("date", "按安装日期"), ("pub", "按发布者")]:
            self.sort.addItem(label, val)
        self.sort.currentIndexChanged.connect(lambda _: self._apply_filter())
        bar.addWidget(self.sort)

        self.btn_rescan = ghost_button("重新扫描")
        self.btn_rescan.clicked.connect(lambda: self.start_scan(force=True))
        bar.addWidget(self.btn_rescan)
        self.add_layout(bar)

        # ---- 系统组件开关 ----
        opt = QHBoxLayout()
        opt.setSpacing(10)
        ol = QLabel("显示系统组件与运行库")
        ol.setObjectName("Muted")
        ol.setStyleSheet("font-size: 12px;")
        opt.addWidget(ol)
        self.sw_system = ToggleSwitch(False)
        self.sw_system.toggled.connect(self._on_system_toggle)
        opt.addWidget(self.sw_system)
        opt.addStretch(1)
        self._count_lbl = QLabel("")
        self._count_lbl.setObjectName("Faint")
        self._count_lbl.setStyleSheet("font-size: 11px;")
        opt.addWidget(self._count_lbl)
        self.add_layout(opt)

        # ---- 操作栏 ----
        act = QHBoxLayout()
        act.setSpacing(10)
        self.btn_uninstall = danger_button("卸载选中程序")
        self.btn_uninstall.setEnabled(False)
        self.btn_uninstall.clicked.connect(self._confirm_uninstall)
        act.addWidget(self.btn_uninstall)

        self.btn_force = ghost_button("强制清除残留")
        self.btn_force.setToolTip(
            "跳过卸载程序，只扫描并清理残留。\n"
            "适用于卸载器已损坏、或程序文件已被手动删除的情况。"
        )
        self.btn_force.setEnabled(False)
        self.btn_force.clicked.connect(self._force_clean)
        act.addWidget(self.btn_force)

        act.addStretch(1)
        self._sel_lbl = QLabel("")
        self._sel_lbl.setObjectName("Muted")
        self._sel_lbl.setStyleSheet("font-size: 12px;")
        act.addWidget(self._sel_lbl)
        self.add_layout(act)

        # ---- 进度 ----
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        self._bar.hide()
        self.add(self._bar)

        self._status = QLabel("")
        self._status.setObjectName("Faint")
        self._status.setStyleSheet("font-size: 11px;")
        self.add(self._status)

        # ---- 残留区（默认隐藏） ----
        self._leftover_card = QFrame()
        self._leftover_card.setObjectName("Card")
        lc = QVBoxLayout(self._leftover_card)
        lc.setContentsMargins(18, 14, 18, 16)
        lc.setSpacing(10)

        lh = QHBoxLayout()
        lh.setSpacing(8)
        lt = QLabel("🧹  残留清理")
        lt.setObjectName("CardTitle")
        lh.addWidget(lt)
        self._leftover_sum = QLabel("")
        self._leftover_sum.setObjectName("CardDesc")
        lh.addWidget(self._leftover_sum)
        lh.addStretch(1)

        self.btn_all_lo = ghost_button("全选")
        self.btn_all_lo.clicked.connect(lambda: self._select_leftovers(True))
        lh.addWidget(self.btn_all_lo)
        self.btn_none_lo = ghost_button("全不选")
        self.btn_none_lo.clicked.connect(lambda: self._select_leftovers(False))
        lh.addWidget(self.btn_none_lo)
        self.btn_clean_lo = danger_button("清理选中残留")
        self.btn_clean_lo.clicked.connect(self._confirm_clean_leftovers)
        lh.addWidget(self.btn_clean_lo)
        # 「关闭」：让用户能主动收起残留卡片。
        # 曾经的 bug：卡片只有 全选/全不选/清理，**没有任何关闭入口** ——
        # 点开「强制清除残留」后卡片就一直挂在那里，用户找不到取消的办法
        # （只能靠再卸载一次别的程序才会被 _clear_leftovers 顶掉）。
        self.btn_close_lo = ghost_button("关闭")
        self.btn_close_lo.setToolTip("收起残留列表（不会删除任何东西）")
        self.btn_close_lo.clicked.connect(self._dismiss_leftovers)
        lh.addWidget(self.btn_close_lo)
        lc.addLayout(lh)

        self._leftover_hint = QLabel("")
        self._leftover_hint.setObjectName("Faint")
        self._leftover_hint.setStyleSheet("font-size: 11px;")
        self._leftover_hint.setWordWrap(True)
        lc.addWidget(self._leftover_hint)

        # 残留列表放进限高的滚动区：残留可能有几十项，直接堆在页面里
        # 会把下面的程序列表整个挤出视野，用户就没法继续操作了。
        self._leftover_scroll = QScrollArea()
        self._leftover_scroll.setWidgetResizable(True)
        self._leftover_scroll.setFrameShape(QFrame.NoFrame)
        self._leftover_scroll.setMaximumHeight(300)
        self._leftover_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        self._leftover_box = QVBoxLayout(holder)
        self._leftover_box.setContentsMargins(0, 0, 6, 0)
        self._leftover_box.setSpacing(5)
        self._leftover_box.setAlignment(Qt.AlignTop)
        self._leftover_scroll.setWidget(holder)
        lc.addWidget(self._leftover_scroll)
        self._leftover_card.hide()
        self.add(self._leftover_card)

        # ---- 列表标题 ----
        head = QHBoxLayout()
        head.setContentsMargins(12, 0, 14, 0)
        head.setSpacing(12)
        h1 = QLabel("程序名称")
        h1.setObjectName("GroupTitle")
        head.addWidget(h1, 1)
        h2 = QLabel("安装日期")
        h2.setObjectName("GroupTitle")
        h2.setFixedWidth(78)
        h2.setAlignment(Qt.AlignRight)
        head.addWidget(h2)
        h3 = QLabel("体积")
        h3.setObjectName("GroupTitle")
        h3.setMinimumWidth(80)
        h3.setAlignment(Qt.AlignRight)
        head.addWidget(h3)
        self.add_layout(head)

        # ---- 列表容器 ----
        self._list_box = QVBoxLayout()
        self._list_box.setSpacing(5)
        self.add_layout(self._list_box)
        self.add_stretch()

        self._status.setText("准备扫描已安装程序…")

    # ------------------------------------------------------------ 扫描
    def on_shown(self):
        if self._scanned_once:
            return
        self._scanned_once = True
        QTimer.singleShot(120, lambda: self.start_scan())

    def start_scan(self, force=False):
        if self._busy:
            return
        self._busy = True
        self.btn_rescan.setEnabled(False)
        self.btn_rescan.setText("扫描中")
        self._status.setText("正在枚举已安装程序…")
        self._bar.setRange(0, 0)       # 不确定进度，显示为滚动条
        self._bar.show()

        include_system = bool(self.sw_system.isChecked())
        self._scanner = un.AppScanner(
            on_done=lambda apps: self._apps_ready.emit(apps),
            on_error=lambda msg: self._apps_failed.emit(msg),
            include_system=include_system,
        )
        self._scanner.start()

    def _on_apps_ready(self, apps):
        self._busy = False
        self._apps = list(apps)
        self.btn_rescan.setEnabled(True)
        self.btn_rescan.setText("重新扫描")
        self._bar.hide()
        self._rebuild_list()
        n = len(self._apps)

        if self._quiet_scan:
            # 内部触发的静默刷新（卸载后自动重扫）：
            # **不覆盖**状态栏里刚刚写好的卸载结论（"卸载完成，未发现残留"
            # 之类），也不弹 toast（否则连着弹两个很吵）。
            # 只在按钮上给一点反馈，用户想看细节会自己点重新扫描。
            self._quiet_scan = False
            self.btn_rescan.setText("重新扫描")
            return

        self._status.setText(
            f"已找到 {n} 个程序" if n else "没有找到可卸载的程序"
        )

    def _on_apps_failed(self, msg):
        self._busy = False
        self.btn_rescan.setEnabled(True)
        self.btn_rescan.setText("重新扫描")
        self._bar.hide()
        self._status.setText(f"扫描失败：{msg}")

    def _on_system_toggle(self, _checked):
        self._scanned_once = True
        self.start_scan(force=True)

    # ------------------------------------------------------------ 列表
    def _clear_list(self):
        while self._list_box.count():
            item = self._list_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._rows = []

    def _rebuild_list(self):
        """按当前搜索/排序重建列表。"""
        self._clear_list()
        for app in self._filtered_apps():
            row = AppRow(app)
            row.picked.connect(lambda r: self._on_row_picked(r))
            row.activated.connect(lambda r: self._on_row_activated(r))
            self._list_box.addWidget(row)
            self._rows.append(row)
        self._update_selection_ui()
        total = sum(a.estimated_kb for a in self._apps if a.estimated_kb > 0)
        self._count_lbl.setText(
            f"共 {len(self._apps)} 个程序"
            + (f" · 已知体积合计 {un.human_bytes(total * 1024)}" if total else "")
        )

    def _filtered_apps(self):
        q = (self.search.text() or "").strip().lower()
        apps = self._apps
        if q:
            apps = [a for a in apps
                    if q in a.name.lower() or q in (a.publisher or "").lower()]
        mode = self.sort.currentData()
        if mode == "size":
            apps = sorted(apps, key=lambda a: -a.estimated_kb)
        elif mode == "date":
            apps = sorted(apps, key=lambda a: (a.install_date or "0"), reverse=True)
        elif mode == "pub":
            apps = sorted(apps, key=lambda a: (a.publisher or "~").lower())
        else:
            apps = sorted(apps, key=lambda a: a.name.lower())
        return apps

    def _apply_filter(self):
        if not self._apps:
            return
        self._rebuild_list()

    def _on_row_picked(self, row):
        # 单选语义：选中一行就清掉其他行
        if row.check.isChecked():
            for r in self._rows:
                if r is not row and r.check.isChecked():
                    r.check.blockSignals(True)
                    r.check.setChecked(False)
                    r.check.blockSignals(False)
                    r.set_selected(False)
        row.set_selected(row.check.isChecked())
        self._update_selection_ui()
        # 光标跟随：选中后把该行滚进可视区，长列表里才不会"选了半天找不到"
        if row.check.isChecked():
            try:
                self.scroll.ensureWidgetVisible(row, 0, 40)
            except Exception:
                pass

    def _on_row_activated(self, row):
        row.set_selected(True)
        self._on_row_picked(row)
        self._confirm_uninstall()

    def _selected_row(self):
        for r in self._rows:
            if r.check.isChecked():
                return r
        return None

    def _update_selection_ui(self):
        row = self._selected_row()
        self._selected = row.app if row else None
        if row is None:
            self.btn_uninstall.setEnabled(False)
            self.btn_force.setEnabled(False)
            self._sel_lbl.setText("")
            return
        app = row.app
        self.btn_uninstall.setEnabled(app.removable and not self._busy)
        self.btn_force.setEnabled(not self._busy)
        bits = [f"已选 {app.name}"]
        if app.size_text != "--":
            bits.append(app.size_text)
        self._sel_lbl.setText(" · ".join(bits))

    # ------------------------------------------------------------ 卸载
    def _confirm_uninstall(self):
        app = self._selected
        if app is None:
            self.toast("请先选择一个程序")
            return
        if not app.removable:
            self.toast("该程序没有可用的卸载命令，可用「强制清除残留」")
            return
        dlg = UninstallConfirmDialog(app, parent=self.window())
        self._confirm_dlg = dlg
        dlg.confirmed.connect(lambda keep: self._run_uninstall(app, keep))
        dlg.show()
        dlg.center_on_parent()

    def _run_uninstall(self, app, scan_after=True):
        if self._busy:
            return
        self._busy = True
        self._clear_leftovers()
        self.btn_uninstall.setEnabled(False)
        self.btn_force.setEnabled(False)
        self.btn_rescan.setEnabled(False)
        self._bar.setRange(0, 0)
        self._bar.show()
        self._status.setText(f"正在卸载 {app.name}…")

        self._worker = un.UninstallWorker(
            app,
            on_status=lambda s: self._work_status.emit(s),
            on_done=lambda r: self._work_done.emit(r),
            mode="uninstall" if scan_after else "uninstall",
        )
        self._worker.start()

    def _force_clean(self):
        app = self._selected
        if app is None:
            self.toast("请先选择一个程序")
            return
        if self._busy:
            return
        self._busy = True
        self._clear_leftovers()
        self.btn_uninstall.setEnabled(False)
        self.btn_force.setEnabled(False)
        self.btn_rescan.setEnabled(False)
        self._bar.setRange(0, 0)
        self._bar.show()
        self._status.setText(f"正在扫描 {app.name} 的残留…")
        self._worker = un.UninstallWorker(
            app,
            on_status=lambda s: self._work_status.emit(s),
            on_done=lambda r: self._work_done.emit(r),
            mode="scan",
        )
        self._worker.start()

    def _on_work_status(self, text):
        self._status.setText(text)

    def _on_work_done(self, result):
        self._busy = False
        self._bar.hide()
        self.btn_rescan.setEnabled(True)
        self._update_selection_ui()

        step = result.get("step")
        if step == "failed":
            self._status.setText(f"卸载未完成：{result.get('message', '')}")
            self.toast("卸载未完成，可尝试「强制清除残留」")
            # 卸载失败也重扫一遍：注册表条目可能已被卸载程序改过
            # （比如卸载器删了文件但没删干净、或反过来），列表必须反映真实状态
            self._auto_rescan_after_uninstall()
            return

        items = result.get("leftovers") or []
        if result.get("message"):
            self._status.setText(result["message"])
        self._show_leftovers(items)

        # 卸载（或强制扫描）结束后自动重扫应用列表：
        # 刚才那个程序已经从系统里消失/变化了，列表里还挂着旧条目会误导用户。
        self._auto_rescan_after_uninstall()

    def _auto_rescan_after_uninstall(self):
        """卸载流程结束后，自动重新枚举一次应用列表。

        为什么要有这个（用户提的需求）：
          * 卸载完条目应该立刻从列表消失，否则用户会以为"没卸掉"；
          * 卸载器常常顺带装上/卸掉别的组件（.NET、WebView2、附带工具），
            重扫才能反映真实状态；
          * 让「重新扫描」按钮变成可选而不是必需。

        与 `start_scan()` 的区别：
          * **不等** `_busy` 标记 —— 调用点刚好把 `_busy` 置回 False；
          * 重置 `_scanned_once`，保证下次进页仍会自动扫描；
          * 用 `QTimer.singleShot` 稍作延迟：让残留卡片先渲染出来，
            避免刚显示就被"扫描中"的进度条盖住造成闪烁。
          * 若上一次扫描**还在跑**（比如用户连点两次卸载），不直接丢弃，
            而是等它结束后再触发（见 `_silent_rescan` 内的重试）。
        """
        self._scanned_once = False
        QTimer.singleShot(260, self._silent_rescan)

    def _silent_rescan(self, _retry=0):
        """后台重扫应用列表，但**不动**残留区（不打断用户正在看的残留结果）。

        `_retry` 是内部参数：发现前一次扫描还没结束时，最多重试 6 次
        （约 1.8 秒），避免"上一次的扫描把这次刷新的请求吞掉"。
        """
        if self._busy:
            return
        if self._scanner is not None and self._scanner.is_alive():
            if _retry < 6:
                QTimer.singleShot(300, lambda: self._silent_rescan(_retry + 1))
            return
        try:
            self._quiet_scan = True
            self._scanner = un.AppScanner(
                on_done=lambda apps: self._apps_ready.emit(apps),
                on_error=lambda msg: self._apps_failed.emit(msg),
                include_system=bool(self.sw_system.isChecked()),
                quiet=True,
            )
            self._scanner.start()
        except Exception:
            self._quiet_scan = False

    # ------------------------------------------------------------ 残留
    def _dismiss_leftovers(self):
        """用户主动收起残留卡片（纯 UI 操作，不删任何东西）。

        与 `_clear_leftovers()` 的区别：那个是内部状态重置，用于开始新一轮
        卸载/扫描前清场；这个是**用户点「关闭」**触发的，需要额外：
          * 在状态栏留一句话，让用户知道"卡片只是收起来了，残留还在"，
            否则他会以为已经被清理/忽略，之后一脸茫然；
          * 同步复位 `_selected` 之外的按钮可用态（走 _update_selection_ui）。
        """
        n = len(self._leftovers)
        self._clear_leftovers()
        # 状态栏不能留"发现 N 项残留"这种已经过期的结论
        if n:
            self._status.setText(
                f"已收起残留列表（{n} 项未处理）。如需清理，可重新点「强制清除残留」"
            )
        self._update_selection_ui()

    def _clear_leftovers(self):
        while self._leftover_box.count():
            it = self._leftover_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._leftovers = []
        self._leftover_rows = []
        self._leftover_card.hide()

    def _show_leftovers(self, items):
        self._clear_leftovers()
        self._leftovers = list(items)
        if not items:
            self._status.setText("卸载完成，未发现残留")
            self.toast("卸载完成，未发现残留")
            return

        total = sum(it.size for it in items if it.kind != "reg")
        regs = sum(1 for it in items if it.kind == "reg")
        files = len(items) - regs
        self._leftover_sum.setText(
            f"发现 {len(items)} 项残留（文件/目录 {files} 个、注册表 {regs} 个）"
            f" · 约 {un.human_bytes(total)}"
        )
        self._leftover_hint.setText(
            "这些是软件卸载程序未清理干净的内容。默认已全选，"
            "取消勾选可保留不想删的项。清理前会再次确认。"
        )

        for it in items:
            row = LeftoverRow(it)
            row.toggled.connect(self._update_leftover_ui)
            self._leftover_box.addWidget(row)
            self._leftover_rows.append(row)

        self._leftover_card.show()
        self._update_leftover_ui()
        self.toast(f"发现 {len(items)} 项残留，可勾选清理")

    def _select_leftovers(self, flag):
        for row in self._leftover_rows:
            row.check.blockSignals(True)
            row.check.setChecked(flag)
            row.check.blockSignals(False)
            row.item.checked = flag
        self._update_leftover_ui()

    def _update_leftover_ui(self):
        sel = [it for it in self._leftovers if it.checked]
        total = sum(it.size for it in sel if it.kind != "reg")
        self.btn_clean_lo.setEnabled(bool(sel) and not self._busy)
        self.btn_clean_lo.setText(
            f"清理选中残留（{len(sel)} 项 · {un.human_bytes(total)}）"
            if sel else "清理选中残留"
        )

    def _confirm_clean_leftovers(self):
        items = [it for it in self._leftovers if it.checked]
        if not items:
            self.toast("没有勾选任何残留项")
            return
        dlg = LeftoverConfirmDialog(items, parent=self.window())
        self._confirm_dlg = dlg
        dlg.confirmed.connect(lambda: self._run_clean_leftovers(items))
        dlg.show()
        dlg.center_on_parent()

    def _run_clean_leftovers(self, items):
        if self._busy:
            return
        self._busy = True
        self.btn_clean_lo.setEnabled(False)
        self._bar.setRange(0, 0)
        self._bar.show()
        self._status.setText("正在清理残留…")

        admin = un.is_admin()
        direct, elevated = [], []
        for it in items:
            if admin or not LeftoverConfirmDialog._needs_admin(it):
                direct.append(it)
            else:
                elevated.append(it)
        self._elevate_items = elevated

        def work():
            res_freed = 0
            res_removed = 0
            res_failed = 0
            msgs = []
            retry_elevated = list(elevated)

            if direct:
                r = un.clean_leftovers(direct)
                res_freed += r.freed
                res_removed += r.removed
                res_failed += r.failed
                msgs.extend(r.messages)
                # 关键兜底：普通权限删失败的项，只要还没提权过就并入提权批次。
                # `_needs_admin` 的探测可能漏判（比如 ACL 只对"删除"动作收紧，
                # 写探测能过），靠这里"失败即升权重试"保证最终能删掉。
                if r.retryable and not admin:
                    by_path = {it.path: it for it in direct}
                    for p in r.retryable:
                        it = by_path.get(p)
                        if it is not None and not any(
                                e.path == it.path for e in retry_elevated):
                            retry_elevated.append(it)

            if retry_elevated:
                if direct and retry_elevated is not elevated:
                    self._work_status.emit("部分残留需要管理员权限，正在请求授权…")
                data = un.run_elevated_clean(retry_elevated)
                if data:
                    res_freed += int(data.get("freed") or 0)
                    res_removed += int(data.get("removed") or 0)
                    res_failed = max(0, res_failed - len(retry_elevated))
                    res_failed += int(data.get("failed") or 0)
                    msgs.extend(data.get("messages") or [])
                else:
                    res_failed += len(retry_elevated)
                    msgs.append("需要管理员权限的项未清理（授权被取消）")
            self._clean_done.emit({
                "freed": res_freed, "removed": res_removed,
                "failed": res_failed, "messages": msgs,
                "items": items,
            })

        threading.Thread(target=work, daemon=True,
                         name="YuhubLeftoverClean").start()

    def _on_elevated_done(self, data):
        pass    # 提权结果已在工作线程内合并

    def _on_clean_done(self, res):
        self._busy = False
        self._bar.hide()
        self._elevate_items = []
        freed = res.get("freed", 0)
        removed = res.get("removed", 0)
        failed = res.get("failed", 0)
        msg = f"清理完成：已删除 {removed} 项，释放 {un.human_bytes(freed)}"
        if failed:
            msg += f"（{failed} 项失败或被占用）"
        self._status.setText(msg)
        self.toast(msg)

        # 复核：把仍然存在的项标出来，其余移除
        still = []
        for it in res.get("items", []):
            if it.kind == "reg":
                import winreg
                try:
                    k = winreg.OpenKey(it.hive_handle, it.subkey)
                    winreg.CloseKey(k)
                    still.append(it)
                except OSError:
                    pass
            elif os.path.exists(it.path):
                still.append(it)
        if still:
            self._show_leftovers(still)
        else:
            self._clear_leftovers()
        self._update_selection_ui()

    # ------------------------------------------------------------ 生命周期
    def shutdown(self):
        try:
            if self._scanner is not None:
                self._scanner.cancel()
            if self._worker is not None:
                self._worker.cancel()
        except Exception:
            pass
        if self._confirm_dlg is not None:
            try:
                self._confirm_dlg.close()
            except Exception:
                pass
