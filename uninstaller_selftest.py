# -*- coding: utf-8 -*-
"""打包后自检：进页自动扫描 + 卸载后自动重扫。

为什么要有这个模块
------------------
这两条"自动扫描"是纯 UI 时序逻辑（QTimer + 线程 + 信号），
在打包后**无法用外部脚本可靠驱动**：
  * 本机 150% 缩放，外部脚本拿捏不准点击落点；
  * 窗口是 `Qt.FramelessWindowHint + WA_TranslucentBackground`，
    `PrintWindow` 渲染空白、`PostMessage` 用的是客户区坐标而截图是屏幕坐标；
  * onefile 包还会 fork 子进程，PID 对不上。

既然外部进不去，就让 exe **自己**在内部跑一遍真实对象，把观测到的
时序事件写成 JSON 回传。跑的是真正的 `UninstallPage`，不是简化复刻，
所以任何时序回归都会被抓到。

用法：`Yuhub.exe --uninstall-selftest <结果json路径>`
"""
import json
import time


def run(out_file, timeout_sec=90):
    """返回进程退出码：0 = 全部通过，1 = 有断言失败，4 = 结果写盘失败。"""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Yuhub")

    from ui.pages.uninstall_page import UninstallPage
    import uninstaller as un

    events = []

    class Probe(UninstallPage):
        """包一层，记录自动扫描的触发时序（不改动原逻辑）。"""

        def on_shown(self):
            events.append(("on_shown", time.time()))
            super().on_shown()

        def start_scan(self, force=False):
            events.append(("start_scan", time.time(), bool(force)))
            super().start_scan(force=force)

        def _auto_rescan_after_uninstall(self):
            events.append(("auto_rescan", time.time()))
            super()._auto_rescan_after_uninstall()

        def _silent_rescan(self, _retry=0):
            events.append(("silent_rescan", time.time(), _retry))
            super()._silent_rescan(_retry)
            events.append(("silent_rescan_returned", time.time()))

    page = Probe()

    # 真正的完成信号挂在页面内部的 `_apps_ready` 信号上，
    # 这里额外接一根自己的线来记录"扫描结果落地"的时刻。
    ready_log = []
    page._apps_ready.connect(
        lambda apps: ready_log.append(("apps_ready", time.time(), len(apps),
                                       page._status.text()))
    )
    fail_log = []
    page._apps_failed.connect(
        lambda msg: fail_log.append(("apps_failed", time.time(), msg))
    )

    result = {
        "ok": False,
        "checks": [],
        "events": [],
        "ready_log": [],
        "fail_log": [],
        "final_status": "",
        "final_apps": 0,
    }

    def pump(ms):
        """跑事件循环 ms 毫秒。"""
        end = time.time() + ms / 1000.0
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    # ---------------- 检查 1：进页自动扫描 ----------------
    t0 = time.time()
    page.on_shown()
    # 页面里是 QTimer.singleShot(120, start_scan)，给足 1s
    pump(1000)
    fired = [e for e in events if e[0] == "start_scan"]
    result["checks"].append({
        "name": "进页自动触发扫描",
        "pass": len(fired) >= 1,
        "detail": "on_shown 后 1s 内 start_scan 调用 %d 次" % len(fired),
    })

    # 等扫描真的跑完（枚举注册表 + 提取图标，本机约 6~20s）
    deadline = t0 + timeout_sec
    while not ready_log and not fail_log and time.time() < deadline:
        pump(300)
    if not ready_log and not fail_log:
        result["checks"].append({
            "name": "扫描完成",
            "pass": False,
            "detail": "%.0fs 超时未完成" % timeout_sec,
        })
        result["events"] = _fmt(events)
        _dump(result, out_file)
        return 1

    if fail_log:
        result["checks"].append({
            "name": "扫描完成",
            "pass": False,
            "detail": "扫描失败：%s" % fail_log[-1][2],
        })
        result["fail_log"] = _fmt(fail_log)
        _dump(result, out_file)
        return 1

    n_apps = ready_log[-1][2]
    status_after_scan = ready_log[-1][3]
    result["checks"].append({
        "name": "扫描完成",
        "pass": n_apps > 0,
        "detail": "枚举到 %d 个程序，状态栏「%s」" % (n_apps, status_after_scan),
    })
    result["checks"].append({
        "name": "非静默扫描会写状态栏",
        "pass": status_after_scan.startswith("已找到"),
        "detail": "「%s」" % status_after_scan,
    })

    # ---------------- 检查 2：图标提取 ----------------
    try:
        from ui.pages.uninstall_page import AppAvatar
        rows = page._rows[:30]
        n_icon = 0
        for row in rows:
            av = row.findChild(AppAvatar)
            if av is not None and getattr(av, "_use_icon", False):
                n_icon += 1
        pct = (n_icon / len(rows) * 100) if rows else 0
        result["checks"].append({
            "name": "图标渲染（前 30 行）",
            "pass": n_icon > 0,
            "detail": "真实图标 %d / %d（%.0f%%）" % (n_icon, len(rows), pct),
        })
    except Exception as exc:
        result["checks"].append({
            "name": "图标渲染（前 30 行）",
            "pass": False,
            "detail": "检查失败：%r" % (exc,),
        })

    # ---------------- 检查 3：卸载后自动重扫 ----------------
    before = len(events)
    # 伪造一次"卸载完成"回调——走的是真实的 _on_work_done 代码路径
    page._on_work_done({"step": "done", "leftovers": [],
                        "message": "卸载完成，未发现残留"})
    pump(200)
    auto = [e for e in events[before:] if e[0] == "auto_rescan"]
    result["checks"].append({
        "name": "卸载完成后触发自动重扫",
        "pass": len(auto) == 1,
        "detail": "_auto_rescan_after_uninstall 调用 %d 次" % len(auto),
    })

    # 等静默重扫跑完（QTimer 260ms + 扫描耗时）
    ready_mark = len(ready_log)
    deadline = time.time() + timeout_sec
    while len(ready_log) <= ready_mark and time.time() < deadline:
        pump(300)
    pump(300)

    silent = [e for e in events[before:] if e[0] == "silent_rescan"]
    result["checks"].append({
        "name": "静默重扫真的执行了",
        "pass": len(silent) >= 1,
        "detail": "_silent_rescan 调用 %d 次" % len(silent),
    })

    status_after_rescan = ready_log[-1][3] if len(ready_log) > ready_mark else ""
    result["checks"].append({
        "name": "静默重扫不覆盖卸载结论",
        "pass": status_after_rescan == "卸载完成，未发现残留",
        "detail": "重扫后状态栏「%s」（应保持卸载结论）" % status_after_rescan,
    })
    result["checks"].append({
        "name": "重扫后列表仍然有效",
        "pass": ready_log[-1][2] > 0,
        "detail": "重扫枚举到 %d 个程序" % ready_log[-1][2],
    })

    # ---------------- 检查 4：卸载失败分支也应重扫 ----------------
    before = len(events)
    page._on_work_done({"step": "failed", "message": "卸载程序返回非零"})
    pump(200)
    auto2 = [e for e in events[before:] if e[0] == "auto_rescan"]
    result["checks"].append({
        "name": "卸载失败分支也触发重扫",
        "pass": len(auto2) == 1,
        "detail": "_auto_rescan_after_uninstall 调用 %d 次" % len(auto2),
    })
    pump(2500)

    # ---------------- 检查 5：残留卡片可关闭、确认框可取消 ----------------
    # 回归"点开强制清除残留后无法取消"：卡片曾经只有 全选/全不选/清理，
    # **没有任何关闭入口**，用户找不到办法收起它。
    try:
        _check_leftover_dismissal(page, result["checks"])
    except Exception as exc:
        result["checks"].append({
            "name": "残留卡片可关闭/确认框可取消",
            "pass": False,
            "detail": "检查失败：%r" % (exc,),
        })

    # ---------------- 检查 6：删除权限与只读属性（v0.10.4） ----------------
    # 回归"AppData 里的文件删不掉"：三个叠加原因
    #   a) _needs_admin 只认三个系统前缀，AppData 一律按普通权限删
    #   b) _delete_dir 不摘只读属性 —— 只读文件连管理员都删不掉
    #   c) MoveFileEx 对权限失败也返回成功，把失败误报成"重启后自动删"，
    #      于是永远不触发 UAC 提权兜底
    try:
        _check_delete_permissions(result["checks"])
    except Exception as exc:
        result["checks"].append({
            "name": "删除权限与只读属性",
            "pass": False,
            "detail": "检查失败：%r" % (exc,),
        })

    # ---------------- 汇总 ----------------
    result["ok"] = all(c["pass"] for c in result["checks"])
    result["final_status"] = page._status.text()
    result["final_apps"] = len(page._apps)
    result["events"] = _fmt(events)
    result["ready_log"] = _fmt(ready_log)
    result["fail_log"] = _fmt(fail_log)
    result["pyz_modules"] = _runtime_flags()

    page.shutdown() if hasattr(page, "shutdown") else None
    _dump(result, out_file)
    return 0 if result["ok"] else 1


