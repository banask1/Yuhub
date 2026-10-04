# -*- coding: utf-8 -*-
"""本地反向代理引擎（Steam++ 同形态）—— 纯标准库，不含 Qt、不碰任何子进程。

做什么
------
把加速域名的 hosts 指到 127.0.0.1，然后在本机监听 443 / 80：

- **443（HTTPS）**：读出 TLS ClientHello 里的 SNI（要访问的域名），按 SNI
  从优选映射表找到真实 IP，连上去之后**原样透传**双向字节流。全程不解密、
  不需要证书 —— 客户端和真实服务器之间的 TLS 握手、证书校验完全不受影响。
  这一步解决的是 DNS 污染：浏览器以为连的是 steamcommunity.com，实际链路
  由我们来选最快的真实节点。
- **80（HTTP）**：按请求头里的 Host 转发，同样是字节流透传。

线程模型
--------
每个监听器一条 accept 线程；每条连接再分 2 条方向线程做透传。
全部是 daemon 线程，`stop()` 时 shutdown + close，进程退出自动收尾。

硬约定
------
1. **纯标准库**（socket / threading / time），绝无第三方依赖。
2. **不解密流量、不存内容**：透传代理只看得到 ClientHello 明文头与 HTTP
   请求头（为了拿 SNI / Host），其余字节原样搬运。
3. **resolver 是外部注入的回调**（`domain -> ip or None`），本模块不关心
   IP 怎么来的（优选引擎的事）；这样自检可以打桩，不碰真网络。
4. 端口被占用（比如 Steam++ 自己在跑）时 `start()` 返回 False 并带原因，
   绝不静默装作在加速。
"""

import socket
import threading
import time

# TLS ContentTypes
_TLS_HANDSHAKE = 0x16
# HandshakeType
_TLS_CLIENT_HELLO = 0x01
# Extension type: server_name
_EXT_SNI = 0x0000

_RECV_HEAD_TIMEOUT = 3.0     # 等客户端发 ClientHello 的耐心
_UPSTREAM_TIMEOUT = 8.0      # 连真实节点的超时
_SPLICE_BUFSZ = 65536


def parse_client_hello_sni(data):
    """从 TLS 握手首包里解析 SNI 域名（纯函数，自检重点覆盖）。

    返回域名字符串；不是 TLS ClientHello、包不完整或没带 SNI 时返回 ""。
    只解析、不抛异常 —— 网络首包什么垃圾字节都可能有。
    """
    try:
        buf = bytes(data)
        # TLS 记录层：type(1) version(2) length(2)
        if len(buf) < 5 or buf[0] != _TLS_HANDSHAKE:
            return ""
        rec_len = int.from_bytes(buf[3:5], "big")
        body = buf[5:5 + rec_len]
        # Handshake: type(1) length(3)
        if len(body) < 4 or body[0] != _TLS_CLIENT_HELLO:
            return ""
        hs_len = int.from_bytes(body[1:4], "big")
        hs = body[4:4 + hs_len]
        # ClientHello: version(2) random(32) session_id_len(1)+id cipher_len(2)+ciphers
        # compression_len(1)+methods extensions_len(2)+extensions
        p = 2 + 32
        if len(hs) < p + 1:
            return ""
        sid_len = hs[p]
        p += 1 + sid_len
        if len(hs) < p + 2:
            return ""
        cs_len = int.from_bytes(hs[p:p + 2], "big")
        p += 2 + cs_len
        if len(hs) < p + 1:
            return ""
        comp_len = hs[p]
        p += 1 + comp_len
        if len(hs) < p + 2:
            return ""
        ext_total = int.from_bytes(hs[p:p + 2], "big")
        p += 2
        end = min(len(hs), p + ext_total)
        while p + 4 <= end:
            etype = int.from_bytes(hs[p:p + 2], "big")
            elen = int.from_bytes(hs[p + 2:p + 4], "big")
            edata = hs[p + 4:p + 4 + elen]
            p += 4 + elen
            if etype != _EXT_SNI or len(edata) < 5:
                continue
            # server_name_list: list_len(2) { name_type(1) len(2) name }
            n_len = int.from_bytes(edata[0:2], "big")
            q = 2
            q_end = min(len(edata), 2 + n_len)
            while q + 3 <= q_end:
                name_type = edata[q]
                name_len = int.from_bytes(edata[q + 1:q + 3], "big")
                name = edata[q + 3:q + 3 + name_len]
                q += 3 + name_len
                if name_type == 0:               # host_name
                    try:
                        return name.decode("ascii").lower()
                    except UnicodeDecodeError:
                        return ""
        return ""
    except Exception:
        return ""


