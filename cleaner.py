"""Yuhub 空间清理引擎（Dism++ Data.xml 移植版）。

规则来源：Dism++ 的 Data.xml（github.com/Chuyu-Team/Dism-Multi-language，
MIT License, Copyright (c) 2016 Chuyu-Team）。本模块把其中 CleanCollection4
的清理项逐条移植为 Python 规则表：General 类激活规则（RootPath + Query +
Excluded + Applicable FileExist/RegExist）移植为声明式 Target，由下方的
"规则解释器"统一扫描/删除；Custom 类过程用系统能力原生复刻（DISM 组件
清理、wevtutil、传递优化缓存、卷影存储等），无法安全复刻的（CompactOS、
硬链接合并、过期驱动、Appx 系）不收录。风险 Level 沿用 Data.xml 原值：
0=最危险 … 3=最安全（与旧版 Yuhub 的语义一致）。

相对旧版引擎修复的问题：
  1. measure() 旧的 2.5s 超时会**静默少算**大目录体积 → 扫描值虚低、
     「已清理」也虚低。现在取消人为限时（扫描本来就在后台线程）。
  2. 旧 `delivery_downloads` 规则名叫「旧版 Windows 安装包」实际删的却是
     SoftwareDistribution\\DataStore（更新安装记录），名不副实；旧
     `win_event_logs` 标为高风险、且 evtx 被服务占用根本删不掉。现按
     Data.xml 拆成「Windows下载缓存 / Windows更新安装记录 / Windows事件」
     三条，事件日志走 wevtutil cl 正确清空。
  3. 旧引擎对目录一律"清空内容"，而 Dism++ 很多条目（Windows.old、
     $Windows.~BT、驱动解压目录、ProgramData\\temp 等）应当**整目录删除**，
     旧做法留下大量空壳目录还照报成功。现在区分 contents / entries 两种模式。
  4. 回收站原来只清系统盘，Dism++ 清所有固定磁盘的 $Recycle.Bin。已改。
  5. 补齐 Dism++ 有而旧版缺失的大项：Windows 日志全家桶、WinINet Cookies、
     NET 程序集缓存、Prefetch、WinSxS 临时文件、MSOCache、ClickToRun、
     Package Cache 系安装源、NuGet、各老版本备份、备份文件组等。

核心原则（与旧版一致）：
  先扫描后清理；一律彻底删除（目标本身是缓存/临时文件），调用方必须先
  二次确认；不碰用户数据；「已清理体积」以**删除前测量、删除后复查**的
  真实对象体积为准，不迷信磁盘读数。

权限模型：
  Yuhub.exe 清单写死 requireAdministrator，进程**本身就是管理员**，引擎
  直接以当前权限执行，不做任何二次提权/弹 UAC；个别条目权限不足时按项
  计入 failed 并如实汇报，绝不部分失败就整段报错。

跨设备兼容性：
  全部规则都是**路径驱动**的，不依赖任何硬件型号或厂商——NVIDIA / AMD /
  Intel / D3DSCache 的着色器缓存是并列规则，本机没有哪个厂商的目录就
  展开为空、条目自动置灰；DISM / vssadmin / wevtutil 的输出解析对系统
  语言做了降级处理（utf-8 → gbk → latin-1，数字与单位恒为 ASCII）。
"""

from __future__ import annotations

import base64
import ctypes
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

IS_WIN = os.name == "nt"

