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
import random
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
MAX_RETRY = 3               # 单块单轮重试次数（第 1 轮用这个值，见 RETRY_SCHEDULE）
SAMPLE_INTERVAL = 0.4       # 速度采样间隔（秒）

# ---- 分块补漏（v0.8.15beta）---------------------------------------------
# 旧版所有分块只跑一轮：某个块重试 3 次仍失败就**静默放弃**，
# 而收尾校验看的是预分配后的文件大小（零填充，恒等于总大小），
# 于是"少了 7 个块"的残包照样被判定为下载成功。
# 现在改成多轮：第 1 轮全并发，之后逐轮缩小并发 + 加长退避，
# 把被限流/断连的那几块捞回来；捞不回来就明确报错，绝不产出坏文件。
MAX_ROUNDS = 4
RETRY_SCHEDULE = (MAX_RETRY, 5, 7, 9)   # 每轮单块重试次数
ROUND_PAUSE = (0.0, 1.5, 4.0, 8.0)      # 轮间等待（秒）
SUPPLEMENT_CONCURRENCY = 4              # 补漏轮次的并发上限（别再压垮线路）

# 取消 / 卡死的响应上限：worker 里任何阻塞（DNS 挂起、TCP 建连）都可能
# 永不返回，`join()` 无限等会让任务状态永远落不了定——用户看到的就是
# "点了取消，界面一直停在「正在取消…」"。
CANCEL_GRACE = 3.0          # 取消后给 worker 自己退出的宽限期（秒）
STALL_TIMEOUT = 45.0        # 看门狗：这么久一个字节没动就判定连接僵死

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
# 小工具
# ---------------------------------------------------------------------------
def _batches(items, size):
    """把列表切成每片最多 size 个（补漏轮次按批跑，控制并发）。"""
    size = max(1, int(size or 1))
    seq = list(items)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _backoff(retry, base=0.6, cap=8.0):
    """指数退避 + 抖动。

    纯指数退避会让同一个批次里失败的 worker 在同一时刻集体重连，
    对已经因为并发过高而掐连接的服务器等于又打了一轮同步攻击；
    加 ±30% 抖动把它们错开。上限 8 秒，避免"重试太久像卡死"。
    """
    raw = min(base * (2 ** max(0, retry - 1)), cap)
    return max(0.05, raw * (0.7 + random.random() * 0.6))


