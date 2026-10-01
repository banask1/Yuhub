"""Yuhub 应用入口。

启动顺序有讲究，别随意调换：
  1. 建 QApplication（QLocalSocket / QSystemTrayIcon 都依赖它）
  2. 单实例判定 —— **必须在建窗口之前**。第二个实例只负责把已有窗口叫起来，
     然后立刻退出；要是先建了窗口再判，用户会看到新窗口一闪而过。
  3. 关掉「最后一个窗口关闭就退出」—— 托盘常驻模式下窗口只是被 hide()，
     但某些路径（比如系统级关闭）仍会触发 lastWindowClosed，
     留着这个默认行为会让程序在用户没要求的时候悄悄结束。
  4. 建窗口 → 按需显示或静默进托盘
"""

import base64
import ctypes
import json
import os
import subprocess
import sys
import time

from PySide6.QtCore import Qt, QLibraryInfo, QSettings, QTranslator
from PySide6.QtGui import QIcon, QFont
from PySide6.QtWidgets import QApplication

from autostart import MINIMIZED_FLAG
from single_instance import SingleInstance
from ui import VERSION
from ui import wheel_guard
from ui.main_window import MainWindow, resource_path, init_theme_from_settings
from ui.splash import SplashScreen

# 装完的翻译对象必须留一个引用，否则会被 GC 掉、翻译当场失效
_TRANSLATORS = []

# 给 easytier-core.exe 启动用的标志位。0x08000000 = CREATE_NO_WINDOW，
# 关键：阻止 Windows 给控制台子系统程序创建 conhost 子进程窗口，
# 从根上消除「C:\Windows\System32\cmd.exe / conhost.exe 闪烁」。
_CREATE_NO_WINDOW = 0x08000000


def _pid_alive(pid, expect_name=""):
    """检查 PID 对应的进程是否还活着（可选：校验进程名，防 PID 复用）。

    用 OpenProcess(SYNCHRONIZE) + WaitForSingleObject(0) 判断：
      - 进程存在且未退出 → WAIT_TIMEOUT(258) → 活
      - 进程已退出       → WAIT_OBJECT_0(0) → 死
      - 进程不存在       → OpenProcess 返回 0 → 死
    expect_name 非空时，再用 QueryFullProcessImageNameW 取 exe 名比对——
    防"父进程退出后 PID 被别的进程复用"导致的误判。
    """
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x102
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32

    # 显式声明原型，避免 64 位句柄被截断成 32 位 int
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong),
    ]

    access = SYNCHRONIZE | (PROCESS_QUERY_LIMITED_INFORMATION if expect_name else 0)
    handle = kernel32.OpenProcess(access, 0, int(pid))
    if not handle:
        return False
    try:
        if kernel32.WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
            return False
        if expect_name:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                if os.path.basename(buf.value).lower() != expect_name.lower():
                    return False    # PID 被复用了，不是原来的进程
        return True
    finally:
        kernel32.CloseHandle(handle)


