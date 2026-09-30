"""Yuhub 空间清理引擎。

规则设计参考 Dism++ 的 Data.xml（github.com/Chuyu-Team/Dism-Multi-language），
按「系统临时文件 / 更新与日志 / 显卡缓存 / 驱动与安装残留」分组，支持逐项勾选。

核心原则：
  1. **先扫描后清理**——扫描只统计体积，不做任何删除。
  2. **删到回收站还是彻底删**——本模块一律**彻底删除**（与 Dism++ 一致），
     因为清理目标本身就是缓存/临时文件；但调用方必须先让用户二次确认。
  3. **不碰用户数据**——只清理明确的缓存/临时目录，绝不递归删除用户文档、桌面、下载等。
  4. **权限不足要优雅降级**——无管理员权限时跳过需要提权的项，其余照常执行，不整段失败。
  5. **复用 DISM++ 的经验**：删除后必须用磁盘可用空间前后对比来验收，
     不能只看文件是否消失（很多路径会被重定向到回收站）。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

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


def _env_path(raw):
    """展开 %VAR% 与 ?GetRegSz(...) 之类的路径写法。"""
    if not raw:
        return ""
    # Dism++ 的注册表取值写法 → 这里只支持最常见的 Machine TEMP
    if raw.startswith("?GetRegSz("):
        inner = raw[len("?GetRegSz("):].rstrip(")")
        try:
            import winreg
            hive_name, sub, value = inner.split("\\", 1)
            hive = {
                "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
                "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
            }.get(hive_name)
            if hive is None:
                return ""
            sub, value = sub.rsplit(",", 1)
            with winreg.OpenKey(hive, sub) as k:
                return str(winreg.QueryValueEx(k, value)[0])
        except Exception:
            return ""
    return os.path.expandvars(raw)


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
    """当前进程是否有管理员权限（UAC 过滤令牌下即使属 Administrators 组也会返回 False）。"""
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def measure(path, max_seconds=2.5) -> int:
    """递归统计目录/文件体积（字节）。带超时保护，避免卡在超大目录上。

    统计失败（无权限 / 不存在）返回 0，不抛异常。
    """
    total = 0
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        if not os.path.isdir(path):
            return 0
        # 用 scandir 递归：比 os.walk 快，且能拿到目录项类型
        stack = [path]
        deadline = time.monotonic() + max_seconds
        while stack:
            if time.monotonic() > deadline:
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
# 规则定义
# ---------------------------------------------------------------------------
@dataclass
class CleanTarget:
    """一条可清理的具体路径（展开通配后的实例）。"""

    path: str
    kind: str = "dir"        # dir | file | glob
    note: str = ""


@dataclass
class CleanRule:
    """一条可勾选的清理项。"""

    key: str
    name: str
    desc: str
    group: str
    level: int = 2           # 0=有风险/需谨慎, 1=轻微风险, 2=安全, 3=非常安全
    admin: bool = False      # 是否需要管理员
    targets: list = field(default_factory=list)   # list[CleanTarget] 或可调用对象
    # 运行期状态
    size: int = 0
    file_count: int = 0
    scanned: bool = False
    available: bool = False   # 本机上是否存在

    @property
    def risky(self):
        return self.level <= 1


def _expand_glob(pattern: str) -> list[str]:
    """展开含通配符的路径（Dism++ 的 RootPath + Query 写法）。"""
    import glob as _glob
    return [p for p in _glob.glob(pattern) if os.path.exists(p)]


def _users_sub(rel: str) -> list[str]:
    """展开 `%SystemDrive%\\Users\\*\\<rel>` 这类"所有用户"路径。"""
    out = []
    sysdrive = os.environ.get("SystemDrive", "C:")
    base = os.path.join(sysdrive + "\\", "Users")
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name, rel)
            if os.path.exists(p):
                out.append(p)
    except OSError:
        pass
    return out


def _safe_dir(*parts):
    """拼一个目录，若存在就返回（单元素列表），否则空列表。"""
    p = os.path.join(*[x for x in parts if x])
    return [p] if p and os.path.isdir(p) else []


def _safe_any(p):
    return [p] if p and os.path.exists(p) else []


# ---------------------------------------------------------------------------
# 规则集合（分组）
# ---------------------------------------------------------------------------
GROUP_TEMP = "临时文件"
GROUP_CACHE = "缓存文件"
GROUP_SYSTEM = "系统与更新"
GROUP_GPU = "显卡缓存"
GROUP_DRIVER = "驱动与安装残留"


def build_rules() -> list[CleanRule]:
    """构建全部清理规则。路径写法参考 Dism++ Data.xml。"""
    LA = os.environ.get("LOCALAPPDATA", "")
    WIN = os.environ.get("SystemRoot", r"C:\Windows")
    PROG = os.environ.get("ProgramData", r"C:\ProgramData")
    PF = os.environ.get("ProgramFiles", r"C:\Program Files")
    PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    TEMP = os.environ.get("TEMP", "")
    sysdrive = os.environ.get("SystemDrive", "C:")

    rules: list[CleanRule] = []

    def add(key, name, desc, group, level, fn, admin=False):
        rules.append(CleanRule(key=key, name=name, desc=desc, group=group,
                               level=level, admin=admin, targets=fn))

    # ---------------- 临时文件 ----------------
    add("user_temp", "用户临时文件",
        "程序运行时产生的临时文件，位于当前用户 Temp 目录。",
        GROUP_TEMP, 3,
        lambda: _safe_dir(TEMP) if TEMP else [])

    add("system_temp", "系统临时文件",
        "Windows 与系统服务使用的临时目录（部分需管理员）。",
        GROUP_TEMP, 3, lambda: (
            _safe_dir(WIN, "Temp")
            + _safe_dir(WIN, "ServiceProfiles", "NetworkService", "AppData", "Local", "Temp")
            + _safe_dir(WIN, "ServiceProfiles", "LocalService", "AppData", "Local", "Temp")
            + _safe_dir(WIN, "System32", "config", "systemprofile", "AppData", "Local", "Temp")
            + _safe_dir(WIN, "SysWOW64", "config", "systemprofile", "AppData", "Local", "Temp")
        ), admin=True)

    add("wininet_cache", "系统网页缓存",
        "Windows 系统级网页缓存（WinINet）与 UWP 应用缓存。",
        GROUP_TEMP, 3, lambda: (
            _users_sub(r"AppData\Local\Microsoft\Windows\INetCache")
            + _users_sub(r"AppData\Local\Microsoft\Windows\Temporary Internet Files")
            + _safe_dir(WIN, "System32", "config", "systemprofile", "AppData", "Local",
                        "Microsoft", "Windows", "INetCache")
        ), admin=True)

    add("recycle_bin", "回收站",
        "清空回收站中已删除的文件（不可恢复）。",
        GROUP_TEMP, 3, lambda: [])

    add("crash_dumps", "崩溃转储文件",
        "程序崩溃时生成的 dmp 文件，以及系统内存转储。",
        GROUP_CACHE, 2, lambda: (
            _users_sub(r"AppData\Local\CrashDumps")
            + _safe_any(os.path.join(WIN, "MEMORY.DMP"))
            + _safe_dir(WIN, "Minidump")
        ))

    add("temp_install", "系统临时安装文件",
        "系统升级/安装过程中留下的临时目录（$Windows.~BT 等）。",
        GROUP_SYSTEM, 2, lambda: (
            _safe_dir(sysdrive + "\\", "$Windows.~BT")
            + _safe_dir(sysdrive + "\\", "$Windows.~WS")
            + _safe_dir(sysdrive + "\\", "$Windows.~LS")
        ), admin=True)

    # ---------------- 缓存文件 ----------------
    add("thumbnail_cache", "缩略图缓存",
        "资源管理器的缩略图与图标缓存。个别文件被占用时会自动重启资源管理器释放"
        "（任务栏会短暂消失一下）。",
        GROUP_CACHE, 3, lambda: (
            _safe_dir(LA, "Microsoft", "Windows", "Explorer")
            + _safe_any(os.path.join(LA, "IconCache.db"))
        ))

    add("wer_reports", "Windows 错误报告",
        "系统与程序的错误报告归档（WER）。",
        GROUP_CACHE, 2, lambda: (
            _users_sub(r"AppData\Local\Microsoft\Windows\WER")
            + _safe_dir(PROG, "Microsoft", "Windows", "WER")
            + _safe_dir(WIN, "LiveKernelReports")
        ), admin=True)

    add("font_cache", "字体缓存",
        "系统字体缓存，清理后首次开机稍慢。",
        GROUP_CACHE, 2, lambda: _safe_dir(WIN, "ServiceProfiles",
                                          "LocalService", "AppData", "Local",
                                          "FontCache"), admin=True)

    add("xde_cache", "XDE 缓存文件",
        "Windows 部署/仿真相关缓存。",
        GROUP_CACHE, 2, lambda: (
            _safe_dir(PROG, "Microsoft", "XDE")
            + _users_sub(r"AppData\Local\Microsoft\XDE")
        ), admin=True)

    add("pdb_cache", "调试符号缓存 (DBG)",
        "Visual Studio / 调试器下载的 PDB 符号缓存。",
        GROUP_CACHE, 2, lambda: (
            _safe_dir(LA, "DBG") + _safe_dir(LA, "Microsoft", "Symbols")
        ))

    add("temp_internet", "Internet 临时文件",
        "IE / 系统组件使用的临时网络文件目录。",
        GROUP_TEMP, 3, lambda: (
            _users_sub(r"AppData\Local\Microsoft\Windows\INetCache\IE")
        ))

    # ---------------- 系统与更新 ----------------
    add("win_update_cache", "Windows 更新缓存",
        "已下载并安装完的更新安装包，可安全删除（约 1~8 GB）。",
        GROUP_SYSTEM, 2, lambda: (
            _safe_dir(WIN, "SoftwareDistribution", "Download")
        ), admin=True)

    add("delivery_optimization", "传递优化缓存",
        "Windows 更新 P2P 分发缓存。",
        GROUP_SYSTEM, 3, lambda: (
            _safe_dir(WIN, "SoftwareDistribution", "DeliveryOptimization")
            + _safe_dir(WIN, "ServiceProfiles", "NetworkService", "AppData", "Local",
                        "Microsoft", "Windows", "DeliveryOptimization")
        ), admin=True)

    add("win_event_logs", "Windows 事件日志",
        "系统事件日志文件（evtx），清理后历史记录丢失。",
        GROUP_SYSTEM, 1, lambda: _safe_dir(WIN, "System32", "winevt", "Logs"),
        admin=True)

    add("defender_history", "Defender 保护历史",
        "Windows Defender 的扫描与查杀历史记录。",
        GROUP_SYSTEM, 2, lambda: _safe_dir(PROG, "Microsoft", "Windows Defender",
                                           "Scans", "History"), admin=True)

    add("delivery_downloads", "旧版 Windows 安装包",
        "Windows 升级后残留的旧系统文件（$Windows.~BT 需管理员）。",
        GROUP_SYSTEM, 2, lambda: _safe_dir(WIN, "SoftwareDistribution", "DataStore"),
        admin=True)

    # ---------------- 显卡缓存 ----------------
    add("gpu_nvidia_shader", "NVIDIA 着色器缓存",
        "NVIDIA 显卡的 DX/GL 着色器缓存，删除后游戏首次加载会重新编译（会自动重建）。",
        GROUP_GPU, 3, lambda: (
            _safe_dir(LA, "NVIDIA", "DXCache")
            + _safe_dir(LA, "NVIDIA", "GLCache")
            + _safe_dir(LA, "NVIDIA", "ComputeCache")
            + _safe_dir(LA, "NVIDIA Corporation", "NV_Cache")
        ))

    add("gpu_amd_shader", "AMD 着色器缓存",
        "AMD 显卡的 DX/GL/Vulkan 着色器缓存（会自动重建）。",
        GROUP_GPU, 3, lambda: (
            _safe_dir(LA, "AMD", "DxCache")
            + _safe_dir(LA, "AMD", "DxcCache")
            + _safe_dir(LA, "AMD", "GLCache")
            + _safe_dir(LA, "AMD", "VkCache")
            # Radeon Software / Adrenalin 的额外缓存与日志
            + _safe_dir(LA, "AMD", "CN")
            + _safe_dir(LA, "AMD", "DxCache", "shader_cache")
        ))

    add("gpu_amd_installer", "AMD 驱动安装源缓存",
        "AMD Radeon Software 下载/解压的驱动安装包与安装源（装完即可删）。",
        GROUP_DRIVER, 1, lambda: (
            _safe_dir(PROG, "AMD", "CIM", "Log")
            + _safe_dir(PROG, "AMD", "CN")
            + _safe_dir(LA, "AMD", "CN", "Store")
            + _expand_glob(os.path.join(sysdrive + "\\", "AMD", "Packages"))
        ), admin=True)

    add("gpu_intel_shader", "Intel 着色器缓存",
        "Intel 核显的着色器缓存（会自动重建）。",
        GROUP_GPU, 3, lambda: (
            _safe_dir(LA, "Intel", "ShaderCache")
            + _safe_dir(LA, "Intel", "Graphics")
        ))

    add("gpu_d3d_shader", "DirectX 通用着色器缓存",
        "Windows 自身的 D3D 着色器缓存，所有显卡共用（会自动重建）。",
        GROUP_GPU, 3, lambda: (
            _safe_dir(LA, "D3DSCache")
            + _users_sub(r"AppData\Local\D3DSCache")
        ))

    # ---------------- 驱动与安装残留 ----------------
    add("nvidia_installer", "NVIDIA 驱动安装源缓存",
        "NVIDIA 驱动安装时解压的安装源，装完即可删。",
        GROUP_DRIVER, 1, lambda: (
            _safe_dir(PF, "NVIDIA Corporation", "Installer2")
            + _users_sub(r"AppData\Local\NVIDIA\NvBackend")
        ), admin=True)

    add("nvidia_downloader", "NVIDIA 驱动下载包",
        "GeForce Experience 下载的驱动安装包。",
        GROUP_DRIVER, 1, lambda: (
            _safe_dir(PROG, "NVIDIA Corporation", "Downloader")
        ), admin=True)

    add("driver_extract", "驱动临时解压目录",
        "显卡驱动安装时在盘根目录留下的临时解压文件夹。",
        GROUP_DRIVER, 1, lambda: (
            _expand_glob(os.path.join(sysdrive + "\\", "NVIDIA"))
            + _expand_glob(os.path.join(sysdrive + "\\", "AMD"))
            + _expand_glob(os.path.join(sysdrive + "\\", "Intel"))
        ), admin=True)

    add("net_install_cache", "Microsoft .NET 安装缓存",
        "Visual Studio / .NET 的安装源缓存。",
        GROUP_DRIVER, 1, lambda: (
            _expand_glob(os.path.join(PF, "Microsoft.NET", "Multi-Targeting Pack", "*", "SetupCache"))
            + _expand_glob(os.path.join(PF86, "Microsoft.NET", "Multi-Targeting Pack", "*", "SetupCache"))
        ), admin=True)

    add("office_install_cache", "Office 安装源缓存",
        "Office 安装程序留下的源缓存（Program Files\\Microsoft Office\\...）。",
        GROUP_DRIVER, 1, lambda: (
            _safe_dir(PF, "Microsoft Office", "root", "Office16", "OfficeSetup")
            + _expand_glob(os.path.join(PF, "Microsoft Office", "Packages"))
        ), admin=True)

    add("visual_studio_cache", "Visual Studio 缓存",
        "VS 的临时跟踪与安装缓存。",
        GROUP_DRIVER, 1, lambda: (
            _safe_dir(LA, "Microsoft", "VisualStudio")
            + _safe_dir(PROG, "Microsoft", "VisualStudio", "Packages")
        ), admin=True)

    return rules


# 需要在删除时特殊处理（走系统 API 而非直接删目录）
_SPECIAL = {"recycle_bin"}

# 这些规则的文件**可能**被 explorer.exe 持有。注意：只有在删除确实失败时
# 才会重启资源管理器重试（重启会让任务栏短暂消失，不能无条件做），
# 与 Dism++ 的 <Activate Restart="Explorer"> 行为一致但更保守。
_NEEDS_EXPLORER_RESTART = {"thumbnail_cache"}


def may_restart_explorer(rule) -> bool:
    """该规则是否**可能**触发资源管理器重启（供 UI 提前提示用户）。"""
    key = getattr(rule, "key", rule)
    return key in _NEEDS_EXPLORER_RESTART


def _explorer_running() -> bool:
    """explorer.exe 是否在运行。

    用 ctypes 枚举进程快照，不解析命令输出——两个原因：
      1. 重启流程要在循环里反复探测，起子进程太浪费；
      2. `tasklist` 的输出编码随系统区域变化，文本匹配不可靠。
    """
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
                ("th32DefaultHeapID", ctypes.c_void_p),   # ULONG_PTR，64 位下 8 字节
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
    """轮询等待 predicate() 变成 True。返回是否在超时前满足。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


