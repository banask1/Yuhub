# -*- coding: utf-8 -*-
"""内存优化：把程序占着却暂时不用的物理内存还给系统。

## 三个动作（能做的才做，做不到的如实跳过）

① **逐进程 EmptyWorkingSet** —— 不需要管理员
   把每个进程工作集里"已占用但当前不活跃"的物理页移出工作集（进系统
   备用列表，必要时落页面文件）。进程**照常运行**，被移走的页在下次
   访问时由系统按需调回。这一步不结束任何进程、不动它的线程。

② **清空系统备用列表（standby list）** —— 需要管理员
   备用列表装的是"已回收、随时可复用"的物理页。它虽然计入任务管理器
   的「可用内存」，但系统要主动去动它才会变成空闲页。走
   `NtSetSystemInformation(SystemMemoryListInformation)` 主动清空后，
   可用内存会明显上涨 —— PCL 讲的"降低约 1/3 物理内存占用"主要来自
   这里。这是**未公开**的 NT 内核接口，普通权限必然失败，我们只跳过
   记状态，不报错、也不影响其余动作。

③ **收缩系统文件缓存** —— 需要管理员
   `SetSystemFileCacheSize` 把系统文件缓存压到 0 再放开，逼系统把缓存
   里的脏页写回、干净的页释放掉。

## 安全边界（写死在代码里，不可配置）

* 本模块**只**调用 EmptyWorkingSet / NtSetSystemInformation /
  SetSystemFileCacheSize / GetProcessMemoryInfo。**没有任何**
  TerminateProcess / taskkill / ExitProcess 之类的结束进程调用 ——
  "内存优化"永远不会把谁的程序关掉。
* 关键系统进程（System / csrss / lsass / dwm / Memory Compression …）
  和本进程自己在 SKIP_NAMES 里。
* 当前**最前面窗口**所在进程默认跳过：那是用户正在用的程序，把它的
  工作集清掉只会让用户立刻感觉到卡顿（要重新调页）。
* 只回收，不动用户数据：不清剪贴板、不删任何文件。

## 两个数字为什么不一样

* `freed_ws`   —— 从各进程工作集里收回的字节数（动作 ① 的贡献）。
  这批页变成了备用列表里的页，所以任务管理器的「可用内存」此时可能
  几乎不变（备用列表本来就算可用）。
* `freed_avail` —— 可用内存的净增量（动作 ②③ 的贡献）。
  非管理员时它接近 0，这是正常的、也是必须如实告诉用户的。
"""

import ctypes
import json
import os
import sys
import time

IS_WIN = os.name == "nt"

# 单次优化的时间上限（秒）：进程特别多、或个别进程收页很慢时到点就停，
# 把已有结果交出去 —— 定时线程不能无限期挂着。
MAX_SECONDS = 15.0

# 优化后等计数器稳定再采样（清 standby 是异步完成的）
SETTLE = 0.25

# 跳过名单（exe 名小写）。这些要么打不开（受保护进程）、要么碰了有害
# （桌面 / 登录 / 音频 / 输入法）。
# 注意 svchost.exe **不在**名单里：它在 SYSTEM 账户下运行，普通权限
# 打不开会自动跳过；管理员模式下清它的页是安全的（服务照跑，只是页被
# 换出后按需调回），而它往往是工作集占用的大头之一。
SKIP_NAMES = {
    "system", "registry", "memory compression", "idle", "secure system",
    "csrss.exe", "smss.exe", "wininit.exe", "services.exe", "lsass.exe",
    "winlogon.exe", "fontdrvhost.exe", "dwm.exe", "audiodg.exe",
    "sihost.exe", "ctfmon.exe", "yuhub.exe",
}

# ---- 访问权限位 ----
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100

TH32CS_SNAPPROCESS = 0x00000002

# ---- 未公开 NT 接口相关常量 ----
SYSTEM_MEMORY_LIST_INFORMATION = 0x50   # SystemMemoryListInformation
MEMORY_PURGE_STANDBY_LIST = 4           # 清空全部备用列表
SIZE_T_MAX = ctypes.c_size_t(-1).value

_LIB_CACHE = {}


# ---------------------------------------------------------------------------
# 基础结构体
# ---------------------------------------------------------------------------
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),   # ULONG_PTR：64 位下 8 字节
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


# ---------------------------------------------------------------------------
# ctypes 绑定（每条都显式声明 argtypes / restype）
# ---------------------------------------------------------------------------
def _k32():
    lib = _LIB_CACHE.get("k32")
    if lib is None:
        lib = ctypes.windll.kernel32
        lib.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        lib.OpenProcess.restype = ctypes.c_void_p
        lib.CloseHandle.argtypes = [ctypes.c_void_p]
        lib.CloseHandle.restype = ctypes.c_bool
        lib.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        lib.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        lib.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.Process32FirstW.restype = ctypes.c_bool
        lib.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.Process32NextW.restype = ctypes.c_bool
        lib.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
        lib.GlobalMemoryStatusEx.restype = ctypes.c_bool
        # 64 位下 SIZE_T 必须声明，否则 -1 会被截断成 32 位
        lib.SetSystemFileCacheSize.argtypes = [
            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_ulong]
        lib.SetSystemFileCacheSize.restype = ctypes.c_bool
        # 伪句柄 -1：不声明 restype 会被截成 32 位，传给 OpenProcessToken 就废了
        lib.GetCurrentProcess.argtypes = []
        lib.GetCurrentProcess.restype = ctypes.c_void_p
        lib.GetLastError.argtypes = []
        lib.GetLastError.restype = ctypes.c_ulong
        _LIB_CACHE["k32"] = lib
    return lib


def _psapi():
    lib = _LIB_CACHE.get("psapi")
    if lib is None:
        lib = ctypes.windll.psapi
        lib.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        lib.EmptyWorkingSet.restype = ctypes.c_bool
        lib.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        lib.GetProcessMemoryInfo.restype = ctypes.c_bool
        _LIB_CACHE["psapi"] = lib
    return lib


def _ntdll():
    lib = _LIB_CACHE.get("ntdll")
    if lib is None:
        lib = ctypes.WinDLL("ntdll.dll")
        lib.NtSetSystemInformation.argtypes = [
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong]
        lib.NtSetSystemInformation.restype = ctypes.c_long   # NTSTATUS
        _LIB_CACHE["ntdll"] = lib
    return lib


def _u32():
    lib = _LIB_CACHE.get("u32")
    if lib is None:
        lib = ctypes.windll.user32
        lib.GetForegroundWindow.restype = ctypes.c_void_p
        lib.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        lib.GetWindowThreadProcessId.restype = ctypes.c_ulong
        _LIB_CACHE["u32"] = lib
    return lib


# ---------------------------------------------------------------------------
# 只读查询
# ---------------------------------------------------------------------------
def memory_status():
    """物理内存状态（字节）。取不到时返回全 0，绝不抛异常。

    注意 Windows 的「可用」= 空闲页 + 备用列表页，与任务管理器同口径。
    """
    blank = {"total": 0, "avail": 0, "used": 0, "percent": 0.0, "load": 0}
    if not IS_WIN:
        return blank
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not _k32().GlobalMemoryStatusEx(ctypes.byref(st)):
            return blank
        total = int(st.ullTotalPhys)
        avail = int(st.ullAvailPhys)
        used = max(0, total - avail)
        return {
            "total": total,
            "avail": avail,
            "used": used,
            "percent": (used * 100.0 / total) if total else 0.0,
            "load": int(st.dwMemoryLoad),
        }
    except Exception:                                    # noqa: BLE001
        return blank


def list_processes():
    """[(pid, exe名小写), ...]。失败返回空列表。"""
    if not IS_WIN:
        return []
    k32 = _k32()
    out = []
    try:
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        invalid = ctypes.c_void_p(-1).value
        if not snap or snap == invalid:
            return []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if not k32.Process32FirstW(snap, ctypes.byref(entry)):
                return []
            while True:
                out.append((int(entry.th32ProcessID),
                            (entry.szExeFile or "").lower()))
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    break
        finally:
            k32.CloseHandle(snap)
    except Exception:                                    # noqa: BLE001
        return out
    return out


def foreground_pid():
    """当前最前面窗口所属进程的 PID（拿不到返回 0）。"""
    if not IS_WIN:
        return 0
    try:
        hwnd = _u32().GetForegroundWindow()
        if not hwnd:
            return 0
        pid = ctypes.c_ulong(0)
        _u32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:                                    # noqa: BLE001
        return 0


