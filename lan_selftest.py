# -*- coding: utf-8 -*-
"""打包后自检：创建跨网房间全流程。

和 `uninstaller_selftest` 同样的思路 —— 跨网房间的启动涉及
UAC 提权 + 独立 watchdog 进程 + 虚拟网卡轮询，**外部脚本没法可靠驱动**
（UAC 是系统级弹窗、+ 150% 缩放下点击落点不准）。所以让 exe 自己在内部
把真实 `EasyTier.start()` 跑一遍，把每一步观测结果写成 JSON 回传。

用法：`Yuhub.exe --lan-selftest <结果json路径> [--room-code xxx] [--keep]`

`--keep`：跑完不自动停止房间（用于人工接着验证游戏联机）。
"""
import json
import os
import sys
import time


def run(out_file, code="yuhub-selftest", password="test1234", keep=False,
        timeout=45.0):
    """返回进程退出码：0 = 通过，1 = 失败，4 = 结果写盘失败。"""
    result = {
        "ok": False,
        "checks": [],
        "logs": [],
        "timeline": [],
        "runtime": {},
    }

    t0 = time.time()

    def mark(name, ok=None, detail=""):
        result["checks"].append({"name": name, "pass": ok, "detail": detail})

    def tlog(msg):
        result["logs"].append({"t": round(time.time() - t0, 3), "msg": msg})

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import etier
    except Exception as exc:
        mark("导入 etier", False, repr(exc))
        _dump(result, out_file)
        return 1

    # ---------------- 环境快照 ----------------
    exe = etier._yuhub_exe_path()
    result["runtime"] = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "sys.executable": sys.executable,
        "_yuhub_exe_path": exe,
        "_parent_process_name": getattr(etier, "_parent_process_name", lambda: "?")()
                                if hasattr(etier, "_parent_process_name") else "n/a",
        "core_path": etier.core_path(),
        "core_exists": os.path.isfile(etier.core_path()),
        "install_dir": etier.install_dir(),
        "virtual_prefix": etier.VIRTUAL_NET_PREFIX,
        "public_servers": list(etier.PUBLIC_SERVERS),
    }

    # ① watchdog 可执行文件是否可用（这是"一直正在启动"的头号根因）
    capable, why = etier.watchdog_capable(exe)
    mark("watchdog 可执行文件可用", capable,
         "%s（%s）" % (os.path.basename(exe) if exe else "无", why))
    if not capable:
        _dump(result, out_file)
        return 1

    # ② watchdog 必须**不是** python.exe（源码态误用会让提权启动静默失败）
    base = os.path.basename(exe).lower()
    mark("watchdog 指向 Yuhub.exe 而非 python.exe",
         base == "yuhub.exe", "实际为 %s" % base)

    # ③ 二进制齐全
    ok_bin, bin_msg = etier.release_binaries()
    mark("EasyTier 二进制释放", ok_bin, str(bin_msg))

    # ④ 启动前应无残留进程
    pre = etier.core_running()
    mark("启动前无残留 easytier-core", not pre, "core_running=%s" % pre)

    # ⑤ 真正启动（这一步会弹一次 UAC —— 自检时请点「是」）
    tier = etier.EasyTier(on_log=lambda m: tlog(m))
    result["timeline"].append({"t": round(time.time() - t0, 3), "ev": "start_begin"})
    try:
        ok, msg = tier.start("yuhub-" + code.lower().replace(" ", ""),
                             password, timeout=timeout, ipv4="10.126.126.1")
    except Exception as exc:
        ok, msg = False, repr(exc)
    elapsed = time.time() - t0
    result["timeline"].append({"t": round(elapsed, 3), "ev": "start_returned",
                               "ok": ok, "msg": str(msg)})
    mark("创建跨网房间", bool(ok), "%.1fs → ok=%s msg=%s" % (elapsed, ok, msg))

    if ok:
        ip = etier.virtual_adapter_ip()
        mark("虚拟网卡拿到 IP", bool(ip), repr(ip))
        mark("IP 落在预期网段", str(ip).startswith(etier.VIRTUAL_NET_PREFIX),
             "%s vs 前缀 %s" % (ip, etier.VIRTUAL_NET_PREFIX))
        mark("core 进程在运行", etier.core_running(),
             "core_running=%s" % etier.core_running())
        # 成员列表接口应能跑通（没人时返回空列表，不应抛异常）
        try:
            members = etier.list_members(ip)
            mark("成员列表接口可用", True, "当前成员 %r" % (members,))
        except Exception as exc:
            mark("成员列表接口可用", False, repr(exc))
        # 网络指纹检测接口
        try:
            changed = tier.network_changed()
            mark("网络切换检测接口可用", True, "network_changed=%s" % changed)
        except Exception as exc:
            mark("网络切换检测接口可用", False, repr(exc))

    # ⑥ 收尾：停止房间（除非 --keep）
    if ok and not keep:
        tier.request_stop()
        stopped = tier.wait_stopped(6.0)
        time.sleep(1.5)
        mark("停止房间后进程已退出", not etier.core_running(),
             "wait_stopped=%s core_running=%s" % (stopped, etier.core_running()))
        mark("停止后虚拟网卡已释放", not etier.virtual_adapter_ip(),
             repr(etier.virtual_adapter_ip()))
    elif ok and keep:
        mark("房间保持运行（--keep）", True, "虚拟 IP %s" % etier.virtual_adapter_ip())

    # ⑦ 界面状态机：按钮文案不能卡在"正在启动…"
    #    用户看到的现象就是"一直显示正在启动"，所以这条必须有。
    _check_ui_state(result, mark)

    result["ok"] = all(c["pass"] for c in result["checks"])
    _dump(result, out_file)
    return 0 if result["ok"] else 1


