"""临时云盘：把文件分享给同一虚拟局域网里的成员，退出房间即失效。

思路与 MCTier 的「文件夹共享」一致（github.com/pmh1314520/MCTier）：
每个客户端在自己那块**虚拟网卡**上起一个 HTTP 服务，成员之间互相拉清单、
拉文件。没有中心存储——谁分享的文件就在谁的机器上，别人是「下载」而不是
「上传」，所以既不占额外空间，也不经第三方服务器。

为什么必须绑虚拟 IP，而不是 0.0.0.0
------------------------------------
绑 0.0.0.0 会连物理网卡一起监听（家里 / 宿舍 / 公司局域网），同网段的
陌生人只要猜到端口就能拖走文件。绑 10.126.126.x 之后，只有持同一组
房间码 + 密码、进了同一虚拟网的成员能访问；**退出房间时虚拟网卡一断，
端口连同服务一起消失**——「退出后无法下载」就是这么来的，不靠额外逻辑，
也没有残留可能。

安全：虚拟网隔离之外，再用「房间码 + 密码」派生的 token 校验请求头，
避免虚拟网里残留的旧房间或撞码的其它房间误访问到本机文件。

本模块是**纯 Python（只用标准库）**，不依赖 Qt——与 etier.py 同一分层，
方便单独测试与在后台线程里调用。
"""

import hashlib
import http.client
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

# 云盘服务端口：固定基址 + 顺序探测。选 4 万段是为了避开系统 / 常见软件，
# 也避开昵称信标的 41234，免得两者抢端口。
BASE_PORT = 41777
PORT_TRIES = 24

CHUNK = 64 * 1024
LIST_TIMEOUT = 3.0
TOKEN_HEADER = "X-Yuhub-Token"

# 单个文件上限：临时云盘走的是社区中继/打洞链路，几十 GB 的文件不现实，
# 也会把界面和磁盘拖垮。8 GB 足够覆盖游戏存档、整合包、录屏。
MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def make_token(room_code, password):
    """由「房间码 + 密码」派生共享 token。

    同一个房间的人算出来必然一致（两边输入本来就要求完全相同），
    虚拟网外的请求拿不到这个值。
    """
    raw = ("%s|%s" % (room_code or "", password or "")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def human_size(n):
    """字节数转可读文本。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def download_dir():
    """下载落地目录。退出房间不清空——用户已经拿到手的文件当然要留着。"""
    path = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub", "lanshare")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name, fallback="file"):
    """清洗成 Windows 可用的文件名（防路径穿越与非法字符）。"""
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = _BAD_NAME.sub("_", name).strip(" .")
    if not name or name in (".", ".."):
        name = fallback
    return name[:180]


def unique_path(directory, name):
    """在 directory 下给 name 找一个不冲突的落盘路径（重名加 (1)、(2)…）。"""
    base = safe_name(name)
    stem, ext = os.path.splitext(base)
    cand = os.path.join(directory, base)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(directory, "%s (%d)%s" % (stem, i, ext))
        i += 1
        if i > 999:
            cand = os.path.join(directory, "%s-%s%s" % (stem, secrets.token_hex(3), ext))
            break
    return cand


# ---------------------------------------------------------------------------
# HTTP 服务端（本人分享出去的文件）
# ---------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    """只实现两个 GET：列清单、取文件。其余一律 404。"""

    protocol_version = "HTTP/1.1"
    timeout = 30                      # 半开连接别一直挂着

    def log_message(self, *args):
        """屏蔽默认的 stderr 访问日志（打包成 --windowed 后没有 stderr，
        写了也看不见，反而可能在极端情况下抛异常）。"""

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        cfg = getattr(self.server, "yuhub", None)
        if cfg is None:
            return self._json(503, {"error": "service unavailable"})
        # ① token 校验：虚拟网隔离之外的第二道门
        if not cfg.token_ok(self.headers.get(TOKEN_HEADER)):
            return self._json(403, {"error": "token mismatch"})

        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/list":
            return self._list()
        if path.startswith("/api/get/"):
            return self._file(path[len("/api/get/"):])
        return self._json(404, {"error": "not found"})

    def do_HEAD(self):
        """只回头部，方便对接方探测（不传 body）。"""
        self.do_GET()

    def do_POST(self):
        return self._json(405, {"error": "read-only"})

    # ------------------------------------------------------------ 实现
    def _list(self):
        cfg = self.server.yuhub
        payload = {
            "app": "yuhub-share",
            "v": 1,
            "nick": cfg.nick,
            "files": cfg.list_files(),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, fid):
        cfg = self.server.yuhub
        entry = cfg.file_entry(urllib.parse.unquote((fid or "").strip()))
        if entry is None:
            return self._json(404, {"error": "no such file"})
        path = entry.get("path") or ""
        try:
            size = os.path.getsize(path)
        except OSError:
            # 分享后被移动/删除：如实回 410，别让对端下到一个空文件
            with cfg._lock:
                cfg._files.pop(entry["id"], None)
            cfg._log("分享的文件已不存在，已从列表移除：%s" % entry.get("name"))
            return self._json(410, {"error": "gone"})

        name = entry.get("name") or os.path.basename(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        # filename* 用 RFC 5987 的 UTF-8 形式，中文名不会乱码
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                sent = 0
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                cfg._bytes_out += sent
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                      # 对端中断/取消下载，正常现象
        except OSError:
            pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True             # 主程序退出时别被工作线程拖住
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """客户端中途断开是**常态**（用户点取消、关掉页面、切网络），
        不该当成错误刷 traceback。

        socketserver 默认把 handler 里任何没被捕获的异常都打一份 traceback
        到 stderr——打包成 --windowed 时看不见，源码态 / 带控制台运行时
        却会污染输出，掩盖真正的错误。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError, socket.timeout)):
            return
        super().handle_error(request, client_address)


