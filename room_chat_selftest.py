# -*- coding: utf-8 -*-
"""打包后自检：房间聊天（文本消息 + 云盘文件消息 + 撤回）。

为什么需要它
------------
聊天的三条承诺都不会抛异常，坏掉时界面看着完全正常——只能靠断言守住：

  1. **昵称随报文走**。进「异地联机」时昵称必填，发送端把昵称写进每条
     报文；接收端直接显示。坏了的表现是"聊天室里全是裸 IP"。
  2. **文件消息与云盘联动**。分享文件 → 聊天室出现带下载图标的消息；
     取消分享 → 同一条变「已撤回」、下载按钮失效。坏了的表现是"文件传了
     但聊天室里没动静"或"取消了分享，聊天室里还能点下载"。
  3. **3 连发要去重、撤回报文不能被去重**。撤回报文复用原消息 id，如果
     也去过重表，会被当成"重复投递"直接丢掉——表现为"我取消了分享，
     队友聊天室里那条还在"（这个 bug 在开发中真的出现过，见 _on_datagram）。

自检分三段，都不需要虚拟网卡、不要管理员权限：
  A 引擎：两个 RoomChat 实例绑 127.0.0.1/127.0.0.2，bcast 互指，
     真收发 UDP（构造参数的 bcast 覆盖口就是为这个准备的）。
  B 界面：offscreen Qt 构造「联机页 + 屏幕共享页」这一对，接信号、
     渲染文本/文件消息、撤回改写、退房清空、守门路径。
  C 端到端：真云盘 HTTP + 真聊天广播，从聊天室点下载下一个真文件，
     逐字节比对。

⚠️ 注意：B/C 段会构造 LanPage，其启动清理线程会顺手清掉**正在运行的**
跨网房间（`_cleanup_orphans_on_launch`）——别在房间运行中跑本自检。

用法：`Yuhub.exe --chat-selftest <结果json路径>`
返回码：0 全部通过 / 1 有断言失败 / 2 参数错误 / 4 结果写盘失败
"""

import json
import os
import shutil
import sys
import tempfile
import time


def _finish(out_file, result):
    checks = result["checks"]
    result["ok"] = bool(checks) and all(c["pass"] for c in checks)
    result["info"]["passed"] = sum(1 for c in checks if c["pass"])
    result["info"]["total"] = len(checks)
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
    except OSError:
        return 4
    return 0 if result["ok"] else 1


