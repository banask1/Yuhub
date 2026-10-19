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
import subprocess
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


def _test_elevation_decision():
    """回归：替换器**必须提权启动** —— 自动更新曾 100% 静默失效。

    2026-10-13 用户实测：v1.1 下载完更新包，程序关了、版本没变。
    根因不是网络也不是文件锁，而是**用非提权方式启动了一个带
    `requireAdministrator` 清单的 exe**，Windows 直接拒绝：
    `WinError 740「请求的操作需要提升」`，替换器从未真正运行。

    而旧 `needs_elevation()` 只探测**目标目录能不能写**。exe 躺在桌面
    这类可写目录 → 判 False → 走不带 runas 的 `subprocess.Popen` →
    必撞 740。**"副本需要提权"与"目录可写"完全是两件事。**

    这组断言把根因钉死，任何人再把判据改回"只看目录可写"都会立刻变红。
    """
    # ---- ① 清单识别：带 requireAdministrator 的 exe 必须判为需要提权 ----
    d = tempfile.mkdtemp(prefix="Yuhub_elev_")
    try:
        # 造一个"带清单标记"的假 exe：只需含 requestedExecutionLevel
        # 与 requireAdministrator 两段文本，判据即命中
        fake_req = os.path.join(d, "req.exe")
        with open(fake_req, "wb") as f:
            f.write(b"MZ" + b"\0" * 64 +
                    b'<requestedExecutionLevel level="requireAdministrator" />'
                    + b"\0" * 64)
        got_req = updater.exe_requires_elevation(fake_req)
        _add("清单识别：requireAdministrator → 需要提权", got_req is True,
             "返回 %r" % (got_req,))

        fake_as = os.path.join(d, "asInvoker.exe")
        with open(fake_as, "wb") as f:
            f.write(b"MZ" + b"\0" * 64 +
                    b'<requestedExecutionLevel level="asInvoker" />'
                    + b"\0" * 64)
        got_as = updater.exe_requires_elevation(fake_as)
        _add("清单识别：asInvoker → 不需要提权", got_as is False,
             "返回 %r" % (got_as,))

        # 真 exe（冻结态）也该判 True —— 这是本项目的常态
        if getattr(sys, "frozen", False):
            got_real = updater.exe_requires_elevation(sys.executable)
            _add("真 exe（uac_admin=True）判为需要提权", got_real is True,
                 "返回 %r" % (got_real,))

        # ---- ② needs_elevation 不能只看目录可写 ----
        # 目录一定可写（temp），但副本要求提权 → 必须判 True。
        # 旧实现在这里返回 False，这就是自动更新失效的直接原因。
        target_in_tmp = os.path.join(d, "Yuhub.exe")
        with open(target_in_tmp, "wb") as f:
            f.write(b"MZ" + b"\0" * 64)
        legacy = updater.needs_elevation(target_in_tmp)          # 旧语义
        fixed = updater.needs_elevation(target_in_tmp,
                                        updater_exe=fake_req)     # 新语义
        _add("旧判据漏判（目录可写 → False，复现事故成因）",
             legacy is False, "返回 %r" % (legacy,))
        _add("新判据正确判 True（副本要求提权）", fixed is True,
             "返回 %r" % (fixed,))
    except Exception as e:
        _add("提权判据自检未崩溃", False, repr(e))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # ---- ③ 参数编码：base64 与带空格路径必须完整送达替换器 ----
    # 旧实现把参数裸拼成字符串给 ShellExecuteW，被按空格拆词。
    payload = {
        "new_exe": os.path.join(tempfile.gettempdir(), "aa bbb", "Yuhub.exe"),
        "target_exe": r"C:\Program Files\Yuhub\Yuhub.exe",
        "old_pid": 1, "new_version": "v1.1.3",
        "launch_args": [], "backup": True,
    }
    b64 = updater.encode_payload(payload)
    params = " ".join(updater._quote_win_arg(a)
                      for a in ["--apply-update", b64])
    _add("参数逐个加引号（4 个引号）", params.count('"') == 4,
         "引号数 %d" % params.count('"'))
    # 往返：交给 Windows 命令行解析器还原，载荷必须一字不差
    rd = tempfile.mkdtemp(prefix="Yuhub_quote_")
    try:
        out = os.path.join(rd, "argv.json")
        rec = os.path.join(rd, "rec.py")
        with io.open(rec, "w", encoding="utf-8") as f:
            f.write("import sys, json\n"
                    "open(r'%s', 'w', encoding='utf-8')"
                    ".write(json.dumps(sys.argv[1:]))\n"
                    % out.replace("\\", "\\\\"))
        subprocess.run('python "%s" %s "%s"' % (rec, params, out),
                       shell=True, capture_output=True, timeout=60)
        with io.open(out, encoding="utf-8") as f:
            got = f.read()
        _add("base64 载荷经命令行往返后完整（含 = 未被吃掉）",
             b64 in got, "长度 %d" % len(got))
    except Exception as e:
        _add("参数编码往返检查未崩溃", False, repr(e))
    finally:
        shutil.rmtree(rd, ignore_errors=True)

    # ---- ④ 非提权启动撞 740 时必须自动升级为提权 ----
    # 740 是本项目常态（副本带 uac_admin 清单），不该当异常抛给用户。
    seen = {}
    real = updater._runas_launch

    def fake_runas(exe, args):
        seen["args"] = args
        return True, "已请求管理员权限启动更新"

    updater._runas_launch = fake_runas
    try:
        # 模拟 Popen 抛 740：用一个不存在的可执行路径触发 OSError 不够真实，
        # 这里直接验证 elevated=True 分支确实走 _runas_launch 且载荷完整
        ok, msg = updater.launch_updater(
            r"C:\nonexistent\Yuhub_updater.exe", payload, elevated=True)
        args = seen.get("args") or []
        _add("elevated=True 走提权启动", ok is True, msg)
        _add("提权启动传参完整（模式 + 载荷）",
             len(args) == 2 and args[0] == "--apply-update" and args[1] == b64,
             "参数 %r" % (args[:1],))
    except Exception as e:
        _add("提权启动分支未崩溃", False, repr(e))
    finally:
        updater._runas_launch = real

    # ---- ⑤ 残留目录清理：只删超龄的，不碰本进程与新鲜目录 ----
    tmp = tempfile.gettempdir()
    old_d = os.path.join(tmp, "Yuhub_upd_888801")
    new_d = os.path.join(tmp, "Yuhub_upd_888802")
    try:
        for p in (old_d, new_d):
            shutil.rmtree(p, ignore_errors=True)
            os.makedirs(p, exist_ok=True)
        past = time.time() - 7200
        os.utime(old_d, (past, past))          # 2 小时前 → 该删
        n = updater._sweep_stale_updater_dirs(keep_pid=os.getpid())
        _add("残留清理：只删超龄目录", n == 1, "清理 %d 个" % n)
        _add("残留清理：超龄目录已删", not os.path.isdir(old_d))
        _add("残留清理：新鲜目录保留", os.path.isdir(new_d))
    except Exception as e:
        _add("残留清理检查未崩溃", False, repr(e))
    finally:
        shutil.rmtree(old_d, ignore_errors=True)
        shutil.rmtree(new_d, ignore_errors=True)

    # ---- ⑥ 替换器日志可写：静默故障必须留痕 ----
    try:
        lp = updater.updater_log_path()
        _add("替换器日志路径可用", bool(lp), lp)
        if lp:
            updater._ulog("自检写入")
            _add("替换器日志可写（故障可追溯）", os.path.isfile(lp))
    except Exception as e:
        _add("替换器日志检查未崩溃", False, repr(e))


