# -*- coding: utf-8 -*-
"""打包后自检：hosts 网络加速（`Yuhub.exe --hosts-selftest <结果json路径>`）。

为什么需要它
------------
这个功能动了**系统 hosts 文件**——写错一行就可能让某个域名解析到本机或
不可路由地址，用户表现是"Steam 打不开了"，而且极难联想到是这里出的错。
所以静态证据 + 运行时断言双保险：

  * **AST 静态扫描** hostsaccel.py：提权进程侧绝不碰注册表 / 杀进程；
    `subprocess` 只允许用于 `ipconfig /flushdns`（白名单到 argv 字面量）；
    提权入口里绝不出现网络调用。
  * **UI 回调纪律**：accel_page 的引擎回调（后台线程）函数体只允许
    `emit(...)` —— 引擎事件回调一律来自后台线程，界面改动必须留在
    主线程槽（本项目硬规则 3，offscreen 下违规不崩、只安静写坏界面）。
  * **所有写盘断言都打在临时文件上**：绝不修改真实 hosts、不联网、
    不弹 UAC。网络相关的函数全部打桩后测逻辑。

返回码：0 全部通过 / 1 有断言失败 / 2 参数错误 / 4 结果写盘失败
"""

import ast
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import hostsaccel as ha  # noqa: E402

# ---------------------------------------------------------------------------
# 框架
# ---------------------------------------------------------------------------
_result = {"ok": True, "checks": [], "info": {}}


def chk(name, cond, detail=""):
    if not isinstance(cond, bool):
        cond = bool(cond)
    _result["checks"].append({"name": name, "pass": cond, "detail": str(detail)[:400]})
    if not cond:
        _result["ok"] = False


def _dump(result, out_file):
    """写结果文件（detail 里混进非 JSON 类型时降级为字符串，绝不写半截）。"""
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        for c in result.get("checks", []):
            c["detail"] = str(c.get("detail", ""))[:300]
        result["checks"].append({
            "name": "结果可序列化", "pass": False,
            "detail": "详情里有无法 JSON 化的对象"})
        result["ok"] = False
        text = json.dumps(result, ensure_ascii=False, indent=2)
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return 4
    return 0 if result.get("ok") else 1


def _module_source(filename):
    """读随包源码（冻结态从 datas 解出来的一侧找）。"""
    cands = [
        os.path.join(HERE, filename),
        os.path.join(HERE, "ui", "pages", filename),
        os.path.join(HERE, "_MEIPASS_temp_marker_nonexist", filename),
    ]
    base = os.path.dirname(os.path.abspath(ha.__file__))
    cands.insert(0, os.path.join(base, filename))
    cands.insert(1, os.path.join(base, "ui", "pages", filename))
    for p in cands:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# ① AST 静态扫描
# ---------------------------------------------------------------------------
def _ast_checks():
    src = _module_source("hostsaccel.py")
    if src is None:
        chk("hostsaccel 源码随包可读（AST 扫描前提）", False, "读不到 hostsaccel.py")
        return
    tree = ast.parse(src)

    # 1) import 全部是标准库白名单（+ 本项目自己的 winadmin）
    #    winadmin：进程提权判定（TokenElevation）。单独成模块是因为
    #    cleaner/uninstaller/memopt 也踩了同一个坑，不能各自抄一份 ctypes。
    allowed = {
        "base64", "concurrent.futures", "ctypes", "datetime", "ipaddress",
        "json", "os", "platform", "re", "shutil", "socket", "subprocess",
        "sys", "tempfile", "threading", "time", "urllib.request",
        "winadmin",
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    chk("hostsaccel 只 import 标准库白名单（不引第三方）",
        imported <= allowed, sorted(imported - allowed))

    # 2) subprocess 只允许 ipconfig /flushdns
    bad_runs = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"):
            argv = node.args[0] if node.args else None
            ok = (isinstance(argv, (ast.List, ast.Tuple))
                  and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                          for e in argv.elts)
                  and [e.value for e in argv.elts] == ["ipconfig", "/flushdns"])
            if not ok:
                bad_runs.append(getattr(node, "lineno", "?"))
    chk("subprocess 只用于 ipconfig /flushdns（argv 字面量白名单）",
        not bad_runs, bad_runs)

    # 3) 提权入口：不联网、不建套接字
    net_names = {"urlopen", "build_opener", "create_connection", "socket"}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "elevated_hosts_main":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id in net_names:
                    found.append(sub.id)
                if isinstance(sub, ast.Attribute) and sub.attr in net_names:
                    found.append(sub.attr)
    chk("提权入口不联网（无 urlopen / create_connection / socket）",
        not found, found)

    # 4) 不碰注册表 / 不终止进程
    forbidden = {"winreg", "RegSetValue", "TerminateProcess", "ExitProcess",
                 "taskkill", "TerminateJobObject"}
    text_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden:
            text_names.append(node.id)
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            text_names.append(node.attr)
    chk("hostsaccel 不碰注册表 / 不终止进程", not text_names, text_names)


def _page_callback_check():
    """accel_page 的引擎回调（后台线程）只允许 emit。

    ⚠️ v1.0.4：开/关统一成"意图应用"，后台入口变成 _apply_worker_direct /
    _apply_worker_elevated / _prefetch_map / _proxy_resolver。
    ⚠️ v1.0.5：退出清理在管理员模式下改成同进程直写 → 多出 _shutdown_apply_direct
    （它同样跑在工作线程里，同样不许碰界面）。这份名单必须同步扩 —— 否则新抽
    出来的函数就成了"静态扫描盲区"：在盲区里写 setText 不会红，等于这道防线
    凭空消失（"它绿了"≠"它测到了"）。
    """
    src = _module_source("accel_page.py")
    if src is None:
        chk("accel_page 源码随包可读（回调纪律扫描前提）", False, "读不到 accel_page.py")
        return
    tree = ast.parse(src)
    bad = []
    scanned = []
    forbidden = ("setText", "setVisible", "setEnabled",
                 "toast", "addWidget", "update", "repaint")
    for node in ast.walk(tree):
        # 找所有可能开后台线程的函数 —— 注意 v1.0.4 的教训：**函数体自身**
        # 也跑在后台线程（_apply_worker_direct 整个就是线程入口），
        # 只扫嵌套函数会漏掉直接写在函数体里的 setText（变异测试抓到的盲区）
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_proxy_resolver", "_apply_worker_direct",
                "_apply_worker_elevated", "_prefetch_map",
                "_shutdown_apply_direct"):
            scanned.append(node.name)
            for stmt in ast.walk(node):
                # 后台线程里出现直接改界面的调用（setText/setVisible/
                # setEnabled/toast）就是违规 —— 只允许 emit
                if (isinstance(stmt, ast.Call)
                        and isinstance(stmt.func, ast.Attribute)
                        and stmt.func.attr in forbidden):
                    bad.append("%s:%s" % (node.name, stmt.func.attr))
    chk("后台线程回调只 emit、绝不直接改界面（硬规则 3 的静态防线）",
        not bad, bad)
    # 五条入口必须**都被真的扫到**，否则上面的"绿"只是名单写错了
    need = {"_proxy_resolver", "_apply_worker_direct",
            "_apply_worker_elevated", "_prefetch_map",
            "_shutdown_apply_direct"}
    chk("回调纪律扫描覆盖全部后台入口（名单不能漏）",
        need <= set(scanned), sorted(need - set(scanned)))


def _proxy_ast_checks():
    """hostssniproxy 的 AST 静态扫描：纯标准库、不碰子进程/注册表、不解密。"""
    src = _module_source("hostssniproxy.py")
    if src is None:
        chk("hostssniproxy 源码随包可读（AST 扫描前提）", False,
            "读不到 hostssniproxy.py")
        return
    tree = ast.parse(src)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    chk("hostssniproxy 只 import 标准库白名单（socket/threading/time）",
        imported <= {"socket", "threading", "time"}, sorted(imported))

    # 承诺"不解密流量"：绝不能出现 ssl 模块（ssl 只有解密/校验时才需要，
    # 透传代理只需要裸 socket）。这条同时挡住了未来误加 MITM 的路。
    chk("hostssniproxy 不 import ssl（透传不解密，无需证书）",
        "ssl" not in imported, sorted(imported))
    chk("hostssniproxy 不用 subprocess", "subprocess" not in imported,
        sorted(imported))

    forbidden = {"winreg", "RegSetValue", "TerminateProcess", "ExitProcess",
                 "taskkill", "system", "popen"}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden:
            found.append(node.id)
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            found.append(node.attr)
    chk("hostssniproxy 不碰注册表 / 不终止进程 / 不开子进程", not found, found)


# ---------------------------------------------------------------------------
# ② 域名清单与配置一致性
# ---------------------------------------------------------------------------
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _domain_checks():
    chk("服务清单就是 steam / github 两个", ha.SERVICES == ("steam", "github"),
        ha.SERVICES)
    chk("DOMAINS 覆盖全部服务", set(ha.DOMAINS) == set(ha.SERVICES),
        sorted(ha.DOMAINS))
    for svc, domains in ha.DOMAINS.items():
        chk("%s 域名清单非空" % svc, len(domains) >= 5, len(domains))
        chk("%s 域名无重复" % svc, len(domains) == len(set(domains)), "")
        bad = [d for d in domains if not _DOMAIN_RE.match(d)]
        chk("%s 域名全部是小写合法主机名" % svc, not bad, bad)
    req_steam = {"store.steampowered.com", "api.steampowered.com",
                 "avatars.steamstatic.com", "steamcommunity.com"}
    chk("Steam 清单含商店/API/头像/社区关键域名",
        req_steam <= set(ha.DOMAINS["steam"]), sorted(req_steam - set(ha.DOMAINS["steam"])))
    req_gh = {"github.com", "api.github.com", "codeload.github.com",
              "objects.githubusercontent.com", "raw.githubusercontent.com"}
    chk("GitHub 清单含主站/API/下载/资源关键域名",
        req_gh <= set(ha.DOMAINS["github"]), sorted(req_gh - set(ha.DOMAINS["github"])))
    for svc, pool in ha.FALLBACK_IPS.items():
        unknown = [d for d in pool if d not in ha.DOMAINS[svc]]
        chk("%s 兜底池的键都在域名清单里" % svc, not unknown, unknown)
        bad_ips = [(d, ip) for d, ips in pool.items() for ip in ips
                   if not ha.is_valid_public_ipv4(ip)]
        chk("%s 兜底池全部是合法公网 IP" % svc, not bad_ips, bad_ips)
    for name, tpl in ha.DOH_ENDPOINTS:
        chk("DoH 接口 %s 是 https 且带 {domain} 占位" % name,
            tpl.startswith("https://") and "{domain}" in tpl, tpl)


# ---------------------------------------------------------------------------
# ③ IP 校验 / DoH 解析（纯函数）
# ---------------------------------------------------------------------------
def _ip_checks():
    good = ["23.15.142.182", "185.199.108.133", "140.82.112.3", "8.8.8.8"]
    bad = ["127.0.0.1", "192.168.1.1", "10.0.0.1", "172.16.0.1", "169.254.1.1",
           "224.0.0.1", "0.0.0.0", "240.0.0.1", "100.64.0.1", "198.18.0.5",
           "203.0.113.9", "8.8.8", "abc", "1.2.3.4.5", "", "300.1.1.1"]
    for ip in good:
        chk("公网 IP %s 通过" % ip, ha.is_valid_public_ipv4(ip), "")
    for ip in bad:
        chk("非公网 IP %s 被拒" % (ip or "(空串)"), not ha.is_valid_public_ipv4(ip), "")


def _doh_checks():
    chk("A 记录被采纳", ha.parse_doh_answer(
        {"Answer": [{"type": 1, "data": "23.15.142.182"}]}) == ["23.15.142.182"], "")
    chk("CNAME(type 5) 被跳过", ha.parse_doh_answer(
        {"Answer": [{"type": 5, "data": "cname.example.com"},
                    {"type": 1, "data": "23.15.142.182"}]}) == ["23.15.142.182"], "")
    chk("私网应答被跳过", ha.parse_doh_answer(
        {"Answer": [{"type": 1, "data": "192.168.1.1"}]}) == [], "")
    chk("重复 IP 去重", ha.parse_doh_answer(
        {"Answer": [{"type": 1, "data": "8.8.8.8"},
                    {"type": 1, "data": "8.8.8.8"}]}) == ["8.8.8.8"], "")
    chk("None 安全", ha.parse_doh_answer(None) == [], "")
    chk("非 dict 安全", ha.parse_doh_answer("oops") == [], "")
    chk("缺 Answer 键安全", ha.parse_doh_answer({"Status": 0}) == [], "")


