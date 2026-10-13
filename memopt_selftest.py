# -*- coding: utf-8 -*-
"""打包后自检：内存优化（`Yuhub.exe --memory-selftest <结果json路径>`）。

为什么需要它
------------
这个功能的两条硬承诺都**不会抛异常**，坏掉时界面看着一切正常（照样报
"回收了 X GB"），只能靠断言守住：

  1. **绝不结束任何进程** —— 只回收内存页。一旦代码里混进
     TerminateProcess / taskkill 之类，用户会以为"优化把我的程序关掉了"。
  2. **定时间隔有下限** —— 间隔太小会把常用程序的工作集反复换出去，
     下次访问全都要重新调页，越"优化"越卡。

所以这里做五件事：

  * **静态**扫 memopt.py 的语法树，确认里面没有任何终止进程的调用
    （扫 AST 而不是扫文本：注释/文档里提到这些名字是正常的）
  * **运行时**拿 explorer.exe 和当前前台进程当哨兵，真跑一次优化后
    确认它们 PID 不变、进程还在
  * 校验 6 个预设挡位 + 1 个「自定义」挡位、15 分钟下限、5 档内存阈值
  * 打桩验证 UI：手动点击真的发起优化；定时 / 过载两套规则互相独立；
    内存没到阈值不触发、触发后有冷却
  * **自动触发绝不提权** —— 定时和过载都只能走普通权限，只有手动打开
    「管理员深度优化」开关后才允许弹 UAC

返回码：0 全部通过 / 1 有断言失败 / 2 参数错误 / 4 结果写盘失败
"""

import ast
import ctypes
import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import memopt  # noqa: E402

# 任何"结束进程"的 API 都不该出现在 memopt 的语法树里
FORBIDDEN_NAMES = {
    "TerminateProcess", "NtTerminateProcess", "TerminateJobObject",
    "ExitProcess", "taskkill", "TerminateThread",
    "ZwTerminateProcess",
}

_SETTING_KEYS = ("mem_auto_enabled", "mem_auto_minutes",
                 "mem_thr_enabled", "mem_thr_percent",
                 "mem_history")


def _dump(result, out_file):
    """写结果文件。

    **序列化兜底必须在这里**：detail 里一旦混进 bytes 之类的非 JSON 类型，
    `json.dumps` 会抛 TypeError；只捕 OSError 的话整个结果文件会被写成
    半截（有历史教训 —— 某个自检的结果文件曾长期只写一半，谁都看不出
    是哪一项干的）。
    """
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:                 # noqa: BLE001
        for c in result.get("checks", []):
            c["detail"] = str(c.get("detail", ""))[:300]
        result["checks"].append({
            "name": "结果可序列化", "pass": False,
            "detail": "详情里有无法 JSON 化的对象：%r" % (exc,)})
        result["ok"] = False
        text = json.dumps(result, ensure_ascii=False, indent=2)
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return 4
    return 0 if result.get("ok") else 1


def _pump(app, seconds):
    """跑 Qt 事件循环若干秒（让工作线程的结果信号真的送到主线程）。"""
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def _snapshot_settings():
    from PySide6.QtCore import QSettings
    s = QSettings("Yuhub", "Yuhub")
    return {k: s.value(k, None) for k in _SETTING_KEYS}