# Win32 CREATE_NO_WINDOW：禁止给控制台子进程创建 conhost 窗口（防白窗闪烁）
_CREATE_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def human_bytes(n, digits=2):
    """字节数 → 易读字符串。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    if n < 0:
        return "--"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{int(n)} {units[0]}"
    return f"{n:.{digits}f} {units[i]}"


def drive_of(path):
    """取路径所在盘符（形如 "C:"）。"""
    p = os.path.abspath(path)
    d = os.path.splitdrive(p)[0]
    return d or "C:"


def drive_space(drive="C:") -> dict:
    """读取盘符的容量信息（ctypes 直调，微秒级）。"""
    letter = drive.rstrip("\\/").rstrip(":") + ":\\"
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    if IS_WIN:
        try:
            ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(letter),
                ctypes.byref(free),
                ctypes.byref(total),
                None,
            )
            if ok:
                f, t = free.value, total.value
                return {
                    "drive": letter.rstrip("\\"),
                    "free_bytes": f,
                    "total_bytes": t,
                    "used_bytes": t - f,
                    "percent": (t - f) / t * 100.0 if t else 0.0,
                }
        except Exception:
            pass
    try:
        u = shutil.disk_usage(letter)
        return {
            "drive": letter.rstrip("\\"),
            "free_bytes": u.free,
            "total_bytes": u.total,
            "used_bytes": u.used,
            "percent": u.used / u.total * 100.0 if u.total else 0.0,
        }
    except Exception:
        return {"drive": letter.rstrip("\\"), "free_bytes": 0,
                "total_bytes": 0, "used_bytes": 0, "percent": 0.0}


def is_admin():
    """当前进程**是否已提权**（TokenElevation，不能用 IsUserAnAdmin 判组）。"""
    if not IS_WIN:
        return False
    try:
        import winadmin
        return bool(winadmin.is_elevated())
    except Exception:
        return False


def fixed_drives() -> list[str]:
    """所有固定磁盘的盘根（形如 "C:\\"）。回收站 / Found.* / MSOCache 用。"""
    out = []
    if not IS_WIN:
        return out
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = letter + ":\\"
        if os.path.isdir(root) and \
                ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:
            out.append(root)
    return out


def measure(path, max_seconds=None, cancel: Callable[[], bool] | None = None) -> int:
    """递归统计目录/文件体积（字节）。

    旧版带 2.5s 超时，实测大目录（几 GB 的缓存）经常被超时截断，扫描值
    严重虚低。现在**不再人为限时**（扫描跑在后台线程，取消走 cancel）；
    max_seconds 参数仅为兼容保留、不再生效。统计失败（无权限/不存在）按 0。
    """
    total = 0
    try:
        if os.path.isfile(path) or os.path.islink(path):
            try:
                return os.path.getsize(path)
            except OSError:
                return 0
        if not os.path.isdir(path):
            return 0
        stack = [path]
        while stack:
            if cancel and cancel():
                break
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            else:
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        pass
    return total


# ---------------------------------------------------------------------------
# 路径表达式求值（对应 Data.xml 的 %var% / ?GetRegSz / ?GetFileVersion /
# ?GetIniData 写法）
# ---------------------------------------------------------------------------
_CUSTOM_ENV = None


def _custom_env() -> dict:
    """Data.xml 里的非标准环境变量 → 实际路径。"""
    global _CUSTOM_ENV
    if _CUSTOM_ENV is None:
        win = os.environ.get("SystemRoot", r"C:\Windows")
        docs = ""
        if IS_WIN:
            try:
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                    r"\User Shell Folders",
                ) as k:
                    docs = os.path.expandvars(str(winreg.QueryValueEx(k, "Personal")[0]))
            except Exception:
                docs = ""
        if not docs:
            docs = os.path.join(os.path.expanduser("~"), "Documents")
        _CUSTOM_ENV = {
            "SYSTEM": os.path.join(win, "System32"),
            "SYSTEMX86": os.path.join(win, "SysWOW64"),
            "DOCUMENTS": docs,
        }
    return _CUSTOM_ENV


def _expand_vars(raw: str) -> str:
    """展开 %VAR%（含 Data.xml 的 %System% / %SystemX86% / %Documents%）。"""
    def _sub(m):
        name = m.group(1).upper()
        if name in _custom_env():
            return _custom_env()[name]
        return os.environ.get(name, m.group(0))
    return re.sub(r"%([A-Za-z_()0-9]+)%", _sub, raw)


def _get_reg_sz(key_path: str, value: str, expand: bool) -> str:
    if not IS_WIN:
        return ""
    try:
        import winreg
        hive_name, sub = key_path.split("\\", 1)
        hive = {
            "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
            "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
            "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
        }.get(hive_name.upper())
        if hive is None:
            return ""
        # Dism++ 的 Wow64 写法在 64 位进程下天然读 64 位视图，无需额外处理
        access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        try:
            k = winreg.OpenKey(hive, sub, 0, access)
        except OSError:
            k = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ)
        with k:
            v = str(winreg.QueryValueEx(k, value)[0])
        return os.path.expandvars(v) if expand else v
    except Exception:
        return ""


def _get_file_version(exe_path: str) -> str:
    """读 exe/dll 的文件版本（"1.2.3.4"）。用于"老版本备份"条目的排除项。"""
    if not IS_WIN or not exe_path or not os.path.isfile(exe_path):
        return ""
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(ctypes.c_wchar_p(exe_path), None)
        if not size:
            return ""
        data = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(ctypes.c_wchar_p(exe_path), 0, size, data):
            return ""
        val = ctypes.c_void_p(0)
        val_len = ctypes.c_uint(0)
        if not ver.VerQueryValueW(data, "\\", ctypes.byref(val), ctypes.byref(val_len)):
            return ""
        # VS_FIXEDFILEINFO：dwFileVersionMS/LS 在偏移 8/12（跨 4 字节签名字段后）
        u32 = ctypes.cast(val, ctypes.POINTER(ctypes.c_uint32 * 5)).contents
        ms, ls = u32[3], u32[4]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        return ""


def _get_ini_data(ini_path: str, section: str, key: str) -> str:
    if not IS_WIN or not os.path.isfile(ini_path):
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.kernel32.GetPrivateProfileStringW(
            ctypes.c_wchar_p(section), ctypes.c_wchar_p(key), "",
            buf, 512, ctypes.c_wchar_p(ini_path),
        )
        return buf.value.strip()
    except Exception:
        return ""


def _eval_expr(raw: str, root_ctx: str = "") -> str:
    """求值一个路径表达式。

    支持：?GetRegSz(HIVE\\KEY,Value[,True/False])、?GetFileVersion(%Root%x.exe)、
    ?GetIniData(%Root%x.ini,Sec,Key)，函数后可以拼接字面后缀
    （如 ...GetRegSz(...)DwnlData\\）；其余按 %var% 展开。返回失败得空串。
    """
    if not raw:
        return ""
    raw = raw.strip()
    while raw.startswith("?"):
        m = re.match(r"\?([A-Za-z]+)\(", raw)
        if not m:
            return ""
        fname = m.group(1)
        # 找与 "(" 配对的 ")"（参数里一般不含括号，按计数配对兜底）
        depth = 0
        close = -1
        for i in range(m.end() - 1, len(raw)):
            if raw[i] == "(":
                depth += 1
            elif raw[i] == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
        if close < 0:
            return ""
        args_str = raw[m.end():close]
        suffix = raw[close + 1:]
        args = [a.strip() for a in args_str.split(",")] if args_str else []
        args = [a.replace("%Root%", root_ctx) for a in args]
        val = ""
        try:
            if fname == "GetRegSz" and len(args) >= 2:
                expand = len(args) >= 3 and args[2].lower() == "true"
                val = _get_reg_sz(args[0], args[1], expand)
            elif fname == "GetFileVersion" and args:
                val = _get_file_version(args[0])
            elif fname == "GetIniData" and len(args) >= 3:
                val = _get_ini_data(args[0], args[1], args[2])
        except Exception:
            val = ""
        if not val:
            return ""
        raw = val + suffix
    out = _expand_vars(raw)
    if ".." in out:                        # WPS 的 InstallRoot\..\ 写法
        out = os.path.normpath(out)
    return out


# ---------------------------------------------------------------------------
# 目标解析：RootPath 展开与 Query/Excluded 匹配
# ---------------------------------------------------------------------------
@dataclass
class Target:
    """一个激活目标（对应 Data.xml 的 <Activate><General .../>）。

    mode:
      contents —— 清空 root 目录的内容、保留目录本身（Data.xml 无 Flags
                  无 Query 的写法，如 %Temp%）；
      entries  —— 按 query 相对模式匹配 root 下的条目并**整条删除**
                  （Data.xml 带 Query 的写法；Flags=Directory 同此）。
    file_only —— 只匹配文件（Data.xml Flags=File，如 Windows 日志 *.log）。
    """

    root: str
    query: tuple = ()
    excluded: tuple = ()
    file_only: bool = False
    mode: str = "contents"


def T(root, *query, excl=(), file_only=False, mode=None):
    """Target 简写构造器。给了 query 就是 entries 模式。"""
    if mode is None:
        mode = "entries" if query else "contents"
    return Target(root=root, query=tuple(query), excluded=tuple(excl),
                  file_only=file_only, mode=mode)


def _expand_root(expr: str) -> list[str]:
    """把 RootPath 表达式展开成实际存在的目录列表。

    支持 '*'/'?' 通配段（逐级按目录名匹配），'*\\' / '*\\X\\Y' 展开为
    所有固定磁盘。返回的一定是已存在的目录。
    """
    s = _eval_expr(expr)
    if not s:
        return []
    s = s.replace("/", "\\")
    parts = [p for p in s.split("\\") if p]
    if not parts:
        return []
    drive = ""
    if len(parts[0]) == 2 and parts[0][1] == ":":
        drive, segs = parts[0], parts[1:]
    elif parts[0] == "*":
        drive, segs = "*", parts[1:]
    else:
        return []
    if drive == "*":
        cur = fixed_drives()
        if not segs:
            return cur
    else:
        cur = [drive + "\\"]
    for seg in segs:
        if ("*" in seg) or ("?" in seg):
            nxt = []
            for base in cur:
                try:
                    entries = os.listdir(base)
                except OSError:
                    continue
                for name in entries:
                    if fnmatch.fnmatch(name, seg) and \
                            os.path.isdir(os.path.join(base, name)):
                        nxt.append(os.path.join(base, name))
            cur = nxt
        else:
            cur = [os.path.join(b, seg) for b in cur]
    return [c for c in cur if os.path.isdir(c)]


def _excluded_match(name: str, excluded: tuple, values: list[str]) -> bool:
    for ex in list(excluded) + list(values):
        if not ex:
            continue
        if ("*" in ex) or ("?" in ex):
            if fnmatch.fnmatch(name, ex):
                return True
        elif name.lower() == ex.lower():
            return True
    return False


def _iter_matches(root_dir: str, tgt: Target, dyn_excl: list[str]):
    """按 query 相对模式逐级匹配 root_dir 下的条目（支持 a\\b\\*.log）。"""
    pats = list(tgt.query)
    if not pats:
        return

    def rec(dirpath, pats_left):
        pat, rest = pats_left[0], pats_left[1:]
        try:
            entries = os.listdir(dirpath)
        except OSError:
            return
        for name in entries:
            if not fnmatch.fnmatch(name, pat):
                continue
            child = os.path.join(dirpath, name)
            if rest:
                if os.path.isdir(child):
                    yield from rec(child, rest)
            else:
                if tgt.file_only and os.path.isdir(child):
                    continue
                if _excluded_match(name, tgt.excluded, dyn_excl):
                    continue
                yield child

    yield from rec(root_dir, pats)


# contents 模式的安全护栏：这些目录绝不允许被"清空内容"
def _critical_roots() -> set:
    win = os.environ.get("SystemRoot", r"C:\Windows")
    sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
    prof = os.environ.get("USERPROFILE", "")
    return {
        os.path.normpath(x).lower() for x in (
            sysdrive, win, os.path.join(win, "System32"),
            os.path.join(win, "SysWOW64"),
            os.path.join(sysdrive, "Users"),
            os.path.join(sysdrive, "Program Files"),
            os.path.join(sysdrive, "Program Files (x86)"),
            os.environ.get("ProgramData", ""),
            prof,
            os.path.join(prof, "AppData") if prof else "",
            os.path.join(prof, "AppData", "Local") if prof else "",
            os.path.join(prof, "AppData", "Roaming") if prof else "",
        ) if x
    }


# ---------------------------------------------------------------------------
# 规则定义（逐条对应 Data.xml CleanCollection4，Name/Level/Warning 原样移植）
# ---------------------------------------------------------------------------
@dataclass
class CleanRule:
    """一条可勾选的清理项。"""

    key: str
    name: str
    desc: str
    group: str
    level: int = 2           # Dism++ Data.xml：0=最危险 … 3=最安全
    admin: bool = False      # 元数据：该条目通常需要管理员权限才能删干净。
                             # 程序本身已由 exe 清单保证管理员启动，引擎不再
                             # 做任何二次提权；万一权限不足，逐项计入 failed。
    targets: list = field(default_factory=list)   # Target 列表
    warning: str = ""        # Data.xml 的 <Warning>（原样移植）
    special: str = ""        # 特殊处理：见 _SPECIALS
    slow_scan: bool = False  # 扫描很慢（要跑外部命令，几十秒~两分钟）→ 排最后
    # 运行期状态
    size: int = 0
    file_count: int = 0
    scanned: bool = False
    available: bool = False

    @property
    def risky(self):
        return self.level <= 1


GROUP_TEMP = "临时文件"
GROUP_CACHE = "缓存文件"
GROUP_GPU = "显卡缓存"
GROUP_SYSTEM = "系统相关"
GROUP_EXPIRED = "过期文件"
GROUP_APP = "应用程序"
GROUP_BACKUP = "备份文件"

GROUP_ORDER = [GROUP_TEMP, GROUP_CACHE, GROUP_GPU, GROUP_SYSTEM,
               GROUP_EXPIRED, GROUP_APP, GROUP_BACKUP]


def build_rules() -> list[CleanRule]:
    """构建全部清理规则（对照 Data.xml 逐条移植；注释里给出来源项名）。"""
    rules: list[CleanRule] = []

    def add(key, name, desc, group, level, targets, admin=False,
            warning="", special="", slow=False):
        rules.append(CleanRule(key=key, name=name, desc=desc, group=group,
                               level=level, targets=list(targets), admin=admin,
                               warning=warning, special=special,
                               slow_scan=slow))

    U = r"%SystemDrive%\Users\*"

    # ================= 临时文件 =================
    # Data.xml「临时文件」(L2)
    add("temp_files", "临时文件",
        "系统与各程序为用户产生的临时文件（用户/系统 Temp、UWP 容器、"
        "磁盘检查残留 Found.* 等）。",
        GROUP_TEMP, 2, [
            T(r"?GetRegSz(HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet"
              r"\Control\Session Manager\Environment,TEMP)"),
            T(r"%Temp%"),
            T(r"%SystemRoot%\Temp"),
            T(U + r"\AppData\Local\Temp"),
            T(U + r"\AppData\Local\Packages\*\AC\Temp"),
            T(U + r"\AppData\Local\Packages\*\AC\#!*\Temp"),
            T(r"%SystemRoot%\ServiceProfiles\*\AppData\Local\Temp"),
            T(r"%SystemRoot%\System32\config\systemprofile\AppData\Local\Temp"),
            T(r"%SystemRoot%\SysWOW64\config\systemprofile\AppData\Local\Temp"),
            T("*", "Found.*", mode="entries"),          # 磁盘检查碎片目录
        ], admin=True)

    # Data.xml「Windows日志」（by chenyu、YukiSakura）(L3)
    add("windows_logs", "Windows 日志",
        "Windows 在运行时产生的各类日志（系统目录、组件日志、WinSAT、"
        "WDI、CLR 等），清理后不影响系统运行。",
        GROUP_TEMP, 3, [
            T(r"%SystemRoot%", "*.log", "*.bak", "*log.txt", "SchedLgU.txt",
              r"Performance\WinSAT\*.log", r"Panther\*.log",
              file_only=True, mode="entries"),
            T(U + r"\AppData",
              r"Local\Microsoft\Windows\*.log", r"Local\MigWiz\*.log",
              r"Local\Microsoft\CLR_*\*.log", r"Local\Microsoft\CLR_*\*\*.log",
              r"Roaming\Microsoft\Windows\*.log",
              file_only=True, mode="entries"),
            T(r"%SystemDrive%\Logs"),
            T(r"%SystemRoot%\Logs"),
            T(r"%SystemRoot%\Performance\WinSAT\DataStore"),
            T(r"*\System Volume Information\Chkdsk"),
            T(r"%ProgramData%\USOShared\Logs"),
            T(r"%SystemRoot%\System32\WDI\LogFiles\*"),
            T(r"%SystemRoot%\System32\LogFiles\*"),
            T(r"%SystemRoot%\debug"),
            T(U + r"\AppData\Local\Packages\*\AC\Microsoft\CLR_*\*.log",
              file_only=True, mode="entries"),
            T(U + r"\AppData\Local\Packages\*\AC\#!*\Microsoft\CLR_*\*.log",
              file_only=True, mode="entries"),
            T(U + r"\AppData\Local\Packages\*\AC\Microsoft\CLR_*\*\*.log",
              file_only=True, mode="entries"),
            T(U + r"\AppData\Local\Packages\*\AC\#!*\Microsoft\CLR_*\*\*.log",
              file_only=True, mode="entries"),
        ], admin=True)

    # Data.xml「WinSxS临时文件」(L3)
    add("winsxs_temp", "WinSxS 临时文件",
        "系统更新时在 WinSxS 与 CbsTemp 里产生的临时文件。",
        GROUP_TEMP, 3, [
            T(r"%SystemRoot%\WinSxS\ManifestCache"),
            T(r"%SystemRoot%\CbsTemp"),
        ], admin=True)

    # Data.xml「常见的驱动临时解压目录」(L3)。Dism++ 还删盘根 "Prog"，
    # 那不是标准目录、误删用户自建目录的风险大，这里刻意不收。
    add("driver_temp", "常见的驱动临时解压目录",
        "Intel、AMD、NVIDIA 驱动在安装时留在盘根的解压目录。",
        GROUP_TEMP, 3, [
            T(r"%SystemDrive%", "AMD", "Intel", "NVIDIA", mode="entries"),
        ], admin=True)

    # Data.xml「Windows临时安装文件」(L2)
    add("windows_temp_install", "Windows 临时安装文件",
        "在系统升级或者还原后留下的系统临时安装文件，如果确定当前没有"
        "在升级系统，可以安全删除。",
        GROUP_TEMP, 2, [
            T(r"%SystemDrive%", "$Windows.~BT", "$Windows.~WS", "$Windows.~LS",
              mode="entries"),
        ], admin=True)

    # Data.xml「QQ临时数据」(L2)
    add("qq_temp", "QQ 临时数据",
        "QQ 运行时产生的临时文件与日志，不影响 QQ 消息记录。",
        GROUP_TEMP, 2, [
            T(U + r"\AppData\Roaming\Tencent", "AndroidAssist", "AndroidServer",
              "Logs", r"TXSSO\SetupLogs", r"TXSSO\SSOTemp", mode="entries"),
            T(U + r"\AppData\Roaming\Tencent\Users\*\WinTemp\*"),
            T(r"%SystemRoot%\System32\config\systemprofile\AppData"
              r"\Roaming\Tencent"),
        ])

    # Data.xml「YY临时文件」(L2)
    add("yy_temp", "YY 临时文件",
        "YY 运行时产生的临时文件与日志。",
        GROUP_TEMP, 2, [
            T(U + r"\AppData\Roaming\duowan\yy", "log", "Cache", mode="entries"),
        ])

    # Data.xml「腾讯相关软件下载目录」(L3)
    add("tencent_temp", "腾讯相关软件下载目录",
        "腾讯相关软件更新时留下的残留（ProgramData\\temp）。",
        GROUP_TEMP, 3, [
            T(r"%ProgramData%", "temp", mode="entries"),
        ], admin=True)

    # Data.xml「微软拼音安装文件」(L3)
    add("kunlun_input", "微软拼音安装文件",
        "微软拼音升级程序遗留下的安装文件。",
        GROUP_TEMP, 3, [
            T(U + r"\AppData\LocalLow", "KunlunInput", mode="entries"),
        ])

    # Data.xml「百度网盘日志」(L3)
    add("baidu_netdisk_logs", "百度网盘日志",
        "百度网盘运行时产生的日志文件。",
        GROUP_TEMP, 3, [
            T(r"%APPDATA%\BaiduYunKernel\Data"),
            T(r"%APPDATA%\BaiduYunGuanjia\logs"),
        ])

    # Data.xml「飞信日志」(L3)
    add("fetion_logs", "飞信日志",
        "飞信运行时产生的日志文件。",
        GROUP_TEMP, 3, [
            T(r"%APPDATA%\FetionV5"),
        ])

    # Data.xml「回收站」(L3) —— 所有固定磁盘
    add("recycle_bin", "回收站",
        "清空所有磁盘回收站中已删除的文件（不可恢复）。",
        GROUP_TEMP, 3, [], special="recycle")

    # Data.xml「系统以及程序崩溃dmp文件」（by YukiSakura）(L1)
    add("crash_dumps", "崩溃转储文件",
        "系统蓝屏或程序崩溃时产生的 dmp 文件（排查问题用，平时可删）。",
        GROUP_TEMP, 1, [
            T(U + r"\appdata\local", "crashdumps", mode="entries"),
            T(r"%SystemRoot%", "MEMORY.DMP", "Minidump", mode="entries"),
        ])

    # Data.xml「微软系安软无用文件」(L2)
    add("ms_av_temp", "微软系安软无用文件",
        "Windows Defender、MSE、SCEP 等微软系安全软件的无用副本与支持文件。",
        GROUP_TEMP, 2, [
            T(r"%ProgramData%\Microsoft\Windows Defender\LocalCopy"),
            T(r"%ProgramData%\Microsoft\Windows Defender\Support"),
            T(r"%ProgramData%\Microsoft\Windows Defender"
              r"\Network Inspection System\Support"),
            T(r"%ProgramData%\Microsoft\Microsoft Antimalware\LocalCopy"),
            T(r"%ProgramData%\Microsoft\Microsoft Antimalware\Support"),
            T(r"%ProgramData%\Microsoft\Microsoft Antimalware"
              r"\Network Inspection System\Support"),
        ], admin=True)

    # Data.xml「Visual Studio日志」(L2)
    add("vs_logs", "Visual Studio 日志",
        "使用 Visual Studio 所产生的日志。",
        GROUP_TEMP, 2, [
            T(U + r"\AppData\Local\Microsoft\VisualStudio\SettingsLogs"),
        ])

    # Data.xml「IDM临时文件」（by MS-PC2）(L2)
    add("idm_temp", "IDM 临时文件",
        "Internet Download Manager 的临时下载文件。",
        GROUP_TEMP, 2, [
            T(r"?GetRegSz(HKEY_CURRENT_USER\Software\DownloadManager,TempPath)"
              r"DwnlData"),
        ], warning="清理 IDM 临时文件可能导致正在下载的任务需要重新下载！")

    # Data.xml「英伟达驱动安装包」（by Ailick）(L1)
    add("nvidia_downloader", "英伟达驱动安装包",
        "NVIDIA GeForce Experience 更新驱动时下载的安装包。",
        GROUP_TEMP, 1, [
            T(r"%ProgramData%\NVIDIA Corporation\Downloader"),
        ], admin=True)

    # Data.xml「小红伞临时文件」(L0)
    add("avira_temp", "小红伞临时文件",
        "小红伞在自动升级过程中残留的临时文件。",
        GROUP_TEMP, 0, [
            T(r"%ProgramData%\Avira\*", "TEMP", "BACKUP", mode="entries"),
        ])

    # ================= 缓存文件 =================
    # Data.xml「WinINet网页缓存」(L3)
    add("wininet_cache", "WinINet 网页缓存",
        "各用户账户、服务与应用容器下 Windows 网络组件（IE、Edge 等）的"
        "网页缓存（默认目录）。",
        GROUP_CACHE, 3, [
            T(U + r"\AppData\Local\Microsoft\Windows\INetCache"),
            T(U + r"\AppData\Local\Microsoft\Windows\Temporary Internet Files"),
            T(r"%SystemRoot%\ServiceProfiles\*\AppData\Local\Microsoft"
              r"\Windows\INetCache"),
            T(r"%SystemRoot%\ServiceProfiles\*\AppData\Local\Microsoft"
              r"\Windows\Temporary Internet Files"),
            T(r"%SystemRoot%\System32\config\systemprofile\AppData\Local"
              r"\Microsoft\Windows\INetCache"),
            T(r"%SystemRoot%\System32\config\systemprofile\AppData\Local"
              r"\Microsoft\Windows\Temporary Internet Files"),
            T(r"%SystemRoot%\SysWOW64\config\systemprofile\AppData\Local"
              r"\Microsoft\Windows\INetCache"),
            T(r"%SystemRoot%\SysWOW64\config\systemprofile\AppData\Local"
              r"\Microsoft\Windows\Temporary Internet Files"),
            T(U + r"\AppData\Local\Packages\*\AC\INetCache"),
            T(U + r"\AppData\Local\Packages\*\AC\#!*\INetCache"),
            T(U + r"\AppData\Local\Packages\*\AC\MicrosoftEdge\Cache"),
            T(U + r"\AppData\Local\Packages\*\AC\#!*\MicrosoftEdge\Cache"),
        ], admin=True)

    # Data.xml「WinINet Cookies」(L2)
    add("wininet_cookies", "WinINet Cookies",
        "各用户账户、服务与应用容器下 Windows 网络组件的 Cookies"
        "（清理后需要重新登录部分网站）。",
        GROUP_CACHE, 2, [
            T(U + r"\AppData\Local\Microsoft\Windows\INetCookies"),
            T(U + r"\AppData\Roaming\Microsoft\Windows\Cookies"),
            T(r"%SystemRoot%\ServiceProfiles\*\AppData\Local\Microsoft"
              r"\Windows\INetCookies"),
            T(r"%SystemRoot%\ServiceProfiles\*\AppData\Roaming\Microsoft"
              r"\Windows\Cookies"),
            T(r"%SystemRoot%\System32\config\systemprofile\AppData\Local"
              r"\Microsoft\Windows\INetCookies"),
            T(r"%SystemRoot%\System32\config\systemprofile\AppData\Roaming"
              r"\Microsoft\Windows\Cookies"),
            T(r"%SystemRoot%\SysWOW64\config\systemprofile\AppData\Local"
              r"\Microsoft\Windows\INetCookies"),
            T(r"%SystemRoot%\SysWOW64\config\systemprofile\AppData\Roaming"
              r"\Microsoft\Windows\Cookies"),
            T(U + r"\AppData\Local\Packages\*\AC\INetCookies"),
            T(U + r"\AppData\Local\Packages\*\AC\#!*\INetCookies"),
            T(U + r"\AppData\Local\Packages\*\AC\MicrosoftEdge\Cookies"),
            T(U + r"\AppData\Local\Packages\*\AC\#!*\MicrosoftEdge\Cookies"),
        ], admin=True)

    # Data.xml「缩略图缓存」(L2)
    add("thumbnail_cache", "缩略图缓存",
        "资源管理器的缩略图与图标缓存。个别文件被占用时会自动重启资源"
        "管理器释放（任务栏会短暂消失一下）。",
        GROUP_CACHE, 2, [
            T(r"%LocalAppData%", r"Microsoft\Windows\Explorer\*", "IconCache.db",
              mode="entries"),
            T(U + r"\AppData\Local", r"Microsoft\Windows\Explorer\*",
              "IconCache.db", mode="entries"),
        ], admin=True)

    # Data.xml「Terminal Server Client缓存」(L3)
    add("tsclient_cache", "远程桌面连接缓存",
        "远程桌面（Terminal Server Client）运行时产生的缓存文件。",
        GROUP_CACHE, 3, [
            T(U + r"\AppData\Local\Microsoft\Terminal Server Client"),
        ])

    # Data.xml「XDE缓存文件」(L2)
    add("xde_cache", "XDE 缓存文件",
        "Windows Phone 开发模拟器的虚拟机状态文件，不建议频繁清理。",
        GROUP_CACHE, 2, [
            T(r"%ProgramData%\Microsoft\XDE"),
            T(U + r"\AppData\Local\Microsoft\XDE"),
        ])

    # Data.xml「PDB缓存文件」(L2)
    add("pdb_cache", "PDB 调试符号缓存",
        "调试程序时创建的符号缓存，不建议频繁清理。",
        GROUP_CACHE, 2, [
            T(r"%LOCALAPPDATA%", "DBG", mode="entries"),
        ])

    # Data.xml「NET程序集缓存」（by 大胖子技术员）(L2)
    add("net_assembly", "NET 程序集缓存",
        ".NET 程序运行时产生的本机映像缓存，清理后首次启动相关程序稍慢。",
        GROUP_CACHE, 2, [
            T(r"%SystemRoot%\assembly\NativeImages_v*", "*", excl=("Temp",),
              mode="entries"),
            T(r"%SystemRoot%\assembly\temp"),
            T(r"%SystemRoot%\assembly\tmp"),
        ], admin=True)

    # Data.xml「Windows预读取文件」（by 辰g.）(L2)
    add("prefetch", "Windows 预读取文件",
        "用于加速下次程序启动，不建议频繁清理此条目。",
        GROUP_CACHE, 2, [
            T(r"%SystemRoot%\Prefetch"),
        ], admin=True)

    # Data.xml「NuGet包缓存」(L2)
    add("nuget_cache", "NuGet 包缓存",
        "Visual Studio 生成引用了 NuGet 包的 .NET 应用时产生的缓存。",
        GROUP_CACHE, 2, [
            T(r"%USERPROFILE%\.nuget"),
        ], warning="如果未在 Visual Studio 中开启“允许 NuGet 下载缺少的程序包”，"
                   "应用生成将会失败。")

    # Data.xml「Intel驱动安装源缓存」（by YukiSakura）(L0)
    add("intel_cache", "Intel 驱动安装源缓存",
        "Intel 驱动安装后留下的安装源缓存。",
        GROUP_CACHE, 0, [
            T(r"%ProgramData%\Intel\Package Cache"),
        ], admin=True, warning="此条目可能影响 Intel 驱动的卸载。")

    # Data.xml「英伟达驱动安装源缓存」（by YukiSakura）(L0)
    add("nvidia_installer", "英伟达驱动安装源缓存",
        "NVIDIA 驱动安装时解压的安装源（Installer2 / NvBackend packages）。",
        GROUP_CACHE, 0, [
            T(r"%ProgramFiles%\NVIDIA Corporation\Installer2"),
            T(U + r"\appdata\nvidia\nvbackend\packages"),
        ], admin=True, warning="此条目可能影响英伟达驱动的卸载。")

    # Data.xml「瑞昱声卡驱动安装源缓存」（by YukiSakura）(L0)
    add("realtek_cache", "瑞昱声卡驱动安装源缓存",
        "Realtek 声卡安装后留下的安装源缓存（保留 HDA / ASIO 运行组件）。",
        GROUP_CACHE, 0, [
            T(r"%ProgramFiles%\Realtek\Audio", "*", excl=("HDA", "ASIO"),
              mode="entries"),
        ], admin=True, warning="此条目可能影响瑞昱声卡驱动的卸载。")

    # Data.xml「Java安装源缓存」（by YukiSakura）(L0)
    add("java_cache", "Java 安装源缓存",
        "Java 安装后留下的旧版 JRE 与安装缓存。",
        GROUP_CACHE, 0, [
            T(U + r"\AppData\Roaming\Oracle\Java", "jre*.*.*_*", "installcache",
              "tmpinstall", mode="entries"),
        ], warning="此条目可能影响 Java 的卸载。")

    # Data.xml「Microsoft .NET安装缓存」（by 撕裂五维空间）(L0)
    add("net_install_cache", "Microsoft .NET 安装缓存",
        "安装 Microsoft .NET 补丁残留的安装缓存（Multi-Targeting Pack "
        "SetupCache）。",
        GROUP_CACHE, 0, [
            T(r"%ProgramFiles%\Microsoft.NET\Multi-Targeting Pack\*",
              "SetupCache", mode="entries"),
            T(r"%ProgramFilesX86%\Microsoft.NET\Multi-Targeting Pack\*",
              "SetupCache", mode="entries"),
        ], admin=True, warning="此条目可能影响 .NET 补丁的卸载。")

    # Data.xml「Microsoft SQL Server更新缓存」（by Honry、撕裂五维空间）(L0)
    add("sql_server_cache", "SQL Server 更新缓存",
        "安装 Microsoft SQL Server 补丁残留的安装缓存。",
        GROUP_CACHE, 0, [
            T(r"%ProgramFiles%\Microsoft SQL Server\*\Setup Bootstrap",
              "Update Cache", "SQLServer2008R2", mode="entries"),
            T(r"%ProgramFilesX86%\Microsoft SQL Server\*\Setup Bootstrap",
              "Update Cache", "SQLServer2008R2", mode="entries"),
            T(r"%ProgramFiles%\Microsoft SQL Server\*\Setup Bootstrap\Log"),
            T(r"%ProgramFilesX86%\Microsoft SQL Server\*\Setup Bootstrap\Log"),
        ], admin=True)

    # Data.xml「Adobe Acrobat安装源缓存」（by Honry）(L0)
    add("adobe_acrobat_cache", "Adobe Acrobat 安装源缓存",
        "安装 Adobe Acrobat 后留下的安装缓存。",
        GROUP_CACHE, 0, [
            T(r"%ProgramFilesX86%\Adobe\Acrobat DC\Setup Files"),
        ], admin=True, warning="删除此条目可能导致 Adobe Acrobat 无法正常卸载！")

    # Yuhub 保留项（Data.xml 没有但安全实用，全部可自动重建）
    LA = os.environ.get("LOCALAPPDATA", "")
    if True:
        add("gpu_nvidia_shader", "NVIDIA 着色器缓存",
            "NVIDIA 显卡的 DX/GL/Compute 着色器缓存，删除后游戏首次加载会"
            "重新编译（自动重建）。",
            GROUP_GPU, 3, [
                T(r"%LocalAppData%\NVIDIA\DXCache"),
                T(r"%LocalAppData%\NVIDIA\GLCache"),
                T(r"%LocalAppData%\NVIDIA\ComputeCache"),
                T(r"%LocalAppData%\NVIDIA Corporation\NV_Cache"),
            ])
        add("gpu_amd_shader", "AMD 着色器缓存",
            "AMD 显卡的 DX/GL/Vulkan 着色器缓存（自动重建）。",
            GROUP_GPU, 3, [
                T(r"%LocalAppData%\AMD\DxCache"),
                T(r"%LocalAppData%\AMD\DxcCache"),
                T(r"%LocalAppData%\AMD\GLCache"),
                T(r"%LocalAppData%\AMD\VkCache"),
            ])
        add("gpu_intel_shader", "Intel 着色器缓存",
            "Intel 核显的着色器缓存（自动重建）。",
            GROUP_GPU, 3, [
                T(r"%LocalAppData%\Intel\ShaderCache"),
                T(r"%LocalAppData%\D3DSCache"),
            ])
        del LA

    # ================= 系统相关 =================
    # Data.xml「Windows报告」(L3)
    add("wer_reports", "Windows 报告",
        "程序遇到问题时系统留下的稳定性报告（WER），建议定期清理。",
        GROUP_SYSTEM, 3, [
            T(U + r"\AppData\Local\Microsoft\Windows\WER"),
            T(r"%ProgramData%\Microsoft\Windows\WER"),
            T(r"%SystemRoot%\LiveKernelReports"),
        ], admin=True)

    # Data.xml「Windows事件」(L3)：wevtutil 清事件日志 + winevt\Logs 内容
    add("win_events", "Windows 事件",
        "Windows 在运行过程中产生的事件日志（evtx），清理后历史记录丢失。",
        GROUP_SYSTEM, 3, [
            T(r"%SystemRoot%\System32\winevt\Logs"),
        ], admin=True, special="win_events")

    # Data.xml「Windows下载缓存」(L2)
    add("wu_download", "Windows 下载缓存",
        "Windows 下载更新与商店应用时产生的临时安装包，可安全删除。",
        GROUP_SYSTEM, 2, [
            T(r"%SystemRoot%\SoftwareDistribution\Download"),
        ], admin=True)

    # Data.xml「Windows更新安装记录」(L2)
    add("wu_records", "Windows 更新安装记录",
        "使用 Windows Update 安装更新后保存的记录（DataStore，需先停"
        "更新服务再清）。",
        GROUP_SYSTEM, 2, [
            T(r"%SystemRoot%\SoftwareDistribution\DataStore"),
        ], admin=True, special="wu_records",
        warning="删除后更新历史记录将被清空。")

    # Data.xml「传递优化缓存」(L2)
    add("do_cache", "传递优化缓存",
        "Win10 起使用传递优化（Delivery Optimization）服务下载更新与 UWP "
        "应用产生的缓存。",
        GROUP_SYSTEM, 2, [
            T(r"%SystemRoot%\SoftwareDistribution\DeliveryOptimization"),
            T(r"%SystemRoot%\DeliveryOptimization"),
            T(r"%SystemRoot%\ServiceProfiles\NetworkService\AppData\Local"
              r"\Microsoft\Windows\DeliveryOptimization"),
        ], admin=True, special="do_cache")

    # Data.xml「零售演示离线内容」(L2)
    add("retail_demo", "零售演示离线内容",
        "为 Windows 10 零售演示模式所缓存的离线内容。",
        GROUP_SYSTEM, 2, [
            T(r"%ProgramData%\Microsoft\Windows\RetailDemo"),
        ], admin=True)

    # Data.xml「被取代的WinSxS组件」(L2)：走系统 DISM 组件清理
    add("dism_component", "被取代的 WinSxS 组件",
        "调用系统 DISM 清理被取代的组件、缩小组件存储体积（过程较慢，"
        "期间请勿关机）。",
        GROUP_SYSTEM, 2, [], admin=True, special="dism_component",
        slow=True,
        warning="清理后将无法卸载已安装的更新。")

    # Data.xml「系统还原点」(L2)：删全部还原点再建一个新还原点
    add("sr_points", "系统还原点",
        "先删除全部还原点，再创建一个新的还原点（当系统还原已启用时）。",
        GROUP_SYSTEM, 2, [], admin=True, special="sr_points",
        warning="清理后将无法使用之前创建的还原点。")

    # ================= 过期文件 =================
    # Data.xml「以前的Windows系统」(L2)
    add("windows_old", "以前的 Windows 系统",
        "系统升级或还原后留下的旧系统备份（Windows.old）。",
        GROUP_EXPIRED, 2, [
            T(r"%SystemDrive%", "Windows.old*", mode="entries"),
        ], admin=True, warning="删除后你将无法回滚到以前安装的系统。")

    # Data.xml「Chrome浏览器老版本备份」（by Honry）(L2)
    add("chrome_old", "Chrome 老版本备份",
        "Chrome 升级到新版本后留下的旧版本目录（当前版本自动排除）。",
        GROUP_EXPIRED, 2, [
            T(r"?GetRegSz(HKEY_CURRENT_USER\Software\Microsoft\Windows"
              r"\CurrentVersion\App Paths\chrome.exe,Path)",
              "*.*.*.*",
              excl=("?GetFileVersion(%Root%Chrome.exe)",), mode="entries"),
            T(r"?GetRegSz(HKEY_LOCAL_MACHINE\Software\Microsoft\Windows"
              r"\CurrentVersion\App Paths\chrome.exe,Path)",
              "*.*.*.*",
              excl=("?GetFileVersion(%Root%Chrome.exe)",), mode="entries"),
        ])

    # Data.xml「360浏览器老版本备份」(L2)
    add("se360_old", "360 浏览器老版本备份",
        "360 浏览器升级后留下的旧版本备份目录（当前版本自动排除）。",
        GROUP_EXPIRED, 2, [
            T(r"%APPDATA%\360se6\Application", "*.*.*.*",
              excl=("?GetFileVersion(%Root%360se.exe)",), mode="entries"),
        ])

    # Data.xml「Opera浏览器老版本备份」（by MS-PC2）(L2)
    add("opera_old", "Opera 老版本备份",
        "Opera 升级到新版本后留下的旧版本目录（当前版本自动排除）。",
        GROUP_EXPIRED, 2, [
            T(r"?GetRegSz(HKEY_CURRENT_USER\Software\Microsoft\Windows"
              r"\CurrentVersion\App Paths\opera.exe,Path)",
              "*.*.*.*",
              excl=("?GetFileVersion(%Root%Launcher.exe)",), mode="entries"),
            T(r"?GetRegSz(HKEY_LOCAL_MACHINE\Software\Microsoft\Windows"
              r"\CurrentVersion\App Paths\opera.exe,Path)",
              "*.*.*.*",
              excl=("?GetFileVersion(%Root%Launcher.exe)",), mode="entries"),
        ])

    # Data.xml「阿里旺旺老版本备份」(L2)
    add("wangwang_old", "阿里旺旺老版本备份",
        "阿里旺旺升级后留下的旧版本备份目录（当前版本自动排除）。",
        GROUP_EXPIRED, 2, [
            T(r"?GetRegSz(HKEY_LOCAL_MACHINE\SOFTWARE\Alibaba\WWLights"
              r"\EE76A38A6C9E01F42C551FE3BE585C3B,Path,True)",
              "*.*.*.*",
              excl=("?GetIniData(%Root%Aliim.ini,Common,Version)",),
              mode="entries"),
        ])

    # Data.xml「阿里亲淘老版本备份」(L2)
    add("qintao_old", "阿里亲淘老版本备份",
        "阿里亲淘升级后留下的旧版本备份目录（当前版本自动排除）。",
        GROUP_EXPIRED, 2, [
            T(r"?GetRegSz(HKEY_LOCAL_MACHINE\SOFTWARE\Alibaba\WWLights"
              r"\810588CDA60249EB05C110B0DED77CD8,Path,True)",
              "*.*.*.*",
              excl=("?GetIniData(%Root%AliQinTao.ini,Common,Version)",),
              mode="entries"),
        ])

    # Data.xml「PPLive老版本备份」（by 何超）(L2)
    add("pplive_old", "PPLive 老版本备份",
        "PPLive 升级后留下的旧版本备份目录（当前版本自动排除）。",
        GROUP_EXPIRED, 2, [
            T(r"?GetRegSz(HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows"
              r"\CurrentVersion\App Paths\PPLive.exe,Path)",
              "*.*.*.*",
              excl=("?GetIniData(%Root%version.dat,version,current)",),
              mode="entries"),
        ])

    # Data.xml「酷狗音乐老版本备份」（by 何超）(L2)
    add("kugou_old", "酷狗音乐老版本备份",
        "酷狗音乐升级后留下的旧版本备份目录（当前版本自动排除）。",
        GROUP_EXPIRED, 2, [
            T(r"?GetRegSz(HKEY_CURRENT_USER\Software\kugou,AppPath)",
              "*.*.*.*",
              excl=(r"?GetRegSz(HKEY_CURRENT_USER\Software\KuGou8,AppVersion)",),
              mode="entries"),
        ])

    # Data.xml「2345拼音输入法老版本备份」（by 何超）(L2)
    add("pinyin2345_old", "2345 拼音输入法老版本备份",
        "2345 拼音输入法升级后留下的旧版本备份目录（当前版本自动排除）。",
        GROUP_EXPIRED, 2, [
            T(r"?GetRegSz(HKEY_LOCAL_MACHINE\SOFTWARE\2345Pinyin,Path,True)",
              "*.*.*.*",
              excl=(r"?GetRegSz(HKEY_LOCAL_MACHINE\SOFTWARE\2345Pinyin"
                    r",Version,True)",),
              mode="entries"),
        ])

    # Data.xml「WPS老版本备份」（by Rambin）(L2)
    add("wps_old", "WPS 老版本备份",
        "WPS 升级后留下的旧版本备份目录（当前版本自动排除）。",
        GROUP_EXPIRED, 2, [
            T("?GetRegSz(HKEY_CURRENT_USER\\SOFTWARE\\kingsoft\\office\\6.0"
              "\\Common,InstallRoot)\\..\\",
              "*.*.*.*",
              excl=(r"?GetRegSz(HKEY_CURRENT_USER\SOFTWARE\kingsoft\office"
                    r"\6.0\Common,Version)",),
              mode="entries"),
        ])

    # ================= 应用程序 =================
    # Data.xml「Windows Defender保护历史记录」(L2)
    add("defender_history", "Defender 保护历史",
        "Windows Defender 的扫描与查杀历史记录（日志过多会导致保护历史"
        "页面闪退）。",
        GROUP_APP, 2, [
            T(r"%ProgramData%\Microsoft\Windows Defender\Scans\History"),
        ], admin=True, warning="清理后无法还原被隔离文件。")

    # Data.xml「WebPI缓存」(L2)
    add("webpi_cache", "WebPI 缓存",
        "Web 平台安装程序（Web Platform Installer）的缓存。",
        GROUP_APP, 2, [
            T(U + r"\AppData\Local\Microsoft\Web Platform Installer"),
        ])

    # Data.xml「Office安装源」(L0)：各盘根 MSOCache
    add("msocache", "Office 安装源",
        "安装 Office 后产生的安装源（MSOCache），删除后能正常安装 Office "
        "更新。",
        GROUP_APP, 0, [
            T("*", "MSOCache", mode="entries"),
        ], admin=True,
        warning="清理后将无法卸载，只能升级安装或强行删除。")

    # Data.xml「Office 365/2016安装源」(L0)
    add("click2run", "Office 365/2016 安装源",
        "点播（Click-to-Run）版 Office 的安装源缓存（保留 MachineData / "
        "UserData 配置）。",
        GROUP_APP, 0, [
            T(r"%ProgramData%\Microsoft\ClickToRun",
              excl=("MachineData", "UserData")),
        ], admin=True, warning="清理可能导致无法正常卸载 Office。")

    # Data.xml「苹果iTunes备份」(L0)
    add("itunes_backup", "苹果 iTunes 备份",
        "苹果设备的备份文件（通讯录、照片、视频等）。",
        GROUP_APP, 0, [
            T(U + r"\AppData\Roaming\Apple Computer\MobileSync\Backup"),
        ], warning="如果你近期需要用电脑恢复苹果设备，请勿勾选此项目！")

    # ================= 备份文件 =================
    # Data.xml「Boot备份信息」(L3)
    add("boot_backup", "Boot 备份信息",
        "修改启动信息后系统创建的备份（Windows\\pss），确认不需要还原"
        "即可删除。",
        GROUP_BACKUP, 3, [
            T(r"%SystemRoot%", "pss", mode="entries"),
        ], admin=True)

    # Data.xml「硬链接备份」(L2)
    add("hardlink_backup", "硬链接备份",
        "硬链接合并功能留下的备份文件（$HardLinkBackup）。",
        GROUP_BACKUP, 2, [
            T(r"%SystemDrive%", "$HardLinkBackup", mode="entries"),
        ], admin=True, warning="删除后硬链接合并操作将无法回滚！")

    # Data.xml「最后一次正确配置」（by Stanlazy）(L2)
    add("lastgood", "最后一次正确配置",
        "最近能正常启动计算机的配置备份（Windows\\lastgood）。",
        GROUP_BACKUP, 2, [
            T(r"%SystemRoot%", "lastgood", "lastgood.tmp", mode="entries"),
        ], admin=True)

    # Data.xml「会声会影素材存档」(L2)
    add("corel_proxy", "会声会影素材存档",
        "会声会影使用过程中产生的素材代理存档。",
        GROUP_BACKUP, 2, [
            T(r"%Documents%\Corel VideoStudio Pro\*\SmartProxy"),
        ], warning="请确保所有编辑的视频已经保存！")

    return rules


# 需要在删除时特殊处理（走系统 API 而非直接删目录）的条目
_SPECIALS = {"recycle", "dism_component", "wu_records", "do_cache",
             "win_events", "sr_points"}

# 这些规则的文件可能被 explorer.exe 持有：只有确实删不掉时才重启
# 资源管理器重试（与 Dism++ 缩略图条目的 ExplorerNotify 行为一致但更保守）
_NEEDS_EXPLORER_RESTART = {"thumbnail_cache"}


def may_restart_explorer(rule) -> bool:
    """该规则是否**可能**触发资源管理器重启（供 UI 提前提示用户）。"""
    key = getattr(rule, "key", rule)
    return key in _NEEDS_EXPLORER_RESTART


# ---------------------------------------------------------------------------
# 资源管理器重启（逻辑与旧版一致，踩坑记录见 git 历史）
# ---------------------------------------------------------------------------
def _explorer_running() -> bool:
    if not IS_WIN:
        return False
    try:
        TH32CS_SNAPPROCESS = 0x00000002
        INVALID = ctypes.c_void_p(-1).value
        k32 = ctypes.windll.kernel32

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_char * 260),
            ]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID:
            return False
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if not k32.Process32First(snap, ctypes.byref(entry)):
                return False
            while True:
                if entry.szExeFile.decode("ascii", "ignore").lower() == "explorer.exe":
                    return True
                if not k32.Process32Next(snap, ctypes.byref(entry)):
                    break
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return False


def _wait_for(predicate, timeout, interval=0.15) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


_restart_in_progress = False
_atexit_registered = False


def _emergency_restore_explorer():
    if not IS_WIN:
        return
    global _restart_in_progress
    if not _restart_in_progress:
        return
    _restart_in_progress = False
    if _explorer_running():
        return
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)
        subprocess.Popen(["explorer.exe"], creationflags=flags)
    except Exception:
        pass


def _ensure_atexit_hook():
    global _atexit_registered
    if _atexit_registered:
        return
    _atexit_registered = True
    try:
        import atexit
        atexit.register(_emergency_restore_explorer)
    except Exception:
        pass


def restart_explorer(wait=3.0, restore_timeout=8.0) -> bool:
    """重启资源管理器以释放缩略图/图标缓存的文件占用。

    关键点：taskkill 返回 ≠ 进程已退出，必须等它真正消失；等系统自动
    重启（AutoRestartShell）失败才手动拉起；返回前复核，并注册退出钩子
    兜底，保证返回时（或进程意外终止时）explorer 一定在运行。
    """
    if not IS_WIN:
        return False
    global _restart_in_progress
    flags = getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)
    _ensure_atexit_hook()

    _restart_in_progress = True
    try:
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "explorer.exe"],
                capture_output=True, timeout=15, creationflags=flags,
            )
        except Exception:
            pass

        _wait_for(lambda: not _explorer_running(), timeout=5.0)

        if not _wait_for(_explorer_running, timeout=max(1.0, wait)):
            try:
                subprocess.Popen(["explorer.exe"], creationflags=flags)
            except Exception:
                pass
            _wait_for(_explorer_running, timeout=restore_timeout)

        alive = _explorer_running()
        if not alive:
            try:
                subprocess.Popen(["explorer.exe"], creationflags=flags)
            except Exception:
                pass
            alive = _wait_for(_explorer_running, timeout=5.0)
        return alive
    finally:
        _restart_in_progress = False


# ---------------------------------------------------------------------------
# 目标展开与扫描
# ---------------------------------------------------------------------------
def rule_targets(rule: CleanRule) -> list[str]:
    """实时展开某条规则在本机上的实际根路径列表（供 UI/调试）。"""
    out = []
    for tgt in rule.targets:
        out.extend(_expand_root(tgt.root))
    return out


def _resolve_entries(tgt: Target) -> list[str]:
    """entries 模式：展开 root 并按 query 匹配出全部待删条目。

    动态排除项（?GetFileVersion / ?GetIniData 等）求值失败（返回空）时，
    **整根跳过**——这说明我们无法识别"当前版本"，此时宁可全不删，
    也不能把不该删的当老版本删掉。
    """
    out = []
    for root_dir in _expand_root(tgt.root):
        dyn = []
        skip_root = False
        for ex in tgt.excluded:
            if not ex.startswith("?"):
                continue
            v = _eval_expr(ex, root_ctx=root_dir + "\\")
            if not v:
                skip_root = True
                break
            dyn.append(v)
        if skip_root:
            continue
        out.extend(_iter_matches(root_dir, tgt, dyn))
    return out


def _scan_target(tgt: Target, cancel=None) -> tuple[int, int]:
    """统计一个 Target 的体积。返回 (字节, 条目数)。"""
    if tgt.mode == "contents":
        total = 0
        n = 0
        for root_dir in _expand_root(tgt.root):
            try:
                entries = os.listdir(root_dir)
            except OSError:
                continue
            for name in entries:
                if cancel and cancel():
                    break
                total += measure(os.path.join(root_dir, name), cancel=cancel)
                n += 1
        return total, n
    # entries
    total = 0
    n = 0
    for p in _resolve_entries(tgt):
        if cancel and cancel():
            break
        total += measure(p, cancel=cancel)
        n += 1
    return total, n


def scan_rule(rule: CleanRule, cancel: Callable[[], bool] | None = None) -> int:
    """统计某条规则的体积。返回字节数。只读，不做任何删除。"""
    if cancel and cancel():
        return 0
    if rule.special == "recycle":
        total = _recycle_bin_size()
        rule.size = total
        rule.available = total > 0
        rule.file_count = 0
        rule.scanned = True
        return total
    if rule.special == "dism_component":
        total = _dism_component_scan()
        rule.size = total
        rule.available = total > 0
        rule.scanned = True
        return total
    if rule.special == "sr_points":
        total = _sr_points_scan()
        rule.size = total
        rule.available = total > 0
        rule.scanned = True
        return total
    total = 0
    count = 0
    for tgt in rule.targets:
        if cancel and cancel():
            break
        t, n = _scan_target(tgt, cancel=cancel)
        total += t
        count += n
    rule.size = total
    rule.file_count = count
    rule.available = total > 0 or count > 0
    rule.scanned = True
    return total


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
@dataclass
class CleanResult:
    key: str
    name: str
    freed: int = 0            # 结论口径：本次实际释放的体积
    estimated: int = 0        # 清理前的扫描体积
    removed_bytes: int = 0    # 真实被删除对象的体积合计
    removed_count: int = 0    # 真实被删除的对象个数
    skipped: int = 0
    failed: int = 0
    disk_delta: int = 0       # 磁盘可用空间读数变化（仅诊断参考）
    verified: bool = True     # False = 数字来自估算（如 DISM 清理），非逐项计数
    messages: list = field(default_factory=list)


def _rm_path(path: str) -> bool:
    """彻底删除一个文件/目录。删完复查存在性（防静默拦截）。"""
    try:
        if os.path.islink(path) or os.path.isfile(path):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
            try:
                os.remove(path)
            except OSError:
                return False
            return not os.path.exists(path)
        if os.path.isdir(path):
            def _onerr(func, p, exc):
                try:
                    os.chmod(p, 0o700)
                    func(p)
                except Exception:
                    pass
            shutil.rmtree(path, onerror=_onerr)
            return not os.path.exists(path)
    except Exception:
        return False
    return False


def _rm_entry(path: str) -> tuple[bool, int]:
    """删除一个条目，返回 (是否成功, 被删对象的字节体积)。

    体积必须在**删除前**测量——删完就问不出大小了。这是「已清理 N MB」
    能给出真实数字（而不是靠扫描预估或磁盘读数）的关键。
    """
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            size = os.path.getsize(path)
        elif os.path.isdir(path):
            size = measure(path)
        else:
            size = 0
    except OSError:
        size = 0
    return _rm_path(path), size


def _empty_dir_stats(path: str, cancel=None) -> tuple[int, int, int]:
    """清空目录内容（保留目录本身），返回 (成功数, 失败数, 被删字节)。"""
    ok = fail = 0
    total = 0
    try:
        entries = os.listdir(path)
    except OSError:
        return 0, 1, 0
    for name in entries:
        if cancel and cancel():
            break
        child = os.path.join(path, name)
        success, size = _rm_entry(child)
        if success:
            ok += 1
            total += size
        else:
            fail += 1
    return ok, fail, total


def _clean_target(tgt: Target, cancel=None, progress=None) -> tuple[int, int, int]:
    """执行一个 Target 的删除。返回 (成功数, 失败数, 被删字节)。"""
    ok = fail = volume = 0
    if tgt.mode == "contents":
        for root_dir in _expand_root(tgt.root):
            if cancel and cancel():
                break
            if progress:
                progress(root_dir)
            if os.path.normpath(root_dir).lower() in _critical_roots():
                continue                      # 安全护栏：绝不清空关键目录
            o, f, v = _empty_dir_stats(root_dir, cancel=cancel)
            ok += o
            fail += f
            volume += v
    else:
        for p in _resolve_entries(tgt):
            if cancel and cancel():
                break
            if progress:
                progress(os.path.basename(p.rstrip("\\/")) or p)
            success, size = _rm_entry(p)
            if success:
                ok += 1
                volume += size
            else:
                fail += 1
    return ok, fail, volume


# ---------------------------------------------------------------------------
# 特殊条目实现（对应 Data.xml 的 Custom 过程）
# ---------------------------------------------------------------------------
def _run_ps(command: str, timeout=120):
    """跑一条 PowerShell（无窗口），返回 CompletedProcess 或 None。"""
    if not IS_WIN:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, timeout=timeout, creationflags=flags,
        )
    except Exception:
        return None


def _recycle_bin_size() -> int:
    """所有固定磁盘回收站的体积。"""
    total = 0
    for root in fixed_drives():
        rb = os.path.join(root, "$Recycle.Bin")
        if os.path.isdir(rb):
            total += measure(rb)
    return total


def _clear_recycle_bin() -> tuple[int, int, int]:
    """清空所有磁盘回收站。返回 (成功数, 失败数, 被删体积)。"""
    before = _recycle_bin_size()
    _run_ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue", timeout=120)
    ok = fail = moved = 0
    for root in fixed_drives():
        rb = os.path.join(root, "$Recycle.Bin")
        if os.path.isdir(rb):
            for name in os.listdir(rb):
                sid = os.path.join(rb, name)
                if os.path.isdir(sid):
                    o, f, v = _empty_dir_stats(sid)
                    ok += o
                    fail += f
                    moved += v
    after = _recycle_bin_size()
    volume = moved if moved > 0 else max(0, before - after)
    return ok, fail, volume


def _dism_output_text(raw: bytes) -> str:
    """DISM 控制台输出 → 文本。

    DISM 输出跟随系统 OEM 代码页（中文系统 GBK、西文系统 CP437/850…），
    **不能**按 UTF-8 严格解码——中文 Windows 上会直接抛 UnicodeDecodeError，
    症状是「WinSxS 组件」这一项在任何中文系统上都扫出 0。这里按
    utf-8 → gbk → latin-1 依次降级，全部 errors=ignore；数字与单位是
    ASCII，任何代码页下都不会丢。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _parse_dism_size(text: str) -> int:
    """从 AnalyzeComponentStore 输出里取「实际大小」。

    输出里有多个体积数字（组件存储总大小 / 实际大小 / 与 Windows 共享 /
    备份和已禁用功能 / 缓存和临时数据），第一个是**总大小**而非可回收量，
    直接取第一个会严重虚高。优先匹配「实际大小 / Actual Size」那一行的
    数字；匹配不到再退回第一个体积（作为上限估算并如实告知）。
    """
    candidates = re.findall(r"(\d+(?:\.\d+)?)\s*(GB|MB|KB)", text, re.IGNORECASE)
    if not candidates:
        return 0

    def _to_bytes(val_s, unit_s):
        mult = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[unit_s.upper()]
        try:
            return int(float(val_s) * mult)
        except ValueError:
            return 0

    # 找「实际大小」关键词所在行里的体积（中英文系统都认）
    for line in text.splitlines():
        if re.search(r"实际大小|Actual\s+Size", line, re.IGNORECASE):
            m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB|KB)", line, re.IGNORECASE)
            if m:
                return _to_bytes(m.group(1), m.group(2))
    return _to_bytes(*candidates[0])