def is_admin():
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                    # noqa: BLE001
        return False


# ---- 提权：启用当前进程的某个特权 ----
# 清备用列表 / 收缩文件缓存需要 SeProfileSingleProcessPrivilege 与
# SeIncreaseQuotaPrivilege。**管理员默认就持有这两个特权，但它们是禁用
# 状态**——不 AdjustTokenPrivileges 打开，NtSetSystemInformation 只会
# 返回 STATUS_PRIVILEGE_NOT_HELD。这是"明明是管理员却清不动"的根因。
_TOKEN_ADJUST_PRIVILEGES = 0x0020
_TOKEN_QUERY = 0x0008
_SE_PRIVILEGE_ENABLED = 0x00000002
_ERROR_NOT_ALL_ASSIGNED = 1300


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", ctypes.c_ulong)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", ctypes.c_ulong),
                ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


def _advapi():
    lib = _LIB_CACHE.get("advapi")
    if lib is None:
        lib = ctypes.windll.advapi32
        lib.LookupPrivilegeValueW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(_LUID)]
        lib.LookupPrivilegeValueW.restype = ctypes.c_bool
        lib.OpenProcessToken.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        lib.OpenProcessToken.restype = ctypes.c_bool
        lib.AdjustTokenPrivileges.argtypes = [
            ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p]
        lib.AdjustTokenPrivileges.restype = ctypes.c_bool
        _LIB_CACHE["advapi"] = lib
    return lib


def enable_privilege(name):
    """启用当前进程的一个特权。成功（含"本来就有"）返回 True。"""
    if not IS_WIN:
        return False
    k32 = _k32()
    adv = _advapi()
    h_token = ctypes.c_void_p()
    try:
        if not adv.OpenProcessToken(
                k32.GetCurrentProcess(),
                _TOKEN_ADJUST_PRIVILEGES | _TOKEN_QUERY,
                ctypes.byref(h_token)):
            return False
        try:
            luid = _LUID()
            if not adv.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                return False
            tp = _TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = _SE_PRIVILEGE_ENABLED
            if not adv.AdjustTokenPrivileges(
                    h_token, False, ctypes.byref(tp), 0, None, None):
                return False
            # AdjustTokenPrivileges 成功也可能"一个都没改上"（账户不持有该特权）
            return k32.GetLastError() != _ERROR_NOT_ALL_ASSIGNED
        finally:
            k32.CloseHandle(h_token)
    except Exception:                                    # noqa: BLE001
        return False


def _sum_working_set(pids):
    """Σ 工作集（字节）+ 读到的进程数。读不到的跳过，不计 0。"""
    psapi = _psapi()
    k32 = _k32()
    total = 0
    got = 0
    pmc = _PROCESS_MEMORY_COUNTERS_EX()
    pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX)
    for pid in pids:
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not h:
            continue
        try:
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                total += int(pmc.WorkingSetSize)
                got += 1
        finally:
            k32.CloseHandle(h)
    return total, got


# ---------------------------------------------------------------------------
# 三个动作
# ---------------------------------------------------------------------------
def _empty_and_measure(pid):
    """对单个进程：先读工作集，再 EmptyWorkingSet。

    返回 (工作集字节 | None, 是否成功回收)。

    权限分两步试：先按 EmptyWorkingSet 文档要求的
    PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA 打开；被拒再退一步用
    PROCESS_QUERY_LIMITED_INFORMATION（部分低完整性进程只认后者）。
    两步都打不开就返回失败，由调用方计入 failed —— **不会**升级成
    "暴力结束进程"之类的操作。
    """
    k32 = _k32()
    psapi = _psapi()
    h = k32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, 0, int(pid))
    if not h:
        h = k32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, 0, int(pid))
    if not h:
        return None, False
    try:
        ws = None
        pmc = _PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX)
        if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            ws = int(pmc.WorkingSetSize)
        ok = bool(psapi.EmptyWorkingSet(h))
        return ws, ok
    except Exception:                                    # noqa: BLE001
        return None, False
    finally:
        try:
            k32.CloseHandle(h)
        except Exception:                                # noqa: BLE001
            pass


