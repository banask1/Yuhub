"""内嵌 EasyTier：跨网组网的"蓝盾体验"，零自建服务器。

## 它在 Yuhub 里的角色

EasyTier（开源 Apache 2.0，Rust）会在本机创建一块**二层虚拟网卡**，
把输入了相同"网络名+密码"的异地电脑拉进同一个虚拟局域网——广播可以
跨网传播，所以 Yuhub 现有的"局域网联机"模式（抓宣告 + 代理端口）
在虚拟网上**原样就能工作**。

分工：
    EasyTier  负责"把两台电脑变成同一个局域网"（打洞 + 社区公共节点中继兜底）
    Yuhub     负责壳：房间码/密码管理、启动/停止、状态显示

## 三个实现要点（都踩过验证过）

1. **提权不可避免**：创建虚拟网卡（wintun）必须管理员权限。Yuhub 主程序
   保持普通权限运行，启动 EasyTier 时用 `ShellExecuteW("runas")` 弹一次 UAC。

2. **提权进程收不到管道输出，普通进程也杀不掉它**。解法是"看门狗"：提权
   启动的不是 easytier 本体，而是 `Yuhub.exe --watchdog` 的一个新实例
   （同一个 GUI 子系统 exe），它用 `CREATE_NO_WINDOW` 标志启动
   easytier-core（避开 conhost 闪烁），再轮询一个**停止信号文件**；
   Yuhub 侧写这个文件，watchdog 看到就 taskkill easytier-core 并自删信号。
   **启动时弹一次 UAC，停止时零弹窗，看门狗期间零窗口闪烁**。

3. **状态靠探测，不靠输出**：提权进程的 stdout 我们拿不到。可靠的就绪判据是
   「系统里出现了虚拟网卡且拿到了 10.126.126.0/24 的地址」——用
   GetIpAddrTable 枚举（见 _ip_addresses）。

## 为什么 watchdog 用 Yuhub.exe，而不是 wscript + VBScript 或 PowerShell

上一版用 wscript.exe 跑 .vbs（`WScript.Shell.Run` 启动 easytier-core），
用户实测看到有窗口反复闪烁，路径指向 `C:\\Windows\\System32`——那是
Windows 给 easytier-core（控制台子系统）分配的 conhost 子进程窗口。
即使窗口模式设 0，**只要创建了 conhost，Windows 在创建/隐藏之间就有
那一瞬的可见窗口**，反复出现就形成"闪烁"。

- PowerShell 也同理，且 `-WindowStyle Hidden` 会让脚本卡住不执行；
- wscript 路径看上去"无控制台"，但子进程窗口层面没法控；
- 让 GUI 子系统的可执行文件（Yuhub.exe 自己）担任 watchdog，再用
  Win32 `CREATE_NO_WINDOW` 标志启动子进程，**根本不让 Windows 创建
  conhost**，从根上消灭闪烁。

参数传递走 base64+JSON：避免命令行引号 + 空格 + 特殊字符的转义陷阱。

## 二进制

resources/easytier/ 内含三个文件（最小依赖集，实测确定）：
    easytier-core.exe  核心（24 MB）
    wintun.dll         虚拟网卡用户态驱动（知名开源，无恶意软件误报风险）
    Packet.dll         core 启动时即加载，缺了直接 0xC0000135

首次使用时释放到 %LOCALAPPDATA%\\Yuhub\\etier\\——**从固定目录运行**，
而不是 PyInstaller 的 _MEIxxxx 临时目录：临时目录随主程序退出被删，
而提权进程可能还引用着那里的文件。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

# Win32 CREATE_NO_WINDOW = 0x08000000：禁止 Windows 给控制台子系统子进程
# 创建 conhost.exe 窗口。我们是 PyInstaller --windowed 打包的 GUI 程序，
# 本身没有控制台，每个 subprocess.run("arp" / "tasklist" / "route" /
# "taskkill" ...) 都会触发 Windows 偷偷创建一个 conhost，在创建/销毁
# 的几百毫秒里那窗口会一闪而过——Yuhub 的 _tick 每 800ms 调一次
# list_members() / core_running()，于是用户看到的就是"C:\Windows\System32
# 路径下的白色窗口反复闪烁"。**所有调控制台子进程的代码**都要带这个 flag。
_CREATE_NO_WINDOW = 0x08000000

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 中继/会合节点。官方公共节点（public.easytier.cn / public.easytier.top /
# public.kkrainbow.top）2026 年起已从 DNS 摘除（NXDomain，实测确认），
# 下面这些是 2026-09-27 实测 TCP 可连、且 core 握手成功的社区共享节点
# （按稳定性排序：无 DNS 依赖的 IP 优先）。节点会失效，连接失败会自动换下一个。
PUBLIC_SERVERS = (
    "tcp://47.108.52.1:11012",
    "tcp://38.147.105.178:11010",
    "tcp://boi.de5.net:11010",
    "tcp://easytier.weiai.org.cn:11010",
    "tcp://et-hk.clickor.click:11010",
)

# 虚拟网卡默认网段（EasyTier DHCP 默认从这里分配）
VIRTUAL_NET_PREFIX = "10.126.126."

RESOURCE_DIR_NAME = "easytier"
BIN_NAMES = ("easytier-core.exe", "wintun.dll", "Packet.dll")

# 停止信号文件（普通权限可写，提权看门狗可读）
_STOP_FILE = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub", "etier_stop")


def install_dir():
    """EasyTier 二进制的落地目录（固定，不用 _MEI 临时目录，见模块注释）。"""
    return os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub", "etier")


def core_path():
    return os.path.join(install_dir(), "easytier-core.exe")


# ---------------------------------------------------------------------------
# 资源释放
# ---------------------------------------------------------------------------
def _resource_dir():
    """源码运行时是 Yuhub/resources/easytier；打包后在 _MEIPASS/easytier。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(here, "resources", RESOURCE_DIR_NAME)
    if os.path.isdir(cand):
        return cand
    import sys
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = os.path.join(meipass, RESOURCE_DIR_NAME)
        if os.path.isdir(cand):
            return cand
    return ""


