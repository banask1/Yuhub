# -*- coding: utf-8 -*-
"""中继节点测速（方案 A）：并发探测内置节点的实测延迟，给下拉框排序与显示。

为什么需要它
------------
内置节点里没有"谁永远最快"这回事：同一个「自动」在上海可能 20ms 打到
国内中继，在美国却要 208ms 才到海波美国；反过来也一样。哪个节点快**取决
于你在哪**，程序写不死，所以让用户看着**自己这台机器测出来的**数字去选。

这也正好补上"人在国外"这个场景：节点列表里谁能连、谁快，一测就知道，
不用挨个盲试（盲试一次就是 20 秒的进房等待）。

三条设计红线
------------
1. **不拿过期数据骗人**。每个结果都带测量时间戳，超过 TTL（默认 90 秒）
   即视为过期：UI 上直接显示「待重测」而不是旧数字，并立刻安排重测。
   宁可短暂少一个数字，也不能让用户按半小时前的延迟去选节点。
2. **测速不扰民**。全程在后台线程，失败静默降级成「超时」，不弹窗、
   不阻塞进房；单个节点最坏几秒，几个节点并发跑。
3. **测法要贴近真实链路**。
     - tcp:// 节点 → 直接 TCP 握手计时（EasyTier 走的就是这条路径，最准）；
     - udp:// 节点 → TCP 端口多半不接，回退系统 ICMP ping 计时（主机往返
       的近似值），并把"TCP 端口不通"如实标出来；
     - 都不通 → 至少给出域名解析结论（解析不出就别指望能连）。
"""

import re
import shutil
import socket
import subprocess
import sys
import threading
import time

# 结果有效期（秒）。超过它，UI 就认为"这个数字算旧了"。
TTL_SECONDS = 90.0

# 「永不过期」的哨兵。v0.8.14beta 起节点延迟改成**进页面 + 手动**刷新，
# 不再定时重测，所以下拉里要一直保留上次实测的数字——年龄由旁边的
# 「延迟数据：x 分钟前」如实说明。不能过 90 秒就整列变回「待测」：
# 那样用户会以为"没数据"，而不是"数据有点旧"。
TTL_NEVER = float("inf")

# 单次探测超时（秒）。TCP 握手用；ICMP 的每次等待也由它派生。
# 真实节点握手在 300ms 内，1.2 秒足够宽松；同时把"节点全挂/整机断网"时
# 一轮测速的最坏耗时压在一轮 ping 之内，别让后台线程长时间占着不放。
PROBE_TIMEOUT = 1.2

# ping 的探测次数（Windows 的 -n / POSIX 的 -c）。取最小 RTT，抗抖动。
PING_COUNT = 3

# 并发上限。节点一般只有几个，够用且不会把网卡打满。
MAX_WORKERS = 6

PROTO_TCP = "tcp"
PROTO_UDP = "udp"

# Windows 上不要把控制台窗口弹出来（Yuhub 的既有纪律，见 etier.py 同名常量）
_CREATE_NO_WINDOW = 0x08000000

# "时间=208ms" / "时间<1ms" / "time=208ms" / "time<1ms"
_RE_RTT = re.compile(r"(?:时间|time)\s*[=＜<]\s*(\d+)\s*ms", re.I)
# "来自 1.2.3.4 的回复: 字节=32 时间=208ms TTL=54" / "Reply from 1.2.3.4: ..."
# 必须先看到"回复"行才认数字：ping 结尾的统计行在完全不通时是一串 0，
# 一旦被正则捞到，就会把"完全不通"报成"0ms 秒连"，而且各语言统计行的
# 措辞差异很大，用排除法守不住——只能正向认"回复行"。
_RE_REPLY = re.compile(r"(的回复)|(reply\s+from)|(bytes=|字节=)", re.I)


