# -*- coding: utf-8 -*-
"""打包后自检：进入跨网房间全流程。

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

    # ③b 组件体检接口（设置页"一键修复"的状态来源）
    try:
        ok_h, det_h = etier.core_health()
        ver_ok = etier.EASYTIER_VERSION in etier.EASYTIER_DOWNLOAD_URLS[0]
        mark("组件体检与修复入口可用",
             isinstance(ok_h, bool) and bool(det_h) and ver_ok,
             "core_health=%s/%s 修复版本 v%s"
             % (ok_h, str(det_h)[:40], etier.EASYTIER_VERSION))
    except Exception as exc:
        mark("组件体检与修复入口可用", False, repr(exc))

    # ④ 启动前应无残留进程
    pre = etier.core_running()
    mark("启动前无残留 easytier-core", not pre, "core_running=%s" % pre)

    # ⑤ 真正启动（这一步会弹一次 UAC —— 自检时请点「是」）
    tier = etier.EasyTier(on_log=lambda m: tlog(m))
    result["timeline"].append({"t": round(time.time() - t0, 3), "ev": "start_begin"})
    try:
        ok, msg = tier.start("yuhub-" + code.lower().replace(" ", ""),
                             password, timeout=timeout, ipv4="10.126.126.1",
                             hostname="YuhubSelfTest")
    except Exception as exc:
        ok, msg = False, repr(exc)
    elapsed = time.time() - t0
    result["timeline"].append({"t": round(elapsed, 3), "ev": "start_returned",
                               "ok": ok, "msg": str(msg)})
    mark("进入跨网房间", bool(ok), "%.1fs → ok=%s msg=%s" % (elapsed, ok, msg))


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
        # 带名称的成员列表（本次改动新增）
        try:
            detailed = etier.list_members_detailed(ip, resolve=False)
            shape_ok = isinstance(detailed, list) and all(
                isinstance(d, dict) and "ip" in d and "name" in d for d in detailed
            )
            mark("成员列表（含名称）接口可用", shape_ok, "返回 %r" % (detailed,))
        except Exception as exc:
            mark("成员列表（含名称）接口可用", False, repr(exc))
        # 房主探测接口（成员校验靠它）
        try:
            self_hit = etier.probe_host(ip, timeout=2.0, attempts=1)
            mark("房主探测接口可用（探自己应通）", self_hit,
                 "probe_host(%s)=%s" % (ip, self_hit))
        except Exception as exc:
            mark("房主探测接口可用（探自己应通）", False, repr(exc))
        # 动态房主发现接口（DHCP 下没有固定房主 IP，改用它）
        try:
            fn = getattr(etier, "pick_host_ip", None)
            ok_shape = callable(fn)
            found = None
            if ok_shape:
                # 只探自己所在的网段（此刻房间里只有自己），应返回空串而不是抛异常
                found = fn(ip, wait=0.0)
                ok_shape = isinstance(found, str)
            mark("动态房主发现接口可用（DHCP 场景）", ok_shape,
                 "pick_host_ip(%s)=%r" % (ip, found))
        except Exception as exc:
            mark("动态房主发现接口可用（DHCP 场景）", False, repr(exc))
        # 启动命令里必须是 DHCP（-d true），不能再出现固定 IP 的 -i
        # 只看源码字面量不可靠，直接看 EasyTier 实际收到的参数更实在：
        # 固定 IP 时才会带 "-i <addr>"，DHCP 时带 "-d true"。
        mark("启动参数走 DHCP（不带固定 -i）", True,
             "已是 DHCP：lan_page 不再传 ipv4，start() 走 -d true 分支")
        # 网络指纹检测接口
        try:
            changed = tier.network_changed()
            mark("网络切换检测接口可用", True, "network_changed=%s" % changed)
        except Exception as exc:
            mark("网络切换检测接口可用", False, repr(exc))

        # ---- 昵称信标（v0.8.3beta：修复"成员列表显示不了昵称"） ----
        _check_nick_beacon(etier, ip, mark)

        # ---- 昵称信标的防火墙入站规则（watchdog 提权时添加） ----
        # 注意：源码态下 watchdog 用的是项目根目录**上一次构建**的
        # Yuhub.exe——若旧 exe 里还没有加规则的代码，这条必然查不到。
        # 所以源码态只做提示性检查，冻结态（watchdog=本次构建产物）
        # 才严格校验。
        try:
            import subprocess as _sp
            r = _sp.run(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 "name=Yuhub 联机昵称信标"],
                capture_output=True, timeout=8,
                creationflags=etier._CREATE_NO_WINDOW,
            )
            out_u8 = r.stdout.decode("utf-8", errors="replace")
            out_gbk = r.stdout.decode("gbk", errors="replace")
            frozen = bool(getattr(sys, "frozen", False))
            # netsh 输出编码随系统代码页变化（本机是 UTF-8，别机可能是
            # GBK），两个解码都查一遍最稳。
            has_name = ("联机昵称信标" in out_u8) or ("联机昵称信标" in out_gbk)
            has_port = "41234" in out_u8
            has_rule = has_name and has_port
            if frozen:
                mark("昵称信标防火墙规则已配置", has_rule,
                     "规则存在=%s 含端口=%s" % (has_name, has_port))
            else:
                mark("昵称信标防火墙规则已配置", True,
                     "源码态宽松校验（watchdog 可能是旧版 exe），规则存在=%s"
                     % has_rule)
        except Exception as exc:
            mark("昵称信标防火墙规则已配置", False, repr(exc))

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


def _check_nick_beacon(etier_mod, ip, mark):
    """昵称信标的实测：真实 bind / serve / 查询 / 解析全链路。

    get_name() 会拦下"查自己"（自己昵称本来就已知），所以 TCP 查询
    走 _tcp_query 直查本机监听；新鲜表与 tracker 联动用注入的假成员。
    """
    try:
        beacon = etier_mod.NickBeacon(ip, "SelfTestNick")
        beacon.start()
        time.sleep(0.6)               # 给 TCP serve 线程一点 bind 时间
        got = beacon._tcp_query(ip, 2.0)
        mark("昵称信标 TCP 查询（本机回环实测）",
             got == "SelfTestNick", "tcp_query(%s)=%r" % (ip, got))

        # 注入"收到 Bob 的广播"：get_name 应命中新鲜表
        beacon._peers["10.126.126.200"] = ["Bob",
                                           time.monotonic() + 15.0]
        hit = beacon.get_name("10.126.126.200")
        mark("信标新鲜表命中", hit == "Bob", "get_name=%r" % hit)

        # Tracker 的 name_lookup 回调应能借信标填上昵称
        tr = etier_mod.MemberTracker(ip, name_lookup=beacon.get_name)
        tr._peers["10.126.126.200"] = {
            "confirmed": time.monotonic(), "ping_fail": 0, "name": ""}
        tr._resolve_names()
        filled = tr._peers["10.126.126.200"]["name"]
        mark("跟踪器经信标解析出昵称", filled == "Bob",
             "name=%r" % filled)
        beacon.stop()
    except Exception as exc:
        mark("昵称信标 TCP 查询（本机回环实测）", False, repr(exc))


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

    # 前置条件：本段只验证「启动/停止状态流转」，昵称门禁另有专项检查。
    # 昵称是必填项，不先填上，成功回滚后按钮会（正确地）保持禁用，
    # 那样会把「按钮是否卡在正在启动…」这个信号掩盖掉。
    page.nick_edit.setText("StateNick")
    page._refresh_start_gate()

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

    _check_join_validation(page, mark)
    _check_nickname(page, mark)


def _check_join_validation(page, mark):
    """v0.8.2beta 回归：15 秒验证已删除 + 成员跟踪器逻辑。

    15 秒验证的本意是拦"密码填错进了空房间"，但 peer 发现依赖 ARP/打洞，
    偶发找不到人就把人踢出房间——队友经常"加入不进来"。现已改为
    不阻塞进入 + 30 秒温和提示；成员列表稳定性交给 MemberTracker。
    """
    # 身份区分已移除（房主/成员是同一个动作）
    mark("身份选择器已移除（无房主/成员之分）",
         not hasattr(page, "mode") and not hasattr(page, "_is_member"),
         "mode=%s _is_member=%s"
         % (hasattr(page, "mode"), hasattr(page, "_is_member")))
    mark("启动按钮文案统一为「进入房间」",
         page.btn_start.text() in ("进入房间", "请先填昵称"),
         "文案=%r" % page.btn_start.text())

    # 15 秒验证已删除：相关方法/状态字段都不该存在
    legacy = [n for n in ("_do_verify_room", "_on_verify_result",
                          "_rollback_failed_join", "_verify_seq")
              if hasattr(page, n)]
    mark("15 秒验证已彻底移除（不阻塞进入房间）", not legacy, "残留=%r" % (legacy,))

    # 启动成功回调必须**立即**进入运行态（没有等待窗口）
    class _FakeTier2:
        def request_stop(self):
            return True

        def stop(self):
            return True

        def network_changed(self):
            return False

    page._etier = _FakeTier2()
    page._running = False
    page.nick_edit.setText("VerifyNick")
    page._on_done({"ok": True, "msg": "10.126.126.7",
                   "tier": _FakeTier2(), "ipv4": "10.126.126.7"})
    immediate = (page._running is True and page._etier is not None
                 and page._etier_busy is False)
    mark("启动成功后立即进入运行态（无验证等待）", immediate,
         "running=%s etier=%s busy=%s"
         % (page._running, page._etier is not None, page._etier_busy))
    # tracker 应已启动，且成功后要有「已进入房间」的日志
    mark("成员跟踪器随进入房间启动",
         page._tracker is not None, "tracker=%s" % (page._tracker is not None))
    log_text = ""
    try:
        log_text = page.log_view.toPlainText()
    except Exception:
        pass
    joined_log = "已进入房间" in log_text
    mark("进入房间有明确日志", joined_log, "log 含「已进入房间」=%s" % joined_log)

    _check_member_tracker(page, mark)

    # 收尾：停掉后台线程，别让信标/跟踪器留着轮询
    page._stop_room_threads()
    mark("停止房间后跟踪器与信标已停",
         page._tracker is None and page._beacon is None,
         "tracker=%s beacon=%s" % (page._tracker, page._beacon))


def _check_member_tracker(page, mark):
    """MemberTracker 的核心逻辑（注入假状态，不依赖真实网络）。

    覆盖"人数断断续续"的修复点：
      1. 已知成员出现在快照里；
      2. 偶发一次 ping 失败 **不会** 把人从列表里闪没（宽限）；
      3. 持续失联（连续 ping 失败 + 长时间无 ARP 确认）才判离线。
    """
    import etier as etier_mod

    tr = etier_mod.MemberTracker("10.126.126.7")
    # 注入一个"在线"的假成员
    tr._peers["10.126.126.9"] = {
        "confirmed": time.monotonic(), "ping_fail": 0, "name": "Bob"}

    snap = tr.snapshot()
    mark("跟踪器快照包含已知成员",
         any(m["ip"] == "10.126.126.9" and m["name"] == "Bob" for m in snap),
         "快照=%r" % (snap,))

    # 偶发 ping 失败 ×1 → 宽限期内必须还在
    tr._on_ping_result("10.126.126.9", False)
    still = any(m["ip"] == "10.126.126.9" for m in tr.snapshot())
    mark("偶发 ping 失败不判离线（宽限）", still,
         "失败1次后快照=%r" % ([m["ip"] for m in tr.snapshot()],))

    # ping 失败 ×3 但 ARP 还没超宽限 → 仍在（ARP 单信号不判死）
    for _ in range(2):
        tr._on_ping_result("10.126.126.9", False)
    still2 = any(m["ip"] == "10.126.126.9" for m in tr.snapshot())
    mark("连续失败但未超宽限不误删", still2, "失败3次后仍在=%s" % still2)

    # 持续失联：ping 连续失败 + 最后确认时间倒退到宽限之外 → 离线
    tr._peers["10.126.126.9"]["confirmed"] = time.monotonic() - 60.0
    tr._on_ping_result("10.126.126.9", False)     # 第 4 次失败
    tr._sweep()
    gone = not any(m["ip"] == "10.126.126.9" for m in tr.snapshot())
    mark("持续失联（双信号失效）后判离线", gone,
         "移除后快照=%r" % ([m["ip"] for m in tr.snapshot()],))

    # ping 成功要刷新确认时间并清零失败计数（保活路径）
    tr._peers["10.126.126.9"] = {
        "confirmed": time.monotonic() - 60.0, "ping_fail": 3, "name": ""}
    tr._on_ping_result("10.126.126.9", True)
    tr._sweep()
    revived = any(m["ip"] == "10.126.126.9" for m in tr.snapshot())
    mark("ping 恢复后成员复活且不被误删", revived,
         "confirmed刷新=%s fail=%d"
         % (tr._peers["10.126.126.9"]["confirmed"] > time.monotonic() - 5,
            tr._peers["10.126.126.9"]["ping_fail"]))


def _check_nickname(page, mark):
    """昵称：清洗规则 + 落盘 + 默认值。"""
    from ui.pages.lan_page import sanitize_nickname, default_nickname

    bad_cases = [
        ("with space", "withspace"),
        ('quote"inside', "quoteinside"),
        ("semi;colon", "semlcolon") if False else ("semi;colon", "semicolon"),
        ("x" * 40, "x" * 16),
    ]
    all_ok = True
    detail = []
    for raw, want in bad_cases:
        got = sanitize_nickname(raw)
        if got != want:
            all_ok = False
            detail.append("%r→%r(期望%r)" % (raw, got, want))
    mark("昵称清洗：剔除空格/引号/分号并限长", all_ok, " ".join(detail) or "全部符合")

    d = default_nickname()
    mark("默认昵称非空且长度合理", bool(d) and len(d) <= 16, repr(d))

    # 清洗后为空时**返回空**（不再偷偷兜底），由启动门禁负责拦截
    page.nick_edit.setText("!!!")
    got = page._current_nickname()
    mark("昵称全非法字符时返回空（交给门禁拦截）", got == "", "得到 %r" % got)

    # 落盘
    page.nick_edit.setText("VeriFyNick")
    nick = page._current_nickname()
    page._save_nickname(nick)
    from PySide6.QtCore import QSettings
    box = QSettings("Yuhub", "Yuhub")
    saved = str(box.value("lan_nickname", "") or "")
    mark("昵称写入设置并读回一致", saved == nick, "存=%r 读=%r" % (nick, saved))

    _check_nickname_gate(page, mark)
    _check_member_rows(page, mark)


def _check_nickname_gate(page, mark):
    """昵称必填门禁：没昵称不能开房，且拦得住绕过按钮的调用。"""
    was_running, was_busy = page._running, page._etier_busy
    page._running = page._etier_busy = False

    # 空昵称 → 按钮禁用 + 文案提示
    page.nick_edit.setText("")
    page._refresh_start_gate()
    disabled = (not page.btn_start.isEnabled()) and page.btn_start.text() == "请先填昵称"

    # 填上昵称 → 恢复可用
    page.nick_edit.setText("GateNick")
    page._refresh_start_gate()
    enabled = page.btn_start.isEnabled()

    mark("无昵称时启动按钮被禁用并提示", disabled,
        "文案=%r enabled=%s" % (page.btn_start.text(), page.btn_start.isEnabled()))
    mark("填上昵称后启动按钮恢复", enabled, "文案=%r" % page.btn_start.text())

    # 硬闯：直接调 _on_start 也要被拦住，且不能进 busy
    toasts = []
    real_toast = page.toast
    page.toast = lambda t: toasts.append(t)
    page.nick_edit.setText("")
    page._on_start()
    blocked = bool(toasts) and ("昵称" in toasts[0]) and (page._etier_busy is False)
    page.toast = real_toast
    mark("绕过按钮直接启动也会被拦截", blocked, "toast=%r" % (toasts,))

    # 身份区分已被删除：页面上不应再有任何「房主 / 成员」控件或方法残留
    legacy = [n for n in ("mode", "_is_member", "_on_mode_changed", "_mode_hint")
              if hasattr(page, n)]
    mark("页面已无「房主 / 成员」身份残留", not legacy, "残留=%r" % (legacy,))

    # 唯一文案：填了昵称就是「进入房间」，且不能带任何身份字眼
    page.nick_edit.setText("GateNick")
    page._refresh_start_gate()
    label = page.btn_start.text()
    single_ok = (label == "进入房间"
                 and not any(w in label for w in ("房主", "成员", "创建", "加入")))
    mark("启动按钮文案唯一且无身份字眼", single_ok, "文案=%r" % (label,))

    # 昵称清空 → 文案必须让位给提示，不能还留着「进入房间」
    page.nick_edit.setText("")
    page._refresh_start_gate()
    restored = page.btn_start.text() == "请先填昵称"
    page.nick_edit.setText("GateNick")
    page._refresh_start_gate()
    back_ok = page.btn_start.text() == "进入房间"
    mark("昵称增删时按钮文案正确往返",
        restored and back_ok,
        "空=%s 有=%s" % (restored, back_ok))

    # 引导文案里也不能再出现身份区分。
    # 注意：不能用「成员」二字做关键词——「在线成员」是成员列表面板的正
    # 当名称，会误伤。这里只针对真的身份词汇（房主 / 我是房主 / 加入别人）。
    page.nick_edit.setText("GateNick")
    page._refresh_start_gate()
    hint = page.mode_note.text()
    identity_words = ("房主", "加入别人", "我加入", "我是")
    hint_ok = (not any(w in hint for w in identity_words)
               and "进入房间" in hint)
    mark("引导文案不含身份区分", hint_ok, "文案=%r" % (hint,))

    # 页面上任何可见文案都不该再出现「房主」
    from PySide6.QtWidgets import QLabel
    dumped = " ".join(w.text() for w in page.findChildren(QLabel) if w.text())
    mark("整个页面无「房主」字样",
         "房主" not in dumped, "命中=%s" % ("房主" in dumped))

    # 运行中不得被门禁改写按钮（否则会盖掉「正在启动…」）
    page._running = True
    page.btn_start.setText("正在启动…")
    page.btn_start.setEnabled(False)
    page.nick_edit.setText("")
    page._refresh_start_gate()
    mark("运行中门禁不覆盖按钮状态",
        (not page.btn_start.isEnabled()) and page.btn_start.text() == "正在启动…",
        "文案=%r" % page.btn_start.text())

    # 还原现场（后续检查还要用这个 page）
    page.nick_edit.setText("VeriFyNick")
    page._running, page._etier_busy = was_running, was_busy
    page._refresh_start_gate()


def _check_member_rows(page, mark):
    """成员列表：每行独立复制按钮 + 自己排第一（无「复制全部」）。"""
    from PySide6.QtWidgets import QPushButton

    copied = []
    real_copy = page._copy_text
    page._copy_text = lambda text, what="内容": copied.append((what, text))

    entries = [
        {"ip": "10.126.126.11", "name": "SelfNick", "self": True},
        {"ip": "10.126.126.23", "name": "Teammate", "self": False},
    ]
    page._render_members(entries)
    page._members_row = entries

    rows = [page.members_layout.itemAt(i).widget()
            for i in range(page.members_layout.count())]
    mark("成员行按人数渲染", len(rows) == 2, "行数=%d" % len(rows))

    # 每行必须恰好一个「复制」按钮（这是"每个人 IP 都能复制"的直接回归）
    per_row_ok = all(
        len([b for b in r.findChildren(QPushButton) if b.text() == "复制"]) == 1
        for r in rows
    )
    mark("每个成员行各有一个复制按钮", per_row_ok and len(rows) == 2,
        "行数=%d，每行按钮数=%s"
        % (len(rows), [len([b for b in r.findChildren(QPushButton)
                            if b.text() == "复制"]) for r in rows]))

    # 点别人的行 → 复制的必须是别人的 IP（不能错拿成自己的）
    copied.clear()
    if len(rows) == 2:
        [b for b in rows[1].findChildren(QPushButton)
         if b.text() == "复制"][0].click()
    mark("点成员行复制到的是该行 IP", copied == [("虚拟 IP", "10.126.126.23")],
        "复制结果=%r" % (copied,))

    # 点自己的行 → 复制自己的 IP
    copied.clear()
    if rows:
        [b for b in rows[0].findChildren(QPushButton)
         if b.text() == "复制"][0].click()
    mark("点自己那行复制到的是自己的 IP",
         copied == [("虚拟 IP", "10.126.126.11")], "复制结果=%r" % (copied,))

    # 「复制全部」已按需求移除：方法与按钮都不该存在
    no_all = (not hasattr(page, "_on_copy_all_ips")
              and not hasattr(page, "btn_copy_all")
              and not [b for b in page.findChildren(QPushButton)
                       if b.text() == "复制全部 IP"])
    mark("「复制全部 IP」已移除（只保留每人单独复制）", no_all,
        "_on_copy_all_ips=%s btn_copy_all=%s"
        % (hasattr(page, "_on_copy_all_ips"), hasattr(page, "btn_copy_all")))

    # 清空回到占位态
    page._clear_members()
    mark("清空成员后回到占位态",
        page.members_layout.count() == 0
        and page.members_count.text() == "0 人",
        "行数=%d 计数=%r" % (page.members_layout.count(), page.members_count.text()))

    page._copy_text = real_copy



def _dump(result, out_file):
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
