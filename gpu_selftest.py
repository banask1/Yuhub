# -*- coding: utf-8 -*-
"""打包后自检：显卡信息、GPU 占用率与温度/功耗（重点覆盖 AMD 显卡）。

为什么需要它
------------
A 卡机器上 nvidia-smi 根本不存在，占用率只能走 Windows 的 PDH 计数器。
而 PDH 有两条"静默失败"的路，都不会抛异常，界面只显示"未检测到"或 0%，
远程完全看不出原因：
  1. ctypes 不显式声明 argtypes → 一条实例都读不到；
  2. 用单值 API 读通配符计数器 → 恒为 0.0%。
温度与功耗更隐蔽：PDH 根本没有这两项，只能走厂商接口（NVIDIA NVML /
AMD ADL），单位换算一错，界面上依然是"看着正常"的数字。这些都在这里
固化成断言，防止以后被改回去。

用法：`Yuhub.exe --gpu-selftest <结果json路径>`
返回码：0 全部通过 / 1 有断言失败 / 2 参数错误 / 4 结果写盘失败
"""

import json
import time


def _finish(out_file, result):
    checks = result["checks"]
    result["ok"] = bool(checks) and all(c["pass"] for c in checks)
    result["info"]["passed"] = sum(1 for c in checks if c["pass"])
    result["info"]["total"] = len(checks)
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    except OSError:
        return 4
    return 0 if result["ok"] else 1


