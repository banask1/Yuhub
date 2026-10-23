"""「C盘清理」打包后自检（只读 + 沙箱删除，绝不碰真实系统文件）。

用法： Yuhub.exe --cleaner-selftest <结果json路径>
返回码：0=全部通过，1=存在失败项。

检查项：
  A. 规则表完整性 —— 规则条数、key 唯一性、字段齐全、level 合法，
     entries 目标必须有 query；
  B. 路径表达式求值 —— %var% / ?GetRegSz / ?GetFileVersion / ?GetIniData；
  B2. DISM 体积解析（中英文、逗号小数、可回收包数为 0）；
  B3. 还原点解析 —— 只统计「已用空间」，不能把已用/分配/最大三行相加；
  C. 目标展开安全护栏 —— contents 模式绝不允许指向关键系统目录；
  D. 沙箱匹配/删除 —— 在临时目录里构造真实结构，验证 entries/contents
     两种模式与 Excluded（含动态版本排除）行为正确、体积统计准确；
  D2. query 语义 —— 多段路径逐级下钻、多个 query 并联、contents 遵守
     excluded、重叠 root 只统计一次（四类都是历史上真实踩过的静默失效）；
  E. 真机只读扫描 —— 抽取 3 条规则走 scan_rule，确认只统计不删除。
"""

