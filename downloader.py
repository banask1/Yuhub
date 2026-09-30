"""Yuhub 多线程下载引擎。

纯标准库实现（urllib + threading），**不依赖 Qt**，便于独立测试。

设计参考 PCL2 的「下载自定义文件」：按线程数把文件切成若干块并行抓取，
把整体进度 / 瞬时速度 / 剩余时间实时汇报给上层。

用法：
    task = DownloadTask(url, save_path, threads=dl.threads_for_level("mid"))
    task.start()
    ...
    snap = task.snapshot()        # 线程安全，UI 定时轮询即可
    task.cancel()

线程数不要写死：`threads_for_level("low"/"mid"/"high")` 会按**本机逻辑核心数**
换算成 1/4、1/2、全部核心数，同一份程序换电脑不用改代码。
    """

import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import unquote

# 本机核心数 / 线程数的**唯一数据源**（首页 CPU 面板也读同一个模块）
from sysinfo import cpu_cores, cpu_threads, describe_cpu

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Yuhub/0.5"
)
# 线程数不设固定默认值：见下方 THREAD_LEVEL_* 与 threads_for_level()，
# 由本机逻辑核心数换算（低 1/4、中 1/2、高全部）。调用方不传 threads 时取中档。
MAX_THREADS = 32
CHUNK = 256 * 1024          # 单次请求读取的字节数
TIMEOUT = 20                # 单次请求超时（秒）
MAX_RETRY = 3               # 单个分块的重试次数
SAMPLE_INTERVAL = 0.4       # 速度采样间隔（秒）

# ---------------------------------------------------------------------------
# 线程档位
# ---------------------------------------------------------------------------
# 不写死"8 线程"这种数字：同一份程序要在不同电脑上跑，
# 4 核机器上开 8 线程是过载（反而更慢），32 核机器上开 8 线程又是浪费。
# 改成按**本机逻辑核心数**换算的三档：
#     低 = 1/4 核心数      中 = 1/2 核心数      高 = 全部核心数
THREAD_LEVELS = ("low", "mid", "high")
THREAD_LEVEL_LABELS = {"low": "低", "mid": "中", "high": "高"}
THREAD_LEVEL_DIVISOR = {"low": 4, "mid": 2, "high": 1}
DEFAULT_LEVEL = "mid"

STATUS_IDLE = "idle"
STATUS_PROBING = "probing"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

# 转义后的文件名非法字符（Windows 下 : * ? " < > | 不能出现在文件名里）
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# 本机核心数 → 线程档位
# ---------------------------------------------------------------------------
# 核心数 / 线程数**统一从 sysinfo 取**（唯一数据源，见文件头 import）。
# 首页 CPU 面板也读同一个函数，否则两个页面会各写各的：
# 曾经首页写「8 核 12 线程」、下载页写「12 核」，被用户当 bug 报回来。
# 别忘术语：os.cpu_count() 给的是**逻辑处理器**数，不是物理核心数。


