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


# ---------------------------------------------------------------------------
# 拟物玻璃贴片（liquid glass 主题专用，其他主题包自动退回极简色块）
# ---------------------------------------------------------------------------
def rgba_str(hex_color, alpha):
    """'#7aa2ff' -> 'rgba(122,162,255,140)'（alpha 取 0-255 整数）。

    Qt 的 QSS 解析器对 rgba() 第 4 个参数 >1 的值会按 /255 归一化，
    所以直接给整数 alpha 即可（不能用 CSS 的小数写法）。
    """
    c = (hex_color or "#7aa2ff").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except (ValueError, IndexError):
        r = g = b = 122
    return "rgba(%d,%d,%d,%d)" % (r, g, b, max(0, min(255, int(alpha))))


def glass_on():
    """当前主题包是否启用拟物玻璃贴片（theme.json 里的 icon_tile_style）。"""
    return current().get("icon_tile_style") == "glass"


def glass_tile(color, radius=10, highlight=None):
    """功能图标贴片的「液态玻璃」样式表。

    手法参考纯 CSS 液态玻璃（gitee greyd097/yzrt「纯CSS液态玻璃」）：
    - 彩色半透明底，沿对角线 45° 渐变（浅→深），模拟玻璃的厚度感
    - 1px 白色高光描边，模拟玻璃边缘的环境光折射
    （Qt QSS 没有 box-shadow/backdrop-filter，这两样就是 Qt 下的等效替代）
    """
    if highlight is None:
        highlight = current().get("glass_highlight", "rgba(255,255,255,105)")
    return (
        "background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
        f" stop:0 {rgba_str(color, 150)}, stop:1 {rgba_str(color, 235)});"
        f" border: 1px solid {highlight};"
        f" border-radius: {radius}px;"
    )


def _parse_rgb(color):
    """解析 `#rgb` / `#rrggbb` / `rgb()` / `rgba()`，返回 (rgb, alpha)。

    解析不了返回 (None, None) —— 用户主题里可能写渐变、关键字色，
    调用方必须能区分"解析失败"和"解析出来是黑色"。
    """
    if not color:
        return None, None
    s = str(color).strip()
    if s.startswith("#"):
        c = s[1:]
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        if len(c) == 6:
            try:
                return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)), None
            except ValueError:
                return None, None
    elif s.lower().startswith("rgba(") or s.lower().startswith("rgb("):
        try:
            inner = s[s.index("(") + 1:s.rindex(")")]
            parts = [p.strip() for p in inner.split(",")]
            alpha = parts[3] if len(parts) > 3 else None
            return tuple(int(float(p)) for p in parts[:3]), alpha
        except (ValueError, IndexError):
            return None, None
    return None, None


def _shade(color, factor):
    """把颜色整体压暗 / 提亮。支持 `#rrggbb` 与 `rgba(r,g,b,a)`。

    解析不了（用户主题里写了渐变、关键字色之类）就原样返回 —— 调用方会
    另想办法保证"按下"仍然看得出变化，不会因此崩掉整份 QSS。
    """
    if not color:
        return color
    rgb, alpha = _parse_rgb(color)
    if rgb is None:
        return color
    out = tuple(max(0, min(255, int(round(v * factor)))) for v in rgb)
    if alpha is None:
        return "#%02x%02x%02x" % out
    return "rgba(%d,%d,%d,%s)" % (out[0], out[1], out[2], alpha)


def _mix(frm, to, t):
    """把 `frm` 朝 `to` 混 `t`（0~1）。解析不了就原样返回 `frm`。"""
    rgb, alpha = _parse_rgb(frm)
    other, _ = _parse_rgb(to)
    if rgb is None or other is None:
        return frm
    out = tuple(int(round(p + (q - p) * t)) for p, q in zip(rgb, other))
    if alpha is None:
        return "#%02x%02x%02x" % out
    return "rgba(%d,%d,%d,%s)" % (out[0], out[1], out[2], alpha)


def _luminance(color):
    """0~1 的感知亮度（Rec.709 系数）；解析不了返回 None。"""
    rgb, _ = _parse_rgb(color)
    if rgb is None:
        return None
    r, g, b = (v / 255.0 for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ring_color(fill, strength=0.72):
    """键盘焦点环色：**跟填充色反着来**，保证任何色板下都看得见。

    为什么不能统一用 `$accent`：主要按钮的常态边框**就是** accent，
    而焦点规则只改边框颜色 → 在它身上 Δ=0（实测），键盘用户在这个按钮上
    完全看不到焦点。所以按填充色的亮度挑方向 —— 暗填充配亮环、亮填充配暗环，
    用户自制主题换个 accent 也自动成立。解析不了返回 None，调用方回退。
    """
    lum = _luminance(fill)
    if lum is None:
        return None
    if lum < 0.62:
        return _mix(fill, "#ffffff", strength)
    return _mix(fill, "#000000", strength)


def _pressed_shade(color, mode):
    """"按住"用的深色档。

    深色主题里 `accent_hover` 是**变亮**的（#3b82f6 → #5b98f8），亮色主题里
    是变暗的。为了在两种模式下"按住"都比 hover 更进一步，这里统一往深处走：
    深色主题压 14%（比 hover 的提亮明显区分开），亮色主题压 22%。
    """
    return _shade(color, 0.86 if mode == "dark" else 0.78)


#: 液态玻璃上的文字往这个深色方向混、混多深。
#: 实测：混 0.94 得到 #09172d 左右，压在深色主题下玻璃的实际呈色（约 #7e99c5，
#: 由 tint #a1c3fb 以 alpha 196 压在侧栏底 #0e0e12 上）上是 4.51:1，
#: 刚好过 WCAG AA 的正文线（4.5:1）。混 0.90 只有 4.27:1 —— 就差这一点，
#: 13px 加粗在浅蓝上斜着看就会糊。
_LIQUID_TEXT_DEEP = "#061020"
_LIQUID_TEXT_T = 0.94


def liquid_palette(palette=None):
    """液态玻璃要用的 5 个色，推导规则与 `build_qss` 完全一致。

    为什么单独开一个函数：`build_qss` 是往**色板副本**里注入这些键的
    （和 accent_pressed / accent_ring 一样，避免污染 current()），所以
    自绘的控件（侧栏指示条、开关轨道）从 `theme.current()` 里取不到它们。
    两处各推一份的话迟早会不一致 —— 界面上就会出现"QSS 画的分段按钮"
    和"自绘的侧栏指示条"不是同一种浅蓝。

    返回 dict: tint / edge / text / top / bottom。
    """
    pal = current() if palette is None else palette
    acc = pal.get("accent", "#3b82f6")
    lift = 0.52 if _current_mode == "dark" else 0.34
    tint = pal.get("liquid_tint") or _mix(acc, "#ffffff", lift)
    return {
        "tint": tint,
        "edge": pal.get("liquid_edge") or _mix(acc, "#ffffff",
                                                min(0.86, lift + 0.26)),
        "text": pal.get("liquid_text") or _mix(acc, _LIQUID_TEXT_DEEP,
                                               _LIQUID_TEXT_T),
        "top": pal.get("liquid_top") or _mix(tint, "#ffffff", 0.36),
        "bottom": pal.get("liquid_bottom") or _mix(tint, "#2a5c8f", 0.24),
    }


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

/* ---------- 标题栏 ----------
   同侧栏：底色由 GlassTitleBar 自绘（背后纹理模糊 + 玻璃面），
   这里留透明，只压一条底边分隔线。 */
#TitleBar {
    background: transparent;
    border-bottom: 1px solid $titlebar_separator;
}
QLabel#AppTitle { font-size: 13px; font-weight: 700; color: $text; background: transparent; }
/* 版本号是一块玻璃贴片（GlassChip 自绘：取景 → 模糊 → 白纱 → 亮边）。
   ⚠️ 这里**不能**再给 background 上色：QSS 没有 backdrop-filter，描一块
   实心/surface_hover 上去就变成"标题栏上贴了张纸"，玻璃感全没了；
   而且背景由 QStyleSheetStyle 在 paintEvent 之前刷，会直接盖住自绘结果。
   只保留透明背景（让 padding 生效）+ 文字样式。 */
