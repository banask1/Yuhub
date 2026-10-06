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

**本程序以管理员身份启动**（PyInstaller 的 `uac_admin=True` → 生成的 exe
清单里写死 `requireAdministrator`，与 Steam++ 的 app.manifest 同一形态）。
所以正常运行时 `is_elevated()` 恒为 True，hosts / 卸载残留 / 内存优化这些
需要权限的操作都在**同一个已提权进程**里完成，全程只弹一次 UAC（启动那次）。
`is_elevated()` 仍要留着并保持正确：源码态调试、以及未来万一改回非提权启动，
判据都不能错。
"""

import ctypes
import os
import sys

IS_WIN = sys.platform.startswith("win")

_TOKEN_QUERY = 0x0008        # TOKEN_QUERY
_TOKEN_ELEVATION = 20        # TokenElevation


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