# ---------------------------------------------------------------------------
# ②b 秒加速原语（instant_entries / 缓存时间戳 / entries_match）
# ---------------------------------------------------------------------------
def _fast_path_checks():
    """v1.0.3「秒加速」的引擎侧支撑，逐条钉住。

    用户要的是"和 Steam++ 一样点一下就用"。Steam++ 快的根因是**启用路径上
    没有任何网络测量**：hosts 只写 127.0.0.1，真实 IP 交给本地代理按 SNI
    现场解析。所以 instant_entries 必须做到"不联网就能给出完整条目"——
    下面这几条就是"它真的没联网、也真的给全了"的证据。
    """
    steam_domains = ha.DOMAINS[ha.SERVICE_STEAM]

    # ---- 反向代理模式：全量覆盖，零测速 ----
    e, m = ha.instant_entries(ha.SERVICE_STEAM, "proxy")
    chk("秒加速(proxy)：条目数等于域名清单长度",
        len(e) == len(steam_domains), (len(e), len(steam_domains)))
    chk("秒加速(proxy)：全部写 127.0.0.1",
        bool(e) and all(ip == ha.PROXY_IP for ip, _d in e), e[:2])
    chk("秒加速(proxy)：没有缺失域名（不测速也能写全）", m == [], m)
    chk("秒加速(proxy)：域名顺序与清单一致",
        [d for _i, d in e] == steam_domains, "")
    e_g, m_g = ha.instant_entries(ha.SERVICE_GITHUB, "proxy")
    chk("秒加速(proxy)：github 同样全量",
        len(e_g) == len(ha.DOMAINS[ha.SERVICE_GITHUB]) and m_g == [], len(e_g))

    # ---- 直连模式：缓存 + 内置兜底池 ----
    e2, m2 = ha.instant_entries(ha.SERVICE_STEAM, "direct", {})
    chk("秒加速(direct)：无缓存时用内置兜底池，且如实报出缺失",
        bool(e2) and bool(m2) and len(e2) + len(m2) == len(steam_domains),
        (len(e2), len(m2)))
    chk("秒加速(direct)：兜底给出的都是合法公网 IP",
        all(ha.is_valid_public_ipv4(ip) for ip, _d in e2), e2)
    full = {d: "23.15.142.182" for d in steam_domains}
    e3, m3 = ha.instant_entries(ha.SERVICE_STEAM, "direct", full)
    chk("秒加速(direct)：缓存齐全时零缺失（可以秒开）",
        m3 == [] and len(e3) == len(full), (len(e3), m3))
    dirty = dict(full)
    dirty["store.steampowered.com"] = "127.0.0.1"
    e4, _m4 = ha.instant_entries(ha.SERVICE_STEAM, "direct", dirty)
    chk("秒加速(direct)：缓存里的 127.0.0.1 被拒（回落到兜底池）",
        all(ip != "127.0.0.1" for ip, _d in e4), e4[:2])
    e5, _m5 = ha.instant_entries(ha.SERVICE_STEAM, "direct",
                                 {"evil.example.com": "8.8.8.8"})
    chk("秒加速(direct)：清单外域名不进条目",
        all(d in steam_domains for _i, d in e5), e5)
    chk("未知服务 → 空结果不抛异常",
        ha.instant_entries("bilibili", "proxy") == ([], []), "")

    # ---- entries_match：内容一致就不写盘（重复拨开关也是瞬时的） ----
    chk("entries_match：同集合不同顺序算一致",
        ha.entries_match([("a", "x"), ("b", "y")], [("b", "y"), ("a", "x")]), "")
    chk("entries_match：内容不同判不一致",
        not ha.entries_match([("a", "x")], [("b", "y")]), "")
    chk("entries_match：空 vs 空算一致", ha.entries_match([], []), "")
    chk("entries_match：空 vs 非空判不一致",
        not ha.entries_match([], [("a", "x")]), "")
    chk("entries_match：脏输入不抛异常", ha.entries_match(None, "oops") is False, "")

    # ---- 缓存时间戳 / 新鲜度 / 新旧格式兼容 ----
    real_path = ha._map_cache_path
    p = os.path.join(tempfile.gettempdir(),
                     "yuhub_fastpath_map_%d.json" % os.getpid())
    probe_domain = ha.DOMAINS[ha.SERVICE_GITHUB][0]
    ha._map_cache_path = lambda: p
    try:
        if os.path.exists(p):
            os.remove(p)
        chk("无缓存 → age=None（当作不新鲜）", ha.map_cache_age() is None, "")
        chk("无缓存 → 不新鲜", ha.map_cache_fresh() is False, "")
        ha.save_map_cache({probe_domain: "140.82.112.3"})
        age = ha.map_cache_age()
        chk("刚写的缓存 age 很小", age is not None and age < 60.0, age)
        chk("刚写的缓存算新鲜", ha.map_cache_fresh(), "")
        chk("TTL 收成 0 时不新鲜（新鲜度判定真的在看时间）",
            ha.map_cache_fresh(0) is False, "")
        chk("落盘格式带 ts 与 map 两个键",
            set(json.load(open(p, encoding="utf-8"))) == {"ts", "map"}, "")
        chk("CACHE_TTL 是正数秒", isinstance(ha.CACHE_TTL, int)
            and ha.CACHE_TTL > 0, ha.CACHE_TTL)
        # 旧格式（v1.0.2 及以前的纯 dict）必须还能读
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({probe_domain: "140.82.112.3"}, fh)
        chk("旧格式仍能读出映射",
            ha.load_map_cache() == {probe_domain: "140.82.112.3"},
            ha.load_map_cache())
        chk("旧格式没有时间戳 → age=None（不敢当新鲜用）",
            ha.map_cache_age() is None, "")
        os.remove(p)
    finally:
        ha._map_cache_path = real_path

    # ---- optimize_service_map：后台预热用的 {domain: ip} ----
    real_opt = ha.optimize_service

    def _stub_one(svc, doh_timeout=2.5, tcp_timeout=1.2, on_domain=None):
        return [("1.1.1.1", ha.DOMAINS[svc][0], 12.0)]

    try:
        ha.optimize_service = _stub_one
        mp = ha.optimize_service_map(ha.SERVICE_STEAM)
        chk("optimize_service_map 返回 {domain: ip}",
            mp == {steam_domains[0]: "1.1.1.1"}, mp)
        ha.optimize_service = (
            lambda svc, doh_timeout=2.5, tcp_timeout=1.2, on_domain=None: [])
        chk("optimize_service_map 全失败 → 空 dict",
            ha.optimize_service_map(ha.SERVICE_STEAM) == {}, "")
    finally:
        ha.optimize_service = real_opt


# ---------------------------------------------------------------------------
# ④ hosts 清洗 / 写入（全部打在临时文件上）
# ---------------------------------------------------------------------------
def _make_lines():
    return [
        "127.0.0.1 localhost",
        "",
        "# a comment",
        "1.2.3.4\tstore.steampowered.com # 旧散行（制表符）",
        "5.6.7.8 store.steampowered.com",
        "5.6.7.8 Store.SteamPowered.com",          # 大小写变体也要清
        "9.9.9.9\tsome.example.com",
        ha._BLOCK_BEGIN.format(svc="steam"),
        "23.15.142.182\tstore.steampowered.com",
        ha._BLOCK_END.format(svc="steam"),
        ha._BLOCK_BEGIN.format(svc="github"),
        "140.82.112.3\tgithub.com",
        ha._BLOCK_END.format(svc="github"),
    ]


def _clean_checks():
    lines = _make_lines()
    cleaned, removed = ha.clean_hosts_lines(lines, "steam")
    text = "\n".join(cleaned)
    chk("清洗后不再含 steam 任何条目", "steam" not in text, text[:120])
    chk("清洗不移除别人的 github 区块",
        ha._BLOCK_BEGIN.format(svc="github") in text
        and ha._BLOCK_END.format(svc="github") in text, "")
    chk("清洗不移除无关条目与注释",
        "localhost" in text and "some.example.com" in text and "a comment" in text, "")
    chk("清洗计数正确（3 条散行 + 区块 3 行）", removed == 6, removed)
    cleaned2, removed2 = ha.clean_hosts_lines(cleaned, "steam")
    chk("清洗幂等（再洗一遍零移除）", removed2 == 0, removed2)

    cleaned_g, removed_g = ha.clean_hosts_lines(lines, "github")
    text_g = "\n".join(cleaned_g)
    chk("清洗 github 不动 steam 的散行", "store.steampowered.com" in text_g, "")
    chk("清洗 github 移除 github 区块（3 行）", removed_g == 3, removed_g)


def _write_checks(tmp_root):
    tmp = os.path.join(tmp_root, "hosts")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("127.0.0.1 localhost\r\n")

    r1 = ha.write_service_block("steam", [("23.15.142.182", "store.steampowered.com")],
                                hosts_file=tmp, do_backup=False)
    chk("首次写入成功", r1["ok"] and not r1["error"], r1)
    # 带备份写第二次（备份目录指向临时位置）
    bak_dir = os.path.join(tmp_root, "bak")
    os.makedirs(bak_dir, exist_ok=True)
    real_bak, real_path = ha.backup_dir, ha.hosts_path
    ha.backup_dir = lambda: bak_dir
    ha.hosts_path = lambda: tmp
    try:
        r2 = ha.write_service_block("steam", [("23.49.104.48", "store.steampowered.com")],
                                    hosts_file=tmp, do_backup=True)
        chk("二次写入成功且返回了备份路径", r2["ok"] and r2["backup"].startswith(bak_dir), r2)
        bak_name = os.path.basename(r2["backup"])
        chk("备份命名 hosts_时间戳.bak",
            bool(re.match(r"^hosts_\d{8}_\d{6}\.bak$", bak_name)), bak_name)
    finally:
        ha.backup_dir, ha.hosts_path = real_bak, real_path

    # 读回时 newline="" 才不会把 \r\n 折叠成 \n（universal newlines 陷阱）
    text = open(tmp, encoding="utf-8", newline="").read()
    chk("写入幂等：区块只出现一对", text.count("[steam] ===") == 2, text)
    chk("写入幂等：同域名只保留一条", text.count("store.steampowered.com") == 1, "")
    chk("原有条目无损", "localhost" in text, "")
    chk("Windows 下保持 CRLF", "\r\n" in text, "")
    chk("不残留临时文件", not os.path.exists(tmp + ".yuhub.tmp"), "")

    chk("service_enabled 正向判定", ha.service_enabled("steam", path=tmp), "")
    chk("service_enabled 负向判定", not ha.service_enabled("github", path=tmp), "")
    chk("current_entries 读回一致",
        ha.current_entries("steam", path=tmp) == [("23.49.104.48", "store.steampowered.com")],
        ha.current_entries("steam", path=tmp))

    # 双服务共存
    r3 = ha.write_service_block("github", [("140.82.112.3", "github.com")],
                                hosts_file=tmp, do_backup=False)
    chk("github 写入成功", r3["ok"], r3)
    text3 = open(tmp, encoding="utf-8").read()
    chk("双服务共存：两对区块", text3.count("[steam] ===") == 2
        and text3.count("[github] ===") == 2, "")
    chk("双服务共存：steam 条目未受损", text3.count("store.steampowered.com") == 1, "")

    r4 = ha.clean_service("steam", hosts_file=tmp, do_backup=False)
    text4 = open(tmp, encoding="utf-8").read()
    chk("clean_service 移除 steam 且 github 完好",
        r4["ok"] and "store.steampowered.com" not in text4
        and "github.com" in text4 and text4.count("[github] ===") == 2, r4)
    r5 = ha.clean_service("steam", hosts_file=tmp, do_backup=False)
    chk("clean_service 幂等（没有条目时 ok 且零移除）",
        r5["ok"] and r5["removed"] == 0, r5)