def build_client_hello(sni):
    """造一个最小 ClientHello（自检/联调用）：合法结构 + 指定 SNI。"""
    name = sni.encode("ascii")
    # server_name 扩展数据：list_len(2) type(1) len(2) name
    sni_data = b"".join([
        int.to_bytes(len(name) + 3, 2, "big"),
        b"\x00",
        int.to_bytes(len(name), 2, "big"),
        name,
    ])
    # 扩展条目：ext_type(2)=server_name + ext_len(2) + data
    sni_ext = int.to_bytes(_EXT_SNI, 2, "big") + \
        int.to_bytes(len(sni_data), 2, "big") + sni_data
    ext_block = int.to_bytes(len(sni_ext), 2, "big") + sni_ext
    body = b"".join([
        b"\x03\x03",                             # client version TLS1.2
        b"\x00" * 32,                            # random
        b"\x00",                                 # session id len
        int.to_bytes(2, 2, "big") + b"\x13\x01", # cipher suites
        b"\x01\x00",                             # compression: null
        ext_block,
    ])
    hs = b"\x01" + int.to_bytes(len(body), 3, "big") + body
    rec = b"\x16\x03\x01" + int.to_bytes(len(hs), 2, "big") + hs
    return rec


def parse_http_head_host(head):
    """从 HTTP 请求头文本里解析 Host（纯函数）。找不到返回 ""。"""
    try:
        text = head.decode("latin-1", "ignore")
    except Exception:
        return ""
    for line in text.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        if key.strip().lower() == "host":
            host = val.strip()
            # 去掉 :port
            if host.startswith("["):              # IPv6 字面量
                return host
            return host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return ""


