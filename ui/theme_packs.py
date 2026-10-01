"""Yuhub 主题包系统：支持从「C盘文档/Yuhub/<主题名>/」加载外部主题。

为什么要做成"外部主题包"
------------------------
原来的主题是写死在 `ui/theme.py` 里的两组色板（DARK / LIGHT），
用户想加一套配色必须改代码。改成主题包后：

    C:\\Users\\<你>\\Documents\\Yuhub\\
        YuUI\\                  ← 主题名即文件夹名
            theme.json          ← 色板（界面上所有颜色）
            theme.qss           ← 样式表模板（可选；缺省时用内置极简色块模板）
            preview.png         ← 预览图（可选）
        <你的新主题>\\
            ...

首次启动会自动创建 `YuUI`（即用户原本在用的极简色块主题）。
用户复制一份改改颜色就成了新主题，程序启动时自动扫描。

与旧 API 的关系
---------------
`ui/theme.py` 仍然对外提供 `set_theme / current / build_qss / bus`，
只是内部改成"主题包驱动"。所有原有调用点（main_window、各 widget）
**不需要改**。
"""

import json
import os
import shutil
import traceback
from string import Template

from PySide6.QtCore import QObject, Signal


# 主题根目录：C:\Users\<名>\Documents\Yuhub
DOCS_DIRNAME = "Yuhub"
FIRST_THEME = "YuUI"
# 内置第二主题（首次启动也会自动落盘）
SECOND_THEME = "sky glass"

# 主题包内约定文件名
FILE_PALETTE = "theme.json"
FILE_QSS = "theme.qss"
FILE_PREVIEW = "preview.png"
FILE_INFO = "info.json"


# ---------------------------------------------------------------------------
# 主题包数据结构
# ---------------------------------------------------------------------------
class ThemePack:
    """一个主题包：名字 + 色板 + QSS 模板 + 目录。"""

    def __init__(self, name, palette, qss=None, path="", builtin=False,
                 info=None):
        self.name = name
        self.palette = palette
        self.qss = qss or ""
        self.path = path
        self.builtin = builtin
        self.info = info or {}

    @property
    def label(self):
        """界面上显示的标题（info.json 的 title 优先）。"""
        return self.info.get("title") or self.name

    @property
    def description(self):
        return self.info.get("description", "")

    @property
    def author(self):
        return self.info.get("author", "")

    @property
    def version(self):
        return self.info.get("version", "")

    def preview_path(self):
        if not self.path:
            return ""
        p = os.path.join(self.path, FILE_PREVIEW)
        return p if os.path.isfile(p) else ""

    def __repr__(self):
        return f"<ThemePack {self.name!r} builtin={self.builtin}>"


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def documents_dir():
    """Windows 的「文档」目录。优先用 shell API（可能被重定向到 OneDrive）。"""
    env = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    # 先试注册表里的真实路径（用户可能把「文档」挪到了别的盘）
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
        val, _ = winreg.QueryValueEx(key, "Personal")
        winreg.CloseKey(key)
        if val and os.path.isdir(val):
            return val
    except Exception:
        pass
    d = os.path.join(env, "Documents")
    return d if os.path.isdir(d) else env


def themes_root():
    """主题根目录 ``...\\Documents\\Yuhub``。"""
    return os.path.join(documents_dir(), DOCS_DIRNAME)


def theme_dir(name):
    return os.path.join(themes_root(), name)


# ---------------------------------------------------------------------------
# 默认主题内容（首次运行时落盘为 YuUI 主题包）
# ---------------------------------------------------------------------------
# 色板与 QSS 模板原样搬自旧 `ui/theme.py`，保证视觉零变化。
DEFAULT_DARK = {
    "window_radius": "8px",
    "window_top": "#0b0b0e",
    "window_bottom": "#0b0b0e",
    "window_border": "#26262e",
    "bg": "#101014",
    "titlebar_bg": "#0b0b0e",
    "sidebar_bg": "#0e0e12",
    "card_top": "#16161b",
    "card_bottom": "#16161b",
    "surface_hover": "#1e1e26",
    "surface_sunken": "#08080a",
    "border": "#242430",
    "border_strong": "#33333f",
    "text": "#f2f4f8",
    "text_dim": "#b9bfcc",
    "text_faint": "#8b93a3",
    "accent": "#3b82f6",
    "accent_hover": "#5b98f8",
    "accent_soft": "#17233c",
    "accent_text": "#ffffff",
    "green": "#3ddc84",
    "red": "#ff5c5c",
    "amber": "#ffb020",
    "purple": "#a78bfa",
    "cyan": "#38bdf8",
    "titlebar_separator": "#1c1c22",
    "sidebar_separator": "#1c1c22",
    "input_bg": "#0c0c10",
    "toggle_off": "#26262e",
    "scrollbar_handle": "#33333f",
    "toast_bg": "#1c1c22",
    "banner_start": "#1d4ed8",
    "banner_end": "#3b82f6",
    "tile_1": "#3b82f6",
    "tile_2": "#3ddc84",
    "tile_3": "#ffb020",
    "tile_4": "#a78bfa",
    "tile_5": "#38bdf8",
    "tile_6": "#fb7185",
    "tile_7": "#6366f1",
}