def _test_ack_handshake():
    """回归：主进程必须拿到替换器的启动暗号，才允许自己退出。

    2026-10-19 修复的核心。`launch_updater` 返回 True 只代表
    ShellExecuteW 接受了启动请求 —— UAC 被点否、替换器被安全软件
    拦截、进程秒退时它照样返回成功，而主进程过去不等任何确认就
    退出自己，用户看到的就是「下载完了、窗口关了、版本没变」。
    （2026-10-15 物证：%TEMP% 留下 Yuhub_upd_5588 而替换器从未收尾。）

    契约：
      * stage_updater 把暗号路径填进 payload["ack_file"]；
      * apply_update_main 解码成功后**第一件事**就是写暗号
        （早于任何文件操作，也早于坏包体检——替换器确实起来了）；
      * 载荷坏到解不开（rc=2）时不会有暗号；
      * wait_for_updater_ack：见到暗号 → True；等不到 → False。
    """
    d = tempfile.mkdtemp(prefix="Yuhub_ack_")
    try:
        # ---- ① 载荷带暗号字段，stage_updater 自动填 ----
        payload = updater.build_request(r"C:\x\new.exe", r"C:\x\Yuhub.exe", 1)
        _add("build_request 带 ack_file 字段（默认空）",
             "ack_file" in payload and payload["ack_file"] == "",
             "ack_file=%r" % (payload.get("ack_file"),))

        staged = updater.stage_updater(payload)
        ack = payload.get("ack_file") or ""
        _add("stage_updater 把暗号路径写进载荷",
             bool(ack) and os.path.isfile(staged),
             "staged=%s ack=%s" % (os.path.basename(staged), ack))
        _add("暗号与替换器副本同目录（同生共死）",
             bool(ack) and os.path.dirname(ack) == os.path.dirname(staged),
             "%s vs %s" % (os.path.dirname(ack), os.path.dirname(staged)))

        # ---- ② 陈旧暗号必须被清掉（否则主进程会被旧文件骗到）----
        if ack:
            with open(ack, "w", encoding="utf-8") as f:
                f.write("stale\n")
            payload2 = updater.build_request(r"C:\x\new.exe",
                                             r"C:\x\Yuhub.exe", 1)
            updater.stage_updater(payload2)
            _add("stage_updater 清掉上一次残留的暗号",
                 payload2.get("ack_file") == ack
                 and not os.path.exists(ack),
                 "ack 仍存在=%s" % os.path.exists(ack))

        # ---- ③ 坏包路径：暗号也要写（替换器确实起来了），rc=9 ----
        work = os.path.join(d, "apply")
        os.makedirs(work, exist_ok=True)
        target = os.path.join(work, "Target.exe")
        shutil.copy2(updater.current_exe(), target)
        original = open(target, "rb").read()
        new_exe = os.path.join(work, "incoming.exe")
        with open(new_exe, "wb") as f:
            f.write(b"NOT-A-PE" * 64)
        ack3 = os.path.join(work, "ack3")
        pl = updater.build_request(new_exe, target, 0,
                                   new_version="vtest", ack_file=ack3)
        rc = updater.apply_update_main(updater.encode_payload(pl))
        _add("坏包被拦下（rc=9）且暗号已写（替换器确实运行过）",
             rc == 9 and os.path.isfile(ack3),
             "rc=%r ack=%s" % (rc, os.path.isfile(ack3)))
        _add("暗号里记的是替换器自己的 PID",
             os.path.isfile(ack3)
             and open(ack3, encoding="utf-8").read().strip().isdigit(),
             open(ack3, encoding="utf-8").read().strip()
             if os.path.isfile(ack3) else "(无)")
        _add("坏包路径目标文件原封不动",
             open(target, "rb").read() == original,
             "%d 字节" % len(original))

        # ---- ④ 载荷解不开 → 没有暗号（主进程会等待超时）----
        rc2 = updater.apply_update_main("bm90LWpzb24=")
        _add("坏载荷 rc=2（旧行为保持）", rc2 == 2, "rc=%r" % (rc2,))

        # ---- ⑤ wait_for_updater_ack 两种结局 ----
        _add("暗号已出现 → 立即可退出",
             updater.wait_for_updater_ack(ack3, timeout=1.0) is True)
        missing = os.path.join(work, "never.ack")
        t0 = time.monotonic()
        got = updater.wait_for_updater_ack(missing, timeout=0.4,
                                           interval=0.05)
        _add("暗号始终不出现 → 超时返回 False（主进程不许退出）",
             got is False and time.monotonic() - t0 < 2.0,
             "got=%r 耗时=%.2fs" % (got, time.monotonic() - t0))
        _add("旧载荷没有暗号路径 → 不等待（向后兼容）",
             updater.wait_for_updater_ack("", timeout=0.1) is True)

        # ---- ⑥ 暗号路径能原样穿过命令行往返（base64 载荷完整性）----
        b64 = updater.encode_payload(pl)
        back = updater.decode_payload(b64)
        _add("暗号路径经 base64 往返后一字不差",
             back.get("ack_file") == ack3,
             "%r vs %r" % (back.get("ack_file"), ack3))
    except Exception as e:
        _add("暗号握手自检未崩溃", False, repr(e))
    finally:
        shutil.rmtree(d, ignore_errors=True)
        # 本测试真的 stage 过一份替换器副本（源码态是 python.exe），
        # 顺手把自己那个清掉，别给 %TEMP% 留垃圾
        try:
            shutil.rmtree(os.path.join(
                tempfile.gettempdir(), "Yuhub_upd_%d" % os.getpid()),
                ignore_errors=True)
        except OSError:
            pass


