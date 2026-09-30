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
}


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
    """首次运行：把内置的 YuUI 主题写到文档目录。

    返回 (是否新建, 主题目录)。
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
    # 同时放一份 README，告诉用户怎么自制主题
    readme = os.path.join(root, "如何自制主题.txt")
    if not os.path.isfile(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(_THEME_README)
        except OSError:
            pass
    return created, d


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
