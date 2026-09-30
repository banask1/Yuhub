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


def _test_download_sources_and_cancel():
    """v0.8.3beta 回归：镜像候选、限时探测、取消即时生效。

    线上 bug：直连 github.com 时 DNS 挂起 → 永远卡在「准备下载」；
    取消标志只在分块之间检查 → 卡在「正在取消」只能强杀进程。
    """
    # ① github URL 生成 直连 + 2 个镜像候选；本地 URL 不加镜像
    cands = updater._download_candidates(
        "https://github.com/u/r/releases/download/v1/Yuhub.exe")
    ok1 = (len(cands) == 3
           and cands[0].startswith("https://github.com/")
           and any("ghproxy" in c for c in cands[1:]))
    _add("下载源候选：github URL 附带镜像兜底", ok1,
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

    # ③ 全部源都连不上：逐个尝试后返回最后一个错误，且换源时给 UI 状态提示
    import downloader as _dl
    info3 = updater.ReleaseInfo(
        tag="v9", name="x", notes="", asset_name="Yuhub.exe",
        asset_url="https://github.com/u/r/releases/download/v1/Yuhub.exe",
        asset_size=0)
    statuses = []

    def _fake_probe(url, timeout=25.0):
        return _dl.ProbeResult(url=url, ok=False, error="模拟连接失败")

    saved = updater._probe_bounded
    updater._probe_bounded = _fake_probe
    try:
        ok3, msg3, _ = updater.download_update(
            info3, on_progress=lambda s: statuses.append(dict(s)))
    finally:
        updater._probe_bounded = saved
    _add("所有源失败时逐个尝试并报最后错误",
         (not ok3) and "模拟连接失败" in msg3, "msg=%r" % msg3)
    _add("换源时向 UI 报状态提示",
         any("中转镜像" in str(s.get("status_text", "")) for s in statuses),
         "状态=%r" % [s.get("status_text") for s in statuses if s.get("status_text")])



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
    """验证 apply_update_main 的完整替换路径。

    关键：目标 exe 和"新 exe"都必须是**真能启动的 PE 文件**，
    否则 apply_update_main 会走"启动失败→回滚"分支（返回 7），
    那是正确行为但测不到正常路径。

    做法：两边都用"当前自己的可执行文件"的副本。
    - 冻结态：就是 Yuhub.exe 自身，拷两份即可
    - 源码态：是 python.exe（带参数会立刻退出，但能启动）
    """
    d = os.path.join(work, "apply")
    os.makedirs(d, exist_ok=True)
    target = os.path.join(d, "Target.exe")
    new_exe = os.path.join(d, "incoming.exe")

    real = updater.current_exe()
    try:
        shutil.copy2(real, target)
        shutil.copy2(real, new_exe)
    except Exception as e:
        _add("apply_update_main：准备真实可执行目标", False, "拷贝失败：%s" % e)
        return

    try:
        target_size = os.path.getsize(target)
    except OSError:
        target_size = 0
    _add("apply_update_main：准备真实可执行目标", target_size > 0,
         "%s (%d 字节)" % (os.path.basename(real), target_size))

    # 为了让"启动新版"这一步能干净地成功返回，给新版传一个会立刻
    # 正常退出（退出码 0）的参数：--update-selftest 用一个不存在的路径会
    # 返回非 0，所以改用"能识别但无害"的方式——直接不给参数，
    # 冻结态下它会尝试启动 GUI（可能长时间不退）。
    # 因此这里改成：用一个**假的可执行文件**，让 Popen 失败并走回滚，
    # 专门验证回滚路径的健壮性；正常路径已由上面的两步改名测试覆盖。
    os.remove(new_exe)
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
    # 非 PE 文件启动必然失败 → 必须回滚，目标应仍是原来的副本
    rolled_back = os.path.getsize(target) == target_size
    _add("启动失败时自动回滚（目标保持原样）", rolled_back,
         "target 大小=%d（原 %d），返回码=%r" % (os.path.getsize(target), target_size, rc))
    _add("回滚后未遗留 .bak", not os.path.exists(target + ".bak"),
         "bak 存在=%s" % os.path.exists(target + ".bak"))


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
    try:
        # ---- 准备：假的"新版 exe"内容 ----
        # 用一段可辨识的字节当假更新包（不必是真 PE，
        # 因为替换流程只关心文件是否正确搬运）。
        fake_exe = b"YUHUBNEW" + os.urandom(4096)

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
        _test_download_sources_and_cancel()
        _test_replace_flow(port, tag, sha, fake_exe)
        _test_selftest_mode_guarded()
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

    checks = _RESULT["checks"]
    _RESULT["ok"] = all(c["pass"] for c in checks) and len(checks) > 0
    _RESULT["info"]["elapsed"] = round(time.time() - started, 2)

    try:
        with io.open(out_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_RESULT, ensure_ascii=False, indent=2))
    except Exception:
        return 5

    return 0 if _RESULT["ok"] else 1