def _run_watchdog(argv):
    """`Yuhub.exe --watchdog <base64-json>` 模式。

    角色：在管理员权限下（runas 启动）启动 easytier-core.exe，负责它的生与死。

    为什么不另写一个 watchdog helper exe：Yuhub.exe 本身是 PyInstaller
    --windowed 打包的（GUI subsystem，无控制台），runas 启动它时
    Windows **不会** 自动给它分配 conhost 窗口；我们在这里又用
    `CREATE_NO_WINDOW` 启动子进程（easytier-core.exe），**因此全程
    不出现任何控制台窗口**——比 wscript + VBScript 干净（VBScript
    通过 WScript.Shell.Run 启动控制台程序时仍会临时创建 conhost）。

    **三种退出条件**（任一满足就停掉 easytier-core 并退出）：
      1. 停止信号文件出现（Yuhub 里点「停止」）
      2. easytier-core 自己退出（节点全挂 / 启动失败）
      3. **父进程（主 Yuhub）消失** —— 这是"退出 Yuhub 自动关房间"的关键：
         用户关窗口 / 托盘退出 / 甚至任务管理器强杀，watchdog 都能感知，
         自动收尾。不依赖主进程"来得及"写出停止信号。

    早返回：本模式不进 Qt / 不进单实例判定，直接 sys.exit，避免
    与主 GUI 实例抢单例锁。
    """
    if not argv:
        return 1
    try:
        payload = json.loads(base64.b64decode(argv[0].encode("ascii")).decode("utf-8"))
    except Exception:
        return 2

    core = payload.get("core") or ""
    args = payload.get("args") or []
    stop = payload.get("stop") or ""
    workdir = payload.get("workdir") or ""
    parent_pid = int(payload.get("parent_pid") or 0)
    parent_name = payload.get("parent_name") or ""

    if not core or not stop or not os.path.isfile(core):
        return 3

    # Pre-flight：本进程已 runas 提权，正是写防火墙例外 + 注册表的时机。
    # （调用 etier.py 的 _preflight_silence_windows——那两个 Windows 自带的
    # 白弹窗没法 hide，只能让用户根本不用回答。）
    et = None
    try:
        import etier as et
        et._preflight_silence_windows(core)
    except Exception:
        pass

    try:
        # 易位主进程自己去当 watchdog 前，先把本进程的工作目录设好，
        # 这样 easytier-core 找 wintun.dll / Packet.dll 时优先看
        # 当前目录而不是 PATH。
        proc = subprocess.Popen(
            [core] + list(args),
            cwd=workdir or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        return 4

    # 虚拟网卡"信任化"（v0.8.4beta）：等网卡出现后设为专用网络 + 放行
    # 游戏入站——wintun 被归为公用网络，MC 等游戏的 IP 直连会被防火墙
    # 丢弃（Radmin 能玩就是因为它归为专用）。必须在这里做：本进程已
    # 提权，主进程没有权限。网卡每次进房都重建，所以每次都要重跑。
    if et is not None:
        try:
            import threading
            threading.Thread(
                target=et.enforce_virtual_net_trust,
                name="TrustNic", daemon=True,
            ).start()
        except Exception:
            pass

    # watchdog 主循环：轮询三个退出条件
    # parent_name 由主进程传入（通常就是 Yuhub.exe），用于防 PID 复用误判。
    while True:
        if os.path.exists(stop):
            _kill_child(proc)
            _remove_quiet(stop)
            return 0
        if proc.poll() is not None:
            # easytier-core 自己挂了（密码错 / 节点全挂 / UAC 后失败等）
            return 0
        if parent_pid and not _pid_alive(parent_pid, expect_name=parent_name):
            # 主 Yuhub 进程没了（正常退出 / 被强杀 / 崩溃）→ 自动收尾
            _kill_child(proc)
            _remove_quiet(stop)
            return 0
        time.sleep(0.5)


def _kill_child(proc):
    """taskkill 掉 watchdog 拉起的 easytier-core（连带子进程）。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=10,
        )
    except Exception:
        pass


def _remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _run_kill_core():
    """`Yuhub.exe --kill-core` 模式：提权强制结束所有 easytier-core.exe。

    用途：处理"看门狗已经不在了、但 easytier-core 还在跑"的残留场景
    （例如旧版本 Yuhub 退出时没清理干净）。普通权限的 taskkill 杀不掉
    提权启动的 easytier-core，所以要通过 runas 走这个模式。
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "easytier-core.exe"],
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=15,
        )
    except Exception:
        pass
    # 顺手把停止信号文件清掉，避免下次启动误判
    try:
        stop = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub", "etier_stop")
        os.remove(stop)
    except OSError:
        pass
    return 0