QLabel#AppSubtitle {
    color: $text_dim;
    font-size: 11px;
    background: transparent;
    padding: 2px 9px;
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

/* ---------- 侧边栏 ----------
   底色不在这里画：侧栏现在是**毛玻璃**（Window 里的 GlassSidebar 自绘：
   先取背后 BackdropLayer 的纹理做模糊，再叠玻璃面与右缘高光）。
   这里必须让 #Sidebar 透明，否则 QSS 的实心底会把模糊层整个盖住。 */
#Sidebar {
    background: transparent;
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
    /* 1px 透明边框占位（内边距同步减 1px，总尺寸与原 `border: none` 一致）：
       焦点环只需要换边框颜色就能生效，不会挤动布局。见 theme_packs.INTERACTION_QSS */
    border: 1px solid transparent;
    border-radius: 5px;
    color: $text_dim;
    text-align: left;
    padding: 9px 11px;
    font-size: 13px;
    margin: 1px 10px;
}
QPushButton#SidebarButton:hover { background: $surface_hover; color: $text; }
/* 选中态不画底：液态玻璃指示条（GlassPill）压在按钮下面，这里再刷底色
   会把它糊掉。文字要用**深色**（$liquid_text）——指示条已经改成浅蓝玻璃，
   再压白字对比度只剩 1.9:1，基本读不清（深色主题下实测）。 */
QPushButton#SidebarButton:checked {
    background: transparent;
    border: 1px solid transparent;
    color: $liquid_text;
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
/* 字距随字号走（大字收紧、小字放开）：21px 的标题按 0 字距会显得字与字之间
   漏风；11px 的小字反而需要一点正字距才看得清。这是 emil 技能库里
   apple-design 那节「tracking 是尺寸相关的、没有万能值」的落地。 */
QLabel#PageTitle { font-size: 21px; font-weight: 800; color: $text; letter-spacing: -0.4px; }
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
/* 按下态统一由文件末尾的 INTERACTION_QSS 定义，这里**不留**规则。
   历史上一句 `:pressed { background: $accent }` 就写在这儿，和末尾那条
   同优先级、只靠"后写的赢"兜底 —— 重排一次规则顺序就会静默失效
   （按下跟常态一模一样），所以整条删掉。 */
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

/* ---------- 数字框 ----------
   内存优化页的"自定义间隔"用它。极简色块风格里不放那对系统小箭头
   （箭头要靠图片才能正确渲染，见上方 QComboBox::down-arrow 的注释），
   直接收起按钮、当成一个窄输入框用；旁边本来就有 6 个挡位按钮。 */
QSpinBox {
    background: $input_bg;
    border: 1px solid $border_strong;
    border-radius: 5px;
    padding: 7px 10px;
    color: $text;
    selection-background-color: $accent;
    selection-color: $accent_text;
}
QSpinBox:focus { border-color: $accent; }
QSpinBox:disabled { color: $text_faint; border-color: $border; }
QSpinBox::up-button, QSpinBox::down-button {
    width: 0px;
    border: none;
    background: transparent;
}

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
    /* 同侧栏项：透明边框占位 + 内边距减 1px，给焦点环留位置而不改尺寸 */
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 5px 13px;
    color: $text_dim;
}
QPushButton#SegmentButton:hover { color: $text; background: $surface_hover; }
/* 选中段**自己不画底**：那块浅蓝液态玻璃由 ui/widgets.SegmentSlider 自绘，
   并且会在切换时用弹簧从旧挡位滑到新挡位（qss 的 :checked 是瞬时换色，
   既不能插值也做不出"滑块"）。这里只留文字色与字重，边框保持透明 ——
   否则按钮的方角边框会压在玻璃片的圆角上，四个角露出直角。
   （浅色主题里这块玻璃的对比度由 SegmentSlider.TINT_ALPHA 控制。） */
