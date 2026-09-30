"""软件卸载核心逻辑（参考 Geek Uninstaller 的实现思路）。

分六个阶段，与 Geek 一致：
  1. 枚举    —— 读三处 Uninstall 注册表 + UWP 应用包，构建程序清单
  2. 卸载    —— 执行 UninstallString / QuietUninstallString，等待进程树结束
  3. 残留扫描 —— 以 InstallLocation / DisplayIcon 为线索反查文件系统，
                 并在 AppData / ProgramData 下按厂商名、产品名搜索
  4. 注册表扫描 —— 在五个根节点下按名称匹配待清理的键
  5. 生成清单 —— 按安全等级分类，交给用户勾选确认
  6. 执行清理 —— 删除文件和注册表键，被占用的用 MoveFileEx 标记重启后删除

设计原则与 cleaner.py 保持一致：
  * 扫描全只读，清理必须经用户二次确认；
  * 删除走白名单校验（`_is_safe_to_delete`），拒绝越界路径；
  * 需要管理员权限的操作走 `Yuhub.exe --uninstall-elevated` 提权子进程。
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import winreg
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_CONSOLE = 0x00000010

# 注册表里的「卸载信息」节点。
# 四个都要读，缺一不可（跨机踩过：只读前三个会漏掉 32 位用户级安装）：
#   * HKLM 64 位  —— 常规的机器级安装
#   * HKLM 32 位  —— WOW6432Node 下的机器级 32 位安装
#   * HKCU 64 位  —— 用户级安装（Chrome 系、VS Code 这类"只装给自己"的）
#   * HKCU 32 位  —— **最容易漏的一个**：64 位系统上以普通权限安装的
#                    32 位程序会写到这里，跳过后用户在列表里根本看不到它
UNINSTALL_KEYS = [
    ("HKLM", winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKLM32", winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU", winreg.HKEY_CURRENT_USER,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ("HKCU32", winreg.HKEY_CURRENT_USER,
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
]

# 残留扫描时，注册表要搜的根节点
REG_SCAN_ROOTS = [
    ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE"),
    ("HKLM32", winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"),
    ("HKCU", winreg.HKEY_CURRENT_USER, r"SOFTWARE"),
    ("HKCR", winreg.HKEY_CLASSES_ROOT, r""),
]

# 文件系统残留的高概率位置（按 用户名 / ProgramData 展开）
_APPDATA_TEMPLATES = [
    r"%APPDATA%",              # Roaming
    r"%LOCALAPPDATA%",         # Local
    r"%LOCALAPPDATA%\Low",     # LocalLow（严格说应是 %USERPROFILE%\AppData\LocalLow）
    r"%PROGRAMDATA%",
    r"%USERPROFILE%\AppData\LocalLow",
]

# 通用词：这些词太常见，不能拿来当搜索关键词，否则会误伤一堆无关目录。
# 教训：曾经把 "Launcher" 当关键词，结果搜 Epic 时把 WPFLauncher、
# nrc_launcher、delta_force_launcher、MCLauncher 全匹配进来了。
_GENERIC_WORDS = {
    # 路径与系统
    "microsoft", "windows", "win", "common", "files", "file", "program",
    "programs", "software", "app", "apps", "application", "applications",
    "update", "updates", "updater", "setup", "install", "installer",
    "uninstall", "uninstaller", "bin", "system", "system32", "syswow64",
    "driver", "drivers", "service", "services", "data", "cache", "caches",
    "config", "configs", "settings", "setting", "user", "users", "local",
    "roaming", "locallow", "temp", "tmp", "log", "logs", "backup",
    # 公司/法律
    "the", "and", "for", "with", "inc", "ltd", "llc", "corp", "co",
    "corporation", "company", "limited", "gmbh", "technologies",
    "technology", "team", "studio", "studios", "labs", "lab", "net",
    "com", "org", "www", "group", "holding", "international",
    # 技术/版本
    "exe", "dll", "x64", "x86", "win32", "win64", "bit", "version",
    "release", "beta", "alpha", "stable", "nightly", "build",
    # 通用产品词（重灾区，宁可少匹配也不能误伤）
    "client", "server", "tool", "tools", "utility", "utilities",
    "core", "runtime", "video", "audio", "media", "player", "library",
    "framework", "sdk", "desktop", "mobile", "web", "cloud", "game",
    "games", "gaming", "edition", "free", "pro", "plus", "premium",
    "ultimate", "standard", "suite", "express", "enterprise", "home",
    "office", "personal", "business", "launcher", "launch", "loader",
    "manager", "helper", "agent", "service", "center", "centre",
    "hub", "panel", "console", "shell", "main", "support", "interface",
    "device", "devices", "network", "security", "guard", "shield",
    "boost", "speed", "fast", "quick", "smart", "easy", "simple",
    "advanced", "professional", "master", "expert", "power", "super",
    "ultra", "max", "mini", "light", "lite", "full", "online", "offline",
    "digital", "global", "world", "space", "zone", "area", "point",
    "link", "sync", "share", "store", "shop", "market", "gate",
}


# 明确不能删的目录名（在 AppData/ProgramData 里也不许碰）
_PROTECTED_DIR_NAMES = {
    "microsoft", "windows", "windowsapps", "packages", "temp", "tmp",
    "packages", "crashdumps", "d3dscache", "nvidia", "amd", "intel",
    "google", "mozilla", "packages", "connecteddevicesplatform",
}


# ---------------------------------------------------------------------------
# 数据中心
# ---------------------------------------------------------------------------
@dataclass
class AppInfo:
    """一个已安装程序。"""

    key: str = ""                 # 唯一标识：hive + 子键名
    name: str = ""                # DisplayName
    version: str = ""
    publisher: str = ""
    install_location: str = ""
    display_icon: str = ""
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    install_date: str = ""        # 原始 yyyymmdd
    estimated_kb: int = 0
    hive: str = ""
    subkey: str = ""
    hive_handle: int = 0          # winreg hive 常量
    kind: str = "win32"           # win32 / msi / uwp
    product_code: str = ""        # MSI 的 GUID 或 UWP 的 PackageFullName
    system_component: bool = False
    no_remove: bool = False
    is_update: bool = False       # 属于某个父程序的更新包
    parent_key: str = ""

    # ---- 展示辅助 ----
    @property
    def size_text(self):
        if self.estimated_kb <= 0:
            return "--"
        return human_bytes(self.estimated_kb * 1024)

    @property
    def date_text(self):
        d = (self.install_date or "").strip()
        if len(d) == 8 and d.isdigit():
            return f"{d[:4]}-{d[4:6]}-{d[6:]}"
        return d or "--"

    @property
    def removable(self):
        """是否具备卸载条件（有卸载命令，且未标记为不可移除）。"""
        if self.no_remove:
            return False
        if self.kind == "uwp":
            return True
        return bool(self.uninstall_string or self.quiet_uninstall_string)


@dataclass
class Leftover:
    """一条残留项（文件或注册表）。"""

    kind: str = "file"            # file / dir / reg
    path: str = ""
    size: int = 0
    hive: str = ""
    subkey: str = ""
    hive_handle: int = 0
    safe: bool = True             # False = 需谨慎（可能被其他程序共享）
    note: str = ""
    checked: bool = True

    @property
    def size_text(self):
        if self.kind == "reg":
            return "注册表项"
        if self.size <= 0:
            return "--"
        return human_bytes(self.size)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def human_bytes(n):
    """字节数转人类可读（与 cleaner.py 保持一致的量纲）。"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024


