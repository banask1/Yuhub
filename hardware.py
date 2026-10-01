r"""Yuhub 硬件信息采集（完全离线，仅依赖 Windows 自带的 CIM/PowerShell）。

设计要点：
  1. 只读、不修改系统，不联网。
  2. 首次采集在后台线程执行（PowerShell 启动约 1~2 秒），完成后通过信号回主线程。
  3. 结果缓存到 QSettings，二次启动秒开、离线可用。
  4. 每一个字段都做容错：任何一项失败都不影响其余项，缺失则显示 "--"。
  5. 全程强制 UTF-8 输出，避免中文系统上的 GBK 乱码。

分类判定（笔记本 / 台式 / 一体机 / 服务器）：
  - 首选 Win32_ComputerSystem.PCSystemType  （1=Desktop 2=Mobile 3=Workstation 4=Enterprise
    5=SOHO 6=Appliance 7=Performance 8=Slender 9=Convertible 10=Detachable 11=IoT 12=Tablet
    13=Server 14=Docking 15=All-in-One 16=Ultra-Mobile 17=Notebook 18=Space-Saving 19=Main
    20=Expansion 21=Sub-Notebook 22=Stick 23=Stand-Alone 24=Unknown）
  - 交叉校验 Win32_SystemEnclosure.ChassisTypes（8/9/10/11/12/14/18/21/30/31/32 = 便携设备）
  - 两者冲突时，出现任一"便携"信号即判为笔记本（游戏本常被 OEM 标成 ChassisType=10）。
  - 电池存在与否作为第三票，并附带型号关键词兜底。

磁盘类型：优先读 MSFT_PhysicalDisk.MediaType（3=HDD 4=SSD 5=SCM），
  因为 Win32_DiskDrive.Model 常常不含 "SSD" 字样（如 FIREBAT FET34-512G 实为 NVMe SSD）。

显卡显存：Win32_VideoController.AdapterRAM 是 32 位，>4GB 会溢出，改为查注册表
  HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-...}\000N\HardwareInformation.qwMemorySize（64 位）
"""

import ctypes
import json
import os
import re
import subprocess
import sys
from threading import Thread

try:
    import winreg                     # Windows 专有标准库
except ImportError:                   # pragma: no cover - 非 Windows 平台
    winreg = None

PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]