QPushButton#SegmentButton:checked {
    background: transparent;
    color: $liquid_text;
    border: 1px solid transparent;
    font-weight: 700;
}

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
QLabel#DialogTitle { color: $text; font-size: 17px; font-weight: 800; background: transparent; letter-spacing: -0.3px; }
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
    # 按下 / 键盘焦点态与 sky glass 模板**共用同一份文本**：
    # 避免"内置那份补了、主题包那份忘了"的漏改（历史上侧栏选中态、
    # 版本号贴片都是这么栽的）。见 theme_packs.INTERACTION_QSS。
    + packs_mod.INTERACTION_QSS
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

    # "按住"色：色板里显式写了就用用户的，没写就按当前模式推导。
    # 放在这里而不是写死进四套内置色板，是为了**用户自制主题也能自动拿到**
    # 一档合理的按下色 —— 否则他们只改 accent 时，按下色会继承默认主题的蓝，
    # 和他们的主色对不上（色板缺键会继承 DEFAULT_*，见 theme_packs.load_pack）。
    mode = _current_mode
    if not pal.get("accent_pressed"):
        pal["accent_pressed"] = _pressed_shade(pal.get("accent", "#3b82f6"), mode)
    if not pal.get("red_pressed"):
        pal["red_pressed"] = _pressed_shade(pal.get("red", "#e5484d"), mode)
    # 焦点环色：主要按钮的常态边框就是 accent，只用 accent 当环 = 看不见。
    # 这里推导一个"跟填充反着来"的环色，用户自制主题也自动成立。
    accent_ring = _ring_color(pal.get("accent", "#3b82f6"))
    pal["accent_ring"] = accent_ring or pal.get("accent", "#3b82f6")

    # ---- 液态玻璃色组（选中态） ----
    # 用户的要求：选中效果"不要实心蓝，要浅蓝的液态玻璃"。所以底色从 accent
    # 往白里提；**文字必须反过来往深里走** —— 浅蓝底（#a1c3fb 左右）上压白字
    # 对比度只剩 1.9:1，基本读不清，而深藏蓝压上去有 4.5:1（AA 线）。
    # 浅色主题混得少一些：底色本来就是浅灰，玻璃太淡会跟背景糊在一起。
    _acc = pal.get("accent", "#3b82f6")
    _lift = 0.52 if mode == "dark" else 0.34
    if not pal.get("liquid_tint"):
        pal["liquid_tint"] = _mix(_acc, "#ffffff", _lift)
    if not pal.get("liquid_edge"):
        pal["liquid_edge"] = _mix(_acc, "#ffffff", min(0.86, _lift + 0.26))
    if not pal.get("liquid_text"):
        pal["liquid_text"] = _mix(_acc, _LIQUID_TEXT_DEEP, _LIQUID_TEXT_T)
    # 玻璃的竖向渐变两端：上端更亮（迎光），下端带一点深（厚度）
    if not pal.get("liquid_top"):
        pal["liquid_top"] = _mix(pal["liquid_tint"], "#ffffff", 0.36)
    if not pal.get("liquid_bottom"):
        pal["liquid_bottom"] = _mix(pal["liquid_tint"], "#2a5c8f", 0.24)

    if pk.qss:
        try:
            return Template(pk.qss).substitute(**pal)
        except (ValueError, KeyError):
            pass
    try:
        return _QSS.substitute(**pal)
    except (ValueError, KeyError):
        return _QSS.safe_substitute(**pal)
