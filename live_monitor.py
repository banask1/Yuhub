r"""Yuhub 实时硬件监控（完全离线，性能优先）。

为什么不用 PowerShell 做实时刷新：
  PowerShell 进程启动 + Get-Counter 首次采样约 **4 秒**，每秒刷新完全不可用。
  实测对比（本机 i5-12450H）：
    - ctypes 直调 GetSystemTimes 差分求 CPU 占用 → ~0.03 ms
    - ctypes 直调 GlobalMemoryStatusEx 求内存占用 → ~0.03 ms
    - nvidia-smi 子进程求 GPU 占用/温度/功耗     → ~240 ms
  因此：CPU / 内存 / 磁盘容量走 ctypes（微秒级，可在主线程直接调）；
  GPU 走 nvidia-smi，且必须放到后台线程，避免阻塞 UI。

线程模型：
  LiveMonitor 内部维护一个守护线程，按 interval 采样并发出 sample 信号。
  nvidia-smi 与主线程的 CPU/内存采样分离——前者慢、后者快，不能互相拖累。
"""

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from threading import Event, Thread

from PySide6.QtCore import QObject, Signal

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
# GPU：nvidia-smi
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


def read_gpu():
    """读取 NVIDIA GPU 实时数据。

    返回 dict 或 None（无 NVIDIA 显卡 / 驱动未装 / 调用失败）。
    非 NVIDIA 显卡（AMD/Intel）此函数返回 None，UI 需自行降级显示。
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
        self._gpu_fail_streak = 0
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
        # GPU 采样比 CPU/内存慢得多（~240ms vs ~0.03ms），
        # 因此 GPU 每 N 个 tick 采一次，中间沿用上次结果，避免拖慢整体节奏。
        gpu_period = 2
        last_gpu = None

        while not self._stop_evt.is_set():
            t_start = time.monotonic()
            payload = {"ts": time.time()}

            cpu_pct, ok = self._cpu.sample()
            payload["cpu"] = {"percent": cpu_pct, "valid": ok}

            mem = read_memory()
            if mem:
                payload["mem"] = mem

            if self._enable_gpu and self._gpu_fail_streak < 5:
                if self._tick % gpu_period == 0:
                    g = read_gpu()
                    if g is None:
                        self._gpu_fail_streak += 1
                        last_gpu = None
                    else:
                        self._gpu_fail_streak = 0
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
