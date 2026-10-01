r"""Yuhub 实时硬件监控（完全离线，性能优先）。

为什么不用 PowerShell 做实时刷新：
  PowerShell 进程启动 + Get-Counter 首次采样约 **4 秒**，每秒刷新完全不可用。
  实测对比（本机 i5-12450H）：
    - ctypes 直调 GetSystemTimes 差分求 CPU 占用 → ~0.03 ms
    - ctypes 直调 GlobalMemoryStatusEx 求内存占用 → ~0.03 ms
    - nvidia-smi 子进程求 GPU 占用/温度/功耗     → ~240 ms
    - nvml.dll 直调求 GPU 温度/功耗/显存         → ~1 ms
  因此：CPU / 内存 / 磁盘容量走 ctypes（微秒级，可在主线程直接调）；
  GPU 走「厂商接口 + PDH 计数器」，且必须放到后台线程，避免阻塞 UI。

显卡数据分两层（见 read_gpu）：
  厂商层  gpu_sensors：温度 / 功耗 / 频率 / 风扇
          NVIDIA → NVML；AMD → ADL；两者都读不到时退回 nvidia-smi 子进程
  通用层  PdhGpuReader：利用率 + 显存（WDDM 上报，AMD / Intel / NVIDIA 通用）

线程模型：
  LiveMonitor 内部维护一个守护线程，按 interval 采样并发出 sample 信号。
  慢速来源（PDH / 子进程）与主线程的 CPU/内存采样分离——不能互相拖累。
"""

import ctypes
import re
import subprocess
import sys
import time
from ctypes import wintypes
from threading import Event, Lock, Thread

from PySide6.QtCore import QObject, Signal

try:                                    # 厂商传感器（NVIDIA NVML / AMD ADL）
    import gpu_sensors
except Exception:                       # pragma: no cover - 极端情况下降级
    gpu_sensors = None

IS_WIN = sys.platform == "win32"

# ---------------------------------------------------------------------------
# ctypes 结构定义
# ---------------------------------------------------------------------------


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _ft_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _read_system_times():
    """返回 (idle, kernel, user) 三个累计 tick 数；非 Windows 返回 None。"""
    if not IS_WIN:
        return None
    try:
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
        if not ok:
            return None
        return _ft_to_int(idle), _ft_to_int(kernel), _ft_to_int(user)
    except Exception:
        return None


class CpuSampler:
    """CPU 占用采样器：用两次 GetSystemTimes 的差值算利用率。

    注意 GetSystemTimes 返回的 kernel 时间**已经包含** idle 时间，
    因此 busy = (kernel + user) 的增量 - idle 增量，total = (kernel + user) 增量。
    """

    def __init__(self):
        self._prev = _read_system_times()

    def sample(self):
        """返回 (总占用百分比, 是否有效)。"""
        cur = _read_system_times()
        if cur is None or self._prev is None:
            return 0.0, False
        idle_d = cur[0] - self._prev[0]
        total_d = (cur[1] - self._prev[1]) + (cur[2] - self._prev[2])
        self._prev = cur
        if total_d <= 0:
            return 0.0, False
        busy = total_d - idle_d
        pct = max(0.0, min(100.0, 100.0 * busy / total_d))
        return pct, True


def read_memory():
    """返回 dict：占用百分比、已用/总量字节。非 Windows 或失败返回 None。"""
    if not IS_WIN:
        return None
    try:
        ms = _MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return None
        total = ms.ullTotalPhys
        avail = ms.ullAvailPhys
        used = total - avail
        return {
            "percent": float(ms.dwMemoryLoad),
            "used_bytes": used,
            "total_bytes": total,
            "avail_bytes": avail,
        }
    except Exception:
        return None


def read_disk_space(drive="C:\\"):
    """读取指定盘符的总量 / 剩余量（字节）。用 GetDiskFreeSpaceExW，微秒级。"""
    if not IS_WIN:
        return None
    try:
        # 统一成 "C:\" 形式，避免调用方传 "C://" 之类的路径
        letter = str(drive).strip().rstrip("\\/").rstrip(":").upper()[:1]
        if not letter.isalpha():
            letter = "C"
        root = f"{letter}:\\"
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(root), None, ctypes.byref(total), ctypes.byref(free)
        )
        if not ok:
            return None
        return {"drive": f"{letter}:", "free_bytes": free.value, "total_bytes": total.value}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# GPU：nvidia-smi（厂商层的最后兜底，见 gpu_sensors / read_gpu）