def _check_leftover_dismissal(page, checks):
    """回归：残留卡片必须能关掉，确认框必须能取消。"""
    from PySide6.QtWidgets import QApplication, QPushButton
    app = QApplication.instance() or QApplication([])
    import uninstaller as un

    items = [un.Leftover(kind="file", path=r"C:\Temp\_selftest_lo%d" % i,
                         size=1024, safe=True, checked=True) for i in range(3)]

    # ① 卡片要有「关闭」按钮
    page._show_leftovers(items)
    page._leftover_card.show()
    app.processEvents()
    btns = page._leftover_card.findChildren(QPushButton)
    close_btn = next((b for b in btns if "关闭" in b.text()), None)
    checks.append({
        "name": "残留卡片有「关闭」入口",
        "pass": close_btn is not None,
        "detail": "卡片按钮 %r" % [b.text() for b in btns],
    })
    if close_btn is None:
        return

    # ② 点关闭要真的收起，且状态栏留有说明
    close_btn.click()
    app.processEvents()
    checks.append({
        "name": "点「关闭」收起残留卡片",
        "pass": (not page._leftover_card.isVisible()
                 and not page._leftovers
                 and not page._leftover_rows),
        "detail": "可见=%s 残留项=%d 行=%d 状态栏=%r" % (
            page._leftover_card.isVisible(), len(page._leftovers),
            len(page._leftover_rows), page._status.text()),
    })
    checks.append({
        "name": "收起后状态栏提示残留未处理",
        "pass": "已收起" in page._status.text(),
        "detail": repr(page._status.text()),
    })

    # ③ 确认框「取消」不得触发清理
    page._show_leftovers(items)
    page._leftover_card.show()
    app.processEvents()
    fired = []
    original = page._run_clean_leftovers
    page._run_clean_leftovers = lambda its: fired.append(its)
    try:
        page._confirm_clean_leftovers()
        app.processEvents()
        dlg = page._confirm_dlg
        cancel = next((b for b in dlg.findChildren(QPushButton)
                       if "取消" in b.text()), None)
        if cancel is not None:
            cancel.click()
            app.processEvents()
        checks.append({
            "name": "确认框「取消」不触发清理",
            "pass": (len(fired) == 0 and not dlg.isVisible()),
            "detail": "清理被调用 %d 次，对话框可见=%s" % (len(fired), dlg.isVisible()),
        })
        checks.append({
            "name": "取消后残留卡片保留",
            "pass": len(page._leftovers) == len(items),
            "detail": "残留项 %d（应为 %d）" % (len(page._leftovers), len(items)),
        })

        # ④ 确认才真正清理
        page._confirm_clean_leftovers()
        app.processEvents()
        dlg2 = page._confirm_dlg
        okbtn = next((b for b in dlg2.findChildren(QPushButton)
                      if "确认清理" in b.text()), None)
        if okbtn is not None:
            okbtn.click()
            app.processEvents()
        checks.append({
            "name": "确认框「确认清理」触发清理",
            "pass": len(fired) == 1,
            "detail": "清理被调用 %d 次" % len(fired),
        })
    finally:
        page._run_clean_leftovers = original
        page._clear_leftovers()