def is_admin():
    """当前进程是否有管理员权限。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _expand(path):
    """展开环境变量与 ~。

    要处理几个跨机的边界情况：
      * `%ProgramFiles(x86)%` / `%ProgramW6432%` 在 **32 位 Windows** 上
        **不存在**，注册表里却常这么写。缺失时 `expandvars` 会原样留下
        `%VAR%` 字符串，导致后面 os.path.isfile 必然失败。
        这里对这几个已知变量做手动兜底，映射到合理的等价目录。
      * 引号包裹（`"C:\\Program Files\\A"`）也顺手剥掉。
    """
    s = (path or "").strip().strip('"')
    if not s:
        return ""
    # 缺失变量的手动兜底（不依赖系统是否定义）
    if "%ProgramFiles(x86)%" in s and not os.environ.get("ProgramFiles(x86)"):
        pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
        s = s.replace("%ProgramFiles(x86)%", pf)
    if "%ProgramW6432%" in s and not os.environ.get("ProgramW6432"):
        pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
        s = s.replace("%ProgramW6432%", pf)
    if "%ProgramFiles%" in s and not os.environ.get("ProgramFiles"):
        s = s.replace("%ProgramFiles%", r"C:\Program Files")
    try:
        s = os.path.expandvars(s)
    except Exception:
        pass
    try:
        s = os.path.expanduser(s)
    except Exception:
        pass
    # 展开后若仍残留未识别的 %XXX%，说明拿不到，返回空让调用方走别的线索
    if re.search(r"%[A-Za-z_][^%]*%", s):
        return ""
    return s.strip()


def _norm(path):
    """归一化路径用于比较：统一分隔符 + 小写 + 去掉末尾分隔符。

    必须把正斜杠转成反斜杠后再比较——注册表里的 InstallLocation
    经常写成 `D:/Apps/AppName/` 这种形式（尤其 InstallShield / Inno 装出来的），
    不统一分隔符会让后面的"路径深度"校验数不到层级，把合法路径误判成危险路径。
    """
    p = _expand(path).replace("/", "\\")
    return p.rstrip("\\").lower()


def _registry_value(hive, subkey, name, default=None):
    """读一个注册表值，任何异常都返回 default。"""
    try:
        with winreg.OpenKey(hive, subkey) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return v
    except OSError:
        return default


def _enum_values(hive, subkey):
    """枚举一个键的全部值 → dict。"""
    out = {}
    try:
        with winreg.OpenKey(hive, subkey) as k:
            i = 0
            while True:
                try:
                    n, v, _ = winreg.EnumValue(k, i)
                    i += 1
                    out[n] = v
                except OSError:
                    break
    except OSError:
        pass
    return out


def _dir_size(path, budget=4.0):
    """递归统计目录体积（带时间预算，超时返回已统计值）。

    用 os.scandir 而不是 os.walk：前者不预建整个列表，对十万级文件
    的目录内存占用小得多，也不会因为权限异常中断整个遍历。
    """
    total = 0
    deadline = time.time() + budget
    stack = [path]
    while stack:
        if time.time() > deadline:
            break
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if time.time() > deadline:
                        break
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except (OSError, ValueError):
                        continue
        except (OSError, PermissionError):
            continue
    return total


def _parse_exe_from_icon(display_icon):
    """从 DisplayIcon 里抠出 exe 路径（可能是 "path,index" 形态）。"""
    s = (display_icon or "").strip().strip('"')
    if not s:
        return ""
    # 形如 C:\App\app.exe,0
    m = re.match(r'^(.*?\.exe)\s*,\s*-?\d+\s*$', s, re.IGNORECASE)
    if m:
        return m.group(1).strip('"')
    # 形如 C:\App\app.exe
    if s.lower().endswith(".exe"):
        return s
    # 可能带引号包裹整体
    if '"' in s:
        parts = s.split('"')
        for p in parts:
            if p.lower().endswith(".exe"):
                return p
    return ""


def _icon_source(app):
    """给一个程序找出"能从哪儿取出图标"的文件路径 + 索引。

    返回 (path, index)，取不到返回 ("", 0)。

    优先级（越靠前越可信）：
      1. DisplayIcon 指向的 **.ico / .exe / .dll** —— 直接就是图标资源
      2. InstallLocation 下**按程序名**找到的主 exe —— 很多软件不写
         DisplayIcon（尤其免安装/绿色版），但安装目录里那个同名 exe 的
         图标就是用户认识的那个
      3. 卸载程序自己的图标（UninstallString 指向的 exe）

    Windows 注册表里的 DisplayIcon 有几种形态，必须都认：
      `C:\\App\\a.exe,0` / `"C:\\Program Files\\A\\a.exe",-1` / `C:\\App\\a.ico`
    索引可以是负数（表示资源 ID 取反），此时按 0 处理。
    """
    raw = (app.display_icon or "").strip()
    idx = 0

    if raw:
        s = raw.strip()
        # 拆出 ",索引" 尾巴（注意：路径本身可能含逗号，所以从**末尾**匹配）
        m = re.match(r'^(.*?)\s*,\s*(-?\d+)\s*$', s)
        if m:
            s = m.group(1).strip().strip('"')
            try:
                idx = int(m.group(2))
            except ValueError:
                idx = 0
        s = _expand(s.strip().strip('"'))
        low = s.lower()
        # .dll 也有图标资源（不少驱动/运行库这么写），一并支持
        if low.endswith((".ico", ".exe", ".dll")) and os.path.isfile(s):
            return s, (idx if idx >= 0 else 0)

    # ---- UWP：从 AppxManifest.xml 里读 Logo ----
    # 应用商店应用的 InstallLocation 指向 WindowsApps（权限锁死），
    # 而且注册表里根本没有 DisplayIcon。正规做法是读清单里的 <Logo> 字段，
    # 它在 InstallLocation 下有对应资源文件（形如 Assets\StoreLogo.png）。
    if app.kind == "uwp":
        src = _uwp_logo(app)
        if src:
            return src, 0
        return "", 0

    # ---- 退回安装目录里找 ----
    loc = _expand((app.install_location or "").strip().strip('"'))
    if loc and os.path.isdir(loc):
        main = _find_main_exe(loc, app.name)
        if main:
            return main, 0
        # InstallLocation 常常只是个"容器目录"，真正的主程序埋在下面两三层。
        # 典型：Epic —— `C:\Program Files\Epic Games\` 下是
        # `Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe`，
        # 而同级还有 `DirectXRedist\DXSETUP.exe` 这种分发包来捣乱。
        #
        # 所以做一次**有界递归**，但规则收紧：
        #   * 深度最多 3 层，目录数最多 ~12/层（别把整个盘扫了）；
        #   * 只接受**名字匹配**的 exe（strict=True），不做"体积最大"兜底
        #     —— 递归时随便挑一个必然选中分发包里的工具；
        #   * 明显是分发包 / 文档 / 驱动 的目录整个跳过。
        hit = _search_main_exe_deep(loc, app.name, depth=5)
        if hit:
            return hit, 0

    # ---- 再退回卸载程序自己的图标 ----
    exe = _parse_exe_from_icon(app.uninstall_string)
    if exe:
        exe = _expand(exe)
        if os.path.isfile(exe):
            return exe, 0
    return "", 0


def _uwp_logo(app):
    """从 UWP 包的 AppxManifest.xml 里取 Logo 资源的磁盘路径。

    清单里 `<Logo>Assets\\StoreLogo.png</Logo>` 是相对 InstallLocation 的。
    优先挑方形的 `Square44x44Logo` / `StoreLogo`（列表里正好用方形），
    普通 `Logo` 往往是宽幅的、缩到 34px 会糊。
    """
    loc = app.install_location or ""
    if not loc or not os.path.isdir(loc):
        return ""
    manifest = os.path.join(loc, "AppxManifest.xml")
    if not os.path.isfile(manifest):
        return ""
    try:
        with open(manifest, "r", encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except OSError:
        return ""

    def pick(tag):
        # 取该标签下第一条相对路径
        for m in re.finditer(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), xml,
                             re.IGNORECASE | re.DOTALL):
            v = (m.group(1) or "").strip()
            if v and not v.lower().startswith(("http:", "ms-resource:")):
                return v
        return ""

    cands = [pick("Square44x44Logo"), pick("Square150x150Logo"),
             pick("StoreLogo"), pick("Logo")]
    for rel in cands:
        if not rel:
            continue
        p = os.path.normpath(os.path.join(loc, rel.replace("/", "\\")))
        if os.path.isfile(p):
            return p
        # 资源实际可能是带缩放的变体：BaseName.scale-100.png / BaseName.targetsize-44.png
        d, base = os.path.dirname(p), os.path.basename(p)
        stem, ext = os.path.splitext(base)
        if os.path.isdir(d):
            try:
                for nm in os.listdir(d):
                    if nm.lower().startswith(stem.lower()) and \
                            nm.lower().endswith(ext.lower()):
                        return os.path.join(d, nm)
            except OSError:
                pass
    return ""


def _find_main_exe(folder, app_name, max_scan=400, strict=False):
    """在安装目录里猜"主程序"exe。

    策略（按可信度递减）：
      1. 文件名（去掉非字母数字后）与程序名**互相包含** —— 最准。
      2. 程序名里的**长词**（>=4 字符）出现在文件名里。
      3. 体积最大的 exe（主程序通常带全部代码，卸载器/更新器都很小）。

    只看目录第一层，不递归 —— 递归进子目录容易撞上 Runtime\\ 里的一堆
    无关 exe，这个交给 `_search_main_exe_deep` 用更严的规则去做。

    `strict=True` 时**跳过第 3 条兜底**：只认"名字对得上"的 exe。
    递归搜索必须用 strict，否则在分发包目录（DirectXRedist 之类）里
    挑"体积最大"必然选到 DXSETUP.exe，给用户显示一个 DirectX 图标。
    """
    try:
        exes = []
        with os.scandir(folder) as it:
            for e in it:
                if len(exes) >= max_scan:
                    break
                try:
                    if e.is_file() and e.name.lower().endswith(".exe"):
                        exes.append((e.name, e.path,
                                     e.stat(follow_symlinks=False).st_size))
                except OSError:
                    continue
    except OSError:
        return ""
    if not exes:
        return ""

    def flat(s):
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (s or "").lower())

    stem = flat(app_name)
    # 排除明显的非主程序名（卸载器 / 安装器 / 运行时分发 / 崩溃上报 / 加速器工具 等）
    noise = ("unins", "uninstall", "setup", "install", "update", "updater",
             "helper", "crashpad", "crashreporter", "vcredist", "dxsetup",
             "dxwebsetup", "directx", "redist", "dotnetfx", "oalinst",
             "repair", "extract", "7zsd", "aria2", "aria2c")

    def is_noise(nm):
        n = nm.lower()
        return any(k in n for k in noise)

    # 1) 程序名与文件名互相包含
    if stem:
        for nm, pth, sz in exes:
            if is_noise(nm):
                continue
            f = flat(nm[:-4])
            if f and (stem in f or f in stem) and len(f) >= 3:
                return pth

    # 2) 程序名里的长词
    words = [w for w in re.split(r"[\s\-_/\\,;:()\[\]{}|+.]+", (app_name or "").lower())
             if len(w) >= 4]
    for w in words:
        for nm, pth, sz in exes:
            if is_noise(nm):
                continue
            if w in nm.lower():
                return pth

    # 3) 体积最大（递归场景下禁用，避免选中分发包里的工具）
    if strict:
        return ""
    cand = [t for t in exes if not is_noise(t[0])] or exes
    cand.sort(key=lambda t: t[2], reverse=True)
    return cand[0][1]


# 递归搜索时要整个跳过的目录名（分发包 / 文档 / 驱动 / 运行库）
_SKIP_DIR_WORDS = (
    "redist", "directx", "dxredist", "vcredist", "dotnet", "directxredist",
    "runtime", "driver", "drivers", "docs", "documentation", "support",
    "locale", "locales", "lang", "languages", "fonts", "samples",
    "templates", "licenses", "readme", "help", "redistributables",
)


def _search_main_exe_deep(root, app_name, depth=5, per_level=12):
    """在 root 下**有界递归**找与程序名匹配的主程序 exe。

    存在的理由：不少软件的 InstallLocation 只是个"容器目录"，
    真正的主程序埋在下面好几层。最典型的是 Epic：
        C:\\Program Files\\Epic Games\\Launcher\\Portal\\Binaries\\Win64\\EpicGamesLauncher.exe
    而它的**同级**还蹲着 `DirectXRedist\\DXSETUP.exe`。

    所以规则收紧（这是与 `_find_main_exe` 的关键差别）：
      * 深度 <= depth、每层最多展开 per_level 个目录（别把盘扫穿）；
      * 只接受名字匹配的 exe（strict=True），不做体积兜底；
      * `_SKIP_DIR_WORDS` 里的目录整棵跳过；
      * 优先展开名字"像主程序目录"的（bin / win64 / x64 / launcher ...），
        让正确结果先被命中，也能早点结束搜索。

    实测代价：Epic 这种 5 层结构在 2 ms 内命中（跳过表把无关目录都剪掉了），
    所以递归本身不构成性能问题。

    返回 exe 路径，找不到返回 ""。
    """
    if depth <= 0 or not root or not os.path.isdir(root):
        return ""
    # 本层先看有没有名字直接对得上的
    hit = _find_main_exe(root, app_name, strict=True)
    if hit:
        return hit
    if depth == 1:
        return ""

    try:
        dirs = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return ""

    def keep(d):
        n = d.name.lower()
        if n.startswith("."):
            return False
        return not any(k in n for k in _SKIP_DIR_WORDS)

    dirs = [d for d in dirs if keep(d)][:per_level]

    def rank(d):
        n = d.name.lower()
        # 越靠前越可能是主程序所在
        if n in ("win64", "x64", "bin", "binaries"):
            return 0
        if any(k in n for k in ("launcher", "client", "app", "program", "core")):
            return 1
        if n in ("win32", "x86"):
            return 2
        return 3

    dirs.sort(key=rank)
    for d in dirs:
        hit = _search_main_exe_deep(d.path, app_name, depth=depth - 1,
                                    per_level=per_level)
        if hit:
            return hit
    return ""


def _keywords(app):
    """从程序信息里提取可用于文件系统/注册表搜索的关键词。

    只取「足够独特」的词：长度 >= 3、不在通用词表里、不含路径分隔符。
    优先级从高到低：安装目录名 / 图标路径目录名（最准）> 产品名 > 厂商名。

    厂商名里常混着人名（"Igor Pavlov"）、地名（"Shenzhen"）、公司后缀
    （"Inc."），拿它去搜目录会误伤一片——所以厂商词只在"整体作为厂商名"
    时用于注册表匹配，不进文件系统搜索词。
    """
    product_words = []

    # ---- 产品名 ----
    # 去掉版本号尾巴（"7-Zip 26.01 (x64)" -> "7-Zip"），否则关键词会变成数字
    name = app.name or ""
    name = re.sub(r"\s*\(?(x64|x86|64-bit|32-bit)\)?\s*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+v?\d+(\.\d+)*\s*$", "", name, flags=re.IGNORECASE)
    for chunk in re.split(r"[\s\-_/\\,;:()\[\]{}|+]+", name):
        c = chunk.strip().strip(".").strip()
        if _is_good_keyword(c):
            if c.lower() not in [w.lower() for w in product_words]:
                product_words.append(c)

    # ---- 从路径补词（最可靠，放最前）----
    path_words = []
    loc = _expand(app.install_location)
    if loc:
        for part in reversed(loc.rstrip("\\/").split("\\")):
            if _is_good_keyword(part):
                path_words.append(part)
                break
    exe = _parse_exe_from_icon(app.display_icon)
    if exe:
        base = os.path.basename(os.path.dirname(exe))
        if _is_good_keyword(base) and base.lower() not in [w.lower() for w in path_words]:
            path_words.append(base)

    # 路径词优先；产品词补齐；最多 4 个
    out = []
    for w in path_words + product_words:
        if w.lower() not in [x.lower() for x in out]:
            out.append(w)
    return out[:4]


def _is_good_keyword(w):
    """判断一个词是否够独特、适合做搜索关键词。"""
    if not w:
        return False
    c = w.strip().strip(".-_")
    if len(c) < 3:
        return False
    low = c.lower()
    if low in _GENERIC_WORDS:
        return False
    # 纯数字 / 版本号
    if re.match(r"^[\d.]+$", low):
        return False
    # 纯符号
    if re.match(r"^[\W_]+$", low):
        return False
    # 含盘符或路径残片
    if ":" in c or "\\" in c or "/" in c:
        return False
    return True


def _app_keys(app):
    """生成用于**目录名匹配**的指纹集合。

    每个指纹都要足够独特，宁可漏一点也不能误伤。三类指纹：
      * fused —— 产品名拼接（"Epic Games" -> "epicgames"）；
                 目录名里含这个串基本就是这个软件的
      * exact —— 单个词本身，要求长度够（精确相等匹配用）

    长度门槛 4：能覆盖 "7zip"、"steam"、"epic" 这类短名，
    挡住 "app"、"web"、"hub"、"pro" 这类 3 字母通用词。
    """
    keys = {"fused": set(), "exact": set()}

    # 产品名：先去版本号尾巴
    name = app.name or ""
    name = re.sub(r"\s*\(?(x64|x86|64-bit|32-bit)\)?\s*$", "", name,
                  flags=re.IGNORECASE)
    name = re.sub(r"\s+v?\d+(\.\d+)*\s*$", "", name, flags=re.IGNORECASE)

    # 切词：中文按原样保留（中文名本身就独特），西文按空格切
    def _good(w):
        w = _clean_word(w)
        if not w or len(w) < 4:
            return False
        # 纯数字/版本号不要
        if re.match(r"^[\d.]+$", w):
            return False
        if w.lower() in _GENERIC_WORDS:
            return False
        return True

    parts = [p for p in (_clean_word(x) for x in re.split(r"\s+", name.strip())) if p]
    good_parts = [p for p in parts if _good(p)]

    # 拼接指纹：优先用"去通用词后的词"拼接，避免 "Epic Games Launcher"
    # 拼成 "epicgameslauncher"（太长）而漏匹配 "EpicGamesLauncher" 的实际写法
    if len(good_parts) >= 2:
        fused = "".join(good_parts).lower()
        if len(fused) >= 4:
            keys["fused"].add(fused)
    # 无通用词可去时，用全名拼接兜底
    if len(parts) >= 2 and not keys["fused"]:
        fused = "".join(parts).lower()
        if len(fused) >= 4:
            keys["fused"].add(fused)

    for p in good_parts:
        low = p.lower()
        keys["fused"].add(low)
        if len(low) >= 5:
            keys["exact"].add(low)

    # 路径末级目录名（最准，直接采用）
    loc = _expand(app.install_location)
    if loc:
        for part in reversed(loc.rstrip("\\/").split("\\")):
            c = _clean_word(part)
            if c and len(c) >= 4 and c.lower() not in _GENERIC_WORDS:
                keys["fused"].add(c.lower())
                keys["exact"].add(c.lower())
                break
    exe = _parse_exe_from_icon(app.display_icon)
    if exe:
        base = _clean_word(os.path.basename(os.path.dirname(exe)))
        if base and len(base) >= 4 and base.lower() not in _GENERIC_WORDS:
            keys["fused"].add(base.lower())
    return keys


def _clean_word(w):
    return (w or "").strip().strip(".-_+").strip()


def _appdir_matches(dir_name_lower, keys):
    """判断一个目录名是否属于目标程序。

    规则（从严）：
      1. 目录名去符号后 == 某个 exact 指纹
      2. 目录名去符号后 **包含** 某个 fused 指纹
      3. 目录名去符号后 == 某个 fused 指纹（对 "7-Zip" 这种短名必须支持）

    长度门槛：fused 词只有 >= 4 才会进匹配池（在 _app_keys 里已经卡过）。
    4 是个平衡点——"epic"(4)、"steam"(5)、"7zip"(4) 都能过，
    而 "app"、"web"、"hub" 这类 3 字母词进不来。
    """
    compact = re.sub(r"[\s\-_.]+", "", dir_name_lower)
    if not compact:
        return False
    if dir_name_lower in keys["exact"] or compact in keys["exact"]:
        return True
    for k in keys["fused"]:
        kc = re.sub(r"[\s\-_.]+", "", k)
        if not kc or len(kc) < 4:
            continue
        if kc == compact:
            return True
        # 子串匹配要求指纹 >= 6，避免 "epic" 命中 "epicurious" 之类的巧合
        if len(kc) >= 6 and kc in compact:
            return True
    return False


# ---------------------------------------------------------------------------
# 阶段 1：枚举
# ---------------------------------------------------------------------------
def _parse_msi_product_code(uninstall_string):
    """从 msiexec 命令行里抠出 {GUID}。"""
    m = re.search(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
                  r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}", uninstall_string or "")
    return m.group(0) if m else ""


def _enum_registry_apps(on_progress=None):
    """枚举四处注册表 Uninstall 节点下的程序。

    任一节点不存在（比如干净系统上从没有过用户级 32 位安装）直接跳过，
    不影响其余节点——这是 `try/except OSError: continue` 的作用。
    """
    apps = []
    for hive_tag, hive, path in UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
                i += 1
            except OSError:
                break
            try:
                sk = winreg.OpenKey(root, sub)
            except OSError:
                continue
            vals = {}
            j = 0
            while True:
                try:
                    vn, vd, _ = winreg.EnumValue(sk, j)
                    j += 1
                    vals[vn] = vd
                except OSError:
                    break

            name = str(vals.get("DisplayName") or "").strip()
            uninst = str(vals.get("UninstallString") or "").strip()
            quiet = str(vals.get("QuietUninstallString") or "").strip()

            # 无显示名 / 无卸载命令的项：Windows 留下了大量这种空壳
            # （AddressBook、IE40 之类），列出来只会干扰用户。
            if not name:
                continue
            if not uninst and not quiet:
                continue

            app = AppInfo(
                key=f"{hive_tag}\\{sub}",
                name=name,
                version=str(vals.get("DisplayVersion") or "").strip(),
                publisher=str(vals.get("Publisher") or "").strip(),
                install_location=str(vals.get("InstallLocation") or "").strip(),
                display_icon=str(vals.get("DisplayIcon") or "").strip(),
                uninstall_string=uninst,
                quiet_uninstall_string=quiet,
                install_date=str(vals.get("InstallDate") or "").strip(),
                estimated_kb=int(vals.get("EstimatedSize") or 0),
                hive=hive_tag,
                subkey=sub,
                hive_handle=hive,
                system_component=(vals.get("SystemComponent") == 1),
                no_remove=(vals.get("NoRemove") == 1),
                parent_key=str(vals.get("ParentKeyName") or "").strip(),
            )
            if app.parent_key:
                app.is_update = True

            # MSI 形态识别
            low = (uninst or quiet).lower()
            if "msiexec" in low:
                app.kind = "msi"
                app.product_code = _parse_msi_product_code(uninst or quiet)

            apps.append(app)
            if on_progress:
                on_progress(app)
    return apps


def _ps_exe():
    """定位 powershell.exe 的绝对路径。

    为什么不直接写 "powershell"（踩过的跨机坑）：
      1. 它依赖 PATH。受组策略限制或被精简过的机器上 PATH 可能不含
         System32\\WindowsPowerShell\\v1.0，直接调用会 FileNotFoundError。
      2. **WOW64 重定向**：32 位进程访问 `System32` 会被静默重定向到
         `SysWOW64`。虽然两个目录下都有 powershell.exe，但显式取绝对路径
         能避免"看到的是 A、跑起来的是 B"这类诡异问题。
    所以按优先级找：PATH → System32 → Sysnative（32 位进程专用的
    "真实 System32" 别名）。
    """
    cand = shutil.which("powershell.exe") or shutil.which("powershell")
    if cand:
        return cand
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    for sub in ("System32", "Sysnative"):
        p = os.path.join(root, sub, "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(p):
            return p
    return "powershell.exe"        # 最后的兜底：交给系统去 PATH 里找


def _run_ps(script, timeout=25):
    """跑一段 PowerShell，返回 (stdout, stderr)。

    不建窗口、不弹控制台；用 -NoProfile 避免加载用户配置拖慢启动。

    ## 编码（跨机最容易翻车的地方）

    绝对不能让 subprocess 用 `text=True` 去猜编码：它按 **locale 首选编码**
    解码，而 PowerShell 实际输出的是**控制台 OEM 代码页**（中文 Windows 是
    CP936 / GBK，英文是 CP437）。两者在非 ASCII 字符上必然对不上——
    实测过：解析到某个含中文的包名时，reader 线程直接抛
    `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd5`，
    结果 `stdout` 变成空串，UI 上表现为"应用商店应用全部消失"。

    所以这里做三件事：
      1. 脚本前**强制把 PS 的输出编码设为 UTF-8**（`[Console]::OutputEncoding`），
         从源头保证字节是 UTF-8；
      2. Python 侧**按 UTF-8 解码 + errors="replace"**，即使有意外字节也
         只丢个别字符，不会整个调用炸掉；
      3. **兜底再试一次 GBK**：万一某些被裁剪过的系统忽略 OutputEncoding
         设置（实测极少但存在），输出的还是 OEM 代码页字节，
         此时 UTF-8 解出来是一片 `\\ufffd`。检测到这种情况就用 GBK 重解，
         能救回中文名字而不是显示一串乱码问号。
    """
    prefix = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
              "$OutputEncoding=[System.Text.Encoding]::UTF8; ")
    try:
        p = subprocess.run(
            [_ps_exe(), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", prefix + script],
            capture_output=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        raw = p.stdout or b""
        out = raw.decode("utf-8", "replace")
        # 兜底：UTF-8 解出大量替换符说明对方根本不是 UTF-8 输出，
        # 试一下 GBK（中文 Windows 的 OEM 代码页 936）。
        if out.count("\ufffd") >= 2:
            try:
                alt = raw.decode("gbk", "replace")
            except (LookupError, UnicodeDecodeError):
                alt = ""
            # 只有 GBK 明显更干净时才采用（替换符更少）
            if alt and alt.count("\ufffd") < out.count("\ufffd"):
                out = alt
        err = (p.stderr or b"").decode("utf-8", "replace")
        return out, err
    except Exception as e:
        return "", str(e)


def _enum_uwp_apps():
    """枚举 UWP / Store 应用（Get-AppxPackage 是最可靠的来源）。

    注册表 AppModel 路径在不同 Windows 版本上结构不一，实测在本机只
    返回 1 个子键，完全不可用；PowerShell 的 Get-AppxPackage 是官方接口，
    稳定得多。只取当前用户安装的非框架包。

    显示名的取法有讲究：`Name` 形如 `Microsoft.WindowsCalculator` 或
    `Clipchamp.Clipchamp`，末段常常只是包名后缀（`Clipchamp`）甚至
    是无意义的短串（`0-x6`、`4`）。真正给人看的是 AppxManifest 里的
    DisplayName，所以优先从 InstallLocation 读清单，读不到再退回 Name。
    """
    script = (
        "Get-AppxPackage | Where-Object { -not $_.IsFramework -and "
        "$_.SignatureKind -ne 'System' } | "
        "Select-Object Name,PackageFullName,Version,"
        "PublisherDisplayName,InstallLocation | ConvertTo-Json -Compress"
    )
    out, _ = _run_ps(script, timeout=30)
    out = (out or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]

    apps = []
    for d in data:
        if not isinstance(d, dict):
            continue
        full = str(d.get("PackageFullName") or "").strip()
        if not full:
            continue
        loc = str(d.get("InstallLocation") or "").strip()
        raw_name = str(d.get("Name") or "").strip()
        publisher = str(d.get("PublisherDisplayName") or "").strip()

        name = _uwp_display_name(loc, raw_name)
        if not name:
            continue
        apps.append(AppInfo(
            key=f"UWP\\{full}",
            name=name,
            version=str(d.get("Version") or "").strip(),
            publisher=publisher,
            install_location=loc,
            hive="UWP",
            subkey=full,
            kind="uwp",
            product_code=full,
        ))
    return apps


def _uwp_display_name(install_location, raw_name):
    """取 UWP 应用的友好显示名。

    先读 AppxManifest.xml 的 <DisplayName>；它是 ms-resource: 间接引用时
    （本地化资源）读不出真名，此时退回从包名推导，并过滤掉明显无意义的短串。
    """
    # 1) 从清单文件读
    if install_location:
        man = os.path.join(_expand(install_location), "AppxManifest.xml")
        if os.path.isfile(man):
            try:
                with open(man, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(40000)
                m = re.search(
                    r"<DisplayName>(.*?)</DisplayName>", head,
                    re.IGNORECASE | re.DOTALL)
                if m:
                    v = m.group(1).strip()
                    # ms-resource: 是本地化资源占位符，不是真名字
                    if v and not v.lower().startswith("ms-resource:"):
                        return v
            except OSError:
                pass

    # 2) 从包名推导：取末段，驼峰加空格
    if not raw_name:
        return ""
    seg = raw_name.split(".")[-1]
    seg = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", seg).strip()
    # 过滤无意义名：纯数字、过短、形如 "0-x6" 的乱码
    if len(seg) < 3:
        return ""
    if re.match(r"^[\d\W_]+$", seg):
        return ""
    # 末段没意义时退回整包名的最后两段（"Clipchamp.Clipchamp" -> "Clipchamp"）
    if raw_name.count(".") >= 2:
        parts = raw_name.split(".")
        seg2 = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", parts[-2]).strip()
        if len(seg2) >= 3 and not re.match(r"^[\d\W_]+$", seg2):
            return f"{seg} ({seg2})" if seg2.lower() not in seg.lower() else seg
    return seg


def _dedup(apps):
    """去重。

    两个层次：
      1. 同名同版本同类型 —— 保留信息最全的那个
      2. 同名但类型不同（典型：Microsoft Edge 同时以 win32 和 uwp 出现）
         —— 只保留更“正统”的那个。别让用户看到两条一样的 Edge。
    """
    rank = {"HKLM": 0, "HKLM32": 1, "HKCU": 2, "HKCU32": 2, "UWP": 3}
    kind_rank = {"win32": 0, "msi": 1, "uwp": 2}

    def score(x):
        # 分越小越优先：有安装路径 > hive 优先级 > 有图标 > kind 优先级
        return (
            (0 if x.install_location else 1),
            rank.get(x.hive, 9),
            (0 if x.display_icon else 1),
            kind_rank.get(x.kind, 9),
        )

    # 第一层：完全同名同版本
    best = {}
    for a in apps:
        k = (a.name.lower(), a.version.lower())
        cur = best.get(k)
        if cur is None or score(a) < score(cur):
            best[k] = a

    # 第二层：同名（忽略版本），只在 kind 不同时才合并——
    # 同名的不同版本（如两套 VC++ 运行时）是合法的独立条目，不能合并。
    by_name = {}
    for a in best.values():
        by_name.setdefault(a.name.lower(), []).append(a)

    out = []
    for name, group in by_name.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        kinds = {g.kind for g in group}
        if len(kinds) == 1:
            out.extend(group)       # 同类型多版本，全留
            continue
        group.sort(key=score)
        out.append(group[0])        # 跨类型重复，只留最优的一个
    return out


# 系统级组件：默认隐藏。它们要么是 Windows 的一部分，要么被大量程序依赖，
# 误删会导致系统或一堆软件直接坏掉（Geek 列出它们是靠"显示系统组件"开关）。
_SYSTEM_APP_PATTERNS = [
    r"^microsoft edge$",
    r"^microsoft edge webview2 runtime$",
    r"^microsoft visual c\+\+",
    r"^microsoft \.net",
    r"^microsoft xna framework",
    r"^microsoft gameinput$",
    r"^microsoft windows",
    r"^windows ",
    r"^nvidia (app|control panel|graphics|hd |physx|usbc|fram|backend|messagebus|display|audio)",
    r"^intel\b",
    r"^amd\b",
    r"^realtek\b",
    r"^synaptics",
    r"^apple mobile device support$",
    r"^apple application support",
    r"^bonjour$",
    r"^microsoft store",
    r"^gaming services$",
    r"^gameinput",
    r"^visual c\+\+",
    r"^python \d",
    r"^winappruntime",
    r"^thundershell$",
    r"^vulkan runtime",
    r"^openal$",
    r"^directx",
    r"^vcredist",
    r"^java",
]


def is_system_app(app):
    """判断是否为系统级/共享组件（默认不展示给用户）。"""
    if app.system_component:
        return True
    name = (app.name or "").strip().lower()
    pub = (app.publisher or "").strip().lower()
    for pat in _SYSTEM_APP_PATTERNS:
        if re.search(pat, name):
            return True
    # 微软/驱动厂商发布的"运行时/驱动/框架"类
    if pub.startswith("microsoft") and any(
            k in name for k in ("runtime", "redistributable", "framework",
                                "driver", "drivers", "sdk")):
        return True
    return False


def scan_apps(on_progress=None, include_system=False):
    """枚举所有已安装程序。

    include_system=True 时连系统组件一起返回——对应 Geek 的"显示系统组件"
    行为；默认不返回，避免普通用户误删运行库、显卡驱动包这类东西。
    """
    raw = _enum_registry_apps()
    apps = []
    for a in raw:
        if not include_system and (is_system_app(a) or a.is_update):
            continue
        apps.append(a)

    try:
        for a in _enum_uwp_apps():
            # UWP 里同样有大量系统内置件（Edge、NVIDIA 控制面板、商店组件）
            if not include_system and is_system_app(a):
                continue
            apps.append(a)
    except Exception:
        pass

    apps = _dedup(apps)
    apps.sort(key=lambda x: x.name.lower())
    return apps


# ---------------------------------------------------------------------------
# 阶段 2：执行卸载
# ---------------------------------------------------------------------------
def build_uninstall_command(app, silent=True):
    """构造卸载命令行。

    几个必须留心的坑：

    * **MSI**：注册表里常见的是 `MsiExec.exe /I{GUID}`（/I 是安装），
      直接执行会变成"修复安装"而不是卸载。必须改成 `/X` + 静默参数。

    * **不能凭空给静默参数**：注册表只有 UninstallString 时，那条命令里
      往往带有软件自己的参数（如 `/currentuser`、`-uninstall=launcher`）。
      我们既不知道它支不支持 `/S`，也不该替它决定——所以只在软件**明确**
      提供了 QuietUninstallString 时才走静默；否则原样执行它自己的命令，
      让卸载向导按软件设计的流程走。曾在这里犯过错：给只有
      UninstallString 的程序硬塞 `/S`，把不认这个参数的卸载器搞乱。

    返回 (argv, 说明文本)；argv 为 None 表示没有可用的卸载命令。
    """
    if app.kind == "uwp":
        # UWP 走 Remove-AppxPackage
        ps = (f"Remove-AppxPackage -Package '{app.product_code}' "
              f"-ErrorAction Stop")
        return (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", ps], "移除应用商店应用")

    if app.kind == "msi" and app.product_code:
        code = app.product_code
        if silent:
            # /X 卸载，/qn 完全静默，/norestart 不自动重启
            return (["msiexec", "/X", code, "/qn", "/norestart"],
                    "MSI 静默卸载")
        return (["msiexec", "/X", code], "MSI 卸载（带卸载向导）")

    # 软件自己声明了静默卸载命令 —— 只有这种情况才走静默
    if silent and app.quiet_uninstall_string:
        return (_split_cmdline(app.quiet_uninstall_string),
                "静默卸载（软件自带）")

    # 其余一律原样执行它自己的卸载命令（不带任何我们编的参数）
    if app.uninstall_string:
        return (_split_cmdline(app.uninstall_string),
                "运行软件自带卸载程序")

    return (None, "该程序没有可用的卸载命令")


def _split_cmdline(cmdline):
    """把一条 Windows 命令行拆成 argv（正确剥掉引号）。

    不能直接用 `re.findall(r'"[^"]*"|\\S+')` —— 那样引号会留在 token 里，
    传给 subprocess 时变成 `'"powershell"'`，系统按这个名字去找可执行文件
    必然找不到（"找不到卸载程序"就是这么来的）。

    真正难缠的地方是引号只包住**一部分**路径的情况，例如
    `C:\\Program Files\\Uninstall.exe /S` 里安装目录带空格但没加引号。
    Windows 的 CreateProcess 自带一套回溯规则来处理它，这里照搬：
    依次尝试把后续片段并进 exe 路径，直到该路径真实存在为止。
    """
    s = (cmdline or "").strip()
    if not s:
        return []

    # 第一步：按 Windows 规则切分（引号分组、反斜杠转义）
    tokens = []
    cur = []
    in_quote = False
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            # 数一数连续的反斜杠
            j = i
            while j < n and s[j] == "\\":
                j += 1
            cnt = j - i
            if j < n and s[j] == '"':
                # 每两个反斜杠还原成一个，最后一个反斜杠转义引号
                cur.append("\\" * (cnt // 2))
                if cnt % 2 == 1:
                    cur.append('"')
                    j += 1
                i = j
                continue
            cur.append("\\" * cnt)
            i = j
            continue
        if c == '"':
            if in_quote:
                in_quote = False
            else:
                in_quote = True
            i += 1
            continue
        if c in " \t" and not in_quote:
            if cur:
                tokens.append("".join(cur))
                cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if cur:
        tokens.append("".join(cur))
    if not tokens:
        return []

    # 第二步：修复"路径含空格但未加引号"的情况。
    # 从原始串里直接找 exe：若第一个 token 指向的文件不存在，
    # 就把后面的 token 依次并进来重试。
    first = _expand(tokens[0])
    if not os.path.isfile(first):
        # 用原始未切分的串做贪心拼接（保留原始空格结构）
        raw = s
        if raw.startswith('"'):
            # 已带引号，取引号内内容作为 exe
            end = raw.find('"', 1)
            if end > 0:
                exe = raw[1:end]
                rest = raw[end + 1:].strip()
                merged = [exe] + (_split_cmdline(rest) if rest else [])
                if os.path.isfile(_expand(exe)):
                    return merged
        else:
            # 不带引号：逐个空格位置尝试切开
            pos = []
            idx = raw.find(" ")
            while idx != -1:
                pos.append(idx)
                idx = raw.find(" ", idx + 1)
            for p in pos:
                cand = raw[:p]
                if os.path.isfile(_expand(cand)):
                    rest = raw[p + 1:].strip()
                    return [cand] + (_split_cmdline(rest) if rest else [])
    return tokens


def run_uninstall(app, silent=True, on_status=None, timeout=None):
    """启动卸载程序并等待整个进程树结束。

    返回 (ok, message, exit_code)。

    Geek 的关键细节：要等**进程树**结束，而不是主进程退出就走人。
    很多卸载器会 fork 一个子进程干活、自己先退出，这时若立刻开始扫描残留，
    会把正在被卸载的文件误判成残留。
    """
    argv, desc = build_uninstall_command(app, silent=silent)
    if not argv:
        return False, desc, -1

    if on_status:
        on_status(desc)

    exe = argv[0]
    # 相对路径的卸载器（如 "uninst.exe" 依赖 cwd）需要把工作目录设对
    workdir = None
    if app.install_location:
        wd = _expand(app.install_location)
        if wd and os.path.isdir(wd):
            workdir = wd
    elif os.path.sep in exe and os.path.isabs(exe):
        workdir = os.path.dirname(exe)

    # 需要 UAC 的卸载器：用 ShellExecuteW("runas") 才能正常提权。
    # 先探测 exe 本身是否要求管理员（manifest 里 requireAdministrator）。
    needs_elevation = _exe_requires_elevation(exe)

    try:
        if needs_elevation and not is_admin():
            ok = _shell_execute_runas(argv, workdir)
            if not ok:
                return False, "提权被取消（UAC）", -1
            _wait_for_process_tree(exe, timeout=timeout)
            return True, "卸载程序已结束", 0

        proc = subprocess.Popen(
            argv, cwd=workdir,
            creationflags=_CREATE_NO_WINDOW,
        )
        _wait_proc_tree(proc.pid, timeout=timeout)
        code = proc.poll()
        return True, "卸载程序已结束", (code if code is not None else 0)
    except FileNotFoundError:
        return False, f"找不到卸载程序：{exe}", -1
    except Exception as e:
        return False, f"启动失败：{e}", -1


def _exe_requires_elevation(exe_path):
    """粗略判断 exe 的 manifest 是否要求管理员权限。

    做法：读取 PE 的 manifest 资源里的 requestedExecutionLevel。
    读不到就返回 False（交给系统按需弹 UAC）。
    """
    p = _expand(exe_path)
    if not p or not os.path.isfile(p):
        # 可能是 PATH 里的裸命令名（如 msiexec）
        return False
    try:
        with open(p, "rb") as f:
            head = f.read(2 * 1024 * 1024)     # manifest 一般在前 2MB
    except OSError:
        return False
    return b"requireAdministrator" in head


def _shell_execute_runas(argv, workdir):
    """用 ShellExecuteW 以管理员身份启动（会弹 UAC）。

    返回 False 表示用户取消了 UAC（ShellExecute 返回 <= 32）。
    """
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_SHOWNORMAL = 1

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wt.HWND),
            ("lpVerb", wt.LPCWSTR),
            ("lpFile", wt.LPCWSTR),
            ("lpParameters", wt.LPCWSTR),
            ("lpDirectory", wt.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wt.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wt.LPCWSTR),
            ("hkeyClass", wt.HKEY),
            ("dwHotKey", wt.DWORD),
            ("hIcon", wt.HANDLE),
            ("hProcess", wt.HANDLE),
        ]

    params = " ".join(_quote(a) for a in argv[1:]) if len(argv) > 1 else None
    info = SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = argv[0]
    info.lpParameters = params
    info.lpDirectory = workdir
    info.nShow = SW_SHOWNORMAL

    try:
        r = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
        return bool(r)
    except Exception:
        return False


def _quote(s):
    if not s:
        return '""'
    if " " in s or "\t" in s:
        return '"' + s + '"'
    return s


def _wait_proc_tree(root_pid, timeout=None):
    """等一个进程及其全部后代结束。

    用 taskkill 无法等待，所以用 WMI 轮询父子关系。为避免引入额外依赖，
    这里退回"轮询根进程 + 短暂宽限"的务实做法：先等根进程，
    再多等 2 秒让子进程收尾。
    """
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x102
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.WaitForSingleObject.restype = ctypes.c_ulong
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    h = k32.OpenProcess(SYNCHRONIZE, 0, int(root_pid))
    if not h:
        time.sleep(1.0)
        return
    try:
        # 让用户有机会走完卸载向导：默认给 10 分钟上限
        ms = int((timeout or 600) * 1000)
        k32.WaitForSingleObject(h, ms)
    finally:
        k32.CloseHandle(h)
    # 宽限期：等卸载器拉起的子进程收尾
    time.sleep(2.0)


def _wait_for_process_tree(exe_name, timeout=None):
    """提权路径下拿不到进程句柄，改为按 exe 名轮询。

    最多等 timeout 秒；进程消失后额外宽限 2 秒。
    """
    base = os.path.basename(_expand(exe_name)).lower()
    if not base:
        return
    deadline = time.time() + (timeout or 600)
    _CREATE_NO_WINDOW = 0x08000000
    while time.time() < deadline:
        try:
            p = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {base}", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
            if base not in (p.stdout or "").lower():
                break
        except Exception:
            break
        time.sleep(1.0)
    time.sleep(2.0)


# ---------------------------------------------------------------------------
# 阶段 3/4：残留扫描
# ---------------------------------------------------------------------------
def _is_safe_to_delete(path):
    """白名单校验：只允许删除用户数据区与程序安装区里的内容。

    拒绝的是系统关键位置——万一关键词匹配错了，也不能把 C:\\Windows
    下的东西删掉。
    """
    p = _norm(path)
    if not p:
        return False
    # 绝对禁止的根
    forbidden = [
        "c:\\windows", "c:\\windows\\system32", "c:\\windows\\syswow64",
        "c:\\program files", "c:\\program files (x86)",
        "c:\\programdata", "c:\\users", "c:\\perflogs", "c:\\recovery",
    ]
    for f in forbidden:
        if p == f:
            return False
    # 禁止删除盘符根目录本身
    if re.match(r"^[a-z]:$", p):
        return False
    # 禁止删除用户主目录本身、各 AppData 根本身
    home = _norm(os.path.expanduser("~"))
    for base in [home,
                 home + "\\appdata",
                 home + "\\appdata\\local",
                 home + "\\appdata\\roaming",
                 home + "\\appdata\\locallow",
                 home + "\\documents", home + "\\desktop",
                 home + "\\downloads"]:
        if p == base:
            return False
    # 路径深度至少两级：盘符 + 至少一层目录（"d:\\apps" 合法，"d:" 不合法）。
    # 注意很多软件就装在盘根下一级（D:\Games、D:\Apps），不能卡得太死。
    if not re.match(r"^[a-z]:\\[^\\]+", p):
        return False
    return True


def _candidate_appdata_dirs():
    out = []
    for t in _APPDATA_TEMPLATES:
        d = _expand(t)
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    # 去重（LocalLow 两种写法可能撞车）
    return out


def scan_leftovers(app, on_status=None, budget=22.0):
    """扫描一个程序的残留（文件 + 注册表）。

    策略与 Geek 一致，三个方向：
      A. 已知路径 —— InstallLocation 直接圈定
      B. 关联路径 —— 从 DisplayIcon 反推程序目录
      C. 搜索发现 —— 在 AppData / ProgramData 下按关键词找同名目录
    """
    deadline = time.time() + budget
    found = []
    seen = set()
    install_loc = _expand(app.install_location)
    kws = _keywords(app)

    def add_path(path, kind=None, safe=True, note=""):
        if time.time() > deadline:
            return
        p = _expand(path)
        if not p or not os.path.exists(p):
            return
        key = _norm(p)
        if key in seen:
            return
        if not _is_safe_to_delete(p):
            return
        seen.add(key)
        if kind is None:
            kind = "dir" if os.path.isdir(p) else "file"
        size = _dir_size(p, budget=min(4.0, max(0.5, deadline - time.time()))) \
            if kind == "dir" else _safe_filesize(p)
        found.append(Leftover(kind=kind, path=p, size=size,
                              safe=safe, note=note))

    # ---- A. 安装目录 ----
    if install_loc and os.path.isdir(install_loc):
        # 若安装目录就是某个通用父目录（如 Program Files），不能整个删
        add_path(install_loc, "dir", safe=True, note="安装目录")

    # ---- B. 从 DisplayIcon 反推 ----
    exe = _parse_exe_from_icon(app.display_icon)
    if exe:
        exe = _expand(exe)
        if os.path.isfile(exe):
            d = os.path.dirname(exe)
            # 只有当这个目录看起来是"专属目录"（不是 Program Files 本身）才收
            base = os.path.basename(d).lower()
            if base not in ("program files", "program files (x86)",
                            "windows", "system32", "bin", "app"):
                add_path(d, "dir", safe=True, note="从图标路径推导")

    # ---- C. AppData / ProgramData 关键词搜索 ----
    # 这一步最容易误伤，规则必须收紧。只用两种模式匹配：
    #   (1) 拼接名（产品名去掉空格，如 "EpicGames"）直接包含在目录名里
    #   (2) 目录名 == 某个关键词本身（精确相等，不做子串）
    # 绝不用单个通用词做子串匹配——"Launcher" 那种做法会命中一切。
    akeys = _app_keys(app)
    if akeys and on_status:
        on_status("扫描用户数据目录…")
    if akeys:
        roots = _candidate_appdata_dirs()
        for root in roots:
            if time.time() > deadline:
                break
            try:
                entries = list(os.scandir(root))
            except OSError:
                continue
            for e in entries:
                if time.time() > deadline:
                    break
                if not e.is_dir(follow_symlinks=False):
                    continue
                nm = e.name.lower()
                if nm in _PROTECTED_DIR_NAMES:
                    continue
                if _appdir_matches(nm, akeys):
                    add_path(e.path, "dir", safe=True,
                             note="用户数据目录（名称匹配）")

    # ---- 注册表扫描 ----
    if on_status:
        on_status("扫描注册表残留…")
    found.extend(_scan_registry_leftovers(app, kws, deadline))

    # ---- 补：该程序自己的卸载信息键 ----
    # 它在 Software\Microsoft\Windows\CurrentVersion\Uninstall 之下，
    # 而常规扫描为了安全会跳过 Microsoft 子树 —— 但这一项属于"卸载后
    # 必须清掉"的典型残留（否则控制面板里会永远留一个幽灵条目）。
    # 用精确的 subkey 定位，不做模糊匹配，所以是安全的。
    self_entry = _uninstall_entry_leftover(app)
    if self_entry is not None:
        found.append(self_entry)

    # 排序：文件在前（按体积降序），注册表在后
    found.sort(key=lambda x: (x.kind == "reg", -x.size, x.path.lower()))
    return found


def _uninstall_entry_leftover(app):
    """构造"程序自己的卸载信息键"这条残留项（若仍存在）。"""
    if app.kind == "uwp" or not app.subkey:
        return None
    for hive_tag, hive, path in UNINSTALL_KEYS:
        if hive_tag != app.hive:
            continue
        full = f"{path}\\{app.subkey}"
        try:
            k = winreg.OpenKey(hive, full)
            winreg.CloseKey(k)
        except OSError:
            return None
        return Leftover(
            kind="reg", path=f"{hive_tag}\\{full}",
            hive=hive_tag, subkey=full, hive_handle=hive,
            safe=True, note="卸载信息登记项",
        )
    return None


def _safe_filesize(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0

def _scan_registry_leftovers(app, kws, deadline):
    """在注册表里搜与程序相关的键。

    匹配规则与文件系统扫描保持同一套指纹（`_app_keys`），理由是两种扫描
    面对的是同一类误伤风险：注册表键名里同样混着大量通用词，
    用宽松关键词（如 "test"、"app"）去子串匹配会捞出一堆别人的键。

    具体规则：
      * 子键名 == 完整厂商名（整串比对，不接受厂商名的片段）
      * 子键名 包含某个长度 >= 5 的独有指纹
    只搜 SOFTWARE 前两层，不碰 Services / 系统关键项。
    """
    out = []
    publisher = (app.publisher or "").strip().lower()
    keys = _app_keys(app)
    # 注册表匹配用的独有词：合并 fused/exact，只留长度 >= 5 的
    strong = {k for k in (keys["fused"] | keys["exact"]) if len(k) >= 5}
    if not strong and len(publisher) < 4:
        return out

    # 明确跳过的子树：删了会出事
    skip_children = {
        "microsoft", "windows", "windows nt", "policies", "classes",
        "clients", "wow6432node", "intel", "nvidia", "amd", "google",
        "mozilla", "realtek", "oracle", "python", "java", "common files",
    }

    def walk(hive_tag, hive, path, depth, max_depth=2):
        if time.time() > deadline or depth > max_depth:
            return
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            return
        i = 0
        while True:
            if time.time() > deadline:
                return
            try:
                sub = winreg.EnumKey(root, i)
                i += 1
            except OSError:
                break
            low = sub.lower()
            if low in skip_children:
                continue
            full = f"{path}\\{sub}" if path else sub
            compact = re.sub(r"[\s\-_.]+", "", low)
            hit = False
            # 厂商名整串相等最可靠（"Apple Inc." 只在完全匹配时才算）
            if publisher and len(publisher) >= 4 and low == publisher:
                hit = True
            elif any(
                (kc in compact if len(kc) >= 5 else False) or kc == compact
                for kc in (re.sub(r"[\s\-_.]+", "", s) for s in strong)
            ):
                hit = True
            if hit:
                out.append(Leftover(
                    kind="reg", path=f"{hive_tag}\\{full}",
                    hive=hive_tag, subkey=full, hive_handle=hive,
                    safe=True, note="注册表残留",
                ))
                continue          # 命中即止，不再往里挖（父键已覆盖子树）
            walk(hive_tag, hive, full, depth + 1, max_depth)

    for hive_tag, hive, root_path in REG_SCAN_ROOTS:
        if time.time() > deadline:
            break
        # HKCR 结构特殊（键名是文件关联），不做深搜，风险收益不划算
        if hive_tag == "HKCR":
            continue
        walk(hive_tag, hive, root_path, 0, 2)
    return out


# ---------------------------------------------------------------------------
# 阶段 6：执行清理
# ---------------------------------------------------------------------------
def _move_file_delayed(path):
    """用 MoveFileExW 把文件/目录标记为"重启后删除"。

    卸载后被占用的 DLL 用普通删除会失败，Geek 也是靠这个 API 兜底。
    MOVEFILE_DELAY_UNTIL_REBOOT = 4
    """
    MOVEFILE_DELAY_UNTIL_REBOOT = 4
    try:
        return bool(ctypes.windll.kernel32.MoveFileExW(
            ctypes.c_wchar_p(path), None, MOVEFILE_DELAY_UNTIL_REBOOT))
    except Exception:
        return False


def _clear_readonly(path):
    """清掉只读/隐藏/系统属性。

    为什么必须做：**管理员权限也不会让只读文件变得可删**。Windows 的
    只读是个硬属性，`DeleteFile` 遇到它直接返回 ERROR_ACCESS_DENIED，
    提权进程一样删不掉。旧版本没有这一步，于是"只读文件残留"无论
    提权多少次都清不掉，表现为"权限不够"。
    """
    FILE_ATTRIBUTE_READONLY = 0x01
    FILE_ATTRIBUTE_HIDDEN = 0x02
    FILE_ATTRIBUTE_SYSTEM = 0x04
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(ctypes.c_wchar_p(path))
        if attrs == -1:                     # INVALID_FILE_ATTRIBUTES
            return False
        want = attrs & ~(FILE_ATTRIBUTE_READONLY |
                         FILE_ATTRIBUTE_HIDDEN |
                         FILE_ATTRIBUTE_SYSTEM)
        if want == attrs:
            return True
        return bool(ctypes.windll.kernel32.SetFileAttributesW(
            ctypes.c_wchar_p(path), want))
    except Exception:
        return False


def _dir_tree_clear_readonly(path):
    """把整棵树的只读/隐藏/系统属性都摘掉（topdown=False 才好逐层处理）。"""
    _clear_readonly(path)
    try:
        for root, dirs, files in os.walk(path):
            for nm in files:
                _clear_readonly(os.path.join(root, nm))
            for nm in dirs:
                _clear_readonly(os.path.join(root, nm))
    except OSError:
        pass


def _delete_dir(path):
    """删目录，返回 (是否删净, 已释放字节, 失败文件数, 失败原因样本)。

    先试 shutil.rmtree（快）；失败再逐项删。三项针对性处理：

      1. **先摘只读属性** —— 只读文件连管理员都删不掉，必须先清属性。
      2. **重试一次** —— 刚被卸载程序释放的句柄有延迟，立刻删会失败，
         短暂等待后重试命中率明显提高。
      3. **MoveFileEx 只当兜底，且要复核** —— 对**被占用**的文件有效；
         对**权限不足**的文件它也会"登记成功"却永远删不掉，所以不能
         把它当成删除成功的凭据，否则会把权限失败误报成"重启后自动删"。

    失败计数只统计"真的没删掉"的项（含仅登记待删的），这样调用方才能
    据此判断是否值得提权重试。
    """
    size = _dir_size(path, budget=6.0)
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
    if not os.path.exists(path):
        return True, size, 0, ""

    # 关键修复：只读属性会让删除必然失败，先统一摘掉再重试一次
    _dir_tree_clear_readonly(path)
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
    if not os.path.exists(path):
        return True, size, 0, ""

    # 逐项删（从最深层往上）
    failed = 0
    reasons = []
    pending = 0
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            fp = os.path.join(root, f)
            err = ""
            for attempt in range(2):
                try:
                    os.remove(fp)
                    err = ""
                    break
                except OSError as e:
                    err = str(e)
                    if attempt == 0:
                        _clear_readonly(fp)
                        time.sleep(0.12)
            if err:
                if _move_file_delayed(fp):
                    pending += 1          # 已登记重启后删，但仍不算"已删净"
                else:
                    failed += 1
                if len(reasons) < 3:
                    reasons.append(f"{os.path.basename(fp)}: {err}")
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                os.rmdir(dp)
            except OSError as e:
                if _move_file_delayed(dp):
                    pending += 1
                else:
                    failed += 1
                if len(reasons) < 3:
                    reasons.append(f"{os.path.basename(dp)}: {e}")
    try:
        os.rmdir(path)
    except OSError as e:
        if _move_file_delayed(path):
            pending += 1
        else:
            failed += 1
        if len(reasons) < 3:
            reasons.append(f"[根目录] {e}")

    gone = not os.path.exists(path)
    if not gone:
        # 目录还在：把"待重启删除"的也计入未完成，否则会被误判成"已处理"
        failed += pending
    return gone, (size if gone else 0), failed, "; ".join(reasons)


def _delete_reg_key(hive_handle, subkey):
    """递归删除注册表键。返回 (是否成功, 消息)。"""
    try:
        winreg.DeleteKeyEx(hive_handle, subkey,
                           winreg.KEY_WOW64_64KEY, 0)
        return True, ""
    except OSError:
        pass
    try:
        _recursive_del_key(hive_handle, subkey)
        return True, ""
    except OSError as e:
        return False, str(e)


def _recursive_del_key(hive, subkey):
    """DFS 递归删除注册表键（winreg 没有递归删除）。"""
    try:
        root = winreg.OpenKey(hive, subkey, 0,
                              winreg.KEY_ALL_ACCESS | winreg.KEY_WOW64_64KEY)
    except OSError:
        root = winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS)

    # 先递归删子键（每次取第 0 个，避免索引失效）
    while True:
        try:
            child = winreg.EnumKey(root, 0)
        except OSError:
            break
        _recursive_del_key(hive, f"{subkey}\\{child}")
    root.Close()
    winreg.DeleteKeyEx(hive, subkey, winreg.KEY_WOW64_64KEY, 0)


@dataclass
class CleanResult:
    freed: int = 0
    removed: int = 0
    failed: int = 0
    messages: list = field(default_factory=list)
    # 没删掉的路径清单。UI 据此判断"是否值得弹一次 UAC 用管理员重试"——
    # 旧版本没有任何这类信号，删不掉的 AppData 目录就这样静默失败了。
    retryable: list = field(default_factory=list)

    def __post_init__(self):
        if self.messages is None:
            self.messages = []
        if self.retryable is None:
            self.retryable = []


def clean_leftovers(items, on_progress=None):
    """删除用户勾选的残留项。

    items 是 Leftover 列表（只处理 checked=True 的）。
    返回 CleanResult。
    """
    res = CleanResult()
    targets = [it for it in items if it.checked]
    total = len(targets)
    for idx, it in enumerate(targets):
        if on_progress:
            on_progress(idx, total, it)
        if it.kind == "reg":
            ok, msg = _delete_reg_key(it.hive_handle, it.subkey)
            if ok:
                res.removed += 1
                res.messages.append(f"已删除注册表项 {it.path}")
            else:
                res.failed += 1
                res.messages.append(f"注册表项删除失败 {it.path}：{msg}")
            continue

        if not _is_safe_to_delete(it.path):
            res.failed += 1
            res.messages.append(f"跳过不安全路径 {it.path}")
            continue

        if it.kind == "dir" and os.path.isdir(it.path):
            gone, size, failed, why = _delete_dir(it.path)
            if gone:
                res.freed += size
                res.removed += 1
                res.messages.append(f"已删除目录 {it.path}")
            else:
                res.failed += max(1, failed)
                # 目录没删净 = 这一项还没完成，登记下来给 UI 做提权重试。
                res.retryable.append(it.path)
                if failed == 0:
                    res.messages.append(f"未能删除（被占用或权限受限）{it.path}")
                else:
                    res.messages.append(
                        f"未能完全删除 {it.path}"
                        + (f"（{why}）" if why else ""))
        elif os.path.isfile(it.path):
            size = _safe_filesize(it.path)
            ok, why = _delete_file(it.path)
            if ok:
                res.freed += size
                res.removed += 1
                if why:
                    res.messages.append(f"已标记重启后删除 {it.path}")
            else:
                res.failed += 1
                res.retryable.append(it.path)
                res.messages.append(f"删除失败 {it.path}" + (f"（{why}）" if why else ""))
    return res


def _delete_file(path):
    """删单个文件，返回 (是否删净, 说明)。

    与 `_delete_dir` 同一套策略：先摘只读属性（否则管理员也删不掉），
    失败短暂重试，最后才用 MoveFileEx 兜底标记重启后删除。
    """
    err = ""
    for attempt in range(2):
        try:
            os.remove(path)
            return True, ""
        except OSError as e:
            err = str(e)
            if attempt == 0:
                _clear_readonly(path)
                time.sleep(0.12)
    if _move_file_delayed(path):
        # 登记成功 != 马上消失，但重启后会被系统清掉，算处理完成
        return True, "已标记重启后删除"
    return False, err


def verify_removed(items):
    """清理后复核：统计仍有几项存在。返回 (剩余数, 检查总数)。"""
    left = 0
    n = 0
    for it in items:
        if not it.checked:
            continue
        n += 1
        if it.kind == "reg":
            try:
                winreg.CloseKey(winreg.OpenKey(it.hive_handle, it.subkey))
                left += 1
            except OSError:
                pass
        elif os.path.exists(it.path):
            left += 1
    return left, n


# ---------------------------------------------------------------------------
# 提权执行（UAC）：删不动的残留交给管理员子进程
# ---------------------------------------------------------------------------
def elevated_uninstall_main(payload_b64):
    """`Yuhub.exe --uninstall-elevated <base64-json>` 的入口。

    payload 里是待删除的残留项与回传文件路径。在管理员权限下删完，
    把结果写成 JSON 回传（和 cleaner 的 elevated_clean_main 同一套路）。
    """
    import base64
    try:
        payload = json.loads(base64.b64decode(payload_b64.encode("ascii"))
                             .decode("utf-8"))
    except Exception:
        return 2

    items_raw = payload.get("items") or []
    out_file = payload.get("out") or ""
    if not items_raw or not out_file:
        return 3

    hive_map = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKLM32": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKCU32": winreg.HKEY_CURRENT_USER,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
    }

    items = []
    for d in items_raw:
        it = Leftover(
            kind=d.get("kind", "file"),
            path=d.get("path", ""),
            size=int(d.get("size") or 0),
            hive=d.get("hive", ""),
            subkey=d.get("subkey", ""),
            safe=True, checked=True,
        )
        it.hive_handle = hive_map.get(it.hive, winreg.HKEY_LOCAL_MACHINE)
        if it.hive == "HKLM32":
            # WOW6432Node 路径已经带上前缀，句柄仍用 HKLM
            pass
        items.append(it)

    res = clean_leftovers(items)
    try:
        # 原子写：先写临时文件再改名，避免主进程读到写了一半的 JSON
        tmp = out_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "freed": res.freed,
                "removed": res.removed,
                "failed": res.failed,
                "messages": res.messages,
                "retryable": res.retryable,
            }, f, ensure_ascii=False)
        os.replace(tmp, out_file)
    except OSError:
        return 4
    return 0


def run_elevated_clean(items, timeout=180):
    """以管理员权限清理残留（弹一次 UAC）。

    返回结果 dict；None 表示 UAC 被取消或超时。
    """
    if not items:
        return {"freed": 0, "removed": 0, "failed": 0, "messages": []}

    exe = sys_executable()
    capable, why = elevation_capable(exe)
    if not capable:
        return {"freed": 0, "removed": 0, "failed": len(items),
                "messages": [why]}

    payload = {
        "items": [
            {"kind": it.kind, "path": it.path, "size": it.size,
             "hive": it.hive, "subkey": it.subkey}
            for it in items if it.checked
        ],
    }
    tmpdir = tempfile.mkdtemp(prefix="yuhub_uninst_")
    out_file = os.path.join(tmpdir, "result.json")
    payload["out"] = out_file

    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    import base64
    b64 = base64.b64encode(raw).decode("ascii")

    try:
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, f'--uninstall-elevated {b64}', None, 0)
        if int(r) <= 32:
            return None          # 用户取消 UAC
    except Exception:
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(out_file):
            try:
                with open(out_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                shutil.rmtree(tmpdir, ignore_errors=True)
                return data
            except (OSError, ValueError):
                pass
        time.sleep(0.4)

    shutil.rmtree(tmpdir, ignore_errors=True)
    return None


def sys_executable():
    """提权时要启动的可执行文件路径。

    必须是**打包后的 Yuhub.exe**，因为提权子进程靠 `--uninstall-elevated`
    这个开关来识别"我是被叫来清理残留的"，而只有 Yuhub.exe 认识它。
    源码模式下 `sys.executable` 是 python.exe：把它 runas 起来，参数会被
    当成脚本名（报 "can't open file '--uninstall-elevated'"），而且
    python.exe 是控制台程序，runas 还会闪一个黑窗口。

    所以这里和 etier.py 用同一套策略：冻结时用自己；否则在本目录里找
    构建好的 Yuhub.exe；确实找不到才回退（调用方会给出明确提示）。
    """
    import sys
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if exe and os.path.isfile(exe):
            return exe
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "Yuhub.exe"),
                 os.path.join(here, "dist", "Yuhub.exe")):
        if os.path.isfile(cand):
            return cand
    return sys.executable


def elevation_capable(exe_path=None):
    """能不能走 UAC 提权通道。返回 (bool, 给人看的原因)。

    源码模式且没构建 Yuhub.exe 时，提权必然失败（黑窗一闪、什么都不做），
    提前告诉用户"请先构建或直接用打包版"，比让他盯着 UAC 弹窗莫名其妙好得多。
    """
    exe = exe_path or sys_executable()
    if not exe:
        return False, "找不到可执行文件，无法提权"
    if os.path.basename(exe).lower() == "python.exe":
        return False, ("当前是源码运行且未找到构建好的 Yuhub.exe，"
                       "无法请求管理员权限。请先运行 build.bat 构建，"
                       "或直接使用打包后的 Yuhub.exe。")
    return True, ""


# ---------------------------------------------------------------------------
# 后台工作线程
# ---------------------------------------------------------------------------
class AppScanner(threading.Thread):
    """后台枚举已安装程序。

    `quiet=True` 表示"用户没主动点扫描，是程序自己刷新"（比如卸载完成后
    自动重扫）。UI 据此决定**不显示进度条、不抢状态栏文案**，避免打断
    用户正在看的残留结果。扫描行为本身完全一致。
    """

    def __init__(self, on_done, on_error=None, include_system=False, quiet=False):
        super().__init__(daemon=True, name="YuhubAppScanner")
        self._on_done = on_done
        self._on_error = on_error
        self._include_system = include_system
        self.quiet = quiet
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    @property
    def cancelled(self):
        return self._stop.is_set()

    def run(self):
        try:
            apps = scan_apps(include_system=self._include_system)
            if not self._stop.is_set():
                self._on_done(apps)
        except Exception as e:
            if self._on_error:
                self._on_error(str(e))


class UninstallWorker(threading.Thread):
    """后台执行卸载 + 残留扫描。"""

    def __init__(self, app, on_status, on_done, mode="uninstall"):
        super().__init__(daemon=True, name="YuhubUninstaller")
        self._app = app
        self._on_status = on_status
        self._on_done = on_done
        self._mode = mode
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    def run(self):
        try:
            if self._mode == "scan":
                # 只扫残留（强制卸载 / 重新扫描）
                items = scan_leftovers(self._app, on_status=self._on_status)
                self._on_done({"step": "scanned", "leftovers": items})
                return

            ok, msg, code = run_uninstall(self._app, silent=False,
                                          on_status=self._on_status)
            if not ok:
                self._on_done({"step": "failed", "message": msg})
                return
            if self._stop.is_set():
                return
            self._on_status("正在扫描残留文件与注册表…")
            items = scan_leftovers(self._app, on_status=self._on_status)
            self._on_done({"step": "scanned", "leftovers": items,
                           "message": msg})
        except Exception as e:
            self._on_done({"step": "failed", "message": str(e)})