# ---------------------------------------------------------------------------
_NVSMI_QUERY = (
    "utilization.gpu,memory.used,memory.total,temperature.gpu,"
    "power.draw,power.limit,clocks.sm,clocks.max.sm,fan.speed"
)

_nvsmi_path_cache = {"path": None, "checked": False}


def _nvidia_smi_path():
    """定位 nvidia-smi.exe（缓存结果，避免每次都查）。"""
    if _nvsmi_path_cache["checked"]:
        return _nvsmi_path_cache["path"]
    _nvsmi_path_cache["checked"] = True
    if not IS_WIN:
        return None

    import os
    import shutil

    found = shutil.which("nvidia-smi")
    if found:
        _nvsmi_path_cache["path"] = found
        return found

    # 常见安装位置（驱动升级后 system32 里的副本可能没有）
    candidates = [
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ]
    for base in (r"C:\Program Files", r"C:\Program Files (x86)"):
        try:
            for name in os.listdir(base):
                if "nvidia" in name.lower():
                    p = os.path.join(base, name, "nvidia-smi.exe")
                    candidates.append(p)
        except OSError:
            pass
    for c in candidates:
        if os.path.exists(c):
            _nvsmi_path_cache["path"] = c
            return c
    return None


def read_gpu_nvsmi():
    """用 nvidia-smi 子进程读 NVIDIA GPU（约 240ms）。

    只在 NVML 不可用（老驱动没带 nvml.dll / 组件缺失）时才走这条路——
    它是"最后兜底"，不是主路径。
    返回 dict 或 None（无 NVIDIA 显卡 / 驱动未装 / 调用失败）。
    """
    path = _nvidia_smi_path()
    if not path:
        return None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WIN else 0
        proc = subprocess.run(
            [path, f"--query-gpu={_NVSMI_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            timeout=8,
            creationflags=flags,
        )
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        first = text.splitlines()[0]
        parts = [p.strip() for p in first.split(",")]

        def num(idx):
            try:
                v = parts[idx]
                return None if v in ("", "N/A", "[N/A]", "[Not Supported]") else float(v)
            except (IndexError, ValueError):
                return None

        return {
            "util": num(0),
            "mem_used_mb": num(1),
            "mem_total_mb": num(2),
            "temp": num(3),
            "power_w": num(4),
            "power_limit_w": num(5),
            "clock_sm_mhz": num(6),
            "clock_max_mhz": num(7),
            "fan_percent": num(8),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# GPU：全厂商通用 —— Windows PDH 性能计数器（WDDM 上报，与任务管理器同源）
# ---------------------------------------------------------------------------
# nvidia-smi 只覆盖 NVIDIA：AMD / Intel 机器上根本没有这个命令，所以 A 卡用户
# 以前只能看到"未检测到可用显卡"。这里用 Windows 自带的 PDH 性能计数器兜底，
# 它由 WDDM 图形内核（VidSch / VidMem）直接上报，AMD / Intel / NVIDIA 全通用：
#
#   \GPU Engine(*)\Utilization Percentage
#       → 每个进程在每个引擎上的占用。实例名形如
#         pid_4956_luid_0x00000000_0x0000D0BA_phys_0_eng_0_engtype_3D
#   \GPU Local Adapter Memory(*)\Local Usage     （新驱动）
#   \GPU Adapter Memory(*)\Dedicated Usage       （旧驱动 / Intel）
#       → 专用显存占用
#
# 【坑 1】ctypes 调 pdh.dll 必须显式声明 argtypes / restype。
#   用默认转换时，64 位句柄会被按 c_int 传入，PdhGetFormattedCounterArrayW
#   会返回 PDH_MORE_DATA 却把所需长度写成 0——表现为"一条实例都读不到"，
#   也就是 A 卡用户看到的"读不到显卡占用率"。显式声明后同样的调用立刻
#   返回 351 个实例，这是本次修复的关键。
#
# 【坑 2】带通配符 (*) 的计数器**不能**用 PdhGetFormattedCounterValue 读单值。
#   实测它只返回第一个匹配实例，而 GPU Engine 的第一个实例往往是空闲引擎，
#   于是恒为 0.0%。这个假数据比报错更有害（界面上显示 0% 而不是"不可用"）。
#   必须用 PdhGetFormattedCounterArrayW 一次取回全部实例。
#
# 【口径】"整卡占用率"取**最繁忙的引擎**，与任务管理器一致。微软 DirectX
#   团队的原话是：简单求和会超 100%，取平均不准，只取 3D 引擎在纯视频解码时
#   又恒为 0，最终选定"当前最繁忙的引擎占用率"作为代表值。同一个引擎类型下
#   多个进程的占用要相加——它们共享同一个引擎的容量。
#
# PDH 的代价是拿不到温度 / 功耗 / 频率（那是 NVAPI、AMD ADL、Intel IGCL 的
# 专属接口，且需要厂商动态库）。本函数只保证 util + 显存，其余字段留 None，
# 由 UI 降级显示。
_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2


class _PDH_COUNTERVALUE(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE。

    CStatus 是 4 字节，后面有一个对齐填充：union 里的 double / LONGLONG
    要求 8 字节对齐。字段顺序和 padding 都不能改，否则读出来是垃圾值。
    """

    class _UNION(ctypes.Union):
        _fields_ = [
            ("longValue", ctypes.c_long),
            ("doubleValue", ctypes.c_double),
            ("largeValue", ctypes.c_longlong),
            ("WideStringValue", ctypes.c_wchar_p),
        ]

    _fields_ = [("CStatus", ctypes.c_uint), ("value", _UNION)]


class _PDH_ITEM(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE_ITEM_W。"""

    _fields_ = [("szName", ctypes.c_wchar_p),
                ("FmtValue", _PDH_COUNTERVALUE)]


class PdhGpuReader:
    """PDH GPU 计数器读取器（AMD / Intel / NVIDIA 全通用）。

    内部持有一个**常驻查询**：只 add 一次通配符计数器，之后每轮采样只做
    CollectQueryData + 读数组，省掉反复枚举数百个 GPU 实例的开销
    （本机实测 351 个实例，每次都重建会明显变慢）。

    非线程安全，应由单个采样线程持有；对外只通过 read_gpu_pdh() 加锁访问。
    """

    ENGINE_PATH = r"\GPU Engine(*)\Utilization Percentage"
    # 显存计数器名随驱动版本而变：实测本机（新驱动）是
    # "GPU Local Adapter Memory\Local Usage"，旧驱动 / Intel 是
    # "GPU Adapter Memory\Dedicated Usage"，两个都挂，命中哪个用哪个。
    MEM_USED_PATHS = (
        r"\GPU Local Adapter Memory(*)\Local Usage",
        r"\GPU Adapter Memory(*)\Dedicated Usage",
    )
    SETTLE = 0.2            # 两次 Collect 之间的最小间隔（秒）
    MAX_RETRY = 3           # 建查询连续失败几次后放弃，避免每轮都重试

    # 实例名拆解：pid_4956_luid_0x00000000_0x0000D0BA_phys_0_eng_0_engtype_3D
    _RE_LUID = re.compile(r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)")
    _RE_PHYS = re.compile(r"phys_(\d+)")
    _RE_ENG = re.compile(r"engtype_([A-Za-z0-9]+)")

    def __init__(self):
        self._pdh = None
        self._query = None
        self._counters = []          # [(path, handle)]
        self._retry = 0

    # ---------------------------------------------------------------- 公共
    def read(self):
        """返回与 read_gpu() 同构的 dict；完全读不到时返回 None。"""
        if not IS_WIN:
            return None
        if self._query is None:
            if self._retry >= self.MAX_RETRY:
                return None
            if not self._open():
                self._retry += 1
                return None
            self._retry = 0

        # 第一次 collect 建立基线，第二次才有速率类计数器的差值
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            self._discard()
            return None
        time.sleep(self.SETTLE)
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            self._discard()
            return None

        util = None
        mem_used_mb = None
        for path, handle in self._counters:
            items = self._read_array(handle)
            if not items:
                continue
            if path == self.ENGINE_PATH:
                util = self._overall_util(items)
            else:
                vals = [v for _n, v in items if v > 0]
                if vals:
                    mem_used_mb = self._as_mb(max(vals))

        if util is None and mem_used_mb is None:
            return None
        return {
            "util": util,
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": _reg_dedicated_vram_mb(),
            "temp": None,
            "power_w": None,
            "power_limit_w": None,
            "clock_sm_mhz": None,
            "clock_max_mhz": None,
            "fan_percent": None,
            "vendor": "pdh",
        }

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _bind():
        """载入 pdh.dll 并显式声明全部原型（见本章开头「坑 1」）。"""
        pdh = ctypes.windll.pdh
        hq = ctypes.POINTER(wintypes.HANDLE)
        pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, hq]
        pdh.PdhOpenQueryW.restype = ctypes.c_long
        pdh.PdhAddCounterW.argtypes = [wintypes.HANDLE, ctypes.c_wchar_p,
                                       ctypes.c_size_t, hq]
        pdh.PdhAddCounterW.restype = ctypes.c_long
        pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        pdh.PdhCollectQueryData.restype = ctypes.c_long
        pdh.PdhGetFormattedCounterArrayW.argtypes = [
            wintypes.HANDLE, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(_PDH_ITEM)]
        pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
        pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
        pdh.PdhCloseQuery.restype = ctypes.c_long
        return pdh

    def _open(self):
        """打开查询并挂上通配符计数器。成功返回 True。"""
        try:
            if self._pdh is None:
                self._pdh = self._bind()
            q = wintypes.HANDLE()
            if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
                return False
            counters = []
            for path in (self.ENGINE_PATH,) + self.MEM_USED_PATHS:
                h = wintypes.HANDLE()
                if self._pdh.PdhAddCounterW(q, path, 0, ctypes.byref(h)) == 0:
                    counters.append((path, h))
            if not counters:
                self._pdh.PdhCloseQuery(q)
                return False
            self._query, self._counters = q, counters
            return True
        except Exception:
            return False

    def _discard(self):
        """查询失效（驱动重启等）→ 关掉，下轮重建。"""
        try:
            if self._pdh and self._query:
                self._pdh.PdhCloseQuery(self._query)
        except Exception:
            pass
        self._query, self._counters = None, []

    def _read_array(self, handle):
        """读一个通配符计数器的全部实例，返回 [(实例名, 值)]。

        两次调用：先问所需缓冲长度（PDH 返回 PDH_MORE_DATA），再按长度取数。
        只保留状态有效的实例——CStatus 为 0x0(VALID) / 0x1(NEW)，其余
        （无数据 / 无效）的 doubleValue 无意义，收了会污染统计。
        """
        size = ctypes.c_uint(0)
        count = ctypes.c_uint(0)
        rc = self._pdh.PdhGetFormattedCounterArrayW(
            handle, _PDH_FMT_DOUBLE, ctypes.byref(size),
            ctypes.byref(count), None)
        if (rc & 0xFFFFFFFF) != _PDH_MORE_DATA or size.value == 0:
            return []
        buf = ctypes.create_string_buffer(size.value)
        rc = self._pdh.PdhGetFormattedCounterArrayW(
            handle, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count),
            ctypes.cast(buf, ctypes.POINTER(_PDH_ITEM)))
        if rc != 0:
            return []
        arr = ctypes.cast(buf, ctypes.POINTER(_PDH_ITEM))
        out = []
        for i in range(count.value):
            item = arr[i]
            if item.FmtValue.CStatus in (0, 1):
                out.append((item.szName or "", item.FmtValue.value.doubleValue))
        return out

    @classmethod
    def _overall_util(cls, items):
        """按「最繁忙的引擎」算整卡占用率（与任务管理器同口径）。"""
        groups = {}
        for name, val in items:
            if val <= 0:
                continue
            key = cls._engine_key(name)
            if key[2]:
                # 能认出引擎类型 → 同一引擎上的各进程占用相加
                groups[key] = groups.get(key, 0.0) + val
            else:
                # 认不出引擎类型（个别老驱动）→ 退化为取最大，宁低勿虚高
                groups[key] = max(groups.get(key, 0.0), val)
        if not groups:
            return 0.0
        return max(0.0, min(100.0, max(groups.values())))

    @classmethod
    def _engine_key(cls, inst):
        """实例名 → (适配器, 物理卡, 引擎类型)，用于把同一引擎上的多进程合并。

        去掉 pid 是关键：同一个引擎被多个进程占用时，各进程各占一份，
        相加才是这个引擎真正的繁忙程度。
        """
        luid = cls._RE_LUID.search(inst or "")
        phys = cls._RE_PHYS.search(inst or "")
        eng = cls._RE_ENG.search(inst or "")
        return (luid.group(1).upper() if luid else "",
                phys.group(1) if phys else "",
                eng.group(1).lower() if eng else "")

    @staticmethod
    def _as_mb(value):
        """PDH 的显存计数器单位是字节，少数系统给 MB，按量级归一。"""
        return (round(value / 1048576.0, 1) if value > 1_000_000
                else round(value, 1))


def _reg_dedicated_vram_mb():
    """从注册表读专用显存总量（MB），取所有适配器中最大的。

    PDH 没有 "Dedicated Limit" 计数器（本机实测 PdhAddCounterW 直接返回
    PDH_CSTATUS_NO_COUNTER），显存总量只能走注册表——与 hardware.py 采集
    显卡信息用的是同一个数据源（HardwareInformation.qwMemorySize，64 位，
    不会有 AdapterRAM 那种 4GB 溢出问题）。
    """
    try:
        sizes = _reg_vram_sizes()
    except Exception:
        return None
    if not sizes:
        return None
    return round(max(size for _n, size in sizes) / 1048576.0, 1)


_PDH_READER = None
_PDH_LOCK = Lock()


def read_gpu_pdh():
    """用 PDH 读 GPU 利用率 + 显存（AMD / Intel / NVIDIA 全通用）。

    返回 dict 或 None。与 read_gpu() 同构，但 temp / power / clock 为 None
    （PDH 拿不到这些厂商私有的传感器指标），UI 需据此降级显示。
    """
    global _PDH_READER
    with _PDH_LOCK:
        if _PDH_READER is None:
            _PDH_READER = PdhGpuReader()
        return _PDH_READER.read()


# 所有厂商都读不到的项一律 None —— UI 据此显示"—"而不是编一个 0
_GPU_FIELDS = ("temp", "power_w", "power_limit_w", "clock_sm_mhz",
               "clock_max_mhz", "fan_percent", "mem_used_mb", "mem_total_mb")


def read_gpu():
    """显卡实时数据的统一入口：厂商层（NVML / ADL）+ 通用层（PDH）。

    字段优先级：
      温度 / 功耗 / 频率 / 风扇   gpu_sensors（NVML → ADL）→ nvidia-smi
      利用率                     PDH → 厂商值
      显存用量 / 总量            NVML → PDH；总量还读不到就用注册表

    全部读不到返回 None。注意"厂商层读不到"不等于"没显卡"——A 卡机器上
    ADL 可用时温度功耗照样能读，这正是以前只有 N 卡才有温度的原因。
    """
    vend = {}
    if gpu_sensors is not None:
        try:
            vend = gpu_sensors.read_sensors() or {}
        except Exception:
            vend = {}
    smi = None
    if vend.get("source") != "nvml":
        # NVML 没拿到（非 N 卡，或老驱动没带 nvml.dll）→ 老路兜一层
        smi = read_gpu_nvsmi()
    pdh = read_gpu_pdh()
    if not vend and not smi and not pdh:
        return None

    def pick(key):
        for src in (vend, smi, pdh):
            if src and src.get(key) is not None:
                return src[key]
        return None

    out = {key: pick(key) for key in _GPU_FIELDS}
    if out["mem_total_mb"] is None:
        out["mem_total_mb"] = _reg_dedicated_vram_mb()

    # 占用率统一用 PDH 口径（与任务管理器一致）。NVML / nvidia-smi 报的是
    # "忙碌时间占比"，桌面待机时能到 20~30%、任务管理器只有个位数，
    # 拿它当主值会让用户以为读错了。只有 PDH 完全读不到时才退回厂商值，
    # 并如实标注来源，便于排查。
    if pdh and pdh.get("util") is not None:
        out["util"], out["util_source"] = pdh["util"], "pdh"
    else:
        out["util"] = pick("util")
        out["util_source"] = ((vend.get("source") or "nvidia-smi")
                              if out["util"] is not None else "")

    out["sensor_source"] = vend.get("source") or ("nvidia-smi" if smi else "")
    out["gpu_name"] = vend.get("name") or ""
    return out


# ---------------------------------------------------------------------------
# 采样线程
# ---------------------------------------------------------------------------
class LiveMonitor(QObject):
    """定时采样 CPU / 内存 / GPU / 磁盘，通过信号把结果推给 UI。

    用法：
        mon = LiveMonitor(interval=1.0)
        mon.sample.connect(self._on_sample)     # dict
        mon.start()
        ...
        mon.stop()
    """

    sample = Signal(dict)

    def __init__(self, interval=1.0, drive="C:\\", enable_gpu=True, parent=None):
        super().__init__(parent)
        self._interval = max(0.2, float(interval))
        self._drive = drive
        self._enable_gpu = enable_gpu
        self._stop_evt = Event()
        self._thread = None
        self._cpu = CpuSampler()
        self._tick = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = Thread(target=self._run, name="YuhubLiveMonitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_evt.set()

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        # GPU 采样比 CPU/内存慢得多（nvidia-smi ~240ms、PDH 两次 collect
        # 至少 0.2s，对比 CPU/内存 ~0.03ms），因此 GPU 每 N 个 tick 采一次，
        # 中间沿用上次结果，避免拖慢整体节奏。
        gpu_period = 2
        # 连续读不到 GPU 时逐步拉长重试间隔，但**永不彻底放弃**：
        # 显卡驱动晚一点就绪、A 卡用户插上独显之后，都能自动恢复。
        gpu_backoff = gpu_period
        gpu_fails = 0
        last_gpu = None

        while not self._stop_evt.is_set():
            t_start = time.monotonic()
            payload = {"ts": time.time()}

            cpu_pct, ok = self._cpu.sample()
            payload["cpu"] = {"percent": cpu_pct, "valid": ok}

            mem = read_memory()
            if mem:
                payload["mem"] = mem

            if self._enable_gpu and self._tick % gpu_backoff == 0:
                # read_gpu 内部已按「厂商层 → 通用层」组合好，并统一了
                # 利用率口径，这里只负责失败退避。
                g = read_gpu()
                if g is None:
                    gpu_fails += 1
                    gpu_backoff = min(120, gpu_period * (2 ** min(gpu_fails, 6)))
                    if gpu_fails >= 6:
                        # 连续失败足够久才认定"这台机器读不到 GPU"，
                        # 否则偶发一轮抖动会让瓦片闪一下"未检测到"。
                        last_gpu = None
                else:
                    gpu_fails = 0
                    gpu_backoff = gpu_period
                    last_gpu = g
            if last_gpu:
                payload["gpu"] = last_gpu

            if self._tick % 5 == 0:
                disk = read_disk_space(self._drive)
                if disk:
                    payload["disk"] = disk

            self._tick += 1
            self.sample.emit(payload)

            # 补偿采样自身耗时，尽量贴近目标间隔
            elapsed = time.monotonic() - t_start
            self._stop_evt.wait(max(0.05, self._interval - elapsed))


# ---------------------------------------------------------------------------
# 详情页数据（按需调 PowerShell，不做实时刷新）
# ---------------------------------------------------------------------------
_DETAIL_CACHE = {}


def query_component_detail(kind, timeout=40):
    """按需查询某个部件的详细参数（生产商 / 序列号 / 插槽 / 固件等）。

    这些都是**静态或低频变化**的数据，只在用户点开详情时查一次并缓存，
    不参与每秒刷新，所以可以用较慢但信息更全的 PowerShell / CIM。
    """
    if kind in _DETAIL_CACHE:
        return _DETAIL_CACHE[kind]

    scripts = {
        "cpu": (
            "Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,"
            "NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,CurrentClockSpeed,"
            "L2CacheSize,L3CacheSize,SocketDesignation,ProcessorId,Architecture,"
            "DataWidth,LoadPercentage,CurrentVoltage | ConvertTo-Json -Compress"
        ),
        "gpu": (
            "$v=@(Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,"
            "DriverDate,VideoProcessor,AdapterCompatibility,PNPDeviceID,Status,"
            "CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate);"
            "ConvertTo-Json -Compress -Depth 4 -InputObject @{controllers=$v}"
        ),
        "ram": (
            "$m=Get-CimInstance Win32_PhysicalMemory | Select-Object BankLabel,DeviceLocator,"
            "Manufacturer,PartNumber,SerialNumber,Capacity,Speed,ConfiguredClockSpeed,"
            "SMBIOSMemoryType,FormFactor;"
            "$a=@(Get-CimInstance Win32_PhysicalMemoryArray);"
            "ConvertTo-Json -Compress -Depth 4 -InputObject @{modules=@($m);"
            " slots=$a.MemoryDevices; maxCapKB=$a.MaxCapacityEx}"
        ),
        "disk": (
            "$d=@(Get-CimInstance Win32_DiskDrive | Select-Object Model,SerialNumber,"
            "FirmwareRevision,Size,InterfaceType,Partitions,Status);"
            "$p=@(Get-CimInstance -Namespace root\\Microsoft\\Windows\\Storage MSFT_PhysicalDisk | "
            "Select-Object FriendlyName,SerialNumber,MediaType,BusType,HealthStatus,Size);"
            "ConvertTo-Json -Compress -Depth 4 -InputObject @{drives=$d; physical=$p}"
        ),
        "os": (
            "ConvertTo-Json -Compress -InputObject @{"
            "os=(Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,"
            "BuildNumber,OSArchitecture,InstallDate,LastBootUpTime,SerialNumber,"
            "WindowsDirectory,SystemDrive);"
            "board=(Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product,"
            "SerialNumber,Version);"
            "bios=(Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion,"
            "ReleaseDate,SerialNumber,Version)}"
        ),
    }

    script = scripts.get(kind)
    if not script:
        return None

    if not IS_WIN:
        return None

    import json

    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + script],
            capture_output=True,
            timeout=timeout,
            creationflags=flags,
        )
        raw = proc.stdout.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
        raw = raw[raw.find("{"):] or raw[raw.find("["):]
        if not raw:
            return None
        if raw.startswith("{"):
            data = json.loads(raw)
        else:
            data = json.loads(raw)
        _DETAIL_CACHE[kind] = data
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 展示辅助
# ---------------------------------------------------------------------------
_VRAM_CACHE = {"data": None}


