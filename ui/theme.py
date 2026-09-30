"""Yuhub 主题系统：极简色块（Pixel-UI）风格 + 主题包驱动 + 动态 QSS。

架构（v0.11 起）
----------------
主题不再是写死的色板，而是**可插拔主题包**，存放在：

    C:\\Users\\<你>\\Documents\\Yuhub\\<主题名>\\
        theme.json     色板
        theme.qss      样式表模板（可选）
        preview.png    预览图（可选）

首次启动自动创建默认主题 ``YuUI``（即原来的极简色块风格）。
用户复制一份改改颜色就是新主题，程序启动时自动扫描。

对外 API 保持不变（`set_theme / get_theme / current / build_qss / bus`），
所以 main_window 与各页面**无需改动**。

新增 API：
    theme.packs()            所有可用主题 {name: ThemePack}
    theme.set_pack(name)     切到某个主题包
    theme.reload_packs()     重新扫描文档目录
    theme.resolve_theme(s)   把 'system' 解析成 'dark'/'light'
"""

from string import Template
import os

from PySide6.QtCore import QObject, Signal

from . import theme_packs as packs_mod


# ---------------------------------------------------------------------------
# 内置默认色板（作为"用户色板的缺省值"，也是主题目录不可写时的兜底）
# ---------------------------------------------------------------------------
DARK = dict(packs_mod.DEFAULT_DARK)
LIGHT = dict(packs_mod.DEFAULT_LIGHT)


class _ThemeBus(QObject):
    changed = Signal(str)
    # 主题列表变化（扫描到新主题 / 删除主题）时发出
    packs_changed = Signal()


bus = _ThemeBus()


# 当前状态
_current_pack_name = packs_mod.FIRST_THEME
_current_mode = "dark"                      # dark / light
_packs = {}                                 # {name: ThemePack}

# 兼容旧代码里 `from .theme import COLORS` 的写法，随主题原地更新
COLORS = dict(DARK)
# 兼容旧代码 `from .theme import PALETTES`（旧的两套色板）
PALETTES = {"dark": DARK, "light": LIGHT}

WINDOW_RADIUS = DARK["window_radius"]


# ---------------------------------------------------------------------------
# 主题包管理
# ---------------------------------------------------------------------------
def reload_packs():
    """重新扫描文档目录里的主题包。返回 {name: ThemePack}。"""
    global _packs, _current_pack_name
    _packs = packs_mod.scan_packs(packs_mod.DEFAULT_DARK,
                                  packs_mod.DEFAULT_LIGHT)
    # 当前主题被删掉时，落回默认主题
    if _current_pack_name not in _packs:
        fallback = (packs_mod.FIRST_THEME if packs_mod.FIRST_THEME in _packs
                    else next(iter(_packs)))
        _current_pack_name = fallback
    bus.packs_changed.emit()
    return _packs


def packs():
    """当前已知的主题包 {name: ThemePack}（不会触发扫描）。"""
    if not _packs:
        reload_packs()
    return dict(_packs)


def pack_names():
    """主题名列表，默认主题排最前，其余按名称排序。"""
    if not _packs:
        reload_packs()
    names = list(_packs.keys())
    first = packs_mod.FIRST_THEME
    names.sort(key=lambda n: (n != first, n.lower()))
    return names


def get_pack():
    if not _packs:
        reload_packs()
    return _packs.get(_current_pack_name) or next(iter(_packs.values()))


def set_pack(name):
    """切到指定主题包（保留当前的深/浅模式）。"""
    global _current_pack_name
    if not _packs:
        reload_packs()
    if name in _packs:
        _current_pack_name = name
    _sync_colors()
    bus.changed.emit(_current_mode)


def themes_root():
    return packs_mod.themes_root()


# ---------------------------------------------------------------------------
# 深/浅模式
# ---------------------------------------------------------------------------
def set_theme(name):
    """设置当前**模式**：'dark' / 'light'（兼容旧 API）。

    注意与 `set_pack` 的区别：这个是"深色还是浅色"，
    那个是"用哪套主题包"。
    """
    global _current_mode
    _current_mode = name if name in ("dark", "light") else "dark"
    _sync_colors()
    bus.changed.emit(_current_mode)


