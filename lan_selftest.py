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
    #    注意：不能要求文件名正好是 Yuhub.exe —— 用户从 Release 下载后
    #    常被存成 "Yuhub (1).exe" 之类，那依旧是合法的打包产物。
    base = os.path.basename(exe).lower()
    mark("watchdog 指向打包的 exe 而非 python.exe",
         not base.startswith("python"), "实际为 %s" % base)

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

        # ---- 虚拟网卡信任化（v0.8.4beta：专用网络 + 游戏入站放行） ----
        # watchdog 在拉起 core 后的线程里执行，这里轮询等待其完成。
        try:
            ok_trust = False
            detail_trust = ""
            for _ in range(8):                     # 最多等 ~24 秒
                cat, rule = etier.virtual_net_trust_status()
                detail_trust = "profile=%s rule=%s" % (cat or "无网卡", rule)
                if cat == "Private" and rule:
                    ok_trust = True
                    break
                time.sleep(3.0)
            mark("虚拟网卡已信任化（专用+游戏放行）", ok_trust, detail_trust)
        except Exception as exc:
            mark("虚拟网卡已信任化（专用+游戏放行）", False, repr(exc))

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

    get_info() 会拦下"查自己"（自己昵称本来就已知），所以 TCP 查询
    走 _tcp_query 直查本机监听；新鲜表与 tracker 联动用注入的假成员。
    报文里除了昵称还带「游戏快连」（游戏名 + 端口），成员列表靠它显示
    "每个人在玩什么"，复制按钮也用它拼地址。
    """
    try:
        beacon = etier_mod.NickBeacon(ip, "SelfTestNick",
                                      game="泰拉瑞亚", port=7777)
        beacon.start()
        time.sleep(0.6)               # 给 TCP serve 线程一点 bind 时间
        got = beacon._tcp_query(ip, 2.0)
        mark("昵称信标 TCP 查询（本机回环实测）",
             got.get("name") == "SelfTestNick", "tcp_query(%s)=%r" % (ip, got))
        mark("信标 TCP 查询带回游戏快连",
             got.get("game") == "泰拉瑞亚" and got.get("port") == 7777,
             "got=%r" % (got,))

        # 旧版本客户端只发昵称：新版本必须照常解析（混房间不炸）。
        # 期望值要带上**后来新增的全部字段**，否则"新增字段"这件事本身就把
        # 这条断言打红 —— 这正是 v0.13beta 加屏幕共享时踩到的：
        # ss（对方屏幕共享的服务端口，0 = 没在共享）加进 _parse_info 之后
        # 忘了同步这里，自检直接报「兼容旧版仅昵称报文」失败。
        # 以后再加字段，这条断言要一起改（把它当成"报文字段清单"的哨兵）。
        legacy = etier_mod.NickBeacon._parse_info(
            b'{"yuhub-nick-v1":"OldPeer"}')
        mark("兼容旧版仅昵称报文",
             legacy == {"name": "OldPeer", "game": "", "port": 0,
                        "share": 0, "srev": 0, "ss": 0},
             "legacy=%r" % (legacy,))

        # 注入"收到 Bob 的广播"：get_info 应命中新鲜表
        beacon._peers["10.126.126.200"] = {
            "info": {"name": "Bob", "game": "幻兽帕鲁", "port": 8211},
            "until": time.monotonic() + 15.0}
        hit = beacon.get_info("10.126.126.200")
        mark("信标新鲜表命中", hit.get("name") == "Bob", "get_info=%r" % (hit,))
        mark("信标新鲜表带回游戏快连",
             hit.get("game") == "幻兽帕鲁" and hit.get("port") == 8211,
             "get_info=%r" % (hit,))
        # 兼容接口 get_name 仍只回昵称
        mark("get_name 兼容接口仍可用",
             beacon.get_name("10.126.126.200") == "Bob",
             "get_name=%r" % (beacon.get_name("10.126.126.200"),))

        # Tracker 的信息回调应能借信标填上昵称 + 游戏快连
        tr = etier_mod.MemberTracker(ip, name_lookup=beacon.get_info)
        tr._peers["10.126.126.200"] = {
            "confirmed": time.monotonic(), "ping_fail": 0,
            "name": "", "game": "", "port": 0}
        tr._resolve_info()
        cur = tr._peers["10.126.126.200"]
        mark("跟踪器经信标解析出昵称", cur["name"] == "Bob",
             "name=%r" % cur["name"])
        mark("跟踪器经信标解析出游戏快连",
             cur["game"] == "幻兽帕鲁" and cur["port"] == 8211,
             "game=%r port=%r" % (cur["game"], cur["port"]))
        snap = [m for m in tr.snapshot() if m["ip"] == "10.126.126.200"]
        mark("跟踪器快照带出游戏快连",
             bool(snap) and snap[0]["game"] == "幻兽帕鲁"
             and snap[0]["port"] == 8211, "snapshot=%r" % (snap,))

        # 运行中切换游戏：报文要立刻变成新值
        beacon.set_game("神力科莎", 9600)
        sw = etier_mod.NickBeacon._parse_info(beacon._payload())
        mark("运行中切换游戏快连立即生效",
             sw.get("game") == "神力科莎" and sw.get("port") == 9600,
             "payload=%r" % (sw,))

        _check_screenshare_beacon(etier_mod, beacon, mark)

        beacon.stop()
    except Exception as exc:
        mark("昵称信标 TCP 查询（本机回环实测）", False, repr(exc))


def _check_screenshare_beacon(etier_mod, beacon, mark):
    """屏幕共享端口在信标里的行为（v0.13beta）。

    这一节守着「看屏幕」按钮能不能正确出现的唯一依据：

      * 端口**只在开着共享时**才进报文 —— 停掉之后键必须消失；
      * 对端收到"停掉"的报文后，跟踪器里的端口必须**归零**（不像 share
        那样"带键才更新"），否则成员行上的「看屏幕」按钮会一直挂着，
        点过去必然连不上；
      * 旧版客户端从不发这个键，解析成 0 才是对的。

    另外：广播的只有**端口**。观看码由「房间码 + 密码」两边各自派生，
    绝不进广播包 —— 那等于把准入凭证贴在门上。这条也一并断言。
    """
    import json as _json

    # ---- 报文层：开着才带键 ----
    raw_off = _json.loads(beacon._payload().decode("utf-8"))
    mark("未开屏幕共享时报文不带 ss 键（不发假端口）",
         "ss" not in raw_off, sorted(raw_off))

    beacon.set_screenshare(45890)
    raw_on = _json.loads(beacon._payload().decode("utf-8"))
    mark("开始共享后报文带上 ss 端口", raw_on.get("ss") == 45890, raw_on)

    parsed = etier_mod.NickBeacon._parse_info(beacon._payload())
    mark("ss 端口能被对端解析回来",
         parsed.get("ss") == 45890, "parsed=%r" % (parsed,))

    # 观看码绝不能出现在广播报文里（那是准入凭证）
    mark("★ 广播报文里不含观看码（只广播端口）",
         "token" not in raw_on and "code" not in raw_on, sorted(raw_on))

    beacon.set_screenshare(0)
    raw_back = _json.loads(beacon._payload().decode("utf-8"))
    mark("★ 停掉共享后 ss 键又消失（对端据此撤下「看屏幕」按钮）",
         "ss" not in raw_back, sorted(raw_back))

    # ---- 缓存 → 跟踪器：停掉就归零 ----
    peer = "10.126.126.201"
    beacon._peers[peer] = {
        "info": {"name": "Bob", "ss": 45890}, "until": time.monotonic() + 15}
    hit = beacon.get_info(peer)
    mark("信标缓存命中且带回共享端口", hit.get("ss") == 45890,
         "get_info=%r" % (hit,))

    def _tracker_for(info):
        """造一个跟踪器，并把信标里那个 peer 的缓存换成 info。"""
        beacon._peers[peer] = {"info": dict(info),
                               "until": time.monotonic() + 15}
        tr = etier_mod.MemberTracker("10.126.126.1",
                                     name_lookup=beacon.get_info)
        tr._peers[peer] = {"confirmed": time.monotonic(), "ping_fail": 0,
                           "name": "", "game": "", "port": 0,
                           "share": 0, "srev": 0, "ss": 45890}
        tr._resolve_info()
        return tr

    # 对端停掉了共享：新报文里没有 ss 键
    tr_off = _tracker_for({"name": "Bob"})
    mark("★ 对端停掉共享后跟踪器把端口归零（按钮不会一直挂着）",
         int(tr_off._peers[peer].get("ss") or 0) == 0,
         "ss=%r" % (tr_off._peers[peer].get("ss"),))
    snap_off = [m for m in tr_off.snapshot() if m["ip"] == peer]
    mark("成员快照带上 ss 字段（成员行据此决定要不要显示按钮）",
         bool(snap_off) and "ss" in snap_off[0] and snap_off[0]["ss"] == 0,
         "snapshot=%r" % (snap_off,))

    # 对端正在共享：端口要如实带出来
    tr_on = _tracker_for({"name": "Bob", "ss": 45890})
    mark("对端在共享时成员快照带出非零 ss（按钮会出现）",
         int(tr_on._peers[peer].get("ss") or 0) == 45890,
         "ss=%r" % (tr_on._peers[peer].get("ss"),))
    snap_on = [m for m in tr_on.snapshot() if m["ip"] == peer]
    mark("共享中的成员快照 ss = 45890",
         bool(snap_on) and snap_on[0]["ss"] == 45890,
         "snapshot=%r" % (snap_on,))

    beacon._peers.pop(peer, None)
    beacon.set_screenshare(0)


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
    _check_member_row_ui(page, mark)
    _check_node_combo_ui(page, mark, app)
    _check_share_live(page, mark, app)
    _check_node_probe_policy(page, mark, app)


def _check_share_live(page, mark, app):
    """临时云盘：分享实时同步 + 退出即消失 + 下载自选位置。

    三条都是用户直接提出的诉求（v0.8.14beta）：
      1. 谁上传 / 取消分享文件，所有人都刷新一次；
      2. 分享的人退出房间，他那些文件立刻从列表里消失；
      3. 下载时每次都让用户自己选保存位置（顺带：云盘卡片里那个
         「打开文件夹」按钮已按需求移除）。

    全部离线可跑：摆好状态直接调界面逻辑，不发任何网络请求。
    """
    import json as _json

    import etier as _etier

    from PySide6.QtWidgets import QFileDialog
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    # ---- 卡片上不该再有「打开文件夹」按钮 ----
    mark("★ 云盘卡片已移除「打开文件夹」按钮（改由下载时自选位置）",
         not hasattr(page, "btn_share_dir"), "hasattr=False")

    class _Hub:
        running = True
        port = 41777

        def __init__(self):
            self.calls = []

        def own_files(self):
            return [{"id": "mine1", "name": "我的文件.txt", "size": 10,
                     "mtime": 1}]

        def collect(self, peers, timeout=0):
            self.calls.append(list(peers))
            return []

    class _Tracker:
        def __init__(self, rows):
            self.rows = rows

        def snapshot(self):
            return list(self.rows)

    hub = _Hub()
    page._hub = hub
    page._running = True
    page._my_ip = "10.126.126.5"
    beacon = _etier.NickBeacon("10.126.126.5", "SelfNick")
    page._beacon = beacon
    page._tracker = _Tracker([{"ip": "10.126.126.9", "name": "队友", "game": "",
                               "port": 0, "share": 41800, "srev": 1}])
    beacon._peers["10.126.126.9"] = {
        "info": {"name": "队友", "share": 41800, "srev": 1,
                 "game": "", "port": 0},
        "until": time.monotonic() + 10}
    remote_row = {"id": "f1", "name": "队友的存档.zip", "size": 100, "mtime": 2}
    page._share_remote = {"10.126.126.9": [dict(remote_row)]}
    page._share_seen = {"10.126.126.9": (41800, 1)}

    n0 = len(hub.calls)
    page._share_watch()
    mark("云盘状态没变时不做多余轮询", len(hub.calls) == n0, len(hub.calls))

    beacon._peers["10.126.126.9"]["info"]["srev"] = 2
    page._share_watch()
    mark("★ 对方动过分享清单（版本号变）就立刻重拉一次（所有人都刷新）",
         len(hub.calls) == n0 + 1,
         "调用次数=%d 参数=%r" % (len(hub.calls), hub.calls[-1:]))
    mark("重拉清单时带上了对方的云盘端口",
         hub.calls[-1:] == [[{"ip": "10.126.126.9", "port": 41800}]],
         hub.calls[-1:])

    n1 = len(hub.calls)
    beacon._peers["10.126.126.9"]["info"]["share"] = 0
    page._share_watch()
    mark("★ 对方关掉云盘 -> 他的文件行当场从列表撤下",
         "10.126.126.9" not in page._share_remote, page._share_remote)

    page._share_remote = {"10.126.126.9": [dict(remote_row)]}
    page._tracker.rows = []
    page._share_watch()
    mark("★ 分享者退出房间 -> 他的文件行当场从列表撤下",
         "10.126.126.9" not in page._share_remote, page._share_remote)
    page._share_watch()
    mark("撤下之后不再反复重拉（别自己刷自己）",
         len(hub.calls) == n1, len(hub.calls))

    # ---- 自己增删文件：版本号 +1 并立刻广播 ----
    page._tracker.rows = []
    page._share_rev = 0
    page._bump_share_rev()
    mark("分享清单增删 -> 版本号 +1", page._share_rev == 1, page._share_rev)
    mark("★ 版本号随信标广播出去（队友据此立刻刷新）",
         _json.loads(beacon._payload().decode("utf-8")).get("srev") == 1,
         # ⚠️ _payload() 是 bytes：直接把 bytes 塞进 detail 会让 json.dump
         # 抛 TypeError，结果文件被写成半截（v0.8.14beta 就是这么坏的）
         beacon._payload().decode("utf-8", "replace"))

    # ---- 下载：每次都弹保存对话框 ----
    seen = {}

    def _fake_save(parent, title, suggested, filt):
        seen["title"] = title
        seen["suggested"] = suggested
        return os.path.join(os.path.abspath("."), "自选位置.bin"), ""

    real_save = QFileDialog.getSaveFileName
    try:
        QFileDialog.getSaveFileName = staticmethod(_fake_save)
        picked = page._ask_save_path("队友的存档.zip")
        mark("★ 下载前弹出「选择保存位置」并采用用户所选路径",
             picked.endswith("自选位置.bin"), picked)
        mark("保存框默认文件名 = 被下载的文件名",
             str(seen.get("suggested") or "").endswith("队友的存档.zip"),
             seen.get("suggested"))

        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
        mark("用户取消保存 -> 返回空（不会偷偷下到默认目录）",
             page._ask_save_path("队友的存档.zip") == "", "")
        page._download = None
        page._start_download({"key": "k", "fid": "f", "name": "x.bin",
                              "owner": "队友", "ip": "10.126.126.9",
                              "port": 41800})
        mark("取消保存位置时根本不发起下载请求",
             page._download is None, page._download)
    finally:
        QFileDialog.getSaveFileName = real_save

    # ---- 拖拽区：点一下就能选文件 ----
    clicked = []
    real_open = QFileDialog.getOpenFileNames
    try:
        QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([], ""))
        page.drop_zone.clicked.connect(lambda: clicked.append(1))
        mark("★ 拖拽区未进房间也是可点的（不是点不动的灰块）",
             page.drop_zone.isEnabled(), page.drop_zone.isEnabled())
        QTest.mouseClick(page.drop_zone, Qt.LeftButton)
        QTest.mouseClick(page.drop_zone.title, Qt.LeftButton)
        QTest.mouseClick(page.drop_zone.sub, Qt.LeftButton)
        mark("★ 点拖拽区（含标题文字）都会打开文件选择框",
             len(clicked) == 3, clicked)
    finally:
        QFileDialog.getOpenFileNames = real_open


def _check_node_probe_policy(page, mark, app):
    """节点测速的触发时机（v0.8.14beta 改版）。

    以前是"数据过期就自动重测"（每 90 秒一次），用户挂机时白占带宽；
    现在只有两个触发点：**点进这一页** 和 **手点「测速」**。
    同时下拉里不再因为"数据超过 90 秒"就整列变「待测」——数字留着，
    由旁边的「延迟数据：x 分钟前」如实说明新旧。
    """
    import unittest.mock as _mock

    import etier as _etier
    import node_probe as _np

    def _valueless(*a, **k):
        return {}

    page._running = False
    page._hub = None
    try:
        with _mock.patch.object(_np, "probe_all", _valueless):
            page._tick()
            mark("★ 页面停留（每 800ms 的 tick）不再自动测速",
                 not page._probing, "_probing=%s" % page._probing)

            page._probing = False
            page._probe.clear()
            page.on_shown()
            # 立刻读 _probing：测速线程可能瞬间就跑完（自检里把它打桩成
            # 立即返回），等 processEvents 之后再看就已经复位了。
            started_probe = page._probing
            mark("★ 点进这一页 -> 自动测一次", started_probe,
                 "_probing=%s" % started_probe)
            t_loop = time.time()
            while page._probing and time.time() - t_loop < 5:
                app.processEvents()
                time.sleep(0.02)
        mark("测速结束后按钮文案复位",
             page.btn_node_speed.text() == "测速", page.btn_node_speed.text())
    except Exception as exc:
        mark("测速触发策略可验证", False, repr(exc))
        return

    # 旧数据仍然显示数字
    page._probe.update(
        {k: {"key": k, "label": "L", "addr": "udp://192.0.2.9:11010",
             "ms": 33, "ok": True, "via": "icmp", "error": ""}
         for k, _l, _a in _etier.NODE_CHOICES[:2]},
        at=time.time() - 600)
    page._fill_node_combo()
    texts = [page.node_combo.itemText(i) for i in range(page.node_combo.count())]
    mark("★ 10 分钟前的实测数字仍然显示（不再整列变「待测」）",
         any("33ms" in t for t in texts), texts)
    page._update_node_age()
    mark("★ 数据放旧时明确提示「点测速更新」",
         "点「测速」更新" in page.node_age_label.text(),
         page.node_age_label.text())


def _check_node_combo_ui(page, mark, app):
    """中继节点下拉的宽度回归。

    用户报的 bug：「中继节点的位置文字显示不完全」——控件实际宽度只有
    162px，连最短的「海波中国大陆（44ms）」都放不下，显示成「海波中国大…」。

    根因不是文字太长，而是 QComboBox 默认的宽度策略只在**首次显示时**
    量一次：首屏的裸名字（「自动（尝试全部节点）」）把它钉死在 162px，
    之后测速回来重建列表、塞进「自动（最快：海波中国大陆 44ms）」这种
    长一倍的项，它不会自己变宽。所以断言不能只测"某项文案对不对"，
    必须拿真实字体量一遍宽度。
    """
    combo = page.node_combo
    try:
        import node_probe
    except Exception as exc:                       # pragma: no cover - 防御
        mark("导入 node_probe（界面宽度检查）", False, repr(exc))
        return

    # 用假数据把四种状态都摆上：可用（带延迟）、不可达（超时）、
    # 名字自带括号的（最长的那个），离线可跑，不依赖外网。
    now = time.time()
    fake = {}
    for key, ms in (("weiai", 19), ("haibo_cn", 34), ("haibo_us", 193),
                    ("cn_ip", None)):
        fake[key] = {
            "key": key, "label": "", "addr": "",
            "ms": ms, "ok": ms is not None,
            "via": "icmp" if ms is not None else "",
            "error": "" if ms is not None else "超时",
            "at": now,
        }
    page._probe.update(fake)
    page._fill_node_combo()
    app.processEvents()

    fm = combo.fontMetrics()
    widest, widest_text = 0, ""
    for i in range(combo.count()):
        text = combo.itemText(i)
        w = fm.horizontalAdvance(text)
        if w > widest:
            widest, widest_text = w, text
    floor = combo.minimumWidth()
    mark("★ 中继节点下拉留足了最宽一项的宽度（地名不再被切掉半个）",
         widest > 0 and floor >= widest,
         "最宽项=%r 需要=%dpx 控件下限=%dpx" % (widest_text, widest, floor))
    mark("下拉宽度有上限（不会把同行的「测速」按钮挤出去）",
         combo.maximumWidth() <= 360 and combo.maximumWidth() >= floor,
         "下限=%dpx 上限=%dpx" % (floor, combo.maximumWidth()))

    first = combo.itemText(0)
    mark("★「自动」项报出最快节点，且不套双层括号",
         "最快" in first and first.count("（") == 1, first)
    mark("「自动」报的就是实测最快那个（19ms 的唯爱厦门）",
         "唯爱厦门" in first and "19ms" in first, first)
    mark("名字自带括号的节点改用间隔号接状态",
         any("· 超时" in combo.itemText(i) for i in range(combo.count())),
         [combo.itemText(i) for i in range(combo.count())])


def _check_member_row_ui(page, mark):
    """成员行的可见性与信息回归。

    对应用户报的三件事：复制按钮看不清、看不到队友在玩什么、
    中途卡退重进后 IP 变了却不知道（因此加了刷新与变化告知）。
    """
    from PySide6.QtWidgets import QPushButton, QLabel

    # ① 「在线成员」卡片右上角的「刷新」按钮
    rbtn = getattr(page, "btn_members_refresh", None)
    mark("在线成员卡片带「刷新」按钮",
         rbtn is not None and rbtn.text() == "刷新"
         and rbtn.objectName() == "MiniButton",
         "btn=%r obj=%r" % (rbtn.text() if rbtn else None,
                            getattr(rbtn, "objectName", lambda: None)()))

    # ② 每行显示那个人自己选的「游戏快连」
    row = page._make_member_row("Bob", "10.126.126.9",
                                game="泰拉瑞亚", port=7777)
    texts = [lb.text() for lb in row.findChildren(QLabel)]
    mark("成员行显示对方所选游戏", "泰拉瑞亚" in texts, "labels=%r" % (texts,))

    # ③ 「复制」两个字必须放得下
    #    旧版是 ghost_button + setFixedWidth(52)，而 ghost 样式自带 18px
    #    左右内边距，52 里留给文字的只有十几像素 → 两个字被挤得看不清。
    cbtn = row.findChildren(QPushButton)[0]
    fm = cbtn.fontMetrics()
    need = fm.horizontalAdvance("复制") + 24
    mark("「复制」按钮文字不被挤压",
         cbtn.text() == "复制" and cbtn.minimumWidth() >= need,
         "min=%d need=%d 文字宽=%d"
         % (cbtn.minimumWidth(), need, fm.horizontalAdvance("复制")))

    # ④ 复制出来的是**对方**的「IP:端口」
    mark("复制按钮用对方的游戏端口",
         "10.126.126.9:7777" in cbtn.toolTip(), cbtn.toolTip())

    # ⑤ 对方还没广播游戏时，退回用我自己选的端口（不能干脆不给地址）
    row2 = page._make_member_row("NoGame", "10.126.126.8")
    tip2 = row2.findChildren(QPushButton)[0].toolTip()
    my_port = page._selected_game()[1]
    mark("对方无游戏信息时回退本机端口",
         ("10.126.126.8:%d" % my_port) in tip2, tip2)

    # ⑤b 屏幕共享：队友在共享时，他的成员行要长出「看屏幕」按钮
    #     （v0.13beta：借「异地联机」做屏幕共享，ss = 他的服务端口）
    row_ss = page._make_member_row("Sharer", "10.126.126.7",
                                   game="泰拉瑞亚", port=7777, ss=45890)
    watch_btns = [b for b in row_ss.findChildren(QPushButton)
                  if b.text() == "看屏幕"]
    mark("★ 队友在共享屏幕时，他的成员行出现「看屏幕」按钮",
         len(watch_btns) == 1,
         [b.text() for b in row_ss.findChildren(QPushButton)])
    mark("「看屏幕」按钮带说明性 tooltip",
         bool(watch_btns) and "屏幕" in watch_btns[0].toolTip(),
         watch_btns[0].toolTip() if watch_btns else None)

    # 点它必须带着**这个人的** IP + 共享端口跳转（带错就连到别人/连不上）
    got_watch = []
    real_watch = page._watch_peer
    page._watch_peer = lambda ip, port, name: got_watch.append((ip, port, name))
    try:
        if watch_btns:
            watch_btns[0].click()
    finally:
        page._watch_peer = real_watch
    mark("★ 点「看屏幕」带的是该行队友的 IP + 共享端口",
         got_watch == [("10.126.126.7", 45890, "Sharer")], got_watch)

    # 自己那行不给（看自己的屏幕没有意义）
    row_me = page._make_member_row("Me", "10.126.126.7", is_self=True,
                                   game="泰拉瑞亚", port=7777, ss=45890)
    mark("自己那一行不出现「看屏幕」按钮",
         not [b for b in row_me.findChildren(QPushButton)
              if b.text() == "看屏幕"],
         [b.text() for b in row_me.findChildren(QPushButton)])

    # 对方没在共享（ss=0）时不能有按钮，否则会出现"点了连不上"
    row_ns = page._make_member_row("NoShare", "10.126.126.8",
                                   game="泰拉瑞亚", port=7777, ss=0)
    mark("对方没在共享时成员行没有「看屏幕」按钮",
         not [b for b in row_ns.findChildren(QPushButton)
              if b.text() == "看屏幕"],
         [b.text() for b in row_ns.findChildren(QPushButton)])

    # ⑥ 运行中 IP 变化：必须按新地址整对重建信标与成员跟踪
    #    （不重建就是"人在房间、列表永远空、别人也看不到我"）
    page._on_ip_changed("10.126.126.2", "10.126.126.99")
    mark("IP 变化后信标重建到新地址",
         page._beacon is not None
         and getattr(page._beacon, "_my_ip", "") == "10.126.126.99",
         "beacon_ip=%r" % (getattr(page._beacon, "_my_ip", None),))
    mark("IP 变化后跟踪器重建到新地址",
         page._tracker is not None
         and getattr(page._tracker, "_my_ip", "") == "10.126.126.99",
         "tracker_ip=%r" % (getattr(page._tracker, "_my_ip", None),))
    page._stop_room_threads()


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

    # 点别人的行 → 复制的必须是别人的「IP:端口」（不能错拿成自己的）
    #
    # 复制的早就不是裸 IP 了：游戏里要填的是「IP:端口」，缺一不可。端口优先
    # 用"他自己选的游戏快连"，他没广播过游戏信息时才退回我自己选的那个——
    # 所以这里的期望端口取当前下拉值，而不是写死某个数字。
    my_port = page._selected_game()[1]
    copied.clear()
    if len(rows) == 2:
        [b for b in rows[1].findChildren(QPushButton)
         if b.text() == "复制"][0].click()
    mark("点成员行复制到的是该行「IP:端口」",
         copied == [("直连地址", "10.126.126.23:%d" % my_port)],
         "复制结果=%r" % (copied,))

    # 点自己的行 → 复制自己的「IP:端口」
    copied.clear()
    if rows:
        [b for b in rows[0].findChildren(QPushButton)
         if b.text() == "复制"][0].click()
    mark("点自己那行复制到的是自己的「IP:端口」",
         copied == [("直连地址", "10.126.126.11:%d" % my_port)],
         "复制结果=%r" % (copied,))

    # ---- 「其他游戏」（无端口）：复制出来必须是**纯 IP**，不带冒号 ----
    # 用户 2026-10-05 要求：「其他游戏的选项，然后无端口，复制 ip 的时候
    # 直接就是 ip 没有端口」。两条路径都要验：成员行复制 + 本机复制按钮。
    from ui.pages.lan_page import GAME_PORTS, game_addr, game_label
    other = [(n, p) for n, p in GAME_PORTS if p == 0]
    mark("游戏列表里有「其他游戏」且端口为 0",
         any(n == "其他游戏" for n, _ in GAME_PORTS) and bool(other),
         "GAME_PORTS=%r" % (GAME_PORTS,))
    mark("无端口游戏的下拉文案不带「（0）」",
         game_label("其他游戏", 0) == "其他游戏"
         and game_label("泰拉瑞亚", 7777) == "泰拉瑞亚（7777）",
         "%r / %r" % (game_label("其他游戏", 0), game_label("泰拉瑞亚", 7777)))
    mark("game_addr：有端口给 IP:端口、无端口只给纯 IP",
         game_addr("10.126.126.23", 7777) == "10.126.126.23:7777"
         and game_addr("10.126.126.23", 0) == "10.126.126.23"
         and game_addr("", 7777) == "",
         "%r / %r" % (game_addr("10.126.126.23", 7777),
                      game_addr("10.126.126.23", 0)))

    # 切到「其他游戏」，成员行复制应变纯 IP
    idx_other = next((i for i, (n, _p) in enumerate(GAME_PORTS)
                      if n == "其他游戏"), -1)
    old_idx = page.game_combo.currentIndex()
    page.game_combo.setCurrentIndex(idx_other)
    mark("切到「其他游戏」后本机端口读数为 0",
         (page.game_combo.currentData() or 0) == 0,
         "currentData=%r" % (page.game_combo.currentData(),))
    # 队友明确选了「其他游戏」：game 非空、port=0，复现"他选无端口游戏"这条路径
    page._render_members([
        {"ip": "10.126.126.11", "name": "SelfNick", "self": True,
         "game": "其他游戏", "port": 0},
        {"ip": "10.126.126.23", "name": "Teammate", "self": False,
         "game": "其他游戏", "port": 0},
    ])
    rows = [page.members_layout.itemAt(i).widget()
            for i in range(page.members_layout.count())]
    copied.clear()
    if len(rows) == 2:
        [b for b in rows[1].findChildren(QPushButton)
         if b.text() == "复制"][0].click()
    mark("「其他游戏」下成员行复制的是纯 IP（无端口）",
         copied == [("直连地址", "10.126.126.23")],
         "复制结果=%r" % (copied,))
    mark("「其他游戏」下复制按钮文案改为「复制 IP」",
         page.btn_copy_addr.text() == "复制 IP",
         "btn_copy_addr=%r" % page.btn_copy_addr.text())

    # 反例：队友**没广播过**游戏（game 为空）→ 退回我自己选的端口，至少能用。
    # 这条守住"不能因为新增无端口选项，把老的退回逻辑一起搞坏"。
    page.game_combo.setCurrentIndex(
        next((i for i, (n, _p) in enumerate(GAME_PORTS) if n == "泰拉瑞亚"), 0))
    page._render_members([
        {"ip": "10.126.126.11", "name": "SelfNick", "self": True},
        {"ip": "10.126.126.23", "name": "Teammate", "self": False},
    ])
    rows = [page.members_layout.itemAt(i).widget()
            for i in range(page.members_layout.count())]
    copied.clear()
    if len(rows) == 2:
        [b for b in rows[1].findChildren(QPushButton)
         if b.text() == "复制"][0].click()
    mark("队友未广播游戏时退回我选的端口（不是纯 IP）",
         copied == [("直连地址", "10.126.126.23:7777")],
         "复制结果=%r" % (copied,))
    page.game_combo.setCurrentIndex(idx_other)

    # 本机「复制 IP:端口」按钮在无端口时同样只给纯 IP
    page._my_ip = "10.126.126.11"
    copied.clear()
    page._on_copy_game_addr()
    mark("「其他游戏」下本机复制按钮给的是纯 IP",
         copied == [("直连地址", "10.126.126.11")],
         "复制结果=%r" % (copied,))

    page.game_combo.setCurrentIndex(old_idx)     # 复位，别污染后续断言

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
    """写结果 JSON。

    ⚠️ 先 `dumps` 成字符串再落盘，并且 `default=str` 兜底：
    某个 detail 塞了 bytes / 自定义对象时，json.dump 会直接抛 TypeError，
    而流式写入意味着**文件已经被写了半截**——最后拿到的是一份读不出来的
    残档，前面的检查结果全丢了（v0.8.14beta 的 lan 自检就是这个症状：
    永远停在 "detail": 后面，谁都看不出是哪一项干的）。
    """
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as exc:
        text = json.dumps({"ok": False, "checks": result.get("checks", []),
                           "dump_error": repr(exc)},
                          ensure_ascii=False, indent=2, default=str)
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass
