"""智能应用控制（Smart App Control, SAC）开关。

SAC 是 Windows 11 的应用执行策略：开启时只允许"有可信签名 / 有良好声誉"的
程序运行；未签名程序会被直接拦截，而且**没有"仍要运行"、也没有按应用例外**。

Windows 没有提供公开的 API 开关，微软官方给出的管理方式是"改注册表 + 刷新
策略"（Microsoft Learn: Windows Defender Application Control）：

    HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy
        VerifiedAndReputablePolicyState   (REG_DWORD)
            0 = 关闭    1 = 施行（开启）    2 = 评估

写入后必须执行 ``%SystemRoot%\\System32\\CiTool.exe -r`` 刷新策略；部分机器
需要重启才完全生效。

三条硬约束（必须如实告诉用户，不能省）：

1. **只有 Windows 11（新装系统）才有 SAC**。Windows 10 / 部分升级机上没有
   这个键。此时**绝不能** SetValueEx —— 那等于凭空造一个策略状态。
2. **关闭会降低整机防护**；老版本 Windows 11 关闭后**无法直接重新开启**
   （要重装 / 重置系统）。较新版本可在同一设置页重新打开。
3. 改 HKLM 需要管理员权限。Yuhub 的 exe 清单是 ``requireAdministrator``，
   正常运行时已具备。

本模块纯标准库 + winreg，**不依赖 Qt**，便于独立测试。
"""

from __future__ import annotations

import os
import subprocess

IS_WIN = os.name == "nt"

#: SAC 策略键（64 位视图）
KEY_PATH = r"SYSTEM\CurrentControlSet\Control\CI\Policy"
VALUE_NAME = "VerifiedAndReputablePolicyState"

STATE_OFF = 0       # 关闭
STATE_ON = 1        # 施行（保护中）
STATE_EVAL = 2      # 评估

STATE_NAMES = {
    STATE_OFF: "已关闭",
    STATE_ON: "已开启（保护中）",
    STATE_EVAL: "评估模式",
}

#: read_state_ex 的失败分类。**UI 必须按 kind 分流，不要按 err 文本匹配** ——
#: 文案会改，分类不会。v1.2.1 之前 UI 用 `"管理员" in err` 分流，把
#: OSError 读取失败和"键不存在"混成一类，提示栏对读取失败的机器谎称
#: "本机没有 SAC"，写入后回读失败时还会把开关永久置灰。
KIND_OK = "ok"           # 读到了状态
KIND_ABSENT = "absent"   # 键/值不存在：本机没有 SAC（Win10 / 升级机）
KIND_DENIED = "denied"   # 权限不足：需要管理员
KIND_ERROR = "error"     # 其它读取失败（**≠ 没有 SAC**）

# Win32 CREATE_NO_WINDOW：别让 CiTool 弹控制台窗口
_CREATE_NO_WINDOW = 0x08000000


def _winreg():
    import winreg
    return winreg


def _open_flags(write=False):
    """64 位注册表视图 + 读/写权限。

    Yuhub 是 64 位进程，默认就在 64 位视图；显式带 ``KEY_WOW64_64KEY`` 是
    为了万一以后出 32 位构建也能改到真正的键 —— 否则会落到 Wow6432Node
    下的影子键，界面显示成功、SAC 纹丝不动（典型静默失效）。
    """
    wr = _winreg()
    flags = wr.KEY_SET_VALUE if write else wr.KEY_READ
    return flags | getattr(wr, "KEY_WOW64_64KEY", 0)