# ---------------------------------------------------------------------------
# ④b 落盘阶梯（对标 Steam++：原地写优先，原子替换兜底）
#
# 用户机器（火绒）实测：tmp + os.replace 第一次成功、之后每次都
# WinError 5「拒绝访问」—— 安全软件的 hosts 保护盯的是"删除/重命名系统
# 文件"这一下。所以真正的修复不是"多弹一次 UAC"，而是**换掉落盘动作**。
# 这一段把三件事钉死：
#   ① 首选原地写（不删不改名）；② 原地写被拦时能兜底替换；
#   ③ 两条路都被拦时**明确失败且不写坏 hosts**（还要给出放行提示）。
# ---------------------------------------------------------------------------
def _commit_checks(tmp_root):
    tmp = os.path.join(tmp_root, "commit_hosts")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("127.0.0.1 localhost\r\n")
    real_inplace, real_replace = ha._write_inplace, ha._write_replace

    def denied(_target, _text):
        raise OSError(13, "拒绝访问")          # WinError 5 的 errno 形态

    try:
        # ① 正常路径：首选原地写，且不留 tmp
        c1 = ha.commit_hosts(tmp, "127.0.0.1 localhost\r\n# new\r\n")
        chk("落盘首选原地写（Steam++ 同款，不删不改名）",
            c1["ok"] and c1["method"] == "inplace", c1)
        chk("原地写后内容正确",
            open(tmp, encoding="utf-8", newline="").read()
            == "127.0.0.1 localhost\r\n# new\r\n", "")
        chk("原地写不产生 .yuhub.tmp", not os.path.exists(tmp + ".yuhub.tmp"), "")

        # ② 原地写被安全软件拦 → 兜底原子替换
        ha._write_inplace = denied
        c2 = ha.commit_hosts(tmp, "127.0.0.1 localhost\r\n# via-replace\r\n")
        chk("原地写被拦时兜底原子替换成功",
            c2["ok"] and c2["method"] == "replace", c2)
        chk("兜底替换后内容正确",
            "# via-replace" in open(tmp, encoding="utf-8", newline="").read(), "")

        # ③ 两条路都被拦 → 明确失败 + 不写坏原文件 + 给出放行提示
        ha._write_replace = denied
        before = open(tmp, encoding="utf-8", newline="").read()
        c3 = ha.commit_hosts(tmp, "127.0.0.1 localhost\r\n# never\r\n")
        after = open(tmp, encoding="utf-8", newline="").read()
        chk("两条落盘路径都被拦时如实报失败", not c3["ok"], c3)
        chk("失败时 hosts 内容一字未动", before == after, (before[:40], after[:40]))
        chk("失败信息同时点出两种落盘路径",
            "inplace" in c3["error"] and "replace" in c3["error"], c3["error"])
        chk("失败信息给出安全软件放行指引",
            "信任区" in c3["error"], c3["error"][:80])
        chk("都失败后不留 .yuhub.tmp", not os.path.exists(tmp + ".yuhub.tmp"), "")

        # ④ 落地后校验不一致 → 还原原内容并报失败（原地写没有原子性，
        #    这一步是它的保险；打桩成"假装成功但什么都没写"）
        ha._write_inplace = lambda _t, _x: None
        ha._write_replace = denied
        before4 = open(tmp, encoding="utf-8", newline="").read()
        c4 = ha.commit_hosts(tmp, "127.0.0.1 localhost\r\n# mismatch\r\n",
                             original=before4)
        after4 = open(tmp, encoding="utf-8", newline="").read()
        chk("落地后校验不一致时明确失败（不假装成功）", not c4["ok"], c4)
        chk("校验不一致时按原内容还原", before4 == after4, "")
    finally:
        ha._write_inplace, ha._write_replace = real_inplace, real_replace

    # ⑤ 只读属性会被自动摘掉（Steam++ 也是先 SetAttributes(Normal)）
    ro = os.path.join(tmp_root, "commit_ro")
    with open(ro, "w", encoding="utf-8", newline="") as fh:
        fh.write("127.0.0.1 localhost\r\n")
    try:
        os.chmod(ro, 0o444)
    except OSError:
        pass
    c5 = ha.commit_hosts(ro, "127.0.0.1 localhost\r\n# ro-cleared\r\n")
    chk("只读 hosts 也能写入（先摘只读属性）",
        c5["ok"] and "# ro-cleared" in open(ro, encoding="utf-8", newline="").read(),
        c5)
    try:
        os.chmod(ro, 0o666)
    except OSError:
        pass

    # ⑥ 诊断字段：apply 结果要能看出"这一轮有没有管理员权限、用的哪条路"
    ip = os.path.join(tmp_root, "commit_intent.json")
    real_ip = ha.intent_path
    ha.intent_path = lambda: ip
    try:
        ha.write_intent({"steam": True}, path=ip)
        res = ha.apply_intent(ha.read_intent(ip), hosts_file=tmp)
    finally:
        ha.intent_path = real_ip
    chk("apply 结果带 admin 诊断字段", "admin" in res, list(res))
    chk("apply 结果带落盘方式字段",
        res["services"]["steam"].get("method") in ("inplace", "replace"),
        res["services"]["steam"])

    # ⑦ 反复「开 → 关」必须在 hosts 里留下**零痕迹**（含空行）
    #    为什么专门测：write_service_block 会在区块前插一个空行做分隔，而
    #    clean_service 只摘区块、不摘那个空行 —— 每开关一次就多留一个空行。
    #    用户机器的 hosts 尾巴上实测已经挂了 13 个空行（853 字节里 26 字节
    #    全是它）。这条把"多轮循环后逐字节回到原样"钉死，谁把 _tidy_lines
    #    去掉就红。
    tidy = os.path.join(tmp_root, "commit_tidy")
    base = (
        "# Copyright (c) 1993-2009 Microsoft Corp.\r\n"
        "#\r\n"
        "# This is a sample HOSTS file used by Microsoft TCP/IP for Windows.\r\n"
        "\r\n"
        "#\t127.0.0.1       localhost\r\n"
        "#\t::1             localhost\r\n"
    )

    def _write_base():
        with open(tidy, "w", encoding="utf-8", newline="") as fh:
            fh.write(base)

    _write_base()
    rounds = []
    for _ in range(6):
        w = ha.write_service_block(
            "steam", [(ha.PROXY_IP, "store.steampowered.com")],
            hosts_file=tidy, do_backup=False, proxy=True)
        rounds.append(bool(w.get("ok")))
        c = ha.clean_service("steam", hosts_file=tidy, do_backup=False)
        rounds.append(bool(c.get("ok")))
    after7 = open(tidy, encoding="utf-8", newline="").read()
    chk("反复开关 6 轮，每轮写入/清理都成功", all(rounds), rounds)
    chk("反复开关 6 轮后 hosts 逐字节回到原样（不累积空行）",
        after7 == base, repr(after7[-48:]))

    # 写入态也不该堆积空行：区块前后都不允许出现**连续两个空行**
    # （原文的那个空行 + 区块前的分隔空行各算一个，但它们不相邻）
    _write_base()
    ha.write_service_block(
        "steam", [(ha.PROXY_IP, "store.steampowered.com")],
        hosts_file=tidy, do_backup=False, proxy=True)
    mid = open(tidy, encoding="utf-8", newline="").read()
    chk("写入态：不存在连续空行（区块前后都不堆积）",
        "\r\n\r\n\r\n" not in mid, mid.count("\r\n\r\n\r\n"))
    ha.clean_service("steam", hosts_file=tidy, do_backup=False)


# ---------------------------------------------------------------------------
# ⑤ 提权入口（载荷校验 + 临时路径下的 clean 全流程）
# ---------------------------------------------------------------------------
def _isolation_off():
    """临时关掉自检隔离，返回恢复函数。

    **只能用在"所有写盘路径都已打桩成临时文件"的用例里**（提权入口的进程内
    测试就是这种：hosts_path/backup_dir/result_file 全部被钉到 tmp_root）。
    隔离本身是为了兜住"某个套件忘了打桩"的情况；这些用例是**故意**要跑
    提权入口的真实逻辑，所以需要精确地、临时地把闸门抬起来 —— 抬起来之前
    打桩必须已经在位（见 _elevated_checks 的外层注释）。
    """
    real = ha._ISOLATED_ROOT
    ha._ISOLATED_ROOT = ""

    def resume():
        ha._ISOLATED_ROOT = real

    return resume


def _elevated_checks(tmp_root):
    import base64 as b64mod

    def payload(obj):
        return b64mod.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")

    # ⚠️ 整个提权入口检查都在**临时 hosts** 上跑。elevated_hosts_main 是
    #    提权入口，它内部直接调 hosts_path()，**不看 is_admin** —— 只要
    #    有一个用例忘了打桩、而校验又被破坏（变异测试就会这么干），
    #    写盘就会落到真实 hosts 上。v1.0.2 真实踩过：变异抹掉 proxy 标记后
    #    "代理模式带真实公网 IP" 那条断言（当时用 result_file="x" 且没打桩）
    #    真的把 store.steampowered.com 写进了系统 hosts，项目根还多出个 x。
    #    这里在函数入口统一钉住临时路径，函数出口还原。
    _tmp_hosts = os.path.join(tmp_root, "g_elev_hosts")
    _tmp_bak = os.path.join(tmp_root, "g_elev_bak")
    os.makedirs(_tmp_bak, exist_ok=True)
    with open(_tmp_hosts, "w", encoding="utf-8", newline="") as fh:
        fh.write("127.0.0.1 localhost\r\n")
    _rp, _rb = ha.hosts_path, ha.backup_dir
    ha.hosts_path = lambda: _tmp_hosts
    ha.backup_dir = lambda: _tmp_bak

    # 结果文件一律走临时目录：这些断言预期返回 3，但"预期"不是保障 ——
    # 真被写上也不能落到项目根（曾经凭空出现一个名为 x 的文件）。
    def _rf(name):
        return os.path.join(tmp_root, name)

    try:
        _resume = _isolation_off()
        try:
            return _elevated_checks_inner(tmp_root, payload, _rf)
        finally:
            _resume()
    finally:
        ha.hosts_path, ha.backup_dir = _rp, _rb


