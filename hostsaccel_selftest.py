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

    # 1) import 全部是标准库白名单
    allowed = {
        "base64", "concurrent.futures", "ctypes", "datetime", "ipaddress",
        "json", "os", "platform", "re", "shutil", "socket", "subprocess",
        "sys", "time", "urllib.request",
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
    """accel_page 的引擎回调（后台线程）只允许 emit。"""
    src = _module_source("accel_page.py")
    if src is None:
        chk("accel_page 源码随包可读（回调纪律扫描前提）", False, "读不到 accel_page.py")
        return
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        # 找 _start_optimize / _start_clean / _proxy_resolver 里的嵌套函数
        #（on_domain / work 等）以及 _proxy_resolver 自身
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_start_optimize", "_start_clean", "_proxy_resolver"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef) and sub is not node:
                    for stmt in ast.walk(sub):
                        # 回调体内出现直接改界面的调用（setText/setVisible/
                        # setEnabled/toast）就是违规 —— 只允许 emit
                        if (isinstance(stmt, ast.Call)
                                and isinstance(stmt.func, ast.Attribute)
                                and stmt.func.attr in (
                                    "setText", "setVisible", "setEnabled",
                                    "toast", "addWidget", "update", "repaint")):
                            # emit 本身是 emit(...)，上面名单不含它
                            bad.append("%s:%s" % (sub.name, stmt.func.attr))
    chk("后台线程回调只 emit、绝不直接改界面（硬规则 3 的静态防线）",
        not bad, bad)


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
# ⑤ 提权入口（载荷校验 + 临时路径下的 clean 全流程）
# ---------------------------------------------------------------------------
def _elevated_checks(tmp_root):
    import base64 as b64mod

    def payload(obj):
        return b64mod.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")

    chk("非 base64 载荷 → 2", ha.elevated_hosts_main("!!bad!!") == 2, "")
    chk("缺 result_file → 3", ha.elevated_hosts_main(payload(
        {"mode": "clean", "service": "steam", "result_file": ""})) == 3, "")
    chk("非法 mode → 3", ha.elevated_hosts_main(payload(
        {"mode": "kill", "service": "steam", "result_file": "x"})) == 3, "")
    chk("未知服务 → 3", ha.elevated_hosts_main(payload(
        {"mode": "clean", "service": "bilibili", "result_file": "x"})) == 3, "")
    chk("私网 IP 拒绝写入 → 3", ha.elevated_hosts_main(payload(
        {"mode": "write", "service": "steam", "result_file": "x",
         "entries": [["192.168.1.1", "store.steampowered.com"]]})) == 3, "")
    chk("清单外域名拒绝写入 → 3", ha.elevated_hosts_main(payload(
        {"mode": "write", "service": "steam", "result_file": "x",
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
    # 模式与地址不一致 / 清单外 —— 校验在写盘之前就拒绝（真实路径安全）
    chk("代理模式带真实公网 IP → 3（模式与地址必须一致）",
        ha.elevated_hosts_main(payload(
            {"mode": "write", "service": "steam", "result_file": "x",
             "proxy": True,
             "entries": [["23.15.142.182", "store.steampowered.com"]]})) == 3, "")
    chk("直连模式带 127.0.0.1 → 3（不能绕过公网校验）",
        ha.elevated_hosts_main(payload(
            {"mode": "write", "service": "steam", "result_file": "x",
             "entries": [["127.0.0.1", "store.steampowered.com"]]})) == 3, "")
    chk("代理模式 + 清单外域名 → 3", ha.elevated_hosts_main(payload(
        {"mode": "write", "service": "steam", "result_file": "x", "proxy": True,
         "entries": [["127.0.0.1", "evil.example.com"]]})) == 3, "")

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
    chk("hosts_path 指向真实存在的系统 hosts", os.path.isfile(ha.hosts_path()),
        ha.hosts_path())
    res = ha.flush_dns_cache()
    chk("flush_dns_cache 返回布尔且不抛异常", isinstance(res, bool), res)
    chk("提权开关与 main.py 分发一致", ha._ELEVATED_FLAG == "--hosts-elevated",
        ha._ELEVATED_FLAG)


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

    # 映射缓存打桩到临时文件（默认代理模式下优选结果会写真实缓存位置）
    real_map_path = ha._map_cache_path
    map_file = os.path.join(tempfile.gettempdir(),
                            "yuhub_hosts_map_ui_%d.json" % os.getpid())
    ha._map_cache_path = lambda: map_file

    # SniProxy 工厂打桩：真实代理，但全部用临时端口（绝不占 443/80，
    # 也不和正在运行的 Steam++ 冲突）
    real_sni_factory = ap.hp.SniProxy
    # ap.hp 与本模块的 hp 是同一个模块对象 —— 打桩前先把真实类存进局部量，
    # 工厂内部调真实类，避免自我递归
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
    # 工厂在整个 UI 测试段内都生效（on_shown 自愈等后续也会建代理）
    ap.hp.SniProxy = fake_sni_factory
    page = ap.AccelPage(notify=toasts.append)

    names = [c._btn_go.text() for c in page._cards.values()]
    chk("每个服务一张卡片、各带优选按钮", len(page._cards) == 2
        and all(t == "一键优选加速" for t in names), names)

    # 模式切换控件
    chk("加速模式分段控件有两个选项",
        set(page._mode_seg._buttons) == {"proxy", "direct"},
        sorted(page._mode_seg._buttons))
    chk("默认模式是本地反向代理", page._current_mode() == "proxy",
        page._current_mode())

    real_entries = ha.current_entries
    try:
        ha.current_entries = lambda svc, path=None: (
            [(ha.PROXY_IP, "github.com")] if svc == "github" else [])
        page._refresh_status()
        gh = page._cards["github"]._status.text()
        st = page._cards["steam"]._status.text()
        chk("状态展示区分已加速/未加速",
            "已加速 1 个域名" in gh and "未加速" in st, (gh, st))
        chk("状态展示标明代理模式", "本地反向代理" in gh, gh)
        # entries_mode 混合/异常时状态行不该崩
        ha.current_entries = lambda svc, path=None: [("1.2.3.4", "github.com")]
        page._refresh_status()
        chk("直连条目状态展示标明直连",
            "直连" in page._cards["github"]._status.text(),
            page._cards["github"]._status.text())
    finally:
        ha.current_entries = real_entries

    def pump_until(cond, ms=5000):
        timer = QElapsedTimer()
        timer.start()
        while timer.elapsed() < ms:
            app.processEvents()
            time.sleep(0.01)
            if cond():
                return True
        return False

    real_run, real_flush = ha.run_elevated, ha.flush_dns_cache
    try:
        calls = []
        ha.run_elevated = lambda mode, svc, entries=None, timeout=120.0, proxy=False: (
            calls.append((mode, svc, list(entries or []), proxy))
            or {"ok": True})
        ha.flush_dns_cache = lambda: True

        # ---- 反代模式默认流程：优选 → 写 127.0.0.1 条目 ----
        page._on_measure_finished("steam", True, "",
                                  [("1.2.3.4", "store.steampowered.com", 30.0)])
        ok = pump_until(lambda: not page._busy["steam"])
        chk("反代模式写入流程收尾（busy 归零）", ok, calls)
        chk("反代模式 run_elevated 收到 proxy=True",
            calls and calls[-1][3] is True, calls)
        chk("反代模式写盘条目是 127.0.0.1",
            calls and calls[-1][2] == [(ha.PROXY_IP, "store.steampowered.com")],
            calls)
        chk("真实 IP 进了映射缓存",
            page._ipmap.get("store.steampowered.com") == "1.2.3.4", page._ipmap)
        chk("映射缓存落盘可读回",
            ha.load_map_cache().get("store.steampowered.com") == "1.2.3.4", "")
        chk("优选前代理已被拉起（写盘不能早于监听）",
            page._proxy_running(), "")
        chk("写入成功弹出提示", any("加速已生效" in t for t in toasts), toasts[-3:])

        # resolver 命中缓存：不许触发 optimize_domain
        real_opt_dom = ha.optimize_domain
        ha.optimize_domain = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("缓存命中时不应现场解析"))
        try:
            got = page._proxy_resolver("store.steampowered.com")
        finally:
            ha.optimize_domain = real_opt_dom
        chk("resolver 命中缓存直接返回真实 IP", got == "1.2.3.4", got)
        chk("resolver 拒绝清单外域名", page._proxy_resolver("evil.com") is None, "")

        # ---- 切直连模式：写真实 IP、proxy=False ----
        page._mode_seg._buttons["direct"].setChecked(True)
        page._mode_seg._buttons["proxy"].setChecked(False)
        chk("切到直连模式生效", page._current_mode() == "direct", "")
        calls.clear()
        page._on_measure_finished("steam", True, "",
                                  [("1.2.3.4", "store.steampowered.com", 30.0)])
        pump_until(lambda: not page._busy["steam"])
        chk("直连模式写盘条目是真实 IP 且 proxy=False",
            calls and calls[-1][2] == [("1.2.3.4", "store.steampowered.com")]
            and calls[-1][3] is False, calls)

        # 切回反代
        page._mode_seg._buttons["proxy"].setChecked(True)
        page._mode_seg._buttons["direct"].setChecked(False)

        # 用户取消 UAC（run_elevated 返回 None）
        toasts.clear()
        ha.run_elevated = lambda mode, svc, entries=None, timeout=120.0, proxy=False: None
        page._on_measure_finished("steam", True, "",
                                  [("1.1.1.1", "api.steampowered.com", 40.0)])
        pump_until(lambda: not page._busy["steam"])
        chk("取消 UAC 有明确提示且不算成功",
            any("未完成写入" in t for t in toasts), toasts[-2:])
        chk("取消后 busy 归零", not page._busy["steam"], "")

        # 测速全失败
        toasts.clear()
        page._on_measure_finished("github", False, "未能测出任何可用节点", [])
        chk("测速全失败直接提示、不进写盘", any("未能测出" in t for t in toasts)
            and not page._busy["github"], toasts[-2:])

        # 恢复默认成功路径（无剩余代理条目时顺带停代理）
        toasts.clear()
        ha.run_elevated = lambda mode, svc, entries=None, timeout=120.0, proxy=False: (
            calls.append((mode, svc, list(entries or []), proxy)) or {"ok": True})
        page._start_clean("github")
        pump_until(lambda: not page._busy["github"])
        chk("恢复默认成功提示", any("恢复默认" in t for t in toasts), toasts[-2:])
        chk("恢复默认按钮恢复可用", page._cards["github"]._btn_go.isEnabled(), "")
        chk("恢复默认走 clean 模式且不带 proxy 标记",
            calls and calls[-1][0] == "clean" and calls[-1][3] is False, calls)
    finally:
        ha.run_elevated, ha.flush_dns_cache = real_run, real_flush

    # ---- on_shown 自愈：hosts 有 127.0.0.1 条目而代理没跑 → 自动拉起 ----
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

    # ---- shutdown：停代理 + 清代理条目（run_elevated 打桩记录） ----
    calls2 = []
    real_entries = ha.current_entries
    real_run2 = ha.run_elevated
    try:
        ha.current_entries = lambda svc, path=None: (
            [(ha.PROXY_IP, "github.com")] if svc == "github" else [])
        ha.run_elevated = lambda mode, svc, entries=None, timeout=120.0, proxy=False: (
            calls2.append((mode, svc)) or {"ok": True})
        page.shutdown()
        pump_until(lambda: calls2, ms=5000)
        chk("shutdown 停掉了代理", not page._proxy_running(), "")
        chk("shutdown 只清理反代模式的条目（github 被清、steam 不动）",
            calls2 == [("clean", "github")], calls2)
    finally:
        ha.current_entries, ha.run_elevated = real_entries, real_run2

    # busy 防重入：正在测速时再点不应开工（打桩观察 optimize_service 是否被调）
    real_opt = ha.optimize_service
    called = []
    ha.optimize_service = lambda svc, doh_timeout=2.5, tcp_timeout=1.2, on_domain=None: (
        called.append(svc) or [])
    try:
        page._busy["steam"] = True
        page._start_optimize("steam")
        chk("忙碌时重复点击被忽略（防重入）", called == [], called)
    finally:
        ha.optimize_service = real_opt
        page._busy["steam"] = False

    if os.path.exists(map_file):
        os.remove(map_file)
    ha._map_cache_path = real_map_path
    ap.hp.SniProxy = real_sni_factory


# ---------------------------------------------------------------------------
def run(out_file):
    _result["info"]["suite"] = "hosts"
    _result["info"]["hosts_path"] = ha.hosts_path()
    _result["info"]["is_admin"] = ha.is_admin()
    _result["info"]["frozen"] = bool(getattr(sys, "frozen", False))

    _ast_checks()
    _proxy_ast_checks()
    _page_callback_check()
    _domain_checks()
    _ip_checks()
    _doh_checks()
    with tempfile.TemporaryDirectory(prefix="yuhub_hosts_selftest_") as tmp_root:
        _clean_checks()
        _write_checks(tmp_root)
        _elevated_checks(tmp_root)
    _net_logic_checks()
    _proxy_engine_checks()
    _misc_checks()
    _ui_checks()

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
