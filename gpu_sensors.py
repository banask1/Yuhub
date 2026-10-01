# -*- coding: utf-8 -*-
"""显卡厂商私有传感器：温度 / 功耗 / 频率 / 风扇。

为什么单独一个模块
------------------
Windows 的 PDH 性能计数器（见 live_monitor.PdhGpuReader）由 WDDM 图形内核
上报，AMD / Intel / NVIDIA 全通用，但它**只有利用率与显存**——温度和功耗
属于厂商私有传感器，没有统一 API，各家一张牌：

    NVIDIA → NVML   nvml.dll（显卡驱动自带，无子进程开销）
    AMD    → ADL    atiadlxx.dll（GPU-Z / LibreHardwareMonitor 走的同一条路）
    Intel  → 官方 IGCL / Level Zero —— 需要在 Intel 机器上实测，暂未覆盖

对外只有一个入口 `read_sensors()`：返回"尽力而为"的字段集合，读不到的项
一律 None，**绝不编造数值**（假 0 比"读不到"更难排查）。

单位换算的依据（都有官方文档 / 成熟参考实现可查）
------------------------------------------------
- `ADL2_OverdriveN_Temperature_Get` / `ADL2_Overdrive6_Temperature_Get`：
  AMD 官方文档写明温度是**毫摄氏度**（milli-degrees Celsius）→ /1000
- `ADL2_Overdrive6_CurrentPower_Get`：功耗单位是 **1/256 瓦** → /256
  （OSHI 与 LibreHardwareMonitor 两个独立实现都是这个换算）
- NVML：`nvmlDeviceGetPowerUsage` 返回**毫瓦** → /1000
"""

import ctypes
import sys
import threading

IS_WIN = sys.platform == "win32"

# 已知的"读不到"哨兵值：部分 A 卡传感器不可用时返回 0 或 54000 毫度
_OD_TEMP_MIN = 1.0          # 摄氏度下限（0 及以下一律视为无效）
_OD_TEMP_MAX = 150.0        # 摄氏度上限（超过就是驱动给的垃圾值）
_OD_POWER_MIN = 0.5         # 瓦特下限
_OD_POWER_MAX = 1000.0      # 瓦特上限


# ---------------------------------------------------------------------------
# 单位换算（抽成纯函数，方便自检里逐条钉死）
# ---------------------------------------------------------------------------
def adl_milli_celsius(raw):
    """ADL 的毫摄氏度 → 摄氏度。超出合理范围返回 None。

    门槛不是"洁癖"：AMD 驱动在温度传感器不可用时会把 0 或一个巨大的
    垃圾值（社区实测有 54000）填进来，直接除 1000 会得到"0°C"或
    "54°C"这种看着很正常、实际是编出来的数字。
    """
    try:
        v = float(raw) / 1000.0
    except (TypeError, ValueError):
        return None
    if _OD_TEMP_MIN <= v <= _OD_TEMP_MAX:
        return round(v, 1)
    return None


def adl_od6_watts(raw):
    """Overdrive6 的功耗原始值 → 瓦特（单位 1/256 W）。"""
    try:
        v = float(raw) / 256.0
    except (TypeError, ValueError):
        return None
    if _OD_POWER_MIN <= v <= _OD_POWER_MAX:
        return round(v, 1)
    return None


