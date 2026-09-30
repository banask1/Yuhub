"""Yuhub 自动更新核心模块（纯逻辑，不依赖 Qt，可独立测试）。

设计要点（详见 docs/自动更新方案.md）：

  * 更新源用 **GitHub Releases**：tag 当版本号、body 当更新说明、assets 当下载地址。
  * 替换手法用 **两步改名**，不是覆盖写 —— Windows 上运行中的 exe
    禁止改写内容（映像节），但**改目录项（改名）是允许的**。
  * 替换器不是独立 exe，而是**把自己拷一份到临时目录**，
    用 `Yuhub.exe --apply-update <base64-json>` 早返回模式启动。
    原因：替换器不能是它要替换的那个文件（自己锁着自己）。

自检入口见 update_selftest.py，用本地 http.server 伪装更新源跑全流程。
"""

import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 常量

# GitHub API 强制要求 User-Agent，不带会被 403
_UA = "Yuhub-Updater"

# 仓库坐标（更新源必须是公开仓库——把 token 放进客户端 = 泄露）
REPO_OWNER = "banask1"
REPO_NAME = "Yuhub"

# 单次网络请求超时（秒）。静默检查绝不能拖慢启动。
HTTP_TIMEOUT = 8

# 下载完成后、启动新 exe 后的观察窗口：这么久内退出且非 0 → 判定新版有问题
NEW_EXE_GRACE = 5.0

# 等旧进程退出的上限，超时就强杀
WAIT_OLD_PID_TIMEOUT = 30.0

_CREATE_NO_WINDOW = 0x08000000


# ================================================================ 版本号

def parse_version(s):
    """把版本字符串解析成可比较的元组。

    规则（宽松，能容纳常见的几种写法）：
      "v0.8beta"   -> (0, 8, -1)       # beta < 正式，用 -1 占位
      "0.8.1"      -> (0, 8, 1)
      "v1.0"       -> (1, 0)
      "1.0.0-rc1"  -> (1, 0, 0, -1)
      ""           -> ()

    尾部数字个数不齐时短的补 0 比较（见 is_newer）。
    """
    if not s:
        return ()
    t = str(s).strip().lstrip("vV")
    # 预发布后缀（beta / rc / alpha / preview）→ 标记为 -1，低于同号正式版
    pre = -1 if re.search(r"(alpha|beta|rc|preview|dev)", t, re.I) else 0
    out = []
    for part in re.split(r"[.\-_+]", t):
        m = re.match(r"^(\d+)", part.strip())
        if m:
            out.append(int(m.group(1)))
    if not out:
        return ()
    # 有预发布标记时，末尾追加 -1 让 (0,8,-1) < (0,8,0)
    if pre == -1:
        out.append(-1)
    return tuple(out)


def is_newer(remote, local):
    """remote 是否比 local 新。

    长度不齐时补 0：("0.8", ) vs ("0.8", 0) 视为相等。
    """
    r = parse_version(remote)
    l = parse_version(local)
    if not r:
        return False
    if not l:
        return True
    n = max(len(r), len(l))
    r = r + (0,) * (n - len(r))
    l = l + (0,) * (n - len(l))
    return r > l


# ================================================================ 清单获取

class ReleaseInfo:
    """一次 Release 的解析结果。"""

    def __init__(self, tag, name, notes, asset_name, asset_url,
                 asset_size=0, sha256="", published=""):
        self.tag = tag                  # "v0.8beta"
        self.name = name                # "Yuhub v0.8beta"
        self.notes = notes              # 更新说明（Markdown）
        self.asset_name = asset_name    # "Yuhub.exe"
        self.asset_url = asset_url      # 直链
        self.asset_size = int(asset_size or 0)
        self.sha256 = (sha256 or "").lower().replace("sha256:", "").strip()
        self.published = published

    @property
    def version(self):
        return self.tag

    def __repr__(self):
        return "<ReleaseInfo %s asset=%s size=%d>" % (
            self.tag, self.asset_name, self.asset_size)

    def to_dict(self):
        return {
            "tag": self.tag, "name": self.name, "notes": self.notes,
            "asset_name": self.asset_name, "asset_url": self.asset_url,
            "asset_size": self.asset_size, "sha256": self.sha256,
            "published": self.published,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("tag", ""), d.get("name", ""), d.get("notes", ""),
                   d.get("asset_name", ""), d.get("asset_url", ""),
                   d.get("asset_size", 0), d.get("sha256", ""),
                   d.get("published", ""))


