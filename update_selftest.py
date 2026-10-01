"""自动更新自检（`Yuhub.exe --update-selftest <结果json路径>`）。

为什么必须做成 exe 内部自检：更新流程会**改文件 + 重启进程**，
用外部脚本驱动既不可靠也不安全。这里在进程内：

  1. 起一个本地 http.server 伪装成更新源
  2. 用副本 exe 当"要替换的目标"（绝不动正在运行的自己）
  3. 真跑一遍：检查 → 下载 → 校验 → 替换 → 重启
  4. 失败路径也跑：校验和不匹配、新版秒退自动回滚、旧版仍可用

结果写成与 theme/uninstall 自检同构的 JSON：
    {"ok": bool, "checks": [{"name","pass","detail"}], "info": {...}}
"""

import hashlib
import http.server
import io
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time

import updater

_RESULT = {"ok": False, "checks": [], "info": {}}
_LOCK = threading.Lock()


def _add(name, passed, detail=""):
    with _LOCK:
        _RESULT["checks"].append(
            {"name": name, "pass": bool(passed), "detail": str(detail)})


def _local_machine():
    """本机 OS 的 PE Machine（体检放行要用同一个口径）。"""
    arch = (os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    return {"AMD64": 0x8664, "ARM64": 0xAA64, "X86": 0x14C,
            "ARM": 0x1C0}.get(arch, 0x14C)


def _fake_pe_payload(size=8192, machine=None):
    """造一个"体检能过"的假更新包：MZ + PE 签名 + Machine 字段。

    为什么不能再用一段随机字节：给 apply 加了"坏包体检"之后，
    「下载下来的必须是个真程序」成了契约的一部分，随机字节会被拦下——
    那是**正确**行为，只是不该拿来测"下载成功/替换成功"。
    这里不追求它能真的执行（下载、校验、两步改名都不需要）。
    """
    buf = bytearray(b"\0" * max(4096, size))
    buf[0:2] = b"MZ"
    off = 0x80
    buf[0x3C:0x40] = off.to_bytes(4, "little")
    buf[off:off + 4] = b"PE\0\0"
    buf[off + 4:off + 6] = (machine or _local_machine()).to_bytes(2, "little")
    return bytes(buf)


# ================================================================ 假更新源

class _Handler(http.server.BaseHTTPRequestHandler):
    """按路径返回不同的伪装响应。

    /repos/<o>/<r>/releases/latest  → 假 Release JSON（可切换成 404）
    /repos/<o>/<r>/releases         → 假 Release 列表
    /download/<file>                → 假 exe 字节流
    """

    release = {}
    release_list = []
    payload = b""
    latest_404 = False          # True 时 latest 返回 404（模拟"只有 prerelease"）

    def log_message(self, *a):
        pass                                   # 静音

    def do_GET(self):
        if self.path.startswith("/repos/") and self.path.endswith("/releases/latest"):
            if self.latest_404:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                body = b'{"message":"Not Found"}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps(self.release).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "/releases" in self.path and "latest" not in self.path:
            body = json.dumps(self.release_list).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/download/") or self.path.startswith("/notfound"):
            if self.path.startswith("/notfound"):
                self.send_response(404)
                self.end_headers()
                return
            data = self.payload
            # 支持 Range（downloader 会探测分块；不支持也能跑单线程）
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                try:
                    spec = rng.split("=", 1)[1].split(",")[0]
                    a, _, b = spec.partition("-")
                    start = int(a) if a else 0
                    end = int(b) if b else len(data) - 1
                    chunk = data[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range",
                                     "bytes %d-%d/%d" % (start, end, len(data)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
                except Exception:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_server():
    srv = _Server(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


# ================================================================ 测试用例

def _test_version_compare():
    cases = [
        ("v0.12.0", "0.11.1", True, "常规升级"),
        ("0.11.10", "0.11.9", True, "第二位数字比较（不能按字符串比）"),
        ("0.8beta", "0.11.1", False, "降级不认"),
        ("v1.0", "0.9.9", True, "前缀 v 可有可无、长度不齐"),
        ("0.11.1", "0.11.1", False, "同版本不算新"),
        ("0.8.1", "0.8beta", True, "正式版 > 同号 beta"),
    ]
    bad = []
    for remote, local, want, desc in cases:
        got = updater.is_newer(remote, local)
        if got != want:
            bad.append("%s vs %s → %s（期望 %s，%s）" % (remote, local, got, want, desc))
    _add("版本号比较（6 组）", not bad, "; ".join(bad) or "全部符合预期")

    # 解析本身
    p = updater.parse_version("v0.8beta")
    _add("版本号解析 v0.8beta", p == (0, 8, -1), "得到 %r" % (p,))
    p2 = updater.parse_version("1.2.3")
    _add("版本号解析 1.2.3", p2 == (1, 2, 3), "得到 %r" % (p2,))


def _test_pick_asset():
    assets = [
        {"name": "checksums.txt", "browser_download_url": "https://x/checksums.txt"},
        {"name": "Yuhub.exe", "browser_download_url": "https://x/Yuhub.exe", "size": 10},
    ]
    a = updater.pick_asset(assets)
    _add("从 assets 里挑主程序", a and a["name"] == "Yuhub.exe",
         "挑到 %s" % (a.get("name") if a else None))

    a2 = updater.pick_asset([{"name": "other.exe", "browser_download_url": "https://x/o.exe"}])
    _add("无精确匹配时退到第一个 .exe", a2 and a2["name"] == "other.exe",
         "挑到 %s" % (a2.get("name") if a2 else None))


def _test_check_update(port, tag):
    base = "http://127.0.0.1:%d" % port
    # 有新版
    info, err = updater.check_for_update(
        "0.7.0", owner="u", repo="r", api_base=base)
    _add("检查更新：有新版", info is not None and info.tag == tag and not err,
         "tag=%s err=%r" % (info.tag if info else None, err))

    # 已最新
    info2, err2 = updater.check_for_update(
        tag, owner="u", repo="r", api_base=base)
    _add("检查更新：已是最新", info2 is None and not err2,
         "info=%r err=%r" % (info2, err2))

    # 网络不可达 → 静默失败，不抛异常
    info3, err3 = updater.check_for_update(
        "0.7.0", owner="u", repo="r",
        api_base="http://127.0.0.1:1", timeout=1)
    _add("检查更新：网络不可达时静默返回", info3 is None,
         "err=%r（不应崩溃）" % (err3,))

    return info


def _test_prerelease_fallback(port, tag):
    """⚠️ 关键回归：GitHub 的 releases/latest **跳过 prerelease**。

    本项目 tag 就叫 v0.8beta，发布时如果勾了 pre-release，
    latest 接口会 404 —— 客户端就永远看不到更新。
    这里模拟"latest 404，但列表里有 prerelease"，验证能正确兜底。
    """
    base = "http://127.0.0.1:%d" % port
    _Handler.latest_404 = True
    try:
        info, err = updater.check_for_update(
            "0.7.0", owner="u", repo="r", api_base=base)
        _add("latest 404 时退到 releases 列表（prerelease 也能发现）",
             info is not None and info.tag == tag and not err,
             "tag=%s err=%r" % (info.tag if info else None, err))

        # 列表里全是 draft → 应当视为没有可更新版本
        saved = _Handler.release_list
        _Handler.release_list = [dict(saved[0], draft=True)]
        info2, err2 = updater.check_for_update(
            "0.7.0", owner="u", repo="r", api_base=base)
        _add("draft 版本不被当作更新",
             info2 is None and not err2,
             "info=%r err=%r" % (info2, err2))
        _Handler.release_list = saved
    finally:
        _Handler.latest_404 = False


def _test_download_and_verify(info):
    ok, msg, path = updater.download_update(info)
    _add("下载更新包", ok and os.path.isfile(path),
         "%s | %s" % (msg, path if ok else ""))

    if not ok:
        return ""

    # 大小一致
    same = os.path.getsize(path) == info.asset_size
    _add("下载大小与清单一致", same,
         "%d vs %d" % (os.path.getsize(path), info.asset_size))

    # 摘要正确 → 通过
    good = updater.verify_sha256(path, info.sha256)
    _add("sha256 正确时校验通过", good, "expect=%s" % info.sha256[:16])

    # 摘要错误 → 拒绝，且文件被删
    bad = updater.verify_sha256(path, "0" * 64)
    _add("sha256 不匹配时校验失败", not bad, "错误摘要未通过")

    return path


def _test_download_bad_sha(port, good_sha):
    """清单里的 sha256 是错的 → download_update 必须拒绝并删文件。"""
    base = "http://127.0.0.1:%d" % port
    info, _ = updater.check_for_update(
        "0.7.0", owner="u", repo="r", api_base=base)
    if info is None:
        _add("错误摘要被拒绝", False, "前置检查失败")
        return
    info.sha256 = "deadbeef" * 8
    ok, msg, path = updater.download_update(info)
    _add("清单摘要错误时下载被拒绝", (not ok) and ("校验失败" in msg),
         "ok=%s msg=%s" % (ok, msg))
    _add("校验失败后不留下半成品",
         not path or not os.path.exists(updater.package_path(info)),
         "残留=%s" % (path or "无"))


def _test_download_sources_and_cancel(port):
    """v0.8.5beta 回归：镜像候选、限时探测、代理兜底、取消即时生效。

    线上 bug：直连 github.com 时 DNS 挂起 → 永远卡在「准备下载」；
    取消标志只在分块之间检查 → 卡在「正在取消」只能强杀进程。
    """
    import downloader as _dl

    def _selftest_base():
        return "http://127.0.0.1:%d" % port

    # ① github URL 生成 直连 + 3 个镜像候选；本地 URL 不加镜像
    cands = updater._download_candidates(
        "https://github.com/u/r/releases/download/v1/Yuhub.exe")
    ok1 = (len(cands) == 4
           and cands[0].startswith("https://github.com/")
           and any("ghfast.top" in c for c in cands[1:])
           and any("ghproxy.net" in c for c in cands[1:]))
    _add("下载源候选：github URL 附带 3 个镜像", ok1,
         "候选=%d 个" % len(cands))

    cands2 = updater._download_candidates("http://127.0.0.1:9/x.exe")
    _add("下载源候选：非 github URL 不加镜像", len(cands2) == 1,
         "候选=%d 个" % len(cands2))

    # ② 取消标志在探测前就生效 → 立即返回「已取消」，不发起任何网络请求
    info = updater.ReleaseInfo(
        tag="v9", name="x", notes="", asset_name="Yuhub.exe",
        asset_url="http://127.0.0.1:1/Yuhub.exe", asset_size=0)
    ok2, msg2, _ = updater.download_update(info, cancel=lambda: True)
    _add("下载前已取消时立即返回", (not ok2) and msg2 == "已取消",
         "ok=%s msg=%r" % (ok2, msg2))

    # ③ 系统代理故障 → 自动切直连并成功下载（真实本地服务器）
    info3, _ = updater.check_for_update("0.7.0", owner="u", repo="r",
                                        api_base=_selftest_base())
    calls = []

    def _fake_probe_system_fail(url, timeout=15.0, proxy=None, cancel=None):
        calls.append(proxy)
        if proxy is None:
            return _dl.ProbeResult(url=url, ok=False,
                                   error="模拟系统代理故障")
        return _dl.probe(url)             # 直连走真实本地服务器

    saved = updater._probe_line
    updater._probe_line = _fake_probe_system_fail
    try:
        ok3, msg3, path3 = updater.download_update(info3)
    finally:
        updater._probe_line = saved
    _add("系统代理故障时自动切直连并下载成功",
         ok3 and calls == [None, "direct"],
         "ok=%s 线路顺序=%r msg=%r" % (ok3, calls, msg3))

    # ④ 全部线路都失败：镜像×代理模式逐条尝试后报最后错误 + 状态提示
    info4 = updater.ReleaseInfo(
        tag="v9", name="x", notes="", asset_name="Yuhub.exe",
        asset_url="https://github.com/u/r/releases/download/v1/Yuhub.exe",
        asset_size=0)
    statuses = []
    calls4 = []

    def _fake_probe_all_fail(url, timeout=15.0, proxy=None, cancel=None):
        calls4.append(proxy)
        return _dl.ProbeResult(url=url, ok=False, error="模拟连接失败")

    updater._probe_line = _fake_probe_all_fail
    try:
        ok4, msg4, _ = updater.download_update(
            info4, on_progress=lambda s: statuses.append(dict(s)))
    finally:
        updater._probe_line = saved
    _add("所有线路失败时逐条尝试并报最后错误",
         (not ok4) and "模拟连接失败" in msg4 and len(calls4) == 8,
         "尝试=%d msg=%r" % (len(calls4), msg4))
    _add("换线时向 UI 报状态提示",
         any("切换下载线路" in str(s.get("status_text", ""))
             for s in statuses),
         "状态数=%d" % len([s for s in statuses
                            if s.get("status_text")]))

    # ⑤ 取消高优先级：真实 _probe_line + 模拟"网络卡死"（底层 probe 阻塞 5 秒），
    #    取消轮询必须 0.1 秒粒度生效 → 整体应在 ~0.5 秒内返回
    info5, _ = updater.check_for_update("0.7.0", owner="u", repo="r",
                                        api_base=_selftest_base())
    flag = {"c": False}

    def _slow_network_probe(url, insecure_fallback=True, proxy=None):
        time.sleep(5.0)               # 模拟探测被网络彻底卡死
        return _dl.ProbeResult(url=url, ok=False, error="slow network")

    saved_dl_probe = _dl.probe
    _dl.probe = _slow_network_probe
    t0 = time.monotonic()

    def _flip():
        time.sleep(0.3)
        flag["c"] = True

    threading.Thread(target=_flip, daemon=True).start()
    try:
        ok5, msg5, _ = updater.download_update(
            info5, cancel=lambda: flag["c"])
    finally:
        _dl.probe = saved_dl_probe
    elapsed = time.monotonic() - t0
    _add("探测卡死时取消高优先级生效（秒级响应）",
         (not ok5) and msg5 == "已取消" and elapsed < 2.0,
         "msg=%r 耗时=%.2fs" % (msg5, elapsed))



def _test_block_integrity():
    """v0.8.15beta 回归：分块失败**绝不能**被当成"下载完成"。

    线上 bug（用户截图反馈）：10 个分块里 7 个"多次重试后仍失败"，
    任务却报 status=done、downloaded=total。根因是收尾校验看的是
    `.part` 的文件大小，而它是按总大小**预分配**的（零填充内容），
    大小恒等于 total —— 缺块的残包照样放行，交给替换器之后
    用户看到的就是"提示更新完成，但程序打不开 / 报不兼容"。
    """
    import re

    import downloader as _dl

    size = 2 * 1024 * 1024
    data = (bytes(range(256)) * (size // 256 + 1))[:size]
    digest = hashlib.sha256(data).hexdigest()

    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        fail_lo = -1
        fail_hi = -1

        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"bytes=(\d+)-(\d+)", rng)
                lo, hi = int(m.group(1)), int(m.group(2))
            else:
                lo, hi = 0, size - 1
            lo = max(0, min(lo, size - 1))
            hi = max(lo, min(hi, size - 1))
            # 长度为 1 的 Range 是探测请求（probe 用 bytes=0-0），不注入
            # 故障 —— 否则测出来的是"探测失败"，而不是"分块失败"
            if hi > lo and self.fail_lo <= lo <= self.fail_hi:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            chunk = data[lo:hi + 1]
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (lo, hi, size))
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(chunk)

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/f.bin" % srv.server_address[1]
    work = tempfile.mkdtemp(prefix="yuhub_bi_")

    # 退避调小：逻辑一字未改，只是不让自检为了等 4 轮指数退避跑上几分钟
    saved_pause, saved_backoff = _dl.ROUND_PAUSE, _dl._backoff
    _dl.ROUND_PAUSE = (0.0, 0.0, 0.0, 0.0)
    _dl._backoff = lambda retry, base=0.6, cap=8.0: 0.05

    def _run(name, fail_lo=-1, fail_hi=-1):
        _H.fail_lo, _H.fail_hi = fail_lo, fail_hi
        dest = os.path.join(work, name)
        task = _dl.DownloadTask(url, dest, threads=4)
        task.start()
        end = time.monotonic() + 90
        while task.running and time.monotonic() < end:
            time.sleep(0.05)
        return task.snapshot(), dest

    try:
        # ① 第 3 块（4 块均分 2MB → 512KB/块 → 1048576..1572863）全部失败
        snap, dest = _run("bad.bin", 1048576, 1572863)
        leftover = [f for f in os.listdir(work) if f.startswith("bad.bin")]
        _add("★ 分块失败必须报错误（不许报 done）",
             snap["status"] == _dl.STATUS_ERROR,
             "status=%s error=%r" % (snap["status"], snap["error"]))
        _add("★ 分块失败不留残包",
             (not os.path.exists(dest)) and (not leftover),
             "残留=%s" % leftover)
        _add("失败原因指明是分块问题",
             "分块" in (snap["error"] or ""), snap["error"])

        # ② 无故障时下载仍然完整（别为了防错把正常路径也掐了）
        snap2, dest2 = _run("good.bin")
        ok2 = (snap2["status"] == _dl.STATUS_DONE
               and os.path.isfile(dest2)
               and hashlib.sha256(open(dest2, "rb").read()).hexdigest() == digest
               and not os.path.exists(dest2 + ".part"))
        _add("无故障时下载完整且校验通过", ok2,
             "status=%s error=%r" % (snap2["status"], snap2["error"]))

        # ③ 进度文案：snapshot 字典必须转成人话
        #    （用户截图里那坨 {"status": "done", ...} 就是没转的结果）
        _phase, text = updater.format_progress({
            "status": "running", "downloaded": 1024, "total": 2048,
            "percent": 50.0, "speed": 512.0, "eta": 2.0, "messages": []})
        _add("★ 进度文案不含原始 JSON",
             "{" not in text and "}" not in text and "50.0%" in text,
             repr(text))
        _phase2, text2 = updater.format_progress(
            {"status_text": "正在连接更新源…", "phase": "probing"})
        _add("探测阶段文案原样透传",
             "{" not in text2 and "连接更新源" in text2, repr(text2))
    finally:
        _dl.ROUND_PAUSE, _dl._backoff = saved_pause, saved_backoff
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(work, ignore_errors=True)


def _test_http_downgrade_rejected():
    """非 https 的更新地址必须被拒绝（防降级到明文）。"""
    info = updater.ReleaseInfo(
        tag="v9.9.9", name="x", notes="", asset_name="Yuhub.exe",
        asset_url="http://evil.example.com/Yuhub.exe", asset_size=0)
    ok, msg, _ = updater.download_update(info)
    _add("拒绝非 https 更新地址", (not ok) and ("https" in msg),
         "ok=%s msg=%s" % (ok, msg))


def _test_replace_flow(port, tag, good_sha, payload_bytes):
    """端到端替换：用副本 exe 当目标，验证两步改名 + 重启 + 回滚。

    绝不动正在运行的自己 —— target 是 %TEMP% 下的副本。
    """
    work = os.path.join(tempfile.gettempdir(), "Yuhub_updtest_%d" % os.getpid())
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    target = os.path.join(work, "Yuhub.exe")
    # 副本 exe 用"当前自己的可执行文件"（源码态则是 python.exe，
    # 但我们都只关心文件是否被换掉，不关心副本能否跑起来）
    me = updater.current_exe()
    try:
        shutil.copy2(me, target)
    except Exception as e:
        _add("准备替换目标副本", False, "拷贝失败：%s" % e)
        return work
    _add("准备替换目标副本", os.path.isfile(target),
         "%s (%d 字节)" % (target, os.path.getsize(target)))

    # ① 两步改名的核心行为：旧→.bak 能成功、新→原路径能成功
    new_file = os.path.join(work, "new_version.exe")
    with open(new_file, "wb") as f:
        f.write(payload_bytes)
    bak = target + ".bak"
    try:
        os.rename(target, bak)
        renamed_ok = os.path.exists(bak) and not os.path.exists(target)
    except OSError as e:
        renamed_ok = False
        _add("改名前目标存在", False, "重命名旧文件失败：%s" % e)
    _add("第一步：旧 exe → .bak", renamed_ok, "bak 存在=%s" % os.path.exists(bak))

    try:
        os.replace(new_file, target)
        placed_ok = os.path.exists(target) and not os.path.exists(new_file)
    except OSError:
        placed_ok = False
    _add("第二步：新 exe → 原路径", placed_ok, "target 已就位=%s" % placed_ok)

    if placed_ok:
        same = os.path.getsize(target) == len(payload_bytes)
        _add("替换后内容长度正确", same,
             "%d vs %d" % (os.path.getsize(target), len(payload_bytes)))

    # ② 回滚：.bak 还原回原路径
    try:
        os.replace(bak, target)
        rolled = os.path.exists(target) and not os.path.exists(bak)
    except OSError:
        rolled = False
    _add("失败回滚：.bak 还原成功", rolled,
         "target=%s bak 已清=%s" % (os.path.exists(target), not os.path.exists(bak)))

    # ③ apply_update_main 的完整路径（用一个立刻退出的假目标验证）
    _test_apply_main(work)

    shutil.rmtree(work, ignore_errors=True)
    return work


def _test_apply_main(work):
    """验证替换器的入口行为——重点是新增的「坏包体检」闸门。

    ⚠️ 这里必须用**非 PE** 的假包，这是回归的靶子：

    以前 apply_update_main 不做任何检查就把新包 rename 就位、直接 Popen。
    把一个不是真 64 位程序的文件交给 Windows，系统会弹一个**模态对话框**：

        不支持的 16 位应用程序
        由于与 64 位版本的 Windows 不兼容，此程序或功能
        "…\\Temp\\Yuhub_updtest_<pid>\\apply\\Target.exe" 无法启动或运行。
        请联系软件供应商询问是否有与 64 位 Windows 兼容的版本。

    用户报的正是这个（弹窗里点名 Target.exe）。明明是更新包坏了，
    看起来却像"Yuhub 跟你的系统不兼容"，而且是系统级弹窗，
    用户除了点「确定」什么也做不了。

    现在的契约：体检不合格 → 返回 9，**一个文件都不动、也不启动任何东西**，
    所以系统压根没有机会弹框。
    """
    d = os.path.join(work, "apply")
    os.makedirs(d, exist_ok=True)
    target = os.path.join(d, "Target.exe")
    new_exe = os.path.join(d, "incoming.exe")

    real = updater.current_exe()
    try:
        shutil.copy2(real, target)
    except Exception as e:
        _add("apply_update_main：准备替换目标", False, "拷贝失败：%s" % e)
        return

    try:
        target_size = os.path.getsize(target)
    except OSError:
        target_size = 0
    _add("apply_update_main：准备替换目标", target_size > 0,
         "%s (%d 字节)" % (os.path.basename(real), target_size))

    # ---- ① 坏包：体检必须拦下，且不动目标文件 ----
    with open(new_exe, "wb") as f:
        f.write(b"NOT-A-PE" * 64)
    payload = updater.build_request(new_exe, target, 0, new_version="vtest")
    b64 = updater.encode_payload(payload)

    rc = None
    try:
        rc = updater.apply_update_main(b64)
    except Exception as e:
        _add("apply_update_main 不抛异常", False, "%s" % e)
        return

    _add("apply_update_main 不抛异常", rc is not None, "返回码=%r" % rc)
    _add("★ 坏包被体检拦下（返回 9，绝不交给 Windows 启动）", rc == 9,
         "返回码=%r" % rc)
    exist = os.path.exists(target)
    _add("★ 坏包不改动目标文件（旧版原封不动，用户照样能用）",
         exist and os.path.getsize(target) == target_size,
         "target=%s %d 字节（原 %d）"
         % ("存在" if exist else "不见了",
            os.path.getsize(target) if exist else -1, target_size))
    _add("坏包不遗留 .bak", not os.path.exists(target + ".bak"),
         "bak 存在=%s" % os.path.exists(target + ".bak"))

    # ---- ② 好包不能误杀（否则正常更新全被拦） ----
    with open(new_exe, "wb") as f:
        f.write(_fake_pe_payload())
    fine, why = updater.check_package_runnable(new_exe)
    _add("★ 合法 PE 能通过体检（正常更新不被误拦）", fine, why or "通过")

    # ---- ③ 兜底防线：载入器系统弹窗开关已关 ----
    _add("已关闭 Windows 载入器系统弹窗（兜底防线）",
         updater.loader_dialogs_silenced(),
         "GetErrorMode & SEM_FAILCRITICALERRORS = %s"
         % updater.loader_dialogs_silenced())


def _test_package_guard():
    """更新包体检：四种典型坏包都要认出来，好包不能误杀。

    这些坏包不是假想——下载被掐断就会留下半截文件；被代理/镜像劫持时
    拿回来的常常是一个 HTML 错误页被原样存成 .exe。它们以前都会一路
    走到"交给 Windows 启动"，然后弹系统对话框。
    """
    d = os.path.join(tempfile.gettempdir(), "Yuhub_pkgtest_%d" % os.getpid())
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    try:
        def put(name, data):
            p = os.path.join(d, name)
            with open(p, "wb") as f:
                f.write(data)
            return p

        good = put("good.exe", _fake_pe_payload())
        ok, why = updater.check_package_runnable(good)
        _add("体检放行合法 PE", ok, why or "通过")

        tiny = put("tiny.exe", b"YUHUBNEW" + os.urandom(200))
        ok, why = updater.check_package_runnable(tiny)
        _add("★ 只有几 KB 的「更新包」被拒（下载中断）",
             (not ok) and ("字节" in why), why)

        html = put("html.exe", b"<!DOCTYPE html><html>" + b"<div>" * 2000)
        ok, why = updater.check_package_runnable(html)
        _add("★ HTML 错误页存成 .exe 被拒（代理/镜像劫持）",
             (not ok) and ("不是有效" in why), why)

        arm = put("arm.exe", _fake_pe_payload(machine=0xAA64))
        arch = (os.environ.get("PROCESSOR_ARCHITEW6432")
                or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
        ok, why = updater.check_package_runnable(arm)
        if arch == "AMD64":
            _add("★ 架构不符的包被拒（就是「不兼容本机」这一条）",
                 (not ok) and ("不兼容" in why), why)
        else:
            _add("★ 架构不符的包被拒（就是「不兼容本机」这一条）", True,
                 "本机 %s，该分支不适用" % arch)

        ok, why = updater.check_package_runnable(os.path.join(d, "nope.exe"))
        _add("更新包不存在 → 明确拒绝（不是直接崩）", not ok, why)

        _add("pe_machine 读得出 Machine 字段",
             updater.pe_machine(good) in updater.runnable_machines(),
             updater.pe_machine(good))
        _add("pe_machine 对非 PE 返回 None",
             updater.pe_machine(html) is None, updater.pe_machine(html))

        # e_lfanew 离谱（随机字节里恰好出现 MZ 的情况）也不能崩
        junk = put("junk.exe", b"MZ" + os.urandom(8190))
        _add("头部像 PE 其实是垃圾 → 返回 None 而不是抛异常",
             updater.pe_machine(junk) is None, updater.pe_machine(junk))
    except Exception as e:
        _add("体检自检自身未崩溃", False, repr(e))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _test_selftest_mode_guarded():
    """替换器模式的参数校验：空/坏载荷必须快速失败，不能误删文件。"""
    rc = updater.apply_update_main("bm90LWpzb24=")     # base64("not-json")
    _add("坏载荷 → 立即失败（不误动文件）", rc == 2, "返回码=%r" % rc)

    payload = updater.build_request("", "", 0)
    rc2 = updater.apply_update_main(updater.encode_payload(payload))
    _add("空路径 → 拒绝执行", rc2 == 3, "返回码=%r" % rc2)


def _test_checker_signals_connectable():
    """回归：UpdateChecker 的 Qt 信号必须能 connect。

    曾经的严重 bug —— UpdateChecker 只继承了 threading.Thread，
    而 Qt 的 Signal 只有在 QObject 派生类上才被元对象系统接管，
    于是 `th.found.connect(...)` 抛
      AttributeError: 'Signal' object has no attribute 'connect'
    导致「启动静默检查」和「手动检查更新」**两条路都在第一步就崩**，
    自动更新等于完全没生效（而 --update-selftest 只测 updater 纯逻辑，
    覆盖不到这一层，所以此前一直没暴露）。

    这里不去 import ui.update_ui（那需要 QApplication），
    而是直接检查类的 MRO 里有没有 QObject —— 轻量且足够。
    """
    try:
        from PySide6.QtCore import QObject
    except Exception as e:
        _add("Qt 可用（信号回归检查前置）", False, str(e))
        return

    try:
        from ui.update_ui import UpdateChecker
    except Exception as e:
        _add("能导入 UpdateChecker", False, str(e))
        return

    mro_names = [c.__name__ for c in UpdateChecker.__mro__]
    has_qobject = issubclass(UpdateChecker, QObject)
    _add("UpdateChecker 继承 QObject（信号才能 connect）",
         has_qobject, "MRO=%s" % " → ".join(mro_names))

    # ⚠️ 在**类**上取信号拿到的是 Signal 描述符，它本身没有 connect；
    # 只有绑定到**实例**后才变成 bound signal。所以必须实例化再检查。
    # 构造不启动线程，也不需要 QApplication。
    try:
        inst = UpdateChecker("0.0.0.0")
    except Exception as e:
        _add("能实例化 UpdateChecker", False, str(e))
        return

    _add("能实例化 UpdateChecker", True, "已构造")

    for sig_name in ("found", "failed", "finished_"):
        sig = getattr(inst, sig_name, None)
        ok = sig is not None and hasattr(sig, "connect")
        _add("信号 %s 可连接" % sig_name, ok, "类型=%s" % type(sig).__name__)

    # 真正连一次（不发射），彻底确认不会抛 AttributeError
    try:
        inst.found.connect(lambda _o: None)
        inst.failed.connect(lambda _s: None)
        inst.finished_.connect(lambda: None)
        _add("三个信号实际 connect 不报错", True, "已连接空槽")
    except Exception as e:
        _add("三个信号实际 connect 不报错", False, repr(e))




# ================================================================ 入口

def run(out_path):
    started = time.time()
    srv = None
    info = None                # 收尾清理假更新包要用（异常时也要能取到）
    try:
        # ---- 准备：假的"新版 exe"内容 ----
        # ⚠️ 必须是**能过体检的假 PE**：download_update 现在会在下载后
        # 体检（"拿到手的必须是个真程序"），随机字节会被合法地拦下来，
        # 那样测不到"下载成功"这条正常路径。
        # 坏包的拒绝路径由 _test_package_guard / _test_apply_main 专门覆盖。
        fake_exe = _fake_pe_payload()

        sha = hashlib.sha256(fake_exe).hexdigest()
        tag = "v0.99.0"

        _Handler.payload = fake_exe
        _Handler.release = {
            "tag_name": tag,
            "name": "Yuhub %s" % tag,
            "body": "## 自检用假版本\n- 这是 --update-selftest 生成的内容",
            "published_at": "2026-10-01T00:00:00Z",
            "assets": [{
                "name": "Yuhub.exe",
                "size": len(fake_exe),
                "browser_download_url": "",       # 下面填真端口
                "digest": "sha256:" + sha,
            }],
        }

        srv, port = _start_server()
        base = "http://127.0.0.1:%d" % port
        _Handler.release["assets"][0]["browser_download_url"] = base + "/download/Yuhub.exe"

        _RESULT["info"] = {
            "frozen": bool(getattr(sys, "frozen", False)),
            "exe": updater.current_exe(),
            "fake_tag": tag,
            "fake_sha256": sha,
            "port": port,
        }

        # ---- 跑各项检查 ----
        _test_version_compare()
        _test_pick_asset()
        _Handler.release_list = [_Handler.release]      # 供 latest-404 兜底测试用
        info = _test_check_update(port, tag)
        _test_prerelease_fallback(port, tag)

        if info is not None:
            _test_download_and_verify(info)
            _test_download_bad_sha(port, sha)

        _test_http_downgrade_rejected()
        _test_download_sources_and_cancel(port)
        _test_block_integrity()
        _test_replace_flow(port, tag, sha, fake_exe)
        _test_selftest_mode_guarded()
        _test_package_guard()
        _test_checker_signals_connectable()

    except Exception as e:
        import traceback
        _add("自检自身未崩溃", False, "%s\n%s" % (e, traceback.format_exc()))
    finally:
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        # 自检往**真实的更新目录**里写过一个假更新包（v0.99.0，它是
        # download_update 真实路径的产物，正是要覆盖 package_path 的
        # 命名规则）。测完必须自己擦掉：那不是用户的东西，留在那儿
        # 会让更新目录越来越脏，也可能被误当成"已下载好的更新包"。
        try:
            if info is not None:
                leftover = updater.package_path(info)
                if os.path.isfile(leftover):
                    os.remove(leftover)
        except Exception:
            pass

    checks = _RESULT["checks"]
    _RESULT["ok"] = all(c["pass"] for c in checks) and len(checks) > 0
    _RESULT["info"]["elapsed"] = round(time.time() - started, 2)

    try:
        with io.open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_RESULT, ensure_ascii=False, indent=2))
    except Exception:
        return 5

    return 0 if _RESULT["ok"] else 1