import json
import os
import shutil
import sys
import tempfile
import time

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
        # entries 目标必须带 query（没有 query 就永远匹配不到任何东西）
        no_q = [r.key for r in rules if any(
            t.mode == "entries" and not t.query for t in r.targets)]
        check("entries 目标都有 query", not no_q, f"缺失: {no_q}")
        # 缩略图缓存必须指向真实文件名（旧规则写成 Explorer\* + IconCache.db
        # 的链式路径，整条规则从未命中过）
        th = next(r for r in rules if r.key == "thumbnail_cache")
        pats = [q for t in th.targets for q in t.query]
        check("★ 缩略图缓存指向真实缓存文件名",
              any("iconcache_" in p for p in pats)
              and any("thumbcache_" in p for p in pats), pats)
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

    # ---------- B3. 还原点：只统计「已用空间」 ----------
    # 旧实现把 vssadmin 每个卷的三行（已用 / 分配 / 最大）括号里的字节数
    # 全部相加，估算约为真实值的 3 倍，「已清理」数字虚高。
    try:
        zh_vss = (
            "已用卷影副本存储关联: C: (\\\\?\\Volume{1}\\)\n"
            "   卷影副本存储卷: (卷 \\\\?\\Volume{2}\\)\n"
            "   已用空间:           1.23 GB (1,320,124,416 字节)\n"
            "   分配的空间:         10.0 GB (10,737,418,240 字节)\n"
            "   最大空间:           20.0 GB (21,474,836,480 字节)\n"
        )
        v = cl._parse_shadowstorage_used(zh_vss)
        check("★ 还原点只统计已用空间（非三行相加）", v == 1320124416, v)
        en_vss = ("Used: 2.00 GB (2,147,483,648 bytes)\n"
                  "Allocated: 8.00 GB (8,589,934,592 bytes)\n"
                  "Maximum: 20.0 GB (21,474,836,480 bytes)\n")
        check("还原点解析-英文只取 Used",
              cl._parse_shadowstorage_used(en_vss) == 2147483648,
              cl._parse_shadowstorage_used(en_vss))
        check("还原点解析-空输入返回 0", cl._parse_shadowstorage_used("") == 0)
    except Exception as exc:
        check("还原点解析", False, f"异常: {exc}")

    # ---------- C. 安全护栏 ----------
    try:
        crit = cl._critical_roots()
        win = os.environ.get("SystemRoot", r"C:\Windows")
        check("护栏包含 Windows/System32",
              os.path.normpath(win).lower() in crit
              and os.path.normpath(os.path.join(win, "System32")).lower() in crit)
        # 构造一个恶意/错误的 contents 目标（指向 Windows 本身）→ 清理必须被拒
        evil = cl.Target(root=win, mode="contents")
        ok, fail, vol, _sk = cl._clean_target(evil)
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

        total, n, _to = cl._scan_target(tgt)
        check("扫描体积正确", total == 4096 + 2048 and n == 2,
              f"total={total} n={n}")

        ok, fail, vol, _sk = cl._clean_target(tgt)
        check("entries 清理正确", ok == 2 and fail == 0 and vol == 4096 + 2048,
              f"ok={ok} vol={vol}")
        check("当前版本保留", os.path.isdir(cur) and os.path.isfile(fake_exe))
        check("老版本已删", not os.path.exists(old1) and not os.path.exists(old2))

        # contents 模式：清空内容、保留目录
        holder = os.path.join(sandbox, "Holder")
        os.makedirs(os.path.join(holder, "junk"))
        with open(os.path.join(holder, "junk", "a.bin"), "wb") as f:
            f.write(b"Z" * 512)
        ok2, fail2, vol2, _sk2 = cl._clean_target(cl.Target(root=holder, mode="contents"))
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
        ok3, _, vol3, _s3 = cl._clean_target(wt)
        check("通配 root 展开", ok3 == 1 and vol3 == 128, f"ok={ok3} vol={vol3}")
    except Exception as exc:
        check("沙箱验证", False, f"异常: {exc}")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    # ---------- D2. query 语义：并联 + 多段 / contents 遵守 excluded ----------
    # 2026-10-19 修复的三处「静默失效」，这里必须各有一条**真调用**的断言：
    #   ① 多段相对路径（Local\Microsoft\Windows\*.log）以前拿整串与单个文件
    #      名比较，永不命中；
    #   ② 多个 query 被当成逐级串联（AMD\Intel\NVIDIA），并联语义才是对的；
    #   ③ contents 模式整段忽略 excluded —— ClickToRun 会连 MachineData /
    #      UserData 一起清掉，后果是 Office 无法修复/卸载。
    sandbox3 = tempfile.mkdtemp(prefix="yuhub_cln_sem_")
    try:
        deep = os.path.join(sandbox3, "Users", "u1", "AppData", "Local",
                            "Microsoft", "Windows")
        os.makedirs(deep)
        for n in ("a.log", "b.log", "keep.txt"):
            with open(os.path.join(deep, n), "wb") as f:
                f.write(b"L" * 10)
        t_deep = cl.Target(root=os.path.join(sandbox3, "Users", "u1", "AppData"),
                           query=(r"Local\Microsoft\Windows\*.log",),
                           file_only=True, mode="entries")
        hits = sorted(os.path.basename(p) for p in cl._iter_matches(
            t_deep.root, t_deep, []))
        check("★ 多段 query 能逐级下钻（命中 2 个 *.log，不误伤 txt）",
              hits == ["a.log", "b.log"], hits)
        ok, fail, vol, _sk = cl._clean_target(t_deep)
        left = sorted(os.listdir(deep))
        check("多段 query 真能删掉（保留 keep.txt）",
              ok == 2 and vol == 20 and left == ["keep.txt"],
              f"ok={ok} vol={vol} left={left}")

        drv = os.path.join(sandbox3, "Drv")
        for n in ("AMD", "Intel", "NVIDIA", "Other"):
            os.makedirs(os.path.join(drv, n))
        t_or = cl.Target(root=drv, query=("AMD", "Intel", "NVIDIA"),
                         mode="entries")
        names = sorted(os.path.basename(p) for p in cl._iter_matches(
            drv, t_or, []))
        check("★ 多个 query 是并联（AMD/Intel/NVIDIA 全中，Other 不中）",
              names == ["AMD", "Intel", "NVIDIA"], names)
        ok, fail, _, _sk = cl._clean_target(t_or)
        left = sorted(os.listdir(drv))
        check("并联模式真能带走全部三项", ok == 3 and left == ["Other"],
              f"ok={ok} left={left}")

        c2r = os.path.join(sandbox3, "ClickToRun")
        for n in ("MachineData", "UserData", "PackageFiles"):
            os.makedirs(os.path.join(c2r, n))
            with open(os.path.join(c2r, n, "x.bin"), "wb") as f:
                f.write(b"Q" * 256)
        t_ex = cl.Target(root=c2r, excluded=("MachineData", "UserData"),
                         mode="contents")
        plan = {r: [os.path.basename(x) for x in ch]
                for r, ch in cl._contents_plan(t_ex)}
        check("★ contents 模式尊重 excluded（计划里只剩 PackageFiles）",
              plan.get(c2r) == ["PackageFiles"], plan)
        ok, fail, vol, _sk = cl._clean_target(t_ex)
        left = sorted(os.listdir(c2r))
        check("★ contents 清理后 MachineData/UserData 仍在",
              ok == 1 and vol == 256 and left == ["MachineData", "UserData"],
              f"ok={ok} vol={vol} left={left}")

        # ⑤ 重叠 root 不能重复统计：%Temp% 与 Users\*\AppData\Local\Temp
        #    在"当前用户"身上就是同一个目录，不去重会把同一份文件算两遍。
        ov = os.path.join(sandbox3, "ov", "Temp")
        os.makedirs(ov)
        with open(os.path.join(ov, "t.tmp"), "wb") as f:
            f.write(b"O" * 1000)
        rule_ov = cl.CleanRule(
            key="ov", name="ov", desc="x", group=cl.GROUP_TEMP, level=3,
            targets=[cl.Target(root=ov), cl.Target(root=ov)])
        n_ov = cl.scan_rule(rule_ov)
        check("★ 重叠 root 只统计一次", n_ov == 1000, n_ov)
        res_ov = cl.clean_rule(rule_ov)
        check("★ 重叠 root 清理量与扫描一致",
              res_ov.freed == 1000 and res_ov.removed_count == 1,
              f"freed={res_ov.freed} count={res_ov.removed_count}")

        # ⑥ 被占用的文件必须归入 RM_IN_USE（"跳过"），不能报成"权限不足"。
        #    这是用户看到的"N 项被占用或者权限不足"假警报的根源：Windows 上
        #    Python 打开文件不带 FILE_SHARE_DELETE，删除会拿到 WinError 32。
        lk = os.path.join(sandbox3, "locky")
        os.makedirs(os.path.join(lk, "sub"))
        with open(os.path.join(lk, "sub", "free.txt"), "w") as f:
            f.write("F" * 32)
        busy = os.path.join(lk, "sub", "busy.txt")
        with open(busy, "w") as f:
            f.write("B" * 32)
        holder = open(busy, "r")
        try:
            kind = cl._rm_path_kind(lk)
            sub_left = (sorted(os.listdir(os.path.join(lk, "sub")))
                        if os.path.isdir(os.path.join(lk, "sub")) else [])
            check("★ 被占用归入 RM_IN_USE（不是权限不足）",
                  kind == cl.RM_IN_USE, f"kind={kind}")
            check("★ 同目录未占用的文件照样删掉（能清多少清多少）",
                  "free.txt" not in sub_left, sub_left)

            # clean_rule 的计数口径要用**另一棵**树：上面那棵的 free.txt
            # 已经被删掉了，再清一次释放量当然是 0（会自欺欺人地测不出东西）。
            lk2 = os.path.join(sandbox3, "locky2")
            os.makedirs(os.path.join(lk2, "sub"))
            with open(os.path.join(lk2, "sub", "free.txt"), "w") as f:
                f.write("F" * 32)
            busy2 = os.path.join(lk2, "sub", "busy.txt")
            with open(busy2, "w") as f:
                f.write("B" * 32)
            holder2 = open(busy2, "r")
            try:
                locked_rule = cl.CleanRule(
                    key="lk", name="lk", desc="x", group=cl.GROUP_TEMP, level=3,
                    targets=[cl.Target(root=lk2)])
                r_lk = cl.clean_rule(locked_rule)
                check("★ 清理结果把占用计入 skipped 而非 failed",
                      r_lk.skipped >= 1 and r_lk.failed == 0,
                      f"skipped={r_lk.skipped} failed={r_lk.failed}")
                check("★ 部分删除时释放量取实测差值（不拿磁盘读数充数）",
                      r_lk.freed == 32, f"freed={r_lk.freed}")
            finally:
                holder2.close()
            check("★ 释放占用后可以删干净",
                  cl._rm_path_kind(lk2) == cl.RM_OK
                  and not os.path.exists(lk2))
        finally:
            holder.close()
        check("★ 释放占用后可以删干净",
              cl._rm_path_kind(lk) == cl.RM_OK and not os.path.exists(lk))

        notes = []
        bad = cl.Target(root=os.environ.get("SystemRoot", r"C:\Windows"),
                        mode="contents")
        ok_b, fail_b, vol_b, _skb = cl._clean_target(bad, notes=notes)
        check("★ 关键目录被护栏拦下并给出说明（不再静默）",
              ok_b == 0 and vol_b == 0 and any("护栏" in n for n in notes),
              f"ok={ok_b} notes={notes}")
        check("护栏后 Windows 目录仍在", os.path.isdir(
            os.environ.get("SystemRoot", r"C:\Windows")))
    except Exception as exc:
        check("query 语义", False, f"异常: {exc}")
    finally:
        shutil.rmtree(sandbox3, ignore_errors=True)

    # ---------- E2. 扫描不能"卡死"：预算截断 + 可取消 + 逐条目进度 ----------
    # 用户实测"扫描有出现卡死"：一条规则碰到畸形/超大目录时，以前没有任何
    # 上限，整轮扫描（以及界面的完成回调）就永远等在那里。现在有三道保险：
    # 规则级预算（超时标 partial 并继续）、measure 每 256 项查一次取消、
    # 逐条目进度回调（界面显示"正在扫描 XXX"，长目录不再像死机）。
    sandbox4 = tempfile.mkdtemp(prefix="yuhub_cln_budget_")
    try:
        import threading as _th2
        big = os.path.join(sandbox4, "big")
        for i in range(40):
            sub = os.path.join(big, "d%02d" % i)
            os.makedirs(sub)
            for j in range(50):
                with open(os.path.join(sub, "f%d.bin" % j), "wb") as f:
                    f.write(b"x" * 8)

        budget_rule = cl.CleanRule(key="bg", name="bg", desc="x",
                                   group=cl.GROUP_TEMP, level=3,
                                   targets=[cl.Target(root=big)])
        t0 = time.monotonic()
        cl.scan_rule(budget_rule, budget=0.0)
        cost = time.monotonic() - t0
        check("★ 预算用尽立刻收尾并标记 partial（不卡死）",
              budget_rule.partial and cost < 5.0,
              f"partial={budget_rule.partial} cost={cost:.2f}s")

        cancel_rule = cl.CleanRule(key="cx", name="cx", desc="x",
                                   group=cl.GROUP_TEMP, level=3,
                                   targets=[cl.Target(root=big)])
        t0 = time.monotonic()
        sz = cl.scan_rule(cancel_rule, cancel=lambda: True)
        cost = time.monotonic() - t0
        check("★ 取消后立刻返回", sz == 0 and cost < 1.0, f"cost={cost:.2f}s")

        notes = []
        cl.scan_rule(budget_rule, budget=60, note=notes.append)
        check("★ 逐条目进度回调生效（带可读路径）",
              len(notes) >= 1 and os.path.isdir(notes[0]), notes[:2])

        full = cl.CleanRule(key="fg", name="fg", desc="x",
                            group=cl.GROUP_TEMP, level=3,
                            targets=[cl.Target(root=big)])
        total = cl.scan_rule(full, budget=None)
        check("完整扫描体积准确且未截断",
              total == 40 * 50 * 8 and not full.partial,
              f"total={total} partial={full.partial}")

        got = []
        done_evt = _th2.Event()
        sc = cl.CleanerScanner()
        sc.start([full], on_progress=lambda r: None,
                 on_done=lambda t, rs: done_evt.set(),
                 on_note=lambda r, txt: got.append(txt))
        done_evt.wait(15)
        check("★ 扫描器转发逐条目进度（界面『正在扫描 XXX』）",
              len(got) >= 1, got[:2])

        # 外部命令（DISM 分析）必须能被"停止扫描"打断：
        # 以前 subprocess.run(timeout=) 只能干等，按钮点了没反应。
        t0 = time.monotonic()
        rc = cl._run_proc([sys.executable, "-c", "import time; time.sleep(5)"],
                          30, cancel=lambda: True)
        cost = time.monotonic() - t0
        check("★ 外部命令可被取消（kill 后立即返回）",
              rc is None and cost < 3.0, f"cost={cost:.2f}s")
    except Exception as exc:
        check("扫描预算/取消", False, f"异常: {exc}")
    finally:
        shutil.rmtree(sandbox4, ignore_errors=True)

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
        ok4, _, vol4, _s4 = cl._clean_target(wt)
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