class ShareServer:
    """本机对房间公开的那份文件清单 + HTTP 服务。线程安全。"""

    def __init__(self, ip, token, nick="", on_log=None):
        self._ip = ip
        self._token = token or ""
        self.nick = (nick or "").strip()[:32]
        self._on_log = on_log or (lambda msg: None)
        self._lock = threading.RLock()
        # fid -> {"id","path","name","size","mtime"}
        self._files = {}
        self._httpd = None
        self._thread = None
        self._port = 0
        self._bytes_out = 0
        self._served = 0              # 被下载次数（诊断用）

    # ------------------------------------------------------------ 生命周期
    @property
    def running(self):
        return self._httpd is not None

    @property
    def port(self):
        return self._port

    @property
    def ip(self):
        return self._ip

    def start(self, attempts=8, delay=0.5):
        """绑虚拟 IP 起服务。返回 (成功, 说明)。

        虚拟 IP 刚由 DHCP 分下来时，Windows 上地址可能还在重复地址检测
        （DAD）阶段，此时 bind 会失败——所以这里带重试，而不是一次定生死。
        """
        if self._httpd is not None:
            return True, "已在运行"
        last = ""
        for _ in range(max(1, attempts)):
            for port in range(BASE_PORT, BASE_PORT + PORT_TRIES):
                try:
                    httpd = _Server((self._ip, port), _Handler)
                except OSError as exc:
                    last = str(exc)
                    continue
                httpd.yuhub = self
                self._httpd = httpd
                self._port = port
                self._thread = threading.Thread(
                    target=httpd.serve_forever, kwargs={"poll_interval": 0.4},
                    name="ShareServer", daemon=True)
                self._thread.start()
                self._on_log("临时云盘已开启（%s:%d）" % (self._ip, port))
                return True, "ok"
            time.sleep(delay)
        msg = "云盘服务无法在 %s 上监听：%s" % (self._ip, last or "地址未就绪")
        self._on_log(msg)
        return False, msg

    def stop(self):
        """关服务并清空清单。退出房间时调用——这也是「退出后无法下载」的落点。"""
        httpd, self._httpd = self._httpd, None
        self._port = 0
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        with self._lock:
            self._files.clear()
        self._thread = None

    def rebind(self, new_ip):
        """本机虚拟 IP 变了：换地址重新监听（不重新分享，清单保留）。"""
        if new_ip == self._ip and self.running:
            return True, "未变化"
        keep = self.list_files()
        paths = [e["path"] for e in keep]
        self.stop()
        self._ip = new_ip
        with self._lock:
            self._files.clear()
        ok, msg = self.start()
        if ok and paths:
            self.add_paths(paths)
        return ok, msg

    # ------------------------------------------------------------ 清单
    def add_paths(self, paths):
        """登记要分享的文件。返回 (加入的条目, 跳过的说明列表)。"""
        added, skipped = [], []
        for raw in paths:
            p = os.path.abspath(str(raw or ""))
            if not os.path.isfile(p):
                skipped.append("%s（不是文件）" % os.path.basename(p or "?"))
                continue
            try:
                size = os.path.getsize(p)
            except OSError as exc:
                skipped.append("%s（%s）" % (os.path.basename(p), exc))
                continue
            if size > MAX_FILE_BYTES:
                skipped.append("%s（超过 %s）"
                               % (os.path.basename(p), human_size(MAX_FILE_BYTES)))
                continue
            with self._lock:
                dup = next((e for e in self._files.values()
                            if os.path.normcase(e["path"]) == os.path.normcase(p)), None)
            if dup is not None:
                skipped.append("%s（已在列表中）" % os.path.basename(p))
                continue
            entry = {
                "id": secrets.token_hex(8),
                "path": p,
                "name": os.path.basename(p),
                "size": size,
                "mtime": int(os.path.getmtime(p)),
            }
            with self._lock:
                self._files[entry["id"]] = entry
            added.append(dict(entry))
        return added, skipped

    def remove(self, fid):
        with self._lock:
            return self._files.pop(fid, None) is not None

    def clear(self):
        with self._lock:
            self._files.clear()

    def file_entry(self, fid):
        with self._lock:
            e = self._files.get(fid)
            return dict(e) if e else None

    def list_files(self):
        """对外公开的清单（**不含本机绝对路径**——路径是隐私，且对端用不上）。"""
        with self._lock:
            items = [(e["mtime"], e) for e in self._files.values()]
        items.sort(key=lambda x: x[0], reverse=True)     # 新加的排前面
        return [{"id": e["id"], "name": e["name"],
                 "size": e["size"], "mtime": e["mtime"]} for _m, e in items]

    def stats(self):
        with self._lock:
            return {"files": len(self._files), "bytes_out": self._bytes_out}

    def token_ok(self, got):
        """常数时间比较，避免逐字符试探。"""
        if not self._token:
            return False
        return secrets.compare_digest(str(got or ""), self._token)

    def _log(self, msg):
        try:
            self._on_log(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP 客户端（拉别人的清单 / 文件）
# ---------------------------------------------------------------------------
class ShareClient:
    """对某一个成员云盘服务的只读视图。无状态，调用完即关连接。"""

    def __init__(self, ip, port, token, timeout=LIST_TIMEOUT):
        self.ip = ip
        self.port = int(port or 0)
        self._token = token or ""
        self._timeout = timeout

    def list_files(self):
        """返回 (成功, 文件列表 或 错误说明)。"""
        if not self.ip or not self.port:
            return False, "对方未开启云盘"
        conn = None
        try:
            conn = http.client.HTTPConnection(self.ip, self.port, timeout=self._timeout)
            conn.request("GET", "/api/list",
                         headers={TOKEN_HEADER: self._token})
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                return False, "HTTP %d" % resp.status
            data = json.loads(raw.decode("utf-8"))
            files = data.get("files") or []
            return True, [{"id": str(f.get("id") or ""),
                           "name": str(f.get("name") or ""),
                           "size": int(f.get("size") or 0),
                           "mtime": int(f.get("mtime") or 0)}
                          for f in files if f.get("id")]
        except (socket.timeout, TimeoutError):
            return False, "连接超时"
        except (ConnectionRefusedError, OSError):
            return False, "未开启或不可达"
        except (ValueError, json.JSONDecodeError):
            return False, "响应格式异常"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def download(self, fid, dest_dir=None, progress=None, is_cancelled=None,
                 timeout=15.0, dest_path=None):
        """下载一个文件。返回 (成功, 落盘路径 或 错误说明)。

        progress(done, total) 会被频繁调用（每个分块一次），调用方自己节流。
        is_cancelled() 返回真时中断并删掉半成品（.part）。

        dest_path 给了就**严格存到那个路径**（用户在保存对话框里选的，
        重名他已经在系统对话框里确认过覆盖了，这里不能再自作主张改名）；
        没给才退回 dest_dir + 去重命名的老行为（自检等无人值守场景用）。
        """
        if not self.ip or not self.port:
            return False, "对方未开启云盘"
        if dest_path:
            dest = os.path.abspath(str(dest_path))
            parent = os.path.dirname(dest)
            if parent:
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as exc:
                    return False, "保存位置不可用：%s" % exc
        else:
            dest = ""
        conn = None
        tmp = None
        try:
            conn = http.client.HTTPConnection(self.ip, self.port, timeout=timeout)
            conn.request("GET", "/api/get/" + urllib.parse.quote(str(fid)),
                         headers={TOKEN_HEADER: self._token})
            resp = conn.getresponse()
            if resp.status != 200:
                return False, "对方返回 HTTP %d" % resp.status
            name = _name_from_disposition(resp.getheader("Content-Disposition"))
            total = int(resp.getheader("Content-Length") or 0)
            if not dest:
                dest = unique_path(dest_dir or download_dir(), name)
            tmp = dest + ".part"
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    if is_cancelled is not None and is_cancelled():
                        raise _Cancelled()
                    chunk = resp.read(CHUNK)          # 服务端关了连接就返回空
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
            if total and done != total:
                raise OSError("文件不完整（%s / %s）"
                              % (human_size(done), human_size(total)))
            os.replace(tmp, dest)
            tmp = None
            return True, dest
        except _Cancelled:
            return False, "已取消"
        except (socket.timeout, TimeoutError):
            return False, "传输超时"
        except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
            return False, "传输失败：%s" % exc
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


class _Cancelled(Exception):
    pass


def _name_from_disposition(value):
    """从 Content-Disposition 里取原始文件名（兼容 filename* 与 filename）。"""
    value = value or ""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", value, re.I)
    if m:
        try:
            return safe_name(urllib.parse.unquote(m.group(1)))
        except Exception:
            pass
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', value, re.I)
    if m:
        return safe_name(m.group(1))
    return "file"


# ---------------------------------------------------------------------------
# 聚合：一个房间一个实例
# ---------------------------------------------------------------------------
class ShareHub:
    """把「我分享的服务」与「所有成员的清单」合成一份视图。

    快照拉取是**网络操作**（每个成员一次 HTTP），必须放后台线程，
    不要在 UI 线程里调 `collect()`。
    """

    def __init__(self, token, nick="", on_log=None):
        self._token = token or ""
        self._on_log = on_log or (lambda msg: None)
        self.server = ShareServer("", self._token, nick=nick, on_log=on_log)
        self._last_error = {}

    # ------------------------------------------------------------ 生命周期
    def start(self, my_ip, attempts=8):
        """进房后调用：绑定本机虚拟 IP 并开始分享。"""
        self.server._ip = my_ip
        return self.server.start(attempts=attempts)

    def stop(self):
        """退出房间：服务下线 + 清单清空。此后队友再也拉不到你的文件。"""
        self.server.stop()
        self._last_error.clear()

    def rebind(self, new_ip):
        return self.server.rebind(new_ip)

    @property
    def running(self):
        return self.server.running

    @property
    def port(self):
        return self.server.port

    def set_nick(self, nick):
        self.server.nick = (nick or "").strip()[:32]

    # ------------------------------------------------------------ 我分享的
    def add_paths(self, paths):
        return self.server.add_paths(paths)

    def add_path(self, path):
        added, skipped = self.server.add_paths([path])
        return (True, added[0]) if added else (False, skipped[0] if skipped else "无法分享")

    def remove(self, fid):
        return self.server.remove(fid)

    def own_files(self):
        return self.server.list_files()

    def stats(self):
        return self.server.stats()

    # ------------------------------------------------------------ 聚合视图
    def collect(self, peers, timeout=LIST_TIMEOUT):
        """拉取所有成员的清单。

        peers: [{"ip","port"}...]（不含自己；port 为 0 表示对方没开云盘）
        返回 [{"ip","ok","files","error"}]，顺序与 peers 一致。
        """
        out = []
        for p in peers or []:
            ip = str(p.get("ip") or "")
            port = int(p.get("port") or 0)
            if not ip or not port:
                out.append({"ip": ip, "ok": False, "files": [],
                            "error": "对方未开启云盘"})
                continue
            ok, data = ShareClient(ip, port, self._token, timeout=timeout).list_files()
            if ok:
                self._last_error.pop(ip, None)
                out.append({"ip": ip, "ok": True, "files": data, "error": ""})
            else:
                self._last_error[ip] = data
                out.append({"ip": ip, "ok": False, "files": [], "error": data})
        return out

    def download(self, ip, port, fid, dest_dir=None, progress=None,
                 is_cancelled=None, timeout=15.0, dest_path=None):
        return ShareClient(ip, int(port or 0), self._token, timeout=timeout).download(
            fid, dest_dir=dest_dir, progress=progress,
            is_cancelled=is_cancelled, timeout=timeout, dest_path=dest_path)
