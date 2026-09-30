"""开机自启动：写 HKCU\\...\\Run 注册表项。

**纯标准库，不依赖 Qt**——这样能脱离 GUI 独立测试，也避免为了让设置页可用
而把整个界面栈拖进测试。

为什么用注册表而不是「启动」文件夹放快捷方式：
  1. 不用造 .lnk（免去 COM / IShellLink 那一串）；开关就是增删一个值。
  2. 用户能在「任务管理器 → 启动应用」里看到并手动禁用——Windows 读的就是这里。
     自己塞启动文件夹的 .lnk 同样能显示，但注册表更直接、更好排查。
  3. HKCU 而非 HKLM：只影响当前用户，不需要管理员权限。
     （要求管理员权限的自启方案是没法"点一下就开"的。）

**路径必须整段加引号**：程序若装在 `C:\\Program Files\\...` 这类含空格的目录，
不带引号会被拆成多个参数，开机时静默启动失败——不报错、不留痕迹，
用户只会觉得"这个自启时灵时不灵"。这是这类功能最常见的坑。

注意源码运行与打包运行要生成**不同**的命令：
  打包后  sys.executable 就是 exe 本身 → `"C:\\...\\Yuhub.exe" --minimized`
  源码运行 sys.executable 是 python.exe → 要用同目录的 pythonw.exe 才不开黑框，
          并且必须显式带上 main.py 的**绝对路径**（开机时工作目录是 system32，
          相对路径必挂）。
"""

import os
import sys

# 自启动时带上这个参数 → 启动后直接静默驻留托盘，不弹大窗口打扰用户
MINIMIZED_FLAG = "--minimized"

# HKCU 下的自启项（Windows 开机的「启动应用」列表就读这里）
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# 注册表里的值名。卸载/关闭时按它删除。
VALUE_NAME = "Yuhub"


def is_supported():
    """当前系统能否设置开机自启（本实现只覆盖 Windows）。"""
    return os.name == "nt"


def _main_script():
    """源码运行时 main.py 的绝对路径（按本模块位置反推，不依赖工作目录）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")


def launch_command(minimized=True):
    """开机自启要执行的命令行。

    minimized=True 时追加 `--minimized`，让程序开机后安静地驻留托盘。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：sys.executable 指向 exe 自身
        cmd = f'"{sys.executable}"'
    else:
        py = sys.executable or "python"
        # 源码运行时优先用 pythonw.exe：它不带控制台，不会闪一个黑框
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(pyw):
            py = pyw
        cmd = f'"{py}" "{_main_script()}"'
    if minimized:
        cmd += " " + MINIMIZED_FLAG
    return cmd


def registered_command(name=VALUE_NAME):
    """读注册表里现存的命令；没有该项则返回空串。"""
    if not is_supported():
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
        return value or ""
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def is_enabled(name=VALUE_NAME):
    """当前是否已登记开机自启。

    以「注册表里有没有这个值」为准，而不是以某个本地配置开关为准——
    用户可能在任务管理器里单独禁用了它，那界面就该如实反映。
    """
    return bool(registered_command(name))


def enable(name=VALUE_NAME, command=None):
    """登记开机自启。返回 (是否成功, 命令或错误说明)。"""
    if not is_supported():
        return False, "当前系统不支持（仅 Windows）"
    cmd = command or launch_command()
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, cmd)
        return True, cmd
    except OSError as exc:
        return False, f"写入注册表失败：{exc}"


def disable(name=VALUE_NAME):
    """取消开机自启。返回 (是否成功, 错误说明)。

    「本来就没有」不算失败——用户的目标状态（不启动）已经达成，
    这里把 FileNotFoundError 当成功处理，否则重复点关闭会弹无意义的错误。
    """
    if not is_supported():
        return False, "当前系统不支持（仅 Windows）"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True, ""
    except FileNotFoundError:
        return True, ""
    except OSError as exc:
        return False, f"删除注册表项失败：{exc}"


def set_enabled(flag, name=VALUE_NAME, command=None):
    """按目标状态开关自启，返回 (是否成功, 说明)。"""
    return enable(name, command) if flag else disable(name)


def program_of(cmd):
    """从命令行里取出「程序路径」部分，用于比对是否指向同一个可执行文件。

    取第一个被双引号包起来的片段；没有引号就取第一个空格前的 token。
    比对的目的是发现「程序被移动过、注册表里还指着老路径」这种自启静默失效。
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return ""
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end > 0:
            return cmd[1:end]
    return cmd.split(" ")[0]


def same_program(a, b):
    """两个命令行是否指向同一个程序（忽略大小写与路径分隔符差异）。"""
    pa, pb = program_of(a), program_of(b)
    if not pa or not pb:
        return False
    norm = lambda p: os.path.normcase(os.path.normpath(p))  # noqa: E731
    return norm(pa) == norm(pb)