# ---------------------------------------------------------------------------
# 地址解析
# ---------------------------------------------------------------------------
def split_address(addr):
    """把 `tcp://host:11010` 拆成 ("tcp", "host", 11010)。

    解析不出来（空串、只有 auto 占位、缺 host）就返回 None——`auto` 那条
    不是节点，本来就不该测。
    """
    text = str(addr or "").strip()
    if not text or "://" not in text:
        return None
    scheme, rest = text.split("://", 1)
    proto = scheme.strip().lower()
    if proto not in (PROTO_TCP, PROTO_UDP):
        return None
    rest = rest.strip().strip("/")
    if not rest:
        return None
    # IPv6 字面量形如 [::1]:11010，本项目的节点都是 IPv4/域名，但别拆错
    if rest.startswith("["):
        end = rest.find("]")
        if end < 0:
            return None
        host = rest[1:end]
        tail = rest[end + 1:]
        port = int(tail[1:]) if tail.startswith(":") and tail[1:].isdigit() else 0
    elif ":" in rest:
        host, _, port_text = rest.rpartition(":")
        port = int(port_text) if port_text.isdigit() else 0
    else:
        host, port = rest, 0
    host = host.strip()
    if not host or not (0 < port < 65536):
        return None
    return proto, host, port


def is_ip_literal(host):
    """host 是不是 IP 字面量（是的话就不必谈"域名解析失败"）。"""
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        pass
    return ":" in host and host.replace(":", "").isalnum()


# ---------------------------------------------------------------------------
# 三种探测手段
# ---------------------------------------------------------------------------
def tcp_rtt_ms(host, port, timeout=PROBE_TIMEOUT, attempts=2):
    """TCP 握手往返耗时（毫秒）；连不上返回 None。多次取最小值抗抖动。"""
    best = None
    for _ in range(max(1, int(attempts))):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        started = time.perf_counter()
        try:
            sock.connect((host, port))
        except OSError:
            continue
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            try:
                sock.close()
            except OSError:
                pass
        if best is None or elapsed < best:
            best = elapsed
    return int(round(best)) if best is not None else None


def _decode_console(data):
    """Windows 的 ping 输出是本地代码页（中文机上是 GBK），逐个编码试。"""
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return ""


def _ping_executable():
    exe = shutil.which("ping")
    if exe:
        return exe
    if sys.platform == "win32":
        import os
        return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "PING.EXE")
    return None


def icmp_rtt_ms(host, timeout=PROBE_TIMEOUT, count=PING_COUNT):
    """ICMP ping 的最小往返耗时（毫秒）；不通 / 环境没有 ping 返回 None。

    走系统 `ping.exe` 而不是自己造 ICMP 包：后者要管理员权限（原始套接字），
    而 Yuhub 平时是普通权限运行的，为了测个延迟去提权不可接受。
    """
    exe = _ping_executable()
    if not exe:
        return None
    if sys.platform == "win32":
        # -w 是"每次回复最多等多少毫秒"，别让它比整体超时还长
        wait_ms = max(200, int(timeout * 1000))
        cmd = [exe, "-n", str(int(count)), "-w", str(wait_ms), host]
    else:
        cmd = [exe, "-c", str(int(count)), "-W", str(max(1, int(timeout))), host]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=int(count) * max(0.5, timeout) + 3.0,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = _decode_console(proc.stdout or b"")
    if not _RE_REPLY.search(text):
        # 一行"回复"都没有：整轮没通。绝不去摘要行里捡数字。
        return None
    hits = [int(m) for m in _RE_RTT.findall(text)]
    if hits:
        return min(hits)
    # 有回复行却没解析出时间（极端本地化文案）：宁可不报，也别报个假值。
    return None


