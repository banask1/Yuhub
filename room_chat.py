# -*- coding: utf-8 -*-
"""房间聊天：虚拟局域网里的一句话广播（临时、无服务器、无历史持久化）。

它在 Yuhub 里的位置
------------------
「异地联机」的房间里，成员之间除了游戏/云盘/分屏，还需要能说几句话。
这条链路不缺——EasyTier 已经给了双方同一块虚拟网卡，而且启动参数里开了
`--enable-udp-broadcast-relay`，广播能跨到所有节点（昵称信标一直这么干，
见 etier.NickBeacon）。所以聊天复用同一机制：

    发送端 拼一条 JSON → UDP 广播到 10.126.126.255:CHAT_PORT
    接收端 收包 → 解析 → 上屏（去重、按 id 撤回）

三条设计红线
------------
1. **昵称随报文走，不搞 IP→昵称解析**。进「异地联机」时昵称是必填的
   （见 lan_page._refresh_start_gate），发送端把自己的昵称写进每条报文，
   接收端直接显示。好处：切换昵称立刻全房间生效，也不需要等信标/反查。
2. **文件消息与「临时云盘」同生共死**。分享文件时广播一条 file 消息
   （带 fid + 云盘端口），取消分享时广播 withdraw——同一条消息 id。
   接收端点消息上的下载图标，走的就是云盘那套 HTTP 下载（lan_share）。
3. **丢包靠重发兜，不搞 ACK**。每条消息连发 3 遍（间隔 80ms）。UDP 广播
   在虚拟网里本来就很稳（信标 3 秒一条一直没出过问题），重发只是把
   "偶尔"变成"几乎不会"；为了"一条都不能少"引入 ACK/序号窗口，复杂度
   与这个功能的体量不匹配。

线程模型
--------
一个接收线程（daemon）绑在虚拟 IP 上收包；发送是即时的（调用方线程里
连发几遍）。`on_message` 回调**可能来自接收线程**，Qt 界面必须自己转
信号（lan_page 用 Signal.emit 搬回主线程），本模块不碰任何 GUI。
"""

import json
import secrets
import socket
import threading
import time

import etier

#: 广播端口。定义在 etier.py（与 NICK_PORT 做邻居）：看门狗的防火墙放行
#: 在那里一起加，聊天端口自己不该知道 netsh 的存在。
CHAT_PORT = etier.CHAT_PORT

#: 协议魔数。与昵称信标（yuhub-nick-v1）分开：两个口、两套报文，
#: 互不打扰；旧版 Yuhub 没有这个模块，收到不认识的魔数直接忽略。
CHAT_MAGIC = "yuhub-chat-v1"

#: 每条消息连发几遍、间隔多久（见模块docstring第 3 条）。
SEND_REPEAT = 3
SEND_GAP = 0.08

#: 单条文本上限。再长就该发文件——云盘就是干这个的。
TEXT_MAX = 300

#: 文件名上限（与 lan_share.safe_name 的截断口径一致）。
NAME_MAX = 180

#: 历史上限。聊天是会话性的：翻不到开头不要紧，内存和渲染列表不能爆。
HISTORY_MAX = 200

#: 去重表上限（按消息 id）。只用于丢"自己广播的回声"和重复投递。
SEEN_MAX = 512

#: 昵称上限（与 etier.NickBeacon 一致）。
NICK_MAX = 32

#: 接收缓冲区。一条文本消息 UTF-8 最多约 1KB，2KB 足够；更大的直接
#: 解析失败忽略（也顺带挡了往这个口灌垃圾的行径）。
RECV_BUF = 2048


def norm_nick(nick):
    """昵称归一化：去首尾空白 + 截断。空就是空（界面层负责兜底显示）。"""
    return (nick or "").strip()[:NICK_MAX]