def purge_standby_list():
    """清空系统备用列表。需要管理员，失败返回 False。

    NtSetSystemInformation(SystemMemoryListInformation, &4, 4)：
    未公开的 NT 内核接口，返回 NTSTATUS（0 = 成功）。
    调用前必须启用 SeProfileSingleProcessPrivilege —— 管理员默认持有但
    是**禁用**状态，不启用必然拿回 STATUS_PRIVILEGE_NOT_HELD。
    """
    if not IS_WIN:
        return False
    try:
        if not enable_privilege("SeProfileSingleProcessPrivilege"):
            return False
        val = ctypes.c_ulong(MEMORY_PURGE_STANDBY_LIST)
        st = _ntdll().NtSetSystemInformation(
            SYSTEM_MEMORY_LIST_INFORMATION, ctypes.byref(val),
            ctypes.sizeof(val))
        return int(st) == 0
    except Exception:                                    # noqa: BLE001
        return False


def shrink_file_cache():
    """把系统文件缓存压到最小再放开。需要管理员，失败返回 False。"""
    if not IS_WIN:
        return False
    try:
        if not enable_privilege("SeIncreaseQuotaPrivilege"):
            return False
        k32 = _k32()
        ok = bool(k32.SetSystemFileCacheSize(0, SIZE_T_MAX, 0))
        # 无论压没压下去都要把上限恢复，否则会一直限制系统缓存
        k32.SetSystemFileCacheSize(SIZE_T_MAX, SIZE_T_MAX, 0)
        return ok
    except Exception:                                    # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def optimize(progress=None, skip_foreground=True, deadline_s=None):
    """执行一次内存优化。**不会结束任何进程。**

    progress(text) 可选，用于回报阶段文案（可能在别的线程里被调用）。
    skip_foreground=True 时跳过当前最前面窗口所在进程。
    返回结果 dict，字段见模块 docstring 与 README 中的说明。
    """
    if not IS_WIN:
        return {"ok": False, "error": "内存优化仅支持 Windows", "ts": time.time()}

    def emit(text):
        if progress is not None:
            try:
                progress(text)
            except Exception:                            # noqa: BLE001
                pass

    t0 = time.perf_counter()
    limit = float(deadline_s if deadline_s else MAX_SECONDS)

    before = memory_status()
    elevated = is_admin()
    fg_pid = foreground_pid() if skip_foreground else 0
    self_pid = os.getpid()

    emit("正在枚举进程…")
    procs = list_processes()
    if not procs:
        # 进程枚举失败（32/64 位不匹配、受限环境）会静默退化成"遍历 0 个
        # 程序"——那是最难排查的一类"看着成功其实没干活"。宁可明确报错。
        return {"ok": False, "ts": time.time(), "elevated": elevated,
                "error": "无法枚举进程（可能是 32/64 位不匹配或权限受限）"}
    targets = []
    skipped = 0
    for pid, name in procs:
        if pid <= 0 or pid == self_pid:
            skipped += 1
            continue
        if name in SKIP_NAMES:
            skipped += 1
            continue
        if fg_pid and pid == fg_pid:
            skipped += 1
            continue
        targets.append((pid, name))

    emit("正在回收 %d 个程序的工作集…" % len(targets))
    ws_before = 0
    measured = []
    emptied = 0
    failed = 0
    timed_out = False
    for pid, name in targets:
        if time.perf_counter() - t0 > limit:
            timed_out = True
            break
        ws, ok = _empty_and_measure(pid)
        if ws is not None:
            ws_before += ws
            measured.append(pid)
        if ok:
            emptied += 1
        else:
            failed += 1

    emit("正在清理系统级缓存…")
    standby = "skip"
    filecache = "skip"
    if elevated:
        standby = "ok" if purge_standby_list() else "failed"
        filecache = "ok" if shrink_file_cache() else "failed"

    time.sleep(SETTLE)
    ws_after = _sum_working_set(measured)[0]
    after = memory_status()

    freed_ws = max(0, ws_before - ws_after)
    freed_avail = after["avail"] - before["avail"]
    return {
        "ok": True,
        "ts": time.time(),
        "elevated": elevated,
        "before": before,
        "after": after,
        "ws_before": ws_before,
        "ws_after": ws_after,
        "freed_ws": freed_ws,
        "freed_avail": freed_avail,
        "used_before": before["used"],
        "used_after": after["used"],
        "targets": len(targets),
        "emptied": emptied,
        "failed": failed,
        "skipped": skipped,
        "measured": len(measured),
        "timed_out": timed_out,
        "foreground_skipped": bool(fg_pid),
        "system_ops": {"standby": standby, "filecache": filecache},
        "seconds": round(time.perf_counter() - t0, 2),
    }