def _http_json(url, timeout=HTTP_TIMEOUT):
    """GET 一个 JSON 接口，返回解析后的 dict。失败抛异常。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def pick_asset(assets, prefer=("Yuhub.exe", ".exe")):
    """从 assets 里挑出主程序。

    优先精确匹配 prefer[0]，否则退到第一个 .exe，再否则退到第一个 asset。
    """
    if not assets:
        return None
    for want in prefer:
        for a in assets:
            if (a.get("name") or "").lower() == want.lower():
                return a
    for a in assets:
        if (a.get("name") or "").lower().endswith(".exe"):
            return a
    return assets[0]


def parse_release(d):
    """把 GitHub releases/latest 或 releases 的单个对象解析成 ReleaseInfo。

    也接受自建的 version.json 结构（字段名不同，做兼容）。
    """
    # 兼容自建清单 {"version":..,"url":..,"sha256":..,"notes":..}
    if "tag_name" not in d and "version" in d:
        return ReleaseInfo(
            tag=d.get("version", ""), name=d.get("name") or d.get("version", ""),
            notes=d.get("notes", ""), asset_name=os.path.basename(d.get("url", "")),
            asset_url=d.get("url", ""), asset_size=d.get("size", 0),
            sha256=d.get("sha256", ""), published=d.get("published", ""),
        )

    assets = d.get("assets") or []
    a = pick_asset(assets) or {}
    digest = a.get("digest") or ""
    return ReleaseInfo(
        tag=d.get("tag_name", ""),
        name=d.get("name") or d.get("tag_name", ""),
        notes=d.get("body", "") or "",
        asset_name=a.get("name", ""),
        asset_url=a.get("browser_download_url", ""),
        asset_size=a.get("size", 0),
        sha256=digest,
        published=d.get("published_at", "") or "",
    )


def fetch_release(owner=REPO_OWNER, repo=REPO_NAME, timeout=HTTP_TIMEOUT,
                  api_base="https://api.github.com"):
    """取最新 Release。返回 (ReleaseInfo | None, error_str)。

    ⚠️ 关键陷阱：GitHub 的 `releases/latest` **会跳过 prerelease 和 draft**。
    对一个还在 beta 阶段的项目（tag 就叫 v0.8beta），如果发布时勾了
    "pre-release"，这个接口会直接 404 —— 客户端就永远检查不到更新。

    所以策略是：先试 `latest`，拿不到再退到 releases 列表，
    取**第一个非 draft** 的版本（预发布也认，因为本项目当前正处在 beta 阶段）。
    """
    base = api_base.rstrip("/")
    latest_url = "%s/repos/%s/%s/releases/latest" % (base, owner, repo)

    # ---- ① 优先 latest（正式版走得通，语义最准）----
    try:
        data = _http_json(latest_url, timeout=timeout)
        if data.get("tag_name"):
            return parse_release(data), ""
    except urllib.error.HTTPError as e:
        if e.code not in (404, 403):
            return None, "服务返回 HTTP %d" % e.code
        if e.code == 403:
            return None, "请求过于频繁或网络受限（HTTP 403）"
        # 404：要么没有任何 Release，要么只有 prerelease —— 往下走列表
    except urllib.error.URLError as e:
        return None, "网络不可达：%s" % (getattr(e, "reason", e),)
    except Exception as e:
        return None, "检查失败：%s" % (e,)

    # ---- ② 退到列表：取第一个非 draft 的版本 ----
    list_url = "%s/repos/%s/%s/releases?per_page=10" % (base, owner, repo)
    try:
        arr = _http_json(list_url, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, ""          # 仓库里一个 Release 都没有
        return None, "服务返回 HTTP %d" % e.code
    except urllib.error.URLError as e:
        return None, "网络不可达：%s" % (getattr(e, "reason", e),)
    except Exception as e:
        return None, "检查失败：%s" % (e,)

    if not isinstance(arr, list) or not arr:
        return None, ""

    for item in arr:
        if item.get("draft"):
            continue                  # 草稿不该被客户端看到
        if not item.get("tag_name"):
            continue
        return parse_release(item), ""

    return None, ""


def github_latest(owner=REPO_OWNER, repo=REPO_NAME, timeout=HTTP_TIMEOUT,
                  api_base="https://api.github.com"):
    """取最新 Release。拿不到返回 None（静默失败，不抛）。"""
    info, _ = fetch_release(owner, repo, timeout, api_base)
    return info


def check_for_update(current_version, owner=REPO_OWNER, repo=REPO_NAME,
                     timeout=HTTP_TIMEOUT, api_base="https://api.github.com"):
    """检查是否有新版本。

    返回 (ReleaseInfo | None, error_str)。
    - 有新版： (info, "")
    - 已最新： (None, "")
    - 出错：   (None, "原因")
    """
    info, err = fetch_release(owner, repo, timeout, api_base)
    if err:
        return None, err
    if info is None:
        return None, ""              # 没有 Release，视为已最新

    if not info.asset_url:
        return None, "该版本没有可下载的文件"
    if is_newer(info.tag, current_version):
        return info, ""
    return None, ""


# ================================================================ 下载与校验

def sha256_file(path, chunk=1024 * 1024):
    """流式计算文件 sha256（60MB 文件约 0.2 秒，不必开线程）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_sha256(path, expect):
    """校验文件 sha256。expect 为空时返回 True（无从校验，放行）。

    ⚠️ 安全说明：拿不到摘要意味着无法防御中间人替换下载内容。
    GitHub Releases 的 digest 字段通常可用；确实拿不到时，
    这里选择放行但把情况记进日志，而不是拒绝更新（否则老版本
    客户端会永远无法升级）。
    """
    if not expect:
        return True
    try:
        return sha256_file(path).lower() == str(expect).lower().replace("sha256:", "")
    except OSError:
        return False