#: DISM 分析结果缓存（TTL 秒）。`Dism /AnalyzeComponentStore` 单项要
#: 几十秒到两分钟，而"重新扫描"在界面上是很常见的动作——同一台机器上
#: 10 分钟内组件存储的状态不会变，没必要每次都重新分析一遍。
_DISM_CACHE = {"t": 0.0, "bytes": 0, "key": ""}
_DISM_CACHE_TTL = 600.0


def _dism_component_scan(force=False) -> int:
    """「被取代的WinSxS组件」：用 DISM /AnalyzeComponentStore 估算。

    注意：DISM 不会给出"可回收多少"的精确值，这里取的是组件存储
    **实际大小**作为上限估算；真正的释放量以清理完成后系统实际表现为准
    （该条目的 CleanResult.verified = False）。

    结果带 10 分钟缓存：这是整个扫描里唯一的"外部命令 + 长耗时"项，
    重复扫描（用户点「重新扫描」）不该重复付这个代价。
    """
    now = time.time()
    if not force and _DISM_CACHE["t"] and \
            now - _DISM_CACHE["t"] < _DISM_CACHE_TTL:
        return _DISM_CACHE["bytes"]
    dism = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", "Dism.exe")
    if not os.path.isfile(dism):
        return 0
    try:
        r = subprocess.run(
            [dism, "/Online", "/Cleanup-Image", "/AnalyzeComponentStore"],
            capture_output=True, timeout=420,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW),
        )
    except Exception:
        return 0
    val = _parse_dism_size(_dism_output_text(r.stdout or b""))
    if val > 0:
        _DISM_CACHE.update({"t": now, "bytes": val})
    return val