def _check_ui_state(result, mark):
    """实例化真实的 LanPage，驱动它的状态流转，确认按钮不会卡住。

    这是对用户可见症状的直接回归测试：启动中/成功后/失败后，
    按钮文案必须回到可再点的状态，不能永远停在"正在启动…"。
    """
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from ui.pages.lan_page import LanPage
    except Exception as exc:
        mark("界面状态机可构造", False, repr(exc))
        return

    try:
        page = LanPage()
    except Exception as exc:
        mark("界面状态机可构造", False, repr(exc))
        return

    # 模拟"正在启动"
    page._etier_busy = True
    page.btn_start.setEnabled(False)
    page.btn_start.setText("正在启动…")

    # 模拟启动成功回调
    class _FakeTier:
        def network_changed(self):
            return False

        def request_stop(self):
            return True

        def wait_stopped(self, t=0):
            return True

    page._on_done({"ok": True, "msg": "10.126.126.1",
                   "tier": _FakeTier(), "ipv4": "10.126.126.1"})
    app.processEvents()
    ok_btn = page.btn_start.text()
    mark("成功后启动按钮不再是「正在启动…」",
         "正在启动" not in ok_btn,
         "按钮文案=%r 停止按钮可用=%s" % (ok_btn, page.btn_stop.isEnabled()))

    # 模拟启动失败回调
    page._on_done({"ok": False, "msg": "模拟失败", "tier": None, "ipv4": ""})
    app.processEvents()
    fail_btn = page.btn_start.text()
    mark("失败后启动按钮可再次点击",
         page.btn_start.isEnabled() and "正在启动" not in fail_btn,
         "按钮文案=%r enabled=%s" % (fail_btn, page.btn_start.isEnabled()))

    # 关键回归：busy 标记必须被清掉，否则用户再点也不会触发启动
    mark("启动结束后 busy 标记已复位",
         page._etier_busy is False, "_etier_busy=%s" % page._etier_busy)


def _dump(result, out_file):
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
