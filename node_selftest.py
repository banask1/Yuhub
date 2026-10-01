# -*- coding: utf-8 -*-
"""打包后自检：中继节点测速（方案 A）。

为什么需要它
------------
这个功能的全部价值压在两条承诺上，而两条都**不会抛异常**，坏掉时界面看着
一切正常：

  1. **延迟是本机实测的**——如果探测退化成"永远拿不到数据"，下拉就退回
     裸节点名，用户以为"这功能就这样"，实际是探测坏了；
  2. **数字是新鲜的**——TTL 一旦失效，用户会拿着半小时前的 26ms 去选一个
     此刻已经 300ms 的节点。这条没有任何报错，只能靠断言守住。

所以下面用**本地监听 / 保留地址**构造各种链路状态（不依赖外网、不碰
EasyTier、不要管理员权限），把上面两条固化成断言：

  - 本地开一个 TCP 服务 → 能测出握手延迟（模拟可用节点）
  - 往一个关闭的本机端口打 → "ping 通但端口不通"必须被标出来，
    且**不能**算作可用
  - TEST-NET 保留地址（192.0.2.0/24、203.0.113.0/24，永不路由）→ 超时，
    且错误原因是"超时"而不是"域名解析失败"
  - 不存在的域名 → "域名解析失败"
  - 把缓存里的时间戳往前挪 → 过期数据必须从下拉里消失

用法：`Yuhub.exe --node-selftest <结果json路径>`
返回码：0 全部通过 / 1 有断言失败 / 2 参数错误 / 4 结果写盘失败
"""

import json
import socket
import time


def _finish(out_file, result):
    checks = result["checks"]
    result["ok"] = bool(checks) and all(c["pass"] for c in checks)
    result["info"]["passed"] = sum(1 for c in checks if c["pass"])
    result["info"]["total"] = len(checks)
    # 先 dumps 再落盘 + default=str：detail 里混进 bytes / 自定义对象时，
    # json.dump 会抛 TypeError，而它是**边序列化边写**的——文件已被写了
    # 半截，最后拿到的是一份读不出来的残档，前面的检查结果全丢
    #（v0.8.14beta 的 lan 自检就这么坏过一次）。
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2,
                                default=str))
    except (OSError, TypeError, ValueError):
        return 4
    return 0 if result["ok"] else 1


# 测试用的假节点表：形状与 etier.NODE_CHOICES 一致（key, 显示名, 地址）。
# 「自动」那条地址为空，正是要验证"它不是节点、不该被测"。
def _choices(local_port):
    return (
        ("auto", "自动（尝试全部节点）", ""),
        ("local", "本地测试节点", "tcp://127.0.0.1:%d" % local_port),
        ("closed", "本地关闭端口", "tcp://127.0.0.1:1"),
        ("dead", "保留地址", "udp://192.0.2.77:11010"),
        ("badhost", "坏域名", "udp://no-such-host-yuhub-test.invalid:11010"),
    )