def _dism_component_clean(res: CleanResult, cancel=None, progress=None):
    """DISM 组件清理（对应 DismComponentCleanup，不带 /ResetBase）。"""
    dism = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", "Dism.exe")
    if not os.path.isfile(dism):
        res.messages.append("本机没有 DISM，无法执行")
        res.failed = 1
        return
    if progress:
        progress("DISM 组件清理（可能持续数分钟到几十分钟）")
    try:
        r = subprocess.run(
            [dism, "/Online", "/Cleanup-Image", "/StartComponentCleanup"],
            capture_output=True, timeout=7200,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW),
        )
        if r.returncode == 0:
            res.freed = res.estimated
            res.removed_bytes = res.estimated
            res.verified = False      # DISM 不会逐项报告，按分析估算
            res.messages.append("DISM 组件清理完成，释放量按分析值估算")
        else:
            res.failed = 1
            res.messages.append(f"DISM 返回码 {r.returncode}，清理未完成")
    except subprocess.TimeoutExpired:
        res.failed = 1
        res.messages.append("DISM 清理超时（>2 小时），已中止等待")
    except Exception as exc:
        res.failed = 1
        res.messages.append(f"异常：{exc}")


def _stop_service(names, start=False):
    for n in names:
        try:
            subprocess.run(
                ["net", "start" if start else "stop", n, "/y"],
                capture_output=True, timeout=90,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW),
            )
        except Exception:
            pass


def _clean_wu_records(res: CleanResult, cancel=None, progress=None):
    """「Windows更新安装记录」：停 wuauserv/bits → 清 DataStore → 再启动。"""
    if progress:
        progress("停止 Windows 更新服务")
    _stop_service(["wuauserv", "bits"])
    try:
        ok = fail = volume = 0
        for root_dir in _expand_root(r"%SystemRoot%\SoftwareDistribution\DataStore"):
            o, f, v = _empty_dir_stats(root_dir, cancel=cancel)
            ok += o
            fail += f
            volume += v
        res.removed_count = ok
        res.removed_bytes = volume
        res.failed = fail
    finally:
        if progress:
            progress("恢复 Windows 更新服务")
        _stop_service(["bits", "wuauserv"], start=True)
    if res.failed:
        res.messages.append(f"{res.failed} 项未能删除")


def _clean_do_cache(res: CleanResult, cancel=None, progress=None):
    """「传递优化缓存」：优先系统 API，失败回退直接删目录。"""
    if progress:
        progress("调用系统传递优化清理")
    _run_ps("Delete-DeliveryOptimizationCache -Force -ErrorAction SilentlyContinue",
            timeout=180)
    ok = fail = volume = 0
    for tgt in DO_CACHE_TARGETS:
        o, f, v = _clean_target(tgt, cancel=cancel, progress=progress)
        ok += o
        fail += f
        volume += v
    res.removed_count = ok
    res.removed_bytes = volume
    res.failed = fail