def update_dir():
    """更新工作目录：%LOCALAPPDATA%\\Yuhub\\update。

    放 LOCALAPPDATA 而不是 exe 同目录 —— 后者可能在 Program Files（不可写）。
    """
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    d = os.path.join(base, "Yuhub", "update")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def package_path(info):
    """更新包在本地的工作路径（带版本号，便于识别）。"""
    safe = re.sub(r"[^\w.\-]", "_", info.tag or "latest")
    return os.path.join(update_dir(), "Yuhub-%s.exe" % safe)


def _url_allowed(url):
    """更新地址是否允许下载。

    默认**只允许 https**（防中间人把更新包降级到明文）。
    唯一例外：`http://127.0.0.1` / `localhost` 回环地址——
    自检用本地 http.server 伪装更新源时必须走这条路，
    而回环流量不出本机，不存在被劫持的问题。
    """
    u = (url or "").lower()
    if u.startswith("https://"):
        return True
    if u.startswith("http://127.0.0.1") or u.startswith("http://localhost"):
        return True
    return False


# ---------------------------------------------------------------- 下载源与探测
# 国内直连 github.com（下载还要再跳 objects.githubusercontent.com）经常
# 连不上或 DNS 挂起。下载源按顺序尝试：官方直连 → 中转镜像。
# 镜像只是给原 URL 加前缀，文件字节完全一致——info.sha256 校验仍然生效，
# 被篡改的镜像过不了校验，所以这里不存在安全降级。
DOWNLOAD_MIRRORS = ("ghproxy.net", "gh-proxy.com")


def _download_candidates(asset_url):
    """生成候选下载地址：官方直连优先，镜像兜底。"""
    urls = [asset_url]
    tail = asset_url.split("://", 1)[1] if "://" in asset_url else ""
    if tail.startswith("github.com/"):
        for m in DOWNLOAD_MIRRORS:
            urls.append("https://%s/https://%s" % (m, tail))
    return urls