def release_binaries(force=False):
    """把内置的 EasyTier 释放到固定目录。返回 (成功, 说明)。"""
    src = _resource_dir()
    if not src:
        return False, "内置 EasyTier 资源缺失"
    dst = install_dir()
    os.makedirs(dst, exist_ok=True)
    for name in BIN_NAMES:
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if not os.path.exists(s):
            return False, f"缺少 {name}"
        if force or not os.path.exists(d) or os.path.getsize(d) != os.path.getsize(s):
            try:
                shutil.copy2(s, d)
            except PermissionError:
                # 目标文件正被运行的 easytier 占用；大小一致就当可用
                if os.path.exists(d) and os.path.getsize(d) == os.path.getsize(s):
                    continue
                return False, f"{name} 被占用且版本不同"
    return True, dst


# ---------------------------------------------------------------------------
# 一键修复：从网上重新下载同版本 EasyTier 并安装
# ---------------------------------------------------------------------------
# 背景：杀毒软件（尤其国内某些）会把刚释放出来的 easytier-core.exe 当风险
# 程序直接删掉，用户下载完 Yuhub 一进联机页就报"内置资源缺失"。重新下载
# Yuhub 本体解决不了（再释放再被删），正确姿势是单独把 EasyTier 组件
# 从官方 GitHub Release 重新拉一份装回去。
#
# 版本必须与内置的完全一致（内置 = 2.6.4-8428a89d，实测 `--version` 确认），
# 否则新旧混跑会出现协议不兼容。下载源按顺序尝试：GitHub 直连 → 两个国内
# 常见的 GitHub 加速镜像（镜像会失效，失败了自然落到下一个）。
EASYTIER_VERSION = "2.6.4"
_EASYTIER_ASSET = ("easytier-windows-x86_64-v%s.zip" % EASYTIER_VERSION)
_EASYTIER_PATH = ("EasyTier/EasyTier/releases/download/v%s/" % EASYTIER_VERSION)
EASYTIER_DOWNLOAD_URLS = (
    "https://github.com/" + _EASYTIER_PATH + _EASYTIER_ASSET,
    "https://ghproxy.net/https://github.com/" + _EASYTIER_PATH + _EASYTIER_ASSET,
    "https://gh-proxy.com/https://github.com/" + _EASYTIER_PATH + _EASYTIER_ASSET,
)


def core_health():
    """检查 EasyTier 组件是否完好。返回 (ok, detail)。

    判据：三个文件都在、easytier-core.exe 大小正常（>1MB，被删一半/截断
    的文件大小会明显异常）。不在这里跑 --version——那是启动路径的职责，
    这里只做"文件级"体检，供设置页轮询显示。
    """
    core = core_path()
    if not os.path.isfile(core):
        return False, "缺少 easytier-core.exe（可能被安全软件删除）"
    if os.path.getsize(core) < 1_000_000:
        return False, "easytier-core.exe 大小异常（文件可能损坏）"
    for name in ("wintun.dll", "Packet.dll"):
        p = os.path.join(install_dir(), name)
        if not os.path.isfile(p):
            return False, "缺少 %s" % name
    return True, core