DEFAULT_LIGHT = {
    "window_radius": "8px",
    "window_top": "#ffffff",
    "window_bottom": "#ffffff",
    "window_border": "#d9dbe0",
    "bg": "#f4f5f7",
    "titlebar_bg": "#ffffff",
    "sidebar_bg": "#eef0f3",
    "card_top": "#ffffff",
    "card_bottom": "#ffffff",
    "surface_hover": "#e9ecf1",
    "surface_sunken": "#e9ecf1",
    "border": "#dfe1e6",
    "border_strong": "#c6cad2",
    "text": "#14161a",
    "text_dim": "#5a6070",
    "text_faint": "#878e9d",
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "accent_soft": "#e4ebfd",
    "accent_text": "#ffffff",
    "green": "#16a34a",
    "red": "#dc2626",
    "amber": "#d97706",
    "purple": "#7c3aed",
    "cyan": "#0284c7",
    "titlebar_separator": "#e6e8ec",
    "sidebar_separator": "#dfe1e6",
    "input_bg": "#ffffff",
    "toggle_off": "#d5d8de",
    "scrollbar_handle": "#c6cad2",
    "toast_bg": "#ffffff",
    "banner_start": "#1d4ed8",
    "banner_end": "#3b82f6",
    "tile_1": "#2563eb",
    "tile_2": "#16a34a",
    "tile_3": "#d97706",
    "tile_4": "#7c3aed",
    "tile_5": "#0284c7",
    "tile_6": "#e11d48",
    "tile_7": "#4f46e5",
}


# ---------------------------------------------------------------------------
# 内置第二主题：sky glass（天空玻璃）
# ---------------------------------------------------------------------------
# 设计手法参考纯 CSS 液态玻璃（gitee greyd097/yzrt「纯CSS液态玻璃」）：
#   - 窗口本体是深靛蓝渐变（模拟桌面壁纸透进来的一层彩色底）
#   - 所有表面用 rgba 半透明白叠加在渐变上 —— 真实的"透底"玻璃感
#     （Qt 的 rgba 背景会与父控件的渐变混合，等效 backdrop 的透底效果）
#   - 玻璃边缘：1px 白色高光描边（glass_highlight），模拟折射
# ⚠️ 以下键会被 QColor() 直接解析（splash 画启动屏、监控图表取色），
#    必须保持 hex，不能写成 rgba()：
#    window_top / window_bottom / window_border / border_strong /
#    toggle_off / accent 系 / tile_* / text 系 / green / red / amber 等。
SKY_DARK = {
    "window_radius": "14px",
    # 窗口渐变（上→下），深色模式是"深夜靛蓝"
    "window_top": "#161b34",
    "window_bottom": "#0c0f22",
    "window_border": "#2c3560",
    "bg": "#10142a",
    # 玻璃表面：白色半透明叠加
    "titlebar_bg": "rgba(255,255,255,18)",
    "titlebar_separator": "rgba(255,255,255,26)",
    "sidebar_bg": "rgba(255,255,255,22)",
    "sidebar_separator": "rgba(255,255,255,26)",
    "card_top": "rgba(255,255,255,26)",
    "card_bottom": "rgba(255,255,255,14)",
    "surface_hover": "rgba(255,255,255,46)",
    "surface_sunken": "rgba(6,8,20,120)",
    "border": "rgba(255,255,255,34)",
    "border_strong": "#39406e",
    "text": "#f2f4ff",
    "text_dim": "#c2c9e8",
    "text_faint": "#8f97c4",
    "accent": "#7aa2ff",
    "accent_hover": "#93b4ff",
    "accent_soft": "rgba(122,162,255,52)",
    "accent_text": "#0a0f24",
    "green": "#5eead4",
    "red": "#ff7b8a",
    "amber": "#ffc75a",
    "purple": "#c0a6ff",
    "cyan": "#6fd8ff",
    "input_bg": "rgba(8,10,26,150)",
    "toggle_off": "#3a4270",
    "scrollbar_handle": "rgba(255,255,255,80)",
    "toast_bg": "rgba(22,27,52,246)",
    "banner_start": "#5b7cff",
    "banner_end": "#8b5cf6",
    "tile_1": "#7aa2ff",
    "tile_2": "#5eead4",
    "tile_3": "#ffc75a",
    "tile_4": "#c0a6ff",
    "tile_5": "#6fd8ff",
    "tile_6": "#ff8fa8",
    "tile_7": "#8b93ff",
    # ---- sky glass 专属键 ----
    # 玻璃边缘高光（描边色）
    "glass_highlight": "rgba(255,255,255,105)",
    # 图标贴片切换到拟物玻璃样式
    "icon_tile_style": "glass",
    # 右键菜单/弹层：接近不透明的深玻璃（太高透明度会让文字压在桌面上读不清）
    "menu_bg": "rgba(20,24,48,242)",
    "menu_border": "rgba(255,255,255,60)",
}