def run(out_file, timeout_sec=120):
    checks = []
    result = {"ok": False, "checks": checks, "info": {}}

    def chk(name, cond, detail=""):
        checks.append({"name": name, "pass": bool(cond), "detail": str(detail)})

    try:
        import node_probe as np
    except Exception as exc:                       # pragma: no cover - 防御
        chk("导入 node_probe", False, repr(exc))
        return _finish(out_file, result)

    started = time.monotonic()

    # 开一个本地 TCP 服务当"可用节点"
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port = srv.getsockname()[1]
    choices = _choices(port)

    try:
        # ------------------------------------------------------ A 地址解析
        chk("解析 tcp:// 地址",
            np.split_address("tcp://225284.xyz:11010") == ("tcp", "225284.xyz", 11010),
            np.split_address("tcp://225284.xyz:11010"))
        chk("解析 udp:// 地址",
            np.split_address("udp://us01.225284.xyz:11010")
            == ("udp", "us01.225284.xyz", 11010),
            np.split_address("udp://us01.225284.xyz:11010"))
        chk("空地址（auto）不是节点", np.split_address("") is None)
        chk("缺端口的地址不合法", np.split_address("tcp://host") is None)
        chk("未知协议不合法", np.split_address("http://host:80") is None)
        chk("端口越界不合法", np.split_address("tcp://host:99999") is None)
        chk("IPv6 字面量能拆对",
            np.split_address("tcp://[::1]:11010") == ("tcp", "::1", 11010),
            np.split_address("tcp://[::1]:11010"))
        chk("is_ip_literal 认 IP",
            np.is_ip_literal("192.0.2.1") and not np.is_ip_literal("a.b.c"))

        # ------------------------------------------------------ B 三种探测手段
        rtt = np.tcp_rtt_ms("127.0.0.1", port)
        chk("★ TCP 握手能测出延迟（可用节点的判定基础）",
            rtt is not None and 0 <= rtt < 3000, rtt)
        chk("TCP 连不上的端口返回 None",
            np.tcp_rtt_ms("127.0.0.1", 1, timeout=0.5, attempts=1) is None)
        before = socket.getdefaulttimeout()
        np.resolve_ok("us01.225284.xyz")
        chk("resolve_ok 不改动进程级默认超时（测速在后台线程，全局开关不能碰）",
            socket.getdefaulttimeout() == before,
            (before, socket.getdefaulttimeout()))
        chk("resolve_ok 认 IP 字面量", np.resolve_ok("192.0.2.77"))
        chk("resolve_ok 对不存在的域名返回 False",
            not np.resolve_ok("no-such-host-yuhub-test.invalid"))
        icmp = np.icmp_rtt_ms("127.0.0.1")
        chk("★ ICMP 回环能测出延迟（udp 节点的唯一退路）",
            icmp is not None and icmp < 3000, icmp)
        t0 = time.monotonic()
        icmp_dead = np.icmp_rtt_ms("192.0.2.77", timeout=0.5, count=1)
        chk("ICMP 对保留地址返回 None（不等于 0）", icmp_dead is None, icmp_dead)
        chk("ICMP 不会超时失控（每轮受 -w/次数约束）",
            time.monotonic() - t0 < 12.0, round(time.monotonic() - t0, 2))

        # ping 输出解析：中文/英文、以及"从统计行里捡 0"这个致命误判
        chk("解析中文 ping 回复行",
            np._RE_RTT.findall("来自 1.2.3.4 的回复: 字节=32 时间=208ms TTL=54")
            == ["208"])
        chk("解析「时间<1ms」",
            np._RE_RTT.findall("来自 1.2.3.4 的回复: 字节=32 时间<1ms TTL=128")
            == ["1"])
        chk("解析英文 ping 回复行",
            np._RE_RTT.findall("Reply from 1.2.3.4: bytes=32 time=208ms TTL=54")
            == ["208"])
        chk("★ 全丢包时的统计行（无「回复」）不被当成延迟",
            not np._RE_REPLY.search(
                "数据包: 已发送 = 1，已接收 = 0，丢失 = 1 (100% 丢失)\n"
                "最短 = 0ms，最长 = 0ms，平均 = 0ms"))

        # ------------------------------------------------------ C probe 整合
        t0 = time.monotonic()
        rec = np.probe("local", "本地测试节点", "tcp://127.0.0.1:%d" % port,
                       timeout=0.5)
        chk("probe 对可用节点给出 ok", rec["ok"] and rec["via"] == "tcp", rec)
        chk("probe 结果带毫秒数", isinstance(rec["ms"], int), rec["ms"])
        chk("probe 结果带测量时间戳",
            abs(rec["at"] - time.time()) < 30, rec["at"])

        rec2 = np.probe("closed", "本地关闭端口", "tcp://127.0.0.1:1",
                        timeout=0.5, ping_count=1)
        chk("★ ping 通但端口不通 → 不算可用（别让用户以为能连）",
            (not rec2["ok"]) and rec2["error"] == "端口不通", rec2)
        chk("该情形仍保留主机延迟（便于判断主机是否活着）",
            rec2["ms"] is not None and rec2["via"] == "icmp", rec2)

        rec3 = np.probe("dead", "保留地址", "udp://192.0.2.77:11010",
                        timeout=0.4, ping_count=1)
        chk("保留地址判为不可用", not rec3["ok"], rec3)
        chk("★ IP 直连的不可达说「超时」，不误报域名解析失败",
            rec3["error"] == "超时", rec3["error"])

        rec4 = np.probe("badhost", "坏域名",
                        "udp://no-such-host-yuhub-test.invalid:11010",
                        timeout=0.4, ping_count=1)
        chk("★ 域名没了要说「域名解析失败」（节点下线，不是自己没网）",
            rec4["error"] == "域名解析失败", rec4["error"])

        rec5 = np.probe("auto", "自动", "", timeout=0.4)
        chk("空地址不会被当成节点测", (not rec5["ok"]) and rec5["error"] == "非节点地址",
            rec5)

        # ------------------------------------------------------ D 并发
        t1 = time.monotonic()
        np.probe("solo", "solo", "udp://203.0.113.9:11010",
                 timeout=0.4, ping_count=1)
        solo = time.monotonic() - t1
        items = [("n%d" % i, "N%d" % i, "udp://203.0.113.%d:11010" % (10 + i))
                 for i in range(4)]
        t6 = time.monotonic()
        res6 = np.probe_all(items, timeout=0.4, ping_count=1)
        many = time.monotonic() - t6
        result["info"]["solo_s"] = round(solo, 2)
        result["info"]["four_s"] = round(many, 2)
        chk("probe_all 返回全部探测结果", len(res6) == 4, len(res6))
        chk("★ 多节点是并发跑的（串行会是单节点的 4 倍）",
            many < solo * 2.6, "solo=%.2fs four=%.2fs" % (solo, many))

        skipped = np.probe_all([("auto", "自动", ""),
                                ("k", "K", "tcp://127.0.0.1:1")],
                               timeout=0.4, ping_count=1)
        chk("probe_all 跳过没有地址的「自动」项",
            "auto" not in skipped and "k" in skipped, list(skipped))

        stop = {"n": 0}

        def _want_stop():
            stop["n"] += 1
            return stop["n"] > 1

        np.probe_all(items, timeout=0.4, should_stop=_want_stop, ping_count=1)
        chk("probe_all 支持中途收手（页面已销毁时不再耗资源）", stop["n"] >= 1,
            stop["n"])

        # ------------------------------------------------------ E TTL 缓存
        cache = np.NodeProbeCache(ttl=10.0)
        chk("新缓存没有数据", (not cache.has_data()) and cache.age() is None)
        chk("★ 从没测过 = 过期（首屏必须自动测一次）", cache.is_stale())
        cache.update({"local": dict(rec), "dead": dict(rec3)})
        chk("update 后 age 归零",
            cache.age() is not None and cache.age() < 1.0, cache.age())
        chk("update 后不再是过期态", not cache.is_stale())
        chk("results() 只吐出当前有效的条目",
            set(cache.results()) == {"local", "dead"}, list(cache.results()))

        aged = {"local": dict(rec, at=time.time() - 100),
                "dead": dict(rec3, at=time.time() - 100)}
        cache2 = np.NodeProbeCache(ttl=10.0)
        cache2.update(aged, at=time.time() - 100)
        chk("★ 超过 TTL 判为过期", cache2.is_stale())
        chk("★ 过期条目不出现在 results()（不给用户看旧数字）",
            cache2.results() == {}, list(cache2.results()))
        chk("过期条目仍可取原始快照（自检/日志用）",
            set(cache2.snapshot()) == {"local", "dead"}, list(cache2.snapshot()))
        chk("keep_stale=True 时能看到过期条目",
            len(cache2.results(keep_stale=True)) == 2)
        chk("get() 对过期条目返回 None", cache2.get("local") is None)

        cache3 = np.NodeProbeCache(ttl=10.0)
        cache3.update({"local": dict(rec), "dead": dict(rec3)})
        cache3.update({"local": dict(rec)})          # 第二轮只测到一个
        chk("新的一轮结果会顶掉旧的（没测到的不会顶着老数字冒充刚测过）",
            set(cache3.snapshot()) == {"local"}, list(cache3.snapshot()))
        cache3.clear()
        chk("clear 之后回到未测状态",
            (not cache3.has_data()) and cache3.is_stale())

        # ------------------------------------------------------ F 展示与排序
        now = time.time()
        fake = {
            "local": {"key": "local", "label": "本地测试节点", "ms": 26,
                      "ok": True, "via": "tcp", "error": "", "at": now},
            "closed": {"key": "closed", "label": "本地关闭端口", "ms": 1,
                       "ok": False, "via": "icmp", "error": "端口不通", "at": now},
            "dead": {"key": "dead", "label": "保留地址", "ms": None,
                     "ok": False, "via": "", "error": "超时", "at": now},
            "badhost": {"key": "badhost", "label": "坏域名", "ms": None,
                        "ok": False, "via": "", "error": "域名解析失败", "at": now},
        }
        decorated = np.decorate_choices(choices, fake)
        keys = [d[0] for d in decorated]
        chk("★「自动」恒排第一（它是模式，不该被排序挤走）",
            keys[0] == "auto", keys)
        chk("★ 可用节点按延迟升序排在前面",
            keys[1] == "local", keys)
        chk("不可达节点沉到最后",
            keys.index("closed") < keys.index("dead"), keys)
        labels = {d[0]: d[1] for d in decorated}
        result["info"]["labels"] = labels
        chk("★「自动」带上当前最快的节点与延迟",
            "最快" in labels["auto"] and "本地测试节点" in labels["auto"]
            and "26ms" in labels["auto"], labels["auto"])
        chk("可用节点标签带延迟", labels["local"] == "本地测试节点（26ms）",
            labels["local"])
        chk("端口不通如实写出来", "端口不通" in labels["closed"], labels["closed"])
        chk("超时节点不带假延迟", labels["dead"] == "保留地址（超时）",
            labels["dead"])

        no_ok = {"dead": fake["dead"]}
        d2 = np.decorate_choices(choices, no_ok, missing_text=None)
        l2 = {d[0]: d[1] for d in d2}
        chk("一个都不通时「自动」退回原说明",
            l2["auto"] == "自动（尝试全部节点）", l2["auto"])
        chk("missing_text=None 时未测项保持裸标签（首屏不刷「待测」）",
            l2["local"] == "本地测试节点", l2["local"])

        fresh = {"local": dict(fake["local"])}
        stale = {"local": dict(fake["local"], at=now - 1000)}
        d3 = np.decorate_choices(choices, stale, now=now, ttl=90)
        l3 = {d[0]: d[1] for d in d3}
        chk("★ 过期数据不参与排序、也不显示数字",
            l3["local"] == "本地测试节点（待测）", l3["local"])
        chk("新鲜数据仍然照常显示",
            {d[0]: d[1] for d in np.decorate_choices(choices, fresh, now=now,
                                                     ttl=90)}["local"]
            == "本地测试节点（26ms）")

        # ------------------------------------------------ 文案：别套双层括号
        # 用户报的 bug：节点位置名显示不全。除了控件宽度（界面侧另测），
        # 文案本身也在往长的方向走——「自动（最快：国内中继（IP 直连） 36ms）」
        # 这种双层括号既是多余长度，读起来也要数括号配对。
        chk("★「自动」里的节点名去掉自带括号（不做双层括号）",
            np.short_name("国内中继（IP 直连）") == "国内中继",
            np.short_name("国内中继（IP 直连）"))
        chk("没括号的名字原样保留",
            np.short_name("海波中国大陆") == "海波中国大陆")
        chk("英文括号同样处理",
            np.short_name("Node (HK)") == "Node")
        paren_choices = tuple(list(choices) + [
            ("paren", "国内中继（IP 直连）", "tcp://127.0.0.1:1")])
        paren_fake = {"paren": {"key": "paren", "label": "国内中继（IP 直连）",
                                "ms": None, "ok": False, "via": "",
                                "error": "超时", "at": now}}
        lp = {d[0]: d[1] for d in np.decorate_choices(paren_choices, paren_fake)}
        chk("★ 名字自带括号时状态用间隔号，不再叠一层括号",
            lp["paren"] == "国内中继（IP 直连） · 超时", lp["paren"])

        chk("format_latency 数字带单位", np.format_latency(208) == "208ms")
        chk("format_latency 测不到说「超时」", np.format_latency(None) == "超时")
        chk("age_text 说人话",
            np.age_text(None) == "尚未测速" and np.age_text(5) == "5 秒前"
            and np.age_text(120) == "2 分钟前", np.age_text(120))
        summary = np.summary_text(fake)
        chk("summary_text 一行可读",
            "本地测试节点 26ms" in summary and "保留地址 超时" in summary,
            summary)

        # ------------------------------------------------------ G 与真实配置对齐
        try:
            import etier
            keys = [k for k, _l, _a in etier.NODE_CHOICES]
            chk("每个可选中继节点的地址都能解析",
                all(np.split_address(a) is not None
                    for k, _l, a in etier.NODE_CHOICES if a),
                [a for k, l, a in etier.NODE_CHOICES if a])
            chk("首个选项是 auto 且没有地址（测速要跳过它）",
                keys[0] == "auto" and not etier.NODE_CHOICES[0][2], keys[0])
            chk("NODE_CHOICES 没有重复 key",
                len(keys) == len(set(keys)), keys)
        except Exception as exc:
            chk("真实节点表可用", False, repr(exc))
    finally:
        try:
            srv.close()
        except OSError:
            pass

    result["info"]["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return _finish(out_file, result)
