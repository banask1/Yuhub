"""「C盘清理」打包后自检（只读 + 沙箱删除，绝不碰真实系统文件）。

用法： Yuhub.exe --cleaner-selftest <结果json路径>
返回码：0=全部通过，1=存在失败项。

检查项：
  A. 规则表完整性 —— 规则条数、key 唯一性、字段齐全、level 合法；
  B. 路径表达式求值 —— %var% / ?GetRegSz / ?GetFileVersion / ?GetIniData；
  C. 目标展开安全护栏 —— contents 模式绝不允许指向关键系统目录；
  D. 沙箱匹配/删除 —— 在临时目录里构造真实结构，验证 entries/contents
     两种模式与 Excluded（含动态版本排除）行为正确、体积统计准确；
  E. 真机只读扫描 —— 抽取 3 条规则走 scan_rule，确认只统计不删除。
"""

import json
import os
import shutil
import sys
import tempfile

import cleaner as cl


def run(out_path):
    checks = []          # {name, ok, detail}

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # ---------- A. 规则表完整性 ----------
    try:
        rules = cl.build_rules()
        keys = [r.key for r in rules]
        check("规则条数 >= 45", len(rules) >= 45, f"实际 {len(rules)} 条")
        check("key 无重复", len(keys) == len(set(keys)),
              f"{len(keys)} keys / {len(set(keys))} unique")
        bad = [r.key for r in rules if not r.name or not r.desc or not r.group
               or r.level not in (0, 1, 2, 3)]
        check("字段齐全且 level 合法", not bad, f"异常项: {bad}")
        missing = {"temp_files", "windows_logs", "wininet_cache", "thumbnail_cache",
                   "recycle_bin", "windows_old", "dism_component", "wu_download",
                   "msocache", "prefetch"} - set(keys)
        check("Dism++ 关键条目在列", not missing, f"缺失: {missing}")
        no_group = [r.key for r in rules if r.group not in cl.GROUP_ORDER]
        check("分组常量齐全", not no_group, f"未分组: {no_group}")
    except Exception as exc:
        check("规则表构建", False, f"异常: {exc}")
        rules = []

    # ---------- B. 路径表达式求值 ----------
    try:
        v = cl._eval_expr(r"%SystemRoot%\Temp")
        check("展开 %SystemRoot%", v.lower().endswith(r"\windows\temp"), v)
        v2 = cl._eval_expr(r"%System%")
        check("展开 %System%", v2.lower() == os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32").lower(), v2)
        # 不存在的注册表值必须优雅返回空串（而不是抛异常）
        v3 = cl._eval_expr(r"?GetRegSz(HKEY_LOCAL_MACHINE\SOFTWARE\__Yuhub_NoKey__,X)")
        check("GetRegSz 缺键返回空", v3 == "", repr(v3))
        # GetFileVersion：对 cmd.exe 求值应得到 x.y.z.w 四段数字
        cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "cmd.exe")
        v4 = cl._get_file_version(cmd)
        check("GetFileVersion", len(v4.split(".")) == 4, v4)
    except Exception as exc:
        check("表达式求值", False, f"异常: {exc}")

    # ---------- B2. DISM 体积解析（修复：逗号小数 + 中英文关键词 + 可清理量）----------
    try:
        # 中文样本：用**真机实测**的字段名（注意「备份和已禁用的功能」中间有个"的"）
        zh = (
            "Windows 资源管理器报告的组件存储大小 : 11.76 GB\n"
            "组件存储的实际大小: 11.57 GB\n"
            "    已与 Windows 共享 : 8.21 GB\n"
            "    备份和已禁用的功能 : 3.36 GB\n"
            "    缓存和临时数据 : 0 bytes\n"
            "可回收的程序包数 : 2\n"
        )
        v_zh = cl._parse_dism_size(zh)
        # 可清理量应取「备份和已禁用的功能」(3.36GB)，**不是**整个实际大小(11.57GB)
        check("DISM 解析-中文取可清理量（3.36GB，非整库 11.57GB）",
              abs(v_zh - int(3.36 * 1024 ** 3)) <= 1024 ** 3 // 20, v_zh)

        # 逗号小数（欧陆/部分区域设置）：4,55 GB → 4.55 GB
        comma = "实际大小: 4,55 GB\n备份和已禁用功能: 0 字节\n"
        v_c = cl._parse_dism_size(comma)
        check("DISM 解析-逗号小数 4,55GB→4.55GB",
              abs(v_c - int(4.55 * 1024 ** 3)) <= 1024 ** 3 // 100, v_c)

        # 英文样本：Actual size / Backups and disabled features
        en = (
            "Size of the component store: 5.10 GB\n"
            "Actual size of the component store: 4.80 GB\n"
            "Shared with Windows: 3.00 GB\n"
            "Backups and disabled features: 900 MB\n"
            "Cache and temporary data: 12 MB\n"
        )
        v_en = cl._parse_dism_size(en)
        # 可清理量 = 900MB + 12MB
        check("DISM 解析-英文取可清理量（912MB）",
              abs(v_en - int(912 * 1024 ** 2)) <= 1024 ** 2, v_en)

        # 退化样本：只有「实际大小」没有分项 → 退回实际大小（上限估计）
        only_actual = "实际大小: 2.00 GB\n"
        v_o = cl._parse_dism_size(only_actual)
        check("DISM 解析-无分项时退回实际大小（2GB）",
              abs(v_o - int(2.0 * 1024 ** 3)) <= 1024 ** 3 // 100, v_o)

        # 可回收包数为 0 / 明确"不推荐清理" → 报 0（不把不可回收的保留负载当可清理）
        zero = ("组件存储的实际大小: 10.40 GB\n"
                "备份和已禁用的功能: 2.21 GB\n"
                "可回收的程序包数 : 0\n"
                "推荐使用组件存储清理 : 否\n")
        check("★ 可回收包数=0 时报 0（保证「扫得出来=清得掉」）",
              cl._parse_dism_size(zero) == 0, cl._parse_dism_size(zero))
        rec_no = "备份和已禁用的功能: 5.00 GB\n推荐使用组件存储清理 : 否\n"
        check("不推荐清理时也报 0", cl._parse_dism_size(rec_no) == 0)
        with_pkgs = "备份和已禁用的功能: 3.36 GB\n可回收的程序包数 : 2\n"
        check("有可回收包时仍报出体积",
              cl._parse_dism_size(with_pkgs) > 0, cl._parse_dism_size(with_pkgs))

        # 幽灵包解析：从 CBS.log 提取「deeply superseded」的 top-level 包名
        cbs_dir = tempfile.mkdtemp(prefix="yuhub_cbs_")
        try:
            cbs = os.path.join(cbs_dir, "CBS.log")
            with open(cbs, "w", encoding="utf-8") as f:
                f.write(
                    "2026-10-13 09:42:14, Info CBS [Package_for_RollupFix"
                    "~31bf3856ad364e35~amd64~~26100.1742.1.10] is a top-level "
                    "package and is deeply superseded\n"
                    "2026-10-13 09:42:14, Info CBS [Microsoft-Windows-"
                    "FodMetadataServicing-Desktop-Metadata-Package"
                    "~31bf3856ad364e35~amd64~~10.0.26100.1742] is a top-level "
                    "package and is deeply superseded\n"
                    "2026-10-13 09:42:14, Info CBS [Foo-Bar~31bf3856ad364e35"
                    "~amd64~~1.2.3] is superseded\n")   # 非 deeply → 不该命中
            names = cl._dism_deeply_superseded_packages(log_path=cbs)
            check("★ 幽灵包解析（只取 deeply superseded 的 top-level 包）",
                  names == [
                      "Package_for_RollupFix~31bf3856ad364e35~amd64~~26100.1742.1.10",
                      "Microsoft-Windows-FodMetadataServicing-Desktop-Metadata-"
                      "Package~31bf3856ad364e35~amd64~~10.0.26100.1742"], names)
            check("幽灵包解析-日志缺失返回空（不崩）",
                  cl._dism_deeply_superseded_packages(
                      log_path=os.path.join(cbs_dir, "nope.log")) == [])
        finally:
            shutil.rmtree(cbs_dir, ignore_errors=True)
    except Exception as exc:
        check("DISM 体积解析", False, f"异常: {exc}")

    # ---------- C. 安全护栏 ----------
    try:
        crit = cl._critical_roots()
        win = os.environ.get("SystemRoot", r"C:\Windows")
        check("护栏包含 Windows/System32",
              os.path.normpath(win).lower() in crit
              and os.path.normpath(os.path.join(win, "System32")).lower() in crit)
        # 构造一个恶意/错误的 contents 目标（指向 Windows 本身）→ 清理必须被拒
        evil = cl.Target(root=win, mode="contents")
        ok, fail, vol = cl._clean_target(evil)
        check("contents 指向 Windows 被拒", ok == 0 and vol == 0,
              f"ok={ok} vol={vol}")
        check("护栏后 Windows 仍在", os.path.isdir(win))
    except Exception as exc:
        check("安全护栏", False, f"异常: {exc}")

    # ---------- D. 沙箱匹配 / 删除 ----------
    sandbox = tempfile.mkdtemp(prefix="yuhub_cln_selftest_")
    try:
        # 模拟 Chrome 老版本备份：当前版本目录名 = 真 PE（cmd.exe 副本）的
        # 文件版本，另外造两个“老版本”目录
        app = os.path.join(sandbox, "App")
        fake_exe = os.path.join(app, "Chrome.exe")
        os.makedirs(app)
        shutil.copyfile(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                     "System32", "cmd.exe"), fake_exe)
        ver = cl._get_file_version(fake_exe)
        check("沙箱真 PE 有版本号", len(ver.split(".")) == 4, ver)
        cur = os.path.join(app, ver)
        old1 = os.path.join(app, "1.0.0.1")
        old2 = os.path.join(app, "2.0.0.2")
        for d in (cur, old1, old2):
            os.makedirs(d)
        payload = os.path.join(old1, "chrome.dll")
        with open(payload, "wb") as f:
            f.write(b"X" * 4096)
        with open(os.path.join(cur, "keep.txt"), "wb") as f:
            f.write(b"K" * 100)
        with open(os.path.join(old2, "other.dll"), "wb") as f:
            f.write(b"O" * 2048)

        tgt = cl.Target(root=app, query=("*.*.*.*",),
                        excluded=("?GetFileVersion(%Root%Chrome.exe)",),
                        mode="entries")
        hits = sorted(cl._resolve_entries(tgt))
        names = [os.path.basename(p) for p in hits]
        # 当前版本目录必须被动态排除
        check("动态排除当前版本", ver not in names and cur not in hits,
              f"ver={ver} hits={names}")
        # 老版本目录全部命中且只命中目录本身
        check("老版本全部命中",
              old1 in hits and old2 in hits and cur not in hits, str(names))

        # 排除项求值失败 → 整根跳过（宁可不删，也不能误删当前版本）
        bad_tgt = cl.Target(root=app, query=("*.*.*.*",),
                            excluded=("?GetFileVersion(%Root%不存在.exe)",),
                            mode="entries")
        check("排除项求值失败整根跳过",
              cl._resolve_entries(bad_tgt) == [], "应返回空")

        total, n = cl._scan_target(tgt)
        check("扫描体积正确", total == 4096 + 2048 and n == 2,
              f"total={total} n={n}")

        ok, fail, vol = cl._clean_target(tgt)
        check("entries 清理正确", ok == 2 and fail == 0 and vol == 4096 + 2048,
              f"ok={ok} vol={vol}")
        check("当前版本保留", os.path.isdir(cur) and os.path.isfile(fake_exe))
        check("老版本已删", not os.path.exists(old1) and not os.path.exists(old2))

        # contents 模式：清空内容、保留目录
        holder = os.path.join(sandbox, "Holder")
        os.makedirs(os.path.join(holder, "junk"))
        with open(os.path.join(holder, "junk", "a.bin"), "wb") as f:
            f.write(b"Z" * 512)
        ok2, fail2, vol2 = cl._clean_target(cl.Target(root=holder, mode="contents"))
        check("contents 清空内容", ok2 == 1 and vol2 == 512
              and os.path.isdir(holder) and not os.path.exists(
                  os.path.join(holder, "junk")),
              f"ok={ok2} vol={vol2}")

        # 通配 root：'*' 中段展开
        star_root = os.path.join(sandbox, "Users", "u1", "Cache")
        os.makedirs(star_root)
        with open(os.path.join(star_root, "c.tmp"), "wb") as f:
            f.write(b"C" * 128)
        wt = cl.Target(root=os.path.join(sandbox, "Users", "*", "Cache"),
                       mode="contents")
        ok3, _, vol3 = cl._clean_target(wt)
        check("通配 root 展开", ok3 == 1 and vol3 == 128, f"ok={ok3} vol={vol3}")
    except Exception as exc:
        check("沙箱验证", False, f"异常: {exc}")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    # ---------- E. 真机只读扫描（不删任何东西） ----------
    try:
        by_key = {r.key: r for r in rules}
        marker_file = os.path.join(tempfile.gettempdir(), "yuhub_selftest_marker.txt")
        with open(marker_file, "w", encoding="utf-8") as f:
            f.write("must survive")
        for key in ("crash_dumps", "thumbnail_cache", "baidu_netdisk_logs"):
            r = by_key[key]
            size = cl.scan_rule(r)
            check(f"扫描 {key} 只读", r.scanned and size >= 0, f"size={size}")
        check("标记文件幸存", os.path.isfile(marker_file))
        os.remove(marker_file)

        # 路径展开冒烟：这些规则的 root 至少能在本机展开出目录
        for key in ("temp_files", "windows_logs", "prefetch"):
            r = by_key[key]
            tgt_names = [t.root for t in r.targets]
            check(f"{key} 有目标", bool(tgt_names))
    except Exception as exc:
        check("真机只读扫描", False, f"异常: {exc}")

    # ---------- G. 扫描器：并行 + 慢条目排最后 + 先解禁界面 ----------
    # 用户反馈"扫描太慢"：Dism /AnalyzeComponentStore 单项几十秒到两分钟，
    # 以前它和几十条快规则串在一条流水线上，界面按钮要等它跑完才解禁。
    # 下面用一个**合成规则表**（没有 targets，扫描瞬间完成）验证调度行为，
    # 不碰真实文件系统。
    try:
        import threading as _th
        events = []
        fake_fast = [cl.CleanRule(key="f%d" % i, name="快%d" % i, desc="x",
                                  group=cl.GROUP_TEMP, level=3)
                     for i in range(4)]
        fake_slow = cl.CleanRule(key="s1", name="慢1", desc="x",
                                 group=cl.GROUP_SYSTEM, level=2, slow_scan=True)
        all_rules = list(fake_fast) + [fake_slow]
        done_evt = _th.Event()
        seen_keys = []

        def on_prog(r):
            seen_keys.append(r.key)
            if r.key == "s1":
                events.append("slow_done")

        scanner = cl.CleanerScanner()
        scanner.start(
            all_rules,
            on_progress=on_prog,
            on_done=lambda total, rules: (events.append("done"), done_evt.set()),
            on_fast_done=lambda total, rules: events.append("fast_done"),
        )
        done_evt.wait(20)
        check("慢条目标记 slow_scan", getattr(fake_slow, "slow_scan", False)
              and not any(getattr(r, "slow_scan", False) for r in fake_fast))
        check("★ on_fast_done 在 on_done 之前触发",
              events == ["fast_done", "slow_done", "done"], events)
        check("★ 慢条目排在（快条目之后）最后扫描",
              seen_keys[-1] == "s1", seen_keys)
        check("★ 并行扫描：全部快条目都扫到了",
              set(r.key for r in fake_fast) <= set(seen_keys), seen_keys)
        check("DISM 组件条目标记为慢（排最后）",
              any(r.slow_scan for r in rules if r.key == "dism_component"))
        check("DISM 分析结果有缓存（重复扫描不重复跑外部命令）",
              isinstance(cl._DISM_CACHE, dict) and cl._DISM_CACHE_TTL >= 60,
              cl._DISM_CACHE_TTL)
    except Exception as exc:
        check("扫描器调度行为", False, f"异常: {exc}")
    # ---------- F. 跨设备兼容性（换机器 / 换硬件厂商 / 换用户名） ----------
    sandbox2 = tempfile.mkdtemp(prefix="yuhub_cln_port_")
    try:
        by_key = {r.key: r for r in rules}
        # ① 厂商条目与本机硬件解耦：本机没有某厂商的目录时，该条目扫出 0、
        #    置灰、不崩溃——AMD 机器上 NVIDIA 条目（反之亦然）就是这样消失的。
        for key in ("avira_temp", "kugou_old", "qq_temp"):
            r = by_key[key]
            size = cl.scan_rule(r)
            check(f"厂商无关条目 {key} 可扫描", r.scanned and size >= 0,
                  f"size={size} available={r.available}")
        # ② 三家 GPU 着色器缓存规则在任何机器上都能安全扫描（谁的存在谁命中）
        for key in ("gpu_nvidia_shader", "gpu_amd_shader", "gpu_intel_shader"):
            r = by_key[key]
            size = cl.scan_rule(r)
            check(f"GPU 缓存 {key} 厂商无关", r.scanned and size >= 0,
                  f"size={size}")
        # ③ 多用户展开：模拟两个用户目录（含空格与中文名），
        #    `Users\*\AppData\Local\Temp` 必须把两个都展开出来。
        users = os.path.join(sandbox2, "Users")
        for uname in ("John Doe", "用户 甲"):
            t = os.path.join(users, uname, "AppData", "Local", "Temp")
            os.makedirs(t)
            with open(os.path.join(t, "x.tmp"), "wb") as f:
                f.write(b"U" * 64)
        wt = cl.Target(root=os.path.join(sandbox2, "Users", "*",
                                         "AppData", "Local", "Temp"),
                       mode="contents")
        roots = cl._expand_root(wt.root)
        check("多用户/中文/空格用户名展开", len(roots) == 2,
              f"roots={roots}")
        ok4, _, vol4 = cl._clean_target(wt)
        check("多用户内容清理", ok4 == 2 and vol4 == 128,
              f"ok={ok4} vol={vol4}")
    except Exception as exc:
        check("跨设备兼容性", False, f"异常: {exc}")
    finally:
        shutil.rmtree(sandbox2, ignore_errors=True)

    # ---------- I. 静态守卫：main.py 无「函数内 import 遮蔽模块级名字」 ----------
    # 2026-10-13 踩坑：为排查 WinSxS 组件，在 main() 里写了 `import os` /
    # `import subprocess` —— Python 会把该名字当成**整个函数的局部变量**，
    # 于是 main() 中更早的 os.xxx 调用（第 696 行 icon_path）直接抛
    # UnboundLocalError → **启动即崩**（PyInstaller 弹 "Failed to execute
    # script 'main'"）。这里用 AST 把这类风险在自检期就抓出来：任何函数体内
    # 的 import，若名字同时是模块级导入的名字，就判红。
    try:
        import ast
        cands = []
        try:
            cands.append(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "main.py"))
        except Exception:                                    # noqa: BLE001
            pass
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(os.path.join(meipass, "main.py"))
        cands.append(os.path.join(os.getcwd(), "main.py"))
        mp = next((p for p in cands if os.path.isfile(p)), None)
        if mp is None:
            check("main.py 静态守卫", False, "找不到 main.py 源码")
        else:
            with open(mp, encoding="utf-8") as fp:
                tree = ast.parse(fp.read())
            module_level = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for a in node.names:
                        module_level.add((a.asname or a.name).split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    for a in node.names:
                        module_level.add(a.asname or a.name)
            hazards = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for sub in ast.walk(node):
                        names = []
                        if isinstance(sub, ast.Import):
                            names = [(a.asname or a.name).split(".")[0]
                                     for a in sub.names]
                        elif isinstance(sub, ast.ImportFrom):
                            names = [a.asname or a.name for a in sub.names]
                        for nm in names:
                            if nm in module_level:
                                hazards.append("%s(): import %s"
                                               % (node.name, nm))
            check("★ main.py 无函数内 import 遮蔽模块级名字（防启动崩溃）",
                  not hazards, str(hazards[:5]))
    except Exception as exc:
        check("main.py 静态守卫", False, f"异常: {exc}")

    result = {
        "ok": all(c["ok"] for c in checks),
        "total": len(checks),
        "passed": sum(1 for c in checks if c["ok"]),
        "failed": sum(1 for c in checks if not c["ok"]),
        "checks": checks,
        "rule_count": len(rules),
    }
    try:
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(result, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, out_path)
    except OSError:
        pass

    # 控制台摘要（打包态看不到，源码运行时有用）
    for c in checks:
        print(("[OK]   " if c["ok"] else "[FAIL] ") + c["name"]
              + ("  --  " + c["detail"] if c["detail"] and not c["ok"] else ""))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "cleaner_selftest_result.json"
    sys.exit(run(out))