SKY_LIGHT = {
    "window_radius": "14px",
    # 窗口渐变，浅色模式是"晨雾蓝白"
    "window_top": "#eaf0ff",
    "window_bottom": "#d6e2f5",
    "window_border": "#b9c6e2",
    "bg": "#eef2fb",
    "titlebar_bg": "rgba(255,255,255,150)",
    "titlebar_separator": "rgba(120,140,190,60)",
    "sidebar_bg": "rgba(255,255,255,140)",
    "sidebar_separator": "rgba(120,140,190,60)",
    "card_top": "rgba(255,255,255,190)",
    "card_bottom": "rgba(255,255,255,150)",
    "surface_hover": "rgba(255,255,255,225)",
    "surface_sunken": "rgba(140,160,210,70)",
    "border": "rgba(120,140,190,90)",
    "border_strong": "#a9b6d6",
    "text": "#1c2440",
    "text_dim": "#4a5470",
    "text_faint": "#7d87a8",
    "accent": "#3f6fff",
    "accent_hover": "#2e5ae8",
    "accent_soft": "rgba(63,111,255,45)",
    "accent_text": "#ffffff",
    "green": "#0ea5a0",
    "red": "#e5484d",
    "amber": "#d98a00",
    "purple": "#7c5cff",
    "cyan": "#0d94d2",
    "input_bg": "rgba(255,255,255,220)",
    "toggle_off": "#c3cde6",
    "scrollbar_handle": "rgba(90,110,160,120)",
    "toast_bg": "rgba(255,255,255,250)",
    "banner_start": "#4c7dff",
    "banner_end": "#8b5cf6",
    "tile_1": "#4c7dff",
    "tile_2": "#14b8a6",
    "tile_3": "#f59e0b",
    "tile_4": "#8b5cf6",
    "tile_5": "#0ea5e9",
    "tile_6": "#f43f6e",
    "tile_7": "#6366f1",
    "glass_highlight": "rgba(255,255,255,235)",
    "icon_tile_style": "glass",
    "menu_bg": "rgba(252,253,255,248)",
    "menu_border": "rgba(120,140,190,140)",
}