def _sane_temp(v):
    """统一给所有来源的温度做一次合理性检查。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if _OD_TEMP_MIN <= v <= _OD_TEMP_MAX else None


def _sane_watts(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if _OD_POWER_MIN <= v <= _OD_POWER_MAX else None


# ---------------------------------------------------------------------------
# NVIDIA：NVML
# ---------------------------------------------------------------------------
class _Nvml:
    """nvml.dll 直调。比 nvidia-smi 子进程快两个数量级（~1ms vs ~240ms），
    而且不依赖 PATH 里有没有 nvidia-smi.exe。"""

    # NVML 常量
    TEMP_GPU = 0                 # NVML_TEMPERATURE_GPU
    CLOCK_SM = 1                 # NVML_CLOCK_SM

    def __init__(self):
        self._lib = None
        self._ok = None          # None=未试过 / False=不可用 / True=可用
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 加载
    def _ensure(self):
        if self._ok is not None:
            return self._ok
        with self._lock:
            if self._ok is not None:
                return self._ok
            self._ok = False
            if not IS_WIN:
                return False
            try:
                lib = ctypes.WinDLL("nvml.dll")
            except OSError:
                return False
            try:
                ctx = ctypes.c_void_p
                u = ctypes.c_uint
                lib.nvmlInit_v2.restype = ctypes.c_int
                if lib.nvmlInit_v2() != 0:
                    return False
                lib.nvmlDeviceGetCount_v2.restype = ctypes.c_int
                lib.nvmlDeviceGetCount_v2.argtypes = [ctypes.POINTER(u)]
                lib.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
                lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [
                    u, ctypes.POINTER(ctx)]
                lib.nvmlDeviceGetName.argtypes = [ctx, ctypes.c_char_p, u]
                lib.nvmlDeviceGetTemperature.argtypes = [
                    ctx, ctypes.c_int, ctypes.POINTER(u)]
                lib.nvmlDeviceGetPowerUsage.argtypes = [ctx, ctypes.POINTER(u)]
                lib.nvmlDeviceGetEnforcedPowerLimit.argtypes = [
                    ctx, ctypes.POINTER(u)]
                lib.nvmlDeviceGetClockInfo.argtypes = [
                    ctx, ctypes.c_int, ctypes.POINTER(u)]
                lib.nvmlDeviceGetMaxClockInfo.argtypes = [
                    ctx, ctypes.c_int, ctypes.POINTER(u)]
                lib.nvmlDeviceGetFanSpeed.argtypes = [ctx, ctypes.POINTER(u)]
                lib.nvmlShutdown.restype = ctypes.c_int
            except AttributeError:
                return False
            self._lib = lib
            self._ok = True
            return True

    # ------------------------------------------------------------------ 读取
    def snapshot(self):
        """返回 dict（至少含 source="nvml"），完全不可用时返回 None。"""
        if not self._ensure():
            return None
        lib = self._lib

        n = ctypes.c_uint(0)
        if lib.nvmlDeviceGetCount_v2(ctypes.byref(n)) != 0 or n.value == 0:
            return None

        for idx in range(min(n.value, 8)):
            dev = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(
                    idx, ctypes.byref(dev)) != 0 or not dev.value:
                continue

            def uint(fn, *args):
                v = ctypes.c_uint(0)
                try:
                    if fn(dev, *args, ctypes.byref(v)) != 0:
                        return None
                except Exception:
                    return None
                return v.value

            temp = _sane_temp(uint(lib.nvmlDeviceGetTemperature, self.TEMP_GPU)
                              or -1)
            if temp is None:
                continue        # 这块卡给不出温度 → 试下一块

            name = ctypes.create_string_buffer(96)
            try:
                lib.nvmlDeviceGetName(dev, name, 96)
                name_txt = name.value.decode("utf-8", "replace")
            except Exception:
                name_txt = ""

            mw = uint(lib.nvmlDeviceGetPowerUsage)
            mw_max = uint(lib.nvmlDeviceGetEnforcedPowerLimit)
            mem_used = mem_total = None
            try:
                class _Mem(ctypes.Structure):
                    _fields_ = [("total", ctypes.c_ulonglong),
                                ("free", ctypes.c_ulonglong),
                                ("used", ctypes.c_ulonglong)]
                lib.nvmlDeviceGetMemoryInfo.argtypes = [
                    ctypes.c_void_p, ctypes.POINTER(_Mem)]
                mi = _Mem()
                if lib.nvmlDeviceGetMemoryInfo(dev, ctypes.byref(mi)) == 0:
                    mem_used = round(mi.used / 1048576.0, 1)
                    mem_total = round(mi.total / 1048576.0, 1)
            except Exception:
                pass

            util = None
            try:
                class _Util(ctypes.Structure):
                    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]
                lib.nvmlDeviceGetUtilizationRates.argtypes = [
                    ctypes.c_void_p, ctypes.POINTER(_Util)]
                ut = _Util()
                if lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(ut)) == 0:
                    util = float(ut.gpu)
            except Exception:
                pass

            return {
                "source": "nvml",
                "vendor": "nvidia",
                "name": name_txt,
                "temp": temp,
                "power_w": _sane_watts(mw / 1000.0) if mw else None,
                "power_limit_w": _sane_watts(mw_max / 1000.0) if mw_max else None,
                "clock_sm_mhz": float(uint(lib.nvmlDeviceGetClockInfo,
                                           self.CLOCK_SM) or 0) or None,
                "clock_max_mhz": float(uint(lib.nvmlDeviceGetMaxClockInfo,
                                            self.CLOCK_SM) or 0) or None,
                "fan_percent": float(uint(lib.nvmlDeviceGetFanSpeed) or 0) or None,
                "mem_used_mb": mem_used,
                "mem_total_mb": mem_total,
                # NVML 的利用率是"过去一段采样期内有任务在跑的时间占比"，
                # 与任务管理器的"引擎占用率"不同口径 → 只在 PDH 不可用时兜底
                "util": util,
                "util_source": "nvml",
            }
        return None


# ---------------------------------------------------------------------------
# AMD：ADL
# ---------------------------------------------------------------------------
# ADL 要求调用方提供内存分配回调（内部有 C 侧分配），回调返回的缓冲区必须
# 由 Python 侧持有引用，否则被 GC 回收后驱动就往野指针写。
_MALLOC_KEEP = []


def _make_malloc_cb():
    if not IS_WIN:
        return None
    proto = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int)

    def _malloc(size):
        buf = ctypes.create_string_buffer(int(size) or 1)
        _MALLOC_KEEP.append(buf)
        return ctypes.addressof(buf)

    return proto(_malloc)


class _Adl:
    """atiadlxx.dll（AMD Display Library）。

    只读 Overdrive 的**温度**与 **功耗**，不碰任何 Set 接口——
    yuhub 不该改用户的风扇 / 功耗墙。
    """

    ADL_OK = 0
    OVERDRIVE_TEMP_EDGE = 1      # ADL_OVERDRIVE_TEMPERATURE_EDGE
    OD6_CURRENT_POWER = 0        # ADL_OVERDRIVE6_CURRENTPOWER

    def __init__(self):
        self._lib = None
        self._ctx = None
        self._ok = None
        self._pick = None        # 选定的适配器序号
        self._last_error = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 加载
    def _ensure(self):
        if self._ok is not None:
            return self._ok
        with self._lock:
            if self._ok is not None:
                return self._ok
            self._ok = False
            if not IS_WIN:
                return False
            lib = None
            for dll in ("atiadlxx.dll", "atiadlxy.dll"):
                try:
                    lib = ctypes.WinDLL(dll)
                    break
                except OSError as exc:
                    self._last_error = "%s: %s" % (dll, exc)
            if lib is None:
                return False
            try:
                cb = _make_malloc_cb()
                if cb is None:
                    return False
                lib.ADL2_Main_Control_Create.argtypes = [
                    type(cb), ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
                lib.ADL2_Main_Control_Create.restype = ctypes.c_int
                lib.ADL2_Adapter_NumberOfAdapters_Get.argtypes = [
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
                lib.ADL2_Adapter_Active_Get.argtypes = [
                    ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
                lib.ADL2_Overdrive_Caps.argtypes = [
                    ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            except AttributeError as exc:
                self._last_error = "ADL 导出函数缺失: %s" % exc
                return False

            ctx = ctypes.c_void_p()
            rc = lib.ADL2_Main_Control_Create(
                cb, 1, ctypes.byref(ctx))      # 1 = 只枚举已连接的适配器
            if rc != self.ADL_OK or not ctx:
                self._last_error = "ADL2_Main_Control_Create rc=%s" % rc
                return False

            # 可选接口：不同 ADL 版本导出的函数不一样，缺了不影响其它项
            for name, argtypes in (
                ("ADL2_OverdriveN_Temperature_Get",
                 [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                  ctypes.POINTER(ctypes.c_int)]),
                ("ADL2_Overdrive6_Temperature_Get",
                 [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]),
                ("ADL2_Overdrive6_CurrentPower_Get",
                 [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                  ctypes.POINTER(ctypes.c_int)]),
            ):
                fn = getattr(lib, name, None)
                if fn is not None:
                    fn.argtypes = argtypes
                    fn.restype = ctypes.c_int

            self._lib, self._ctx, self._ok = lib, ctx, True
            return True

    # ------------------------------------------------------------------ 读取
    def _caps(self, idx):
        """(是否支持 Overdrive, 版本号)。版本号 >= 7 用 ODN，>= 6 有功耗。"""
        sup, en, ver = ctypes.c_int(0), ctypes.c_int(0), ctypes.c_int(0)
        fn = getattr(self._lib, "ADL2_Overdrive_Caps", None)
        if fn is None:
            return False, 0
        try:
            if fn(self._ctx, idx, ctypes.byref(sup), ctypes.byref(en),
                  ctypes.byref(ver)) != self.ADL_OK:
                return False, 0
        except Exception:
            return False, 0
        return sup.value != 0, ver.value

    def _temperature(self, idx, version):
        """按 API 世代依次尝试：ODN(v7+) → OD6 → OD5 需要的结构更老，跳过。"""
        raw = ctypes.c_int(0)
        if version >= 7:
            fn = getattr(self._lib, "ADL2_OverdriveN_Temperature_Get", None)
            if fn is not None:
                try:
                    if fn(self._ctx, idx, self.OVERDRIVE_TEMP_EDGE,
                          ctypes.byref(raw)) == self.ADL_OK:
                        v = adl_milli_celsius(raw.value)
                        if v is not None:
                            return v, "adl-odn"
                except Exception:
                    pass
        fn = getattr(self._lib, "ADL2_Overdrive6_Temperature_Get", None)
        if fn is not None:
            try:
                if fn(self._ctx, idx, ctypes.byref(raw)) == self.ADL_OK:
                    v = adl_milli_celsius(raw.value)
                    if v is not None:
                        return v, "adl-od6"
            except Exception:
                pass
        return None, ""

    def _power(self, idx):
        fn = getattr(self._lib, "ADL2_Overdrive6_CurrentPower_Get", None)
        if fn is None:
            return None
        raw = ctypes.c_int(0)
        try:
            if fn(self._ctx, idx, self.OD6_CURRENT_POWER,
                  ctypes.byref(raw)) == self.ADL_OK:
                return adl_od6_watts(raw.value)
        except Exception:
            pass
        return None

    def snapshot(self):
        """在活动适配器里挑一块能给出读数的，返回 dict；全都不行返回 None。"""
        if not self._ensure():
            return None
        n = ctypes.c_int(0)
        try:
            if self._lib.ADL2_Adapter_NumberOfAdapters_Get(
                    self._ctx, ctypes.byref(n)) != self.ADL_OK:
                return None
        except Exception:
            return None

        # 已经选定的适配器优先（多卡机器上避免每轮在两张卡之间跳）
        order = list(range(max(0, min(n.value, 32))))
        if self._pick in order:
            order.remove(self._pick)
            order.insert(0, self._pick)

        for idx in order:
            act = ctypes.c_int(0)
            try:
                if self._lib.ADL2_Adapter_Active_Get(
                        self._ctx, idx, ctypes.byref(act)) != self.ADL_OK:
                    continue
            except Exception:
                continue
            if not act.value:
                continue                       # 未连接的适配器不读
            supported, version = self._caps(idx)
            # 注意：这里**不因为** Overdrive 被关掉就跳过——部分驱动在
            # Overdrive 关闭时仍能报温度，而"跳过"会让用户彻底看不到读数。
            # 真正决定用哪一代 API 的是版本号（版本为 0 时只试 OD6，
            # 不去碰 ODN：有些卡在不支持的型号上调 ODN 会回填 54000 这种
            # 哨兵值，除 1000 后看着像正常的 54°C）。
            temp, src = self._temperature(idx, version)
            if temp is None:
                continue
            self._pick = idx
            return {
                "source": src,
                "vendor": "amd",
                "name": "",
                "temp": temp,
                "power_w": self._power(idx),
                "power_limit_w": None,
                "clock_sm_mhz": None,
                "clock_max_mhz": None,
                "fan_percent": None,
                "mem_used_mb": None,
                "mem_total_mb": None,
                "util": None,
                "util_source": "",
                "adl_version": version,
                "od_supported": bool(supported),
                "adapter_index": idx,
            }
        return None

    def diagnostics(self):
        """给自检用的诊断信息（不抛异常）。"""
        info = {"dll_loaded": False, "context": False, "adapters": None,
                "active": [], "caps": {}, "error": self._last_error}
        try:
            if not self._ensure():
                return info
            info["dll_loaded"] = True
            info["context"] = True
            n = ctypes.c_int(0)
            self._lib.ADL2_Adapter_NumberOfAdapters_Get(
                self._ctx, ctypes.byref(n))
            info["adapters"] = n.value
            for idx in range(max(0, min(n.value, 8))):
                act = ctypes.c_int(0)
                self._lib.ADL2_Adapter_Active_Get(
                    self._ctx, idx, ctypes.byref(act))
                if not act.value:
                    continue
                sup, ver = self._caps(idx)
                temp, src = self._temperature(idx, ver)
                info["active"].append({
                    "index": idx,
                    "od_supported": bool(sup),
                    "od_version": ver,
                    "temp": temp,
                    "temp_source": src,
                    "power_w": self._power(idx),
                })
        except Exception as exc:
            info["error"] = repr(exc)
        return info


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
_NVML = None
_ADL = None
_LOCK = threading.Lock()


def _providers():
    global _NVML, _ADL
    with _LOCK:
        if _NVML is None:
            _NVML = _Nvml()
        if _ADL is None:
            _ADL = _Adl()
    return _NVML, _ADL


def read_sensors():
    """读取当前主显卡的厂商传感器。

    返回 dict（可能只有部分字段，读不到的都是 None）；两个厂商都不可用时
    返回 {}。绝不抛异常——它跑在实时采样线程里。
    """
    nvml, adl = _providers()
    # N 卡优先：笔记本上"AMD 核显 + NVIDIA 独显"很常见，用户关心的是独显
    for provider in (nvml, adl):
        try:
            data = provider.snapshot()
        except Exception:
            data = None
        if data:
            return data
    return {}


def diagnostics():
    """自检用：两个厂商接口各自的可用性与实测读数。"""
    nvml, adl = _providers()
    out = {"nvml": {"available": False}, "adl": adl.diagnostics()}
    try:
        snap = nvml.snapshot()
    except Exception as exc:
        snap, out["nvml"]["error"] = None, repr(exc)
    if snap:
        out["nvml"] = {
            "available": True,
            "name": snap.get("name"),
            "temp": snap.get("temp"),
            "power_w": snap.get("power_w"),
            "mem_used_mb": snap.get("mem_used_mb"),
            "mem_total_mb": snap.get("mem_total_mb"),
        }
    else:
        out["nvml"]["available"] = False
    return out