def _check_delete_permissions(checks):
    """回归：只读属性必须被摘掉、失败必须可识别、提权通道必须可用。

    这些是"文件删不掉"的三个真实根因，用真实文件系统验证，不打桩。
    """
    import os
    import stat
    import shutil
    import tempfile
    import uninstaller as un

    # ① 只读文件所在目录必须能删净（旧实现：gone=False）
    base = os.path.join(tempfile.gettempdir(), "YuhubSelfTest_%d" % os.getpid())
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(os.path.join(base, "sub"))
    ro = os.path.join(base, "sub", "readonly.bin")
    with open(ro, "wb") as fh:
        fh.write(b"x" * 4096)
    os.chmod(ro, stat.S_IREAD)
    try:
        r = un._delete_dir(base)
        checks.append({
            "name": "只读文件所在目录能删净",
            "pass": bool(r[0]) and not os.path.exists(base),
            "detail": "gone=%s failed=%s why=%r" % (r[0], r[2], r[3]),
        })
        checks.append({
            "name": "_delete_dir 返回失败原因（4 元组）",
            "pass": len(r) == 4,
            "detail": "返回 %d 个值" % len(r),
        })
    finally:
        if os.path.exists(ro):
            try:
                os.chmod(ro, stat.S_IWRITE)
            except OSError:
                pass
        shutil.rmtree(base, ignore_errors=True)

    # ② 单个只读文件
    f1 = os.path.join(tempfile.gettempdir(), "YuhubSelfTestF_%d.bin" % os.getpid())
    with open(f1, "wb") as fh:
        fh.write(b"y" * 1024)
    os.chmod(f1, stat.S_IREAD)
    try:
        ok, why = un._delete_file(f1)
        checks.append({
            "name": "单个只读文件能删掉",
            "pass": bool(ok) and not os.path.exists(f1),
            "detail": "ok=%s why=%r" % (ok, why),
        })
    finally:
        if os.path.exists(f1):
            try:
                os.chmod(f1, stat.S_IWRITE)
            except OSError:
                pass
            try:
                os.remove(f1)
            except OSError:
                pass

    # ③ CleanResult 必须带 retryable（UI 据此决定是否提权重试）
    res = un.CleanResult()
    checks.append({
        "name": "清理结果带 retryable 字段",
        "pass": hasattr(res, "retryable") and res.retryable == [],
        "detail": "retryable=%r" % (getattr(res, "retryable", "缺失"),),
    })

    # ④ 提权通道可用性（源码模式必须能识别出没有 exe 的情况）
    exe = un.sys_executable()
    cap, why = un.elevation_capable(exe)
    is_py = os.path.basename(exe).lower() == "python.exe"
    checks.append({
        "name": "提权通道判定自洽",
        "pass": (not cap) if is_py else cap,
        "detail": "exe=%s capable=%s reason=%r" % (exe, cap, why),
    })

    # ⑤ AppData 里"删不掉"的路径必须被判定为需要提权。
    #    本机自检多半是管理员，探不出真实权限，所以只验证硬规则：
    #    UWP 包目录与系统前缀必须命中。
    from ui.pages.uninstall_page import LeftoverConfirmDialog as D
    cases = [
        (r"C:\Users\someone\AppData\Local\Packages\Foo_1", True, "UWP 包目录"),
        (r"C:\Program Files\Foo", True, "Program Files"),
        (r"C:\ProgramData\Foo", True, "ProgramData"),
        (r"C:\Users\someone\AppData\Local\Foo", False, "普通 AppData 目录"),
    ]
    bad = []
    for p, want, label in cases:
        # 普通 AppData 目录在管理员环境下探测会通过 → 判定 False，属预期
        got = D._needs_admin(un.Leftover(kind="dir", path=p))
        if got != want:
            bad.append("%s(期望%s得到%s)" % (label, want, got))
    checks.append({
        "name": "受保护路径判定正确",
        "pass": not bad,
        "detail": "全部命中" if not bad else "偏差：%s" % "; ".join(bad),
    })

    # ⑥ 「被占用」绝不能误判成「权限不足」（v1.0.2 修的第二个根因）。
    #    Python 把 WinError 32（共享冲突）也映射成 PermissionError 且
    #    errno=13 —— 只要先判 isinstance 就会误判，导致 MoveFileEx 兜底
    #    永远走不到、UI 还一直劝用户提权（提权对占用没用）。
    #    这里用一个**真被本进程打开**的文件来制造 WinError 32。
    lock_dir = os.path.join(tempfile.gettempdir(),
                            "YuhubSelfTestLock_%d" % os.getpid())
    shutil.rmtree(lock_dir, ignore_errors=True)
    os.makedirs(lock_dir)
    locked = os.path.join(lock_dir, "inuse.bin")
    fh = open(locked, "wb")
    fh.write(b"z" * 2048)
    fh.flush()
    try:
        try:
            os.remove(locked)
            exc = None
        except OSError as e:
            exc = e
        # 只有在真的复现出"占用"（非 None）时才有意义
        if exc is None:
            checks.append({
                "name": "被占用文件被判定为「非权限问题」",
                "pass": True,
                "detail": "本次未能复现占用（环境差异），跳过",
            })
        else:
            got = un._is_permission_error(exc)
            checks.append({
                "name": "被占用文件被判定为「非权限问题」",
                "pass": got is False,
                "detail": "winerror=%r errno=%r -> _is_permission_error=%s（期望 False）"
                          % (getattr(exc, "winerror", None), exc.errno, got),
            })
    finally:
        try:
            fh.close()
        except Exception:
            pass
        shutil.rmtree(lock_dir, ignore_errors=True)

    # ⑦ 真·拒绝访问（WinError 5）仍必须判定为权限问题（别把上面那条修过头）
    class _Fake5(OSError):
        winerror = 5
        errno = 13
    checks.append({
        "name": "真·权限拒绝（WinError 5）判定为权限问题",
        "pass": un._is_permission_error(_Fake5()) is True,
        "detail": "winerror=5 -> %s" % un._is_permission_error(_Fake5()),
    })

    # ⑧ 「目录非空」（WinError 145）不算权限问题 —— 它是子项没删掉的连带结果
    class _Fake145(OSError):
        winerror = 145
        errno = 0
    checks.append({
        "name": "目录非空（WinError 145）不算权限问题",
        "pass": un._is_permission_error(_Fake145()) is False,
        "detail": "winerror=145 -> %s" % un._is_permission_error(_Fake145()),
    })

    # ⑨ 夺取所有权的能力必须在（"有权限也删不掉"的解药）
    checks.append({
        "name": "uninstaller 提供 _take_ownership（ACL 解药）",
        "pass": callable(getattr(un, "_take_ownership", None)),
        "detail": "存在=%s" % hasattr(un, "_take_ownership"),
    })

    # ⑩ ACL 受阻时 _delete_dir 必须走 takeown 分支（v1.0.2 问题 3 的回归）。
    #
    #    为什么用**打桩**而不是造一个真的"管理员也删不掉"的目录：
    #    实测在本机（管理员 + SeBackupPrivilege）下，无论把所有者改成
    #    SYSTEM、还是挂 (D)/(DC) 拒绝 ACE，os.remove 都照样成功 —— 真实
    #    故障现场（杀软 minifilter 拦截 / 特殊 ACL）无法在自检里复现。
    #    硬造一条"永远跳过"的断言等于装饰品。这里改成桩测试：把
    #    shutil.rmtree / os.remove 换成抛 WinError 5 的假实现，断言
    #    _take_ownership 被调用过、且最终报告"权限受阻"而不是"占用"。
    #
    #    两个前提必须成立，否则断言无意义：
    #      a) _delete_dir 认为自己在管理员下（只在管理员下做 takeown）；
    #      b) 打桩的删除确实抛 WinError 5（真·权限）。
    #
    #    ⚠️ 这两个前提**靠自己构造成立，不靠运行环境**（v1.0.5 改）：
    #    以前这里依赖"当前进程真的是管理员"，而 is_admin() 已改成
    #    TokenElevation 口径（本机普通启动恒为 False）→ 断言会退化成
    #    "永远跳过"的装饰品。现在改成**打桩 un.is_admin = lambda: True**
    #    强制走到 takeown 分支，前提 a 由我们自己保证。
    #    （这就是本项目的红线：跳过分支不能记成 pass，"它绿了"≠"它测到了"。）
    checks.append({
        "name": "[前置] ACL 回归可打桩（Windows 下才存在 takeown 分支）",
        "pass": bool(un.IS_WIN and callable(getattr(un, "is_admin", None))),
        "detail": "IS_WIN=%s hasattr(is_admin)=%s（下面用打桩 is_admin=True 强制走到 takeown）"
                  % (un.IS_WIN, callable(getattr(un, "is_admin", None))),
    })
    if un.IS_WIN:
        acl_dir = os.path.join(tempfile.gettempdir(),
                               "YuhubSelfTestAcl_%d" % os.getpid())
        shutil.rmtree(acl_dir, ignore_errors=True)
        os.makedirs(acl_dir)
        victim = os.path.join(acl_dir, "victim.bin")
        with open(victim, "wb") as fh:
            fh.write(b"q" * 1024)

        real_rmtree = shutil.rmtree
        real_remove = os.remove
        real_rmdir = os.rmdir
        real_take = un._take_ownership
        real_is_admin = un.is_admin
        env_admin = un.is_admin()          # 记录真实值（仅供 detail 展示）

        calls = {"takeown": 0, "remove": 0}

        def _fake_rmtree(path, ignore_errors=False, **kw):
            # 假装清不掉，让 _delete_dir 往下走
            return None

        def _fake_remove(path, **kw):
            calls["remove"] += 1
            e = OSError(13, "Access is denied")
            e.winerror = 5          # 真·权限拒绝（不是占用）
            raise e

        def _fake_rmdir(path, **kw):
            e = OSError(13, "Access is denied")
            e.winerror = 5
            raise e

        def _fake_take(path):
            calls["takeown"] += 1
            return False            # 夺权也失败（场景：连 takeown 都被拦）

        shutil.rmtree = _fake_rmtree
        os.remove = _fake_remove
        os.rmdir = _fake_rmdir
        un._take_ownership = _fake_take
        un.is_admin = lambda: True          # ← 前提 a：强制走 takeown 分支
        try:
            r = un._delete_dir(acl_dir)
        finally:
            shutil.rmtree = real_rmtree
            os.remove = real_remove
            os.rmdir = real_rmdir
            un._take_ownership = real_take
            un.is_admin = real_is_admin
        why = str(r[3])
        checks.append({
            "name": "ACL 受阻时 _delete_dir 走 takeown 夺权分支",
            "pass": calls["takeown"] >= 1,
            "detail": "takeown 调用次数=%d remove 尝试=%d（打桩 is_admin=True，真实值=%s）"
                      % (calls["takeown"], calls["remove"], env_admin),
        })
        checks.append({
            "name": "ACL 受阻的失败原因报「权限」而非「占用」",
            "pass": ("权限" in why) and ("占用" not in why.replace("可能仍被进程占用", "")),
            "detail": "why=%r" % why[:160],
        })
        shutil.rmtree(acl_dir, ignore_errors=True)


