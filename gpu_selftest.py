# -*- coding: utf-8 -*-
"""打包后自检：显卡信息与 GPU 占用率（重点覆盖 AMD 显卡）。

为什么需要它
------------
A 卡机器上 nvidia-smi 根本不存在，占用率只能走 Windows 的 PDH 计数器。
而 PDH 有两条"静默失败"的路，都不会抛异常，界面只显示"未检测到"或 0%，
远程完全看不出原因：
  1. ctypes 不显式声明 argtypes → 一条实例都读不到；
  2. 用单值 API 读通配符计数器 → 恒为 0.0%。
外加显卡信息本身也有坑：部分 AMD 驱动下 Win32_VideoController 返回空。
这些都在这里固化成断言，防止以后被改回去。

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

    return _finish(out_file, result)
