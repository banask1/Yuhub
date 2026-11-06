# -*- coding: utf-8 -*-
"""按进程映像名判活的 Win32 快路径（不拉起 tasklist）。

## 为什么值得单独一个模块

`tasklist` 每次调用要 **80–90 ms**（起进程 + 控制台子系统 + 遍历整张进程表），
而它被放在"进页面就要刷新状态"的位置上 —— 用户体感就是**点一下侧栏、界面僵
一下**。Toolhelp 快照读同一张表只要 1–3 ms，差两个数量级。

本项目里 `uninstaller.py` / `memopt.py` 各有一份同样的 `PROCESSENTRY32W` 定义
（它们的自检已经钉过布局）。这里是**第三个调用点**，所以抽成模块而不是再抄
一遍；那两份各自还需要完整的父进程关系/遍历逻辑，暂不动。

## 硬契约（改结构体前必读）

`PROCESSENTRY32W` 的布局是 Windows 定死的：
  · `sizeof` = 568（64 位）/ 32 位进程跑会错位；
  · `szExeFile` 偏移 = 44。
手改字段顺序/类型会让 `szExeFile` 静默读到垃圾 —— 判活永远返回 False，
而"没在跑"和"读错了"在界面上都是同一句"未启动"。所以下面的 `selfcheck()`
会在真实进程表上验证：**至少能读到自己这个进程**。
"""
import ctypes
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    """Win32 PROCESSENTRY32W（与 uninstaller/memopt 里的同款）。"""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),   # ULONG_PTR
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE,
                                  ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE,
                                 ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


def iter_processes():
    """(pid, 映像名) 的生成器。枚举失败就什么都不产出，绝不抛异常。"""
    try:
        k = _k32()
    except Exception:
        return
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not k.Process32FirstW(snap, ctypes.byref(entry)):
            return
        while True:
            yield int(entry.th32ProcessID), entry.szExeFile
            if not k.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        try:
            k.CloseHandle(snap)
        except Exception:
            pass


def running_by_name(exe_name):
    """指定映像名（可传一个或多个）是否正在运行。大小写不敏感。

    语义与 `tasklist /FI "IMAGENAME eq xxx"` 一致：比的是**映像名**，
    不是完整路径。枚举失败返回 False（与"没在跑"同义 —— 调用方本来就要
    能容忍读不到）。
    """
    names = [exe_name] if isinstance(exe_name, str) else list(exe_name or [])
    want = {str(n).strip().lower() for n in names if str(n or "").strip()}
    if not want:
        return False
    for _pid, exe in iter_processes():
        if (exe or "").strip().lower() in want:
            return True
    return False


def selfcheck():
    """断言结构体布局没坏：真进程表里必须能读出 flake 自己。

    只读、无副作用。返回 (ok, 说明)。布局错位时这里会红 —— 而不会等到
    "加速内核明明在跑、界面却说未启动"才发现。
    """
    import os
    import sys
    me = os.path.basename(os.path.abspath(sys.executable or ""))
    got = list(iter_processes())
    if not got:
        return False, "枚举不到任何进程（Toolhelp 不可用）"
    names = {n.lower() for _p, n in got if n}
    ok_struct = ctypes.sizeof(PROCESSENTRY32W) == 568
    ok_off = PROCESSENTRY32W.szExeFile.offset == 44
    ok_me = (me.lower() in names) or bool(running_by_name(me))
    return (ok_struct and ok_off and ok_me,
            "sizeof=%d offset=%d 进程数=%d 自身(%s)可见=%s"
            % (ctypes.sizeof(PROCESSENTRY32W), PROCESSENTRY32W.szExeFile.offset,
               len(got), me, me.lower() in names))