# ---------------------------------------------------------------------------
# 提权（以管理员身份做系统级清理）
# ---------------------------------------------------------------------------
_ELEVATED_FLAG = "--mem-optimize-elevated"


def exe_path():
    """当前程序的 exe 路径；源码运行（python.exe）时返回空串。"""
    if not getattr(sys, "frozen", False):
        return ""
    exe = sys.executable or ""
    if exe and os.path.isfile(exe) and exe.lower().endswith(".exe"):
        return exe
    return ""


def _result_path():
    d = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(d, "memopt_result.json")


def elevated_optimize_main(payload_b64):
    """`Yuhub.exe --mem-optimize-elevated <base64-json>` 入口（提权进程侧）。

    提权进程无窗口、无 UI，只做一次完整优化，把结果 JSON 原子写回文件。
    """
    import base64

    try:
        payload = json.loads(
            base64.b64decode(payload_b64.encode("ascii")).decode("utf-8"))
        result_file = payload.get("result_file") or ""
        skip_fg = bool(payload.get("skip_foreground", True))
    except Exception:                                    # noqa: BLE001
        return 2
    if not result_file:
        return 3

    res = optimize(skip_foreground=skip_fg)
    try:
        tmp = result_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(res, fp, ensure_ascii=False)
        os.replace(tmp, result_file)
    except OSError:
        return 4
    return 0


def run_elevated_optimize(timeout=120.0, skip_foreground=True):
    """runas 起一个提权进程做完整优化（会弹一次 UAC）。

    返回结果 dict；用户取消 UAC / 启动失败 / 超时 / 源码运行时返回 None。
    """
    import base64

    if not IS_WIN:
        return None
    exe = exe_path()
    if not exe:
        return None                       # 源码运行：没法把 python.exe 当 exe 提权
    result_file = _result_path()
    if not result_file:
        return None
    for p in (result_file, result_file + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass

    payload = json.dumps(
        {"result_file": result_file, "skip_foreground": bool(skip_foreground)},
        ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")

    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, "%s %s" % (_ELEVATED_FLAG, b64), None, 0)
    except OSError:
        return None
    if rc <= 32:
        return None                       # 用户取消了 UAC

    deadline = time.monotonic() + max(5.0, float(timeout))
    while time.monotonic() < deadline:
        if os.path.exists(result_file):
            time.sleep(0.08)
            try:
                with open(result_file, "r", encoding="utf-8") as fp:
                    return json.load(fp)
            except (OSError, ValueError):
                return None
        time.sleep(0.15)
    return None


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------
def human_bytes(n):
    """1536 -> '1.50 KB'。用于界面文案。"""
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    neg = v < 0
    v = abs(v)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024.0 or unit == "TB":
            if unit == "B":
                out = "%d B" % int(v)
            elif unit in ("KB", "MB"):
                out = "%.1f %s" % (v, unit)
            else:
                out = "%.2f %s" % (v, unit)
            return ("-" + out) if neg else out
        v /= 1024.0
    return "0 B"


def describe(res):
    """把 optimize() 的结果压成一句人能读的结论。"""
    if not res or not res.get("ok"):
        return (res or {}).get("error") or "优化未完成"
    ws = res.get("freed_ws", 0)
    av = res.get("freed_avail", 0)
    parts = []
    if ws:
        parts.append("从 %d 个程序回收 %s" % (res.get("emptied", 0), human_bytes(ws)))
    else:
        parts.append("已遍历 %d 个程序" % res.get("targets", 0))
    if av > 0:
        parts.append("可用内存 +%s" % human_bytes(av))
    elif not res.get("elevated"):
        parts.append("系统级缓存需管理员权限")
    return " · ".join(parts)