# sky glass 专属 QSS 模板：与内置极简色块模板覆盖**完全相同的选择器**，
# 但把所有表面换成玻璃质感（渐变窗口 + rgba 半透明叠加 + 大圆角）。
SKY_QSS = """\
* {
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    outline: none;
    selection-background-color: $accent;
    selection-color: $accent_text;
}

QWidget {
    color: $text;
    background: transparent;
    font-size: 13px;
}

/* ---------- 窗口外壳：深靛蓝渐变（玻璃的"彩底"） ---------- */
#Root { background: transparent; }

QFrame#AppFrame {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 $window_top, stop:1 $window_bottom);
    border: 1px solid $window_border;
}
QFrame#AppFrame[rounded="true"] { border-radius: $window_radius; }
QFrame#AppFrame[rounded="false"] { border-radius: 0px; }

/* ---------- 标题栏：玻璃横条 ---------- */
#TitleBar {
    background: $titlebar_bg;
    border-bottom: 1px solid $titlebar_separator;
}
QLabel#AppTitle { font-size: 13px; font-weight: 700; color: $text; background: transparent; }
QLabel#AppSubtitle {
    color: $text_dim;
    font-size: 11px;
    background: $surface_hover;
    border: 1px solid $glass_highlight;
    border-radius: 4px;
    padding: 1px 6px;
}

QPushButton#TitleButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    color: $text_dim;
    font-size: 13px;
}
QPushButton#TitleButton:hover {
    background: $surface_hover;
    border-color: $glass_highlight;
    color: $text;
}
QPushButton#TitleButton:checked { background: $accent_soft; border-color: $accent; color: $accent; }
QPushButton#TitleButton[danger="true"]:hover { background: #e81123; border-color: #e81123; color: #ffffff; }

/* ---------- 侧边栏：整条竖玻璃 ---------- */
#Sidebar {
    background: $sidebar_bg;
    border-right: 1px solid $sidebar_separator;
}
QLabel#SidebarSection {
    color: $text_faint;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
    padding: 4px 14px;
    background: transparent;
}

/* 导航按钮：玻璃药丸；选中时染色玻璃 + 高光描边 */
QPushButton#SidebarButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    color: $text_dim;
    text-align: left;
    padding: 9px 12px;
    font-size: 13px;
    margin: 1px 10px;
}
QPushButton#SidebarButton:hover {
    background: $surface_hover;
    border-color: $glass_highlight;
    color: $text;
}
QPushButton#SidebarButton:checked {
    background: $accent_soft;
    color: $accent;
    border: 1px solid $accent;
    font-weight: 700;
}

/* ---------- 卡片：玻璃面板 ---------- */
QFrame#Card {
    background: $card_top;
    border: 1px solid $border;
    border-radius: 12px;
}
QFrame#Card[clickable="true"]:hover {
    background: $surface_hover;
    border: 1px solid $accent;
}

/* ---------- 文本 ---------- */
QLabel#PageTitle { font-size: 21px; font-weight: 800; color: $text; }
QLabel#PageSubtitle { font-size: 13px; color: $text_dim; }
QLabel#CardTitle { font-size: 14px; font-weight: 700; color: $text; }
QLabel#CardDesc { font-size: 12px; color: $text_dim; }

QLabel#Badge {
    color: $text_dim;
    background: $surface_hover;
    border: 1px solid $border_strong;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 11px;
}
QLabel#BadgeOk {
    color: $green;
    background: $surface_hover;
    border: 1px solid $green;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 11px;
}
QLabel#BadgeWarn {
    color: $amber;
    background: $surface_hover;
    border: 1px solid $amber;
    border-radius: 4px;
    padding: 2px 10px;
    font-size: 11px;
}

QLabel#Muted { color: $text_dim; }
QLabel#Faint { color: $text_faint; }
QLabel#Warn { color: $amber; }
QLabel#Success { color: $green; }
QLabel#Danger { color: $red; }
QLabel#Accent { color: $accent; }

/* ---------- 下拉框（玻璃输入件 + 玻璃弹层） ---------- */
QComboBox {
    background: $input_bg;
    border: 1px solid $border_strong;
    border-radius: 8px;
    padding: 7px 10px;
    color: $text;
    min-height: 20px;
}
QComboBox:hover { border-color: $accent; }
QComboBox:focus { border-color: $accent; }
QComboBox:disabled { color: $text_faint; border-color: $border; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    image: url($arrow);
    width: 10px;
    height: 6px;
    margin-right: 8px;
}
QComboBox::down-arrow:hover { image: url($arrow_hover); }
QComboBox::down-arrow:disabled { image: url($arrow_faint); }
QComboBox QAbstractItemView {
    background: $menu_bg;
    border: 1px solid $menu_border;
    border-radius: 8px;
    padding: 4px;
    color: $text;
    outline: none;
    selection-background-color: $accent;
    selection-color: $accent_text;
}
QComboBox QAbstractItemView::item {
    padding: 6px 10px;
    border-radius: 5px;
    min-height: 20px;
}
QComboBox QAbstractItemView::item:hover {
    background: $surface_hover;
    color: $text;
}
QComboBox QAbstractItemView::item:selected {
    background: $accent;
    color: $accent_text;
}

/* ---------- 按钮：主按钮是"彩色玻璃"（渐变） ---------- */
QPushButton#PrimaryButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 $accent_hover, stop:1 $accent);
    color: $accent_text;
    border: 1px solid $glass_highlight;
    border-radius: 9px;
    padding: 10px 20px;
    font-weight: 700;
}
QPushButton#PrimaryButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 $accent, stop:1 $accent_hover);
}
QPushButton#PrimaryButton:pressed { background: $accent; }
QPushButton#PrimaryButton:disabled {
    background: $surface_hover; border-color: $border_strong; color: $text_faint;
}

QPushButton#GhostButton {
    background: $card_top;
    color: $text_dim;
    border: 1px solid $border_strong;
    border-radius: 9px;
    padding: 9px 18px;
}
QPushButton#GhostButton:hover { border-color: $accent; color: $accent; background: $accent_soft; }
QPushButton#GhostButton:disabled { color: $text_faint; border-color: $border; }

QPushButton#DangerButton {
    background: transparent;
    color: $red;
    border: 1px solid $red;
    border-radius: 9px;
    padding: 9px 18px;
}
QPushButton#DangerButton:hover { background: $red; color: #ffffff; }

QPushButton#MiniButton {
    background: $card_top;
    color: $text_dim;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 2px 12px;
    font-size: 11px;
}
QPushButton#MiniButton:hover { border-color: $accent; color: $accent; background: $accent_soft; }
QPushButton#MiniButton:disabled { color: $text_faint; border-color: $border; }

/* ---------- 输入框：下沉玻璃 ---------- */
QLineEdit {
    background: $input_bg;
    border: 1px solid $border_strong;
    border-radius: 8px;
    padding: 9px 12px;
    color: $text;
}
QLineEdit:focus { border-color: $accent; }

/* ---------- 右键菜单：深玻璃浮层 ---------- */
QMenu {
    background: $menu_bg;
    border: 1px solid $menu_border;
    border-radius: 10px;
    padding: 6px;
    color: $text;
}
QMenu::item {
    background: transparent;
    color: $text;
    padding: 7px 28px 7px 14px;
    border-radius: 6px;
}
QMenu::item:selected {
    background: $accent;
    color: $accent_text;
}
QMenu::item:disabled { color: $text_faint; }
QMenu::separator {
    height: 1px;
    background: $border;
    margin: 5px 8px;
}

/* ---------- 进度条 ---------- */
QProgressBar {
    background: $surface_sunken;
    border: 1px solid $border;
    border-radius: 4px;
    text-align: center;
    color: transparent;
    height: 8px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 $accent, stop:1 $cyan);
    border-radius: 3px;
}

/* ---------- 复选框：玻璃小方片 ---------- */
QCheckBox { spacing: 9px; color: $text; background: transparent; }
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid $border_strong;
    border-radius: 5px;
    background: $input_bg;
}
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked {
    background: $accent;
    border-color: $glass_highlight;
}
QCheckBox::indicator:disabled { border-color: $border; background: $surface_sunken; }

/* 清理项行 */
QFrame#CleanRow {
    background: $card_top;
    border: 1px solid $border;
    border-radius: 8px;
}
QFrame#CleanRow:hover { border-color: $border_strong; }
QLabel#CleanName { font-size: 13px; font-weight: 700; color: $text; background: transparent; }
QLabel#CleanDesc { font-size: 11px; color: $text_dim; background: transparent; }
QLabel#CleanSize { font-size: 13px; font-weight: 700; color: $text; background: transparent; }
QLabel#GroupTitle { font-size: 12px; font-weight: 700; color: $text_dim; background: transparent; }

/* 磁盘占用条 */
QFrame#DiskBar {
    background: $surface_sunken;
    border: 1px solid $border;
    border-radius: 5px;
}

/* ---------- 多线程下载页 ---------- */
QFrame#DlMetric {
    background: $card_top;
    border: 1px solid $border;
    border-radius: 8px;
}
QLabel#DlMetricLabel { font-size: 11px; color: $text_faint; background: transparent; }
QLabel#DlMetricValue {
    font-size: 14px; font-weight: 700; color: $text; background: transparent;
}
QLabel#DlFileName { font-size: 14px; font-weight: 700; color: $text; background: transparent; }
QLabel#DlSub { font-size: 11px; color: $text_faint; background: transparent; }
QLabel#DlState {
    font-size: 11px; font-weight: 700; background: transparent;
    border-radius: 4px; padding: 2px 9px;
}
QLabel#DlStateIdle { font-size: 11px; font-weight: 700; color: $text_dim;
                     background: transparent; border: 1px solid $border_strong;
                     border-radius: 4px; padding: 2px 9px; }
QLabel#DlStateOk { font-size: 11px; font-weight: 700; color: $green;
                   background: transparent; border: 1px solid $green;
                   border-radius: 4px; padding: 2px 9px; }
QLabel#DlStateErr { font-size: 11px; font-weight: 700; color: $red;
                    background: transparent; border: 1px solid $red;
                    border-radius: 4px; padding: 2px 9px; }
QLabel#DlStateWarn { font-size: 11px; font-weight: 700; color: $amber;
                     background: transparent; border: 1px solid $amber;
                     border-radius: 4px; padding: 2px 9px; }
QLabel#DlBlockLabel { font-size: 11px; color: $text_faint; background: transparent; }

/* ---------- 分段选择器 ---------- */
QFrame#Segment {
    background: $surface_sunken;
    border: 1px solid $border;
    border-radius: 9px;
}
QPushButton#SegmentButton {
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 6px 14px;
    color: $text_dim;
}
QPushButton#SegmentButton:hover { color: $text; background: $surface_hover; }
QPushButton#SegmentButton:checked { background: $accent; color: $accent_text; font-weight: 700; }

/* ---------- 弹窗 ---------- */
QDialog {
    background: transparent;
    color: $text;
}
QFrame#DialogFrame {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 $window_top, stop:1 $window_bottom);
    border: 1px solid $window_border;
    border-radius: $window_radius;
}
QFrame#DialogHeader { background: transparent; border: none; }
QDialog QLabel { color: $text; background: transparent; }
QLabel#DialogTitle { color: $text; font-size: 17px; font-weight: 800; background: transparent; }
QLabel#DialogSection {
    color: $text_dim;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    background: transparent;
    padding: 12px 0 6px 0;
}
QLabel#DialogKey { color: $text_dim; background: transparent; }
QLabel#DialogVal { color: $text; background: transparent; }
QLabel#DialogValStrong { color: $text; font-weight: 700; background: transparent; }
QFrame#DetailRow { background: $card_top; border: 1px solid $border; border-radius: 8px; }
QDialog QScrollArea { background: transparent; }
QDialog QScrollArea > QWidget > QWidget { background: transparent; }

/* ---------- 局域网联机页 ---------- */
QLabel#LanCode {
    font-size: 25px;
    font-weight: 800;
    color: $accent;
    background: $accent_soft;
    border: 1px solid $accent;
    border-radius: 8px;
    padding: 5px 16px;
    letter-spacing: 3px;
}
QLabel#LanCode[off="true"] {
    color: $text_faint;
    background: $surface_sunken;
    border: 1px solid $border_strong;
}
QFrame#LanMember {
    background: $card_top;
    border: 1px solid $border;
    border-radius: 8px;
}
QLabel#LanMemberName { font-size: 13px; font-weight: 700; color: $text; background: transparent; }
QLabel#LanMemberMeta { font-size: 11px; color: $text_faint; background: transparent; }
QFrame#LanStat { background: $card_top; border: 1px solid $border; border-radius: 8px; }
QLabel#LanStatLabel { font-size: 11px; color: $text_faint; background: transparent; }
QLabel#LanStatValue { font-size: 13px; font-weight: 700; color: $text; background: transparent; }
QFrame#LanFwd { background: $card_top; border: 1px solid $border; border-radius: 8px; }
QPlainTextEdit#LanLog {
    background: $input_bg;
    border: 1px solid $border;
    border-radius: 8px;
    color: $text_dim;
    font-family: "Consolas", "Cascadia Mono", "Courier New", monospace;
    font-size: 11px;
    padding: 6px;
}

/* ---------- 滚动区域 ---------- */
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scrollbar_handle; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: $accent; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scrollbar_handle; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: $accent; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QToolTip {
    background: $toast_bg;
    color: $text;
    border: 1px solid $menu_border;
    border-radius: 6px;
    padding: 6px 10px;
}
"""