def _reg_vram_sizes():
    """从注册表读取各显示适配器的真实显存（字节）。

    必需的原因：Win32_VideoController.AdapterRAM 是 32 位有符号整数，
    显存 >4GB 时会溢出成错误值。注册表的 qwMemorySize 是 64 位，准确。
    结果缓存，因为驱动信息在一次运行内不会变。
    """
    if _VRAM_CACHE["data"] is not None:
        return _VRAM_CACHE["data"]
    if not IS_WIN:
        _VRAM_CACHE["data"] = []
        return []

    import re
    import winreg

    sizes = []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not re.fullmatch(r"\d{4}", sub):
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name = None
                        try:
                            name, _ = winreg.QueryValueEx(k, "DriverDesc")
                        except OSError:
                            pass
                        try:
                            qw, _ = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")
                            if name:
                                sizes.append((name, int(qw)))
                        except OSError:
                            pass
                except OSError:
                    continue
    except OSError:
        pass

    _VRAM_CACHE["data"] = sizes
    return sizes


def fmt_bytes(n, digits=1):
    """字节 → 易读字符串。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    if n <= 0:
        return "--"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            if unit in ("B", "KB", "MB"):
                return f"{n:.0f} {unit}"
            return f"{n:.{digits}f} {unit}"
        n /= 1024
    return "--"


SMBIOS_MEM_TYPE = {
    20: "DDR", 21: "DDR2", 22: "DDR2 FB-DIMM", 24: "DDR3",
    26: "DDR4", 27: "LPDDR", 28: "LPDDR2", 29: "LPDDR3",
    30: "LPDDR4", 34: "DDR5", 35: "LPDDR5", 36: "LPDDR5X",
}

FORM_FACTOR = {
    1: "其他", 2: "未知", 3: "SIMM", 4: "SIP", 5: "芯片", 6: "DIP",
    7: "ZIP", 8: "金手指", 9: "DIMM", 10: "TSOP", 11: "SIP",
    12: "SODIMM", 13: "SRIMM", 14: "SMD", 15: "SSMP", 16: "QFP",
}

ARCH_MAP = {0: "x86", 1: "MIPS", 2: "Alpha", 3: "PowerPC", 5: "ARM", 6: "安腾",
            9: "x64", 12: "ARM64"}

HEALTH_MAP = {0: "健康", 1: "警告", 2: "不健康", 3: "未知"}

BUS_TYPE = {1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "IEEE1394", 5: "SSA", 6: "光纤",
            7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD",
            13: "MMC", 14: "虚拟", 15: "文件支持", 16: "存储空间", 17: "NVMe",
            18: "SCM", 19: "UFS"}


def describe_media(mediatype):
    return {3: "HDD（机械硬盘）", 4: "SSD（固态硬盘）", 5: "SCM"}.get(mediatype, "未知")