def _probe_bounded(url, timeout=25.0):
    """带**上限**的探测。返回 ProbeResult；超时返回 None。

    「一直卡在准备下载」的根因：DNS 解析（getaddrinfo）**不受 socket
    超时控制**——urlopen(timeout=20) 管得住连接和读，管不住域名解析，
    而解析 github.com 卡住几分钟是国内网络的常态。这里把探测放进
    守护线程，join 到点就放弃（遗弃的 daemon 线程自生自灭，无害），
    主流程换下一个源。
    """
    box = {}

    def _job():
        try:
            import downloader
            box["res"] = downloader.probe(url)
        except Exception as exc:  # noqa: BLE001
            try:
                import downloader
                box["res"] = downloader.ProbeResult(url=url, ok=False,
                                                    error=str(exc))
            except Exception:
                pass

    t = threading.Thread(target=_job, name="YuhubUpdProbe", daemon=True)
    t.start()
    t.join(timeout)
    return box.get("res")


def download_update(info, on_progress=None, cancel=None, threads=None):
    """下载更新包。返回 (ok, message, path)。

    v0.8.3beta 修复两个用户实测的 bug：
      1. **卡在「准备下载」**：原实现走 downloader.download()，其探测
         阶段的 DNS 不受 socket 超时控制，直连 github.com 卡住就永远
         卡住。现在：候选源 = 官方直连 + 两个中转镜像，逐个尝试；
         每个源的探测上限 25 秒，卡住就换下一个。
      2. **点取消后卡在「正在取消」**：原来取消标志只在分块之间检查，
         线程卡在探测/DNS 时永远到不了下一个分块。现在取消在每个
         阶段边界（换源前 / 进度泵每 0.2 秒）都会生效，探测本身也
         有上限，不存在永远等不到的等待。

    下载复用 downloader.DownloadTask（断点续传式分块 / 进度快照），
    与下载页体验一致；下载后的 sha256 校验对镜像同样生效。
    """
    if not info or not info.asset_url:
        return False, "没有可下载的更新包", ""
    if not _url_allowed(info.asset_url):
        return False, "更新地址不是 https，已拒绝", ""

    try:
        import downloader
    except Exception as e:  # noqa: BLE001
        return False, "下载模块不可用：%s" % (e,), ""

    dest = package_path(info)
    last_err = ""

    def cancelled():
        return bool(cancel and cancel())

    def report_status(text):
        # 让 UI 的状态行知道"正在换源"（snapshot 里带 status_text 扩展键）
        if on_progress:
            try:
                on_progress({"status_text": text})
            except Exception:
                pass

    candidates = _download_candidates(info.asset_url)
    for idx, url in enumerate(candidates, 1):
        if cancelled():
            return False, "已取消", ""
        if idx > 1:
            report_status("直连失败，正在尝试中转镜像（%d/%d）…"
                          % (idx - 1, len(candidates) - 1))
        else:
            report_status("正在连接更新源…")

        # ---- ① 限时探测：拿大小/分块支持，同时筛掉连不上的源 ----
        pres = _probe_bounded(url, timeout=25.0)
        if cancelled():
            return False, "已取消", ""
        if pres is None:
            last_err = "连接超时（源 %d/%d）" % (idx, len(candidates))
            continue
        if not pres.ok:
            last_err = pres.error or "连接失败（源 %d/%d）" % (idx, len(candidates))
            continue

        # ---- ② 下载（探测结果直接传入，任务内不再重复探测） ----
        task = downloader.DownloadTask(url, dest, threads=threads,
                                       probe_result=pres)
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
        if on_progress:
            try:
                on_progress(snap)
            except Exception:
                pass

        if snap["status"] == downloader.STATUS_CANCELLED:
            return False, "已取消", ""
        if snap["status"] != downloader.STATUS_DONE:
            last_err = snap["error"] or "下载失败（源 %d/%d）" % (idx, len(candidates))
            continue                     # 这个源不行，换下一个

        # ---- ③ 校验（大小 + sha256，镜像下载的字节同样要过这一关） ----
        if not os.path.isfile(dest):
            return False, "下载完成但文件不存在", ""
        if info.asset_size and os.path.getsize(dest) < info.asset_size:
            return False, ("文件不完整（%d / %d 字节）"
                           % (os.path.getsize(dest), info.asset_size)), ""
        if not verify_sha256(dest, info.sha256):
            try:
                os.remove(dest)
            except OSError:
                pass
            return False, "校验失败，文件可能已损坏或被篡改，已删除", ""
        return True, "下载完成", dest

    return False, last_err or "所有更新源均不可达", ""


# ================================================================ 进程工具