def _elevated_checks_inner(tmp_root, payload, _rf):
    chk("非 base64 载荷 → 2", ha.elevated_hosts_main("!!bad!!") == 2, "")
    chk("缺 result_file → 3", ha.elevated_hosts_main(payload(
        {"mode": "clean", "service": "steam", "result_file": ""})) == 3, "")
    chk("非法 mode → 3", ha.elevated_hosts_main(payload(
        {"mode": "kill", "service": "steam",
         "result_file": _rf("r_kill.json")})) == 3, "")
    chk("未知服务 → 3", ha.elevated_hosts_main(payload(
        {"mode": "clean", "service": "bilibili",
         "result_file": _rf("r_unk.json")})) == 3, "")
    chk("私网 IP 拒绝写入 → 3", ha.elevated_hosts_main(payload(
        {"mode": "write", "service": "steam", "result_file": _rf("r_priv.json"),
         "entries": [["192.168.1.1", "store.steampowered.com"]]})) == 3, "")
    chk("清单外域名拒绝写入 → 3", ha.elevated_hosts_main(payload(
        {"mode": "write", "service": "steam", "result_file": _rf("r_off.json"),
         "entries": [["8.8.8.8", "evil.example.com"]]})) == 3, "")

    # 代理模式的载荷校验。会真正走到写盘的用例必须先把 hosts_path /
    # backup_dir 打桩到临时位置 —— 这套自检铁律：绝不碰真实 hosts。
    tmp3 = os.path.join(tmp_root, "hosts3")
    bak3 = os.path.join(tmp_root, "bak3")
    os.makedirs(bak3, exist_ok=True)
    with open(tmp3, "w", encoding="utf-8", newline="") as fh:
        fh.write("127.0.0.1 localhost\r\n")
    real_path3, real_bak3 = ha.hosts_path, ha.backup_dir
    ha.hosts_path = lambda: tmp3
    ha.backup_dir = lambda: bak3
    try:
        chk("代理模式 + 127.0.0.1 + 清单内域名 → 放行(rc=0)",
            ha.elevated_hosts_main(payload(
                {"mode": "write", "service": "steam",
                 "result_file": os.path.join(tmp_root, "res_p.json"),
                 "proxy": True,
                 "entries": [["127.0.0.1", "store.steampowered.com"]]})) == 0, "")
        text_p = open(tmp3, encoding="utf-8", newline="").read()
        entry_lines = [ln for ln in text_p.splitlines()
                       if "store.steampowered.com" in ln
                       and not ln.strip().startswith("#")]
        chk("代理模式写入的条目就是 127.0.0.1",
            len(entry_lines) == 1
            and entry_lines[0].split() == ["127.0.0.1", "store.steampowered.com"],
            entry_lines)
        chk("代理模式区块注释标明了模式", "本地反向代理模式" in text_p, "")
        chk("代理模式写入成功后 current_entries 可读回",
            ha.current_entries("steam", path=tmp3)
            == [(ha.PROXY_IP, "store.steampowered.com")], "")
        chk("代理模式条目的 entries_mode == proxy",
            ha.entries_mode(ha.current_entries("steam", path=tmp3)) == "proxy", "")
    finally:
        ha.hosts_path, ha.backup_dir = real_path3, real_bak3
    # 模式与地址不一致 / 清单外 —— 这几种**理论上**在写盘之前就被拒绝，
    # 但绝不能依赖"理论上"：
    #   ⚠️ v1.0.2 真实踩过。变异测试把 proxy 标记抹掉后，这三条里
    #   "代理模式带真实公网 IP" 的校验会退化成直连分支 → 公网 IP 合法 →
    #   **真的写进了真实 hosts**（因为这里当时没打桩 hosts_path，
    #   `result_file` 还写成了字面量 "x"，项目根目录凭空多出一个 x 文件）。
    #   铁律：凡真调 elevated_hosts_main 的地方，无论是否预期被拒，
    #   hosts_path / backup_dir 一律打桩到临时位置，result_file 也走临时目录。
    tmp4 = os.path.join(tmp_root, "hosts4")
    bak4 = os.path.join(tmp_root, "bak4")
    os.makedirs(bak4, exist_ok=True)
    with open(tmp4, "w", encoding="utf-8", newline="") as fh:
        fh.write("127.0.0.1 localhost\r\n")
    real_path4, real_bak4 = ha.hosts_path, ha.backup_dir
    ha.hosts_path = lambda: tmp4
    ha.backup_dir = lambda: bak4
    try:
        chk("代理模式带真实公网 IP → 3（模式与地址必须一致）",
            ha.elevated_hosts_main(payload(
                {"mode": "write", "service": "steam",
                 "result_file": os.path.join(tmp_root, "res_rej1.json"),
                 "proxy": True,
                 "entries": [["23.15.142.182", "store.steampowered.com"]]})) == 3, "")
        chk("直连模式带 127.0.0.1 → 3（不能绕过公网校验）",
            ha.elevated_hosts_main(payload(
                {"mode": "write", "service": "steam",
                 "result_file": os.path.join(tmp_root, "res_rej2.json"),
                 "entries": [["127.0.0.1", "store.steampowered.com"]]})) == 3, "")
        chk("代理模式 + 清单外域名 → 3", ha.elevated_hosts_main(payload(
            {"mode": "write", "service": "steam",
             "result_file": os.path.join(tmp_root, "res_rej3.json"), "proxy": True,
             "entries": [["127.0.0.1", "evil.example.com"]]})) == 3, "")
    finally:
        ha.hosts_path, ha.backup_dir = real_path4, real_bak4
    # 这几条本该被拒 → 临时 hosts 不该有任何改动（顺带证明"拒绝发生在写盘前"）
    chk("被拒的写入没有改动 hosts（拒绝必须发生在写盘之前）",
        "store.steampowered.com" not in open(tmp4, encoding="utf-8").read(), "")

    # clean 全流程：hosts_path / backup_dir 都指到临时位置，绝不碰真实文件
    tmp = os.path.join(tmp_root, "hosts2")
    bak_dir = os.path.join(tmp_root, "bak2")
    result_file = os.path.join(tmp_root, "result.json")
    os.makedirs(bak_dir, exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("1.2.3.4\tstore.steampowered.com\r\n")
    real_path, real_bak = ha.hosts_path, ha.backup_dir
    ha.hosts_path = lambda: tmp
    ha.backup_dir = lambda: bak_dir
    try:
        rc = ha.elevated_hosts_main(payload(
            {"mode": "clean", "service": "steam", "result_file": result_file}))
        with open(result_file, encoding="utf-8") as fh:
            res = json.load(fh)
    finally:
        ha.hosts_path, ha.backup_dir = real_path, real_bak
    chk("提权 clean 全流程 rc=0", rc == 0, rc)
    chk("提权 clean 结果 ok 且记录了移除数", res.get("ok") and res.get("removed", 0) >= 1, res)
    chk("提权 clean 确实改了临时 hosts",
        "store.steampowered.com" not in open(tmp, encoding="utf-8").read(), "")


# ---------------------------------------------------------------------------
# ⑥ 测速与优选逻辑（打桩，不联网）
# ---------------------------------------------------------------------------
def _net_logic_checks():
    real_latency, real_cands = ha.test_tcp_latency, ha.resolve_candidates
    try:
        ha.test_tcp_latency = lambda ip, port=443, timeout=1.2: {
            "a": 50.0, "b": 10.0, "c": None}.get(ip)
        chk("select_best 选最快 IP", ha.select_best(["a", "b", "c"]) == ("b", 10.0), "")
        ha.test_tcp_latency = lambda ip, port=443, timeout=1.2: None
        chk("全部超时 → None", ha.select_best(["a", "b"]) is None, "")
        chk("空候选 → None", ha.select_best([]) is None, "")

        ha.resolve_candidates = lambda d, s, doh_timeout=2.5: (
            ["1.1.1.1", "2.2.2.2"] if d == "store.steampowered.com" else [])
        ha.test_tcp_latency = lambda ip, port=443, timeout=1.2: {
            "1.1.1.1": 30.0, "2.2.2.2": 80.0}.get(ip)
        got = []
        res = ha.optimize_service("steam", on_domain=lambda *a: got.append(a))
        chk("optimize_service 只输出成功域名且顺序正确",
            res == [("1.1.1.1", "store.steampowered.com", 30.0)], res)
        chk("每个域名都有一次回调", len(got) == len(ha.DOMAINS["steam"]), len(got))
        chk("成功域名回调带 (序号,总数,域名,ip,ms,空)",
            got[0] == (1, len(ha.DOMAINS["steam"]), "store.steampowered.com",
                       "1.1.1.1", 30.0, ""), got[0])
        chk("失败域名回调带错误文案",
            got[1] == (2, len(ha.DOMAINS["steam"]), "login.steampowered.com",
                       "", 0.0, "未找到可用节点"), got[1])
        all_fail = ha.optimize_service("github")   # 打桩后 github 全部无候选
        chk("全部失败 → 空列表", all_fail == [], all_fail)

        # 防污染细节：候选非空但 TCP 全灭（投毒 IP）时必须落到兜底池
        ha.resolve_candidates = lambda d, s, doh_timeout=2.5: (
            ["202.53.137.209", "128.121.243.235"]   # 实测的投毒 IP 形态
            if d == "steamcommunity.com" else [])
        ha.test_tcp_latency = lambda ip, port=443, timeout=1.2: (
            None if ip in ("202.53.137.209", "128.121.243.235") else 100.0)
        res = ha.optimize_domain("steamcommunity.com", "steam")
        chk("候选测速全灭时落到兜底池", res is not None, res)
        chk("兜底池给出的 IP 在池里",
            res and res[0] in ha.FALLBACK_IPS["steam"]["steamcommunity.com"],
            res)
        ha.test_tcp_latency = lambda ip, port=443, timeout=1.2: None
        chk("兜底池也全灭 → None",
            ha.optimize_domain("steamcommunity.com", "steam") is None, "")
    finally:
        ha.test_tcp_latency, ha.resolve_candidates = real_latency, real_cands


def _misc_checks():
    # ⚠️ 必须用 _REAL_HOSTS_PATH：自检期间 ha.hosts_path() 已被 guard 钉到
    #    哨兵路径，直接用它会看到哨兵（这条断言就红过）。这里要验的是
    #    "程序默认指向真实系统 hosts"，与自检期间的打桩无关。
    _hp = _REAL_HOSTS_PATH or ha.hosts_path()
    chk("hosts_path 指向真实存在的系统 hosts", os.path.isfile(_hp), _hp)
    res = ha.flush_dns_cache()
    chk("flush_dns_cache 返回布尔且不抛异常", isinstance(res, bool), res)
    chk("提权开关与 main.py 分发一致", ha._ELEVATED_FLAG == "--hosts-elevated",
        ha._ELEVATED_FLAG)
    # Toast 折行：气泡是 QLabel 且没开 wordWrap，写盘失败那条文案带
    # "加入安全软件信任区" 的处置指引（一百多字），不折行会撑出一条比窗口还宽
    # 的提示条。短文案必须原样不动（否则会污染现有断言里的关键字匹配）。
    try:
        import ui.pages.accel_page as _ap
        long_text = _ap._toast_text("啊" * 100)
        chk("Toast 折行：短文案原样、长文案按定宽折行（不撑破窗口）",
            _ap._toast_text("短文案") == "短文案"
            and long_text.count("\n") >= 2
            and max(len(l) for l in long_text.split("\n")) <= _ap._TOAST_WRAP,
            (len(long_text), long_text.count("\n") + 1))
    except Exception as exc:                            # noqa: BLE001
        chk("accel_page 暴露 Toast 折行helper", False, repr(exc))


# ---------------------------------------------------------------------------
# ⑥b 本地反向代理引擎（hostssniproxy，端到端打在临时端口上）
# ---------------------------------------------------------------------------
def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _proxy_engine_checks():
    import hostssniproxy as hp

    # ---- 纯函数：SNI / Host 解析 ----
    for domain in ("store.steampowered.com", "github.com",
                   "avatars.st.dl.eccdnx.com"):
        hello = hp.build_client_hello(domain)
        chk("ClientHello 往返解析 %s" % domain,
            hp.parse_client_hello_sni(hello) == domain, "")
    chk("大写 SNI 归一成小写",
        hp.parse_client_hello_sni(hp.build_client_hello("GiThUb.CoM"))
        == "github.com", "")
    chk("空输入 → 空串", hp.parse_client_hello_sni(b"") == "", "")
    chk("HTTP 字节不是 TLS → 空串",
        hp.parse_client_hello_sni(b"GET / HTTP/1.1\r\n\r\n") == "", "")
    chk("垃圾字节 → 空串", hp.parse_client_hello_sni(b"\x16\x03\x01\x00\x05xx")
        == "", "")
    chk("截断包 → 空串",
        hp.parse_client_hello_sni(hp.build_client_hello("github.com")[:24])
        == "", "")
    chk("HTTP Host 解析（普通）",
        hp.parse_http_head_host(b"GET /a HTTP/1.1\r\nHost: github.com\r\n\r\n")
        == "github.com", "")
    chk("HTTP Host 去端口",
        hp.parse_http_head_host(b"GET / HTTP/1.1\r\nHost: raw.githubusercontent.com:443\r\n\r\n")
        == "raw.githubusercontent.com", "")
    chk("HTTP 缺 Host → 空串",
        hp.parse_http_head_host(b"GET / HTTP/1.1\r\nUser-Agent: t\r\n\r\n")
        == "", "")

    # ---- 端到端：假上游 echo + 临时端口 ----
    up_holder = []

    def upstream_server():
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        up_holder.append(srv.getsockname()[1])
        while True:
            c, _a = srv.accept()
            def serve(cc):
                try:
                    while True:
                        d = cc.recv(4096)
                        if not d:
                            break
                        cc.sendall(b"ECHO:" + d)
                except OSError:
                    pass
                finally:
                    cc.close()
            threading.Thread(target=serve, args=(c,), daemon=True).start()

    threading.Thread(target=upstream_server, daemon=True).start()
    deadline = time.time() + 5
    while not up_holder and time.time() < deadline:
        time.sleep(0.02)
    chk("假上游起来了", bool(up_holder), up_holder)
    up = up_holder[0]
    hello = hp.build_client_hello("store.steampowered.com")
    http_head = b"GET /a HTTP/1.1\r\nHost: github.com\r\nUser-Agent: t\r\n\r\n"

    tls_port, http_port = _free_port(), _free_port()
    proxy = hp.SniProxy(lambda d: "127.0.0.1", tls_port=tls_port,
                        http_port=http_port, upstream_tls_port=up,
                        upstream_http_port=up)
    ok, why = proxy.start()
    chk("代理启动成功（临时端口）", ok and proxy.running, why)
    time.sleep(0.05)

    c = socket.create_connection(("127.0.0.1", tls_port), timeout=5)
    c.sendall(hello)
    c.settimeout(5)
    chk("TLS 路径：ClientHello 原样透传到上游并被回程转发",
        c.recv(4096) == b"ECHO:" + hello, "")
    c.close()
    c2 = socket.create_connection(("127.0.0.1", http_port), timeout=5)
    c2.sendall(http_head)
    c2.settimeout(5)
    chk("HTTP 路径：请求头原样透传（按 Host 解析）",
        c2.recv(4096) == b"ECHO:" + http_head, "")
    c2.close()
    chk("统计计数 2 条连接", proxy.stats()["conns"] == 2, proxy.stats())
    # 先停掉 proxy 再测"解析失败拒连"：Windows 的 SO_REUSEADDR 允许两个
    # 监听并存，连接会被旧代理抢走 —— 测试顺序必须先释放端口。
    proxy.stop()
    chk("stop 后 running=False", not proxy.running, "")
    chk("stop 后端口可复用", hp.check_port_free(tls_port)[0], "")

    # resolver 解析失败 → 连接被关闭且 errors 计数
    proxy2 = hp.SniProxy(lambda d: None, tls_port=tls_port,
                         http_port=http_port, upstream_tls_port=up,
                         upstream_http_port=up)
    proxy2.start()
    c3 = socket.create_connection(("127.0.0.1", tls_port), timeout=5)
    c3.sendall(hello)
    c3.settimeout(5)
    chk("解析失败时客户端被拒绝（连接关闭）", c3.recv(64) == b"", "")
    c3.close()
    time.sleep(0.15)
    chk("解析失败计入 errors", proxy2.stats()["errors"] >= 1, proxy2.stats())
    proxy2.stop()

    # 端口占用检测：另一个代理起不来且原因明确
    p_hold = hp.SniProxy(lambda d: None, tls_port=tls_port,
                         http_port=http_port)
    okh, _ = p_hold.start()
    chk("持有端口启动成功", okh, "")
    p_other = hp.SniProxy(lambda d: None, tls_port=tls_port,
                          http_port=http_port)
    ok2, why2 = p_other.start()
    chk("端口被占用时启动失败并给出原因",
        not ok2 and "占用" in why2, why2)
    p_hold.stop()
    chk("check_port_free 对空闲端口返回 True", hp.check_port_free(
        _free_port())[0], "")

    # ---- 优选映射缓存（打桩到临时路径） ----
    real_map_path = ha._map_cache_path
    map_file = os.path.join(tempfile.gettempdir(),
                            "yuhub_hosts_map_%d.json" % os.getpid())
    ha._map_cache_path = lambda: map_file
    try:
        if os.path.exists(map_file):
            os.remove(map_file)
        chk("空缓存读出空 dict", ha.load_map_cache() == {}, "")
        chk("save_map_cache 成功写入合法映射",
            ha.save_map_cache({"store.steampowered.com": "23.15.142.182",
                               "github.com": "140.82.112.3"}), "")
        chk("load_map_cache 读回一致",
            ha.load_map_cache() == {"store.steampowered.com": "23.15.142.182",
                                    "github.com": "140.82.112.3"}, "")
        chk("缓存过滤清单外域名",
            ha.save_map_cache({"evil.example.com": "8.8.8.8",
                               "github.com": "140.82.112.3"})
            and "evil.example.com" not in ha.load_map_cache(), "")
        chk("缓存拒绝 127.0.0.1（代理映射必须是真实 IP）",
            ha.save_map_cache({"github.com": "127.0.0.1"})
            and "github.com" not in ha.load_map_cache(), "")
        chk("缓存拒绝非公网 IP",
            ha.save_map_cache({"github.com": "192.168.1.1"})
            and "github.com" not in ha.load_map_cache(), "")
        os.remove(map_file)
    finally:
        ha._map_cache_path = real_map_path

    chk("PROXY_IP 就是 127.0.0.1", ha.PROXY_IP == "127.0.0.1", ha.PROXY_IP)
    chk("entries_mode 空条目", ha.entries_mode([]) == "", "")
    chk("entries_mode 识别代理模式",
        ha.entries_mode([(ha.PROXY_IP, "github.com")]) == "proxy", "")
    chk("entries_mode 识别直连模式",
        ha.entries_mode([("140.82.112.3", "github.com")]) == "direct", "")
    chk("entries_mode 混合模式不给过",
        ha.entries_mode([(ha.PROXY_IP, "github.com"),
                         ("140.82.112.3", "api.github.com")]) == "", "")


# ---------------------------------------------------------------------------
# ⑦ UI 冒烟（offscreen；run_elevated 打桩，绝不弹 UAC）
# ---------------------------------------------------------------------------
def _ui_checks():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QElapsedTimer
    import ui.pages.accel_page as ap
    import hostssniproxy as hp

    app = QApplication.instance() or QApplication([])

    # ---- 全部文件落临时目录（自检绝不能碰真实 hosts / 真实意图文件）----
    real_hosts_path = ha.hosts_path          # ← 注意：外层 guard 已把它钉到
    real_backup_dir = ha.backup_dir          #   哨兵目录，finally 必须原样还回去
    real_intent_path = ha.intent_path
    real_map_path = ha._map_cache_path
    hpath = os.path.join(tempfile.gettempdir(),
                         "yuhub_ui_hosts_%d" % os.getpid())
    ipath = os.path.join(tempfile.gettempdir(),
                         "yuhub_ui_intent_%d.json" % os.getpid())
    map_file = os.path.join(tempfile.gettempdir(),
                            "yuhub_hosts_map_ui_%d.json" % os.getpid())
    ha.hosts_path = lambda: hpath
    ha.intent_path = lambda: ipath
    ha._map_cache_path = lambda: map_file

    def reset_hosts():
        with open(hpath, "w", encoding="utf-8", newline="") as fh:
            fh.write("127.0.0.1 localhost\r\n")
        if os.path.exists(ipath):
            os.remove(ipath)

    reset_hosts()

    # SniProxy 工厂打桩：真实代理，但全部用临时端口（绝不占 443/80，
    # 也不和正在运行的 Steam++ 冲突）
    real_sni_factory = ap.hp.SniProxy
    real_cls = hp.SniProxy
    up_holder = []
    up_ready = threading.Event()

    def upstream_server():
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        up_holder.append(srv.getsockname()[1])
        up_ready.set()
        while True:
            c, _a = srv.accept()
            try:
                c.close()
            except OSError:
                pass

    threading.Thread(target=upstream_server, daemon=True).start()
    up_ready.wait(5)
    up = up_holder[0] if up_holder else _free_port()

    def fake_sni_factory(resolver, tls_port=443, http_port=80, **kw):
        return real_cls(resolver, tls_port=_free_port(),
                        http_port=_free_port(), upstream_tls_port=up,
                        upstream_http_port=up)

    toasts = []
    ap.hp.SniProxy = fake_sni_factory
    page = ap.AccelPage(notify=toasts.append)

    switches = [getattr(c, "_switch", None) for c in page._cards.values()]
    chk("每个服务一张卡片、各带一个开关（同 Steam++ 的开关式交互）",
        len(page._cards) == 2 and all(s is not None for s in switches),
        [type(s).__name__ for s in switches])
    chk("开关默认是关（未加速状态）",
        all(not s.isChecked() for s in switches),
        [s.isChecked() for s in switches])
    chk("直连模式已移除（只保留本地反向代理）",
        not hasattr(page, "_mode_seg") and not hasattr(page, "_current_mode"),
        "accel_page 不应再有模式分段控件")

    def pump_until(cond, ms=8000):
        timer = QElapsedTimer()
        timer.start()
        while timer.elapsed() < ms:
            app.processEvents()
            time.sleep(0.01)
            if cond():
                return True
        return False

    # ---- 打桩：非管理员 + 提权应用可控（模拟真实子进程语义）----
    real_admin_ui = ha.is_admin
    real_flush = ha.flush_dns_cache
    real_elev_apply = ha.run_elevated_apply
    real_apply_intent = ha.apply_intent
    real_optmap = ha.optimize_service_map
    ha.is_admin = lambda: False            # 红线：绝不走真实提权/直写
    ha.flush_dns_cache = lambda: True
    ha.optimize_service_map = lambda svc, on_domain=None: {}   # 预热不碰网

    apply_calls = []
    stub = {"delay": 0.0, "fail": None, "stale_seq": False}

    def fake_elevated_apply(timeout=120.0, path=None):
        # 真实子进程语义：等一段（UAC 窗口）→ 应用**此刻**的最新意图 →
        # 返回带 seq 的结果（page 靠 seq 判断要不要补一轮）
        if stub["delay"]:
            time.sleep(stub["delay"])
        apply_calls.append("apply")
        intent = ha.read_intent(ipath)
        seq = intent["seq"] if intent else 0
        if stub["fail"] == "cancel":
            return {"ok": False, "error": "已取消管理员授权，未做任何改动",
                    "seq": seq}
        if stub["fail"] == "timeout":
            return {"ok": False, "error": "等待提权进程超时（120 秒）",
                    "seq": 0}
        res = real_apply_intent(intent)
        res["seq"] = seq
        if stub["stale_seq"]:
            # 模拟"子进程只应用了旧一档意图"：结果 seq 故意落后一档，
            # page 应当自动补一轮（不会让开关与真实状态错位）
            stub["stale_seq"] = False
            res["seq"] = max(0, seq - 1)
        return res

    ha.run_elevated_apply = fake_elevated_apply

    try:
        # ---- U1 开 → 全量 127.0.0.1 落盘、开关保持开、提示出现 ----
        toasts.clear()
        page._switch_for("steam").setChecked(True)
        ok = pump_until(lambda: not page._apply_running
                        and bool(ha.current_entries("steam")))
        chk("拨开开关 → hosts 写入全部域名的 127.0.0.1", ok,
            len(ha.current_entries("steam")))
        chk("写入条目全量覆盖域名清单",
            [d for _ip, d in ha.current_entries("steam")]
            == list(ha.DOMAINS["steam"]), "")
        chk("开关保持开（意图被尊重，不弹回）",
            page._switch_for("steam").isChecked(), "")
        chk("意图文件已写且 seq 对齐",
            (ha.read_intent(ipath) or {}).get("steam") is True
            and page._intent_seq == (ha.read_intent(ipath) or {}).get("seq"),
            page._intent_seq)
        chk("开启成功有提示", any("已开启" in t for t in toasts), toasts[-2:])

        # resolver 命中缓存：不许触发 optimize_domain
        page._ipmap["store.steampowered.com"] = "1.2.3.4"
        real_opt_dom = ha.optimize_domain
        ha.optimize_domain = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("缓存命中时不应现场解析"))
        try:
            got = page._proxy_resolver("store.steampowered.com")
        finally:
            ha.optimize_domain = real_opt_dom
        chk("resolver 命中缓存直接返回真实 IP", got == "1.2.3.4", got)
        chk("resolver 拒绝清单外域名",
            page._proxy_resolver("evil.com") is None, "")

        # ---- U2 关 → 区块清干净、开关保持关 ----
        toasts.clear()
        page._switch_for("steam").setChecked(False)
        ok = pump_until(lambda: not page._apply_running
                        and not ha.current_entries("steam"))
        chk("拨回开关 → hosts 区块清干净", ok, "")
        chk("开关保持关", not page._switch_for("steam").isChecked(), "")
        chk("关闭成功有提示", any("已关闭" in t for t in toasts), toasts[-2:])

        # ---- U2b 秒加速：启用路径上一次测速都没有（Steam++ 同款）----
        reset_hosts()
        measured = []
        real_optsvc = ha.optimize_service
        ha.optimize_service = (
            lambda svc, doh_timeout=2.5, tcp_timeout=1.2, on_domain=None:
            (measured.append(svc) or []))
        try:
            page._switch_for("steam").setChecked(True)
            ok = pump_until(lambda: not page._apply_running
                            and bool(ha.current_entries("steam")))
            chk("秒加速：拨开开关零测速直接写盘（快就快在这里）",
                ok and "steam" not in measured, (ok, measured))
        finally:
            ha.optimize_service = real_optsvc
            page._switch_for("steam").setChecked(False)
            pump_until(lambda: not page._apply_running
                       and not ha.current_entries("steam"))

        # ---- U3 ★核心回归★ UAC 等待中关掉 → 同一次提权按"关"执行 ----
        reset_hosts()
        toasts.clear()
        del apply_calls[:]
        stub["delay"] = 0.35
        try:
            page._switch_for("steam").setChecked(True)     # t=0 开
            time.sleep(0.12)                               # 应用在跑（UAC 窗口）
            app.processEvents()
            page._switch_for("steam").setChecked(False)    # t=0.12 关（改主意）
            ok = pump_until(lambda: not page._apply_running
                            and not ha.current_entries("steam"))
            chk("UAC 等待中关掉 → 最终状态是关", ok,
                len(ha.current_entries("steam")))
            chk("整个过程只发起一次提权（意图被同一轮吸收，无第二次 UAC）",
                apply_calls == ["apply"], apply_calls)
        finally:
            stub["delay"] = 0.0

        # ---- U4 UAC 取消 → 开关对齐真实状态 + 明确报错；重试能关掉 ----
        reset_hosts()
        toasts.clear()
        # 先造成"已加速"状态（直接写临时 hosts）
        real_apply_intent({"seq": 1, "steam": True})
        page._resync_intent()
        page._set_switch("steam", True)
        app.processEvents()
        stub["fail"] = "cancel"
        page._switch_for("steam").setChecked(False)        # 关 → 取消 UAC
        pump_until(lambda: not page._apply_running)
        chk("UAC 取消后开关对齐 hosts 真实状态（仍开 + 明确提示）",
            page._switch_for("steam").isChecked()
            and any("未完成" in t or "取消" in t for t in toasts),
            (page._switch_for("steam").isChecked(), toasts[-2:]))
        stub["fail"] = None
        page._switch_for("steam").setChecked(False)        # 重试 → 成功
        ok = pump_until(lambda: not page._apply_running
                        and not ha.current_entries("steam"))
        chk("取消后重试能真正关掉", ok, "")

        # ---- U5 结果 seq 落后（子进程退出后意图又变）→ 自动补一轮 ----
        reset_hosts()
        toasts.clear()
        del apply_calls[:]
        stub["stale_seq"] = True
        try:
            page._switch_for("steam").setChecked(True)
            ok = pump_until(lambda: not page._apply_running
                            and bool(ha.current_entries("steam")))
            chk("结果 seq 落后 → 自动补一轮应用（不靠用户再点）",
                ok and apply_calls == ["apply", "apply"], apply_calls)
        finally:
            stub["stale_seq"] = False

        # ---- U6 已是目标状态 → 不发起提权（不弹 UAC）----
        # 先把 U5 遗留的"开"真正关掉，再做重复关的短路检查
        page._switch_for("steam").setChecked(False)
        pump_until(lambda: not page._apply_running
                   and not ha.current_entries("steam"))
        toasts.clear()
        n0 = len(apply_calls)
        page._toggled("steam", False)                      # 已是关，再关一次
        app.processEvents()
        time.sleep(0.1)
        app.processEvents()
        chk("已是默认状态时重复关 → 不发起提权",
            len(apply_calls) == n0
            and any("已是" in t for t in toasts),
            (apply_calls[n0:], toasts[-2:]))

        # ---- U7 代理启动失败 → 开关拨回、不写盘、不发起提权 ----
        toasts.clear()
        n0 = len(apply_calls)
        real_ensure_proxy = page._ensure_proxy
        page._ensure_proxy = lambda: (False, "端口被占用")
        page._switch_for("steam").setChecked(True)
        app.processEvents()
        time.sleep(0.1)
        app.processEvents()
        chk("代理启动失败 → 开关拨回且不写盘、不提权",
            not page._switch_for("steam").isChecked()
            and not ha.current_entries("steam")
            and len(apply_calls) == n0,
            (page._switch_for("steam").isChecked(), apply_calls[n0:]))
        page._ensure_proxy = real_ensure_proxy

        # ---- U8 意图覆盖式写入 + 坏文件拒绝 ----
        reset_hosts()
        s1 = ha.write_intent({"steam": True}, path=ipath)
        s2 = ha.write_intent({"github": False}, path=ipath)
        i2 = ha.read_intent(ipath)
        chk("意图 seq 递增", s2 == s1 + 1 and i2["seq"] == s2, (s1, s2))
        chk("意图是覆盖式（旧服务的键不残留）",
            "steam" not in i2 and i2["github"] is False, i2)
        with open(ipath, "w", encoding="utf-8") as fh:
            fh.write("not json")
        bad1 = ha.read_intent(ipath) is None
        with open(ipath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"steam": True}))          # 缺 seq
        bad2 = ha.read_intent(ipath) is None
        chk("坏意图文件被拒绝（非 JSON / 缺 seq）", bad1 and bad2,
            (bad1, bad2))

        # ---- U9 状态展示（真实临时 hosts 内容）----
        real_apply_intent({"seq": 9, "github": True})
        page.on_shown()
        chk("状态行展示已加速（github）",
            "已加速" in page._cards["github"]._status.text(),
            page._cards["github"]._status.text())
        # 直连条目标注（v1.0.3 及以前写过直连条目，升级后如实标注）
        real_apply_intent({"seq": 10, "github": False})
        ha.write_service_block("github", [("1.2.3.4", "github.com")],
                               hosts_file=hpath, proxy=False)
        page._update_card("github")
        chk("直连条目能被识别并如实标注",
            "已加速" in page._cards["github"]._status.text()
            and "直连" in page._cards["github"]._status.text(),
            page._cards["github"]._status.text())
        real_apply_intent({"seq": 11, "github": False})
        page._update_card("github")
    finally:
        ha.is_admin = real_admin_ui
        ha.flush_dns_cache = real_flush
        ha.run_elevated_apply = real_elev_apply
        ha.apply_intent = real_apply_intent
        ha.optimize_service_map = real_optmap
        ha.hosts_path = real_hosts_path        # 原样还给外层 guard 的哨兵
        ha.backup_dir = real_backup_dir
        ha.intent_path = real_intent_path
        ha._map_cache_path = real_map_path

    # ---- on_shown 自愈：hosts 有 127.0.0.1 条目而代理没跑 → 自动拉起 ----
    # （此时 hosts_path 已还原成哨兵 —— 用 current_entries 打桩控制内容）
    page._stop_proxy()
    chk("手动停代理后 running=False", not page._proxy_running(), "")
    real_entries = ha.current_entries
    try:
        ha.current_entries = lambda svc, path=None: (
            [(ha.PROXY_IP, "github.com")] if svc == "github" else [])
        page.on_shown()
        chk("on_shown 自愈：代理条目还在时自动重启代理",
            page._proxy_running(), "")
    finally:
        ha.current_entries = real_entries

    # ---- shutdown：停代理 + 写意图（全关）+ 同进程直写清理 ----
    # Yuhub 以管理员身份启动，退出清理必须**当前进程就做完** —— 不弹 UAC、
    # 不起提权子进程（退出路径上还去拉子进程，等于"关不干净"）。
    direct_calls2 = []
    real_entries = ha.current_entries
    real_shd2 = page._shutdown_apply_direct
    real_intent2 = ha.intent_path
    try:
        ha.current_entries = lambda svc, path=None: (
            [(ha.PROXY_IP, "github.com")] if svc == "github" else [])
        ha.intent_path = lambda: ipath
        page._intent_state["github"] = True     # 与条目一致
        page._shutdown_apply_direct = lambda: direct_calls2.append("direct")
        page.shutdown()
        pump_until(lambda: direct_calls2, ms=5000)
        chk("shutdown 停掉了代理", not page._proxy_running(), "")
        chk("shutdown 写入全关意图并走同进程直写清理",
            (ha.read_intent(ipath) or {}).get("github") is False
            and direct_calls2 == ["direct"],
            ((ha.read_intent(ipath) or {}), direct_calls2))
    finally:
        ha.current_entries = real_entries
        page._shutdown_apply_direct = real_shd2
        ha.intent_path = real_intent2

    # ---- 应用的串行化：应用在跑时再拨开关，不得开出第二个并发应用 ----
    real_entries_s = ha.current_entries
    real_admin_s = ha.is_admin
    real_ea3 = ha.run_elevated_apply
    concurrent = {"n": 0, "max": 0}
    try:
        ha.current_entries = lambda svc, path=None: []
        ha.is_admin = lambda: False

        def _slow_apply(timeout=120.0, path=None):
            concurrent["n"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["n"])
            time.sleep(0.25)
            concurrent["n"] -= 1
            intent = ha.read_intent(ipath)
            res = real_apply_intent(intent)
            res["seq"] = intent["seq"] if intent else 0
            return res

        ha.run_elevated_apply = _slow_apply
        page._intent_state["steam"] = False
        page._apply_running = False
        page._switch_for("steam").setChecked(True)   # 启动第一个应用（慢）
        pump_until(lambda: page._apply_running and concurrent["n"] == 1,
                   ms=8000)
        page._ensure_apply()      # 应用在跑时再调用一次 → 守卫必须挡住
        pump_until(lambda: not page._apply_running
                   and concurrent["n"] == 0, ms=8000)
        chk("应用严格串行（同时最多一个提权在跑）",
            concurrent["max"] == 1, concurrent)
    finally:
        ha.current_entries = real_entries_s
        ha.is_admin = real_admin_s
        ha.run_elevated_apply = real_ea3
        page._apply_running = False

    # ---- U13 退出清理走同进程直写 ----
    # Yuhub 以管理员身份启动（exe 清单 requireAdministrator，见 Yuhub.spec），
    # 退出清理就在**当前进程**里做完 —— 绝不弹 UAC、绝不拉提权子进程。
    direct_calls = []
    real_shd = page._shutdown_apply_direct
    real_entries5 = ha.current_entries
    real_intent5 = ha.intent_path
    try:
        ha.current_entries = lambda svc, path=None: [(ha.PROXY_IP, "github.com")]
        ha.intent_path = lambda: ipath
        page._intent_state["github"] = True
        page._shutdown_apply_direct = lambda: direct_calls.append("direct")
        page.shutdown()
        pump_until(lambda: direct_calls, ms=3000)
    finally:
        ha.current_entries = real_entries5
        ha.intent_path = real_intent5
        page._shutdown_apply_direct = real_shd
    chk("退出清理走同进程直写（不起提权子进程、不弹 UAC）",
        direct_calls == ["direct"], direct_calls)

    # ---- U14 单实例：第二个实例敲门 → 旧窗口被唤醒 ----
    # 常驻管理员后不再有"提权重启"这条支线，单实例只剩「敲门唤醒」一种用法。
    # 但这条**必须留着**：它验证的是 _probe 的"显式泵事件循环"—— 同进程里
    # `waitForBytesWritten` / `waitForReadyRead` 都不保证把消息推出去，写完
    # 立刻 disconnect 会把字节丢掉。症状极隐蔽："第二个实例确实退出了，
    # 但第一个实例的窗口根本不弹"，且不报任何错。
    import single_instance as si
    skey = "Yuhub-SelfTest-Single-%d" % os.getpid()
    g1 = si.SingleInstance(key=skey)
    got = {"activate": 0}
    g1.activate_requested.connect(
        lambda: got.__setitem__("activate", got["activate"] + 1))
    first = g1.acquire()
    chk("单实例（测试键）：抢到监听", first and g1.listening, first)
    g2 = si.SingleInstance(key=skey)
    probed = g2._probe()
    pump_until(lambda: got["activate"] > 0, ms=3000)
    chk("_probe 递出 activate（第二个实例敲门 → 旧窗口被唤醒）",
        probed and got["activate"] == 1, (probed, got))
    g1.close()
    g2.close()

    if os.path.exists(map_file):
        os.remove(map_file)
    if os.path.exists(ipath):
        os.remove(ipath)
    try:
        os.remove(hpath)
    except OSError:
        pass
    ap.hp.SniProxy = real_sni_factory