def get_theme():
    return _current_mode


def current():
    """当前生效的完整色板（主题包 + 模式合成后的结果）。"""
    pk = get_pack()
    return pk.palette.get(_current_mode) or pk.palette["dark"]


def current_pack_name():
    return get_pack().name


def resolve_theme(setting):
    """把 'system' 解析成实际的 'dark'/'light'（默认 dark）。"""
    if setting == "system":
        return "light" if _system_is_light() else "dark"
    return setting if setting in ("dark", "light") else "dark"


def _sync_colors():
    """把当前生效色板同步到全局 COLORS / PALETTES（兼容旧调用点）。"""
    pk = get_pack()
    for pname, pal in pk.palette.items():
        PALETTES[pname] = pal
    COLORS.clear()
    COLORS.update(current())


def _system_is_light():
    """读取 Windows 深浅色设置（AppsUseLightTheme）。失败则按深色处理。"""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return value == 1
    except Exception:
        return False


# ---------------------------------------------------------------------------
# QSS 模板 —— 极简色块：无渐变、无半透明、小圆角、硬边框
# ---------------------------------------------------------------------------
_QSS = Template(
    """
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

/* ---------- 窗口外壳（实心色块，无玻璃） ---------- */
#Root { background: transparent; }

QFrame#AppFrame {
    background: $window_top;
    border: 1px solid $window_border;
}
QFrame#AppFrame[rounded="true"] { border-radius: $window_radius; }
QFrame#AppFrame[rounded="false"] { border-radius: 0px; }

/* ---------- 标题栏 ---------- */
#TitleBar {
    background: $titlebar_bg;
    border-bottom: 1px solid $titlebar_separator;
}
QLabel#AppTitle { font-size: 13px; font-weight: 700; color: $text; background: transparent; }
QLabel#AppSubtitle {
    color: $text_faint;
    font-size: 11px;
    background: $surface_hover;
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
QPushButton#TitleButton:hover { background: $surface_hover; border-color: $border_strong; color: $text; }
QPushButton#TitleButton:checked { background: $accent_soft; border-color: $accent; color: $accent; }
QPushButton#TitleButton[danger="true"]:hover { background: #e81123; border-color: #e81123; color: #ffffff; }

/* ---------- 侧边栏 ---------- */
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

QPushButton#SidebarButton {
    background: transparent;
    border: none;
    border-radius: 5px;
    color: $text_dim;
    text-align: left;
    padding: 10px 12px;
    font-size: 13px;
    margin: 1px 10px;
}
QPushButton#SidebarButton:hover { background: $surface_hover; color: $text; }
QPushButton#SidebarButton:checked {
    background: $accent_soft;
    color: $accent;
    border: 1px solid $accent;
    font-weight: 700;
}

/* ---------- 卡片（实心色块 + 硬边框） ---------- */
QFrame#Card {
    background: $card_top;
    border: 1px solid $border;
    border-radius: 5px;
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

/* ---------- 下拉框 ----------
   必须显式写。原因和 QMenu 那节一模一样：
   顶部 `QWidget { background: transparent }` 的通配规则会作用到 QComboBox
   及其弹出列表。QComboBox 是原生绘制的复合控件，本体被刷成透明后只剩
   一个深色底（或漏出系统底色），而下拉弹出的 `QAbstractItemView` 是
   独立顶层窗口，不写样式就是"深色底 + 主题色文字"，在浅色主题下几乎
   不可读。

   三个必须写全的点：
     1. `QComboBox` 本体 —— 底、边框、内边距、文字色
     2. `QComboBox::drop-down` + `::down-arrow` —— 右侧箭头。见下方注释：
        必须用图片，不能用 border 拼三角
     3. `QComboBox QAbstractItemView` —— **下拉弹出列表**。注意选择器要用
        后代写法（不是 `QComboBox::item`），条目高亮用 `::item:selected`。 */
QComboBox {
    background: $input_bg;
    border: 1px solid $border_strong;
    border-radius: 5px;
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
/* 箭头必须用图片，**不能**用 border 拼三角。
   实测（150% 缩放，把控件右侧裁出来放大 8 倍看像素）：
   写 `border-style: solid; border-width: 5px 4px 0 4px; width:0; height:0`
   这种 CSS 经典三角写法，Qt 并不按 border 边角对接的规则渲染，
   而是老老实实画出一个 15x8 的**实心矩形**。
   （原因：`::down-arrow` 是子控件，Qt 走的是 `QStyleSheetStyle` 的
   图片绘制分支；不给 `image:` 时只做背景/边框填充，没有边角斜接。）

   所以这里用内联 SVG 画三角。`$arrow` 由 build_qss() 注入一张
   data:image/svg+xml;base64 的 URL，颜色跟当前主题的 text_dim 一致。 */
QComboBox::down-arrow {
    image: url($arrow);
    width: 10px;
    height: 6px;
    margin-right: 8px;
}
QComboBox::down-arrow:hover { image: url($arrow_hover); }
QComboBox::down-arrow:disabled { image: url($arrow_faint); }
QComboBox QAbstractItemView {
    background: $card_top;
    border: 1px solid $border_strong;
    border-radius: 5px;
    padding: 4px;
    color: $text;
    outline: none;
    /* 不写 selection-background-color 的话，悬停项会用系统高亮色，
       在深色主题下是很扎眼的亮蓝，与整体配色不搭。 */
    selection-background-color: $accent;
    selection-color: $accent_text;
}
QComboBox QAbstractItemView::item {
    padding: 6px 10px;
    border-radius: 3px;
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

/* ---------- 按钮 ---------- */
QPushButton#PrimaryButton {
    background: $accent;
    color: $accent_text;
    border: 1px solid $accent;
    border-radius: 6px;
    padding: 10px 20px;
    font-weight: 700;
}
QPushButton#PrimaryButton:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#PrimaryButton:pressed { background: $accent; }
QPushButton#PrimaryButton:disabled {
    background: $surface_hover; border-color: $border_strong; color: $text_faint;
}

QPushButton#GhostButton {
    background: transparent;
    color: $text_dim;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 9px 18px;
}
QPushButton#GhostButton:hover { border-color: $accent; color: $accent; background: $accent_soft; }
QPushButton#GhostButton:disabled { color: $text_faint; border-color: $border; }

QPushButton#DangerButton {
    background: transparent;
    color: $red;
    border: 1px solid $red;
    border-radius: 6px;
    padding: 9px 18px;
}
QPushButton#DangerButton:hover { background: $red; color: #ffffff; }

QPushButton#MiniButton {
    background: transparent;
    color: $text_dim;
    border: 1px solid $border_strong;
    border-radius: 4px;
    padding: 2px 12px;
    font-size: 11px;
}
QPushButton#MiniButton:hover { border-color: $accent; color: $accent; background: $accent_soft; }
QPushButton#MiniButton:disabled { color: $text_faint; border-color: $border; }

/* ---------- 输入框 ---------- */
QLineEdit {
    background: $input_bg;
    border: 1px solid $border_strong;
    border-radius: 5px;
    padding: 9px 12px;
    color: $text;
}
QLineEdit:focus { border-color: $accent; }

/* ---------- 右键菜单 ----------
   上面那条 `QWidget { background: transparent }` 通配规则**也会作用到 QMenu**。
   QMenu 是原生弹窗窗口，"透明背景"会让它自身的底不被绘制 → 漏出窗口默认的黑底；
   而文字颜色又跟着主题走，于是浅色主题变成"近黑文字压黑底"（完全看不见），
   深色主题反而勉勉强强能读。所以必须给 QMenu 显式补一套实心样式。
   注意条目要写在 QMenu::item 上，只给 QMenu 设 background 的话菜单项仍是系统的白底。 */
QMenu {
    background: $card_top;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 5px;
    color: $text;
}
QMenu::item {
    background: transparent;
    color: $text;
    padding: 7px 28px 7px 14px;
    border-radius: 4px;
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
    border-radius: 2px;
    text-align: center;
    color: transparent;
    height: 8px;
}
QProgressBar::chunk { background: $accent; border-radius: 0px; }

/* ---------- 复选框（极简色块：方形勾选框） ---------- */
QCheckBox { spacing: 9px; color: $text; background: transparent; }
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid $border_strong;
    border-radius: 3px;
    background: $surface_sunken;
}
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked {
    background: $accent;
    border-color: $accent;
    image: none;
}
QCheckBox::indicator:disabled { border-color: $border; background: $surface_sunken; }

/* 清理项行（可勾选） */
QFrame#CleanRow {
    background: $surface_sunken;
    border: 1px solid $border;
    border-radius: 4px;
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
    border-radius: 3px;
}

/* ---------- 多线程下载页 ---------- */
QFrame#DlMetric {
    background: $surface_sunken;
    border: 1px solid $border;
    border-radius: 4px;
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
    border-radius: 6px;
}
QPushButton#SegmentButton {
    background: transparent;
    border: none;
    border-radius: 4px;
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
    background: $window_top;
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
QFrame#DetailRow { background: $surface_sunken; border: 1px solid $border; border-radius: 4px; }
QDialog QScrollArea { background: transparent; }
QDialog QScrollArea > QWidget > QWidget { background: transparent; }

/* ---------- 局域网联机页 ---------- */
QLabel#LanCode {
    font-size: 25px;
    font-weight: 800;
    color: $accent;
    background: $accent_soft;
    border: 1px solid $accent;
    border-radius: 5px;
    padding: 5px 16px;
    letter-spacing: 3px;
}
QLabel#LanCode[off="true"] {
    color: $text_faint;
    background: $surface_sunken;
    border: 1px solid $border_strong;
}
QFrame#LanMember {
    background: $surface_sunken;
    border: 1px solid $border;
    border-radius: 4px;
}
QLabel#LanMemberName { font-size: 13px; font-weight: 700; color: $text; background: transparent; }
QLabel#LanMemberMeta { font-size: 11px; color: $text_faint; background: transparent; }
QFrame#LanStat { background: $surface_sunken; border: 1px solid $border; border-radius: 4px; }
QLabel#LanStatLabel { font-size: 11px; color: $text_faint; background: transparent; }
QLabel#LanStatValue { font-size: 13px; font-weight: 700; color: $text; background: transparent; }
QFrame#LanFwd { background: $surface_sunken; border: 1px solid $border; border-radius: 4px; }
QPlainTextEdit#LanLog {
    background: $surface_sunken;
    border: 1px solid $border;
    border-radius: 4px;
    color: $text_dim;
    font-family: "Consolas", "Cascadia Mono", "Courier New", monospace;
    font-size: 11px;
    padding: 6px;
}

/* ---------- 滚动区域 ---------- */
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scrollbar_handle; border-radius: 2px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: $accent; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scrollbar_handle; border-radius: 2px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: $accent; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QToolTip {
    background: $toast_bg;
    color: $text;
    border: 1px solid $border_strong;
    border-radius: 4px;
    padding: 6px 10px;
}
"""
)