# 传递优化缓存的目录（clean 复用；scan 走规则 targets，两处保持一致）
DO_CACHE_TARGETS = [
    T(r"%SystemRoot%\SoftwareDistribution\DeliveryOptimization"),
    T(r"%SystemRoot%\DeliveryOptimization"),
    T(r"%SystemRoot%\ServiceProfiles\NetworkService\AppData\Local\Microsoft"
      r"\Windows\DeliveryOptimization"),
]


def _clean_win_events(res: CleanResult, cancel=None, progress=None):
    """「Windows事件」：wevtutil cl 清全部事件日志 + 清 winevt\\Logs 内容。"""
    if progress:
        progress("清空事件日志（wevtutil）")
    r = _run_ps(
        "wevtutil el | ForEach-Object { wevtutil cl $_ 2>$null }",
        timeout=300,
    )
    ok = fail = volume = 0
    for root_dir in _expand_root(r"%SystemRoot%\System32\winevt\Logs"):
        o, f, v = _empty_dir_stats(root_dir, cancel=cancel)
        ok += o
        fail += f
        volume += v
    res.removed_count = ok
    res.removed_bytes = volume
    res.failed = fail
    if r is None or (hasattr(r, "returncode") and r.returncode != 0):
        res.messages.append("部分事件日志被服务占用，未能清空")