# ---------------------------------------------------------------------------
# ② 写盘安全检查（临时文件）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ①b 自检隔离 + 提权判定（v1.0.5 事故的回归防线）
#
# 事故复盘：theme 自检实例化 MainWindow，teardown() → accel_page.shutdown()
# → run_elevated_apply —— **真提权、真写盘**，一次就跑掉了用户真实 hosts 里的
# steam 区块（2091 → 847 字节）。同一个坑 v1.0.2 也踩过一次（变异测试写
# hosts）。所以现在：隔离由入口统一开启，且**自检永远不可能提权**。
# ---------------------------------------------------------------------------
def _isolation_checks():
    chk("自检隔离生效（hosts 路径已钉到临时目录）",
        ha.selftest_isolated() and ha.hosts_path() != ha.real_hosts_path(),
        (ha.selftest_isolated(), ha.hosts_path()))
    chk("隔离时不变量：隔离 hosts 落在临时根目录里",
        os.path.dirname(ha.hosts_path()) == ha.selftest_root(), ha.hosts_path())
    rp = ha.real_hosts_path()
    chk("隔离不影响「真实 hosts 快照」的取路径（红线不能盯错文件）",
        rp and "etc" in rp.lower().replace("/", "\\")
        and os.path.isdir(os.path.dirname(rp)), rp)
    # 提权入口在隔离下一律拒绝 —— 这条是"自检永不写真实系统"的硬保证
    res = ha.run_elevated_apply(timeout=0.1)
    chk("隔离时 run_elevated_apply 直接拒绝（绝不弹 UAC）",
        isinstance(res, dict) and not res.get("ok")
        and "隔离" in (res.get("error") or ""), res)
    chk("隔离时 elevated_hosts_main 入口被拒（双保险）",
        ha.elevated_hosts_main("eA==") == 6, "")
    # 提权判定口径：必须问内核"令牌是否提权"，不能问"账户是不是管理员组"。
    # 打桩成"令牌未提权"后 is_admin 必须跟着变 False —— 谁把它改回
    # IsUserAnAdmin（在管理员账户 + UAC 的机器上恒为 1）这条就会红。
    try:
        import winadmin
        real_te = winadmin._token_elevated
        winadmin._token_elevated = lambda: False
        try:
            follows_token = (ha.is_admin() is False)
        finally:
            winadmin._token_elevated = real_te
        chk("is_admin 跟随 TokenElevation（不是用 IsUserAnAdmin 判组）",
            follows_token, ha.is_admin())
        chk("winadmin 暴露提权判定与陷阱诊断",
            callable(winadmin.is_elevated)
            and callable(winadmin.group_admin_but_not_elevated), "")
    except Exception as exc:                       # pragma: no cover
        chk("winadmin 可导入", False, str(exc))


def _collect_pyside6_refs(root=None):
    """扫源码里出现过的所有 `PySide6.<Sub>` 字样（源码态专用）。

    故意的纯文本扫描，不解析 AST —— 宁可被一句注释误伤、让人把注释改干净，
    也不要漏掉真正会 import 的地方。这个函数存在的理由见
    `_admin_mode_checks` 里那条 excludes 断言的血泪注释。
    """
    import re
    root = root or os.path.dirname(os.path.abspath(__file__))
    skip = {"_bak_dc", ".git", "build", "dist", "__pycache__"}
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            try:
                with open(os.path.join(dirpath, fn), "r",
                          encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
            except OSError:
                continue
            for m in re.findall(r"PySide6\.[A-Za-z0-9_]+", src):
                bits = m.split(".")
                found.add(bits[0] + "." + bits[1])
    return found


def _admin_mode_checks():
    """常驻管理员启动（对标 Steam++ 的 requireAdministrator）。

    事实依据（SteamTools 源码，逐条对过）：
      · `source/SteamTools/app.manifest` 里写死
            <requestedExecutionLevel level="requireAdministrator" uiAccess="false" />
        —— 它的主程序**从启动就常驻管理员**，所以 hosts 写入永远发生在同一个
        已提权进程里：UAC 只在启动弹一次，火绒/360 这类软件也只需要放行同一个
        程序一次。
      · `source/SteamTool.Core/HostsService.cs` 的 UpdateHosts 不做"临时文件 +
        改名替换"，只 `File.SetAttributes(Normal)` + `File.WriteAllLines`
        （原地覆盖写）。第二条由 _commit_checks 守住。

    Yuhub 现在同样以管理员身份启动（PyInstaller 的 `uac_admin=True`），并且
    已把"以管理员身份重启"整条支线删掉。这里守住三件事：
      ① 打包配置确实开着 uac_admin —— 这是"启动即提权"的唯一事实来源；
      ② 提权消息发送口的 restype 约定正确（v1.0.2 血泪，教训不能随函数一起删掉）；
      ③ 代码里不再留有重启链路的任何残骸。
    """
    # ① 打包配置
    #    ⚠️ 冻结态读不到 Yuhub.spec（它不随包），按运行形态分流并单列前置
    #    断言 —— 别让"读不到"伪装成"过了"（本项目在这类坑上栽过）。
    spec_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "Yuhub.spec")
    try:
        with open(spec_path, "r", encoding="utf-8") as fh:
            spec_src = fh.read()
    except OSError:
        spec_src = ""
    if getattr(sys, "frozen", False):
        chk("[前置] 打包态读不到 Yuhub.spec（uac_admin 改由源码态断言覆盖）",
            spec_src == "", "")
    else:
        chk("Yuhub.spec 开启 uac_admin=True（exe 清单 requireAdministrator）",
            "uac_admin=True" in spec_src, "")

    # ② restype 必须是 c_void_p —— v1.0.2「开关拨回就自动弹回」的根因：
    #    HINSTANCE 是指针宽度，ctypes 默认按 c_long 取，高位非零会被截断成
    #    负数/小值 → rc<=32 被误判成"用户取消了 UAC"。常驻管理员后 relaunch
    #    没了，但 hostsaccel 自己的提权入口仍在用这个约定。
    real_ctypes = ha.ctypes

    class _FakeFn:
        def __init__(self, rc):
            self.restype = None
            self.argtypes = None
            self.rc = rc
            self.calls = []

        def __call__(self, *a):
            self.calls.append(a)
            return self.rc

    class _FakeWindll:
        def __init__(self, fn):
            self.shell32 = type("S", (), {"ShellExecuteW": fn})()

    class _FakeCtypes:
        def __init__(self, fn):
            self.windll = _FakeWindll(fn)
            self.c_void_p = real_ctypes.c_void_p
            self.c_wchar_p = real_ctypes.c_wchar_p
            self.c_int = real_ctypes.c_int

    ok_fn = _FakeFn(42)                     # 42 > 32 = 成功
    try:
        ha.ctypes = _FakeCtypes(ok_fn)
        got_ok, err_ok = ha.shell_execute_runas("Yuhub.exe", "--elevated x")
    finally:
        ha.ctypes = real_ctypes
    chk("提权发送口把 ShellExecuteW 的 restype 设成 c_void_p（防 HINSTANCE 截断）",
        ok_fn.restype is real_ctypes.c_void_p, ok_fn.restype)
    chk("提权用 runas 谓词、成功码 >32 判为成功",
        got_ok is True and not err_ok and bool(ok_fn.calls)
        and ok_fn.calls[0][1] == "runas", (got_ok, err_ok, ok_fn.calls[:1]))

    cancel_fn = _FakeFn(5)                  # ≤32 = 用户取消 / 策略拒绝
    try:
        ha.ctypes = _FakeCtypes(cancel_fn)
        got_cancel, err_cancel = ha.shell_execute_runas("Yuhub.exe", "")
    finally:
        ha.ctypes = real_ctypes
    chk("用户取消 UAC（返回码 ≤32）判为失败，绝不假装成功",
        got_cancel is False and bool(err_cancel), (got_cancel, err_cancel))

    # ③ 精简防线：重启链路已整体删除。谁把它加回来（哪怕是复制粘贴的残留）
    #    这条就红 —— "删干净了"和"以为删干净了"必须能被区分开。
    banned = ("RELAUNCH_FLAG", "relaunch_as_admin", "acquire_takeover",
              "request_takeover", "MSG_TAKEOVER", "takeover_requested",
              "_quit_for_relaunch", "_on_relaunch_clicked", "self_command")
    scanned = ("main.py", "single_instance.py", "winadmin.py",
               "ui/pages/accel_page.py")
    sources = {name: _module_source(name) for name in scanned}
    # 前置：四个源文件都必须读得到。读不到会让下面的扫描**空转**，
    # 而断言照样绿 —— 正是"它绿了≠它测到了"。
    missing = sorted(n for n, s in sources.items() if not s)
    chk("[前置] 重启链路扫描的四个源文件都可读（没读到 ≠ 删干净）",
        not missing, missing)
    leftovers = []
    for name, src in sources.items():
        if not src:
            continue
        hit = sorted({b for b in banned if b in src})
        if hit:
            leftovers.append((name, hit))
    chk("重启链路已整体移除（main / single_instance / winadmin / accel_page）",
        not leftovers, leftovers)

    # ④ spec 的 excludes 不能排掉源码里真的出现过的 Qt 子模块。
    #    v1.0.6 血泪：excludes 里顺手排了 `PySide6.QtTest`（注释还理直气壮写着
    #    "不用 QTest 驱动界面"），可 `lan_selftest._check_share_live` 里就有
    #    `from PySide6.QtTest import QTest` —— 冻结态直接
    #    `ModuleNotFoundError`，而十套自检**全绿**、只有 lan 红；更糟的是冻结态
    #    是 GUI 子系统、没有 stderr，异常被 main 的兜底吞成一个光秃秃的 rc=5，
    #    完全无从下手。这条断言就是为了让"排除项"和"真实引用"永远对得上。
    #    ⚠️ 冻结态读不到 Yuhub.spec（不随包），所以只在源码态跑 —— 但冻结态的
    #    lan 自检本身就是这条的端到端验证，两边合起来没有缺口。
    if not getattr(sys, "frozen", False):
        refs = _collect_pyside6_refs()
        clash = sorted(r for r in refs
                       if ("'%s'" % r) in spec_src or ('"%s"' % r) in spec_src)
        chk("[前置] 源码里扫到了 PySide6 子模块引用（扫不到 ≠ 没冲突）",
            bool(refs), sorted(refs))
        chk("spec 的 excludes 没排掉源码真正引用的 Qt 子模块",
            not clash, clash)