def read_state_ex():
    """读取 SAC 当前状态，带失败分类。

    返回 ``(state, error, kind)``，kind 取 KIND_* 常量：

    * ``state`` 为 0/1/2 时 ``kind == KIND_OK``、``error`` 为空串；
    * ``state`` 为 None 时 ``kind`` 说明**为什么**读不到，``error`` 给人看。

    **不抛异常**：所有失败都变成可读的 error 字符串，界面直接展示。
    """
    if not IS_WIN:
        return None, "仅支持 Windows", KIND_ERROR
    try:
        wr = _winreg()
        with wr.OpenKey(wr.HKEY_LOCAL_MACHINE, KEY_PATH, 0, _open_flags()) as k:
            val, _typ = wr.QueryValueEx(k, VALUE_NAME)
    except FileNotFoundError:
        return (None, "本机没有智能应用控制（仅 Windows 11 新装系统才有）",
                KIND_ABSENT)
    except PermissionError:
        return None, "读取注册表需要管理员权限", KIND_DENIED
    except OSError as exc:
        return None, "读取失败：%s" % exc, KIND_ERROR
    try:
        return int(val), "", KIND_OK
    except (TypeError, ValueError):
        return None, "注册表值类型异常：%r" % (val,), KIND_ERROR


def read_state():
    """读取 SAC 当前状态（二值旧接口）。

    ``read_state_ex`` 的便捷包装。需要区分「没有 SAC / 需要管理员 /
    读取失败」三种情况的调用方（设置页 UI）**必须**用 ``read_state_ex``
    —— 二值接口把三种失败压成一个 error 字符串，UI 只能靠文本匹配猜，
    而"读取失败"和"没有 SAC"给的提示完全是两码事。
    """
    state, err, _kind = read_state_ex()
    return state, err


def is_supported():
    """本机是否真的有 SAC（值存在且读得到）。"""
    return read_state()[0] is not None


def citool_path():
    """CiTool.exe 的绝对路径（System32）。"""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return os.path.join(root, "System32", "CiTool.exe")


def _refresh_policy(timeout=20):
    """执行 ``CiTool.exe -r`` 让策略立即重载。返回 ``(ok, msg)``。"""
    exe = citool_path()
    if not os.path.isfile(exe):
        return False, "未找到 CiTool.exe，需重启后生效"
    try:
        proc = subprocess.run(
            [exe, "-r"],
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "刷新策略超时，需重启后生效"
    except Exception as exc:                                   # noqa: BLE001
        return False, "刷新策略失败：%s" % exc
    out = (proc.stdout or b"") + (proc.stderr or b"")
    text = out.decode("utf-8", "replace").strip()
    if proc.returncode == 0:
        return True, text or "策略已刷新"
    return False, text or ("CiTool 返回 %d，需重启后生效" % proc.returncode)


def set_state(state, refresh=True):
    """把 SAC 设成指定状态。返回 ``(ok, msg)``。

    ⚠️ 只是"尽力而为"：写入成功不代表策略立即生效（可能要重启），
    调用方应重新 ``read_state()`` 校验，并把 msg 如实展示。

    关键前置：先 ``read_state()`` 确认值**本来就存在**。不存在（Win10 /
    没有 SAC 的机器）时直接拒绝 —— 绝不 SetValueEx 造一个新值。
    """
    if state not in (STATE_OFF, STATE_ON, STATE_EVAL):
        return False, "非法状态：%r" % (state,)
    if not IS_WIN:
        return False, "仅支持 Windows"

    cur, err = read_state()
    if cur is None:
        return False, err or "无法读取当前状态"

    try:
        wr = _winreg()
        with wr.OpenKey(wr.HKEY_LOCAL_MACHINE, KEY_PATH, 0,
                        _open_flags(write=True)) as k:
            wr.SetValueEx(k, VALUE_NAME, 0, wr.REG_DWORD, int(state))
    except FileNotFoundError:
        return False, "本机没有智能应用控制（仅 Windows 11 新装系统才有）"
    except PermissionError:
        return False, "需要管理员权限才能修改（请以管理员身份运行 Yuhub）"
    except OSError as exc:
        return False, "写入注册表失败：%s" % exc

    if not refresh:
        return True, "已写入，重启后生效"
    ok, msg = _refresh_policy()
    if ok:
        return True, msg
    return True, "已写入；%s" % msg


def state_text(state):
    """状态 → 中文说明。"""
    return STATE_NAMES.get(state, "未知")


def disable(refresh=True):
    """关闭 SAC。"""
    return set_state(STATE_OFF, refresh=refresh)


def enable(refresh=True):
    """重新开启 SAC（老版 Win11 可能无效，调用方要如实回报）。"""
    return set_state(STATE_ON, refresh=refresh)