class RoomChat(object):
    """一个跨网房间对应一个实例。线程安全（内部有锁）。

    用法：
        chat = RoomChat(my_vip, nick, on_message=lambda rec: signal.emit(rec))
        chat.start()
        chat.send_text("开黑了")
        rec = chat.send_file("整合包.zip", size, fid, cloud_port)
        chat.withdraw(rec["id"])          # 取消分享 → 消息变「已撤回」
        chat.stop()

    on_message 收到的记录（dict）：
        {"id", "kind": "text"/"file", "from_ip", "nick", "me", "ts",
         "withdrawn", "event": "new"/"update",
         "text"                    # kind == "text"
         "name", "size", "fid", "port"}   # kind == "file"
    """

    def __init__(self, my_ip, nick, on_message=None, on_log=None, bcast=""):
        self._my_ip = (my_ip or "").strip()
        self._nick = norm_nick(nick)
        self._on_message = on_message or (lambda rec: None)
        self._on_log = on_log or (lambda msg: None)
        #: 广播目标。默认是虚拟网广播地址；留一个覆盖口是为了自检
        #: （没有虚拟网卡的机器上指到另一个回环地址就能练收发）。
        self._bcast = (bcast or "").strip()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._recv = None            # 接收 socket（绑虚拟 IP）
        self._sock = None            # 发送 socket（懒建）
        self._msgs = []              # [record]，新年在后
        self._by_id = {}             # id -> record（撤回要靠它找回消息）
        self._seen = []              # 最近处理过的 id（丢回声/重复）

    # ---------------------------------------------------------------- 日志
    def _log(self, msg):
        try:
            self._on_log(msg)
        except Exception:
            pass

    # ------------------------------------------------------------ 生命周期
    @property
    def running(self):
        t = self._thread
        return t is not None and t.is_alive()

    def start(self):
        """起接收线程。重复调用无害；监听失败返回 False（聊天不可用，
        但不该影响联机本身——调用方照常弹一句提示就行）。"""
        if self.running:
            return True
        if not self._my_ip:
            self._log("聊天未启动：还没有虚拟 IP")
            return False
        self._stop.clear()
        try:
            self._recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._recv.bind((self._my_ip, CHAT_PORT))
            self._recv.settimeout(0.5)
        except OSError as exc:
            # 端口被占 / 地址还没就绪（DAD）。聊天是锦上添花，不拦着联机。
            self._log("聊天监听失败（不影响联机）：%s" % exc)
            self._recv = None
            return False
        self._thread = threading.Thread(target=self._loop,
                                        name="RoomChatRx", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """停接收线程、关 socket。瞬时返回，不 join 死等。"""
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(1.0)
        for attr in ("_recv", "_sock"):
            s = getattr(self, attr, None)
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
                setattr(self, attr, None)

    # ---------------------------------------------------------------- 昵称
    def set_nick(self, nick):
        """运行中改昵称（联机页的昵称框变化时调用）。"""
        with self._lock:
            self._nick = norm_nick(nick)

    def nick(self):
        with self._lock:
            return self._nick

    # ---------------------------------------------------------------- 历史
    def history(self):
        """当前消息列表的副本（切到聊天页时补画用）。"""
        with self._lock:
            return [dict(m) for m in self._msgs]

    # ---------------------------------------------------------------- 发送
    def send_text(self, text):
        """发一条文本消息。返回本条记录（发不出去也上屏，附错误说明）。"""
        text = (text or "").strip()
        if not text:
            return None
        if len(text) > TEXT_MAX:
            text = text[:TEXT_MAX]
        rec = self._record("text", text=text)
        ok, err = self._deliver(rec)
        if not ok:
            rec["error"] = err
        self._commit(rec)
        return rec

    def send_file(self, name, size, fid, port):
        """发一条文件消息（临时云盘分享的文件）。

        fid/port 是对方下载时要用的：云盘文件 id + 分享者云盘端口。
        """
        rec = self._record("file",
                           name=str(name or "")[:NAME_MAX],
                           size=int(size or 0),
                           fid=str(fid or ""),
                           port=int(port or 0))
        ok, err = self._deliver(rec)
        if not ok:
            rec["error"] = err
        self._commit(rec)
        return rec

    def withdraw(self, msg_id):
        """撤回一条消息。只有**本机发出去的**才允许撤——那是别人分享的
        文件，不能替别人撤（本地那份也保持原样，照常能下载）。"""
        mid = str(msg_id or "")
        if not mid:
            return False
        with self._lock:
            rec = self._by_id.get(mid)
            if rec is None or rec.get("withdrawn") or not rec.get("me"):
                return False
            rec["withdrawn"] = True
            out = dict(rec)
        self._send({"m": CHAT_MAGIC, "t": "withdraw", "id": mid,
                    "n": self.nick(), "ts": time.time()})
        out["event"] = "update"
        self._emit(out)
        return True

    # ------------------------------------------------------------ 发送内部
    def _record(self, kind, **kw):
        rec = {"id": secrets.token_hex(6), "kind": kind,
               "from_ip": self._my_ip, "nick": self.nick(), "me": True,
               "ts": time.time(), "withdrawn": False}
        rec.update(kw)
        return rec

    def _deliver(self, rec):
        """发一条本机消息：先登记 id，再连发。

        登记必须**早于**发送：广播回声（自己发的包被中继绕回来）会在
        ~160ms 的连发窗口里到达，如果那时 id 还没进表，接收线程会把它
        当成别人的消息上屏，紧接着 `_commit` 又上一次——同一条出现两遍。
        """
        with self._lock:
            self._remember_id(rec["id"])
        return self._send(self._wire(rec))

    @staticmethod
    def _wire(rec):
        """记录 → 广播报文（字段名用短的，UDP 包能小一点是一点）。"""
        obj = {"m": CHAT_MAGIC, "t": rec["kind"], "id": rec["id"],
               "n": rec.get("nick") or "", "ts": rec.get("ts") or 0}
        if rec["kind"] == "text":
            obj["x"] = rec.get("text") or ""
        else:
            obj["x"] = rec.get("name") or ""
            obj["sz"] = int(rec.get("size") or 0)
            obj["fid"] = rec.get("fid") or ""
            obj["p"] = int(rec.get("port") or 0)
        return obj

    def _send(self, obj, repeat=SEND_REPEAT):
        """连发几遍。返回 (成功, 错误说明)。"""
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            return False, "消息序列化失败"
        if len(data) > RECV_BUF:
            return False, "消息太长"
        if not self._my_ip:
            return False, "还没有虚拟 IP（没在房间里）"
        target = self._bcast or (etier.VIRTUAL_NET_PREFIX + "255")
        try:
            if self._sock is None:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                try:
                    # 绑到虚拟 IP：这样源地址确定就是"我"。对端靠
                    # `from_ip == my_ip` 识别自己广播的回声，源地址不确定
                    # 时这条判据会失效（回环/多网卡环境下系统选的源地址
                    # 未必是期望的那个）。绑不上也不拦着发——系统会选一个。
                    s.bind((self._my_ip, 0))
                except OSError:
                    pass
                self._sock = s
            for i in range(max(1, int(repeat))):
                try:
                    self._sock.sendto(data, (target, CHAT_PORT))
                except OSError as exc:
                    return False, "发送失败：%s" % exc
                if i + 1 < repeat:
                    time.sleep(SEND_GAP)
            return True, ""
        except OSError as exc:
            return False, "发送失败：%s" % exc

    def _commit(self, rec):
        """本机发出的消息：进历史 + 通知界面（id 在 _deliver 里已登记）。"""
        with self._lock:
            self._remember_id(rec["id"])
            self._by_id[rec["id"]] = rec
            self._msgs.append(rec)
            self._trim()
        out = dict(rec)
        out["event"] = "new"
        self._emit(out)

    # ------------------------------------------------------------ 接收内部
    def _loop(self):
        sock = self._recv
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(RECV_BUF)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._on_datagram(data, addr[0] if addr else "")
            except Exception:
                pass        # 单条坏报文不该弄死接收线程
        # 线程退出前把 socket 丢掉（stop() 可能已经换过引用）
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _on_datagram(self, data, from_ip):
        try:
            d = json.loads((data or b"").decode("utf-8"))
        except Exception:
            return
        if not isinstance(d, dict) or d.get("m") != CHAT_MAGIC:
            return
        mid = str(d.get("id") or "")
        kind = str(d.get("t") or "")
        if not mid or kind not in ("text", "file", "withdraw"):
            return

        if from_ip == self._my_ip:
            # 自己广播的回声（EasyTier 中继偶尔会绕回来）：只记 id，不上屏。
            with self._lock:
                self._remember_id(mid)
            return

        with self._lock:
            # 去重只对 text/file 生效：撤回报文**复用**原消息的 id，
            # 如果也去过重表，它会被当成"3 连发的另外两遍"直接丢掉——
            # 表现为"我取消了分享，队友聊天室里那条还在"。
            if kind in ("text", "file") and self._id_seen(mid):
                return
            self._remember_id(mid)
            out = None
            if kind == "withdraw":
                rec = self._by_id.get(mid)
                if rec is not None and not rec.get("withdrawn"):
                    rec["withdrawn"] = True
                    out = dict(rec)
            else:
                rec = {"id": mid, "kind": kind, "from_ip": from_ip,
                       "nick": norm_nick(d.get("n")), "me": False,
                       "ts": float(d.get("ts") or 0) or time.time(),
                       "withdrawn": False}
                if kind == "text":
                    rec["text"] = str(d.get("x") or "")[:TEXT_MAX]
                    if not rec["text"]:
                        return
                else:
                    rec["name"] = str(d.get("x") or "")[:NAME_MAX]
                    try:
                        rec["size"] = int(d.get("sz") or 0)
                    except (TypeError, ValueError):
                        rec["size"] = 0
                    rec["fid"] = str(d.get("fid") or "")
                    try:
                        rec["port"] = int(d.get("p") or 0)
                    except (TypeError, ValueError):
                        rec["port"] = 0
                    if not rec["fid"]:
                        return              # 没有 fid 就没法下载，不上屏
                self._by_id[mid] = rec
                self._msgs.append(rec)
                self._trim()
                out = dict(rec)
        if out is not None:
            out["event"] = "update" if kind == "withdraw" else "new"
            self._emit(out)

    # ------------------------------------------------------------ 小工具
    def _remember_id(self, mid):
        """记一个处理过的 id（幂等；列表超长就丢最旧的）。"""
        if mid and mid not in self._seen:
            self._seen.append(mid)
            if len(self._seen) > SEEN_MAX:
                del self._seen[:len(self._seen) - SEEN_MAX]

    def _id_seen(self, mid):
        return mid in self._seen

    def _trim(self):
        """历史超长就丢最旧的（by_id 里留着，撤回还要能找到）。"""
        while len(self._msgs) > HISTORY_MAX:
            self._msgs.pop(0)
        if len(self._by_id) > HISTORY_MAX * 2:
            self._by_id = {m["id"]: m for m in self._msgs}

    def _emit(self, rec):
        try:
            self._on_message(rec)
        except Exception:
            pass