def check_port_free(port, host="127.0.0.1"):
    """端口当前是否可绑定。返回 (ok, 原因)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True, ""
    except OSError as exc:
        return False, "端口 %d 已被占用（%s）——请关闭占用它的程序（如 Steam++）" % (
            port, exc)
    finally:
        try:
            s.close()
        except OSError:
            pass


class _Splice:
    """把一条已建立的连接双向搬运到另一条连接上。"""

    @staticmethod
    def pump(src, dst, close_event):
        try:
            while not close_event.is_set():
                chunk = src.recv(_SPLICE_BUFSZ)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            close_event.set()
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class SniProxy:
    """本地 HTTPS(SNI 透传) + HTTP(Host 转发) 反向代理。

    resolver: `f(domain) -> ip or None`，在工作线程被调用，允许阻塞
    （做 DoH + 测速都行，但必须是线程安全的）。
    """

    def __init__(self, resolver, tls_port=443, http_port=80,
                 bind_host="127.0.0.1", upstream_tls_port=443,
                 upstream_http_port=80):
        self._resolver = resolver
        self._tls_port = int(tls_port)
        self._http_port = int(http_port)
        self._up_tls_port = int(upstream_tls_port)      # 转发目标端口（自检打桩用）
        self._up_http_port = int(upstream_http_port)
        self._bind = bind_host
        self._tls_srv = None
        self._http_srv = None
        self._threads = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stats = {"conns": 0, "errors": 0, "last_error": "",
                       "unknown_domains": 0}
        self.running = False

    # ------------------------------------------------------------- 状态
    def stats(self):
        with self._lock:
            return dict(self._stats)

    def _note(self, ok, msg=""):
        with self._lock:
            if ok:
                self._stats["conns"] += 1
            else:
                self._stats["errors"] += 1
                self._stats["last_error"] = msg
            if "不在优选清单" in msg or "解析失败" in msg:
                self._stats["unknown_domains"] += 1

    # ------------------------------------------------------------- 启停
    def start(self):
        """绑定并启动监听。返回 (ok, 原因)。幂等：已在跑直接成功。"""
        if self.running:
            return True, ""
        for port in (self._tls_port, self._http_port):
            ok, why = check_port_free(port, self._bind)
            if not ok:
                return False, why
        try:
            self._tls_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._tls_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._tls_srv.bind((self._bind, self._tls_port))
            self._tls_srv.listen(32)
            self._http_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._http_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._http_srv.bind((self._bind, self._http_port))
            self._http_srv.listen(32)
        except OSError as exc:
            self._close_servers()
            return False, "监听失败：%s" % exc
        self._stop.clear()
        for srv, fn in ((self._tls_srv, self._accept_tls),
                        (self._http_srv, self._accept_http)):
            t = threading.Thread(target=fn, args=(srv,),
                                 name="YuhubSniProxy", daemon=True)
            t.start()
            self._threads.append(t)
        self.running = True
        return True, ""

    def stop(self):
        self._stop.set()
        self._close_servers()
        self.running = False
        self._threads = []

    def _close_servers(self):
        for s in (self._tls_srv, self._http_srv):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._tls_srv = self._http_srv = None

    # ------------------------------------------------------------- 接收循环
    def _accept_tls(self, srv):
        while not self._stop.is_set():
            try:
                client, addr = srv.accept()
            except OSError:
                break                                   # 服务端被关闭
            threading.Thread(target=self._handle_tls, args=(client,),
                             daemon=True).start()

    def _accept_http(self, srv):
        while not self._stop.is_set():
            try:
                client, addr = srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_http, args=(client,),
                             daemon=True).start()

    # ------------------------------------------------------------- HTTPS
    def _handle_tls(self, client):
        client.settimeout(_RECV_HEAD_TIMEOUT)
        head = b""
        sni = ""
        try:
            # ClientHello 可能一包就来，也可能拆包 —— 循环读直到解析成功
            deadline = time.monotonic() + _RECV_HEAD_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    chunk = client.recv(16384)
                except socket.timeout:
                    break
                if not chunk:
                    break
                head += chunk
                sni = parse_client_hello_sni(head)
                if sni:
                    break
                if len(head) > 65536:
                    break
        except OSError:
            self._note(False, "读取 ClientHello 失败")
            try:
                client.close()
            except OSError:
                pass
            return
        if not sni:
            self._note(False, "未解析到 SNI")
            try:
                client.close()
            except OSError:
                pass
            return
        ip = self._safe_resolve(sni)
        if not ip:
            self._note(False, "SNI %s 解析失败" % sni)
            try:
                client.close()
            except OSError:
                pass
            return
        # 关键：读进来的 ClientHello 字节必须原样转给上游 —— 这是 TLS 握手
        # 的第一段，吞掉它服务器永远等不到握手，连接会挂死。
        self._relay(client, (ip, self._up_tls_port), first_data=head)

    # ------------------------------------------------------------- HTTP
    def _handle_http(self, client):
        client.settimeout(_RECV_HEAD_TIMEOUT)
        head = b""
        host = ""
        try:
            while len(head) < 65536:
                chunk = client.recv(16384)
                if not chunk:
                    break
                head += chunk
                if b"\r\n\r\n" in head:
                    break
        except OSError:
            self._note(False, "读取 HTTP 头失败")
            try:
                client.close()
            except OSError:
                pass
            return
        head_part = head.split(b"\r\n\r\n", 1)[0]
        host = parse_http_head_host(head_part)
        if not host:
            self._note(False, "HTTP 请求缺 Host")
            try:
                client.close()
            except OSError:
                pass
            return
        ip = self._safe_resolve(host)
        if not ip:
            self._note(False, "Host %s 解析失败" % host)
            try:
                client.close()
            except OSError:
                pass
            return
        self._relay(client, (ip, self._up_http_port), first_data=head)

    # ------------------------------------------------------------- 转发
    def _safe_resolve(self, domain):
        """resolver 包一层：异常一律当解析失败（回调在别人的线程里跑）。"""
        try:
            return self._resolver(domain)
        except Exception:
            return None

    def _relay(self, client, upstream_addr, first_data=b""):
        try:
            client.settimeout(None)
            upstream = socket.create_connection(upstream_addr,
                                                timeout=_UPSTREAM_TIMEOUT)
        except OSError as exc:
            self._note(False, "连接上游 %s:%d 失败：%s" % (
                upstream_addr[0], upstream_addr[1], exc))
            try:
                client.close()
            except OSError:
                pass
            return
        self._note(True)
        done = threading.Event()
        if first_data:
            try:
                upstream.sendall(first_data)
            except OSError:
                done.set()
        t1 = threading.Thread(target=_Splice.pump,
                              args=(client, upstream, done), daemon=True)
        t2 = threading.Thread(target=_Splice.pump,
                              args=(upstream, client, done), daemon=True)
        t1.start()
        t2.start()
        # 兜底收割：任一方向结束即通知，两条线程都会在 Event 上退出循环
        t1.join()
        t2.join()
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass
