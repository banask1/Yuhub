"""页面基类：统一的页头（色块图标 + 标题 + 副标题 + 徽标）与可滚动内容区。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QFrame,
)

from .. import theme


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
        scroll.setWidget(self._content_widget)
        root.addWidget(scroll, 1)

        # 暴露给子页面，用于响应式布局
        self.scroll = scroll
        self.content_widget = self._content_widget

        theme.bus.changed.connect(lambda _: self._apply_icon_tint())
        self._apply_icon_tint()

    def _apply_icon_tint(self):
        color = theme.current().get(self._icon_key, theme.current()["accent"])
        self._icon_tile.setStyleSheet(
            f"QFrame {{ background: {color}; border: none; border-radius: 5px; }}"
        )

    def add(self, widget):
        """向内容区添加一个控件。"""
        self._content.addWidget(widget)

    def add_layout(self, layout):
        self._content.addLayout(layout)

    def add_stretch(self):
        self._content.addStretch(1)

    def on_shown(self):
        """页面被切换到前台时调用（子类可覆盖做惰性初始化 / 自动刷新）。

        每次切入都会调用；需要「只做一次」的子类请自行记忆状态。
        """
        pass

    def toast(self, text):
        self.notify(text)