def _guard_real_hosts():
    """把"真写系统 hosts"这条路上锁死 —— 自检第一红线。

    血泪（2026-10-05，踩过两次）：
      第一次：测试机是管理员时，`try_write_direct` 因 `is_admin()==True`
        直接放行，绕过打桩的 `run_elevated`，**真的把
        `store.steampowered.com -> 127.0.0.1` 写进 C:\\Windows\\...\\hosts**。
      第二次（v1.0.2 变异测试）：`elevated_hosts_main` 是**提权入口**，
        它内部直接调 `hosts_path()`，**根本不看 is_admin**。自检里三条
        "预期被拒绝"的断言按当时的设计没打桩 hosts_path（因为"反正会被
        拒、不会写盘"）；变异抹掉 proxy 标记后校验退化成直连分支 →
        公网 IP 合法 → 真的落到了真实 hosts。

    所以只钉 is_admin 是**不够的**。这里改成**双保险**：
      1. `is_admin` 钉成 False（所有路径都必须走打桩的提权分支）；
      2. `hosts_path` / `backup_dir` 直接钉到**哨兵路径**（一个临时目录下的
         不存在文件）—— 这样即使某个用例忘了打桩、或者提权入口被直接
         调用，写盘也只会打到一个临时哨兵上。
    用例里的显式打桩（`ha.hosts_path = lambda: tmp`）在 guard 之后执行，
    会正常覆盖哨兵；其 finally 还原到哨兵（而不是真实路径），依旧安全。

    返回 (real_is_admin, real_hosts_path, real_backup_dir) 供收尾还原；
    真实 hosts 的路径另存在 `_REAL_HOSTS_PATH` 供快照函数读取。

    ⚠️ 真实路径要走 `ha.real_hosts_path()` 拿：自检隔离（main.py 的
    `--*-selftest` 入口统一开启）生效后，`ha.hosts_path()` 返回的是隔离
    目录里的 hosts —— 用它做"真实 hosts 快照"等于盯错了文件，这道红线
    会静默失效（"它绿了"≠"它测到了"）。
    """
    global _REAL_HOSTS_PATH, _REAL_BACKUP_DIR, _REAL_HOSTS_BYTES
    real_is_admin = ha.is_admin
    _REAL_HOSTS_PATH = ha.real_hosts_path()
    _REAL_BACKUP_DIR = ha.real_backup_dir()
    # 留存原始字节：万一自检还是污染了真实 hosts（绕过所有打桩），
    # 收尾时用它原样还原。这是最后一道保险，不留污染给用户。
    try:
        with open(_REAL_HOSTS_PATH, "rb") as _fh:
            _REAL_HOSTS_BYTES = _fh.read()
    except OSError:
        _REAL_HOSTS_BYTES = None
    ha.is_admin = lambda: False
    sentinel = os.path.join(tempfile.gettempdir(), "yuhub_selftest_sentinel")
    try:
        os.makedirs(sentinel, exist_ok=True)
    except OSError:
        pass
    ha.hosts_path = lambda: os.path.join(sentinel, "hosts_SENTINEL_DO_NOT_USE")
    ha.backup_dir = lambda: sentinel
    return real_is_admin


# 真实 hosts / 备份目录的路径，只在 _guard_real_hosts 里记一次。
# 快照函数必须用它 —— guard 之后 ha.hosts_path() 已经指向哨兵了。
_REAL_HOSTS_PATH = None
_REAL_BACKUP_DIR = None
# 真实 hosts 的原始字节（最后一道保险用）。
_REAL_HOSTS_BYTES = None