def _sr_points_scan() -> int:
    """「系统还原点」：vssadmin list shadowstorage 解析已用空间。"""
    if not IS_WIN:
        return 0
    try:
        r = subprocess.run(
            ["vssadmin", "list", "shadowstorage"],
            capture_output=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW),
        )
        text = (r.stdout or b"").decode("gbk", "ignore")
    except Exception:
        return 0
    total = 0
    # 形如「已用空间: 1.23 GB (1,320,124,416 字节)」/「Used: 1.23 GB (1320124416 bytes)」
    for m in re.finditer(r"(\d[\d,\.]*)\s*字节|(\d[\d,\.]*)\s*bytes", text, re.IGNORECASE):
        raw = (m.group(1) or m.group(2)).replace(",", "")
        try:
            total += int(float(raw))
        except ValueError:
            continue
    return total


def _clean_sr_points(res: CleanResult, cancel=None, progress=None):
    """删全部卷影副本，再尝试建一个新还原点（对应 NCSRPointOnlineCleanup）。"""
    if progress:
        progress("删除全部还原点")
    try:
        subprocess.run(
            ["vssadmin", "delete", "shadows", "/all", "/quiet"],
            capture_output=True, timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW),
        )
        res.verified = False
        res.freed = res.estimated
        res.removed_bytes = res.estimated
        res.messages.append("已删除全部还原点，释放量按扫描值估算")
    except Exception as exc:
        res.failed = 1
        res.messages.append(f"删除还原点失败：{exc}")
        return
    if progress:
        progress("创建新的还原点")
    r = _run_ps(
        'Checkpoint-Computer -Description "Yuhub 清理" '
        "-RestorePointType MODIFY_SETTINGS -ErrorAction SilentlyContinue",
        timeout=300,
    )
    if r is not None and r.returncode == 0:
        res.messages.append("已创建新的还原点")
    else:
        res.messages.append("未能创建新还原点（系统还原可能未开启或当天已建过）")