def resolve_ok(host, port=0):
    """域名能否解析。IP 字面量恒为 True（它不需要解析）。

    刻意**不动 socket.setdefaulttimeout**：那是进程级全局开关，测速跑在
    后台线程，改了会把其它线程新建的 socket 一起带进超时语义里。
    getaddrinfo 自身没有超时参数，交给系统解析链（NXDomain 通常毫秒级返回）。
    """
    if is_ip_literal(host):
        return True
    try:
        socket.getaddrinfo(host, port or 80, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 单个节点探测
# ---------------------------------------------------------------------------
def probe(key, label, addr, timeout=PROBE_TIMEOUT, now=None,
          ping_count=PING_COUNT):
    """探测一个节点，返回一条结果记录。

    记录字段：
        key/label/addr  : 原样带过
        ms              : 实测毫秒（None = 没测出来）
        ok              : 这个节点现在**能不能用**
        via             : "tcp" / "icmp" / ""
        error           : 不可用原因（可直接显示给用户）
        at              : 测量时刻（time.time），TTL 判定的依据

    ping_count 一般不用传：默认 3 次取最小，抗抖动。自检里对"注定不可达"
    的地址会传 1 次——不可达时 Windows 每次等待远长于 -w，3 次能拖到 3.5 秒，
    而自检关心的只是"判定对不对"，不是"抖不抖"。
    """
    info = {
        "key": key, "label": label, "addr": addr,
        "ms": None, "ok": False, "via": "", "error": "",
        "at": float(now if now is not None else time.time()),
    }
    parsed = split_address(addr)
    if parsed is None:
        info["error"] = "非节点地址"
        return info
    proto, host, port = parsed

    # 1) TCP 握手。tcp:// 节点走的就是这条路径，最贴近 EasyTier 的实际体验。
    #    udp:// 节点只试一次：它的同号 TCP 端口开不开纯属巧合，握不上就交给
    #    下面的 ICMP，别为一个"顺便试试"的路径白等两轮超时。
    ms = tcp_rtt_ms(host, port, timeout=timeout,
                    attempts=2 if proto == PROTO_TCP else 1)
    if ms is not None:
        info.update(ms=ms, ok=True, via="tcp")
        return info

    # 2) ICMP 兜底。udp:// 节点的同号 TCP 端口通常没开，这时 ping 是唯一
    #    能测到东西的手段；主机往返延迟虽不等于 UDP 中继质量，但足以区分
    #    "我在美国的 200ms" 和 "国内直连的 20ms"。
    ms = icmp_rtt_ms(host, timeout=timeout, count=ping_count)
    if ms is not None:
        info["ms"] = ms
        info["via"] = "icmp"
        if proto == PROTO_UDP:
            info["ok"] = True
        else:
            # tcp:// 节点能 ping 通却握不上手 = 中继端口关了/被墙，别让用户
            # 以为"能 ping 通就能用"，如实标出来（这种情况该换节点）。
            info["error"] = "端口不通"
        return info

    # 3) 连 ping 都不通。至少要分清"域名没了"和"网络到不了"——前者是节点
    #    下线，后者可能只是自己这会儿没网，用户的处置完全不同。
    if not resolve_ok(host, port):
        info["error"] = "域名解析失败"
    else:
        info["error"] = "超时"
    return info


def probe_all(items, timeout=PROBE_TIMEOUT, workers=MAX_WORKERS,
              should_stop=None, on_result=None, ping_count=PING_COUNT):
    """并发探测一批节点，返回 {key: 结果记录}。

    items 是 (key, label, addr) 三元组；addr 为空的（auto）自动跳过。
    should_stop 返回真时尽快收手（页面已销毁 / 用户退出），剩下的节点不再
    探测——它们的槽位保持"没有数据"，UI 会显示成"待测"而不是错误。

    并发用**自己管的 daemon 线程**，而不是 ThreadPoolExecutor：后者的工作
    线程是非 daemon 的，解释器退出时会挨个 join。若用户此刻正在关 Yuhub、
    而一轮测速还没跑完（整机断网时最坏几秒），他就会看到"点了退出却半天
    不关"——这是 Yuhub 的既有纪律（见 etier.py 里"退出必须干净"那段）。
    daemon 线程随进程一起结束，没有这个问题。
    """
    todo = [it for it in items if split_address(it[2]) is not None]
    results = {}
    if not todo:
        return results

    lock = threading.Lock()
    pending = list(range(len(todo)))

    def _take():
        with lock:
            return pending.pop(0) if pending else None

    def _worker():
        while True:
            if should_stop is not None and should_stop():
                return
            idx = _take()
            if idx is None:
                return
            key, label, addr = todo[idx]
            try:
                rec = probe(key, label, addr, timeout, None, ping_count)
            except Exception:
                # 单个节点探测出意外不该让整轮全灭：跳过它，UI 上表现为
                # 这一项"待测"，比整块延迟一起消失好得多。
                continue
            if not rec:
                continue
            with lock:
                results[rec["key"]] = rec
            if on_result is not None:
                try:
                    on_result(rec)
                except Exception:
                    pass

    threads = [
        threading.Thread(target=_worker, daemon=True,
                         name="yuhub-nodeprobe-%d" % i)
        for i in range(max(1, min(workers, len(todo))))
    ]
    for th in threads:
        th.start()
    # 硬上限：最慢的节点也就 TCP 两轮超时 + 一轮 ping，等不到就别等了，
    # 已经拿到的结果照样有效（记下的 at 就是真实测量时刻）。
    deadline = time.monotonic() + max(4.0, timeout * 2 + 6.0)
    for th in threads:
        while th.is_alive():
            if should_stop is not None and should_stop():
                break
            if time.monotonic() >= deadline:
                break
            th.join(0.1)
    return results


# ---------------------------------------------------------------------------
# 结果缓存（带时间戳的 TTL）
# ---------------------------------------------------------------------------
class NodeProbeCache:
    """一次测速结果的容器：记得每条的测量时刻，过期就承认过期。

    UI 每次画下拉都问它要数据，所以"是不是过期"必须能在这里一眼判定——
    过期判断绝不能散落在界面代码里，否则迟早有一处忘了判、把旧数字当新的用。
    """

    def __init__(self, ttl=TTL_SECONDS):
        self._ttl = float(ttl)
        self._items = {}
        self._at = 0.0

    # -- 写 ---------------------------------------------------------------
    def update(self, results, at=None):
        """并入一轮测速结果。

        这一轮**没测到**的节点（取消/异常）会被清掉，避免它顶着一个
        更早的数字冒充"刚测过"。
        """
        stamp = float(at if at is not None else time.time())
        clean = {}
        for key, rec in dict(results or {}).items():
            item = dict(rec or {})
            item.setdefault("key", key)
            item["at"] = stamp
            clean[key] = item
        self._items = clean
        self._at = stamp
        return self

    def clear(self):
        self._items = {}
        self._at = 0.0

    # -- 读 ---------------------------------------------------------------
    def results(self, now=None, ttl=None, keep_stale=False):
        """取可用的结果。

        keep_stale=False（默认）时，过期条目**不返回**——调用方拿到的
        永远是当前有效的数据；这是"不给用户看过期延迟"的最后一道闸。
        """
        limit = self._ttl if ttl is None else float(ttl)
        now = time.time() if now is None else float(now)
        out = {}
        for key, rec in self._items.items():
            if keep_stale or (now - float(rec.get("at") or 0.0)) <= limit:
                out[key] = dict(rec)
        return out

    def snapshot(self):
        """全部原始条目（含可能过期的），给自检与日志用。"""
        return {k: dict(v) for k, v in self._items.items()}

    def get(self, key, now=None):
        return self.results(now=now).get(key)

    def has_data(self):
        return bool(self._items)

    def age(self, now=None):
        """距上次测速过了多少秒；从没测过返回 None。"""
        if not self._items:
            return None
        now = time.time() if now is None else float(now)
        return max(0.0, now - self._at)

    def is_stale(self, now=None, ttl=None):
        """该重新测速了吗？从没测过 = 过期（首屏就该测一次）。"""
        age = self.age(now=now)
        if age is None:
            return True
        return age > (self._ttl if ttl is None else float(ttl))

    @property
    def ttl(self):
        return self._ttl


# ---------------------------------------------------------------------------
# 展示：格式化 + 排序 + 标签
# ---------------------------------------------------------------------------
def format_latency(ms):
    """数字 → "208ms"；测不到 → "超时"（跟"还没测"是两回事）。"""
    if ms is None:
        return "超时"
    ms = int(ms)
    return "%dms" % ms


def age_text(seconds):
    """把"多久之前测的"说成人话。"""
    if seconds is None:
        return "尚未测速"
    seconds = max(0.0, float(seconds))
    if seconds < 3:
        return "刚刚"
    if seconds < 60:
        return "%d 秒前" % int(seconds)
    if seconds < 3600:
        return "%d 分钟前" % int(seconds // 60)
    return "%d 小时前" % int(seconds // 3600)


def _rank(rec):
    """排序键：可用且快的排前面，不可达的沉底。

    不可达的节点用大数兜住延迟，避免它们因为"超时=没有数字"被排到只有
    超时节点时的最前面而看起来像可用；同档内按延迟升序。
    """
    if not rec:
        return (3, 10 ** 6)
    if rec.get("ok"):
        return (0, int(rec.get("ms") or 10 ** 6))
    if rec.get("ms") is not None:
        return (1, int(rec["ms"]))          # ping 通但端口不通：主机活着
    return (2, 10 ** 6)


def short_name(label):
    """把「国内中继（IP 直连）」压成「国内中继」。

    只用在要嵌进别的括号里的场合（「自动（最快：…）」）。节点名自带的
    括号是用来补充说明的，套一层就成「自动（最快：国内中继（IP 直连） 36ms）」
    ——双层括号，读起来要数括号配对，反而看不清最快的到底是哪个。
    """
    txt = str(label or "").strip()
    for sep in ("（", "("):
        cut = txt.find(sep)
        if cut > 0:
            txt = txt[:cut]
            break
    return txt.strip() or str(label or "")


def _node_label(label, rec, missing_text="待测"):
    """单个节点的下拉文案。

    missing_text 给 None 就保持裸标签——首屏（还没测过）用它，免得一开页
    就看到一列「（待测）」；测过一次之后"某项本轮没数据"才是要说的信息。
    """
    if not rec:
        return "%s（%s）" % (label, missing_text) if missing_text else label
    if rec.get("ok"):
        suffix = format_latency(rec.get("ms"))
    else:
        suffix = rec.get("error") or "超时"
    # 节点名自带括号时（「国内中继（IP 直连）」）再套一层就成了
    # 「国内中继（IP 直连）（超时）」——连着两组括号要数配对，读着累。
    # 这种情况改用间隔号，一眼能看出后半截是状态而不是名字的一部分。
    if "（" in str(label):
        return "%s · %s" % (label, suffix)
    return "%s（%s）" % (label, suffix)


def decorate_choices(choices, results, now=None, ttl=None, missing_text="待测"):
    """把 NODE_CHOICES 变成"可排序、带延迟"的下拉项。

    返回 [(key, 最终文案, 地址, 结果记录)]，顺序即下拉框顺序：
      - 「自动」恒在第一（它是模式，不是某个节点，不该被延迟排序挤走）；
      - 具体节点按实测延迟升序，不可达的排最后。
    """
    limit = TTL_SECONDS if ttl is None else float(ttl)
    now = time.time() if now is None else float(now)
    fresh = {}
    for key, rec in dict(results or {}).items():
        if not rec:
            continue
        if (now - float(rec.get("at") or 0.0)) > limit:
            continue                            # 过期的当没有，绝不外显
        fresh[key] = rec

    auto_key = choices[0][0] if choices else "auto"
    auto_label_text = "自动（尝试全部节点）"
    # "自动"的副标题：把当前最快节点报出来，让用户知道 auto 会优先打到哪
    best = None
    for key, _label, addr in choices:
        if key == auto_key or not addr:
            continue
        rec = fresh.get(key)
        if rec and rec.get("ok"):
            if best is None or int(rec.get("ms") or 0) < int(best[1].get("ms") or 0):
                best = (_label, rec)
    if best is not None:
        auto_label_text = "自动（最快：%s %s）" % (
            short_name(best[0]), format_latency(best[1].get("ms")))

    ordered = []
    rest = []
    for key, label, addr in choices:
        if key == auto_key or not addr:
            ordered.append((key, auto_label_text, addr, None))
        else:
            rest.append((key, label, addr, fresh.get(key)))
    rest.sort(key=lambda item: _rank(item[3]))
    for key, label, addr, rec in rest:
        ordered.append((key, _node_label(label, rec, missing_text), addr, rec))
    return ordered


def summary_text(results, limit=4):
    """一行摘要，给日志用：「海波美国 208ms、唯爱厦门 超时」。"""
    parts = []
    for key, rec in sorted(dict(results or {}).items(),
                           key=lambda kv: _rank(kv[1])):
        if len(parts) >= limit:
            break
        label = (rec or {}).get("label") or key
        parts.append("%s %s" % (label, format_latency((rec or {}).get("ms"))))
    return "、".join(parts)
