# -*- coding: utf-8 -*-
"""打包后自检：按进程名判活（winproc / Toolhelp32）没静默失效。

用法：`Yuhub.exe --winproc-selftest <结果json路径>`

## 为什么必须有这套自检

`winproc.running_by_name()` 是加速页"脏退自愈"的判活依据：它一旦恒为
False，Yuhub 会以为内核没在跑，从而在**加速正常使用时**去清 hosts ——
比不修还糟。而 `PROCESSENTRY32W` 布局写错的典型表现恰恰是"读出来的映像名
全是垃圾"，于是判活永远 False、一句报错都没有。这是本项目反复吃亏的
"静默失效"形态。

所以判据不能只查常量：**必须真的枚举一次系统进程表**，并与一个独立的
权威来源（tasklist）交叉比对。
"""
import json
import os
import sys
import time


def run(out_file):
    """外层兜底：自检**自己**崩了也要留下一份可读的红灯 JSON。

    冻结态是 GUI 子系统、没有 stderr，自检抛异常就只剩一个光秃秃的退出码
    —— 那正是本项目反复吃亏的"静默失效"。宁可把异常写进结果文件。
    """
    try:
        return _run(out_file)
    except Exception as exc:
        import traceback
        _dump({"ok": False,
               "checks": [{"name": "★ 自检自身未崩溃", "pass": False,
                           "detail": "%r\n%s" % (exc, traceback.format_exc())}],
               "runtime": {}}, out_file)
        return 5


def _run(out_file):
    result = {"ok": False, "checks": [], "runtime": {}}
    t0 = time.time()

    def mark(name, ok, detail=""):
        result["checks"].append({"name": name, "pass": bool(ok),
                                 "detail": str(detail)})

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import winproc
    except Exception as exc:
        mark("导入 winproc", False, repr(exc))
        _dump(result, out_file)
        return 1

    # ① 结构体布局：64 位下必须是 568 / 44，改字段顺序就会静默错位
    size = winproc.ctypes.sizeof(winproc.PROCESSENTRY32W)
    off = winproc.PROCESSENTRY32W.szExeFile.offset
    mark("★ PROCESSENTRY32W 布局（sizeof=568 / szExeFile 偏移=44）",
         size == 568 and off == 44, "sizeof=%d offset=%d" % (size, off))

    # ② 真枚举：拿得到进程表，且**自己**必须在里面
    try:
        procs = list(winproc.iter_processes())
    except Exception as exc:
        procs = []
        mark("枚举进程表", False, repr(exc))
    else:
        me = os.path.basename(sys.executable or "")
        names = {str(n or "").strip().lower() for _p, n in procs}
        mark("★ 真枚举到系统进程表", len(procs) > 10, "共 %d 个进程" % len(procs))
        mark("★ 自己的进程映像名出现在真进程表里", me.lower() in names,
             "sys.executable=%s 命中=%s" % (me, me.lower() in names))
        result["runtime"]["process_count"] = len(procs)
        result["runtime"]["self_exe"] = me

    # ③ 判活语义：自己 = True、不存在的名字 = False、大小写不敏感
    me = os.path.basename(sys.executable or "")
    mark("★ running_by_name(自己) 为 True",
         winproc.running_by_name(me), me)
    mark("★ running_by_name(不存在的名字) 为 False",
         not winproc.running_by_name("YuhubNoSuchProcess_9F3A.exe"),
         "YuhubNoSuchProcess_9F3A.exe")
    mark("大小写不敏感", winproc.running_by_name(me.upper()),
         me.upper())
    mark("空参数不入栈、不抛异常", not winproc.running_by_name(""),
         "空字符串")

    # ④ 与独立权威来源交叉比对：同一时刻 tasklist 与 Toolhelp 的结论必须一致。
    #    这是整套检查里最有价值的一条 —— 只信自己的实现就是"自洽比较"。
    try:
        import subprocess
        probe = "explorer.exe"
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq " + probe, "/NH"],
            capture_output=True, timeout=10,
            creationflags=0x08000000)   # CREATE_NO_WINDOW，别闪黑窗
        txt = (r.stdout or b"").decode("utf-8", "replace").lower()
        by_tasklist = probe in txt
        by_toolhelp = winproc.running_by_name(probe)
        mark("★ 与 tasklist 结论一致（独立交叉验证）",
             by_tasklist == by_toolhelp,
             "tasklist=%s toolhelp=%s（探测 %s）"
             % (by_tasklist, by_toolhelp, probe))
    except Exception as exc:
        # 拿不到 tasklist 就如实记红，绝不记成 pass —— 那样等于把洞埋起来
        mark("★ 与 tasklist 结论一致（独立交叉验证）", False,
             "tasklist 不可用：%r" % (exc,))

    # ⑤ 模块自带的 selfcheck（它也做一次真枚举）
    try:
        ok, detail = winproc.selfcheck()
        mark("★ winproc.selfcheck()", ok, detail)
    except Exception as exc:
        mark("★ winproc.selfcheck()", False, repr(exc))

    result["runtime"]["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    result["ok"] = all(c["pass"] for c in result["checks"])
    _dump(result, out_file)
    return 0 if result["ok"] else 1


def _dump(result, out_file):
    try:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as exc:
        text = json.dumps({"ok": False, "checks": result.get("checks", []),
                           "dump_error": repr(exc)},
                          ensure_ascii=False, indent=2, default=str)
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return 4
    return 0