def _pump(app, seconds=1.2):
    """转 Qt 事件循环 + 睡一会：让后台线程的消息/下载回调跑完。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def run(out_file, timeout_sec=90):
    checks = []
    result = {"ok": False, "checks": checks, "info": {}}

    def chk(name, cond, detail=""):
        checks.append({"name": name, "pass": bool(cond),
                       "detail": str(detail)[:400]})

    started = time.monotonic()
    work = tempfile.mkdtemp(prefix="yuhub_chat_st_")
    chats, hubs, pages = [], [], []
    try:
        import room_chat
        import lan_share
        chk("导入 room_chat / lan_share", True)

        # ================================================================
        # A 引擎：回环互发
        # ================================================================
        got_a, got_b = [], []
        chat_a = room_chat.RoomChat("127.0.0.1", "甲", bcast="127.0.0.2",
                                    on_message=got_a.append)
        chat_b = room_chat.RoomChat("127.0.0.2", "乙", bcast="127.0.0.1",
                                    on_message=got_b.append)
        chats.extend((chat_a, chat_b))
        chk("两个实例都能监听", chat_a.start() and chat_b.start())

        rec = chat_a.send_text("开黑了")
        time.sleep(0.6)
        chk("本机发的消息立刻上屏（不等广播回声）",
            len(got_a) == 1 and got_a[0].get("text") == "开黑了"
            and got_a[0].get("me") is True, got_a)
        chk("对端收到文本且昵称正确",
            len(got_b) == 1 and got_b[0].get("text") == "开黑了"
            and got_b[0].get("nick") == "甲" and got_b[0].get("me") is False,
            got_b)

        frec = chat_b.send_file("整合包.zip", 12345678, "fid-001", 41777)
        time.sleep(0.6)
        files = [r for r in got_a if r.get("kind") == "file"]
        chk("文件消息字段齐全（fid/大小/端口/文件名）",
            len(files) == 1 and files[0].get("fid") == "fid-001"
            and files[0].get("size") == 12345678
            and files[0].get("port") == 41777
            and files[0].get("name") == "整合包.zip", files)

        chat_b.withdraw(frec["id"])
        time.sleep(0.6)
        ups = [r for r in got_a if r.get("event") == "update"]
        chk("★ 撤回报文不被去重（复用原 id 也要送达）",
            len(ups) == 1 and ups[0].get("id") == frec["id"]
            and ups[0].get("withdrawn") is True, ups)
        hist = chat_a.history()
        chk("撤回是改写原消息（历史里同一条变 withdrawn，不新增）",
            len(hist) == 2 and any(h.get("kind") == "file"
                                   and h.get("withdrawn") for h in hist),
            [(h.get("kind"), h.get("withdrawn")) for h in hist])

        texts = [r for r in got_b if r.get("kind") == "text"]
        chk("3 连发被去重（对端只上屏一次）", len(texts) == 1, len(texts))

        frec2 = chat_b.send_file("另一个包.zip", 999, "fid-002", 41777)
        time.sleep(0.6)
        n_before = len(got_a)
        refused = chat_a.withdraw(frec2["id"])
        time.sleep(0.4)
        b_copy = [h for h in chat_a.history() if h.get("id") == frec2["id"]]
        chk("不能撤别人的消息（拒绝 + 本地不变 + 不广播）",
            refused is False and len(got_a) == n_before
            and bool(b_copy) and b_copy[0].get("withdrawn") is False,
            (refused, b_copy))

        chat_a.set_nick("甲改名")
        chat_a.send_text("我改名了")
        time.sleep(0.6)
        texts2 = [r for r in got_b if r.get("kind") == "text"]
        chk("运行中改昵称，下一条消息立刻带新昵称",
            bool(texts2) and texts2[-1].get("nick") == "甲改名", texts2[-1:])

        chk("空文本被拒（不发空消息）", chat_a.send_text("   ") is None)
        long_rec = chat_a.send_text("长" * 500)
        chk("超长文本截到 %d" % room_chat.TEXT_MAX,
            long_rec is not None
            and len(long_rec.get("text") or "") == room_chat.TEXT_MAX)
        chk("坏报文被忽略（魔数不对直接丢）",
            chat_a._on_datagram(b'{"m":"other","t":"text"}', "10.0.0.9")
            is None)

        n_b = len(got_b)
        chat_b.stop()
        time.sleep(0.2)
        chat_a.send_text("乙已经停了")
        time.sleep(0.6)
        chk("stop 之后不再收新消息", len(got_b) == n_b,
            (n_b, len(got_b)))

        # ================================================================
        # B 界面：两页 + 信号 + 渲染 + 撤回 + 守门
        # ================================================================
        try:
            from PySide6.QtWidgets import QApplication, QWidget
            app = QApplication.instance() or QApplication([])

            def make_member(ip, peer_ip, nick):
                win = QWidget()
                lan_p = _new_lan_page(win)
                share_p = _new_share_page(win)
                win._pages = {"lan": lan_p, "share": share_p}
                win.resize(1280, 800)
                win.show()
                chat = room_chat.RoomChat(
                    ip, nick, bcast=peer_ip,
                    on_message=lambda r: lan_p.chat_message.emit(r))
                chat.start()
                lan_p._chat = chat
                pages.append((win, lan_p, share_p))
                chats.append(chat)
                return lan_p, share_p

            import screenshare
            screenshare.set_active_room("SELF-TEST", "pw1234", "10.126.126.9")
            lan_a, share_a = make_member("127.0.0.1", "127.0.0.2", "甲")
            lan_b, share_b = make_member("127.0.0.2", "127.0.0.1", "乙")
            share_a.on_shown()
            share_b.on_shown()
            _pump(app, 0.3)
            chk("进房后徽标变「房间聊天中」且输入可用",
                share_b._chat_badge.text() == "房间聊天中"
                and share_b._chat_input.isEnabled(),
                share_b._chat_badge.text())
            chk("聊天信号接到联机页", share_b._chat_connected)

            chat_a.send_text("开黑了，速来")
            _pump(app)
            chk("队友文本渲染成一行",
                len(share_b._chat_rows) == 1, list(share_b._chat_rows))

            hub_a = lan_share.ShareHub(
                lan_share.make_token("SELF-TEST", "pw1234"), nick="甲")
            ok, msg = hub_a.start("127.0.0.1")
            hubs.append(hub_a)
            chk("甲的云盘起来（供 C 段下载）", ok, msg)
            lan_a._hub = hub_a
            lan_a._running = True
            hub_b = lan_share.ShareHub(
                lan_share.make_token("SELF-TEST", "pw1234"), nick="乙")
            hub_b.start("127.0.0.2")
            hubs.append(hub_b)
            lan_b._hub = hub_b
            lan_b._running = True

            src = os.path.join(work, "整合包.zip")
            with open(src, "wb") as f:
                f.write(os.urandom(256 * 1024))
            added, skipped = hub_a.add_paths([src])
            chk("文件进云盘清单", bool(added) and not skipped,
                (added, skipped))
            lan_a._announce_share_files(added)
            _pump(app)
            file_keys = [k for k, r in share_b._chat_rows.items()
                         if r.get("btn") is not None]
            chk("★ 分享文件 → 聊天室出现带下载按钮的消息",
                len(file_keys) == 1, list(share_b._chat_rows))
            refs = share_b._chat_rows.get(file_keys[0]) if file_keys else {}
            # 新版文件卡把"文件名"和"体积"拆成两个标签（文件名加粗、体积淡色），
            # 断言跟着拆开：名字在 name_lbl、体积在 size_lbl。
            chk("文件名与大小都显示",
                refs.get("name_lbl") is not None
                and "整合包.zip" in refs["name_lbl"].text()
                and refs.get("size_lbl") is not None
                and "256.0 KB" in refs["size_lbl"].text(),
                (refs.get("name_lbl").text() if refs.get("name_lbl") else "",
                 refs.get("size_lbl").text() if refs.get("size_lbl") else ""))

            # 守门：引擎没起时发送被拦（这里引擎起着，改测空文案）
            ok_send, err = lan_b.send_chat_text("   ")
            chk("空文本发送被守门拦住", ok_send is False and "空" in err, err)

            # 退房 → 行清空 + 输入禁用
            screenshare.set_active_room()
            share_b._tick()
            _pump(app, 0.2)
            chk("退房后徽标/输入/消息行归位",
                share_b._chat_badge.text() == "未进入房间"
                and not share_b._chat_input.isEnabled()
                and share_b._chat_rows == {},
                (share_b._chat_badge.text(), list(share_b._chat_rows)))

            # ============================================================
            # C 端到端：重新进房，从聊天室真下载一个文件
            # ============================================================
            screenshare.set_active_room("SELF-TEST", "pw1234", "10.126.126.9")
            share_a.on_shown()
            share_b.on_shown()
            _pump(app, 0.3)
            lan_a._announce_share_files(added)
            _pump(app)
            fk = [k for k, r in share_b._chat_rows.items()
                  if r.get("btn") is not None]
            frec_ui = next((h for h in lan_b.chat_history()
                            if h.get("id") == fk[0]), None) if fk else None
            chk("再次进房后文件消息补画出来", frec_ui is not None, frec_ui)
            if frec_ui is not None:
                dest = os.path.join(work, "down", "整合包.zip")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                lan_b._ask_save_path = lambda name: dest   # 免掉模态框
                lan_b.download_chat_file(dict(frec_ui))
                _pump(app, 3.0)
                same = (os.path.exists(dest)
                        and os.path.getsize(dest) == os.path.getsize(src)
                        and open(dest, "rb").read() == open(src, "rb").read())
                chk("★ 从聊天室下载的文件与源逐字节一致", same,
                    (os.path.exists(dest),
                     os.path.getsize(dest) if os.path.exists(dest) else -1))

            # 撤回的端到端表现
            if added:
                lan_a._withdraw_chat_file(added[0]["id"])
                _pump(app)
                rk = [k for k, r in share_b._chat_rows.items()
                      if r.get("name_lbl") is not None]
                r2 = share_b._chat_rows.get(rk[0]) if rk else {}
                chk("★ 取消分享 → 队友消息变「已撤回」且按钮失效",
                    r2.get("name_lbl") is not None
                    and r2["name_lbl"].text() == "已撤回"
                    and not r2["btn"].isEnabled(),
                    r2.get("name_lbl").text() if r2.get("name_lbl") else "")

            for _win, lan_p, share_p in pages:
                try:
                    share_p.shutdown()
                except Exception:
                    pass
            pages = []
        except Exception as exc:
            import traceback
            chk("界面段可运行", False, traceback.format_exc())

    except Exception as exc:
        import traceback
        chk("引擎段可运行", False, traceback.format_exc())
    finally:
        for c in chats:
            try:
                c.stop()
            except Exception:
                pass
        for h in hubs:
            try:
                h.stop()
            except Exception:
                pass
        for _win, lan_p, share_p in pages:
            try:
                share_p.shutdown()
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)

    result["info"]["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return _finish(out_file, result)


def _new_lan_page(win):
    from ui.pages.lan_page import LanPage
    return LanPage(notify=lambda *a, **k: None, parent=win)


def _new_share_page(win):
    from ui.pages.share_page import SharePage
    return SharePage(notify=lambda *a, **k: None, parent=win)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(2)
    sys.exit(run(sys.argv[1]))