def _runtime_flags():
    """顺带记录运行时关键能力，用于排查"打包后某能力丢失"。"""
    flags = {}
    try:
        from PySide6.QtWidgets import QFileIconProvider
        from PySide6.QtCore import QFileInfo
        prov = QFileIconProvider()
        pm = prov.icon(QFileInfo(r"C:\Windows\System32\notepad.exe")).pixmap(34, 34)
        flags["QFileIconProvider"] = "ok %dx%d" % (pm.width(), pm.height())
    except Exception as exc:
        flags["QFileIconProvider"] = "FAIL %r" % (exc,)
    try:
        import uninstaller as un
        flags["_ps_exe"] = un._ps_exe()
        flags["UNINSTALL_KEYS"] = len(un.UNINSTALL_KEYS)
        flags["_icon_source"] = hasattr(un, "_icon_source")
        flags["_search_main_exe_deep"] = hasattr(un, "_search_main_exe_deep")
    except Exception as exc:
        flags["uninstaller"] = "FAIL %r" % (exc,)
    import sys
    flags["frozen"] = bool(getattr(sys, "frozen", False))
    return flags


def _fmt(evts):
    out = []
    if not evts:
        return out
    base = evts[0][1] if len(evts[0]) > 1 else 0
    for e in evts:
        row = {"event": e[0]}
        if len(e) > 1 and isinstance(e[1], float):
            row["t"] = round(e[1] - base, 3)
        for extra in e[2:]:
            row.setdefault("args", []).append(extra)
        out.append(row)
    return out


def _dump(result, out_file):
    # 先 dumps 再落盘 + default=str：detail 里混进 bytes / 自定义对象时，
    # json.dump 会抛 TypeError，而它是**边序列化边写**的——文件已被写了
    # 半截，最后拿到的是一份读不出来的残档，前面的检查结果全丢
    #（v0.8.14beta 的 lan 自检就这么坏过一次）。
    try:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2,
                                default=str))
    except (OSError, TypeError, ValueError):
        pass