def run(out_file, timeout_sec=60):
    checks = []
    result = {"ok": False, "checks": checks, "info": {}}

    def chk(name, cond, detail=""):
        checks.append({"name": name, "pass": bool(cond), "detail": str(detail)})

    try:
        import hardware as hw
        import live_monitor as lm
    except Exception as exc:                       # pragma: no cover - 防御
        chk("导入 hardware / live_monitor", False, repr(exc))
        return _finish(out_file, result)

    reader = lm.PdhGpuReader

    # ---------------------------------------------------------------- A
    # 实例名解析。N 卡 / A 卡的实例名格式一致，但不能假设一定有 engtype
    # 或 luid（个别老驱动不带），解析必须容忍缺字段。
    names = [
        ("pid_4956_luid_0x00000000_0x0000D0BA_phys_0_eng_0_engtype_3D",
         "0X00000000_0X0000D0BA", "0", "3d"),
        ("pid_777_luid_0x00000000_0x0000C1C4_phys_0_eng_3_engtype_3D",
         "0X00000000_0X0000C1C4", "0", "3d"),
        ("pid_4_luid_0x00000000_0x0000D0BA_phys_0_eng_2_engtype_VideoDecode",
         "0X00000000_0X0000D0BA", "0", "videodecode"),
    ]
    for raw, luid, phys, eng in names:
        got = reader._engine_key(raw)
        chk("解析实例名 %s…" % raw[:28],
            got == (luid, phys, eng), "得到 %r" % (got,))

    key_noeng = reader._engine_key("pid_99_luid_0x1_0x2_phys_0_eng_5")
    chk("缺 engtype 时仍能解析出适配器", key_noeng[0] == "0X1_0X2"
        and key_noeng[2] == "", key_noeng)

    key_garbage = reader._engine_key("完全看不懂的实例名")
    chk("无法解析的名字返回空键而不抛异常", key_garbage == ("", "", ""),
        key_garbage)

    # ---------------------------------------------------------------- B
    # 整卡占用率口径：同一引擎上的多进程相加，再取最忙的引擎。
    # 这三条对应微软 DirectX 团队解释过的取舍——求和会超 100%、
    # 取平均不准、只取 3D 引擎在纯解码时恒为 0。
    chk("同引擎多进程相加后取最忙引擎",
        abs(reader._overall_util([
            ("pid_1_luid_0x0_0xA_phys_0_eng_0_engtype_3D", 30.0),
            ("pid_2_luid_0x0_0xA_phys_0_eng_0_engtype_3D", 12.0),
            ("pid_1_luid_0x0_0xA_phys_0_eng_5_engtype_Copy", 4.0),
        ]) - 42.0) < 0.01, "期望 42.0")

    chk("视频解码满载时不会被 3D 的 0 掩盖",
        abs(reader._overall_util([
            ("pid_1_luid_0x0_0xA_phys_0_eng_0_engtype_3D", 0.5),
            ("pid_1_luid_0x0_0xA_phys_0_eng_2_engtype_VideoDecode", 88.0),
        ]) - 88.0) < 0.01, "期望 88.0")

    chk("多适配器分开统计后取最忙的一张",
        abs(reader._overall_util([
            ("pid_1_luid_0x0_0xA_phys_0_eng_0_engtype_3D", 20.0),
            ("pid_1_luid_0x0_0xB_phys_0_eng_0_engtype_3D", 70.0),
        ]) - 70.0) < 0.01, "期望 70.0")

    chk("认不出引擎类型时退化为取最大（不相加，避免虚报 100%）",
        abs(reader._overall_util([
            ("weird_1", 40.0), ("weird_2", 40.0)]) - 40.0) < 0.01,
        "期望 40.0")

    chk("占用率不会超过 100%",
        reader._overall_util([
            ("pid_1_luid_0x0_0xA_phys_0_eng_0_engtype_3D", 90.0),
            ("pid_2_luid_0x0_0xA_phys_0_eng_0_engtype_3D", 90.0),
        ]) == 100.0, "期望 100.0")

    chk("空实例列表返回 0 而不是崩溃", reader._overall_util([]) == 0.0)

    # ---------------------------------------------------------------- C
    # 真实 PDH 读取。这里失败就是 A 卡用户看到"未检测到可用显卡"的原因。
    probe = lm.PdhGpuReader()
    opened = probe._open()
    chk("PDH 查询可以打开", opened, "PdhOpenQueryW / PdhAddCounterW 失败")

    if opened:
        probe._pdh.PdhCollectQueryData(probe._query)
        time.sleep(0.3)
        probe._pdh.PdhCollectQueryData(probe._query)
        items = probe._read_array(probe._counters[0][1])
        result["info"]["engine_instances"] = len(items)
        result["info"]["sample_names"] = [n for n, _v in items[:5]]
        # 这条是防退回老实现的关键：用单值 API 读通配符时恒为 0 条实例。
        chk("数组 API 能取回全部 GPU 引擎实例", len(items) > 1,
            "只拿到 %d 个实例（旧实现恒为 0 个）" % len(items))
        chk("实例名能解析出引擎类型",
            any(reader._engine_key(n)[2] for n, _v in items),
            "前三个: %s" % [n for n, _v in items[:3]])
        chk("能算出整卡占用率",
            reader._overall_util(items) >= 0.0)

        mem_paths = [p for p, _h in probe._counters[1:]]
        result["info"]["mem_counter_paths"] = mem_paths
        chk("至少挂上一个显存计数器", bool(mem_paths), "一个都没挂上")
        for path, handle in probe._counters[1:]:
            vals = [v for _n, v in probe._read_array(handle)]
            if vals:
                result["info"]["mem_sample"] = [path, max(vals)]
                break
        probe._discard()

    live = lm.read_gpu_pdh()
    result["info"]["pdh_read"] = live
    chk("read_gpu_pdh() 返回了数据", live is not None,
        "返回 None —— A 卡用户会看到'未检测到可用显卡'")
    if live:
        chk("给出了占用率数值", live.get("util") is not None, live.get("util"))

    # ---------------------------------------------------------------- D
    # 注册表显存（PDH 没有 Dedicated Limit 计数器，总量只能从这里来）
    vram_mb = lm._reg_dedicated_vram_mb()
    result["info"]["reg_vram_mb"] = vram_mb
    # 核显 / 无独显的机器读不到专用显存属正常，不做强制断言

    # ---------------------------------------------------------------- E
    # 显卡信息：厂商判定 + WMI 落空时的注册表兜底
    chk("A 卡名称判为 AMD",
        hw.gpu_vendor("AMD Radeon RX 6600")[0] == "amd")
    chk("A 卡核显名称判为 AMD",
        hw.gpu_vendor("AMD Radeon(TM) Graphics")[0] == "amd")
    chk("「Microsoft 基本显示适配器」判为虚拟/基础显示",
        hw.gpu_vendor("Microsoft 基本显示适配器")[0] == "virtual")
    chk("基础显示适配器不会因 PnP 残留 VEN_1002 而误判成 AMD",
        hw.gpu_vendor("Microsoft 基本显示适配器",
                      r"PCI\VEN_1002&DEV_73FF")[0] == "virtual")

    reg = hw._reg_gpus()
    result["info"]["reg_gpus"] = [[n, m, d] for n, m, d in reg]
    chk("注册表能枚举到显示适配器", bool(reg),
        "类键下没有 DriverDesc，兜底路径会失效")
    chk("注册表能读到显存或至少名称",
        any(len(n) > 3 for n, _m, _d in reg), reg)

    try:
        prof = hw.build_profile({"gpu": []})       # 模拟 WMI 一条显卡都没给
        got = (prof or {}).get("gpus") or []
        result["info"]["fallback_gpus"] = [g["name"] for g in got]
        chk("WMI 无显卡时注册表兜底生效", bool(got), "兜底后仍为空")
        chk("兜底条目被标记来源",
            all(g.get("from_registry") for g in got) if got else False)
    except Exception as exc:
        chk("WMI 无显卡时注册表兜底生效", False, repr(exc))

    # ---------------------------------------------------------------- F
    # 厂商私有传感器：温度 / 功耗。PDH 只上报利用率与显存，温度和功耗必须走
    # NVML（N 卡）/ ADL（A 卡）—— 以前"只有 N 卡能看到温度"就是卡在这里。
    # 换算比例写死在断言里：AMD 官方文档给的是毫摄氏度，OD6 功耗是 1/256 W，
    # 换错一个 1000 倍界面上看着依然"像那么回事"，最容易被改回去。
    gs = None
    try:
        import gpu_sensors as gs
    except Exception as exc:
        chk("导入 gpu_sensors（厂商传感器）", False, repr(exc))

    if gs is not None:
        chk("ADL 温度按毫摄氏度换算（45000 → 45.0）",
            gs.adl_milli_celsius(45000) == 45.0, gs.adl_milli_celsius(45000))
        chk("ADL 传感器不可用的哨兵 0 → None（不显示 0°C）",
            gs.adl_milli_celsius(0) is None, gs.adl_milli_celsius(0))
        chk("ADL 超范围垃圾值 999999 → None",
            gs.adl_milli_celsius(999999) is None, gs.adl_milli_celsius(999999))
        chk("ADL 负温度越界 → None",
            gs.adl_milli_celsius(-5000) is None, gs.adl_milli_celsius(-5000))
        chk("OD6 功耗按 1/256 W 换算（2560 → 10.0）",
            gs.adl_od6_watts(2560) == 10.0, gs.adl_od6_watts(2560))
        chk("OD6 功耗哨兵 0 → None", gs.adl_od6_watts(0) is None,
            gs.adl_od6_watts(0))
        chk("OD6 功耗不可能的量级 → None",
            gs.adl_od6_watts(25600000) is None, gs.adl_od6_watts(25600000))

        try:
            diag = gs.diagnostics()
            result["info"]["sensors"] = diag
            chk("厂商传感器诊断不抛异常", True)
        except Exception as exc:
            diag = {}
            chk("厂商传感器诊断不抛异常", False, repr(exc))

        snap = gs.read_sensors()            # 内部已吞异常，这里只做结果断言
        result["info"]["sensor_snapshot"] = snap
        if snap:
            chk("厂商接口给出了温度",
                snap.get("temp") is not None and 1 <= snap["temp"] <= 150,
                snap.get("temp"))
            chk("厂商接口标明了来源",
                (snap.get("source") or "") in ("nvml", "adl-odn", "adl-od6"),
                snap.get("source"))
        # 本机既没有 NVML 也没有 ADL（Intel 集显 / 无独显）属正常，不作断言

        unified = lm.read_gpu()
        result["info"]["read_gpu"] = unified
        chk("read_gpu() 统一入口可用", unified is not None,
            "厂商层与通用层都没数据")
        if unified:
            chk("统一入口给出占用率", unified.get("util") is not None,
                unified.get("util"))
            chk("统一入口标出传感器来源", "sensor_source" in unified,
                sorted(unified))
            if live and live.get("util") is not None:
                # PDH 有值时占用率必须是 PDH 口径（= 任务管理器口径）
                chk("占用率采用 PDH 口径（与任务管理器一致）",
                    unified.get("util_source") == "pdh",
                    unified.get("util_source"))

        _check_adl_logic(gs, chk, result)

    return _finish(out_file, result)