def _test_replace_fallbacks():
    """回归：③b 就位失败不能放弃治疗 + 回滚要带重试。

    两个真实场景：
      * **跨盘**：exe 装在 D:\、更新包在 C:\\LOCALAPPDATA，改名就位
        可能直接失败。过去这里只能回滚 —— 装在非系统盘的用户
        每次更新都静默失败。现在先试「拷贝 + 大小校验 + 删源」。
      * **回滚被短暂占用**：新版秒退后文件句柄可能还被杀软占着，
        一次 os.replace 失败就放弃会把用户留在「没有 exe」的状态。
        现在回滚带 5 次重试。
    """
    d = tempfile.mkdtemp(prefix="Yuhub_fb_")
    try:
        # ---- ① _copy_into_place 单测：正常路径 ----
        src = os.path.join(d, "src.bin")
        dst = os.path.join(d, "dst.bin")
        data = os.urandom(4096)
        with open(src, "wb") as f:
            f.write(data)
        ok_copy = updater._copy_into_place(src, dst)
        _add("拷贝兜底：正常路径成功", ok_copy and os.path.isfile(dst)
             and not os.path.exists(src)
             and open(dst, "rb").read() == data,
             "ok=%s dst=%s" % (ok_copy, os.path.isfile(dst)))

        # 源不存在 → 明确失败，不半吊子
        ok_missing = updater._copy_into_place(os.path.join(d, "nope.bin"),
                                              os.path.join(d, "dst2.bin"))
        _add("拷贝兜底：源不存在时返回 False", ok_missing is False,
             "返回 %r" % (ok_missing,))

        # ---- ② 集成：os.replace 就位失败时必须走拷贝兜底 ----
        work = os.path.join(d, "apply")
        os.makedirs(work, exist_ok=True)
        target = os.path.join(work, "Target.exe")
        shutil.copy2(updater.current_exe(), target)
        original = open(target, "rb").read()
        new_exe = os.path.join(work, "incoming.exe")
        with open(new_exe, "wb") as f:
            f.write(_fake_pe_payload())
        pl = updater.build_request(new_exe, target, 0, new_version="vtest")

        real_replace = os.replace
        state = {"n": 0}

        def flaky_replace(a, b):
            # 只针对「新包 → 目标」这一次失败（模拟跨盘 MoveFileEx 报错），
            # 其余（含回滚的 .bak → target）原样放行
            if a == new_exe and b == target:
                state["n"] += 1
                if state["n"] == 1:
                    raise OSError(17, "系统无法将文件移到不同的磁盘驱动器。",
                                  None, 17)
            return real_replace(a, b)

        spy = {"called": False}
        real_copy = updater._copy_into_place

        def spy_copy(a, b):
            spy["called"] = True
            return real_copy(a, b)

        updater._copy_into_place = spy_copy
        os.replace = flaky_replace
        try:
            rc = updater.apply_update_main(updater.encode_payload(pl))
        finally:
            os.replace = real_replace
            updater._copy_into_place = real_copy

        _add("③b 改名失败时走了拷贝兜底", spy["called"] is True,
             "替换尝试=%d" % state["n"])
        # 兜底成功 → 流程继续走到「启动新版」；假 PE 起不来 → rc=7 + 回滚。
        # 若没有兜底，这里会是 rc=6（直接回滚）。用 rc 区分两条路。
        _add("兜底后流程继续（启动失败 rc=7 而非就位失败 rc=6）",
             rc == 7, "rc=%r" % (rc,))
        _add("回滚后目标恢复原样（用户至少还能用旧版）",
             os.path.isfile(target)
             and open(target, "rb").read() == original,
             "target 存在=%s" % os.path.isfile(target))
        _add("回滚后不遗留 .bak",
             not os.path.exists(target + ".bak"),
             "bak=%s" % os.path.exists(target + ".bak"))

        # ---- ③ 回滚重试：第一次 os.replace 失败不应放弃 ----
        target2 = os.path.join(work, "Target2.exe")
        bak2 = target2 + ".bak"
        with open(target2, "wb") as f:
            f.write(b"OLD")
        with open(bak2, "wb") as f:
            f.write(b"NEWBACKUP")
        tries = {"n": 0}

        def flaky_rollback(a, b):
            if a == bak2 and b == target2:
                tries["n"] += 1
                if tries["n"] < 3:
                    raise OSError(32, "另一个程序正在使用此文件。", None, 32)
            return real_replace(a, b)

        os.replace = flaky_rollback
        try:
            rolled = updater._rollback(bak2, target2, True)
        finally:
            os.replace = real_replace
        _add("回滚带重试：前两次失败后第三次成功",
             rolled is True and tries["n"] == 3
             and open(target2, "rb").read() == b"NEWBACKUP",
             "rolled=%s 尝试=%d" % (rolled, tries["n"]))
    except Exception as e:
        _add("替换兜底自检未崩溃", False, repr(e))
    finally:
        shutil.rmtree(d, ignore_errors=True)


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

    # ---- 启动暗号的 UI 接线必须存在（2026-10-19 修复的核心 glue）----
    # 纯函数 wait_for_updater_ack 由 _test_ack_handshake 覆盖；这里钉住
    # 主窗口那一层：_apply_update 之后必须走暗号等待，而不是直接退出。
    # ⚠️ 源码引用检查只在**源码态**做：冻结态只有 .pyc，inspect.getsource
    # 必然失败（那不是产品问题，是断言自己的局限）。
    try:
        import inspect

        from ui.main_window import MainWindow
        has_wait = callable(getattr(MainWindow,
                                    "_await_updater_ack_then_quit", None))
        _add("主窗口有暗号等待方法（_await_updater_ack_then_quit）",
             has_wait, "存在=%s" % has_wait)
        if getattr(sys, "frozen", False):
            _add("_apply_update 接线到暗号等待（冻结态验方法存在）",
                 has_wait, "冻结态无源码可查，方法存在=%s" % has_wait)
        else:
            try:
                src = inspect.getsource(MainWindow._apply_update)
            except Exception:  # noqa: BLE001
                src = ""
            wired = ("_await_updater_ack_then_quit" in (src or "")
                     and "ack_file" in (src or ""))
            _add("_apply_update 接线到暗号等待（拿到确认才退出）", wired,
                 "源码引用=%s" % wired)
    except Exception as e:
        _add("主窗口暗号接线检查未崩溃", False, repr(e))




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
        _test_elevation_decision()
        _test_ack_handshake()
        _test_replace_fallbacks()
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
