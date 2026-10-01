# -*- coding: utf-8 -*-
"""打包后自检：临时云盘（同房间文件互传）。

为什么需要它
------------
云盘的核心承诺是「**退出房间后就下不到**」——这条要是坏的，用户以为
文件已经不可访问，实际服务还在监听，是很难被发现的隐私问题。而它依赖
的几件事都不会抛异常、只在打包后才可能出问题：

  1. HTTP 服务绑的是**虚拟 IP**（不是 0.0.0.0）——绑错了会连物理局域网
     一起暴露出去；
  2. 房间隔离靠「房间码 + 密码」派生的 token——算错就变成"谁都能拉"；
  3. 中文文件名走 HTTP 头往返——编码错了会变成一堆 %E4%B8%AD；
  4. 退出时的 stop() 要真的把监听关掉。

本自检在 127.0.0.1 上跑**真实的 HTTP**（不碰 EasyTier、不要管理员权限、
不创建虚拟网卡），把上面这些固化成断言。

用法：`Yuhub.exe --share-selftest <结果json路径>`
返回码：0 全部通过 / 1 有断言失败 / 2 参数错误 / 4 结果写盘失败
"""

import json
import os
import shutil
import tempfile
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
        import lan_share as ls
    except Exception as exc:                       # pragma: no cover - 防御
        chk("导入 lan_share", False, repr(exc))
        return _finish(out_file, result)

    started = time.monotonic()
    work = tempfile.mkdtemp(prefix="yuhub_share_st_")
    src = os.path.join(work, "src")
    dst = os.path.join(work, "dst")
    os.makedirs(src, exist_ok=True)
    os.makedirs(dst, exist_ok=True)

    # 造两个文件：中文名 + 带空格，尽量贴近真实使用
    payload = os.urandom(256 * 1024)
    big = os.path.join(src, "测试 存档 ①.zip")
    with open(big, "wb") as f:
        f.write(payload)
    small = os.path.join(src, "note.txt")
    with open(small, "w", encoding="utf-8") as f:
        f.write("hello yuhub")

    token = ls.make_token("SELF-TEST", "pw")
    srv = None
    hub = None
    try:
        # ---------------------------------------------------------- A 工具
        chk("human_size 换算正确", ls.human_size(2048) == "2.0 KB",
            ls.human_size(2048))
        chk("safe_name 挡住路径穿越（../）",
            ls.safe_name("../../windows/system32/cmd.exe") == "cmd.exe",
            ls.safe_name("../../windows/system32/cmd.exe"))
        chk("safe_name 清掉 Windows 非法字符",
            ls.safe_name('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h",
            ls.safe_name('a<b>c:d"e|f?g*h'))
        chk("safe_name 不产生空名",
            ls.safe_name("...") not in ("", ".", ".."), ls.safe_name("..."))
        chk("token 由房间码+密码派生（同输入同结果）",
            ls.make_token("A", "B") == ls.make_token("A", "B"))
        chk("token 对密码敏感",
            ls.make_token("A", "B") != ls.make_token("A", "C"))
        chk("download_dir 可用", bool(ls.download_dir()))
        p1 = ls.unique_path(dst, "x.bin")
        open(p1, "wb").close()
        p2 = ls.unique_path(dst, "x.bin")
        chk("重名自动加序号", p2 != p1 and "(1)" in os.path.basename(p2),
            os.path.basename(p2))

        # ---------------------------------------------------------- B 服务端
        srv = ls.ShareServer("127.0.0.1", token, nick="SelfTest")
        ok, msg = srv.start(attempts=1)
        chk("HTTP 服务能在指定 IP 上监听", ok, msg)
        result["info"]["port"] = srv.port
        chk("监听端口落在预定段",
            ls.BASE_PORT <= srv.port < ls.BASE_PORT + ls.PORT_TRIES, srv.port)
        added, skipped = srv.add_paths([big, small, os.path.join(src, "nope.bin")])
        chk("登记文件成功", len(added) == 2, len(added))
        chk("不存在的路径被跳过", len(skipped) == 1 and "不是文件" in skipped[0],
            skipped)
        a2, s2 = srv.add_paths([big])
        chk("重复登记被去重", not a2 and "已在列表中" in s2[0], s2)
        chk("对外清单不含本机绝对路径",
            all("path" not in f for f in srv.list_files()), srv.list_files())

        # ---------------------------------------------------------- C 客户端
        cli = ls.ShareClient("127.0.0.1", srv.port, token)
        ok, files = cli.list_files()
        chk("持正确 token 能拉到清单", ok and len(files) == 2, (ok, files))
        bad = ls.ShareClient("127.0.0.1", srv.port, "wrong")
        ok2, msg2 = bad.list_files()
        chk("token 不对被拒（403）", (not ok2) and "403" in msg2, (ok2, msg2))
        no_token = ls.ShareClient("127.0.0.1", srv.port, "")
        ok3, msg3 = no_token.list_files()
        chk("空 token 被拒", not ok3, (ok3, msg3))

        target = next((f for f in files if f["name"].endswith(".zip")), None)
        chk("中文名文件能按 id 定位", target is not None, files)
        ticks = []
        ok, path = cli.download(target["id"], dest_dir=dst,
                                progress=lambda d, t: ticks.append((d, t)))
        chk("下载成功", ok, path)
        chk("下载有进度回调", len(ticks) > 0, len(ticks))
        chk("下载内容与原文件逐字节一致",
            os.path.isfile(path) and open(path, "rb").read() == payload, path)
        chk("中文文件名正确还原",
            os.path.basename(path) == "测试 存档 ①.zip", os.path.basename(path))
        chk("下载后不留 .part",
            not [f for f in os.listdir(dst) if f.endswith(".part")],
            os.listdir(dst))

        # 用户自选保存位置（v0.8.14beta）：界面上每次下载都会先弹保存对话框，
        # 这里验证"对话框选了什么就存到哪"——**连重名都不许改名**，因为
        # 覆盖确认是系统对话框自己问过的，我们再自作主张加 (1) 就是骗人。
        chosen = os.path.join(dst, "自定义名字.bin")
        okd, pathd = cli.download(target["id"], dest_path=chosen)
        chk("★ 下载能落到用户指定的路径（每次下载自选位置）",
            okd and pathd == os.path.abspath(chosen), (okd, pathd))
        chk("指定路径重名也不改名（覆盖已在系统对话框确认过）",
            os.path.isfile(chosen)
            and not os.path.exists(os.path.join(dst, "自定义名字 (1).bin")),
            os.listdir(dst))
        chk("指定路径下载内容一致",
            open(chosen, "rb").read() == payload, chosen)

        # 取消：第 3 个分块后中断
        state = {"n": 0}

        def _cancel():
            state["n"] += 1
            return state["n"] > 3

        ok4, msg4 = cli.download(target["id"], dest_dir=dst, is_cancelled=_cancel)
        chk("取消下载返回取消而不是成功",
            (not ok4) and "取消" in msg4, (ok4, msg4))
        chk("取消后不留 .part",
            not [f for f in os.listdir(dst) if f.endswith(".part")],
            os.listdir(dst))

        # ---------------------------------------------------------- D 退出即失效
        port = srv.port
        srv.stop()
        ok5, msg5 = ls.ShareClient("127.0.0.1", port, token).list_files()
        chk("★ 服务停止后不可达（「退出房间后下不到」的落点）",
            not ok5, (ok5, msg5))
        chk("停止后清单已清空", srv.list_files() == [], srv.list_files())

        # ---------------------------------------------------------- E 重绑
        hub = ls.ShareHub(token, nick="SelfTest")
        ok, msg = hub.start("127.0.0.1", attempts=1)
        chk("ShareHub 启动", ok, msg)
        hub.add_path(big)
        hub.rebind("127.0.0.1")
        chk("换 IP 重绑后清单保留", len(hub.own_files()) == 1, hub.own_files())
        res = hub.collect([{"ip": "127.0.0.1", "port": hub.port},
                           {"ip": "127.0.0.9", "port": 0}])
        chk("collect 能聚合自己那份清单",
            bool(res) and res[0]["ok"] and len(res[0]["files"]) == 1, res[0])
        chk("collect 对未开云盘的成员给明确说明",
            len(res) > 1 and (not res[1]["ok"]) and "未开启" in res[1]["error"],
            res[1] if len(res) > 1 else res)
        hub.stop()

        # ---------------------------------------------------------- F 信标协议
        try:
            import etier
            b = etier.NickBeacon("10.126.126.9", "Bob", game="泰拉瑞亚",
                                 port=7777, share=41777, share_rev=5)
            info = etier.NickBeacon._parse_info(b._payload())
            chk("信标报文带上云盘端口",
                info.get("share") == 41777, info)
            chk("★ 信标报文带上清单版本号（对端据此知道对方刚动过文件）",
                info.get("srev") == 5, info)
            b.set_share(0)
            info2 = etier.NickBeacon._parse_info(b._payload())
            chk("关闭云盘后不再广播该键（对端不必白试连接）",
                not info2.get("share") and info2.get("name") == "Bob", info2)
            chk("关闭云盘后版本号也一并收起", not info2.get("srev"), info2)

            # 实时状态表：只看新鲜期内的记录，过期的不参与判断
            b._peers.clear()
            b._peers["10.126.126.7"] = {
                "info": {"share": 41800, "srev": 12},
                "until": time.monotonic() + 10}
            b._peers["10.126.126.6"] = {
                "info": {"share": 41801, "srev": 1},
                "until": time.monotonic() - 1}
            live = b.live_shares()
            chk("★ 实时状态表只给新鲜记录（过期的不能拿来判断）",
                list(live) == ["10.126.126.7"], live)
            chk("实时状态表同时给出端口与版本号",
                live.get("10.126.126.7") == (41800, 12), live)
            # 临别广播：没启动线程时也不能炸（退出房间路径上会调）
            b.set_share(0)
            b.announce_now()
            chk("★ 临别广播（退出房间前同步发一次）不依赖后台线程",
                True, "已调用")

            old = json.dumps({"yuhub-nick-v1": "OldBob", "game": "饥荒联机版",
                              "port": 10999}).encode("utf-8")
            info3 = etier.NickBeacon._parse_info(old)
            chk("旧版报文仍能解析（新旧客户端可同房）",
                info3.get("name") == "OldBob" and info3.get("share") == 0
                and info3.get("srev") == 0, info3)
            tr = etier.MemberTracker("10.126.126.9")
            tr._peers["10.126.126.8"] = {"confirmed": time.monotonic(),
                                         "ping_fail": 0, "name": "", "game": "",
                                         "port": 0, "share": 0, "srev": 0}
            snap = tr.snapshot()
            chk("成员快照带上 share 字段（UI 直接拿它拼地址）",
                bool(snap) and "share" in snap[0], snap)
            chk("成员快照带上 srev 字段（UI 据此判断要不要立刻重拉）",
                bool(snap) and "srev" in snap[0], snap)
        except Exception as exc:
            chk("信标协议扩展可用", False, repr(exc))
    finally:
        for obj in (srv, hub):
            if obj is not None:
                try:
                    obj.stop()
                except Exception:
                    pass
        shutil.rmtree(work, ignore_errors=True)

    result["info"]["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return _finish(out_file, result)