_ARROW_CACHE = {}


def _arrow_cache_dir():
    """下拉框箭头 PNG 的缓存目录。

    放临时目录而不是文档目录：这些是纯运行期产物，用户不该看到，也不该
    备份进主题文件夹里。打包成 exe 后 `sys.frozen` 下 cwd 可能不可写，
    所以固定用 `%TEMP%/yuhub_arrows`。
    """
    import tempfile

    d = os.path.join(tempfile.gettempdir(), "yuhub_arrows")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _arrow_url(color: str) -> str:
    """给 QSS 的 `image:` 返回一张"向下实心三角"图片的**文件路径**。

    踩过的坑（实测，见 _svg_probe 系列脚本）：
    Qt 的 QSS `image:` 属性**不支持 data: URI**，也不支持 `file:///` 前缀：
      - `url(data:image/svg+xml;base64,...)`  -> 什么都不画
      - `url(file:///C:/x.svg)`               -> 什么都不画
      - `url(C:/x.svg)` （裸绝对路径）         -> ✅ 正常
    而 QImageReader 本身是能读 SVG 的，所以问题出在 QSS 的资源加载分支上。
    结论：只能落盘成真实文件，再用裸绝对路径引用。

    又因为箭头颜色要跟随主题（主题包是运行时扫描出来的），不能预置一张
    固定颜色的图片。所以这里按颜色做缓存，每种颜色只生成一次。

    ⚠️ 不能用 QImage/QPainter 来画这张图。`build_qss()` 会在
    `QApplication` 创建之前被调用（比如做自检、或者算窗口初始样式），
    此时构造 QImage/QPainter 会让进程**直接崩掉**（无 Python 异常，
    退出码 127）。所以这里用 zlib + struct 手写一个最小 PNG，
    全程只依赖标准库。
    """
    if color in _ARROW_CACHE:
        return _ARROW_CACHE[color]

    d = _arrow_cache_dir()
    key = "".join(ch for ch in color.lstrip("#").lower() if ch in "0123456789abcdef")
    path = os.path.join(d, "arrow_%s.png" % (key or "default"))
    if not os.path.exists(path):
        try:
            _write_triangle_png(path, color, w=20, h=12)
        except Exception:
            return ""

    # QSS 里的 url() 用正斜杠，反斜杠会被当成转义
    url = path.replace("\\", "/")
    _ARROW_CACHE[color] = url
    return url


def _write_triangle_png(path: str, color: str, w: int = 20, h: int = 12):
    """手写一个带 alpha 的 RGBA PNG：上半部分是底边，向下收成一个尖。

    纯标准库（zlib + struct），不碰 Qt，因此可以在 QApplication
    创建之前安全调用。

    图形：顶边从 (0,0) 到 (w-1,0)，底尖在 (w/2, h-1)。
    横向采样时按三角形在该行的实际半宽做 4x 超采样，得到平滑边缘。
    """
    import struct
    import zlib

    # 解析 #rrggbb
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        c = "b9bfcc"
    r0 = int(c[0:2], 16)
    g0 = int(c[2:4], 16)
    b0 = int(c[4:6], 16)

    cx = (w - 1) / 2.0
    half = w / 2.0
    sub = 4  # 每像素横向超采样次数

    rows = []
    for y in range(h):
        # 该行三角形半宽（线性收窄到 0）
        t = y / float(h - 1) if h > 1 else 0.0
        hw = half * (1.0 - t)
        row = bytearray()
        for x in range(w):
            cov = 0
            for s in range(sub):
                px = x + (s + 0.5) / sub
                if abs(px - cx) <= hw:
                    cov += 1
            alpha = int(round(255 * cov / sub))
            row += bytes((r0, g0, b0, alpha))
        rows.append(bytes(row))

    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def build_qss():
    """生成当前主题的全局样式表。

    主题包自带 `theme.qss` 时用它，否则退回内置的极简色块模板。
    两者都用 `string.Template` 语法（`$accent` 这样的占位符），
    替换值即当前色板。

    容错：用户模板里若出现 `$` 后跟非法标识符（样式里的字面量 `$`），
    `substitute` 会抛 `ValueError`。这时退回内置模板而不是让界面白屏——
    皮肤写坏不该导致程序打不开。
    """
    pk = get_pack()
    pal = dict(current())
    # 下拉框箭头：三张按当前色板着色的三角 PNG。先塞进色板副本，
    # 这样主题包自带的 theme.qss 里也能用 $arrow / $arrow_hover / $arrow_faint。
    try:
        pal["arrow"] = _arrow_url(pal.get("text_dim", "#b9bfcc"))
        pal["arrow_hover"] = _arrow_url(pal.get("accent", "#3b82f6"))
        pal["arrow_faint"] = _arrow_url(pal.get("text_faint", "#8b93a3"))
    except Exception:
        # 生成箭头图失败不能拖垮整个皮肤
        pal.setdefault("arrow", "")
        pal.setdefault("arrow_hover", "")
        pal.setdefault("arrow_faint", "")
    if pk.qss:
        try:
            return Template(pk.qss).substitute(**pal)
        except (ValueError, KeyError):
            pass
    try:
        return _QSS.substitute(**pal)
    except (ValueError, KeyError):
        return _QSS.safe_substitute(**pal)