def repair_binaries(progress=None, timeout=600):
    """下载与内置同版本的 EasyTier 并重新安装到固定目录。

    progress: callable(str)，汇报进度文字（供 UI 显示）。可为 None。
    返回 (ok, msg)。ok=True 时 msg 是成功说明，否则是给用户看的原因。

    注意：房间运行中 easytier-core.exe 被占用、覆盖必然失败，所以这里
    会先尝试停掉它（写停止信号 + 兜底 taskkill，均不弹 UAC）；停不下来
    就明确让用户手动停止，绝不硬来。
    """
    report = progress or (lambda s: None)

    if core_running():
        report("检测到跨网房间正在运行，先停止它…")
        tier = EasyTier()
        tier.request_stop()
        if not tier.wait_stopped(5.0):
            force_kill_core()
            time.sleep(1.0)
        if core_running():
            return False, "无法停止正在运行的 easytier-core，请先停止跨网房间再修复"

    tmpdir = os.path.join(install_dir(), "_repair_tmp")
    zpath = os.path.join(tmpdir, "easytier.zip")
    last_err = ""
    try:
        os.makedirs(tmpdir, exist_ok=True)
        import urllib.request
        for i, url in enumerate(EASYTIER_DOWNLOAD_URLS, 1):
            report("正在下载 EasyTier v%s（第 %d/%d 个源，约 32 MB）…"
                   % (EASYTIER_VERSION, i, len(EASYTIER_DOWNLOAD_URLS)))
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Yuhub-Repair",
                    "Accept": "application/octet-stream",
                })
                # urlopen 会遵循系统/环境代理（urllib.request.getproxies），
                # 走系统代理的机器不会因为 GitHub 直连不通而失败。
                with urllib.request.urlopen(req, timeout=60) as resp, \
                        open(zpath, "wb") as f:
                    total = int(resp.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            report("下载中 %d%%（%.1f / %.1f MB）"
                                   % (got * 100 // total,
                                      got / 1048576.0, total / 1048576.0))
                if got < 10_000_000:
                    raise IOError("下载不完整（仅 %d 字节）" % got)
                last_err = ""
                break
            except Exception as exc:
                last_err = str(exc)
                continue
        if last_err:
            return False, "下载失败：%s（请检查网络后重试）" % last_err

        report("下载完成，正在解压安装…")
        import zipfile
        extracted = {}
        with zipfile.ZipFile(zpath) as z:
            for entry in z.namelist():
                base = os.path.basename(entry)
                if base in BIN_NAMES and base not in extracted:
                    z.extract(entry, tmpdir)
                    extracted[base] = os.path.join(tmpdir, entry)
        missing = [w for w in BIN_NAMES if w not in extracted]
        if missing:
            return False, "压缩包里缺少 %s（上游资产可能已变更）" % "、".join(missing)

        os.makedirs(install_dir(), exist_ok=True)
        for name, src in extracted.items():
            # tmpdir 就在 install_dir 里，同盘 os.replace 是原子改名
            os.replace(src, os.path.join(install_dir(), name))
    except Exception as exc:
        return False, "修复失败：%s" % exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    ok, detail = core_health()
    if not ok:
        return False, "安装后校验未通过：%s" % detail
    # 最后跑一次 --version 确认组件真的可用（文件在 ≠ 能跑）
    try:
        r = subprocess.run(
            [core_path(), "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
        ver = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        ver = ver[0].strip() if ver else ""
    except Exception as exc:
        return False, "安装后 easytier-core 无法运行：%s" % exc
    return True, ("已重新安装 EasyTier %s" % (ver or EASYTIER_VERSION))


# ---------------------------------------------------------------------------
# 提权启动（UAC）与看门狗
# ---------------------------------------------------------------------------
# 看门狗改用 Yuhub.exe --watchdog 模式（自己当 helper），而不是 wscript + VBScript
# 或 PowerShell。原因（都实测踩过）：
#   1) PowerShell 是控制台程序，ShellExecuteW(runas) 启动时 conhost 窗口会闪；
#      命令行加 -WindowStyle Hidden 又会让它卡住不执行（本环境确定性复现）；
#      ShowWindow / GetConsoleWindow 在分离会话下拿不到句柄，也藏不住。
#   2) wscript.exe 看似"无控制台"，但用 WScript.Shell.Run 启动控制台子系统程序
#      （如 easytier-core.exe）时，Windows 仍会分配一个 conhost 子进程；
#      窗口模式 0 = 隐藏对 conhost 无效，会被用户看到 C:\Windows\System32 路径
#      的窗口一闪而过——反复出现就成了"白窗口闪烁"。
#   3) 让 GUI 子系统的可执行程序（Yuhub.exe 自身）当 watchdog，再在脚本里用
#      Win32 CREATE_NO_WINDOW 标志启动 easytier-core：Windows 不给它分配
#      conhost，从根上消除窗口闪烁。
# 参数走 base64+JSON 序列化，再无命令行引号/空格/特殊字符的转义陷阱。
def _yuhub_exe_path():
    """watchdog 模式要启动的 Yuhub.exe 完整路径。

    这里的返回值会作为 `ShellExecuteW("runas", exe, "--watchdog <payload>")`
    的**可执行文件**，所以它必须是**认识 --watchdog 这个开关的那个程序**。

    坑（实测踩过，是"进入房间后一直显示正在启动"的根因之一）：
      源码运行时 `sys.executable` 是 **python.exe**，不是 Yuhub.exe。
      `python.exe --watchdog <b64>` 会被 Python 当成"要执行名为 --watchdog
      的脚本"，找不到就立刻退出；`core_running()` 在 3 秒宽限期后仍为假，
      于是报出误导性的"EasyTier 进程意外退出（密码含特殊字符或节点不可达？）"。
      而且 python.exe 是**控制台**程序，runas 启动它还会闪一个黑窗口——
      正是 watchdog 方案当初要消灭的现象。

    所以：
      * 打包后（sys.frozen）→ `sys.executable` 就是 Yuhub.exe，直接用；
      * 源码运行 → 必须去找**真正构建出来的 Yuhub.exe**（项目根目录），
        实在找不到才回落到 python.exe 并让调用方给出可读的错误提示。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 打包态：sys.executable 就是 Yuhub.exe
        exe = sys.executable
        if exe and os.path.isfile(exe):
            return exe

    # 源码运行态：从 etier.py 所在目录（项目根）找构建产物
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "Yuhub.exe"),
        os.path.join(here, "dist", "Yuhub.exe"),
    ):
        if os.path.isfile(cand):
            return cand

    # 最后兜底：打包态下 sys.executable 一般可用；源码态下这里会拿到
    # python.exe —— 调用方 `launch_elevated` 会识别并给出明确提示，
    # 而不是让它静默地去启动一个不认识 --watchdog 的程序。
    return sys.executable or ""


def _parent_process_name():
    """当前进程真实的 exe 文件名（供 watchdog 做防 PID 复用校验）。

    优先读**真实映像名**而不是 `sys.executable`：
    源码态下两者可能都不等于 watchdog 眼里的父进程名，
    但用 `QueryFullProcessImageNameW` 拿到的才是权威值。
    取不到时回落到 sys.executable 的 basename，绝不返回空串
    （空串会让 watchdog 跳过 PID 校验）。
    """
    try:
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong),
        ]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, os.getpid())
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value)
            finally:
                k32.CloseHandle(h)
    except Exception:
        pass
    return os.path.basename(sys.executable or "")


def watchdog_capable(exe_path=None):
    """判断给定程序能否充当 watchdog（即它是不是带 --watchdog 的 Yuhub.exe）。

    源码开发态没有构建产物时，提权启动必然失败，与其让用户看到
    "进程意外退出"这种误导信息，不如提前识别、给出可操作的提示。
    """
    path = (exe_path or _yuhub_exe_path()) or ""
    if not path or not os.path.isfile(path):
        return False, "找不到 Yuhub.exe（请先构建，或直接运行打包后的 exe）"
    base = os.path.basename(path).lower()
    if base == "yuhub.exe":
        return True, path
    # 源码态且没有构建产物 —— 明确拒绝，不把 python.exe 当 watchdog 用
    if base.startswith("python"):
        return False, ("当前是源码运行且未找到构建好的 Yuhub.exe，"
                       "无法创建虚拟网卡（提权看门狗需要 Yuhub.exe）。"
                       "请先运行 build.bat 构建，或改用打包后的 Yuhub.exe。")
    return False, "watchdog 可执行文件不可用：%s" % path


def build_watchdog_payload(core, args, stopfile, workdir, parent_pid=0, parent_name=""):
    """把 watchdog 需要的核心参数打包成 base64(JSON)。

    流程：JSON 序列化 → UTF-8 → base64（ASCII）→ 主进程通过
    `Yuhub.exe --watchdog <payload>` 把它交给 watchdog 进程。

    parent_pid / parent_name：主 Yuhub 进程的 PID 与 exe 名。watchdog 会持续
    检查它是否还活着——一旦主进程消失（正常退出 / 被任务管理器强杀 / 崩溃），
    watchdog 自动停掉 easytier-core 并退出。这是"退出 Yuhub 自动关房间"的
    关键：不依赖主进程主动写停止信号，主进程**没了**本身就是停止信号。
    parent_name 用于防 PID 复用（父进程退出后 PID 被别的进程占了的误判）。
    """
    payload = {"core": core, "args": list(args),
               "stop": stopfile, "workdir": workdir,
               "parent_pid": int(parent_pid or 0),
               "parent_name": parent_name or ""}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def launch_elevated(payload_b64):
    """UAC 提权启动 watchdog（Yuhub.exe --watchdog <payload>）。

    用户会在屏幕上看到一次 UAC 确认框（"Yuhub.exe"请求管理员权限），
    取消则返回失败。watchdog 是 GUI 子系统的程序，本进程无控制台窗口；
    它用 CREATE_NO_WINDOW 启动 easytier-core，**彻底不会**让
    `C:\\Windows\\System32\\cmd.exe / conhost.exe` 之类的窗口出现，
    解决"白窗口反复闪烁"问题。
    """
    exe = _yuhub_exe_path()
    # 先确认这个程序真的认识 --watchdog。否则 runas 会把开关交给
    # python.exe / 别的东西，对方立刻退出，用户却只看到"进程意外退出"。
    capable, why = watchdog_capable(exe)
    if not capable:
        return False, why
    # ShellExecuteW 的 lpParameters 是单个命令行字符串；用 base64（仅含
    # [A-Za-z0-9+/=]）传参，**完全无需关心 Windows 命令行解析规则**。
    params = "--watchdog " + payload_b64
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 0,
        )
        if rc > 32:
            return True, "已请求管理员权限"
        return False, f"启动被取消或失败（代码 {rc}）"
    except OSError as exc:
        return False, f"提权启动失败：{exc}"


def kill_core_elevated():
    """提权强制结束残留的 easytier-core（Yuhub.exe --kill-core）。

    场景：watchdog 已经不在了（被强杀 / 异常），但 easytier-core 还在跑，
    普通权限的 taskkill 杀不掉提权进程。这里 runas 走 --kill-core 模式清理。
    **会弹一次 UAC**，只在确实需要时调用。
    返回 (成功, 说明)。
    """
    exe = _yuhub_exe_path()
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, "--kill-core", None, 0,
        )
        if rc > 32:
            return True, "已请求管理员权限清理残留"
        return False, f"清理被取消或失败（代码 {rc}）"
    except OSError as exc:
        return False, f"提权清理失败：{exc}"


# ---------------------------------------------------------------------------
# 状态探测：枚举本机 IPv4 地址，找 EasyTier 虚拟网段
# ---------------------------------------------------------------------------
# 为什么不用 GetAdaptersAddresses：它的 IP_ADAPTER_ADDRESSES 结构体字段
# 极多，手写 ctypes 定义一旦偏移错位就是段错误（实测崩过）。
# GetIpAddrTable 只要一个 MIB_IPADDRROW（7 个字段），稳如老狗；
# 判据只需要"出现 10.126.126.x"，用不着网卡名。
class _MibIpAddrRow(ctypes.Structure):
    _fields_ = [("dwAddr", wt.DWORD), ("dwIndex", wt.DWORD),
                ("dwMask", wt.DWORD), ("dwBCastAddr", wt.DWORD),
                ("dwReasmSize", wt.DWORD), ("unused1", wt.USHORT),
                ("wType", wt.USHORT)]


def _ip_addresses():
    """本机所有 IPv4 地址（点分字符串列表）。失败返回 []。"""
    size = wt.ULONG(0)
    if ctypes.windll.iphlpapi.GetIpAddrTable(None, ctypes.byref(size), 0) != 122:
        return []                                # 122 = ERROR_INSUFFICIENT_BUFFER
    buf = ctypes.create_string_buffer(size.value)
    if ctypes.windll.iphlpapi.GetIpAddrTable(buf, ctypes.byref(size), 0) != 0:
        return []
    count = wt.ULONG.from_buffer_copy(buf, 0).value
    rows = []
    for i in range(count):
        row = _MibIpAddrRow.from_buffer_copy(buf, 4 + i * ctypes.sizeof(_MibIpAddrRow))
        raw = row.dwAddr
        rows.append("%d.%d.%d.%d" % (raw & 255, (raw >> 8) & 255,
                                     (raw >> 16) & 255, (raw >> 24) & 255))
    return rows


def virtual_adapter_ip():
    """虚拟网卡就绪时返回虚拟 IP，否则空串。

    判据：IPv4 落在 10.126.126.0/24（EasyTier DHCP 默认段）。
    """
    for ip in _ip_addresses():
        if ip.startswith(VIRTUAL_NET_PREFIX):
            return ip
    return ""


def _resolve_name(ip, timeout=0.6):
    """把虚拟 IP 反查成主机名（昵称）。

    EasyTier 的虚拟网卡走的是 NetBIOS/LLMNR 名称解析，`socket.gethostbyaddr`
    在 Windows 上会走系统解析链，多数情况能把设置过 --hostname 的节点查出来。
    查不到返回空串——调用方回落到只显示 IP。
    """
    try:
        socket.setdefaulttimeout(timeout)
        name, _aliases, _addrs = socket.gethostbyaddr(ip)
        return (name or "").strip()
    except Exception:
        return ""
    finally:
        socket.setdefaulttimeout(None)


def list_members_detailed(my_ip="", resolve=True):
    """列出在线成员，尽量带上名称。

    返回 [{"ip": "10.126.126.3", "name": "XiaoYu"}, ...]，不含自己。

    名称来源优先级：
      1. 反查主机名（EasyTier 的 --hostname 会体现在这里）
      2. 查不到就留空，UI 只显示 IP

    反查有超时（每个 0.6 秒），所以成员多时耗时会线性增长——
    调用方（lan_page 的 800ms tick）应传入 resolve=False 做快速刷新，
    只在需要时偶尔做一次带名称的完整刷新。
    """
    ips = list_members(my_ip)
    out = []
    for ip in ips:
        name = _resolve_name(ip) if resolve else ""
        out.append({"ip": ip, "name": name})
    return out


def probe_host(host_ip, timeout=2.0, attempts=2):
    """探测某个虚拟 IP 是否在线（用于成员校验"房主是否真的在房间里"）。

    用 ping 而不是 ARP：ARP 条目可能是陈旧的缓存，ping 是主动探测更可信。
    Windows 的 ping 即使不通也返回 0，所以必须看输出里的 TTL/时间字段。
    """
    for _ in range(max(1, attempts)):
        try:
            r = subprocess.run(
                ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host_ip],
                capture_output=True, text=True, encoding="gbk",
                errors="replace", timeout=timeout + 2,
                creationflags=_CREATE_NO_WINDOW,
            )
            out = (r.stdout or "")
            # 中文系统输出 "TTL=" / 英文 "TTL="；不通时是 "无法访问目标主机"/"timed out"
            if "TTL=" in out.upper() or "ttl=" in out:
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# 成员在线跟踪：解决"在线人数断断续续"
# ---------------------------------------------------------------------------
# 根因：以前直接拿 `arp -a` 的结果当在线名单。Windows 的 ARP 条目受
# "可达性超时"控制——一段时间没有流量，条目就从表里消失（哪怕对方还在线）；
# EasyTier 节点之间没有持续大流量时这几乎是必然发生的。于是成员列表每隔
# 十几秒就"闪没"又"闪回来"。
#
# 解法 = 保活 + 宽限：
#   1. **保活**：后台线程每 3 秒对已知成员逐个 ping（1 次、0.5s 超时）。
#      ICMP 流量本身会刷新 ARP 条目（条目不会过期），ping 通同时是对端
#      活性的主动确认，比被动等 ARP 靠谱。
#   2. **宽限**：离线判定要求**两个信号同时失效**——ARP 里连续 12 秒
#      没见到、且连续 4 次 ping 都不通。任何一个信号单独抖动都不会把人
#      从列表里闪没。
# 名称解析（gethostbyaddr，每个最长 0.8s）也挪进这个后台线程，解析一次
# 就缓存——UI 线程从此只读快照，一个子进程都不起、零阻塞。
class MemberTracker:
    """一个跨网房间对应一个实例。UI 通过 snapshot() 拿稳定视图。"""

    PING_INTERVAL = 3.0      # 每轮 ping 的间隔（秒）
    PING_TIMEOUT = 0.5       # 单次 ping 超时（秒）
    PING_FAIL_EVICT = 4      # 连续 ping 失败次数达到该值才可能判离线
    CONFIRM_GRACE = 12.0     # 最后一次确认（ARP 见到 / ping 通）后的宽限（秒）

    def __init__(self, my_ip, on_log=None):
        self._my_ip = my_ip
        self._on_log = on_log or (lambda msg: None)
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread = None
        # ip -> {"confirmed": monotonic, "ping_fail": int, "name": str}
        self._peers = {}

    # ------------------------------------------------ 对外接口（UI 用）
    def start(self):
        """启动后台跟踪线程。重复调用无害。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop, name="MemberTracker", daemon=True)
        self._thread.start()

    def stop(self):
        """停止后台线程（停止房间 / 回滚时调用）。瞬时返回。"""
        self._stop_evt.set()

    def snapshot(self):
        """当前在线成员 [{"ip", "name"}]，按 IP 排序。UI 线程调用，零阻塞。"""
        with self._lock:
            return [{"ip": ip, "name": (p.get("name") or "")}
                    for ip, p in sorted(self._peers.items())]

    # ------------------------------------------------ 后台线程主体
    def _loop(self):
        while not self._stop_evt.is_set():
            try:
                self._round()
            except Exception:
                pass                    # 单轮失败不影响下一轮
            self._stop_evt.wait(self.PING_INTERVAL)

    def _round(self):
        now = time.monotonic()
        # ① ARP 扫描：新面孔入场 + 老成员确认在场
        try:
            arp = set(list_members(self._my_ip))
        except Exception:
            arp = set()
        with self._lock:
            for ip in arp:
                p = self._peers.setdefault(
                    ip, {"confirmed": now, "ping_fail": 0, "name": ""})
                p["confirmed"] = now
                p["ping_fail"] = 0
        # ② 逐个 ping：保活 ARP + 主动确认活性
        with self._lock:
            targets = list(self._peers.keys())
        for ip in targets:
            if self._stop_evt.is_set():
                return
            alive = probe_host(ip, timeout=self.PING_TIMEOUT, attempts=1)
            self._on_ping_result(ip, alive, now)
        # ③ 离线判定（双信号失效才移除）
        self._sweep(now)
        # ④ 名称解析：只解析还没名字的，解析一次就缓存
        self._resolve_names()

    def _on_ping_result(self, ip, alive, now=None):
        """记录一次 ping 结果（抽出来是为了可测）。"""
        now = now if now is not None else time.monotonic()
        with self._lock:
            p = self._peers.get(ip)
            if p is None:
                return
            if alive:
                p["confirmed"] = now
                p["ping_fail"] = 0
            else:
                p["ping_fail"] += 1

    def _sweep(self, now=None):
        """移除确认离线的成员：连续 ping 失败 **且** ARP 长时间未见。"""
        now = now if now is not None else time.monotonic()
        with self._lock:
            gone = [ip for ip, p in self._peers.items()
                    if p["ping_fail"] >= self.PING_FAIL_EVICT
                    and now - p["confirmed"] > self.CONFIRM_GRACE]
            for ip in gone:
                del self._peers[ip]
        for ip in gone:
            self._on_log("成员 %s 已离线" % ip)

    def _resolve_names(self):
        with self._lock:
            need = [ip for ip, p in self._peers.items() if not p.get("name")]
        for ip in need:
            if self._stop_evt.is_set():
                return
            name = _resolve_name(ip, timeout=0.8)
            if name:
                with self._lock:
                    p = self._peers.get(ip)
                    if p is not None:
                        p["name"] = name


def pick_host_ip(my_ip, exclude_ips=None, hostname="", wait=0.0, interval=0.8):
    """在虚拟网里找出**房主**的 IP（DHCP 动态分配下用）。

    为什么需要它：以前房主固定 10.126.126.1，成员直接 ping 那个地址就能
    判断"房间是否存在"。改用 DHCP 后房主 IP 不再固定，必须动态发现。

    判定顺序（从可信到将就）：
      1. `hostname` 匹配 —— 房主在界面上填的昵称能被反查出来，这是
         **最可信**的判据。房主自己知道自己叫什么，所以自己先试这一条。
      2. 回落到"网段里除了已知成员外，还能 ping 通的那个" —— 新成员
         加入时其他成员是它没见过的，谁通谁就是房间里的老节点。

    为什么要 `exclude_ips`：在**已经**连上房间的成员视角下，网段里会
    同时出现房主和其他成员。准入校验要的是"房间里有没有别人"，对成员来说
    这是天然成立的（它已经连上了），所以这个方法主要用于**新加入者**——
    对新加入者来说，除自己外的一切都是它第一次见到的。

    注意这是"尽力而为"的启发式：它无法 100% 区分"房主"和"另一个成员"。
    真要精确到房主需要应用层信令（引入中心服务器），而准入校验真正要
    回答的问题是"房间码+密码对不对"，这个启发式已经足够。

    my_ip：本机虚拟 IP（必须排除自己）
    返回发现的 IP，找不到返回 ""。
    """
    me = my_ip or virtual_adapter_ip()
    skip = set(exclude_ips or ())
    skip.add(me)
    end = time.monotonic() + wait
    while True:
        candidates = [ip for ip in list_members(me) if ip not in skip]
        if hostname:
            # ① 按昵称精确匹配（房主自报家门，最可信）
            for ip in candidates:
                if _resolve_name(ip, timeout=0.8).lower() == hostname.lower():
                    return ip
        # ② 谁能 ping 通就算数（房间里有活人 = 码和密码是对的）
        for ip in candidates:
            if probe_host(ip, timeout=1.5, attempts=1):
                return ip
        if time.monotonic() >= end:
            return ""
        time.sleep(interval)



def list_members(my_ip=""):
    """列出当前跨网网络里的在线成员（虚拟 IP 列表，不含自己）。

    原理：EasyTier 建连后，对端节点的虚拟 IP 会出现在本机 ARP 表里
    （10.126.126.0/24 网段）。用 `arp -a` 抓取该网段的所有条目，
    过滤掉本机自身 IP，剩下的就是在线成员。比 ping 探测更快更准。
    """
    members = []
    try:
        out = subprocess.run(
            ["arp", "-a"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=8, creationflags=_CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return members
    seen = set()
    for line in out.splitlines():
        # 只抓 10.126.126.x 网段的条目
        if VIRTUAL_NET_PREFIX not in line:
            continue
        parts = line.split()
        for p in parts:
            if p.startswith(VIRTUAL_NET_PREFIX):
                ip = p.strip()
                # 排除自己、广播地址 .255、网络地址 .0、以及网关等非主机地址
                last = ip.rsplit(".", 1)[-1]
                if last in ("0", "255"):
                    continue
                if ip != my_ip and ip not in seen:
                    seen.add(ip)
                    members.append(ip)
    return members


def core_running():
    """easytier-core.exe 是否在运行（tasklist 查询，普通权限即可）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq easytier-core.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=8, creationflags=_CREATE_NO_WINDOW,
        )
        return "easytier-core.exe" in (out.stdout or "")
    except Exception:
        return False


def force_kill_core():
    """强制结束所有 easytier-core.exe（用于清理切换网络后残留的进程）。

    看门狗可能因为网络切换等异常没来得及收尾，留下孤儿进程；再启动时
    core_running() 恒真、虚拟网卡又拿不到 IP，就会一直卡在「已有房间在运行」。
    这里用 taskkill /F 直接清掉（普通权限即可杀自己提权拉起的同用户进程）。
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "easytier-core.exe"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=10, creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        pass
    # 清掉可能残留的停止信号文件
    try:
        os.remove(_STOP_FILE)
    except OSError:
        pass


def _preflight_silence_windows(core_exe_path):
    """启动 easytier-core 之前：消除 Windows 两个常见弹窗。

    1) **Windows Defender 防火墙**：第一次以网络应用身份启动 easytier-core 时
       会弹"已阻止 easytier-core.exe 的部分功能"白弹窗。用 `netsh advfirewall`
       给该 exe 同时加 inbound + outbound 例外规则，**任何端口都允许**。
       这一步要管理员权限——本函数本来就在 watchdog 上下文（已 runas 提权）跑。
       规则已存在就静默跳过，不报错。

    2) **新网络连接通知弹窗**：wintun 第一次创建虚拟网卡时 Windows 会弹
       "选择网络位置（家庭/工作/公共）"——这是注册表控制项：
       `HKLM\\System\\CurrentControlSet\\Control\\Network\\NewNetworkNotify`
       设为 DWORD=0 即可彻底禁用，且不影响其它网络提示。
       也会改 `NlaSvc\\AlwaysAllowApp` 让网络位置感知不再追问。

    这两个修改都是注册表 / 防火墙的标准做法，影响范围限定在 easytier-core.exe
    这一条规则，**不会影响系统的其它行为**。
    """
    # 1) 防火墙例外：inbound + outbound 各一条即可（protocol=any 同时覆盖 TCP+UDP）。
    #    规则名如果已存在，netsh 会报错"对象已存在"——一律 try/except 静默吃掉，
    #    不要中途 continue 掉另一半 inbound/outbound。
    rule_name = "Yuhub EasyTier (允许异机联机)"
    for direction in ("in", "out"):
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule",
             "name=" + rule_name],
            capture_output=True, encoding="gbk", errors="replace",
            timeout=8, creationflags=_CREATE_NO_WINDOW,
        )
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "add", "rule",
             "name=" + rule_name,
             "dir=" + direction,
             "action=allow",
             "program=" + core_exe_path,
             "protocol=any",
             "enable=yes"],
            capture_output=True, encoding="gbk", errors="replace",
            timeout=10, creationflags=_CREATE_NO_WINDOW,
        )

    # 2) 关闭"新网络连接通知"对话框：写注册表 DWORD=0
    try:
        import winreg
        key_path = r"System\CurrentControlSet\Control\Network\NewNetworkNotify"
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "NewNetworkNotify", 0, winreg.REG_DWORD, 0)
    except Exception:
        pass

    # 3) 网络位置感知 NLA：禁止对这台电脑自动弹位置询问
    #    改 `HKLM\\SYSTEM\\CurrentControlSet\\Services\\NlaSvc\\Parameters\\Internet`
    #    加 ActiveConnections = 0 是不彻底的；正确做法是关闭 NLA 的"主动探测"。
    #    但简单且稳妥的是把 `HKLM\\Software\\Microsoft\\Windows NT\\CurrentVersion\\
    #    NetworkList\\NlaSvc\\NewNetworks` 的 DWORD `NewNetwork` 设为 0：
    #    这个是 NLA 服务自己读的配置，改完服务下次重启生效。
    #    用户场景下，easytier 第一次启动可能仍弹一次，重启后再启动就完全不弹。
    try:
        import winreg
        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE,
            r"Software\Microsoft\Windows NT\CurrentVersion\NetworkList\NlaSvc",
            0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "NewNetworks", 0, winreg.REG_DWORD, 0)
    except Exception:
        pass