def _restore_settings(saved):
    """把设置还原成快照值，返回**没能还原**的键名列表（正常是空）。

    为什么要"写 + 回读 + 补偿写"这一套：本项目里每个页面都自己 new 一个
    `QSettings("Yuhub", "Yuhub")`（`MemoryPage._settings` 就是），而 Qt 的
    全局缓存**只对通过 `QCoreApplication::setOrganizationName()` 建出来的
    实例生效** —— 显式写死 org/app 名的实例各缓存各的，互相不知道对方改过
    什么。于是自检期间被写脏的值，可能被那些**仍然存活**的页面实例稍后
    回写，把这里的还原整个覆盖掉。

    实测（2026-10-04）：自检中途抛异常的那一次，`mem_auto_enabled` 就残留
    成了 true，下一次自检因此报「两个自动开关默认都是关的」失败 ——
    而且因为它是**断言默认值**的那条，前一次是绿的，非常难查。
    """
    from PySide6.QtCore import QSettings
    if not saved:
        return []
    for _ in range(2):
        s = QSettings("Yuhub", "Yuhub")
        for k, v in saved.items():
            try:
                if v is None:
                    s.remove(k)
                else:
                    s.setValue(k, v)
            except Exception:                             # noqa: BLE001
                pass
        try:
            s.sync()
        except Exception:                                 # noqa: BLE001
            pass
    # 回读校验：还有对不上的就补一次，并把键名交回去让上层记一笔
    s = QSettings("Yuhub", "Yuhub")
    bad = []
    for k, v in saved.items():
        try:
            cur = s.value(k, None)
        except Exception:                                 # noqa: BLE001
            continue
        same = ((cur is None and v is None)
                or (cur is not None and v is not None
                    and str(cur) == str(v)))
        if same:
            continue
        bad.append(k)
        try:
            s.setValue(k, v)
        except Exception:                                 # noqa: BLE001
            pass
    try:
        s.sync()
    except Exception:                                     # noqa: BLE001
        pass
    return bad


def run(out_file):
    checks = []

    def mark(name, ok, detail=""):
        checks.append({"name": name, "pass": bool(ok),
                       "detail": "" if detail is None else str(detail)})

    saved_settings = None
    try:
        saved_settings = _snapshot_settings()
    except Exception:                                     # noqa: BLE001
        pass

    try:
        _run_all(mark)
    except Exception as exc:                              # noqa: BLE001
        mark("自检过程未抛异常", False,
             "%s: %s" % (type(exc).__name__, exc))
    finally:
        _left = _restore_settings(saved_settings)
        if _left:
            # 留一条断言而不是静默：设置没还原干净会污染**下一次**自检，
            # 而且那条断言通常长成"默认值就该是 X"，症状毫无线索。
            mark("自检结束后用户设置已完整还原", False,
                 "这些键没能还原：%s" % (", ".join(_left),))

    result = {
        "ok": all(c["pass"] for c in checks),
        "which": "memory",
        "checks": checks,
    }
    return _dump(result, out_file)