def _open_rw(path):
    """以读写方式打开文件；Windows 下额外允许"被删除 / 被改名"。

    为什么不直接用 `open(path, "r+b")`：
    Windows 上 Python 的 `open()` 共享模式是 READ|WRITE，**不含 DELETE**，
    只要还有一个句柄没关，这个文件就既删不掉、也改不了名。
    取消下载时 worker 可能卡在 DNS 解析里几十秒（socket 超时管不住 DNS），
    期间它攥着 `.part` 的句柄，于是：
      * `_cleanup_part` 删不掉 → 半成品赖在磁盘上；
      * 紧接着重新下载，收尾的 `os.replace` 报
        "另一个程序正在使用此文件，进程无法访问"（WinError 32）。
    加上 FILE_SHARE_DELETE 之后，句柄还在也不妨碍改名/删除
    （Windows 会把它标记为"待删除"，名字立刻腾出来）。
    失败则退化到普通打开：功能不受影响，只是极端情况下要多等一会儿。
    """
    if os.name != "nt":
        return open(path, "r+b")
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE,
        ]
        GENERIC_RW = 0x80000000 | 0x40000000
        SHARE_ALL = 0x1 | 0x2 | 0x4        # READ | WRITE | DELETE
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 3, 0x80
        INVALID = 0xFFFFFFFFFFFFFFFF

        h = k32.CreateFileW(path, GENERIC_RW, SHARE_ALL, None,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if not h or h == INVALID:
            raise OSError(ctypes.get_last_error(), "CreateFileW 失败")
        fd = msvcrt.open_osfhandle(h, os.O_RDWR | os.O_BINARY)
        return os.fdopen(fd, "r+b")
    except Exception:  # noqa: BLE001
        return open(path, "r+b")


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
        # 当前轮次的单块重试预算（_download_multi 每轮设置，worker 读取）
        self._retry_budget = MAX_RETRY
        # 看门狗 / 放弃等待（见 _await）
        self._last_progress = time.time()
        self._cancel_at = 0.0
        self._abandoned = False

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
            self._last_progress = time.time()
        self._cancel_at = 0.0
        self._abandoned = False
        self._retry_budget = MAX_RETRY

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
        ③ 记下取消时刻：`_await` 用它做宽限期兜底——万一有 worker
           卡在 DNS 解析这类"关不掉的阻塞"里，到点就放弃等待、
           先让状态落定（见 CANCEL_GRACE）。
        状态仍由 `_run` 在 worker 全部退出后统一落定。
        """
        if not self._cancel_at:
            self._cancel_at = time.monotonic()
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

            if self._cancel.is_set() or self._abandoned:
                # 顺序很重要：**先把状态落定**，让 UI 立刻响应，
                # 再谈磁盘清理。
                # 反过来的话，清理要重试好几轮（句柄没释放时每次删失败
                # 都要等），用户点完取消会眼睁睁看着界面卡住不还。
                # 若刚放弃等待过僵尸线程，文件句柄可能还攥在它手里，
                # 清理交给 _reap 在后台完成。
                cancelled = self._cancel.is_set()
                if not self._abandoned:
                    self._cleanup_part()
                with self._lock:
                    self._status = (STATUS_CANCELLED if cancelled
                                    else STATUS_ERROR)
                    if not cancelled:
                        self._error = "下载停滞：长时间没有收到数据，已中止"
                    self._finished_at = time.time()
                return

            # 子流程（预分配失败 / 连接中断等）已经报错，别再往下走 _finish
            if self.status == STATUS_ERROR:
                return

            # ---- 完整性闸门（v0.8.15beta 修复的核心）----
            # 不能只看文件大小：`.part` 是按总大小**预分配**的（零填充），
            # 哪怕一个字节都没下到，大小也恒等于 total。
            # 旧版据此判定成功，把满是空洞的残包交给替换器，
            # 用户看到的就是"提示下载完成，但程序打不开/报错"。
            missing = self._missing_indexes()
            if missing:
                total_blocks = len(self._blocks) or 1
                self._fail(
                    "下载不完整：%d/%d 个分块多次重试后仍失败，"
                    "已丢弃不完整文件，请重试或更换下载线路"
                    % (len(missing), total_blocks))
                return

            self._finish()

        except Exception as exc:  # noqa: BLE001
            self._fail(f"下载异常：{exc}")

    # ------------------------------------------------------------ 多线程
    def _download_multi(self, total):
        """分块下载：**多轮补漏**（v0.8.15beta）。

        第 1 轮按线程数全并发；之后每轮只看"还没下完的块"，
        且逐轮缩小并发、加长退避——国内直连 GitHub 常见的情况是
        首轮并发一上去就被限流/掐断，若干块集中失败；这时候用更少的
        连接慢慢补，成功率远高于原地死磕。

        任何一轮补完就收工；跑满 MAX_ROUNDS 仍缺块 → 由 _run 的
        完整性闸门报错。**绝不**把缺块的残包当成功品。
        """
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
        try:
            for rnd in range(MAX_ROUNDS):
                if self._cancel.is_set() or self._abandoned:
                    return
                todo = self._missing_indexes()
                if not todo:
                    return
                if rnd:
                    self._note("第 %d 轮补下：%d/%d 个分块需要重试"
                               % (rnd + 1, len(todo), n))
                    if not self._sleep_cancellable(ROUND_PAUSE[rnd]):
                        return
                self._retry_budget = RETRY_SCHEDULE[rnd]
                # 首轮全并发；补漏轮次限制并发，别再压垮本就不稳的线路
                cap = len(todo) if rnd == 0 else max(
                    1, min(SUPPLEMENT_CONCURRENCY, len(todo)))
                for batch in _batches(todo, cap):
                    if self._cancel.is_set() or self._abandoned:
                        return
                    workers = []
                    for i in batch:
                        t = threading.Thread(
                            target=self._worker,
                            args=(i, blocks[i][0], blocks[i][1]),
                            name=f"YuhubDl-{i}", daemon=True,
                        )
                        workers.append(t)
                        t.start()
                    with self._lock:
                        self._threads.extend(workers)
                    if not self._await(workers):
                        return
                if not self._missing_indexes():
                    return
        finally:
            self._stop_sampler()

    # ------------------------------------------------------------ 分块进度
    def _missing_indexes(self):
        """还没下满的分块下标。

        判据是**每块实际写入的字节数**，不是文件大小——
        预分配过的 .part 大小恒等于总大小，拿它做判断永远"完整"。
        """
        with self._lock:
            blocks = list(self._blocks)
        out = []
        for i, (start, end, done) in enumerate(blocks):
            span = (end - start + 1) if end >= start else 0
            if span > 0 and done != span:
                out.append(i)
        return out

    def _sleep_cancellable(self, seconds):
        """可被取消打断的退避睡眠。

        旧版直接 `time.sleep(5)`：用户点取消后，正在退避的 worker
        还要睡满 5 秒才轮到检查取消标志——"取消不灵"的体感多半来自这里。
        切成 0.1 秒小片，取消最多 0.1 秒生效。
        """
        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self._cancel.is_set() or self._abandoned:
                return False
            left = end - time.monotonic()
            if left <= 0:
                return True
            time.sleep(min(0.1, left))

    def _await(self, workers):
        """等 worker 退出，但**不无限等**。

        worker 里的阻塞调用（DNS 解析挂起、TCP 建连被黑洞）可能永不返回。
        无限 `join()` 的后果不是"慢"，是**任务状态永远落不了定**：
        UI 停在「正在取消…」，用户怎么点都没反应，只能杀进程。
        两种情况下放弃等待，先把状态交出去，僵尸线程交给 _reap 收尾：
          ① 已取消且超过 CANCEL_GRACE 秒还没退干净；
          ② 连续 STALL_TIMEOUT 秒没有任何字节进展（连接僵死）。
        """
        while True:
            alive = [t for t in workers if t.is_alive()]
            if not alive:
                return True
            if self._cancel.is_set() and self._cancel_at:
                if time.monotonic() - self._cancel_at >= CANCEL_GRACE:
                    self._abandon(alive, "已取消但线程未响应")
                    return False
            elif time.monotonic() - self._last_progress >= STALL_TIMEOUT:
                self._abandon(alive, "长时间没有数据")
                return False
            time.sleep(0.1)

    def _abandon(self, alive, why):
        """放弃等待这些卡死的 worker，并安排后台收尾。"""
        self._abandoned = True
        self._note("有 %d 个下载线程（%s）未在限时内退出，已放弃等待"
                   % (len(alive), why))
        threading.Thread(target=self._reap, args=(list(alive),),
                         name="YuhubDlReap", daemon=True).start()

    def _reap(self, workers):
        """后台收尾：等僵尸线程真退出后，把半成品文件删干净。

        不能在这条路径上做清理——那正是要躲开的阻塞。
        """
        for t in workers:
            try:
                t.join()
            except Exception:  # noqa: BLE001
                pass
        self._cleanup_part()

    def _worker(self, idx, start, end):
        """抓取 [start, end] 这一段（闭区间）。

        **支持续下**：从本块已有的写入进度处接着下。
        补漏轮次靠这个特性避免把已经拿到的部分重下一遍（进度条也不会回退）。
        """
        f = None
        try:
            # 用 _open_rw 而不是 open(..., "r+b")：允许句柄未关时改名/删除，
            # 否则僵尸 worker 会把这半成品文件"锁死"（见 _open_rw 注释）
            f = _open_rw(self.part_path)
        except Exception:  # noqa: BLE001
            return
        span = end - start + 1
        with self._lock:
            have = int(self._blocks[idx][2]) if idx < len(self._blocks) else 0
        pos = start + max(0, min(have, span))
        retry = 0
        budget = max(1, int(self._retry_budget))
        try:
            while pos <= end:
                if self._cancel.is_set() or self._abandoned:
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
                    if retry > budget:
                        self._note(f"分块 {idx + 1} 失败：HTTP {exc.code}"
                                   f"（已重试 {budget} 次）")
                        return
                    if not self._sleep_cancellable(_backoff(retry)):
                        return
                    continue
                except Exception:  # noqa: BLE001
                    retry += 1
                    if retry > budget:
                        self._note(f"分块 {idx + 1} 重试 {budget} 次后仍失败")
                        return
                    if not self._sleep_cancellable(_backoff(retry)):
                        return
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
                    # 看门狗心跳：有字节落地就说明连接还活着
                    self._last_progress = time.time()
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
        budget = max(1, int(self._retry_budget))
        try:
            with open(self.part_path, "wb") as f:
                while True:
                    if self._cancel.is_set() or self._abandoned:
                        return
                    try:
                        data = resp.read(CHUNK)
                    except Exception:  # noqa: BLE001
                        retry += 1
                        if retry > budget:
                            self._note("多次重试后连接中断，下载未完成")
                            self._fail("连接中断：重试 %d 次后仍未恢复" % budget)
                            return
                        if not self._sleep_cancellable(_backoff(retry)):
                            return
                        continue
                    if not data:
                        break
                    retry = 0
                    f.write(data)
                    with self._lock:
                        self._downloaded += len(data)
                        if self._blocks:
                            self._blocks[0][2] += len(data)
                        self._last_progress = time.time()
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
        with self._lock:
            got = self._downloaded
        if not os.path.exists(self.part_path):
            self._fail("临时文件丢失，下载未完成")
            return
        # 用**实际写入的字节数**校验，而不是 os.path.getsize()。
        # 分块模式下 .part 是按总大小预分配的（零填充），大小恒等于 total，
        # 拿它做判断等于没有校验——旧版正是靠这个"永远通过"的检查，
        # 把缺了 7 个分块的残包判定为"下载完成"。
        # 分块的完整性由 _run 的闸门（_missing_indexes）保证，
        # 这里再对总量兜一次底。
        if total > 0 and got != total:
            self._fail("下载不完整：期望 %s，实际 %s"
                       % (human_bytes(total), human_bytes(got)))
            return
        err = self._replace_with_retry(self.part_path, self.save_path)
        if err is not None:
            self._fail(f"无法保存到目标路径：{err}")
            return
        with self._lock:
            self._total = total or got
            self._status = STATUS_DONE
            self._finished_at = time.time()

    def _replace_with_retry(self, src, dst, wait=8.0, step=0.2):
        """把临时文件挪到目标路径，遇到"被占用"就退避重试。

        正常取消时 worker 秒退，句柄立刻释放，第一次就能成。
        只有在 worker 卡死（DNS 挂起之类）时才会撞上 WinError 32——
        这时等它退出比直接报错好：等不到再如实报错，用户至少知道
        是被什么挡住了，而不是看到一句看不出所以然的"无法保存"。

        返回 None = 成功，否则返回最后的异常。
        """
        end = time.monotonic() + max(0.0, wait)
        last = None
        while True:
            try:
                os.replace(src, dst)
                return None
            except OSError as exc:
                last = exc
                # 5 = 拒绝访问，32 = 文件被占用；其它错误重试也没意义
                if getattr(exc, "winerror", None) not in (5, 32):
                    return exc
                if time.monotonic() >= end or self._cancel.is_set():
                    return exc
                time.sleep(step)

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
        # 最后一搏：删不掉通常是还有僵尸线程攥着句柄。
        # 改名虽然仍占磁盘，但能保证下次往同一路径下载不撞车
        #（否则下次 "wb" 打开就撞上被占用的文件，报的错还看不出原因）。
        try:
            bad = self.part_path + ".bad"
            os.replace(self.part_path, bad)
            with self._lock:
                self._messages.append(
                    "半成品文件被占用，已改名保留：%s" % os.path.basename(bad))
            return False
        except Exception:
            pass
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