_SPECIAL_CLEAN = {
    "recycle": None,           # 走专门分支（体积统计口径不同）
    "dism_component": _dism_component_clean,
    "wu_records": _clean_wu_records,
    "do_cache": _clean_do_cache,
    "win_events": _clean_win_events,
    "sr_points": _clean_sr_points,
}


def clean_rule(rule: CleanRule, cancel: Callable[[], bool] | None = None,
               progress: Callable[[str], None] | None = None) -> CleanResult:
    """执行一条规则的清理。返回结果统计。

    「释放了多少」的口径：以**真实被删除对象的体积合计**为准（删前测量、
    删后复查）。部分特殊条目（DISM / 还原点）无法逐项计数，改用扫描估算
    并把 verified 置 False；磁盘读数变化放 disk_delta 仅作诊断。
    """
    res = CleanResult(key=rule.key, name=rule.name)
    if not rule.scanned:
        try:
            scan_rule(rule, cancel=cancel)
        except Exception:
            pass
    res.estimated = rule.size
    drive = drive_of(os.environ.get("SystemDrive", "C:") + "\\")
    before = drive_space(drive)["free_bytes"]

    if cancel and cancel():
        return res

    if rule.special == "recycle":
        ok, fail, volume = _clear_recycle_bin()
        res.removed_count = ok
        res.removed_bytes = volume
        res.failed = fail
        res.messages.append(f"清空 {ok} 项，失败 {fail} 项")
    elif rule.special in _SPECIAL_CLEAN and _SPECIAL_CLEAN[rule.special]:
        try:
            _SPECIAL_CLEAN[rule.special](res, cancel=cancel, progress=progress)
        except Exception as exc:
            res.failed += 1
            res.messages.append(f"异常：{exc}")
    else:
        count, fail, volume = _clean_targets(rule, cancel, progress)

        # 缩略图/图标缓存可能被 explorer.exe 持有：只有确实删不掉时才重启
        if fail > 0 and rule.key in _NEEDS_EXPLORER_RESTART:
            if progress:
                progress("重启资源管理器")
            if restart_explorer():
                res.messages.append("已重启资源管理器释放占用后重试")
                c2, f2, v2 = _clean_targets(rule, cancel, progress)
                count += c2
                volume += v2
                fail = f2
            else:
                res.messages.append("资源管理器未能重启，部分文件仍被占用")

        res.removed_count = count
        res.removed_bytes = volume
        res.failed = fail
        if fail:
            res.messages.append(f"{fail} 项被占用或权限不足，未能删除")

    after = drive_space(drive)["free_bytes"]
    res.disk_delta = after - before

    res.freed = res.removed_bytes
    if res.removed_bytes == 0 and res.estimated > 0 and res.removed_count == 0 \
            and not res.verified:
        # 特殊条目失败（DISM 出错等）：如实报告
        res.freed = 0
    if res.removed_bytes == 0 and res.estimated > 0 and res.removed_count == 0 \
            and res.verified and rule.special not in _SPECIALS:
        res.verified = False
        if not any("失败" in m or "占用" in m for m in res.messages):
            res.messages.append("未能删除任何对象（可能被占用或权限不足）")
    return res


def _clean_targets(rule: CleanRule, cancel=None, progress=None):
    """删除一条规则的所有 Target，返回 (成功数, 失败数, 被删字节)。"""
    count = fail = volume = 0
    for tgt in rule.targets:
        if cancel and cancel():
            break
        ok, f, v = _clean_target(tgt, cancel=cancel, progress=progress)
        count += ok
        fail += f
        volume += v
    return count, fail, volume


# ---------------------------------------------------------------------------
# 后台扫描器 / 清理器（接口与旧版一致）
# ---------------------------------------------------------------------------
class CleanerScanner:
    """后台扫描所有规则（**并行** + 慢条目排最后）。

    两条提速措施（2026-10-11，用户反馈"扫描太慢"）：

      ① **并行**：扫描全程只读（os.scandir + getsize），是典型 I/O 密集，
         开 4~6 条线程能把墙钟时间压到 1/2~1/4。进度回调可能在**非 GUI
         线程**里被调用 —— 调用方（页面）一直是用 Qt 信号转发的，天然安全。
      ② **慢条目排最后**：`Dism /AnalyzeComponentStore` 单项就要几十秒到
         两分钟。把它放到最后，并用 `on_fast_done` 在"除它以外全部扫完"时
         就通知界面（按钮解禁、结果可清），用户不用盯着一个卡住的进度条
         等 DISM 分析完。
    """

    #: 并行度。取 4~6：SSD 上收益明显，机械盘上也不会把磁头抖成随机读。
    WORKERS = 6

    def __init__(self):
        self._thread = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, rules, on_progress, on_done, on_fast_done=None):
        if self.running:
            return False
        self._cancel.clear()

        def _run():
            total = 0
            try:
                fast = [r for r in rules if not getattr(r, "slow_scan", False)]
                slow = [r for r in rules if getattr(r, "slow_scan", False)]
                if fast:
                    from concurrent.futures import ThreadPoolExecutor
                    workers = max(1, min(self.WORKERS, len(fast)))
                    with ThreadPoolExecutor(max_workers=workers,
                                            thread_name_prefix="YuhubScan") as pool:
                        futs = [pool.submit(scan_rule, r, self._cancel.is_set)
                                for r in fast]
                        for fut, r in zip(futs, fast):
                            try:
                                fut.result()
                            except Exception:
                                pass
                            total += r.size
                            try:
                                on_progress(r)
                            except Exception:
                                pass
                if on_fast_done is not None:
                    try:
                        on_fast_done(total, rules)
                    except Exception:
                        pass
                for r in slow:
                    if self._cancel.is_set():
                        break
                    try:
                        scan_rule(r, cancel=self._cancel.is_set)
                    except Exception:
                        pass
                    total += r.size
                    try:
                        on_progress(r)
                    except Exception:
                        pass
            finally:
                with self._lock:
                    self._thread = None
                try:
                    on_done(total, rules)
                except Exception:
                    pass

        with self._lock:
            self._thread = threading.Thread(target=_run, name="YuhubCleanerScan",
                                            daemon=True)
            self._thread.start()
        return True

    def cancel(self):
        self._cancel.set()


class CleanerWorker:
    """后台线程执行清理（逐条规则，可取消）。"""

    def __init__(self):
        self._thread = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, rules, on_rule_done, on_done, on_status=None):
        if self.running:
            return False
        self._cancel.clear()

        def _run():
            results = []
            try:
                for r in rules:
                    if self._cancel.is_set():
                        break
                    try:
                        res = clean_rule(
                            r, cancel=self._cancel.is_set,
                            progress=(lambda t, _r=r: on_status(_r, t)) if on_status else None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        res = CleanResult(key=r.key, name=r.name)
                        res.messages.append(f"异常：{exc}")
                    results.append(res)
                    try:
                        on_rule_done(res)
                    except Exception:
                        pass
            finally:
                with self._lock:
                    self._thread = None
                try:
                    on_done(results)
                except Exception:
                    pass

        with self._lock:
            self._thread = threading.Thread(target=_run, name="YuhubCleanerWorker",
                                            daemon=True)
            self._thread.start()
        return True

    def cancel(self):
        self._cancel.set()