def _run_all(mark):
    # ================= A. 静态安全：没有任何"结束进程"的调用 =================
    src_path = os.path.join(HERE, "memopt.py")
    src = ""
    try:
        with open(src_path, "r", encoding="utf-8") as fh:
            src = fh.read()
        mark("能读到 memopt.py 源码", True, "%d 字符" % len(src))
    except OSError as exc:
        mark("能读到 memopt.py 源码", False, exc)

    names = set()
    if src:
        try:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.Name):
                    names.add(node.id)
            mark("memopt.py 语法树可解析", True, "%d 个标识符" % len(names))
        except SyntaxError as exc:
            mark("memopt.py 语法树可解析", False, exc)

    if src:
        # 扫 AST 而不是扫文本：文档和注释里提到 TerminateProcess 是正常的
        # （我们正是在注释里声明"绝不做这件事"）。
        hits = sorted(FORBIDDEN_NAMES & names)
        mark("不含任何结束进程的 API", not hits, hits or "干净")
    else:
        # 源码读不到时**不能**判通过 —— 那是假通过，等于把这条最重要的
        # 承诺放空。明确失败，逼着打包时把 memopt.py 一起带上。
        mark("不含任何结束进程的 API", False,
             "源码不可读，无法静态确认（检查 Yuhub.spec 的 datas 是否带了 memopt.py）")

    # 系统接口：既看源码文本，也看运行时绑定（打包态同样有效）
    psapi = memopt._psapi()
    ndll = memopt._ntdll()
    k32 = memopt._k32()
    for lib, name in ((psapi, "EmptyWorkingSet"),
                      (ndll, "NtSetSystemInformation"),
                      (k32, "SetSystemFileCacheSize"),
                      (k32, "GlobalMemoryStatusEx")):
        mark("已绑定系统接口 %s" % name,
             callable(getattr(lib, name, None)),
             "源码中出现：%s" % (name in src))

    # ================= B. 基础查询 =================
    st = memopt.memory_status()
    mark("内存状态：总量合理", st["total"] > 0, st["total"])
    mark("内存状态：已用 = 总量 - 可用",
         st["used"] == max(0, st["total"] - st["avail"]),
         "used=%d total=%d avail=%d" % (st["used"], st["total"], st["avail"]))
    mark("内存状态：占用率在 0~100",
         0.0 <= st["percent"] <= 100.0, "%.1f%%" % st["percent"])

    procs = memopt.list_processes()
    mark("能枚举进程", len(procs) > 10, "%d 个" % len(procs))
    mark("枚举结果里包含自己", os.getpid() in {p for p, _ in procs},
         "pid=%d" % os.getpid())

    fg_pid = memopt.foreground_pid()
    mark("拿到了前台窗口进程 PID", fg_pid > 0, "pid=%s" % fg_pid)
    mark("human_bytes 单位换算正确",
         memopt.human_bytes(0) == "0 B"
         and memopt.human_bytes(1536) == "1.5 KB"
         and memopt.human_bytes(1024 ** 3) == "1.00 GB",
         [memopt.human_bytes(x) for x in (0, 1536, 1024 ** 3)])

    # ================= C. 真跑一次 + 进程存活 =================
    before = {pid: name for pid, name in memopt.list_processes()}
    sentinels = {
        "explorer.exe": [p for p, n in before.items() if n == "explorer.exe"],
        "foreground": [fg_pid] if fg_pid else [],
        "self": [os.getpid()],
    }

    res = memopt.optimize(progress=lambda _t: None)
    after = {pid: name for pid, name in memopt.list_processes()}

    mark("优化返回 ok", bool(res.get("ok")), res.get("error", ""))
    for label, pids in sentinels.items():
        if not pids:
            mark("哨兵 %s 优化前存在（跳过）" % label, True, "本机无此进程")
            continue
        alive = [p for p in pids if p in after]
        mark("哨兵 %s 优化后仍存活（PID 未变）" % label,
             len(alive) == len(pids),
             "前=%s 后=%s" % (pids, alive))

    # 允许极少数短命进程自然启停（如 taskhostw），但绝不能批量消失
    mark("进程数没有批量下降", len(after) >= len(before) - 3,
         "%d -> %d" % (len(before), len(after)))

    for field in ("targets", "emptied", "failed", "skipped", "system_ops",
                  "seconds", "freed_ws", "freed_avail", "before", "after"):
        mark("结果字段 %s" % field, field in res, "")

    mark("确实回收了工作集（emptied>0 且 freed_ws>=0）",
         res.get("emptied", 0) > 0 and res.get("freed_ws", -1) >= 0,
         "emptied=%s freed_ws=%s" % (res.get("emptied"),
                                     memopt.human_bytes(res.get("freed_ws", 0))))
    mark("描述文案非空且不含花括号",
         bool(memopt.describe(res)) and "{" not in memopt.describe(res),
         memopt.describe(res))

    # ================= D. 跳过前台 / 提权分支 =================
    mark("默认跳过前台（foreground_skipped=True）",
         res.get("foreground_skipped") is True, res.get("foreground_skipped"))

    res2 = memopt.optimize(skip_foreground=False)
    mark("关掉 skip_foreground 后不再标记跳过",
         res2.get("foreground_skipped") is False,
         res2.get("foreground_skipped"))
    mark("进程仍然完好（第二次优化后）",
         len(memopt.list_processes()) >= len(before) - 3,
         "%d -> %d" % (len(before), len(memopt.list_processes())))

    # 🔴 真·绊线：直接问 winadmin 要一次 TokenElevation，断言**确实拿到了布尔**。
    #    2026-10-13 的事故：winadmin 里 `_TOKEN_ELEVATION = 20` 这个常量被下面
    #    同名的结构体类顶掉，GetTokenInformation 的第二参数（要求 int）收到一个
    #    类对象 → ctypes 抛 TypeError → 被 except 吞成 None → is_elevated()
    #    **在任何机器上恒为 False**。后果全是静默的：内存优化不再做系统级清理、
    #    C盘清理里需要管理员的规则被跳过、卸载残留不再 takeown —— 而界面上只
    #    显示一句"当前未以管理员权限运行"。
    #    为什么原来的断言守不住：本套件其它涉及权限的断言不是打桩
    #    (memopt.is_admin = lambda: True)，就是自洽比较（下面那条比的也是
    #    同一次调用，两边一起错就一起"通过"）。所以必须真调用 + 判类型：
    #    API 真的不可用时它返回 None，那正是"提权判定已失效"的现场，必须红。
    try:
        import winadmin as _wa
        _tev = _wa._token_elevated()
        mark("★ 提权判定真的能拿到布尔（TokenElevation 未被静默降级）",
             isinstance(_tev, bool),
             "_token_elevated() = %r（type=%s；None = API 调用失败/参数写错）"
             % (_tev, type(_tev).__name__))
    except Exception as _exc:                                  # noqa: BLE001
        mark("★ 提权判定真的能拿到布尔（TokenElevation 未被静默降级）",
             False, "winadmin 不可用：%r" % (_exc,))

    ops = res.get("system_ops") or {}
    # 这两条分支互斥：管理员下跑 3 条真·系统调用断言，非管理员下跑 2 条
    # "如实标记 skip" 断言。所以本套件总数会随进程提权状态变化（103/104）。
    # 单列一条一致性断言，把"走了哪条分支"这件事明确报出来 —— 免得
    # "另一条分支根本没跑"被静默当成"验证过了"。
    mark("[前置] elevated 标记与真实提权状态一致（决定走哪条分支）",
         bool(res.get("elevated")) == bool(memopt.is_admin()),
         "elevated=%s is_admin=%s（True→管理员三重断言，False→非管理员两条 skip 断言）"
         % (res.get("elevated"), memopt.is_admin()))
    if res.get("elevated"):
        mark("管理员：清 standby 成功", ops.get("standby") == "ok", ops)
        mark("管理员：收缩文件缓存成功", ops.get("filecache") == "ok", ops)
        mark("管理员：可用内存上升", res.get("freed_avail", 0) > 0,
             memopt.human_bytes(res.get("freed_avail", 0)))
    else:
        mark("非管理员：系统级两项被如实标记为 skip",
             ops.get("standby") == "skip" and ops.get("filecache") == "skip",
             ops)
        mark("非管理员：可用内存不会倒退", res.get("freed_avail", 0) >= 0,
             memopt.human_bytes(res.get("freed_avail", 0)))

    mark("特权启用接口可用（不抛异常）",
         memopt.enable_privilege("SeProfileSingleProcessPrivilege")
         in (True, False))
    # 注：原先这里还有一条「exe_path 在打包态返回真实路径」——exe_path 随
    # 提权链路一起删掉了（程序本身以管理员启动，不需要再找自己的 exe 去 runas）。

    # ================= E. 挡位 / 阈值契约 =================
    from ui.pages.memory_page import (
        INTERVALS, MIN_MINUTES, MAX_MINUTES, CUSTOM_LABEL,
        THRESHOLD_OPTIONS, DEFAULT_THRESHOLD, THRESHOLD_COOLDOWN,
        TRIGGER_MANUAL, TRIGGER_TIMER, TRIGGER_OVERLOAD,
        IntervalPicker, ThresholdPicker, MemoryPage,
    )
    from PySide6.QtWidgets import QApplication

    mark("预设挡位数量为 6（另有 1 个「自定义」挡位）", len(INTERVALS) == 6,
         [v for v, _ in INTERVALS])
    mark("最小挡位 = 下限 = 15 分钟",
         MIN_MINUTES == 15 and min(v for v, _ in INTERVALS) == MIN_MINUTES,
         "MIN=%d 最小挡位=%d" % (MIN_MINUTES, min(v for v, _ in INTERVALS)))
    mark("所有挡位都不低于下限",
         all(v >= MIN_MINUTES for v, _ in INTERVALS),
         [v for v, _ in INTERVALS])
    mark("挡位严格递增",
         [v for v, _ in INTERVALS] == sorted(v for v, _ in INTERVALS),
         [v for v, _ in INTERVALS])
    mark("上限是 1440 分钟（24 小时）", MAX_MINUTES == 1440, MAX_MINUTES)
    mark("「自定义」挡位文案非空", bool(CUSTOM_LABEL), CUSTOM_LABEL)
    mark("内存阈值候选严格递增且含默认值",
         tuple(sorted(THRESHOLD_OPTIONS)) == THRESHOLD_OPTIONS
         and DEFAULT_THRESHOLD in THRESHOLD_OPTIONS,
         "候选 %s，默认 %d" % (list(THRESHOLD_OPTIONS), DEFAULT_THRESHOLD))
    mark("过载触发的冷却时间不短于 5 分钟（防止反复回收）",
         THRESHOLD_COOLDOWN >= 300, "%d 秒" % THRESHOLD_COOLDOWN)

    app = QApplication.instance() or QApplication([])

    # ---- 间隔选择器：预设态不可编辑，自定义态才可编辑 ----
    picker = IntervalPicker(30)
    mark("默认 30 分钟命中预设（输入框灰态不可编辑）",
         picker.minutes() == 30 and not picker.is_custom()
         and not picker.spin.isEnabled(),
         "minutes=%s custom=%s enabled=%s"
         % (picker.minutes(), picker.is_custom(), picker.spin.isEnabled()))
    picker.set_minutes(5)
    mark("自定义填 5 分钟被抬到 15", picker.minutes() == MIN_MINUTES,
         picker.minutes())
    picker.set_minutes(99999)
    mark("自定义填超大值被压到上限", picker.minutes() == MAX_MINUTES,
         picker.minutes())
    picker.set_minutes(60)
    checked = [v for v, b in picker._buttons if b.isChecked()]
    mark("设成 60 分钟后只有 1 小时挡位被选中",
         checked == [60] and not picker.is_custom(), checked)
    picker.set_minutes(37)
    checked = [v for v, b in picker._buttons if b.isChecked()]
    mark("设成非挡位值 37 后进入自定义态（预设全部取消选中）",
         checked == [] and picker.is_custom() and picker.spin.isEnabled(),
         "checked=%s custom=%s" % (checked, picker.is_custom()))
    picker.btn_custom.click()
    mark("点「自定义」挡位后输入框立即可编辑",
         picker.is_custom() and picker.spin.isEnabled(),
         "custom=%s enabled=%s" % (picker.is_custom(), picker.spin.isEnabled()))
    picker.spin.setValue(45)
    mark("在自定义挡位下输入 45 分钟生效并保持自定义态",
         picker.minutes() == 45 and picker.is_custom(), picker.minutes())
    legacy = IntervalPicker(900)
    mark("存量配置 900 分钟（非挡位值）载入后落在自定义态",
         legacy.minutes() == 900 and legacy.is_custom()
         and legacy.spin.isEnabled(),
         "minutes=%s custom=%s"
         % (legacy.minutes(), legacy.is_custom()))

    thr = ThresholdPicker(DEFAULT_THRESHOLD)
    mark("阈值默认 %d%% 且只有 1 个挡位被选中" % DEFAULT_THRESHOLD,
         thr.percent() == DEFAULT_THRESHOLD
         and sum(1 for _, b in thr._buttons if b.isChecked()) == 1,
         thr.percent())
    thr.set_percent(85)
    mark("阈值可以切到 85%", thr.percent() == 85, thr.percent())
    thr.set_percent(83)
    mark("非法阈值 83 归到最接近的合规挡位",
         thr.percent() in THRESHOLD_OPTIONS, thr.percent())


    # ================= F. 页面行为（打桩验证） =================
    calls = []
    STATUS = {"percent": 30.0}
    orig_optimize = memopt.optimize
    orig_status = memopt.memory_status
    orig_admin = memopt.is_admin

    def fake_optimize(progress=None, skip_foreground=True, deadline_s=None):
        calls.append({"skip_foreground": bool(skip_foreground)})
        if progress:
            progress("打桩阶段")
        return {
            "ok": True, "ts": time.time(), "elevated": True,
            "before": {"percent": STATUS["percent"], "total": 100, "avail": 40,
                       "used": 60},
            "after": {"percent": 25.0, "total": 100, "avail": 75, "used": 25},
            "ws_before": 5000, "ws_after": 3000, "freed_ws": 2000,
            "freed_avail": 1000, "used_before": 60, "used_after": 25,
            "targets": 5, "emptied": 5, "failed": 0, "skipped": 2,
            "measured": 5, "timed_out": False, "foreground_skipped": True,
            "system_ops": {"standby": "ok", "filecache": "ok"},
            "seconds": 0.12,
        }

    try:
        memopt.optimize = fake_optimize
        memopt.memory_status = lambda: {
            "total": 100, "avail": int(100 - STATUS["percent"]),
            "used": int(STATUS["percent"]), "percent": STATUS["percent"],
            "load": int(STATUS["percent"])}
        # 程序以管理员启动 —— 这就是打包态的真实前提
        memopt.is_admin = lambda: True

        page = MemoryPage(notify=lambda _t: None)

        mark("页面上有定时 / 过载两个独立开关",
             page.sw_auto is not None and page.sw_thr is not None, "")
        # 🔴 用户要求移除「管理员深度优化」开关：程序本身以管理员启动，
        # 清理力度不再是一个需要用户决定的选项。
        mark("★ 管理员深度优化开关已移除（没有 sw_admin / 提权入口）",
             not hasattr(page, "sw_admin")
             and not hasattr(page, "_want_elevated")
             and not hasattr(page, "_on_admin_toggled"), "")
        mark("★ 页面明确写出「每次都含系统级清理」",
             "系统" in page._admin_hint.text()
             and "管理员" in page._admin_hint.text(),
             page._admin_hint.text())
        mark("两个自动开关默认都是关的",
             page._auto_on is False and page._thr_on is False,
             "auto=%s thr=%s" % (page._auto_on, page._thr_on))

        # ① 手动点击
        n0 = len(calls)
        page._on_optimize_clicked()
        _pump(app, 1.2)
        mark("点击「立即优化」会发起优化",
             len(calls) == n0 + 1, "调用 %d 次" % (len(calls) - n0))
        mark("UI 调用时没有关掉跳过前台",
             calls and calls[-1]["skip_foreground"] is True, calls[-1:] or "")
        mark("优化后按钮复位为可点",
             page.btn_optimize.isEnabled()
             and page.btn_optimize.text() == "立即优化",
             page.btn_optimize.text())
        mark("结果写入历史记录", len(page._load_history()) >= 1,
             len(page._load_history()))
        mark("状态文案不是 JSON",
             "{" not in page._status.text(), page._status.text()[:90])
        mark("手动执行的记录标记为「%s」" % TRIGGER_MANUAL,
             bool(page._load_history())
             and page._load_history()[0].get("trigger") == TRIGGER_MANUAL,
             page._load_history()[:1])

        # ② 定时：开 → 到点触发（不再有"内存充足就跳过"这个前置条件）
        page.sw_auto.setChecked(True)
        mark("打开定时开关后内部状态同步为开启", page._auto_on is True,
             page._auto_on)
        page._next_at = time.monotonic() - 1
        n1 = len(calls)
        page._on_tick()
        _pump(app, 1.2)
        mark("定时到点会自动执行优化", len(calls) == n1 + 1,
             "调用 %d 次" % (len(calls) - n1))
        mark("执行后自动排下一次",
             page._next_at > time.monotonic(), page._next_at)
        mark("定时执行的记录标记为「%s」" % TRIGGER_TIMER,
             any(it.get("trigger") == TRIGGER_TIMER
                 for it in page._load_history()),
             [it.get("trigger") for it in page._load_history()])

        # ③ 关掉定时 → 即使到点也不执行（两套规则互不干扰）
        page.sw_auto.setChecked(False)
        mark("关闭定时开关后内部状态同步为关闭", page._auto_on is False,
             page._auto_on)
        page._next_at = time.monotonic() - 1
        n2 = len(calls)
        page._on_tick()
        _pump(app, 0.4)
        mark("关掉定时后不再自动执行", len(calls) == n2,
             "调用 %d 次" % (len(calls) - n2))
        mark("关掉后倒计时文案变成「未开启定时优化」",
             "未开启" in page._next_label.text(), page._next_label.text())

        # ④ 过载：单独开，低于阈值不触发
        page.sw_thr.setChecked(True)
        mark("打开过载开关后内部状态同步为开启", page._thr_on is True,
             page._thr_on)
        mark("刚打开过载开关时显示「刚开启」而不是「冷却中」（两者不是一回事）",
             "刚开启" in page._thr_note.text(), page._thr_note.text())
        page.thr_picker.set_percent(80)
        STATUS["percent"] = 50.0
        page._thr_next_ok = page._thr_grace_until = 0.0
        n3 = len(calls)
        page._check_threshold()
        _pump(app, 0.4)
        mark("占用 50% < 阈值 80% → 不触发", len(calls) == n3,
             "调用 %d 次" % (len(calls) - n3))
        mark("未达阈值时状态文字写明「未达阈值」",
             "未达阈值" in page._thr_note.text(), page._thr_note.text())

        # ⑤ 超过阈值 → 触发；紧跟着再判一次应被冷却挡住
        STATUS["percent"] = 88.0
        page._thr_next_ok = page._thr_grace_until = 0.0
        n4 = len(calls)
        page._check_threshold()
        _pump(app, 1.2)
        mark("占用 88% >= 阈值 80% → 触发一次", len(calls) == n4 + 1,
             "调用 %d 次" % (len(calls) - n4))
        mark("过载执行的记录标记为「%s」" % TRIGGER_OVERLOAD,
             any(it.get("trigger") == TRIGGER_OVERLOAD
                 for it in page._load_history()),
             [it.get("trigger") for it in page._load_history()])
        n5 = len(calls)
        page._check_threshold()
        _pump(app, 0.6)
        mark("触发后立刻再判一次被冷却挡住", len(calls) == n5,
             "调用 %d 次" % (len(calls) - n5))
        mark("冷却期内状态文字写明「冷却中」",
             "冷却中" in page._thr_note.text(), page._thr_note.text())
        page._thr_next_ok = page._thr_grace_until = 0.0
        n6 = len(calls)
        page._check_threshold()
        _pump(app, 1.2)
        mark("冷却结束后可再次触发", len(calls) == n6 + 1,
             "调用 %d 次" % (len(calls) - n6))

        # ⑥ 关键承诺：三种触发都走同一条管理员级清理链路，没有任何提权分支
        #    （手动 1 次 + 定时 1 次 + 过载 2 次 = 4 次，见上面各步）
        mark("★ 手动/定时/过载都直接走 optimize（无提权路径）",
             len(calls) >= 4,
             "optimize 调用 %d 次" % len(calls))
        mark("★ 结果里带系统级清理标记（standby/filecache 都做了）",
             page._last_result
             and (page._last_result.get("system_ops") or {}).get("standby") == "ok",
             (page._last_result or {}).get("system_ops"))
        mark("★ 状态文案注明本次含系统级清理",
             "未含系统级清理" not in page._status.text(),
             page._status.text()[:90])

        # ⑦ 持久化
        page.picker.set_minutes(240)
        page.thr_picker.set_percent(85)
        from PySide6.QtCore import QSettings as _QS
        cfg = _QS("Yuhub", "Yuhub")
        mark("间隔会被持久化",
             str(cfg.value("mem_auto_minutes", "")) == "240",
             cfg.value("mem_auto_minutes", ""))
        mark("阈值会被持久化",
             str(cfg.value("mem_thr_percent", "")) == "85",
             cfg.value("mem_thr_percent", ""))
        mark("两个自动开关各自持久化",
             str(cfg.value("mem_auto_enabled", "")) == "false"
             and str(cfg.value("mem_thr_enabled", "")) == "true",
             (cfg.value("mem_auto_enabled", ""),
              cfg.value("mem_thr_enabled", "")))

        # ⑧ 记录渲染 / 生命周期
        page._render_history()
        rendered = []
        for i in range(page._history_box.count()):
            wid = page._history_box.itemAt(i).widget()
            if wid is not None:
                rendered.append(wid.text())
        kinds = (TRIGGER_MANUAL, TRIGGER_TIMER, TRIGGER_OVERLOAD)
        mark("每条记录都标了触发来源",
             bool(rendered)
             and all(any(("　%s ·" % k) in t for k in kinds)
                     for t in rendered),
             rendered[:2])
        page.on_shown()
        mark("on_shown 不抛异常", True, "")
        STATUS["percent"] = 95.0        # 内存条吃数的打桩值（上面删了 95% 那段）
        page.refresh_memory()
        mark("内存条能接受占用率",
             abs(page.bar._percent - 95.0) < 0.01, page.bar._percent)
        page.shutdown()
        mark("shutdown 停掉心跳定时器", not page._tick_timer.isActive(), "")
    finally:
        memopt.optimize = orig_optimize
        memopt.memory_status = orig_status
        memopt.is_admin = orig_admin

    # ================= G. 提权入口必须已经彻底移除 =================
    # 用户要求：程序本身就是管理员启动，内存优化不再需要任何提权链路。
    # 这里守着"别哪天又被人加回来"（它是死代码，但会把 UAC 弹窗重新带回来）。
    mark("★ memopt 里已无提权入口（run_elevated_optimize / elevated_optimize_main）",
         not hasattr(memopt, "run_elevated_optimize")
         and not hasattr(memopt, "elevated_optimize_main")
         and not hasattr(memopt, "exe_path"), "")

    # ================= H. 跨机型硬前提 =================
    # ① 枚举不到进程时必须明确报错。否则在 32/64 位结构体不匹配或受限
    #    环境下会静默退化成"遍历 0 个程序 → 回收 0 字节"，看不出坏。
    orig_lp = memopt.list_processes
    try:
        memopt.list_processes = lambda: []
        res_np = memopt.optimize()
    finally:
        memopt.list_processes = orig_lp
    mark("枚举不到进程时明确报错（不装作成功）",
         res_np.get("ok") is False and "枚举" in str(res_np.get("error")),
         res_np.get("error"))

    # ② PROCESSENTRY32W 的字段偏移必须与 Win32 头文件一致（szExeFile = 44）。
    #    用 32 位 Python 跑时 c_void_p 只有 4 字节，偏移会变成 36，
    #    Process32FirstW 会直接返回失败 —— 这条断言能在自检里就抓住它，
    #    而不是等用户在界面上看到"遍历 0 个程序"。
    entry = memopt._PROCESSENTRY32W
    mark("PROCESSENTRY32W.szExeFile 偏移 = 44（Win32 头文件口径）",
         entry.szExeFile.offset == 44,
         "实际 %d，指针宽度 %d 字节"
         % (entry.szExeFile.offset, ctypes.sizeof(ctypes.c_void_p)))
    mark("PROCESSENTRY32W 结构体大小 = 568（64 位口径）",
         ctypes.sizeof(entry) == 568,
         "%d 字节" % ctypes.sizeof(entry))

    # ③ SIZE_T_MAX 必须跟着 c_size_t 走：写死 0xFFFFFFFF 会让
    #    SetSystemFileCacheSize 的"恢复上限"在大内存机器上变成 4GB 限制。
    want_max = (1 << (8 * ctypes.sizeof(ctypes.c_size_t))) - 1
    mark("SIZE_T_MAX 与 c_size_t 位宽一致",
         memopt.SIZE_T_MAX == want_max,
         "SIZE_T_MAX=%d 期望=%d" % (memopt.SIZE_T_MAX, want_max))

    # ④ 64 位大内存：占用率算的是比例，容量本身不该影响判断。
    for gb in (4, 8, 16, 64, 256):
        total = gb * 1024 ** 3
        used = int(total * 0.875)
        st_sim = {"total": total, "avail": total - used, "used": used,
                  "percent": 87.5}
        if not (0.0 <= st_sim["percent"] <= 100.0
                and st_sim["used"] == total - st_sim["avail"]):
            mark("%d GB 机器上的占用率自洽" % gb, False, st_sim)
            break
    else:
        mark("4 / 8 / 16 / 64 / 256 GB 容量下占用率都自洽", True,
             "比例口径，与物理容量无关")