# 一次 PowerShell 调用批量取回全部信息，减少进程启动开销。
# 头部强制 UTF-8 输出编码，避免中文系统上的 GBK 乱码。
_QUERY = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$r = [ordered]@{}
$r.cs      = Get-CimInstance Win32_ComputerSystem
$r.product = Get-CimInstance Win32_ComputerSystemProduct
$r.chassis = Get-CimInstance Win32_SystemEnclosure
$r.cpu     = @(Get-CimInstance Win32_Processor)
$r.gpu     = @(Get-CimInstance Win32_VideoController)
$r.ram     = @(Get-CimInstance Win32_PhysicalMemory)
$r.disk    = @(Get-CimInstance Win32_DiskDrive)
$r.pdisk   = @(Get-CimInstance -Namespace root\Microsoft\Windows\Storage MSFT_PhysicalDisk)
$r.bios    = Get-CimInstance Win32_BIOS
$r.os      = Get-CimInstance Win32_OperatingSystem
$r.board   = Get-CimInstance Win32_BaseBoard
$r.battery = @(Get-CimInstance Win32_Battery)
$out = [ordered]@{}
$out.manufacturer = $r.cs.Manufacturer
$out.model        = $r.cs.Model
$out.family       = $r.cs.SystemFamily
$out.pcsystype    = $r.cs.PCSystemType
$out.totalram     = $r.cs.TotalPhysicalMemory
$out.productname  = $r.product.Name
$out.productver   = $r.product.Version
$out.chassis      = @($r.chassis.ChassisTypes)
$out.cpu          = @($r.cpu | ForEach-Object { [ordered]@{ name=$_.Name; cores=$_.NumberOfCores; threads=$_.NumberOfLogicalProcessors; mhz=$_.MaxClockSpeed; socket=$_.SocketDesignation } })
$out.gpu          = @($r.gpu | ForEach-Object { [ordered]@{ name=$_.Name; vram=$_.AdapterRAM; driver=$_.DriverVersion; w=$_.CurrentHorizontalResolution; h=$_.CurrentVerticalResolution; pnp=$_.PNPDeviceID } })
$out.ram          = @($r.ram | ForEach-Object { [ordered]@{ maker=$_.Manufacturer; cap=$_.Capacity; speed=$_.Speed; part=$_.PartNumber; type=$_.SMBIOSMemoryType } })
$out.disk         = @($r.disk | ForEach-Object { [ordered]@{ model=$_.Model; size=$_.Size; iface=$_.InterfaceType; media=$_.MediaType; pnp=$_.PNPDeviceID } })
$out.pdisk        = @($r.pdisk | ForEach-Object { [ordered]@{ friendly=$_.FriendlyName; mediatype=$_.MediaType; bustype=$_.BusType; size=$_.Size } })
$out.biosver      = $r.bios.SMBIOSBIOSVersion
$out.osname       = $r.os.Caption
$out.osbuild      = $r.os.BuildNumber
$out.osarch       = $r.os.OSArchitecture
$out.boardmaker   = $r.board.Manufacturer
$out.boardname    = $r.board.Product
$out.battery      = @($r.battery)
$out | ConvertTo-Json -Compress -Depth 5
"""

# 精简版：去掉显卡的 WMI 查询。某些 AMD / 老驱动会让
# Win32_VideoController 长时间不返回，把整条查询拖到超时——那样用户看到的
# 是"未检测到硬件信息"整页空白。降级后会走注册表兜底补上显卡。
_QUERY_LITE = _QUERY.replace(
    "$r.gpu     = @(Get-CimInstance Win32_VideoController)",
    "$r.gpu     = @()",
)
assert _QUERY_LITE != _QUERY, "精简查询的替换锚点失效，请同步 _QUERY"


def _run_ps(query=None, timeout=45):
    """执行 PowerShell 查询，返回 dict；失败返回 None。"""
    try:
        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.run(
            PS + [query or _QUERY],
            capture_output=True,
            timeout=timeout,
            creationflags=flags,
        )
        raw = proc.stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            return None
        # PowerShell 可能因编码在开头写入 BOM
        raw = raw.lstrip("\ufeff")
        start = raw.find("{")
        if start > 0:
            raw = raw[start:]
        return json.loads(raw)
    except Exception:
        return None


# 显示适配器类键（{4d36e968-...} 是显卡类 GUID 的固定值）
_REG_GPU_CLASS = (r"SYSTEM\CurrentControlSet\Control\Class"
                  r"\{4d36e968-e325-11ce-bfc1-08002be10318}")


def _reg_value(key, name):
    """读注册表值，不存在返回 None。"""
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _reg_gpus():
    """从注册表枚举显示适配器，返回 [(名称, 显存字节, 驱动版本)]。

    这是显卡信息的**兜底数据源**：部分 AMD 驱动上
    `Get-CimInstance Win32_VideoController` 会返回空数组，或慢到让整条
    PowerShell 查询超时，于是界面显示"未检测到"。而驱动安装时必定会往
    这个类键下写入 DriverDesc（显卡名）、HardwareInformation.qwMemorySize
    （显存，64 位，没有 AdapterRAM 那种 >4GB 溢出）和 DriverVersion。

    只取 \\0000~\\9999 形式的子键——类键下还有 Configuration / Properties
    等非适配器子键，靠"四位数字"这个特征把它们排除掉。
    """
    if winreg is None:
        return []
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REG_GPU_CLASS) as root:
            for index in range(64):
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                if not re.fullmatch(r"\d{4}", sub):
                    continue
                try:
                    with winreg.OpenKey(root, sub) as key:
                        name = (_reg_value(key, "DriverDesc") or "").strip()
                        if not name:
                            continue
                        mem = _reg_value(key,
                                         "HardwareInformation.qwMemorySize")
                        out.append((
                            name,
                            int(mem) if mem else 0,
                            (_reg_value(key, "DriverVersion") or "").strip(),
                        ))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _reg_vram_bytes():
    """从注册表读取各显示适配器的真实显存（解决 AdapterRAM 32 位溢出）。"""
    return [(name, mem) for name, mem, _drv in _reg_gpus() if mem]


def _gpus_from_registry():
    """注册表兜底：WMI 一条显卡都没给时，用驱动注册表信息顶上。

    产出结构与 build_profile 里的 gpu_items 一致，让 UI 不必区分来源。
    """
    items = []
    for name, mem, driver in _reg_gpus():
        vkey, vlabel = gpu_vendor(name)
        items.append({
            "name": name,
            "vram": human_bytes(mem) if mem else "--",
            "driver": driver,
            "resolution": "",
            "vendor": vkey,
            "vendor_label": vlabel,
            "is_virtual": vkey in ("virtual", "microsoft"),
            "from_registry": True,
        })
    # 真实显卡排在虚拟/基础显示适配器前面
    items.sort(key=lambda x: x["is_virtual"])
    return items


# ---------------------------------------------------------------------------
# 数据整理
# ---------------------------------------------------------------------------
CHASSIS_PORTABLE = {8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32}
PC_TYPE_MAP = {
    1: ("desktop", "台式机"),
    2: ("laptop", "笔记本"),
    3: ("desktop", "工作站"),
    4: ("desktop", "企业级主机"),
    5: ("desktop", "小型办公主机"),
    6: ("desktop", "嵌入式设备"),
    7: ("desktop", "高性能主机"),
    8: ("laptop", "轻薄本"),
    9: ("laptop", "翻转本"),
    10: ("laptop", "可拆卸平板"),
    11: ("desktop", "物联网设备"),
    12: ("laptop", "平板电脑"),
    13: ("desktop", "服务器"),
    14: ("laptop", "扩展坞设备"),
    15: ("desktop", "一体机"),
    16: ("laptop", "超便携本"),
    17: ("laptop", "笔记本"),
    18: ("laptop", "节能便携设备"),
    19: ("desktop", "大型主机"),
    20: ("desktop", "扩展主机"),
    21: ("laptop", "亚笔记本"),
    22: ("laptop", "计算棒"),
    23: ("desktop", "独立主机"),
    24: ("unknown", "未知"),
}


def human_bytes(n):
    """把字节数格式化为易读字符串。"""
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
            return f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return "--"


# GPU 厂商判断：优先看 PnP 设备 ID 里的 VEN_ 前缀（最权威），
# 再回落到名称关键词（虚拟/基础显示适配器等特殊情形）。
_GPU_VENDOR_BY_VEN = {
    "10DE": ("nvidia", "NVIDIA"),
    "1002": ("amd", "AMD"),
    "8086": ("intel", "Intel"),
    "1414": ("microsoft", "Microsoft"),
    "1AF4": ("virtio", "Virtual"),
    "15AD": ("vmware", "VMware"),
}
# "基础显示适配器"这类名字是**强信号**：出现它就说明显卡驱动没装好或没生效。
# 中文系统上的名字是"Microsoft 基本显示适配器"，不含英文关键词，所以中英都要有。
_VIRTUAL_NAME_KEYS = ("virtual", "basic display", "remote", "microsoft basic",
                      "standard vga", "displaylink", "rdp",
                      "基本显示适配器", "基础显示适配器", "标准 vga")


def gpu_vendor(name, pnp=""):
    """判断显卡厂商。返回 (key, 中文标签)。

    key ∈ {"amd","nvidia","intel","microsoft","virtual","other"}。

    顺序：名称里的"基础显示适配器"关键词 → PnP 的 VEN_xxxx → 名称厂商词。
    把虚拟/基础显示提到最前，是因为驱动被卸载或没生效时，PnP 里常常还留着
    真实厂商的 VEN_（如 VEN_1002），若按 VEN_ 判成 AMD，就会把它的显存当成
    真实显卡的显存显示出去。
    """
    nl = (name or "").lower()
    for k in _VIRTUAL_NAME_KEYS:
        if k in nl:
            return ("virtual", "虚拟/基础显示")
    if pnp:
        m = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp or "")
        if m:
            hit = _GPU_VENDOR_BY_VEN.get(m.group(1).upper())
            if hit:
                return hit
    if "radeon" in nl or "amd" in nl or "ati" in nl:
        return ("amd", "AMD")
    if "nvidia" in nl or "geforce" in nl or "quadro" in nl or "rtx" in nl or "gtx" in nl:
        return ("nvidia", "NVIDIA")
    if "intel" in nl and "arc" in nl or "iris" in nl or "uhd graphics" in nl or "hd graphics" in nl:
        return ("intel", "Intel")
    return ("other", "其他")


def read_logical_drives():
    """枚举本机所有逻辑盘（含盘符）的容量与占用。

    纯 ctypes 直调（GetLogicalDriveStringsW / GetDriveTypeW /
    GetDiskFreeSpaceExW / GetVolumeInformationW），**不启动子进程、不联网**，
    毫秒级返回，因此可以随硬件面板一起刷新。

    返回 list[dict]：
        letter   盘符，如 "C:"（这就是用户说的"盘号"）
        total/free/used  字节
        percent  已用百分比
        kind     fixed / removable / network / cdrom / unknown
        fs       文件系统（NTFS / exFAT …）
        label    卷标（可能为空）
    """
    drives_out = []
    if os.name != "nt":
        return drives_out
    try:
        k32 = ctypes.windll.kernel32
    except Exception:
        return drives_out

    TYPE_MAP = {
        2: "removable", 3: "fixed", 4: "network",
        5: "cdrom", 6: "ramdisk",
    }

    # 取所有逻辑盘根路径（"C:\\\0D:\\\0…\0\0"）
    buf = ctypes.create_unicode_buffer(512)
    length = k32.GetLogicalDriveStringsW(512, buf)
    if not length:
        return drives_out
    roots = [s for s in buf[:length].split("\x00") if s]

    for root in roots:
        try:
            dtype = k32.GetDriveTypeW(ctypes.c_wchar_p(root))
        except Exception:
            continue
        kind = TYPE_MAP.get(dtype, "unknown")
        # 只列真正有容量的盘（跳过空读卡器 / 未插盘的驱动器）
        if kind in ("cdrom", "unknown", "network"):
            continue

        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        avail = ctypes.c_ulonglong(0)
        ok = k32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(root),
            ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free),
        )
        if not ok or total.value <= 0:
            continue

        fs_name = ctypes.create_unicode_buffer(16)
        vol_label = ctypes.create_unicode_buffer(261)
        try:
            k32.GetVolumeInformationW(
                ctypes.c_wchar_p(root), vol_label, 261,
                None, None, None, fs_name, 16,
            )
        except Exception:
            pass

        letter = root.rstrip("\\/")
        total_b, free_b = total.value, free.value
        drives_out.append({
            "letter": letter,
            "total": total_b,
            "free": free_b,
            "used": total_b - free_b,
            "percent": (total_b - free_b) / total_b * 100.0,
            "kind": kind,
            "fs": fs_name.value or "",
            "label": vol_label.value or "",
        })

    # 固定盘在前，其次移动盘；同组按盘符字母序
    order = {"fixed": 0, "removable": 1}
    drives_out.sort(key=lambda d: (order.get(d["kind"], 2), d["letter"]))
    return drives_out


def formatted_drives():
    """返回 (drives, total_text, free_text)，供 UI 直接展示。

    drives 每项含盘符 / 容量 / 可用 / 占用率，全部为**实时值**
    （ctypes 直调，毫秒级），因此缓存 profile 里也可以安全地重新取一遍。
    """
    try:
        raw = read_logical_drives()
    except Exception:
        raw = []
    # 注意：合计要基于**原始字节**求和，不能拿格式化后的字符串相加
    total_bytes = sum(d["total"] for d in raw)
    free_bytes = sum(d["free"] for d in raw)
    drives = [
        {
            "letter": d["letter"],
            "total": human_bytes(d["total"]),
            "free": human_bytes(d["free"]),
            "used": human_bytes(d["used"]),
            "percent": d["percent"],
            "kind": d["kind"],
            "kind_label": "固定" if d["kind"] == "fixed" else "可移动",
            "fs": d["fs"],
            "label": d["label"],
            "total_bytes": d["total"],
            "free_bytes": d["free"],
            "used_bytes": d["used"],
        }
        for d in raw
    ]
    total = human_bytes(total_bytes) if raw else "--"
    free = human_bytes(free_bytes) if raw else "--"
    return drives, total, free


def detect_form_factor(pcsystype, chassis_types, battery_present, model_hint=""):
    """判定设备形态：返回 (kind, label, reason)。

    kind ∈ {laptop, desktop, unknown}
    """
    votes_laptop = 0
    votes_desktop = 0

    kind_by_pc = None
    if isinstance(pcsystype, int):
        kind_by_pc, label_by_pc = PC_TYPE_MAP.get(pcsystype, ("unknown", "未知"))
    else:
        label_by_pc = "未知"

    if kind_by_pc == "laptop":
        votes_laptop += 2
    elif kind_by_pc == "desktop":
        votes_desktop += 2

    chassis_list = []
    if isinstance(chassis_types, list):
        chassis_list = [c for c in chassis_types if isinstance(c, int)]
    elif isinstance(chassis_types, int):
        chassis_list = [chassis_types]

    if any(c in CHASSIS_PORTABLE for c in chassis_list):
        votes_laptop += 2
    elif chassis_list:
        votes_desktop += 2

    # 有电池是强力的便携信号（台式机极少有电池）
    if battery_present:
        votes_laptop += 1
    else:
        votes_desktop += 1

    hint = (model_hint or "").lower()
    if any(k in hint for k in ("laptop", "notebook", "ultrabook", "gaming laptop")):
        votes_laptop += 1
    if any(k in hint for k in ("desktop", "tower", "workstation", "all-in-one", "aio")):
        votes_desktop += 1

    if votes_laptop > votes_desktop:
        label = label_by_pc if kind_by_pc == "laptop" else "笔记本"
        return "laptop", label, f"PCSystemType={pcsystype} / Chassis={chassis_list or '未知'} / 电池={'有' if battery_present else '无'}"
    if votes_desktop > votes_laptop:
        label = label_by_pc if kind_by_pc == "desktop" else "台式机"
        return "desktop", label, f"PCSystemType={pcsystype} / Chassis={chassis_list or '未知'} / 电池={'有' if battery_present else '无'}"
    return "unknown", "未知设备", "缺少足够判定依据"


def build_profile(raw):
    """把 PowerShell 原始结果整理成 UI 可直接使用的结构。"""
    if not raw:
        return None

    def g(key, default=None):
        v = raw.get(key)
        return default if v is None else v

    # ---- CPU ----
    cpus = g("cpu", [])
    if isinstance(cpus, dict):
        cpus = [cpus]
    cpus = [c for c in cpus if c]
    cpu = cpus[0] if cpus else {}
    cpu_name = (cpu.get("name") or "--").strip()

    # 核心数 / 线程数优先用 sysinfo（ctypes 直读，与下载页**同一数据源**）。
    # 两边各自取值会分叉：曾经首页写「8 核 12 线程」、下载页写「12 核」，
    # 被用户当 bug 报回来。WMI 只在 sysinfo 取不到时兜底。
    try:
        import sysinfo

        cpu_cores = sysinfo.cpu_cores() or None
        cpu_threads = sysinfo.cpu_threads() or None
    except Exception:  # noqa: BLE001
        cpu_cores = cpu_threads = None

    if not cpu_cores or not cpu_threads:
        # 多路机器（双路工作站/服务器）WMI 会返回**每个物理插槽一条**
        # Win32_Processor，只取 cpus[0] 会把核数显示成单颗的，所以求和。
        def _sum_int(key):
            vals = [c.get(key) for c in cpus if isinstance(c.get(key), (int, float))]
            return int(sum(vals)) if vals else None

        cpu_cores = cpu_cores or _sum_int("cores")
        cpu_threads = cpu_threads or _sum_int("threads")

    cpu_mhz = cpu.get("mhz")
    cpu_clk = f"{cpu_mhz / 1000:.2f} GHz" if isinstance(cpu_mhz, (int, float)) and cpu_mhz else "--"
    cpu_detail = []
    if cpu_cores:
        cpu_detail.append(f"{cpu_cores} 核")
    if cpu_threads:
        cpu_detail.append(f"{cpu_threads} 线程")
    if cpu_clk != "--":
        cpu_detail.append(cpu_clk)

    # ---- GPU ----
    gpus = g("gpu", [])
    if isinstance(gpus, dict):
        gpus = [gpus]
    reg_vram = dict(_reg_vram_bytes())
    gpu_items = []
    # 全机只有"一块 WMI 显卡 + 一项注册表显存"时才允许按名字无关地兜底：
    # A 卡 WMI 名偶尔带 "(TM)"、厂商后缀，逐字匹配容易漏。但机器上有
    # 核显 + 独显时 len(reg_vram) 常常只有 1（核显没有专用显存，被过滤掉），
    # 那种情况下再兜底会把独显的显存安到核显头上，所以同时要求 WMI 也只有一条。
    wmi_count = len([x for x in gpus if (x.get("name") or "").strip()])
    for item in gpus:
        name = (item.get("name") or "--").strip()
        if not name:
            continue
        vkey, vlabel = gpu_vendor(name, item.get("pnp") or "")
        is_virtual = vkey in ("virtual", "microsoft")
        vram = item.get("vram")
        # 注册表值优先（能正确处理 >4GB），否则退回 WMI 值
        real = None
        for rname, rsize in reg_vram.items():
            if rname and (rname.lower() in name.lower() or name.lower() in rname.lower()):
                real = rsize
                break
        # 名字对不上时按位置兜底，但必须同时满足：这块不是基础显示适配器、
        # 注册表只有一项显存、WMI 也只给了一条。否则会出现把独显的 4GB
        # 安到"Microsoft 基本显示适配器"头上的荒唐结果。
        if (real is None and not is_virtual
                and len(reg_vram) == 1 and wmi_count == 1):
            real = next(iter(reg_vram.values()))
        if real:
            vram = real
        # WMI 的 AdapterRAM 若恰好接近 4GB 上限，视为溢出不可信
        if isinstance(vram, (int, float)) and 3.9e9 < vram < 4.3e9 and not real:
            vram_txt = "4 GB（可能溢出）"
        else:
            vram_txt = human_bytes(vram)
        res = ""
        w, h = item.get("w"), item.get("h")
        if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
            res = f"{w}×{h}"
        gpu_items.append({
            "name": name,
            "vram": vram_txt,
            "driver": (item.get("driver") or "").strip(),
            "resolution": res,
            "vendor": vkey,
            "vendor_label": vlabel,
            "is_virtual": is_virtual,
        })

    # WMI 一条显卡都没返回（部分 AMD 驱动上 Win32_VideoController 会空，
    # 或慢到让整条 PowerShell 查询超时）→ 用驱动注册表兜底，至少把
    # 型号 / 显存 / 驱动版本显示出来，而不是让用户看到"未检测到"。
    if not gpu_items:
        gpu_items = _gpus_from_registry()

    # ---- 内存 ----
    rams = g("ram", [])
    if isinstance(rams, dict):
        rams = [rams]
    total_ram = g("totalram")
    if not total_ram:
        total_ram = sum(r.get("cap", 0) or 0 for r in rams)
    dimm_count = len(rams) if rams else 0
    speeds = sorted({r.get("speed") for r in rams if isinstance(r.get("speed"), int) and r.get("speed")})
    speed_txt = " / ".join(f"{s} MT/s" for s in speeds) if speeds else ""
    makers = sorted({(r.get("maker") or "").strip() for r in rams if (r.get("maker") or "").strip()})
    # SMBIOSMemoryType: 26=DDR4 34=DDR5 24=DDR3 等
    ddr_map = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5", 35: "LPDDR4", 36: "LPDDR5"}
    ddr = ""
    for r in rams:
        t = r.get("type")
        if isinstance(t, int) and t in ddr_map:
            ddr = ddr_map[t]
            break
    mem_detail = []
    if dimm_count:
        mem_detail.append(f"{dimm_count} 条")
    if ddr:
        mem_detail.append(ddr)
    if speed_txt:
        mem_detail.append(speed_txt)

    # ---- 硬盘 ----
    disks = g("disk", [])
    if isinstance(disks, dict):
        disks = [disks]
    pd_disks = g("pdisk", [])
    if isinstance(pd_disks, dict):
        pd_disks = [pd_disks]

    # MSFT_PhysicalDisk.MediaType: 3=HDD 4=SSD 5=SCM；BusType: 17=NVMe 11=SATA 8=USB ...
    def _match_pdisk(model):
        key = (model or "").lower()
        best = None
        for pd in pd_disks:
            fname = (pd.get("friendly") or "").lower()
            if not fname:
                continue
            if fname in key or key in fname:
                return pd
            # 退化到首词匹配（"FIREBAT FET34-512G" vs "FIREBAT FET34-512G"）
            fw = fname.split()[0] if fname.split() else ""
            if fw and fw in key and best is None:
                best = pd
        return best

    def _media_label(pd):
        mt = pd.get("mediatype") if pd else None
        bt = pd.get("bustype") if pd else None
        if mt == 4:
            base = "SSD"
        elif mt == 3:
            base = "HDD"
        elif mt == 5:
            base = "SCM"
        else:
            base = None
        if base and bt == 17:
            base = "NVMe " + base
        return base

    disk_items = []
    for d in disks:
        size = d.get("size")
        model = (d.get("model") or "--").strip()
        media = (d.get("media") or "").strip()
        iface = (d.get("iface") or "").strip()
        pnp = (d.get("pnp") or "").strip()

        pd = _match_pdisk(model)
        kind = _media_label(pd)
        if not kind:
            # 退回名称 / MediaType 推断
            low = model.lower()
            if "nvme" in low:
                kind = "NVMe SSD"
            elif "ssd" in low or "solid" in media.lower():
                kind = "SSD"
            elif "hdd" in low or "fixed hard disk" in media.lower():
                kind = "HDD"
            else:
                kind = "硬盘"

        # 外置盘识别：USB 总线 / MediaType 含 External / 型号含 USB
        low_all = (model + " " + pnp + " " + media).lower()
        is_external = (
            "external" in media.lower()
            or "removable" in media.lower()
            or (pd.get("bustype") == 8 if pd else False)
            or "usb" in low_all
        )
        if is_external and "外置" not in kind:
            kind = "外置 " + kind

        disk_items.append({
            "model": model,
            "size": human_bytes(size),
            "size_bytes": size if isinstance(size, (int, float)) else 0,
            "kind": kind,
            "iface": "NVMe" if (pd and pd.get("bustype") == 17) else iface,
            "external": is_external,
        })
    disk_items.sort(key=lambda x: (x["external"], -x["size_bytes"]))
    disk_total = sum(d["size_bytes"] for d in disk_items if not d["external"])

    # ---- 机型 ----
    manufacturer = (g("manufacturer") or "").strip()
    model = (g("model") or "").strip()
    product_name = (g("productname") or "").strip()
    product_ver = (g("productver") or "").strip()
    # 型号取信息量最大的一个
    model_best = model or product_name or "--"
    if product_name and model and product_name.lower() not in model.lower() and len(product_name) > len(model):
        model_best = product_name

    batteries = g("battery", [])
    if isinstance(batteries, dict):
        batteries = [batteries]
    battery_present = bool(batteries)

    kind, kind_label, reason = detect_form_factor(
        g("pcsystype"), g("chassis"), battery_present, model_best + " " + product_ver
    )

    os_name = (g("osname") or "--").strip()
    os_name = os_name.replace("Microsoft ", "")

    # 逻辑盘（带盘符）：ctypes 直调，毫秒级，失败也不影响整体采集
    try:
        drives, drives_total, drives_free = formatted_drives()
    except Exception:
        drives, drives_total, drives_free = [], "--", "--"

    return {
        "kind": kind,
        "kind_label": kind_label,
        "kind_reason": reason,
        "manufacturer": manufacturer or "--",
        "model": model_best,
        "product_version": product_ver,
        "cpu_name": cpu_name,
        "cpu_detail": " · ".join(cpu_detail) or "--",
        "cpu_cores": cpu_cores,
        "cpu_threads": cpu_threads,
        "cpu_clock": cpu_clk,
        "gpus": gpu_items,
        "gpu_primary": gpu_items[0]["name"] if gpu_items else "未检测到",
        "ram_total": human_bytes(total_ram),
        "ram_detail": " · ".join(mem_detail) or "--",
        "ram_makers": " / ".join(makers) or "",
        "disks": disk_items,
        "disk_total": human_bytes(disk_total),
        # 逻辑盘（C:/D:/E:…）：每个盘的容量、可用、占用率
        "drives": drives,
        "drives_total": drives_total,
        "drives_free": drives_free,
        "os": os_name,
        "os_build": g("osbuild") or "--",
        "os_arch": g("osarch") or "--",
        "board": f"{g('boardmaker') or ''} {g('boardname') or ''}".strip() or "--",
        "bios": g("biosver") or "--",
    }


def query_hardware():
    """同步采集（阻塞约 1~3 秒）。建议放在后台线程调用。"""
    raw = _run_ps()
    if raw is None:
        # 整条查询超时——最常见的原因是某个显卡驱动让 Win32_VideoController
        # 长时间不返回。改用去掉显卡的精简查询再试一次：宁可显卡那项退回
        # 注册表兜底，也别让用户看到整页"未能读取到硬件信息"。
        raw = _run_ps(_QUERY_LITE, timeout=30)
        if raw is not None:
            raw["_gpu_skipped"] = True
    return build_profile(raw)


class HardwareScanner:
    """后台线程采集 + 信号回调的封装（避免阻塞 UI）。"""

    def __init__(self, on_done, on_error=None):
        self._on_done = on_done
        self._on_error = on_error

    def start(self):
        Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            profile = query_hardware()
            if profile:
                self._on_done(profile)
            elif self._on_error:
                self._on_error("未能读取到硬件信息")
        except Exception as exc:  # pragma: no cover - 防御性
            if self._on_error:
                self._on_error(str(exc))


if __name__ == "__main__":
    import pprint

    pprint.pprint(query_hardware())