def install_translations(app):
    """加载 Qt 自带的中文翻译。

    不装的话，QLineEdit 的标准右键菜单是 Qt 内置英文串（Undo / Redo / Cut /
    Copy / Paste / Delete / Select All），在中文界面里很突兀。
    优先用随包携带的 resources/qtbase_zh_CN.qm（源码运行与打包后行为一致），
    找不到再退到 Qt 自己的 translations 目录。
    """
    candidates = [
        resource_path(os.path.join("resources", "qtbase_zh_CN.qm")),
        os.path.join(QLibraryInfo.path(QLibraryInfo.TranslationsPath), "qtbase_zh_CN.qm"),
    ]
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        tr = QTranslator(app)
        if tr.load(path):
            app.installTranslator(tr)
            _TRANSLATORS.append(tr)     # 防 GC
            return path
    return ""


def main():
    # ---- watchdog 模式（在所有 Qt / 单例初始化之前早返回） ----
    # etier.py 会用 ShellExecuteW("runas") 启动"Yuhub.exe --watchdog <base64-json>"，
    # 让本程序自己充当 watchdog helper：GUI 子系统 + CREATE_NO_WINDOW 启 easytier-core，
    # 全程无控制台窗口闪烁。注意必须在 import Qt / 单例之前判定，避免抢单例锁。
    if "--watchdog" in sys.argv:
        idx = sys.argv.index("--watchdog")
        sys.exit(_run_watchdog(sys.argv[idx + 1:]))

    # ---- kill-core 模式：提权强制结束残留的 easytier-core（同样早返回） ----
    if "--kill-core" in sys.argv:
        sys.exit(_run_kill_core())

    # ---- clean-elevated 模式：提权静默清理需要管理员权限的项（同样早返回） ----
    # cleaner.py 的 run_elevated_clean() 会用 ShellExecuteW("runas") 启动
    # `Yuhub.exe --clean-elevated <base64-json>`，在管理员权限下清完把结果
    # 写成 JSON 回传。全程无窗口（GUI 子系统 exe + 不建任何 UI）。
    if "--clean-elevated" in sys.argv:
        idx = sys.argv.index("--clean-elevated")
        rest = sys.argv[idx + 1:]
        if not rest:
            sys.exit(2)
        try:
            import cleaner
            sys.exit(cleaner.elevated_clean_main(rest[0]))
        except Exception:
            sys.exit(5)

    # ---- uninstall-elevated 模式：提权删除受保护位置的软件残留（同样早返回） ----
    # uninstaller.py 的 run_elevated_clean() 会启动
    # `Yuhub.exe --uninstall-elevated <base64-json>`，在管理员权限下删掉
    # Program Files / ProgramData / HKLM 里的残留项，结果写 JSON 回传。
    if "--uninstall-elevated" in sys.argv:
        idx = sys.argv.index("--uninstall-elevated")
        rest = sys.argv[idx + 1:]
        if not rest:
            sys.exit(2)
        try:
            import uninstaller
            sys.exit(uninstaller.elevated_uninstall_main(rest[0]))
        except Exception:
            sys.exit(5)

    # ---- uninstall-selftest 模式：无窗口自检"进页自动扫描 + 卸载后自动重扫" ----
    # 用法： Yuhub.exe --uninstall-selftest <结果json路径>
    # 目的：打包后**无法用外部脚本可靠驱动 Qt 界面**（150% 缩放 + 无边框窗口 +
    # PostMessage 客户端坐标），但这两条自动扫描恰恰是易回归的逻辑。
    # 所以在 exe 内部直接把流程跑一遍，把观测结果写成 JSON 回传。
    if "--uninstall-selftest" in sys.argv:
        idx = sys.argv.index("--uninstall-selftest")
        rest = sys.argv[idx + 1:]
        if not rest:
            sys.exit(2)
        try:
            import uninstaller_selftest
            sys.exit(uninstaller_selftest.run(rest[0]))
        except Exception:
            sys.exit(5)

    # ---- theme-selftest 模式：无窗口自检主题包系统 ----
    # 用法： Yuhub.exe --theme-selftest <结果json路径>
    # 主题包涉及"文档目录创建 / 扫描 / 色板合并 / QSS 生成 / 切换持久化"，
    # 打包后最容易出问题的是**文档目录路径解析**（打包环境变量不同），
    # 所以在 exe 内部跑一遍真实流程最可靠。
    if "--theme-selftest" in sys.argv:
        idx = sys.argv.index("--theme-selftest")
        rest = sys.argv[idx + 1:]
        if not rest:
            sys.exit(2)
        try:
            import theme_selftest
            sys.exit(theme_selftest.run(rest[0]))
        except Exception:
            sys.exit(5)

    # ---- lan-selftest 模式：无窗口自检"进入跨网房间"全流程 ----
    # 用法： Yuhub.exe --lan-selftest <结果json路径> [--room-code xxx] [--keep]
    # 跨网房间要弹 UAC + 拉起独立 watchdog 进程 + 等虚拟网卡，
    # 这些外部脚本都驱动不了，只能让 exe 自己跑真实流程。
    if "--lan-selftest" in sys.argv:
        idx = sys.argv.index("--lan-selftest")
        rest = sys.argv[idx + 1:]
        if not rest:
            sys.exit(2)
        out_path = rest[0]
        code = "yuhub-selftest"
        if "--room-code" in sys.argv:
            code = sys.argv[sys.argv.index("--room-code") + 1]
        try:
            import lan_selftest
            sys.exit(lan_selftest.run(out_path, code=code,
                                      keep=("--keep" in sys.argv)))
        except Exception:
            sys.exit(5)

    # ---- apply-update 模式：替换自己的 exe 并重启（同样早返回） ----
    # 主进程把"当前的自己"拷一份到 %TEMP%\Yuhub_upd_<pid>\Yuhub_updater.exe，
    # 用 `--apply-update <base64-json>` 拉起它，然后自己退出。
    # 替换器等旧 PID 死 → 两步改名（旧→.bak，新→原路径）→ 启动新版。
    # 必须在这里早返回：替换器不该去抢单例锁，也不该建 Qt 界面。
    if "--apply-update" in sys.argv:
        idx = sys.argv.index("--apply-update")
        rest = sys.argv[idx + 1:]
        if len(rest) != 1:
            sys.exit(2)
        try:
            import updater
            sys.exit(updater.apply_update_main(rest[0]))
        except Exception:
            sys.exit(5)

    # ---- update-selftest 模式：无窗口自检自动更新全流程 ----
    # 用法： Yuhub.exe --update-selftest <结果json路径>
    # 更新流程涉及"改自己的文件 + 重启自己"，在真 exe 上第一次试风险太高，
    # 所以在 exe 内部用本地 http.server 伪装更新源、用副本 exe 当替换目标，
    # 把下载 / 校验 / 替换 / 回滚全跑一遍，结果写 JSON 回传。
    if "--update-selftest" in sys.argv:
        idx = sys.argv.index("--update-selftest")
        rest = sys.argv[idx + 1:]
        if not rest:
            sys.exit(2)
        try:
            import update_selftest
            sys.exit(update_selftest.run(rest[0]))
        except Exception:
            sys.exit(5)

    # ---- gpu-selftest 模式：无窗口自检显卡信息与 GPU 占用率 ----
    # 用法： Yuhub.exe --gpu-selftest <结果json路径>
    # A 卡机器上没有 nvidia-smi，占用率只能走 PDH 计数器；而 PDH 出错时
    # 不抛异常，只表现为"读不到实例"或"恒为 0%"，远程无法诊断。
    # 因此在 exe 内部把解析 / 分组 / 真实读取 / 注册表兜底全跑一遍。
    if "--gpu-selftest" in sys.argv:
        idx = sys.argv.index("--gpu-selftest")
        rest = sys.argv[idx + 1:]
        if not rest:
            sys.exit(2)
        try:
            import gpu_selftest
            sys.exit(gpu_selftest.run(rest[0]))
        except Exception:
            sys.exit(5)

    # ---- share-selftest 模式：无窗口自检「临时云盘」----
    # 用法： Yuhub.exe --share-selftest <结果json路径>
    # 云盘的核心承诺是"退出房间就下不到"，而这依赖 HTTP 服务只绑虚拟 IP、
    # token 派生正确、stop() 真的关掉监听——三样都不会抛异常，坏掉的话
    # 表现是"看着正常但谁都能拉"。这里在 127.0.0.1 上跑真实 HTTP 把它
    # 固化成断言（不碰 EasyTier、不要管理员权限）。
    if "--share-selftest" in sys.argv:
        idx = sys.argv.index("--share-selftest")
        rest = sys.argv[idx + 1:]
        if not rest:
            sys.exit(2)
        try:
            import share_selftest
            sys.exit(share_selftest.run(rest[0]))
        except Exception:
            sys.exit(5)

    # ---- 错误日志钩子（隐藏功能，无 UI）：出错/崩溃时静默写
    #      文档\Yuhub\error\crash_*.log。必须在建 QApplication 之前装好，
    #      否则 Qt 初始化阶段的崩溃就抓不到了。所有 --*-selftest 模式
    #      都在上面早返回了，不会受影响。
    try:
        import errorlog
        errorlog.install()
    except Exception:
        pass                        # 日志系统自身故障绝不影响启动

    # 高 DPI 适配（Qt6 默认开启，这里显式声明以保证高分屏清晰）
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Yuhub")
    app.setApplicationVersion(VERSION)
    app.setOrganizationName("Yuhub")
    app.setFont(QFont("Microsoft YaHei UI", 9))

    install_translations(app)

    # 禁用「选择类控件」上的滚轮改值（下拉框/数字框/滑块）。
    # 必须持有引用：守卫是 QObject，被 GC 掉过滤器就失效了。
    _wheel_guard = wheel_guard.install(app)

    # 应用图标（源码运行时从 resources 加载，打包后从临时目录加载）
    icon_path = resource_path(os.path.join("resources", "app.ico"))
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # ---- 单实例：抢不到说明已经有一个在跑，且已被唤醒，自己直接退出 ----
    guard = SingleInstance()
    if not guard.acquire():
        return 0

    app.setQuitOnLastWindowClosed(False)

    try:
        # ---- 启动画面：先于主窗口构建弹出（双击 exe 后很快就能看到）----
        # 主题解析依赖 QSettings，这里先做一次（MainWindow 里再做是幂等的），
        # 否则浅色用户会先看到一屏默认深色。
        splash = None
        if MINIMIZED_FLAG not in sys.argv:
            init_theme_from_settings(QSettings("Yuhub", "Yuhub"))
            splash = SplashScreen()
            splash.show()
            # 让启动画面先画出来，再继续构建主窗口（构建页面要几百毫秒）
            app.processEvents()

        window = MainWindow(start_minimized=(MINIMIZED_FLAG in sys.argv))
        # 第二个实例来敲门 → 把窗口显示到前台
        guard.activate_requested.connect(window.activate_window)
        # 兜底收尾：无论从哪条路退出，都要停掉后台线程、撤掉托盘图标
        app.aboutToQuit.connect(window.teardown)

        if window.started_hidden:
            # 开机自启：已经安静地待在托盘里了。
            # 这里不调 show()，于是 showEvent 也不会触发，硬件采集自然跳过——
            # 等用户真去看首页时再扫，不浪费开机那几秒。也不放启动动画。
            pass
        elif splash is not None:
            # 窗口保持隐藏，后台采集首页硬件数据（CPU / 显卡 / 硬盘）；
            # 就绪后显示窗口，启动画面模糊 → 淡出退场。
            splash.set_status("正在加载硬件信息…")

            def _enter_app():
                window.show()
                window.activateWindow()
                splash.finish()

            window.launch_with_data_wait(_enter_app)
        else:
            # 开机自启但托盘不可用的回退路径：直接显示窗口，无启动动画
            window.show()

        return app.exec()
    finally:
        guard.close()


if __name__ == "__main__":
    sys.exit(main())