# ---------------------------------------------------------------------------
# A 卡逻辑：用假 ADL 接口驱动，避免"本机是 N 卡就完全测不到 A 卡路径"
# ---------------------------------------------------------------------------
class _FakeAdlLib:
    """同签名的假 ADL。可配置适配器数量 / 活动状态 / 各接口返回码与原始值。

    真实 atiadlxx.dll 只在 A 卡机器上存在；但"哪块适配器被选中、ODN 与 OD6
    怎么回退、哨兵值怎么挡掉"这些**逻辑**与 DLL 无关，可以在任何机器上验证。
    """

    def __init__(self, n=1, active=(True,), od_ver=7, od_supported=True,
                 odn_raw=None, od6_raw=None, power_raw=None,
                 odn_rc=0, od6_rc=0, power_rc=0):
        self.n = n
        self.active = active
        self.od_ver = od_ver
        self.od_supported = od_supported
        self.odn_raw = odn_raw if odn_raw is not None else [0] * n
        self.od6_raw = od6_raw if od6_raw is not None else [0] * n
        self.power_raw = power_raw if power_raw is not None else [0] * n
        self.odn_rc = odn_rc
        self.od6_rc = od6_rc
        self.power_rc = power_rc
        self.calls = []

    def ADL2_Adapter_NumberOfAdapters_Get(self, ctx, p):
        p._obj.value = self.n
        return 0

    def ADL2_Adapter_Active_Get(self, ctx, idx, p):
        p._obj.value = 1 if self.active[idx] else 0
        return 0

    def ADL2_Overdrive_Caps(self, ctx, idx, sup, en, ver):
        sup._obj.value = 1 if self.od_supported else 0
        en._obj.value = sup._obj.value
        ver._obj.value = self.od_ver
        return 0

    def ADL2_OverdriveN_Temperature_Get(self, ctx, idx, ttype, p):
        self.calls.append(("odn", idx, ttype))
        if self.odn_rc != 0:
            return self.odn_rc
        p._obj.value = self.odn_raw[idx]
        return 0

    def ADL2_Overdrive6_Temperature_Get(self, ctx, idx, p):
        self.calls.append(("od6temp", idx))
        if self.od6_rc != 0:
            return self.od6_rc
        p._obj.value = self.od6_raw[idx]
        return 0

    def ADL2_Overdrive6_CurrentPower_Get(self, ctx, idx, ptype, p):
        self.calls.append(("od6power", idx, ptype))
        if self.power_rc != 0:
            return self.power_rc
        p._obj.value = self.power_raw[idx]
        return 0