# 资源管理器重启期间的标志位：万一本进程在"explorer 已杀、还没回来"的窗口内
# 异常退出或被杀，靠这个标志在退出钩子里把它拉起来，避免留下没有任务栏的桌面。
_restart_in_progress = False
_atexit_registered = False


def _emergency_restore_explorer():
    """退出时兜底：如果重启流程没走完，确保 explorer 一定被拉起。"""
    if not IS_WIN:
        return
    global _restart_in_progress
    if not _restart_in_progress:
        return
    _restart_in_progress = False
    if _explorer_running():
        return
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
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

    **务必保证返回时 explorer 一定在运行**，否则用户会遇到"任务栏消失"。

    旧版实现在这里踩过坑：`taskkill /f` 返回只代表"杀进程的请求已发出"，
    并不代表进程已经退出。旧代码只 `sleep(0.6)` 就检查一次，此时那个正在
    退出的 explorer 仍会被 tasklist 列出来，于是**误判为"已自动重启"而跳过
    手动拉起**；等它真正退出后就再没人启动 shell 了——表现为任务栏消失，
    要按 Win 键才回来。

    现在的流程：等它真正退出 → 等系统自动重启 → 仍没回来就主动拉起 → 复核。
    另外注册了退出钩子，万一本进程在重启窗口内挂掉，也会把 explorer 拉回来。
    """
    if not IS_WIN:
        return False
    global _restart_in_progress
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
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

        # 1) 等 explorer 真正退出（关键：taskkill 返回 ≠ 进程已消失）
        _wait_for(lambda: not _explorer_running(), timeout=5.0)

        # 2) 等系统自动重启（Windows 的 AutoRestartShell 一般 1~3 秒拉起）
        if not _wait_for(_explorer_running, timeout=max(1.0, wait)):
            # 3) 没自动回来就自己拉起；只试一次不够，要等它起来
            try:
                subprocess.Popen(["explorer.exe"], creationflags=flags)
            except Exception:
                pass
            _wait_for(_explorer_running, timeout=restore_timeout)

        # 4) 复核：无论如何都要确认 shell 已经回来
        alive = _explorer_running()
        if not alive:
            # 最后再拼一次（例如 Popen 被安全策略拦掉的场景）
            try:
                subprocess.Popen(["explorer.exe"], creationflags=flags)
            except Exception:
                pass
            alive = _wait_for(_explorer_running, timeout=5.0)
        return alive
    finally:
        _restart_in_progress = False




def rule_targets(rule: CleanRule) -> list[str]:
    """实时展开某条规则在本机上的实际路径列表。"""
    try:
        if callable(rule.targets):
            return list(rule.targets())
        return list(rule.targets)
    except Exception:
        return []


def scan_rule(rule: CleanRule, cancel: Callable[[], bool] | None = None) -> int:
    """统计某条规则的体积。返回字节数。"""
    if cancel and cancel():
        return 0
    paths = rule_targets(rule)
    rule.available = bool(paths)
    total = 0
    count = 0
    if rule.key in _SPECIAL:
        total = _recycle_bin_size()
        rule.size = total
        rule.file_count = 0
        rule.scanned = True
        return total
    for p in paths:
        if cancel and cancel():
            break
        total += measure(p)
        count += 1
    rule.size = total
    rule.file_count = count
    rule.scanned = True
    return total


def _recycle_bin_size() -> int:
    """统计回收站体积（不依赖 COM，用目录遍历）。"""
    if not IS_WIN:
        return 0
    total = 0
    sysdrive = os.environ.get("SystemDrive", "C:")
    root = os.path.join(sysdrive + "\\", "$Recycle.Bin")
    if not os.path.isdir(root):
        return 0
    try:
        for sid in os.listdir(root):
            total += measure(os.path.join(root, sid), max_seconds=3.0)
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
@dataclass
class CleanResult:
    key: str
    name: str
    freed: int = 0            # 结论口径：本次实际"释放"的体积（见 clean_rule 的说明）
    estimated: int = 0        # 清理前的扫描体积
    removed_bytes: int = 0    # 真实被删除的对象体积合计
    removed_count: int = 0    # 真实被删除的对象个数
    skipped: int = 0
    failed: int = 0
    disk_delta: int = 0       # 磁盘可用空间的原始读数变化（可能不敏感，仅作参考）
    verified: bool = True     # 是否拿到了可信的数据（False=磁盘读数不可信，按扫描体积计）
    messages: list = field(default_factory=list)


def _rm_path(path: str) -> bool:
    """彻底删除一个文件/目录。

    返回是否**确实**删除成功——注意有些环境（安全封装 / 杀软）会静默拦截删除，
    os.remove 不报错但文件仍在，所以这里删完要复查存在性。
    """
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
            # 复查：拦截式删除不会抛异常，但文件还在
            return not os.path.exists(path)
        if os.path.isdir(path):
            # 用 onerror 提升权限后重试，处理只读文件
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


def _rm_entry(path: str) -> tuple[int, int]:
    """删除一个条目，返回 (是否成功, 被删对象的字节体积)。

    体积必须在**删除前**测量——删完就问不出大小了。这也是「已清理 N MB」
    能给出真实数字（而不是只能靠扫描预估）的关键。
    """
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            size = os.path.getsize(path)
        elif os.path.isdir(path):
            size = measure(path, max_seconds=6.0)
        else:
            size = 0
    except OSError:
        size = 0
    return _rm_path(path), size


def _empty_dir(path: str) -> tuple[int, int]:
    """清空目录内容但保留目录本身，返回 (删除成功数, 失败数)。

    注意：某些环境会静默拦截删除（os.remove 不报错但文件仍在），
    故 _rm_path 会复查存在性，这里据其返回值统计。
    """
    ok = fail = 0
    try:
        entries = os.listdir(path)
    except OSError:
        return 0, 1
    for name in entries:
        child = os.path.join(path, name)
        if _rm_path(child):
            ok += 1
        else:
            fail += 1
    return ok, fail


def _empty_dir_stats(path: str) -> tuple[int, int, int]:
    """清空目录内容，返回 (成功数, 失败数, 被删对象体积合计)。

    与 `_empty_dir` 的区别：额外统计**真实删除的字节数**，
    用于给出可信的「已清理体积」，而不必依赖磁盘可用空间读数
    （实测部分环境下删除文件后可用空间读数不会回升）。
    """
    ok = fail = 0
    total = 0
    try:
        entries = os.listdir(path)
    except OSError:
        return 0, 1, 0
    for name in entries:
        child = os.path.join(path, name)
        success, size = _rm_entry(child)
        if success:
            ok += 1
            total += size
        else:
            fail += 1
    return ok, fail, total


def _clear_recycle_bin() -> tuple[int, int, int]:
    """清空回收站：优先 PowerShell Clear-RecycleBin，失败则直接删目录内容。

    返回 (成功数, 失败数, 被删体积)。体积在清理前先测一遍回收站大小。
    """
    if not IS_WIN:
        return 0, 0, 0
    before = _recycle_bin_size()
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
            capture_output=True, timeout=90, creationflags=flags,
        )
    except Exception:
        pass
    # 兜底：直接删各 SID 目录内容
    sysdrive = os.environ.get("SystemDrive", "C:")
    root = os.path.join(sysdrive + "\\", "$Recycle.Bin")
    ok = fail = 0
    moved = 0
    if os.path.isdir(root):
        try:
            for sid in os.listdir(root):
                sid_path = os.path.join(root, sid)
                if os.path.isdir(sid_path):
                    o, f, sz = _empty_dir_stats(sid_path)
                    ok += o
                    fail += f
                    moved += sz
        except OSError:
            fail += 1
    after = _recycle_bin_size()
    # PowerShell 清空时 moved 会是 0（不经过我们的删除路径），用前后体积差兜底
    volume = moved if moved > 0 else max(0, before - after)
    return ok, fail, volume


def _clean_targets(rule: CleanRule, cancel=None, progress=None) -> tuple[int, int, int]:
    """删除一条规则的所有目标，返回 (成功数, 失败数, 被删体积)。

    只做删除，不碰 explorer——是否重启资源管理器由调用方决定。
    """
    count = fail = volume = 0
    for p in rule_targets(rule):
        if cancel and cancel():
            break
        if progress:
            progress(os.path.basename(p.rstrip("\\/")) or p)
        if os.path.isfile(p):
            success, size = _rm_entry(p)
            if success:
                count += 1
                volume += size
            else:
                fail += 1
        elif os.path.isdir(p):
            # 保留目录本身（很多是程序运行期需要的），只清内容
            ok, f, vol = _empty_dir_stats(p)
            count += ok
            fail += f
            volume += vol
        else:
            fail += 1
    return count, fail, volume


def clean_rule(rule: CleanRule, cancel: Callable[[], bool] | None = None,
               progress: Callable[[str], None] | None = None) -> CleanResult:
    """执行一条规则的清理。返回结果统计。

    关于「释放了多少」的口径（重要，实测踩坑）：
      本模块**不迷信磁盘可用空间读数**。实测在部分环境下（沙箱 / 文件系统
      重定向），创建文件时可用空间会立刻下降，但**删除文件后可用空间不回升**，
      于是「清理后 = 清理前」恒成立，任何基于读数的差值都会得出 0。
      因此这里改为统计**真实被删除对象的体积合计**（删前测量、删后复查存在性，
      只有确认删掉了才计入），把它作为 freed 的结论口径；
      磁盘读数的变化单独放在 `disk_delta` 里仅供诊断，不作为结论。
    """
    res = CleanResult(key=rule.key, name=rule.name)
    # 记录扫描体积作为兜底（磁盘读数可能不敏感）
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

    if rule.key in _SPECIAL:
        ok, fail, volume = _clear_recycle_bin()
        res.removed_count = ok
        res.removed_bytes = volume
        res.failed = fail
        res.messages.append(f"清空 {ok} 项，失败 {fail} 项")
    else:
        count, fail, volume = _clean_targets(rule, cancel, progress)

        # 缩略图/图标缓存可能被 explorer.exe 持有。
        # **只有确实删不掉时**才重启资源管理器重试——重启会让任务栏短暂消失，
        # 无条件重启纯属打扰（而且正是旧版"任务栏消失"bug 的触发源）。
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

    # 结论口径：以「真实删掉的体积」为准
    res.freed = res.removed_bytes
    res.verified = True
    if res.removed_bytes == 0 and res.estimated > 0 and res.removed_count == 0:
        # 一个都没删掉（被占用/无权限），但仍然让用户看到曾扫描到多少
        res.verified = False
        if not any("失败" in m or "占用" in m for m in res.messages):
            res.messages.append("未能删除任何对象（可能被占用或权限不足）")
    return res


# ---------------------------------------------------------------------------
# 提权清理（需要管理员权限的项）
# ---------------------------------------------------------------------------
# 背景：需要管理员权限的规则（Windows 更新缓存、系统日志、部分驱动残留等）
# 在普通权限下**必然失败**——旧版本只在 UI 上标了个"需管理员"标签，却没有任何
# 提权动作，用户点了清理这些项全部原地失败，体验就是"清不掉"。
#
# 正确做法：普通权限清不掉的那部分，用 runas 唤起 **Yuhub.exe 自己的
# `--clean-elevated` 静默模式**清理（弹一次 UAC、无窗口、无额外 UI），
# 结果通过临时 JSON 文件回传。跟 etier.py 的 watchdog 是同一套思路：
# 复用主 exe、base64+JSON 传参、结果走文件（提权进程 stdout 拿不到）。
_ELEVATED_FLAG = "--clean-elevated"


def _elevated_result_path():
    """提权清理结果的落地路径（提权进程写、普通进程读）。"""
    d = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(d, "clean_result.json")


def _yuhub_exe_path():
    """Yuhub.exe 的完整路径（提权时用自己启动自己）。"""
    exe = sys.executable
    if exe and os.path.isfile(exe):
        return exe
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "Yuhub.exe")


def elevated_clean_main(payload_b64):
    """`Yuhub.exe --clean-elevated <base64-json>` 的入口（提权进程侧）。

    payload: {"keys": [...], "result_file": "..."}
    在管理员权限下逐条清理指定规则，把结果写成 JSON 到 result_file。
    全程无窗口（本 exe 是 PyInstaller --windowed + 这里不建任何 UI）。
    """
    try:
        payload = json.loads(base64.b64decode(payload_b64.encode("ascii")).decode("utf-8"))
        keys = payload.get("keys") or []
        result_file = payload.get("result_file") or ""
    except Exception:
        return 2
    if not result_file:
        return 3

    rules = {r.key: r for r in build_rules()}
    out = {}
    for key in keys:
        rule = rules.get(key)
        if rule is None:
            continue
        try:
            res = clean_rule(rule)
        except Exception as exc:                     # noqa: BLE001
            res = CleanResult(key=key, name=key)
            res.messages.append(f"异常：{exc}")
        out[key] = {
            "key": res.key, "name": res.name,
            "freed": res.freed, "estimated": res.estimated,
            "removed_bytes": res.removed_bytes,
            "removed_count": res.removed_count,
            "failed": res.failed, "verified": res.verified,
            "messages": list(res.messages),
        }

    try:
        # 写临时文件再原子改名，避免主进程读到写了一半的 JSON
        tmp = result_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(out, fp, ensure_ascii=False)
        os.replace(tmp, result_file)
    except OSError:
        return 4
    return 0


def run_elevated_clean(keys, timeout=300.0):
    """提权清理指定 key 的规则（会弹一次 UAC）。

    返回 {key: dict}（清理结果）或 None（用户取消 UAC / 启动失败 / 超时）。
    dict 字段同 CleanResult：freed / removed_bytes / removed_count / failed / messages。
    """
    keys = [k for k in (keys or []) if k]
    if not keys:
        return {}
    if not IS_WIN:
        return None

    result_file = _elevated_result_path()
    if not result_file:
        return None
    # 清掉上次可能留下的结果，避免误读旧数据
    for p in (result_file, result_file + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass

    payload = json.dumps({"keys": keys, "result_file": result_file},
                         ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")

    exe = _yuhub_exe_path()
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, f"{_ELEVATED_FLAG} {b64}", None, 0,
        )
    except OSError:
        return None
    if rc <= 32:
        return None                                  # 用户取消了 UAC

    # 轮询等结果文件。提权进程慢慢腾腾地清完才写，给足超时。
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(result_file):
            time.sleep(0.08)                         # 等文件写完（os.replace 已原子化）
            try:
                with open(result_file, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
            except (OSError, ValueError):
                time.sleep(0.3)
                continue
            try:
                os.remove(result_file)
            except OSError:
                pass
            return data
        time.sleep(0.25)
    return None


# ---------------------------------------------------------------------------
# 后台扫描器
# ---------------------------------------------------------------------------
class CleanerScanner:
    """后台线程扫描所有规则，通过回调逐条投递结果。"""

    def __init__(self):
        self._thread = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    @property
    def running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, rules, on_progress, on_done):
        """on_progress(rule) 每扫完一条调用一次；on_done(total_bytes, rules) 全部完成。"""
        if self.running:
            return False
        self._cancel.clear()

        def _run():
            total = 0
            try:
                for r in rules:
                    if self._cancel.is_set():
                        break
                    scan_rule(r, cancel=self._cancel.is_set)
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
            self._thread = threading.Thread(target=_run, name="YuhubCleanerScan", daemon=True)
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
            self._thread = threading.Thread(target=_run, name="YuhubCleanerWorker", daemon=True)
            self._thread.start()
        return True

    def cancel(self):
        self._cancel.set()