# ---------------------------------------------------------------------------
# 主题包落盘 / 读取
# ---------------------------------------------------------------------------
def _pack_theme_json(name, title, description, dark, light):
    return {
        "name": name,
        "title": title,
        "description": description,
        "author": "Yuhub",
        "version": "1.0",
        "dark": dark,
        "light": light,
    }


def ensure_builtin_theme():
    """首次运行：把内置主题写到文档目录（YuUI + sky glass）。

    返回 (是否新建, YuUI 主题目录)。
    """
    root = themes_root()
    d = theme_dir(FIRST_THEME)
    created = False
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return False, d
    tjson = os.path.join(d, FILE_PALETTE)
    if not os.path.isfile(tjson):
        try:
            data = _pack_theme_json(
                FIRST_THEME, "YuUI 极简色块",
                "Yuhub 默认主题：实心色块、小圆角、1px 硬边框，"
                "深色底近黑，对比度高。",
                DEFAULT_DARK, DEFAULT_LIGHT)
            with open(tjson, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            created = True
        except OSError:
            pass
    _ensure_sky_glass_theme()
    # 同时放一份 README，告诉用户怎么自制主题
    readme = os.path.join(root, "如何自制主题.txt")
    if not os.path.isfile(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(_THEME_README)
        except OSError:
            pass
    return created, d


def _ensure_sky_glass_theme():
    """内置 sky glass 主题：不存在才写，绝不覆盖用户的改动。

    主题包带专属 theme.qss（玻璃质感模板），色板里还有玻璃专属键
    （glass_highlight / icon_tile_style / menu_bg 等）。
    """
    d = theme_dir(SECOND_THEME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return
    tjson = os.path.join(d, FILE_PALETTE)
    if not os.path.isfile(tjson):
        try:
            data = _pack_theme_json(
                SECOND_THEME, "Sky Glass 天空玻璃",
                "天空玻璃主题：靛蓝渐变底 + 半透明玻璃面板 + 拟物玻璃图标贴片，"
                "右键菜单为深玻璃浮层。深浅两套模式齐备。",
                SKY_DARK, SKY_LIGHT)
            with open(tjson, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    qss_path = os.path.join(d, FILE_QSS)
    if not os.path.isfile(qss_path):
        try:
            with open(qss_path, "w", encoding="utf-8") as f:
                f.write(SKY_QSS)
        except OSError:
            pass


_THEME_README = """\
Yuhub 主题目录
==============

这里的每个文件夹就是一个主题（文件夹名 = 主题名）。
Yuhub 启动时会自动扫描本目录，在「设置中心 → 外观」里列出来供你切换。

目录结构
--------
    Documents\\Yuhub\\
        YuUI\\               ← 默认主题（极简色块）
            theme.json      ← 色板，改这里就能改颜色
            theme.qss       ← 可选。不提供则使用内置样式表模板
            preview.png     ← 可选。设置页里显示的预览图
        MyTheme\\            ← 你自己复制的
            theme.json
            ...

theme.json 的最小写法
--------------------
    {
      "name": "MyTheme",
      "title": "我的主题",
      "description": "随便写点介绍",
      "dark":  { "accent": "#ff6600", ... },
      "light": { "accent": "#cc5500", ... }
    }

要点
----
* name 建议与文件夹名一致（不一致时以文件夹名为准）。
* dark / light 两套色板都要有。界面在「跟随系统」模式下会按系统深浅色选。
* 色板是"缺省合并"的：只写你想改的键，其余自动继承 YuUI 的值。
  所以做一个新主题，最少只要写 {"dark": {"accent": "#ff6600"}} 这么点。
* 完整的键名清单可以直接抄 YuUI/theme.json。

新增主题后重启 Yuhub 即可看到；也可以直接在设置页点「重新扫描主题」。
"""


def load_pack(dir_path, default_dark=None, default_light=None):
    """从目录读一个主题包。读不出来返回 None（跳过该目录）。"""
    if not os.path.isdir(dir_path):
        return None
    tjson = os.path.join(dir_path, FILE_PALETTE)
    if not os.path.isfile(tjson):
        return None
    try:
        with open(tjson, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    name = os.path.basename(dir_path.rstrip("\\/")) or data.get("name") or "?"

    # 缺省合并：用户只写要改的键，其余继承默认主题
    base_dark = dict(default_dark or DEFAULT_DARK)
    base_light = dict(default_light or DEFAULT_LIGHT)
    dark = dict(base_dark)
    light = dict(base_light)
    if isinstance(data.get("dark"), dict):
        dark.update({k: str(v) for k, v in data["dark"].items()})
    if isinstance(data.get("light"), dict):
        light.update({k: str(v) for k, v in data["light"].items()})

    # 主题可能只给一套色板：缺的那套退回默认，避免切换时崩
    palette = {"dark": dark, "light": light}

    qss = ""
    qp = os.path.join(dir_path, FILE_QSS)
    if os.path.isfile(qp):
        try:
            with open(qp, "r", encoding="utf-8") as f:
                qss = f.read()
        except OSError:
            qss = ""

    info = {}
    ip = os.path.join(dir_path, FILE_INFO)
    if os.path.isfile(ip):
        try:
            with open(ip, "r", encoding="utf-8") as f:
                info = json.load(f) or {}
        except (OSError, ValueError):
            info = {}
    # theme.json 顶层的 title/description 也算元信息
    for k in ("title", "description", "author", "version"):
        if data.get(k) and k not in info:
            info[k] = data[k]

    return ThemePack(name, palette, qss=qss, path=dir_path,
                     builtin=(name == FIRST_THEME), info=info)


def scan_packs(default_dark=None, default_light=None):
    """扫描主题目录，返回 {name: ThemePack}。

    目录不存在时会先创建并写入默认 YuUI 主题。
    """
    ensure_builtin_theme()
    root = themes_root()
    packs = {}
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        entries = []
    for nm in entries:
        d = os.path.join(root, nm)
        if not os.path.isdir(d):
            continue
        # 跳过隐藏目录与临时目录。注意**不要跳过下划线开头**：
        # 用户可能就想用 _MyTheme 这样的名字。
        if nm.startswith(".") or nm in ("__pycache__", "temp", "tmp"):
            continue
        pack = load_pack(d, default_dark, default_light)
        if pack is not None:
            packs[pack.name] = pack

    # 兜底：一个主题包都没读出来时，用内存里的内置主题，
    # 保证程序永远有界面可显示（比如文档目录没写权限的机器）。
    if not packs:
        packs[FIRST_THEME] = ThemePack(
            FIRST_THEME,
            {"dark": dict(default_dark or DEFAULT_DARK),
             "light": dict(default_light or DEFAULT_LIGHT)},
            builtin=True,
            info={"title": "YuUI 极简色块（内置）",
                  "description": "未能读取文档目录，使用内置色板。"})
    return packs


def create_theme_from_template(new_name, source_pack):
    """按现有主题复制一份新主题到文档目录。返回 (成功, 消息/路径)。"""
    new_name = (new_name or "").strip()
    if not new_name:
        return False, "主题名不能为空"
    bad = set('\\/:*?"<>|')
    if any(c in bad for c in new_name):
        return False, "主题名不能包含 \\ / : * ? \" < > | 这些字符"
    if new_name in (".", ".."):
        return False, "主题名不合法"
    dest = theme_dir(new_name)
    if os.path.exists(dest):
        return False, f"主题「{new_name}」已存在"
    try:
        os.makedirs(dest, exist_ok=False)
    except OSError as exc:
        return False, f"创建目录失败：{exc}"

    src = source_pack.palette
    data = _pack_theme_json(
        new_name, new_name,
        f"从「{source_pack.label}」复制而来，改改颜色就是新主题。",
        src.get("dark", DEFAULT_DARK), src.get("light", DEFAULT_LIGHT))
    try:
        with open(os.path.join(dest, FILE_PALETTE), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        return False, f"写入 theme.json 失败：{exc}"
    return True, dest


def delete_theme(name):
    """删除一个主题目录（默认主题 YuUI 不允许删）。"""
    if name == FIRST_THEME:
        return False, "默认主题不可删除"
    d = theme_dir(name)
    if not os.path.isdir(d):
        return False, "主题不存在"
    try:
        shutil.rmtree(d)
    except OSError as exc:
        return False, f"删除失败：{exc}"
    return True, "已删除"


def open_themes_folder():
    """在资源管理器里打开主题目录。返回是否成功。"""
    d = themes_root()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return False
    try:
        os.startfile(d)          # noqa: S606  (Windows-only，项目本就只跑 Windows)
        return True
    except Exception:
        try:
            import subprocess
            subprocess.Popen(["explorer", d])
            return True
        except Exception:
            return False