def _physical_network_fingerprint():
    """本机物理网络的指纹：非虚拟网段（排除 10.126.126.x 和常见虚拟网卡段）的
    IPv4 集合 + 默认网关。网络一换（WiFi→热点、换 WiFi）指纹就变，用于检测
    「切换网络导致虚拟局域网失效」这个场景。

    返回一个可比较的元组（排序后的 IP 列表 + 网关），失败返回 None。
    """
    ips = []
    for ip in _ip_addresses():
        # 排除 EasyTier 虚拟网段
        if ip.startswith(VIRTUAL_NET_PREFIX):
            continue
        # 排除回环
        if ip.startswith("127."):
            continue
        ips.append(ip)
    gateway = _default_gateway()
    return (tuple(sorted(ips)), gateway)


def _default_gateway():
    """用 route print 抓默认网关（0.0.0.0 目标）。失败返回空串。"""
    try:
        out = subprocess.run(
            ["route", "print", "0.0.0.0"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            timeout=8, creationflags=_CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return ""
    # 取 0.0.0.0 0.0.0.0 那一行的网关（下一跳）列，IPv4 段里
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
            gw = parts[2]
            # 只认合法 IPv4
            if gw.count(".") == 3 and all(p.isdigit() for p in gw.split(".")):
                return gw
    return ""



# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
class EasyTier:
    """一个跨网房间对应一个实例。线程安全（内部有锁）。"""

    def __init__(self, on_log=None):
        self._lock = threading.Lock()
        self._on_log = on_log or (lambda msg: None)
        self._started_at = 0.0
        self._base_fingerprint = None    # 启动时的物理网络指纹（用于检测切换网络）

    def _cleanup_residual(self):
        """清理上次会话残留的 easytier-core 进程。返回是否已清理干净。

        策略从轻到重（每级都比上一级更"重"、体验更差）：
          1. **写停止信号** —— 如果上次的 watchdog 还活着，它会自己在 500ms
             内收尾（零 UAC 弹窗、零窗口闪烁）。这是最常见且最干净的情况。
          2. **普通权限 taskkill** —— watchdog 死了、easytier-core 是普通权限
             起的（少见）时有效。
          3. **提权 --kill-core** —— 最后兜底，会弹一次 UAC。watchdog 死了但
             easytier-core 是提权起的，只有这条路能杀掉。
        """
        # 1. 写停止信号，等 watchdog 自己收（~2 秒）
        self.request_stop()
        if self.wait_stopped(2.0):
            return True
        # 2. 普通权限直接杀
        self._on_log("停止信号未生效，尝试直接结束进程…")
        force_kill_core()
        time.sleep(0.5)
        if not core_running():
            return True
        # 3. 提权兜底（弹一次 UAC）
        self._on_log("需要管理员权限才能结束残留进程，请在 UAC 弹窗点「是」…")
        ok, msg = kill_core_elevated()
        if not ok:
            self._on_log(f"提权清理失败：{msg}")
            return False
        time.sleep(1.5)                 # 给提权进程一点时间跑完 taskkill
        return not core_running()

    def start(self, name, secret, timeout=20.0, ipv4="", hostname=""):
        """创建/加入跨网网络。阻塞直到虚拟网卡就绪或超时。

        name/secret 即 EasyTier 的 network-name / network-secret。
        ipv4：固定虚拟 IP；传空串则走 DHCP 动态分配（推荐）。
              动态分配由 EasyTier 的 DHCP 完成，**遇到 IP 冲突会自动改地址**，
              所以多人/多房间并存时不会互相抢 IP（固定 IP 会）。
              代价是 IP 不固定，不能靠"猜房主 IP"来做准入校验，改由
              `host_ip()` 动态发现（见 lan_page 的成员校验）。
        hostname：本节点在房间里的显示名（队友在成员列表里看到的就是它）；
                  为空则 EasyTier 用系统计算机名（DESKTOP-XXXX 不好认）。
        返回 (成功, 说明)。成功后 virtual_adapter_ip() 可查虚拟 IP。
        """
        with self._lock:
            # 关键修复：切换网络 / 上次异常退出后可能残留孤儿 easytier-core 进程，
            # 导致 core_running() 恒真、永远提示「已有一个房间在运行」。
            # lan_page 保证了 start() 只在"本会话没启动过"时被调用，所以这里
            # 遇到的 core_running() **一定是上会话的残留**，直接清掉。
            if core_running():
                self._on_log("检测到上次残留的 EasyTier 进程，正在清理…")
                if not self._cleanup_residual():
                    return False, ("无法结束上次残留的 EasyTier 进程，"
                                   "请在任务管理器里手动结束 easytier-core.exe 后重试")
                self._on_log("残留进程已清理，继续启动")
            ok, msg = release_binaries()
            if not ok:
                self._on_log(f"EasyTier 释放失败：{msg}")
                return False, msg
            # 清掉上次可能残留的停止信号
            try:
                os.remove(_STOP_FILE)
            except OSError:
                pass
            # 记录启动时的物理网络指纹，供 run 期间检测网络切换
            self._base_fingerprint = _physical_network_fingerprint()
            # Pre-flight：启动 easytier-core 之前先消除 Windows 自身的两个白弹窗。
            # 这一步要管理员权限（netsh advfirewall + 写注册表），所以放在 watchdog
            # 进程里跑——etier.py 当前是普通权限，写不了。返回 task 给 watchdog。
            self._on_log("准备：watchdog 会先静默配置防火墙例外 + 抑制新网络通知，再启 easytier-core")
            # 把"启动 easytier-core + 轮询停止信号"打包成一个 base64 payload，
            # 通过 `Yuhub.exe --watchdog <payload>` 提权启动。watchdog 是 GUI
            # 子系统进程 + 用 CREATE_NO_WINDOW 启 easytier-core，**不闪窗**。
            args = [
                "--network-name", name,
                "--network-secret", secret,
                "--latency-first",
                "--no-listener",
                # 捕获物理网卡上的 UDP 广播包并转发给对等节点：让依赖
                # 局域网广播发现房间的游戏（Minecraft 等）能看到彼此。
                # 仅 Windows 生效，需要管理员权限——watchdog 本来就是提权进程。
                "--enable-udp-broadcast-relay", "true",
            ]
            if hostname:
                # 队友在成员列表里看到的名字（不传就是计算机名 DESKTOP-XXXX）
                args += ["--hostname", hostname]
            if ipv4:
                # 固定虚拟 IP：只给 -i、不加 -d，否则 DHCP 会覆盖它（-d 与 -i 互斥）
                args += ["-i", ipv4]
            else:
                # 不指定 IP 时用 DHCP 自动分配（并自动创建虚拟网卡）
                args += ["-d", "true"]
            for srv in PUBLIC_SERVERS:
                args += ["-p", srv]
            payload = build_watchdog_payload(
                core_path(), args, _STOP_FILE, install_dir(),
                parent_pid=os.getpid(),
                # 用 watchdog 实际会看到的父进程名做防 PID 复用校验。
                # 注意不能用 sys.executable 的 basename：源码态下那是 python.exe，
                # 而 watchdog 眼里父进程是"拉起它的那个 Yuhub.exe"，对不上就会
                # 误判父进程已死 → 刚起来就自我收尾，表现为"一直正在启动"。
                parent_name=_parent_process_name(),
            )
            ok, msg = launch_elevated(payload)
            if not ok:
                self._on_log(f"EasyTier 启动失败：{msg}")
                return False, msg
            self._started_at = time.monotonic()
            # 轮询等虚拟网卡就绪（首次要装 wintun 适配器，可能十几秒）。
            #
            # "进程意外退出"的判定必须**从 easytier-core 真的出现过之后**才开始算：
            # 用户在 UAC 弹窗上停留的时间是不确定的（可能去接杯水），
            # 这段时间 core 本来就没在跑。若拿"发起提权"当起点，
            # 只要用户点"是"慢了 3 秒，就会误报"进程意外退出"。
            saw_core = False
            core_seen_at = 0.0
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                running = core_running()
                if running and not saw_core:
                    saw_core = True
                    core_seen_at = time.monotonic()
                    self._on_log("easytier-core 已拉起，正在等待虚拟网卡就绪…")
                if not running and saw_core and time.monotonic() - core_seen_at > 6:
                    # 出现过又消失 → 才是真的异常退出
                    return False, ("EasyTier 进程已退出（可能是密码含特殊字符、"
                                   "或公共节点均不可达）")
                ip = virtual_adapter_ip()
                if ip:
                    self._on_log(f"跨网网卡就绪，虚拟 IP {ip}")
                    return True, ip
                time.sleep(0.8)
            # 超时：区分"根本没起来"和"起来了但网卡没就绪"，提示要对得上
            if not saw_core:
                return False, ("%d 秒内未等到 EasyTier 启动。请确认已在 UAC 弹窗里"
                               "点「是」授权；若没有弹窗，可能是被安全软件拦截了。"
                               % int(timeout))
            return False, ("EasyTier 已运行但虚拟网卡未就绪（等待 %d 秒）。"
                           "首次创建虚拟网卡较慢，可稍后在「重新检测」或重试；"
                           "若反复失败，检查是否有其他 VPN 软件占用了网卡驱动。"
                           % int(timeout))

    def request_stop(self):
        """**同步**写出停止信号文件（瞬时完成，不等看门狗收尾）。

        用途：Yuhub 退出（关窗口 / 托盘退出）时调用。**不能**用 daemon
        线程去调 stop()——主进程退出会立刻杀掉 daemon 线程，"写停止信号"
        这一步都可能还没执行，看门狗收不到信号、easytier-core 就永久残留
        （下次打开 Yuhub 会看到"已有一个房间正在运行"）。
        纯文件写入是同步的、微秒级完成，退出路径上一定跑得完。
        """
        try:
            os.makedirs(os.path.dirname(_STOP_FILE), exist_ok=True)
            with open(_STOP_FILE, "w") as f:
                f.write("stop")
            self._base_fingerprint = None
            return True
        except OSError:
            return False

    def wait_stopped(self, timeout=2.0):
        """等 easytier-core 退出，最多 timeout 秒。返回是否已退出。

        退出路径上给个短预算（例如 2 秒）——看门狗轮询间隔 500ms，
        正常情况 1 秒内就收尾了；等不到也**不影响清理**，看门狗会继续
        处理（它是独立进程，不随 Yuhub 退出而消失）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not core_running():
                return True
            time.sleep(0.3)
        return False

    def stop(self):
        """停止跨网网络。写停止信号，看门狗负责清理。零 UAC 弹窗。"""
        with self._lock:
            if not core_running():
                return True, "没有在运行的跨网房间"
            self.request_stop()
            # 等看门狗干活
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if not core_running():
                    self._base_fingerprint = None
                    return True, "已停止"
                time.sleep(0.4)
            # 看门狗没在 8 秒内收掉，直接强杀兜底
            force_kill_core()
            self._base_fingerprint = None
            return True, "已停止（强制清理）"

    def network_changed(self):
        """检测运行期间物理网络是否切换（WiFi→热点 / 换 WiFi）。

        返回 True 表示网络已变、虚拟局域网大概率已失效，应自动关闭。
        仅在启动时成功记录了基线指纹的情况下判断；基线为空则视为未变化。
        """
        if self._base_fingerprint is None:
            return False
        now = _physical_network_fingerprint()
        if now is None:
            return False
        # 网关变了或物理 IP 集合变了，都算切换网络
        return now != self._base_fingerprint

    def status(self):
        """给 UI 轮询的快照。"""
        ip = virtual_adapter_ip()
        return {
            "running": core_running(),
            "ip": ip,
            "uptime": time.monotonic() - self._started_at if self._started_at else 0,
        }