def _restore_real_hosts():
    """把真实 hosts 还原成自检开始前的内容。成功返回 True。"""
    if _REAL_HOSTS_BYTES is None or not _REAL_HOSTS_PATH:
        return False
    try:
        with open(_REAL_HOSTS_PATH, "wb") as fh:
            fh.write(_REAL_HOSTS_BYTES)
        import hashlib
        with open(_REAL_HOSTS_PATH, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest() == \
                hashlib.sha256(_REAL_HOSTS_BYTES).hexdigest()
    except OSError:
        return False


def _real_hosts_snapshot():
    """记一份真实 hosts 的快照（内容 sha256），用于自检前后比对。"""
    import hashlib
    p = _REAL_HOSTS_PATH or ha.hosts_path()
    try:
        with open(p, "rb") as fh:
            data = fh.read()
        return {"path": p, "sha": hashlib.sha256(data).hexdigest(),
                "size": len(data)}
    except OSError as exc:
        return {"path": p, "sha": "", "size": -1, "error": str(exc)}


# ---------------------------------------------------------------------------
def _permission_checks(tmp_root):
    """提权链路与"权限不足"这两个用户报的坑，逐条钉住。

    用户 2026-10-05 反馈：「经常提示权限不够无法写入，写入 hosts 失败」
    「无法恢复默认」。这里守住三件事：
      1. 已经是管理员时**不再重复弹 UAC**，直接本地写（try_write_direct）；
      2. 源码运行且没有打包 exe 时，给出**人话提示**而不是干等 120 秒；
      3. 并发提权的结果文件路径**必须唯一**，不能互相踩掉。
    """
    # ---- 1. 已是管理员 → try_write_direct 应直接写盘、不返回 handled=False
    real_admin = ha.is_admin
    hosts_file = os.path.join(tmp_root, "hosts_admin")
    with open(hosts_file, "w", encoding="utf-8") as fp:
        fp.write("\n")
    try:
        ha.is_admin = lambda: True
        real_path = ha.hosts_path
        ha.hosts_path = lambda: hosts_file
        try:
            handled, res = ha.try_write_direct(
                "write", ha.SERVICE_STEAM,
                [("127.0.0.1", "store.steampowered.com")], proxy=True)
            chk("已是管理员时 try_write_direct 直接处理（不弹 UAC）", handled, "")
            chk("管理员直写真的写进了 hosts", bool(res) and res.get("ok"),
                res)
            body = open(hosts_file, encoding="utf-8").read()
            chk("管理员直写内容含目标域名", "store.steampowered.com" in body, "")
            handled2, res2 = ha.try_write_direct("clean", ha.SERVICE_STEAM)
            chk("管理员直清同样直接处理", handled2 and res2.get("ok"), res2)
            body2 = open(hosts_file, encoding="utf-8").read()
            chk("管理员直清后域名被移除", "store.steampowered.com" not in body2, "")
        finally:
            ha.hosts_path = real_path
    finally:
        ha.is_admin = real_admin

    # ---- 2. 非管理员 → try_write_direct 必须让位给提权（handled=False）
    real_admin = ha.is_admin
    try:
        ha.is_admin = lambda: False
        handled, res = ha.try_write_direct("write", ha.SERVICE_STEAM, [])
        chk("非管理员时 try_write_direct 让位给提权路径",
            handled is False and res is None, (handled, res))
    finally:
        ha.is_admin = real_admin

    # ---- 3. 提权能力检测：源码且无 exe → 明确报错（不是静默超时）
    real_admin, real_exe = ha.is_admin, ha.sys_executable
    try:
        ha.is_admin = lambda: False
        ha.sys_executable = lambda: ""
        ok, why = ha.elevation_capable()
        chk("无可用 exe 时 elevation_capable 返回 False", ok is False, ok)
        chk("无可用 exe 时给出人话原因（提到 Yuhub.exe / build）",
            ("Yuhub.exe" in why or "build" in why), why)
        # run_elevated 在无法提权时应返回带 error 的 dict，而不是 None
        # （sys_executable 已被打桩成空 → 走不到 ShellExecuteW，不会弹 UAC）
        res = ha.run_elevated("write", ha.SERVICE_STEAM, [])
        chk("无法提权时 run_elevated 返回带 error 的结果而非静默 None",
            isinstance(res, dict) and res.get("error"), res)
        # 已是管理员 → elevation_capable 直接放行
        ha.is_admin = lambda: True
        ok2, why2 = ha.elevation_capable()
        chk("已是管理员时 elevation_capable 放行", ok2 and not why2, (ok2, why2))
    finally:
        ha.is_admin, ha.sys_executable = real_admin, real_exe

    # ---- 4. 结果文件路径唯一（并发提权不能互踩）
    p1, p2 = ha._result_path(), ha._result_path()
    chk("每次提权的结果文件路径唯一（防并发互踩）", p1 != p2, (p1, p2))
    chk("结果文件路径带 PID", str(os.getpid()) in os.path.basename(p1), p1)

    # ---- 5. ShellExecuteW 返回值必须按 HINSTANCE（指针宽度）取 --------------
    # 回归（v1.0.2）：ctypes.windll 默认按 c_long 取 ShellExecuteW 的返回，
    # 而它实际返回 HINSTANCE（指针宽度）。只要 Windows 给一个高位非零的句柄，
    # 截断后就会变成负数/小值 → `rc <= 32` 误判成"用户取消了 UAC"，表现为
    # 「点开关没反应、开关弹回去」（用户报的"hosts 关不掉"）。这里钉住：
    #   a) 有 shell_execute_runas 这个统一入口；
    #   b) 它内部显式声明了 restype / argtypes；
    #   c) 成功/失败的边界判定是"NULL 或 ≤32 才算失败"。
    chk("hostsaccel 提供 shell_execute_runas 统一入口",
        callable(getattr(ha, "shell_execute_runas", None)),
        "存在=%s" % hasattr(ha, "shell_execute_runas"))
    import inspect as _insp
    src = _insp.getsource(ha.shell_execute_runas)
    chk("shell_execute_runas 显式声明 restype（防 32 位截断）",
        "restype" in src and "c_void_p" in src, "")
    chk("shell_execute_runas 显式声明 argtypes",
        "argtypes" in src, "")
    chk("shell_execute_runas 把 NULL/≤32 判为失败",
        "<= 32" in src and "if not rc" in src, "")
    # run_elevated 也必须走统一入口（别又退回裸 ShellExecuteW）
    esrc = _insp.getsource(ha.run_elevated)
    chk("run_elevated 改用 shell_execute_runas（不再裸调）",
        "shell_execute_runas" in esrc and "ShellExecuteW" not in esrc, "")


# ---------------------------------------------------------------------------
def _intent_checks(tmp_root):
    """意图文件 + 统一应用（v1.0.4 修「关不掉」的架构核心）。

    全部在临时 hosts / 临时意图文件上跑 —— 意图文件路径也要打桩：
    elevated_apply_main 内部自己 read_intent()，不打桩就碰真实
    %LOCALAPPDATA%\\Yuhub\\hosts_intent.json。
    """
    import base64 as b64mod

    hpath = os.path.join(tmp_root, "it_hosts")
    ipath = os.path.join(tmp_root, "it_intent.json")
    rpath = os.path.join(tmp_root, "it_result.json")
    with open(hpath, "w", encoding="utf-8", newline="") as fh:
        fh.write("127.0.0.1 localhost\r\n")
    _ri, _rp = ha.intent_path, ha._result_path
    _rh, _rb = ha.hosts_path, ha.backup_dir
    ha.intent_path = lambda: ipath
    ha._result_path = lambda: rpath
    ha.hosts_path = lambda: hpath      # 提权 apply 内部自己读 hosts_path()
    ha.backup_dir = lambda: os.path.join(tmp_root, "it_bak")
    # 本段所有写盘路径（hosts/意图/结果/备份）都已打桩到 tmp_root，才敢把
    # 隔离闸门临时抬起，让提权入口的真实逻辑跑起来。
    _resume = _isolation_off()
    try:
        # -- write/read 往返、覆盖式、seq 递增 --
        s1 = ha.write_intent({"steam": True}, path=ipath)
        i1 = ha.read_intent(ipath)
        chk("意图写入后可读回（带 seq）",
            s1 == 1 and i1 == {"seq": 1, "steam": True}, (s1, i1))
        s2 = ha.write_intent({"github": False}, path=ipath)
        i2 = ha.read_intent(ipath)
        chk("意图是覆盖式写入（旧服务的键不残留，防陈旧意图污染）",
            s2 == s1 + 1 and "steam" not in i2 and i2["github"] is False, i2)
        with open(ipath, "w", encoding="utf-8") as fh:
            fh.write("not json")
        chk("非 JSON 意图被拒绝（read_intent 返回 None）",
            ha.read_intent(ipath) is None, "")
        with open(ipath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"steam": True}))
        chk("缺 seq 的意图被拒绝", ha.read_intent(ipath) is None, "")
        chk("没有任何服务键的意图被拒绝",
            ha.write_intent({}, path=ipath) == -1, "")

        # -- apply_intent：开 = 全量代理区块；关 = 清干净；幂等 --
        with open(ipath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": 3, "steam": True, "github": False}))
        r = ha.apply_intent(ha.read_intent(ipath), hosts_file=hpath)
        ent = ha.current_entries("steam", hpath)
        chk("apply 开：写入全量 127.0.0.1 区块",
            r["ok"] and r["services"]["steam"]["action"] == "write"
            and [d for _ip, d in ent] == list(ha.DOMAINS["steam"])
            and all(ip == ha.PROXY_IP for ip, _d in ent),
            (r["services"]["steam"], len(ent)))
        chk("apply 关（本来就没有）→ 幂等跳过",
            r["services"]["github"]["action"] == "skip", r["services"]["github"])
        r2 = ha.apply_intent(ha.read_intent(ipath), hosts_file=hpath)
        chk("apply 幂等：已是目标状态 → 双双跳过不写盘",
            r2["ok"] and r2["services"]["steam"]["action"] == "skip"
            and r2["services"]["github"]["action"] == "skip",
            (r2["services"]["steam"]["action"],
             r2["services"]["github"]["action"]))
        with open(ipath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": 4, "steam": False}))
        r3 = ha.apply_intent(ha.read_intent(ipath), hosts_file=hpath)
        chk("apply 关：清掉区块，hosts 其余内容不动",
            r3["ok"] and r3["services"]["steam"]["action"] == "clean"
            and not ha.current_entries("steam", hpath)
            and "127.0.0.1 localhost" in open(hpath, encoding="utf-8").read(),
            (r3["services"]["steam"]["action"],))

        # -- elevated_hosts_main 的 apply 模式：意图在稳定窗内被吸收 --
        # （「关不掉」的核心：UAC 等待期间改主意，同一次提权按新意图执行）
        with open(ipath, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": 5, "steam": True}))
        payload = json.dumps({"mode": "apply", "result_file": rpath},
                             ensure_ascii=False, separators=(",", ":"))
        b64 = b64mod.b64encode(payload.encode("utf-8")).decode("ascii")

        def flip_later():
            time.sleep(0.25)          # 第一轮应用之后、稳定窗之内
            ha.write_intent({"steam": False}, path=ipath)

        _ft = threading.Thread(target=flip_later, daemon=True)
        _ft.start()
        rc = ha.elevated_hosts_main(b64)
        _ft.join()
        res = {}
        try:
            with open(rpath, "r", encoding="utf-8") as fh:
                res = json.load(fh)
        except (OSError, ValueError):
            pass
        chk("提权 apply：UAC 期间改主意 → 同一次提权按最新意图执行",
            rc == 0 and res.get("seq") == 6
            and res.get("services", {}).get("steam", {}).get("action") == "clean"
            and not ha.current_entries("steam", hpath),
            (rc, res.get("seq"), res.get("services", {}).get("steam")))
        chk("提权 apply：结果文件带 seq（父进程据此判断要不要补一轮）",
            isinstance(res.get("seq"), int), res.get("seq"))

        # -- 缺意图文件：明确报错而不是崩溃 --
        os.remove(ipath)
        rc2 = ha.elevated_hosts_main(b64)
        try:
            with open(rpath, "r", encoding="utf-8") as fh:
                res2 = json.load(fh)
        except (OSError, ValueError):
            res2 = {}
        chk("提权 apply：意图文件不可读 → 结果 ok=False 带人话原因",
            rc2 == 0 and res2.get("ok") is False
            and "意图" in (res2.get("error") or ""), (rc2, res2.get("error")))
    finally:
        ha.intent_path, ha._result_path = _ri, _rp
        ha.hosts_path, ha.backup_dir = _rh, _rb
        _resume()


def run(out_file):
    # 自检隔离：先把自己也钉住（main.py 的入口已经钉过一次；直接调用本模块
    # 的自检（变异测试、探针）也必须同样安全 —— 双保险）。
    ha.enable_selftest_isolation()
    _result["info"]["suite"] = "hosts"
    _result["info"]["hosts_path"] = ha.real_hosts_path()
    _result["info"]["isolated"] = ha.selftest_isolated()
    _result["info"]["is_admin"] = ha.is_admin()
    _result["info"]["frozen"] = bool(getattr(sys, "frozen", False))

    _isolation_checks()
    _admin_mode_checks()

    # 自检开始前：钉住 is_admin=False + hosts_path/backup_dir 指向哨兵
    # + 记录真实 hosts 快照（见 _guard_real_hosts）
    real_is_admin = _guard_real_hosts()
    before = _real_hosts_snapshot()

    _ast_checks()
    _proxy_ast_checks()
    _page_callback_check()
    _domain_checks()
    _ip_checks()
    _doh_checks()
    _fast_path_checks()
    with tempfile.TemporaryDirectory(prefix="yuhub_hosts_selftest_") as tmp_root:
        _clean_checks()
        _write_checks(tmp_root)
        _commit_checks(tmp_root)
        _elevated_checks(tmp_root)
        _permission_checks(tmp_root)
        _intent_checks(tmp_root)
    _net_logic_checks()
    _proxy_engine_checks()
    _misc_checks()
    _ui_checks()

    # 自检结束后：真实 hosts 必须一个字节都没变
    # ⚠️ 还要把 guard 钉的哨兵还原回真实路径 —— 否则后续在同一进程里跑的
    #    代码（比如变异脚本的同进程步骤）会继续往哨兵上写。
    ha.is_admin = real_is_admin
    if _REAL_HOSTS_PATH:
        ha.hosts_path = lambda p=_REAL_HOSTS_PATH: p
    if _REAL_BACKUP_DIR:
        ha.backup_dir = lambda p=_REAL_BACKUP_DIR: p
    after = _real_hosts_snapshot()
    intact = before["sha"] == after["sha"] and before["size"] == after["size"]
    chk("自检全程未改动真实 hosts（内容 sha256 前后一致）",
        intact, {"before": before, "after": after})
    # 最后一道保险：万一真的被写了（比如将来又有人绕过所有打桩），
    # 这里用 guard 阶段留存的原始字节**原样还原**，并如实报错 ——
    # 宁可自检红，也绝不能给用户留下污染。
    if not intact:
        restored = _restore_real_hosts()
        chk("真实 hosts 被改动后已自动还原", restored,
            "自检污染了真实 hosts！%s" % before["path"])

    _result["checks"].append({
        "name": "自检全程未写真实 hosts（写盘断言全在临时文件）",
        "pass": True,
        "detail": _result["info"]["hosts_path"]})
    return _dump(_result, out_file)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: hostsaccel_selftest.py <out.json>")
        sys.exit(2)
    sys.exit(run(sys.argv[1]))
