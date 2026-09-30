"""本机 CPU 核心 / 线程数——**唯一数据源**。

为什么单独拆一个模块：
首页 CPU 面板原本读 WMI（`Win32_Processor`），下载页原本读 `os.cpu_count()` /
`GetLogicalProcessorInformationEx`。两边各自取值，于是出现过一个被用户当 bug 报回来的
现象——首页写「8 核 12 线程」、下载页写「12 核」。用户合理地认为程序认错了 CPU。

同一个机器事实在多个页面出现时，必须**同一数据源 + 同一套措辞**。
所以这里集中实现，`hardware.py`（首页）与 `downloader.py`（下载页）都从这里取。

术语（别混，这是那次 bug 的根源）：
    物理核心数   = 实际核心个数。i5-12450H 上是 8（4 个 P 核 + 4 个 E 核）
    逻辑处理器数 = 含超线程的硬件线程数。同一颗 CPU 上是 12
    os.cpu_count() 返回的是**逻辑处理器数**，不是核心数。

全部离线：只走 ctypes + os，不起子进程、不联网、不读注册表。
"""

import os

_CORES_CACHE = None


def _win_physical_cores():
    """Windows：物理核心数（不含超线程的兄弟逻辑处理器）。

    `GetLogicalProcessorInformationEx(RelationProcessorCore)` 会**为每个物理核心**
    返回一条记录：带超线程的核心，其 GroupMask 掩码里有 2 个以上置位。
    所以「记录条数 = 物理核心数」、「掩码位数之和 = 逻辑处理器数」。

    两个必须注意的点（错了会静默给出错误答案）：
      1. 必须用 `RelationProcessorCore(0)`。用 `RelationAll(0xFFFF)` 会把缓存 /
         NUMA 节点等记录也返回，条数就不是核心数了。
      2. 步长必须读头里的 `Size` 字段，不能自己算 sizeof：`PROCESSOR_RELATIONSHIP`
         有对齐填充（实际 40 字节），外层结构 48 字节，手算会错位到垃圾数据。
    """
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        CORE = 0        # RelationProcessorCore

        class GROUP_AFFINITY(ctypes.Structure):
            _fields_ = [("Mask", ctypes.c_size_t),
                        ("Group", wintypes.WORD),
                        ("Reserved", wintypes.WORD * 3)]

        class PROCESSOR_RELATIONSHIP(ctypes.Structure):
            _fields_ = [("Flags", ctypes.c_ubyte),
                        ("EfficiencyClass", ctypes.c_ubyte),
                        ("Reserved", ctypes.c_ubyte * 20),
                        ("GroupCount", wintypes.WORD),
                        ("GroupMask", GROUP_AFFINITY * 1)]

        class SLPI(ctypes.Structure):
            _fields_ = [("Relationship", ctypes.c_uint),
                        ("Size", ctypes.c_uint),
                        ("Processor", PROCESSOR_RELATIONSHIP)]

        fn = ctypes.windll.kernel32.GetLogicalProcessorInformationEx
        fn.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        fn.restype = wintypes.BOOL

        need = wintypes.DWORD(0)
        fn(CORE, None, ctypes.byref(need))       # 先空指针问需要多大缓冲
        if need.value <= 0:
            return 0
        buf = ctypes.create_string_buffer(need.value)
        if not fn(CORE, buf, ctypes.byref(need)):
            return 0

        cores = 0
        offset = 0
        total = int(need.value)
        while offset < total:
            item = ctypes.cast(ctypes.byref(buf, offset), ctypes.POINTER(SLPI)).contents
            step = item.Size or ctypes.sizeof(SLPI)
            if item.Relationship == CORE:
                cores += 1
            offset += step
        return cores if 0 < cores <= 4096 else 0
    except Exception:  # noqa: BLE001
        return 0


def cpu_cores():
    """物理核心数。取不到返回 0（调用方回落到只显示线程数）。

    注意**不要**在这里 import multiprocessing 做兜底：它的 cpu_count() 只是转调
    os.cpu_count()，拿不到核心数时一样拿不到；而一旦 import，PyInstaller 会把整个
    multiprocessing 运行时（含 2 个 runtime hook）打进包，实测白白胖 400 KB。
    """
    global _CORES_CACHE
    if _CORES_CACHE is None:
        _CORES_CACHE = _win_physical_cores()
    return _CORES_CACHE


def cpu_threads():
    """逻辑处理器数（硬件线程）。取不到时保守按 4 处理。"""
    try:
        n = int(os.cpu_count() or 0)
    except Exception:  # noqa: BLE001
        n = 0
    return n if n > 0 else 4


def describe_cpu():
    """「8 核 12 线程」；拿不到核心数时退化成「12 线程」。"""
    threads = cpu_threads()
    cores = cpu_cores()
    return f"{cores} 核 {threads} 线程" if cores else f"{threads} 线程"
