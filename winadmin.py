"""进程提权状态判定（纯 ctypes，无第三方依赖）。

⚠️ **不能用 `shell32.IsUserAnAdmin()`** —— 它的语义是"当前用户**是不是
Administrators 组成员**"，而不是"当前进程**是否已提权**"。在"账户是管理员 +
UAC 开着 + 进程未提权"的机器上它照样返回 1（本机实测：`IsUserAnAdmin() == 1`
而 `TokenElevation == 0`）。拿它决定"要不要弹 UAC / 能不能直写"，结果是：

    程序以为"我已经有权限了，直接写" → 真正的写盘/删除动作拿不到权限
    → WinError 5「拒绝访问」→ 用户看到"明明有权限却提示权限不够""hosts
      未被改动""有权限也删不掉残留"。

正确做法是 `GetTokenInformation(TokenElevation)`：直接问内核"这个令牌是不是
提权令牌"，与 UAC 的真实行为一一对应。拿不到就**保守当作未提权**（多走一次
提权没有代价 —— 进程本已提权时 runas 不会再弹窗；反过来误判成"有权限"则会
让写盘直接失败）。
"""

import ctypes
import os
import sys

IS_WIN = sys.platform.startswith("win")

_TOKEN_QUERY = 0x0008        # TOKEN_QUERY
_TOKEN_ELEVATION = 20        # TokenElevation

#: 「以管理员身份重启」时自带的标志。新实例看到它就**不去敲旧实例的门**，
#: 而是请旧实例让位、自己接管单实例管道（见 single_instance.acquire_takeover）。
#: 定义在这里是为了让 UI 与 main.py 共用同一个字面量。
RELAUNCH_FLAG = "--relaunch-elevated"


class _TOKEN_ELEVATION(ctypes.Structure):        # noqa: N801
    _fields_ = [("TokenIsElevated", ctypes.c_ulong)]


def _token_elevated():
    """GetTokenInformation(TokenElevation)。API 不可用/失败返回 None。"""
    try:
        k32 = ctypes.windll.kernel32
        a32 = ctypes.windll.advapi32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        a32.OpenProcessToken.restype = ctypes.c_int
        a32.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.POINTER(ctypes.c_void_p)]
        a32.GetTokenInformation.restype = ctypes.c_int
        a32.GetTokenInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32)]

        handle = ctypes.c_void_p()
        if not a32.OpenProcessToken(k32.GetCurrentProcess(), _TOKEN_QUERY,
                                    ctypes.byref(handle)):
            return None
        try:
            val = _TOKEN_ELEVATION()
            need = ctypes.c_uint32()
            if not a32.GetTokenInformation(
                    handle, _TOKEN_ELEVATION, ctypes.byref(val),
                    ctypes.sizeof(val), ctypes.byref(need)):
                return None
            return bool(val.TokenIsElevated)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def is_elevated():
    """当前进程是否已提权（管理员权限）。

    非 Windows：euid == 0。
    Windows：TokenElevation；拿不到时保守返回 False（宁可多走一次提权，
    也不要在没权限的时候自以为有权限）。
    """
    if not IS_WIN:
        try:
            return os.geteuid() == 0
        except Exception:
            return False
    got = _token_elevated()
    return bool(got) if got is not None else False


def group_admin_but_not_elevated():
    """诊断用：账户在管理员组、但进程没提权（就是那个坑）。

    返回 True 表示"旧判据 IsUserAnAdmin 会误报有权限"。自检拿它证明
    「不能再用 IsUserAnAdmin」，也让现场问题一眼可辨。
    """
    if not IS_WIN:
        return False
    try:
        by_group = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
    return by_group and not is_elevated()


# ---------------------------------------------------------------------------
# 以管理员身份重新拉起自己（对标 Steam++ 的 requireAdministrator）
#
# SteamTools 的主程序清单里写死了
#     <requestedExecutionLevel level="requireAdministrator" />
# 也就是**整个程序从启动就常驻管理员**。这一点很关键：它的 hosts 写入永远
# 发生在**同一个已提权进程**里 —— UAC 只弹一次（启动那次），杀软（火绒/360）
# 也只需要放行同一个程序一次。
#
# 我们原来不是这样：普通权限启动，每次拨开关都 `runas` 拉一个**新的提权
# 子进程**去写盘。后果有两层：
#   ① 每次操作都弹一次 UAC（用户感知："这软件怎么老弹"）；
#   ② 写盘的是另一个进程实例，HIPS 类软件会把它当"新来的"重新审视，
#      拦一次我们就拿到 WinError 5 —— 用户看到"提示 hosts 未被改动"。
#
# 所以补上这个开关：用户可以选择"以管理员身份重启"，重启后本进程就是
# 管理员，`apply_intent` 直接走同进程写盘（try_write_direct），
# 不再弹 UAC、不再换进程。这就是 Steam++ 的形态。
# ---------------------------------------------------------------------------
def _quote_args(args):
    """把参数列表拼成命令行（含空格的加引号）。"""
    out = []
    for a in args:
        a = str(a)
        if not a:
            continue
        out.append('"%s"' % a if (" " in a or "\t" in a) else a)
    return " ".join(out)


def self_command(extra=()):
    """拼出"重新拉起本程序"的命令行，返回 (可执行文件, 参数串)。

    打包态（frozen）：exe 就是自己，参数只有 extra。
    源码态：python.exe + main.py 绝对路径 + extra —— 开发时也能用这个功能。
    """
    extra = [str(a) for a in extra if str(a)]
    if getattr(sys, "frozen", False):
        return sys.executable, _quote_args(extra)
    script = ""
    try:
        import __main__
        script = getattr(__main__, "__file__", "") or ""
    except Exception:
        script = ""
    if not script or not os.path.isfile(script):
        # 退一步用 argv[0]（`python main.py` 时它就是 main.py）
        cand = sys.argv[0] if sys.argv else ""
        if cand.endswith(".py") and os.path.isfile(cand):
            script = os.path.abspath(cand)
    if script:
        return sys.executable, _quote_args([script] + extra)
    return sys.executable, _quote_args(extra)


def shell_execute_runas(executable, args="", cwd=None, show=1):
    """以管理员身份启动 executable（弹 UAC）。

    返回 True 表示用户同意、进程已创建；False 表示被取消或被策略拒绝。

    ⚠️ **必须显式声明 restype = c_void_p**：`ShellExecuteW` 返回的是
    `HINSTANCE`（指针宽度），ctypes.windll 默认按 `c_long`（32 位）取，
    高位非零的句柄会被截断成负数/小值，于是下游 `rc <= 32` 把"成功"
    误判成"用户取消了 UAC"（v1.0.2 的"开关弹回去"就是这么来的）。
    """
    if not IS_WIN:
        return False
    try:
        fn = ctypes.windll.shell32.ShellExecuteW
        fn.restype = ctypes.c_void_p
        fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                       ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        rc = fn(None, "runas", str(executable), args or "",
                cwd, int(show))
    except Exception:
        return False
    # NULL(0) / ≤32 = 失败（ShellExecute 的返回码约定）
    return bool(rc) and int(rc) > 32


def relaunch_as_admin(extra=(), cwd=None):
    """把本程序以管理员身份重新启动（UAC）。返回 True=用户同意了。

    调用方拿到 True 之后应当**退出自己**，把单实例管道让给新进程
    （见 single_instance.SingleInstance.acquire_takeover）。
    """
    exe, args = self_command(extra)
    return shell_execute_runas(exe, args, cwd=cwd)