def _fake_adl(gs, fake):
    adl = gs._Adl()
    adl._lib, adl._ctx, adl._ok = fake, 1, True      # 绕过真实 DLL 加载
    return adl


def _check_adl_logic(gs, chk, result):
    """A 卡的适配器选择 / API 回退 / 哨兵值过滤（15 项）。"""
    try:
        # 未连接的适配器必须跳过：多卡机器上 0 号常是"未连接"的空槽位
        f = _FakeAdlLib(n=3, active=(False, True, True),
                        odn_raw=[0, 45000, 60000], power_raw=[0, 5120, 25600])
        s = _fake_adl(gs, f).snapshot()
        chk("A 卡：跳过未连接适配器，选中 1 号", bool(s) and s["adapter_index"] == 1,
            s and s.get("adapter_index"))
        chk("A 卡：ODN 毫摄氏度 → 45.0 °C", bool(s) and s["temp"] == 45.0,
            s and s.get("temp"))
        chk("A 卡：OD6 功耗 1/256 W → 20.0 W", bool(s) and s["power_w"] == 20.0,
            s and s.get("power_w"))
        chk("A 卡：来源标记为 adl-odn", bool(s) and s["source"] == "adl-odn",
            s and s.get("source"))
        chk("A 卡：ODN 走 EDGE 温度通道",
            ("odn", 1, gs._Adl.OVERDRIVE_TEMP_EDGE) in f.calls, f.calls)

        # ODN 不可用（新架构驱动上常见）→ 必须回退 OD6
        f = _FakeAdlLib(n=1, odn_rc=-1, od6_raw=[62000], power_raw=[12800])
        s = _fake_adl(gs, f).snapshot()
        chk("A 卡：ODN 报错时回退 OD6 温度", bool(s) and s["temp"] == 62.0,
            s and s.get("temp"))
        chk("A 卡：回退后来源标记 adl-od6", bool(s) and s["source"] == "adl-od6",
            s and s.get("source"))
        chk("A 卡：回退路径仍能读功耗 50.0 W", bool(s) and s["power_w"] == 50.0,
            s and s.get("power_w"))

        # 版本 < 7 不碰 ODN：有些卡在 ODN 上回填 54000 哨兵，
        # 除 1000 后看着像正常的 54°C，只能靠版本号挡
        f = _FakeAdlLib(n=1, od_ver=6, odn_raw=[54000],
                        od6_raw=[48000], power_raw=[2560])
        s = _fake_adl(gs, f).snapshot()
        chk("A 卡：OD6 世代不调用 ODN",
            not any(c[0] == "odn" for c in f.calls), f.calls)
        chk("A 卡：取 OD6 的 48.0 °C（而非 ODN 哨兵 54.0）",
            bool(s) and s["temp"] == 48.0, s and s.get("temp"))

        # 温度全不可用 → 宁可不显示，也不编一个 0°C
        chk("A 卡：温度全不可用时返回 None（不显示 0°C）",
            _fake_adl(gs, _FakeAdlLib(n=1)).snapshot() is None)

        f = _FakeAdlLib(n=2, active=(True, True),
                        odn_raw=[999999, 40000], power_raw=[2560, 2560])
        s = _fake_adl(gs, f).snapshot()
        chk("A 卡：第一块给垃圾温度就继续找第二块",
            bool(s) and s["adapter_index"] == 1, s)

        chk("A 卡：没有活动适配器时返回 None",
            _fake_adl(gs, _FakeAdlLib(n=2, active=(False, False))).snapshot()
            is None)

        f = _FakeAdlLib(n=1, odn_raw=[41000], power_rc=-9)
        s = _fake_adl(gs, f).snapshot()
        chk("A 卡：功耗读失败不影响温度",
            bool(s) and s["temp"] == 41.0 and s["power_w"] is None, s)

        f = _FakeAdlLib(n=2, active=(True, True),
                        odn_raw=[45000, 70000], power_raw=[2560, 2560])
        adl = _fake_adl(gs, f)
        adl.snapshot()
        adl._pick = 1
        s = adl.snapshot()
        chk("A 卡：已锁定的适配器优先（不在两卡间反复跳）",
            bool(s) and s["adapter_index"] == 1, s)
        result["info"]["adl_logic"] = "15 项已跑"
    except Exception as exc:                        # pragma: no cover - 防御
        chk("A 卡逻辑回归（假 ADL）", False, repr(exc))
