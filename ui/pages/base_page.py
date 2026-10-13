"""页面基类：统一的页头（色块图标 + 标题 + 副标题 + 徽标）与可滚动内容区。"""

from PySide6.QtCore import Qt, QEvent, QObject
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QFrame,
    QLayout,
)

from .. import theme


class _CardMinHeightSync(QObject):
    """让「含自动换行说明文字」的卡片，最小高度跟得上当前宽度。

    Qt 在计算布局最小尺寸时用的宽度和卡片最终宽度不一定一致：含 wordWrap
    说明文字的卡片会被**低估**——实测「启动与托盘」卡真实需要 281px，
    上层父布局只给 221px。多出来的 60px 亏空就从卡片内部抠：最下面的
    「立即检查更新」按钮被压成 14~19px（sizeHint 是 37），文字被裁掉。
    内存页、清理页的卡片同理。

    修法：卡片每次 resize 后，按**当前宽度**问布局要真实需要的高度
    （`heightForWidth`），把它写成卡片的 minimumHeight。explicit 最小尺寸
    在 Qt 里是权威值（父布局的 minimumSize 计算必须尊重它），卡片就再也
    压不扁了；内容变高触发 LayoutRequest，外层滚动区会跟着长。
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Resize:
            lay = obj.layout()
            if lay is not None:
                try:
                    need = (lay.heightForWidth(obj.width())
                            if lay.hasHeightForWidth()
                            else lay.minimumSize().height())
                except (TypeError, RuntimeError):
                    return False
                if need and need > 0 and abs(obj.minimumHeight() - need) > 1:
                    obj.setMinimumHeight(need)
        return False


class BasePage(QWidget):
    # 页头徽标文案，子类可覆盖（None 表示不显示徽标）
    BADGE = "功能开发中"
    # 徽标配色："" = 按文案自动（"功能开发中"灰、其余绿）；"warn" = 琥珀色
    BADGE_STYLE = ""

    def __init__(self, title, subtitle, icon="", notify=None, parent=None):
        super().__init__(parent)
        self.notify = notify or (lambda text: None)
        self._icon_key = getattr(self, "TINT_KEY", "tile_1")
        self._build(title, subtitle, icon)

    def _build(self, title, subtitle, icon):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(0)

        # 页头
        header = QHBoxLayout()
        header.setSpacing(14)

        # 页头图标：实心色块（极简色块风格）
        icon_tile = QFrame()
        icon_tile.setFixedSize(42, 42)
        icon_lay = QVBoxLayout(icon_tile)
        icon_lay.setContentsMargins(0, 0, 0, 0)
        icon_lbl = QLabel(icon)
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet("font-size: 20px; background: transparent; color: #ffffff;")
        icon_lay.addWidget(icon_lbl)
        self._icon_tile = icon_tile
        header.addWidget(icon_tile, 0, Qt.AlignTop)

        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("PageTitle")
        s = QLabel(subtitle)
        s.setObjectName("PageSubtitle")
        s.setWordWrap(True)
        title_box.addWidget(t)
        title_box.addWidget(s)
        header.addLayout(title_box, 1)

        badge_text = getattr(self, "BADGE", "功能开发中")
        if badge_text:
            style = getattr(self, "BADGE_STYLE", "")
            if style == "warn":
                badge_name = "BadgeWarn"
            elif badge_text == "功能开发中":
                badge_name = "Badge"
            else:
                badge_name = "BadgeOk"
            badge = QLabel(badge_text)
            badge.setObjectName(badge_name)
            header.addWidget(badge, 0, Qt.AlignTop)

        root.addLayout(header)
        root.addSpacing(16)

        # 可滚动内容区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._content_widget = QWidget()
        self._content = QVBoxLayout(self._content_widget)
        self._content.setContentsMargins(0, 0, 6, 0)
        self._content.setSpacing(12)
        self._content.setAlignment(Qt.AlignTop)

        # 🔴 内容控件的高度**交给 Qt 自己维护**：SetMinimumSize 让布局在每次
        #    激活时把「控件最小高」同步成「布局最小高」。
        #
        # 这里是"软件卸载列表整片空白"的根治点，记下来别再改回去：
        #   旧实现用事件过滤器 + setFixedHeight(max(视口高, 最小高)) 锁高度，
        #   而重算那一步是**延后一拍用 0ms 定时器**做的（还带一个 _pending
        #   去重标志）。LayoutRequest 与那次定时器都可能被丢掉/晚到，于是
        #   内容控件被钉死在**视口高**、可它的布局最小高已经涨到几千像素。
        #   QBoxLayout 在"可用空间 < 最小需求"时不会溢出，而是把**每一项
        #   压成 0 高**——110 行程序全部渲染成 0 像素。表现就是：
        #   表头在、"共 110 个程序 · 已知体积合计 17.1 GB"在、"已找到 110 个
        #   程序"在，**列表区域全空**，且滚不动（滚动条范围也是 0）。
        #   （本机实测复现：show() 之后立刻重建列表 → 行几何 = QRect(x,y,w,0)。）
        #
        #   它同时解释了"为什么只有别的电脑上出问题"：死不死取决于那两拍
        #   事件的先后，机器快慢/字体度量/DPI 一变就可能落在坏的那一侧。
        #
        # 改用 SetMinimumSize 后，最小高由 C++ 在布局激活时维护，没有任何
        # 时序竞态；而原先那段 setFixedHeight 想解决的"内容被撑到 sizeHint、
        # 平白多出一截滚动"也不会回来——滚动区是按 clamp(视口, 最小, 最大)
        # 给内容控件定尺寸的，用的是布局**最小**高而不是 sizeHint。
        self._content.setSizeConstraint(QLayout.SetMinimumSize)

        scroll.setWidget(self._content_widget)
        root.addWidget(scroll, 1)

        # 暴露给子页面，用于响应式布局
        self.scroll = scroll
        self.content_widget = self._content_widget

        theme.bus.changed.connect(lambda _: self._apply_icon_tint())
        self._apply_icon_tint()

    def _apply_icon_tint(self):
        cur = theme.current()
        color = cur.get(self._icon_key, cur["accent"])
        if theme.glass_on():
            # 液态玻璃主题：页头图标换成拟物玻璃贴片
            self._icon_tile.setStyleSheet(
                "QFrame { %s }" % theme.glass_tile(color, radius=10))
        else:
            self._icon_tile.setStyleSheet(
                f"QFrame {{ background: {color}; border: none; border-radius: 5px; }}"
            )

    def add(self, widget, stretch=0):
        """向内容区添加一个控件（stretch 控制占剩余空间的比例）。"""
        self._content.addWidget(widget, stretch)

    def add_layout(self, layout, stretch=0):
        """向内容区添加一个布局（stretch 控制占剩余空间的比例）。"""
        self._content.addLayout(layout, stretch)

    def add_stretch(self):
        self._content.addStretch(1)

    def showEvent(self, event):
        super().showEvent(event)
        # 页面每次显示时把「卡片最小高度同步」挂到所有 CARD 上（幂等）。
        # 放这里而不是 _build_content 之后，是因为卡片也可能在页面构建
        # 之后动态创建，show 时扫一遍最省心。
        self._install_card_height_sync()

    def _install_card_height_sync(self):
        """给所有 #Card 卡片装最小高度同步（见 _CardMinHeightSync）。"""
        sync = getattr(self, "_card_sync_obj", None)
        if sync is None:
            sync = _CardMinHeightSync(self)
            self._card_sync_obj = sync
        for card in self._content_widget.findChildren(QFrame):
            if card.objectName() != "Card":
                continue
            if getattr(card, "_minh_sync_on", False):
                continue
            card._minh_sync_on = True
            card.installEventFilter(sync)

    def on_shown(self):
        """页面被切换到前台时调用（子类可覆盖做惰性初始化 / 自动刷新）。

        每次切入都会调用；需要「只做一次」的子类请自行记忆状态。
        """
        pass

    def toast(self, text):
        self.notify(text)