def threads_for_level(level):
    """把档位换算成本机实际线程数。

    档位按**逻辑处理器**（硬件线程）数换算——高档的定义就是"所有线程"，
    而且下载是 IO 密集型的，超线程出来的那部分并发同样有用。
    低 = 1/4、中 = 1/2、高 = 全部；结果至少 1，最多 MAX_THREADS。
    用整除而不是四舍五入：8 线程机器上"1/4"口径最好解释。
    """
    n = cpu_threads()
    divisor = THREAD_LEVEL_DIVISOR.get(level)
    if not divisor:
        level = DEFAULT_LEVEL
        divisor = THREAD_LEVEL_DIVISOR[level]
    return int(max(1, min(n // divisor, MAX_THREADS)))


def describe_level(level):
    """给 UI 用的一句话说明。

    核心数与线程数**都要写**：只写"12 核"会让用户以为程序认错了 CPU
    （实际是 8 核 12 线程），只写"12 线程"用户又不知道中档的 6 是怎么来的。
    """
    label = THREAD_LEVEL_LABELS.get(level, "中")
    return f"本机 {describe_cpu()} · {label}档 {threads_for_level(level)} 线程"


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------
def human_bytes(n, digits=2):
    """把字节数格式化成人类可读的字符串。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    if n < 0:
        return "--"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{int(n)} B"
    return f"{n:.{digits}f} {units[i]}"


def human_speed(bps):
    """下载速度：1234567 → '1.18 MB/s'。"""
    if not bps or bps <= 0:
        return "--"
    return human_bytes(bps, 2) + "/s"


def human_time(seconds):
    """剩余时间：3725 → '1:02:05'，65 → '1:05'。"""
    if seconds is None:
        return "--"
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "--"
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "--"
    sec = int(seconds + 0.5)
    if sec < 3600:
        return f"{sec // 60}:{sec % 60:02d}"
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def sanitize_filename(name):
    """去掉 Windows 文件名里的非法字符，并压缩空白。"""
    name = unquote(name or "").strip().strip('"').strip("'")
    name = _BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name


def normalize_url(url):
    """把 URL 里的非 ASCII 字符转义成 %XX。

    浏览器会自动做这件事，但用户**手动粘贴**的链接经常是原文，
    例如 https://x.com/下载/Yuhub_安装包.zip。
    urllib 在拼请求行时按 ASCII 编码，遇到这种链接会直接抛
    `'ascii' codec can't encode characters ...`，表现为"连接失败"。
    中文域名（IDN）同理，需要转成 punycode。
    """
    url = (url or "").strip()
    if not url or url.isascii():
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return url

    # 主机名：分离 userinfo / port 后单独做 IDNA
    netloc = parts.netloc
    if not netloc.isascii():
        userinfo = ""
        hostport = netloc
        if "@" in netloc:
            userinfo, hostport = netloc.rsplit("@", 1)
            userinfo += "@"
        host, sep, port = hostport.partition(":")
        try:
            host = host.encode("idna").decode("ascii")
        except Exception:
            host = urllib.parse.quote(host)
        netloc = userinfo + host + sep + port

    # 路径与查询：保留已有转义（'%' 放进 safe），避免把 %20 二次编码成 %2520
    path = urllib.parse.quote(parts.path, safe="/%:@&=+$,;~()!*'[]")
    query = urllib.parse.quote(parts.query, safe="=&%:@+$,;~()!*'/?[]")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, parts.fragment))


def guess_filename(url, content_disposition=""):
    """从 Content-Disposition 或 URL 路径推断文件名。

    优先 Content-Disposition（支持 filename*=UTF-8''xxx 与 filename=xxx），
    其次 URL 最后一段，都拿不到就回落到 download.bin。
    """
    # 1) Content-Disposition
    if content_disposition:
        cd = content_disposition
        # RFC 5987: filename*=UTF-8''%E4%B8%AD%E6%96%87.zip
        m = re.search(r"filename\*\s*=\s*([^;]+)", cd, re.I)
        if m:
            raw = m.group(1).strip()
            if "''" in raw:
                raw = raw.split("''", 1)[1]
            name = sanitize_filename(raw)
            if name:
                return name
        m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I) or \
            re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
        if m:
            name = sanitize_filename(m.group(1))
            if name:
                return name

    # 2) URL 路径最后一段
    # 注意：解析失败要单独兜，不要让 sanitize_filename 的异常被一起吞掉
    #（曾经把 unquote 未导入的 NameError 也吞了，表现为"文件名总是回落 download.bin"）
    try:
        path = urllib.parse.urlparse(url).path
    except Exception:
        path = ""
    name = sanitize_filename(path.rsplit("/", 1)[-1]) if path else ""
    if name:
        return name
    return "download.bin"


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------
@dataclass
class ProbeResult:
    """一次 URL 探测的结果。"""

    ok: bool = False
    url: str = ""
    final_url: str = ""
    size: int = 0                 # 总字节数（0 表示服务器未给出）
    supports_range: bool = False  # 是否支持分块（206）
    filename: str = ""
    insecure: bool = False        # 是否走了「忽略证书校验」兜底
    error: str = ""
    messages: list = field(default_factory=list)


def _make_request(url, extra_headers=None, insecure=False):
    # 唯一入口处统一做一次非 ASCII 转义，所有请求（探测 / 分块 / 单线程）都受益
    url = normalize_url(url)
    headers = {
        "User-Agent": DEFAULT_UA,
        # 明确要求不压缩：否则 Content-Length 与实际解压后大小不一致，
        # 会让分块偏移全错（这是多线程下载最隐蔽的一类 bug）
        "Accept-Encoding": "identity",
        "Accept": "*/*",
        "Connection": "close",
    }
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme in ("http", "https") and p.netloc:
            # 部分站点有防盗链，带一个同源 Referer 能显著提高成功率
            headers["Referer"] = f"{p.scheme}://{p.netloc}/"
    except Exception:
        pass
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, headers=headers)
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return req, ctx
    return req, None


def _open(url, extra_headers=None, insecure=False, proxy=None):
    """打开一个下载请求。

    proxy=None（默认）：走系统代理（urllib 自动读取环境变量 / 注册表）。
    proxy="direct"：**绕过一切代理直连**——系统代理配置了但代理软件已死
    时，所有请求都会卡死在坏代理上，这是"换什么源都连不上"的另一个
    隐蔽根因，所以下载源要支持逐个尝试「系统代理 → 直连」。
    """
    req, ctx = _make_request(url, extra_headers, insecure)
    if proxy == "direct":
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        if ctx is not None:
            return opener.open(req, timeout=TIMEOUT, context=ctx)
        return opener.open(req, timeout=TIMEOUT)
    if ctx is not None:
        return urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _is_cert_error(exc):
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ssl.SSLError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(exc).upper()


def probe(url, insecure_fallback=True, proxy=None):
    """探测 URL：总大小、是否支持分块、文件名。

    用 `Range: bytes=0-0` 而不是 HEAD —— 不少站点根本不支持 HEAD，
    却对 Range 请求正常返回 206，这样一次请求就能同时拿到大小和分块能力。
    """
    res = ProbeResult(url=url)
    if not url or not url.strip():
        res.error = "链接为空"
        return res
    url = url.strip()
    res.url = url
    if not re.match(r"^https?://", url, re.I):
        res.error = "链接必须以 http:// 或 https:// 开头"
        return res

    insecure = False
    try:
        resp = _open(url, {"Range": "bytes=0-0"}, proxy=proxy)
    except urllib.error.HTTPError as exc:
        # 服务器不喜欢 Range（或要求其它条件）：退回普通 GET 再试一次
        if exc.code in (400, 416, 501):
            try:
                resp = _open(url, proxy=proxy)
            except Exception as exc2:
                res.error = f"HTTP {getattr(exc2, 'code', '?')}：{exc2}"
                return res
        else:
            res.error = f"HTTP {exc.code}：{exc.reason}"
            return res
    except Exception as exc:  # noqa: BLE001
        if insecure_fallback and _is_cert_error(exc):
            try:
                resp = _open(url, {"Range": "bytes=0-0"}, insecure=True,
                             proxy=proxy)
                insecure = True
                res.messages.append("服务器证书校验失败，已临时忽略校验继续")
            except Exception as exc2:  # noqa: BLE001
                res.error = f"连接失败：{exc2}"
                return res
        else:
            res.error = f"连接失败：{exc}"
            return res

    try:
        status = getattr(resp, "status", resp.getcode())
        res.final_url = resp.geturl() or url
        headers = resp.headers
        cd = headers.get("Content-Disposition", "") or ""

        content_range = headers.get("Content-Range", "") or ""
        if status == 206 and content_range:
            # 形如 "bytes 0-0/12345678"
            m = re.search(r"/(\d+)\s*$", content_range)
            if m:
                res.size = int(m.group(1))
            res.supports_range = True
        else:
            # 200：服务器忽略了 Range，只能老老实实整包下
            cl = headers.get("Content-Length", "")
            res.size = int(cl) if cl.isdigit() else 0
            res.supports_range = False

        # 若 206 但没给 Content-Range，用 Accept-Ranges 判断
        if not res.supports_range:
            ar = (headers.get("Accept-Ranges", "") or "").lower()
            if ar == "bytes" and res.size:
                res.supports_range = True

        res.filename = guess_filename(res.final_url or url, cd)
        res.insecure = insecure
        res.ok = True
        if not res.supports_range:
            res.messages.append("服务器不支持分块下载，将使用单线程")
        if not res.size:
            res.messages.append("服务器未返回文件大小，无法显示百分比")
    except Exception as exc:  # noqa: BLE001
        res.error = f"解析响应失败：{exc}"
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return res


# ---------------------------------------------------------------------------
# 下载任务
# ---------------------------------------------------------------------------
class DownloadTask:
    """一次多线程下载任务。

    线程模型：
      - N 个 worker 线程各负责一个字节区间，各自持有独立的文件句柄
        （`seek` 是 per-fd 的，因此并发写同一文件的不同区域是安全的）；
      - 1 个 sampler 线程每 0.4 秒采一次样，算瞬时速度与剩余时间。
    上层只需定时调用 `snapshot()` 读取状态，不必处理高频信号。
    """

    def __init__(self, url, save_path, threads=None, probe_result=None,
                 proxy=None):
        self.url = (url or "").strip()
        self.save_path = save_path
        # threads=None 时按本机核心数自动取"中档"，这样换一台电脑不用改代码
        if threads is None:
            threads = threads_for_level(DEFAULT_LEVEL)
        self.threads = max(1, min(int(threads or 1), MAX_THREADS))
        self._probe = probe_result
        # None = 系统代理；"direct" = 绕过代理直连（见 _open 注释）
        self._proxy = proxy

        # 完成前的文件先叫 xxx.part，全部写完再原子改名，
        # 避免「下到一半的文件」被误当成完整文件使用
        self.part_path = self.save_path + ".part"

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._threads = []
        self._sampler = None
        # 在途响应登记：cancel 时立即 close，让阻塞在 read()/connect() 的
        # worker **秒退**，而不是等最长 20 秒的 socket 超时（v0.8.5beta：
        # 取消是高优先级操作）
        self._resp_lock = threading.Lock()
        self._active_resps = []

        self._status = STATUS_IDLE
        self._error = ""
        self._total = 0
        self._downloaded = 0
        self._speed = 0.0
        self._blocks = []          # [(start, end, done_bytes), ...]
        self._block_count = 0
        self._single = False       # 单线程直写模式（服务器不支持分块）
        self._started_at = 0.0
        self._finished_at = 0.0
        self._messages = []
        self._samples = []         # [(t, downloaded), ...]
        self._fallback_single = False

    # ------------------------------------------------------------ 属性
    @property
    def running(self):
        with self._lock:
            return self._status in (STATUS_PROBING, STATUS_RUNNING)

    @property
    def status(self):
        with self._lock:
            return self._status

    # ------------------------------------------------------------ 启动
    def start(self):
        """探测（若尚未探测）+ 启动下载线程。返回 True 表示已启动。"""
        if self.running:
            return False
        self._cancel.clear()
        with self._lock:
            self._status = STATUS_PROBING
            self._error = ""
            self._messages = list(getattr(self._probe, "messages", []) or [])
            self._downloaded = 0
            self._speed = 0.0
            self._samples = []
            self._started_at = time.time()
            self._finished_at = 0.0

        t = threading.Thread(target=self._run, name="YuhubDownload", daemon=True)
        self._threads = [t]
        t.start()
        return True

    def cancel(self):
        """请求取消（**高优先级**）。

        ① 置位取消事件：worker 在每个分块边界检查；
        ② 立即关闭所有在途响应：阻塞在 read()/connect() 里的 worker
           会马上抛异常退出，而不是干等 20 秒 socket 超时——
           用户点取消后界面应在 1 秒内响应，而不是"卡在正在取消"。
        状态仍由 `_run` 在 worker 全部退出后统一落定。
        """
        self._cancel.set()
        with self._resp_lock:
            resps = list(self._active_resps)
        for r in resps:
            try:
                r.close()
            except Exception:
                pass

    def _track(self, resp):
        with self._resp_lock:
            self._active_resps.append(resp)

    def _untrack(self, resp):
        with self._resp_lock:
            try:
                self._active_resps.remove(resp)
            except ValueError:
                pass

    # ------------------------------------------------------------ 主流程
    def _run(self):
        try:
            if self._probe is None:
                p = probe(self.url)
                self._probe = p
                if not p.ok:
                    self._fail(p.error or "探测失败")
                    return
                with self._lock:
                    self._messages.extend(p.messages)

            p = self._probe
            total = p.size

            with self._lock:
                cancelled_early = self._cancel.is_set()
                if not cancelled_early:
                    self._status = STATUS_RUNNING
                    self._total = total
            if cancelled_early:
                self._cleanup_part()
                with self._lock:
                    self._status = STATUS_CANCELLED
                    self._finished_at = time.time()
                return

            if not p.supports_range or total <= 0:
                # 服务器不给大小 / 不支持分块 → 单线程流式写
                self._single = True
                self._block_count = 1
                with self._lock:
                    # 必须是 list（可变），worker 要在原地累加已完成字节数
                    self._blocks = [[0, total if total > 0 else -1, 0]]
                self._download_single()
            else:
                self._download_multi(total)

            if self._cancel.is_set():
                # 顺序很重要：**先清临时文件、再落状态**。
                # 反过来的话，状态一变成「已取消」，UI/调用方立刻就能看到，
                # 但此时磁盘上的 .part 还没删掉（删除带重试等待），
                # 表现为"取消了但文件还在"，若紧接着往同一路径再下一次还会撞车。
                self._cleanup_part()
                with self._lock:
                    self._status = STATUS_CANCELLED
                    self._finished_at = time.time()
                return

            # 子流程（预分配失败 / 连接中断等）已经报错，别再往下走 _finish
            if self.status == STATUS_ERROR:
                return

            self._finish()

        except Exception as exc:  # noqa: BLE001
            self._fail(f"下载异常：{exc}")

    # ------------------------------------------------------------ 多线程
    def _download_multi(self, total):
        n = min(self.threads, max(1, total // (64 * 1024)))  # 文件太小就别开那么多线程
        n = int(max(1, n))
        self._block_count = n

        # 按总大小均匀切块（最后一块吃掉余数）
        size = total // n
        blocks = []
        for i in range(n):
            start = i * size
            end = (start + size - 1) if i < n - 1 else (total - 1)
            blocks.append([start, end, 0])
        with self._lock:
            self._blocks = blocks

        # 预分配成最终大小：worker 才能从任意偏移写入
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.part_path)) or ".", exist_ok=True)
            with open(self.part_path, "wb") as f:
                f.truncate(total)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"无法创建目标文件：{exc}")
            return

        self._start_sampler()
        workers = []
        for i in range(n):
            t = threading.Thread(
                target=self._worker, args=(i, blocks[i][0], blocks[i][1]),
                name=f"YuhubDl-{i}", daemon=True,
            )
            workers.append(t)
            t.start()
        with self._lock:
            self._threads.extend(workers)
        for t in workers:
            t.join()
        self._stop_sampler()

    def _worker(self, idx, start, end):
        """抓取 [start, end] 这一段（闭区间）。"""
        f = None
        try:
            f = open(self.part_path, "r+b")
        except Exception:  # noqa: BLE001
            return
        pos = start
        retry = 0
        try:
            while pos <= end:
                if self._cancel.is_set():
                    return
                req_end = min(pos + CHUNK - 1, end)
                want = req_end - pos + 1
                try:
                    resp = _open(
                        self.url,
                        {"Range": f"bytes={pos}-{req_end}"},
                        insecure=getattr(self._probe, "insecure", False),
                        proxy=self._proxy,
                    )
                    self._track(resp)
                    try:
                        data = resp.read(want)
                    finally:
                        try:
                            resp.close()
                        except Exception:
                            pass
                        self._untrack(resp)
                except urllib.error.HTTPError as exc:
                    if exc.code == 416:      # 区间越界 = 这段已经下完
                        break
                    retry += 1
                    if retry > MAX_RETRY:
                        self._note(f"分块 {idx + 1} 失败：HTTP {exc.code}")
                        return
                    time.sleep(min(1.5 * retry, 5.0))
                    continue
                except Exception:  # noqa: BLE001
                    retry += 1
                    if retry > MAX_RETRY:
                        self._note(f"分块 {idx + 1} 多次重试后仍失败")
                        return
                    time.sleep(min(1.5 * retry, 5.0))
                    continue

                if not data:
                    break                    # 服务器提前收尾
                retry = 0
                try:
                    f.seek(pos)
                    f.write(data)
                except Exception:  # noqa: BLE001
                    return
                pos += len(data)
                with self._lock:
                    self._downloaded += len(data)
                    if idx < len(self._blocks):
                        self._blocks[idx][2] += len(data)
        finally:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass

    # ------------------------------------------------------------ 单线程
    def _download_single(self):
        self._start_sampler()
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.part_path)) or ".", exist_ok=True)
            resp = _open(self.url, insecure=getattr(self._probe, "insecure", False),
                         proxy=self._proxy)
            self._track(resp)
        except Exception as exc:  # noqa: BLE001
            self._stop_sampler()
            self._fail(f"连接失败：{exc}")
            return
        retry = 0
        try:
            with open(self.part_path, "wb") as f:
                while True:
                    if self._cancel.is_set():
                        return
                    try:
                        data = resp.read(CHUNK)
                    except Exception:  # noqa: BLE001
                        retry += 1
                        if retry > MAX_RETRY:
                            self._note("多次重试后连接中断")
                            break
                        time.sleep(min(1.5 * retry, 5.0))
                        continue
                    if not data:
                        break
                    retry = 0
                    f.write(data)
                    with self._lock:
                        self._downloaded += len(data)
                        if self._blocks:
                            self._blocks[0][2] += len(data)
        finally:
            try:
                resp.close()
            except Exception:
                pass
            self._untrack(resp)
            self._stop_sampler()

    # ------------------------------------------------------------ 采样
    def _start_sampler(self):
        # 先垫一个 (起始时刻, 0) 采样点：否则要等满两个采样周期（0.8 秒）
        # 才算出速度，短下载就永远显示不出速度
        with self._lock:
            self._samples = [(time.time(), self._downloaded)]

        def _loop():
            while not self._cancel.is_set():
                with self._lock:
                    if self._status not in (STATUS_PROBING, STATUS_RUNNING):
                        return
                    self._samples.append((time.time(), self._downloaded))
                    # 只保留最近 8 个样本（约 3 秒窗口）
                    if len(self._samples) > 8:
                        self._samples = self._samples[-8:]
                    self._speed = self._calc_speed()
                time.sleep(SAMPLE_INTERVAL)

        self._sampler = threading.Thread(target=_loop, name="YuhubDlSpeed", daemon=True)
        self._sampler.start()

    def _stop_sampler(self):
        # sampler 靠状态自行退出；这里只做一次收尾计算
        with self._lock:
            self._speed = self._calc_speed()

    def _calc_speed(self):
        """用最近一个采样窗口算瞬时速度（必须已持锁）。"""
        if len(self._samples) < 2:
            return 0.0
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        dt = t1 - t0
        if dt <= 0.05:
            return 0.0
        return max(0.0, (b1 - b0) / dt)

    # ------------------------------------------------------------ 收尾
    def _note(self, text):
        with self._lock:
            self._messages.append(text)

    def _finish(self):
        total = self._total
        got = os.path.getsize(self.part_path) if os.path.exists(self.part_path) else 0
        if total > 0 and got != total:
            self._fail(f"文件大小不符：期望 {human_bytes(total)}，实际 {human_bytes(got)}")
            return
        try:
            os.replace(self.part_path, self.save_path)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"无法保存到目标路径：{exc}")
            return
        with self._lock:
            self._downloaded = got
            self._total = total or got
            self._status = STATUS_DONE
            self._finished_at = time.time()

    def _cleanup_part(self):
        """删除半成品文件（取消 / 失败时调用）。

        删除失败必须**记下来**，不能静默吞掉——否则表现为
        "取消了但 .part 还留在磁盘上"，而且完全查不出原因
        （Windows 上只要还有句柄没关就会 PermissionError）。
        """
        if not os.path.exists(self.part_path):
            return True
        last = None
        for attempt in range(6):
            try:
                os.remove(self.part_path)
                return True
            except Exception as exc:  # noqa: BLE001
                last = exc
                # 给还没关干净的句柄一点时间释放
                time.sleep(0.15 * (attempt + 1))
        with self._lock:
            self._messages.append(f"临时文件清理失败：{last}")
        return False

    def _fail(self, message):
        # 与取消路径同理：先把磁盘收拾干净，再让状态对外可见
        self._cleanup_part()
        with self._lock:
            self._status = STATUS_ERROR
            self._error = message
            self._finished_at = time.time()

    # ------------------------------------------------------------ 快照
    def snapshot(self):
        """给 UI 用的线程安全状态快照。"""
        with self._lock:
            total = self._total
            done = self._downloaded
            status = self._status
            speed = self._speed
            blocks = list(self._blocks)
            single = self._single
            error = self._error
            messages = list(self._messages)
            started = self._started_at
            finished = self._finished_at

        percent = (done / total * 100.0) if total > 0 else 0.0
        percent = max(0.0, min(100.0, percent))

        # 每个分块的完成度（0~1），供 UI 画方块组
        fractions = []
        for start, end, bdone in blocks:
            span = (end - start + 1) if end >= start else 0
            fractions.append(min(1.0, bdone / span) if span > 0 else 0.0)
            if span <= 0:
                fractions[-1] = 1.0 if status == STATUS_DONE else 0.0

        elapsed = (finished or time.time()) - started if started else 0.0
        # 平均速度：瞬时速度要有两个采样点才有值，刚开始时用平均值兜底，
        # 下载结束后也靠它给用户一个"这次到底多快"的结论
        avg_speed = (done / elapsed) if (elapsed > 0.05 and done > 0) else 0.0

        eta = None
        if status == STATUS_RUNNING and total > 0:
            ref = speed if speed > 0 else avg_speed
            if ref > 0:
                eta = max(0.0, (total - done) / ref)

        return {
            "status": status,
            "error": error,
            "total": total,
            "downloaded": done,
            "percent": percent,
            "speed": speed if status == STATUS_RUNNING else 0.0,
            "avg_speed": avg_speed,
            "eta": eta,
            "elapsed": max(0.0, elapsed),
            "blocks": fractions,
            "block_count": len(fractions),
            "single_thread": single,
            "supports_range": bool(getattr(self._probe, "supports_range", False)),
            "filename": getattr(self._probe, "filename", "") or os.path.basename(self.save_path),
            "messages": messages,
            "running": status in (STATUS_PROBING, STATUS_RUNNING),
            # 已发出取消请求但 worker 尚未退出：UI 用这个显示「正在取消…」
            "cancelling": self._cancel.is_set()
            and status in (STATUS_PROBING, STATUS_RUNNING),
        }


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------
def download(url, save_path, threads=None, on_progress=None,
             cancel=None):
    """同步下载（阻塞直到完成），返回 (ok, message)。

    on_progress(snapshot) 会被周期性调用；cancel() 返回 True 表示请求中止。
    主要用于命令行测试。
    """
    task = DownloadTask(url, save_path, threads=threads)
    task.start()
    asked = False
    while task.running:
        snap = task.snapshot()
        if on_progress:
            try:
                on_progress(snap)
            except Exception:
                pass
        if cancel and not asked and cancel():
            asked = True
            task.cancel()
        time.sleep(0.2)
    snap = task.snapshot()
    if snap["status"] == STATUS_DONE:
        return True, f"完成，共 {human_bytes(snap['downloaded'])}"
    if snap["status"] == STATUS_CANCELLED:
        return False, "已取消"
    return False, snap["error"] or "下载失败"