def _pid_alive(pid):
    """PID 是否还活着（OpenProcess + WaitForSingleObject）。"""
    if not pid:
        return False
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x102
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.WaitForSingleObject.restype = ctypes.c_ulong
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(SYNCHRONIZE, 0, int(pid))
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


def wait_pid_exit(pid, timeout=WAIT_OLD_PID_TIMEOUT):
    """等 PID 退出。超时返回 False（调用方决定是否强杀）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.25)
    return False


def kill_pid(pid):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                       capture_output=True, creationflags=_CREATE_NO_WINDOW,
                       timeout=15)
    except Exception:
        pass


def current_exe():
    """当前运行的可执行文件路径（打包后就是 Yuhub.exe）。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0])


def sys_executable():
    """应该被替换 / 被启动的那个 exe。

    打包后就是 Yuhub.exe 本身。源码态没有"exe 可替换"这回事，
    返回当前解释器路径（调用方通常会发现无法替换而放弃）——
    但绝不能返回 `-c` 这种 argv 残留，否则替换器会去改一个不存在的文件。
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    arg0 = sys.argv[0] if sys.argv else ""
    if arg0 and os.path.isfile(arg0):
        return os.path.abspath(arg0)
    return os.path.abspath(sys.executable)


# ================================================================ 替换请求

def build_request(new_exe, target_exe, old_pid, new_version="",
                  launch_args=None, backup=True):
    """构造传给替换器的载荷（会被 base64 编码）。"""
    return {
        "new_exe": os.path.abspath(new_exe),
        "target_exe": os.path.abspath(target_exe),
        "old_pid": int(old_pid or 0),
        "new_version": new_version,
        "launch_args": list(launch_args or []),
        "backup": bool(backup),
    }


def encode_payload(payload):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_payload(b64):
    return json.loads(base64.b64decode(b64.encode("ascii")).decode("utf-8"))


def stage_updater(payload):
    """把「当前的自己」拷一份到临时目录当替换器，返回副本路径。

    ⚠️ 必须拷贝，不能直接用自己：替换器不能是它要替换的那个文件
    （自己锁着自己，改名后行为不可预期）。

    PyInstaller onefile 的 exe 拷到别处能独立运行（依赖解到 _MEIxxxx），
    已实测 22/22 自检通过，所以这条路是通的。
    """
    me = current_exe()
    d = os.path.join(tempfile.gettempdir(), "Yuhub_upd_%d" % os.getpid())
    os.makedirs(d, exist_ok=True)
    upd = os.path.join(d, "Yuhub_updater.exe")
    try:
        shutil.copy2(me, upd)
    except Exception:
        # 同盘拷贝一般没问题；失败时退回一个更短的名字再试
        upd = os.path.join(tempfile.gettempdir(), "Yuhub_upd_%d.exe" % os.getpid())
        shutil.copy2(me, upd)
    return upd


def launch_updater(updater_path, payload, elevated=False):
    """拉起替换器进程。返回 (ok, message)。

    elevated=True 时走 ShellExecuteW("runas")——用于 exe 装在
    Program Files 之类需要管理员权限才能改名的位置。
    """
    b64 = encode_payload(payload)
    args = ["--apply-update", b64]

    if elevated:
        try:
            import ctypes
            SW_SHOWNORMAL = 1
            r = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", updater_path, " ".join(args), None, SW_SHOWNORMAL)
            if int(r) > 32:
                return True, "已请求管理员权限启动更新"
            return False, "提权启动失败（用户取消？）"
        except Exception as e:
            return False, "提权启动异常：%s" % (e,)

    try:
        subprocess.Popen([updater_path] + args,
                         creationflags=_CREATE_NO_WINDOW,
                         close_fds=True)
        return True, "更新程序已启动"
    except Exception as e:
        return False, "启动更新程序失败：%s" % (e,)


def needs_elevation(target_exe):
    """目标 exe 所在目录是否不可写（需要提权才能改名）。"""
    d = os.path.dirname(os.path.abspath(target_exe)) or "."
    probe = os.path.join(d, ".yuhub_write_test_%d" % os.getpid())
    try:
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return False
    except OSError:
        return True


# ================================================================ 替换器侧

def apply_update_main(b64_payload):
    """`Yuhub.exe --apply-update <base64-json>` 模式（在 main() 里早返回）。

    职责：等旧进程退出 → 两步改名替换 → 启动新版 → 观察是否秒退 → 必要时回滚。

    返回进程退出码（0 表示成功完成替换流程）。
    """
    try:
        p = decode_payload(b64_payload)
    except Exception:
        return 2

    new_exe = p.get("new_exe") or ""
    target = p.get("target_exe") or ""
    old_pid = int(p.get("old_pid") or 0)
    do_backup = bool(p.get("backup", True))
    launch_args = p.get("launch_args") or []

    if not new_exe or not target or not os.path.isfile(new_exe):
        return 3

    # ---- ① 等旧进程彻底退出 ----
    if old_pid and _pid_alive(old_pid):
        if not wait_pid_exit(old_pid, WAIT_OLD_PID_TIMEOUT):
            kill_pid(old_pid)             # 超时不退就强杀
            if not wait_pid_exit(old_pid, 5.0):
                return 4                  # 还是活着，放弃（不动文件，保证可回退）

    bak = target + ".bak"

    # ---- ② 清理上次可能残留的 .bak ----
    # 上次更新成功后 .bak 可能因为文件句柄没释放而删不掉，这里补删。
    if os.path.exists(bak):
        try:
            os.remove(bak)
        except OSError:
            # 删不掉就改个名让开位置，避免下面 rename 失败
            try:
                os.replace(bak, bak + ".%d" % int(time.time()))
            except OSError:
                pass

    # ---- ③ 两步改名（本方案的核心）----
    # 运行中的 exe 不能覆盖写，但可以改名（只改目录项，不动内容）。
    try:
        if do_backup:
            os.rename(target, bak)        # 旧 → .bak
        else:
            os.remove(target)
    except OSError as e:
        return 5                          # 连改名都失败：权限/占用，保持原样退出

    try:
        os.replace(new_exe, target)       # 新 → 原路径
    except OSError as e:
        # 新文件就位失败 → 回滚旧版，保证用户至少还能用
        try:
            if do_backup and os.path.exists(bak):
                os.replace(bak, target)
        except OSError:
            pass
        return 6

    # ---- ④ 启动新版 ----
    try:
        proc = subprocess.Popen([target] + list(launch_args),
                                cwd=os.path.dirname(target) or None,
                                close_fds=True)
    except Exception:
        # 打不开新版 → 回滚
        _rollback(bak, target, do_backup)
        return 7

    # ---- ⑤ 观察窗口：新版秒退且非 0 → 判定有问题，自动回滚 ----
    # 这是"更新把自己搞挂"的最后一道保险。
    try:
        code = proc.wait(timeout=NEW_EXE_GRACE)
    except subprocess.TimeoutExpired:
        code = None                       # 活着超过宽限期，认为正常

    if code is not None and code != 0:
        try:
            proc.kill()
        except Exception:
            pass
        if _rollback(bak, target, do_backup):
            try:
                subprocess.Popen([target] + list(launch_args),
                                 cwd=os.path.dirname(target) or None,
                                 close_fds=True)
            except Exception:
                pass
        return 8

    # ---- ⑥ 成功：删掉 .bak（删不掉也无害，下次启动再删）----
    if do_backup:
        for _ in range(5):
            try:
                os.remove(bak)
                break
            except OSError:
                time.sleep(0.4)

    # ---- ⑦ 清理自己的临时目录 ----
    _cleanup_self()

    return 0


def _rollback(bak, target, do_backup):
    """把 .bak 还原成 target。返回是否成功。"""
    if not do_backup or not os.path.exists(bak):
        return False
    try:
        os.replace(bak, target)
        return True
    except OSError:
        return False


def _cleanup_self():
    """删除替换器自己所在的临时目录（尽力而为）。

    进程自己占着 exe 文件，直接删目录会失败，所以让开一个
    cmd 延时删除；失败也无害，%TEMP% 会被系统回收。
    """
    try:
        me = current_exe()
        d = os.path.dirname(me)
        if os.path.basename(d).startswith("Yuhub_upd_"):
            subprocess.Popen(
                ["cmd", "/c", "ping", "127.0.0.1", "-n", "3", ">nul",
                 "&", "rd", "/s", "/q", d],
                creationflags=_CREATE_NO_WINDOW,
                close_fds=True)
    except Exception:
        pass
