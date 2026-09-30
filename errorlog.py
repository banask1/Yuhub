"""隐藏的错误日志：软件出错或崩溃时，静默把日志写进 文档\\Yuhub\\error\\。

设计原则（对应"隐藏、后台运行"的需求）：
    * 零 UI、零提示、零弹窗——装好后用户完全无感；
    * 纯标准库，必须在任何 Qt 对象创建之前就能工作；
    * 不 import 本项目其它模块（ui 包会拉起 Qt）， documents 目录的
      解析逻辑从 ui/theme_packs.py 复制了一份（十几行，换取独立性）。

覆盖三层故障：

    1. sys.excepthook          主线程未捕获的 Python 异常
    2. threading.excepthook    后台线程未捕获的 Python 异常
       （Yuhub 大量使用后台线程；线程异常默认只打到 stderr，
        GUI 程序根本没有 stderr，用户什么都看不到）
    3. faulthandler            **硬崩溃**（Qt/C++ 层的访问违例、段错误）
       Python 的 excepthook 抓不到这类崩溃——进程直接被系统撕掉。
       faulthandler 是标准库，enable 后遇到致命信号会把当时所有线程的
       Python 调用栈 dump 到指定文件。

文件策略：
    * 每次出错写一个独立文件：crash_YYYYMMDD_HHMMSS.log
      （不往单个大文件里追加，用户好挑最近一份发回来）
    * 目录里最多保留 MAX_LOGS 份，超了自动删最老的，避免无限膨胀
    * 写日志本身包在 try/except 里——日志系统绝不能再引发二次崩溃
"""

import faulthandler
import os
import sys
import threading
import time
import traceback
import winreg

MAX_LOGS = 20                 # error 目录里最多保留的日志份数
_dir_lock = threading.Lock()
_fault_file = None            # faulthandler 的常驻句柄（必须全局持有防 GC）


def _documents_dir():
    """用户的「文档」目录。优先读注册表（OneDrive 重定向也能拿对），
    失败回落 %USERPROFILE%\\Documents。与 ui/theme_packs 同逻辑。"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion"
            r"\Explorer\User Shell Folders")
        val, _ = winreg.QueryValueEx(key, "Personal")
        winreg.CloseKey(key)
        if val:
            val = os.path.expandvars(val)
            if os.path.isdir(val):
                return val
    except Exception:
        pass
    env = os.environ.get("USERPROFILE", "")
    d = os.path.join(env, "Documents")
    return d if os.path.isdir(d) else env


def log_dir():
    """错误日志目录：文档\\Yuhub\\error。"""
    return os.path.join(_documents_dir(), "Yuhub", "error")


def install():
    """安装全部三层钩子。在 QApplication 创建之前调用；可重复调用。"""
    global _fault_file
    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        # faulthandler：常驻打开一个日志文件，硬崩溃时把所有线程的栈
        # dump 进去。句柄必须一直持有（模块级引用），否则被 GC 关掉。
        fatal_path = os.path.join(d, "crash_fatal.log")
        try:
            # append 模式会跨会话越写越大：超过 5MB 就重开（丢旧保新）
            if os.path.exists(fatal_path) and os.path.getsize(fatal_path) > 5_000_000:
                os.remove(fatal_path)
        except OSError:
            pass
        _fault_file = open(fatal_path, "ab", buffering=0)
        faulthandler.enable(file=_fault_file)
    except Exception:
        pass                        # 目录建不出来（权限等）→ 静默放弃
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


def _excepthook(exc_type, exc_value, exc_tb):
    """主线程未捕获异常的落点。"""
    try:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _write("uncaught_exception", "MainThread", text)
    except Exception:
        pass
    # 链回默认行为（控制台态打印 traceback），绝不吞掉原有语义
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _thread_excepthook(args):
    """后台线程未捕获异常的落点（threading.excepthook 是 3.8+）。"""
    try:
        exc_type = args.exc_type
        exc_value = args.exc_value
        exc_tb = args.exc_traceback
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        thread = args.thread
        name = getattr(thread, "name", "") or "unnamed"
        _write("thread_exception", name, text)
    except Exception:
        pass


def _write(kind, where, text):
    """写一份独立日志 + 清理旧文件。任何一步失败都静默。"""
    with _dir_lock:
        try:
            d = log_dir()
            os.makedirs(d, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(d, "crash_%s.log" % stamp)
            # 同一秒内第二次出错：换名字，别把第一份覆盖掉
            n = 1
            while os.path.exists(path):
                n += 1
                path = os.path.join(d, "crash_%s_%d.log" % (stamp, n))
            from ui import VERSION_LABEL
            header = (
                "Yuhub 错误日志\n"
                "时间: %s\n"
                "类型: %s  (线程: %s)\n"
                "版本: %s  打包: %s\n"
                "Python: %s\n"
                "----------------------------------------\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"),
                   kind, where,
                   VERSION_LABEL,
                   "exe" if getattr(sys, "frozen", False) else "source",
                   sys.version.split()[0])
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(header)
                f.write(text.rstrip() + "\n")
            _cleanup(d)
        except Exception:
            pass


def _cleanup(d, keep=MAX_LOGS):
    """只留最近 keep 份 crash_*.log（crash_fatal.log 不占名额、不删）。"""
    try:
        files = [os.path.join(d, n) for n in os.listdir(d)
                 if n.startswith("crash_") and n.endswith(".log")
                 and n != "crash_fatal.log"]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception:
        pass
