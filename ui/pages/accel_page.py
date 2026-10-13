"""网络加速页 —— 布局对标 Steam++（Watt Toolkit）的「网络加速」。

## 这一版是什么（先把边界说清楚）

只有界面骨架，还没有加速能力。 上一版（1.0.6 及以前）的加速是"纯 Python
SNI 透传 + hosts"，已在 1.0.7 里整体移除；新的加速由独立的、按需下载的框架
承担（.NET/YARP + Native AOT，实测单文件 16.24 MB、无需 .NET 运行时）。

所以本页现在的职责是：
  1. 把「加速哪些平台」这件事做成可操作的界面，选择结果持久化；
  2. 如实显示框架是否已安装，并给出安装入口 —— 没装框架时"一键加速"不可用；
  3. 结构对齐 Steam++：顶部工具条（一键加速 / 刷新 / 加速设置）+ 平台列表
     （勾选框 + 图标 + 名称 + 展开箭头）。

## 平台清单的来源（不是编的）

来自两处实测：
  · 用户提供的 Steam++「网络加速」页截图（Steam 服务 / Discord 语音聊天 /
    Twitch 直播 / Origin / Uplay / 公共 CDN / 国外验证码平台 / Github）；
  · 从本机 `%LOCALAPPDATA%\\Steam++\\Plugins\\Accelerator\\LOCAL_ACCELERATE.json`
    里解出的中文条目名（服务 / 商店 / 社区 / 图片 / 静态资源 / 更新 / 讨论 /
    留言 / 解锁访问限制 / 直播 / 掉宝 / 聊天 / 视频封面加载 / 大文件 /
    第三方关联 / 网页加载 / 游戏下载 / 翻译 / 图床 …）。

⚠️ 权威清单在 Steam++ 服务端下发，本地只有混淆后的缓存，所以这里的
`items` 是"已知条目"的整理，不保证与官方逐条一致。将来框架从真实规则集
（或用户导出）加载后，本页应改为由规则集驱动，而不是写死。
"""

import json
import os
import shutil
import subprocess
import sys

from PySide6.QtCore import QByteArray, QPointF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from .. import theme
from ..widgets import ghost_button, info_card, primary_button
from .base_page import BasePage

#: 加速框架的落点（框架随本体**内嵌**，运行时解包到这里的稳定路径 ——
#: 不能直接跑 _MEIPASS 里的临时文件：程序一退出就被清掉，serve 也活不过本体）。
_FRAMEWORK_DIR = r"%LOCALAPPDATA%\Yuhub\accel"
_FRAMEWORK_EXE = "YuhubAccel.exe"
#: 实测体积（win-x64 / Native AOT 单文件，含 YARP + HTTPS），用于文案里的预期管理
_FRAMEWORK_SIZE = "11.1 MB"

#: 设置键：用户勾选的平台
_SETTINGS_KEY = "accel/services"

#: 起框架时不要闪黑窗（与 main.py / cleaner.py 同一约定）
_CREATE_NO_WINDOW = 0x08000000

#: 加速内核（serve）的 pid 文件与运行日志 —— 框架启动时写、退出时删
_PID_NAME = "serve.pid"
_LOG_NAME = "serve.log"
#: 「hosts 已被改写」的脏标记。**必须与框架 Hosts.DirtyFlagPath 同名同目录**
#: （框架写、Yuhub 读；改单边等于这条防线直接失效）。
_DIRTY_NAME = "hosts_dirty"
#: hosts 里本框架标记块的开头（判"hosts 到底写进去没有"）
_HOSTS_BEGIN = "# >>> YuhubAccel v1 >>>"


def _process_alive(pid):
    """Windows 上判断进程还活着（OpenProcess + GetExitCodeProcess）。"""
    import ctypes
    k32 = ctypes.windll.kernel32
    # 🔴 必须显式声明签名：不声明时 64 位句柄会被截成 32 位 c_int，
    #    运气差时句柄坏了 → GetExitCodeProcess 失败 → 活进程被判死。
    k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    k32.GetExitCodeProcess.restype = ctypes.c_int
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_uint32()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == 259                  # STILL_ACTIVE
        return False
    finally:
        k32.CloseHandle(h)


def _terminate_pid(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    h = k32.OpenProcess(0x0001, False, pid)           # PROCESS_TERMINATE
    if h:
        k32.TerminateProcess(h, 1)
        k32.CloseHandle(h)


# ---------------------------------------------------------------------------
# 真品牌图标（Simple Icons，CC0；商标归各自所有者，此处仅作指明性使用）
#
# 图标是**单色 SVG 路径**，所以做法是"渲染成位图 → 用 SourceIn 染成白色"，
# 稳稳地压在品牌色块上 —— 深色品牌（Steam 近黑）在深色主题里也不会消失。
# 找不到文件就退回 emoji，绝不因为缺个图标把整页弄崩。
# ---------------------------------------------------------------------------
def _resource_path(rel):
    """兼容源码运行与 PyInstaller 打包后的资源路径。"""
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.dirname(
                       os.path.dirname(os.path.abspath(__file__)))))
    return os.path.join(base, rel)


def _embedded_framework_path():
    """内嵌在本体里的框架 exe 的路径。

    打包态：spec 把它收进了 datas，落在 `_MEIPASS/accel/YuhubAccel.exe`；
    源码态：直接用仓库里 dotnet publish 的产物（accel-framework/out/）。
    找不到返回 None（此时才退回手动选文件的兜底）。
    """
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        p = os.path.join(sys._MEIPASS, "accel", _FRAMEWORK_EXE)
    else:
        root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        p = os.path.join(root, "accel-framework", "out", _FRAMEWORK_EXE)
    return p if os.path.isfile(p) else None


# ---------------------------------------------------------------------------
# 加速框架：落点 / 状态 / 安装 / 修复（模块级 —— 加速页与设置页共用）
# ---------------------------------------------------------------------------
def framework_dir():
    return os.path.expandvars(_FRAMEWORK_DIR)


def framework_path():
    return os.path.join(framework_dir(), _FRAMEWORK_EXE)


def framework_installed():
    """落点里是否已有一份可用的框架 exe。"""
    try:
        p = framework_path()
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False


def run_framework(args, timeout=10):
    """跑框架的一次性命令。返回 (rc, stdout, stderr)；框架不在则 rc=-1。"""
    p = framework_path()
    if not os.path.isfile(p):
        return (-1, "", "框架未安装")
    try:
        r = subprocess.run(
            [p] + list(args), capture_output=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW)
        return (r.returncode,
                r.stdout.decode("utf-8", "replace"),
                r.stderr.decode("utf-8", "replace"))
    except Exception as exc:
        return (-1, "", str(exc))


def framework_status():
    """`status --json` 解析成 dict（ca / thumbprint / trusted / expires_in_days）。

    拿不到（框架缺失、输出不合法）返回 None —— 调用方必须能区分
    "没读出来"和"读出来是未信任"。
    """
    _rc, out, _err = run_framework(["status", "--json"], timeout=8)
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 脏退自愈：上次加速没走完收尾 → 域名还指向一个没人监听的 127.0.0.1
#
# 🔴 这是"别的电脑上加速坏了、网页全打不开"的最后一层防线。
#   框架写 hosts 之后、内核真正监听之前，任何一步暴死（AOT fail-fast /
#   被强杀）都来不及跑自己的 finally，而残留的后果最严重：**所有加速域名被
#   指向 127.0.0.1 的 443，那里没人监听** —— 浏览器、Steam 直接打不开页面。
#   框架因此会在改写 hosts 时落一枚 `hosts_dirty` 标记、还原成功才抹掉；
#   这里负责"发现标记还在 + 内核没在跑"时替它收拾干净，并如实说一句人话。
# ---------------------------------------------------------------------------
def dirty_flag_path():
    return os.path.join(framework_dir(), _DIRTY_NAME)


def hosts_dirty():
    try:
        return os.path.isfile(dirty_flag_path())
    except OSError:
        return False


def _hosts_text_path():
    # 口径必须与框架 Hosts.Path 一致：%SystemRoot%\**System32**\drivers\etc\hosts
    # （Environment.SpecialFolder.System 就是 System32，别漏这一层 ——
    #   少写一层会得到一个不存在的路径，判据永远返回"读不到 hosts"）
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(root, "System32", "drivers", "etc", "hosts")


def hosts_has_block():
    """hosts 里有没有本框架的标记块。返回 (bool, 说明)。

    "内核在跑" 与 "hosts 真的写进去了" 是两件事：内核起来了但 hosts 没落地，
    流量根本不会经过它 —— 界面上一切正常、实际一点没加速。这条判据就是用来
    把这种静默失效挡在"加速已开启"这句文案之外的。读 hosts 不需要管理员。
    """
    try:
        with open(_hosts_text_path(), "r", encoding="utf-8",
                  errors="replace") as fh:
            return (_HOSTS_BEGIN in fh.read(), "标记块不存在")
    except OSError as exc:
        return (False, "读不到 hosts（%s）" % exc)


def recover_dirty_hosts():
    """把上次脏退留下的 hosts 还原回去。返回 (是否动过手, 人话说明)。

    ⚠️ 调用方必须先确认**内核确实没在跑** —— 内核活着时那份 hosts 正是它
    要的，清了反而把正常加速打断。
    """
    if not hosts_dirty():
        return (False, "")
    rc, out, err = run_framework(["hosts-clean"], timeout=20)
    lines = (out or err or "").strip().splitlines()
    tail = lines[-1] if lines else ""
    if rc == 0:
        try:
            os.remove(dirty_flag_path())
        except OSError:
            pass
        return (True, "上次加速异常退出，已自动还原 hosts"
                + ("（%s）" % tail if tail else ""))
    if rc == -1:
        return (True, "上次加速异常退出，但加速框架不在，hosts 未能还原：%s" % tail)
    return (True, "上次加速异常退出，hosts 还原失败（%s）—— 可点「一键修复」" % tail)


def clear_dirty_flag():
    try:
        os.remove(dirty_flag_path())
    except OSError:
        pass


def framework_running_by_name():
    """按进程名判加速内核在不在跑。

    为什么不能只看 pid 文件：pid 文件可能被安全软件/清垃圾工具删掉，而内核还
    活得好好的。判活的判据一旦漏掉这种情况，"脏退自愈"会在**内核正常运行**时
    去清 hosts，直接把正在用的加速打断 —— 那比不修还糟。
    """
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq " + _FRAMEWORK_EXE, "/NH"],
            capture_output=True, timeout=8, creationflags=_CREATE_NO_WINDOW)
        out = (r.stdout or b"").decode("utf-8", "replace").lower()
        return _FRAMEWORK_EXE.lower() in out
    except Exception:
        return False


def install_framework(progress=None):
    """把**内嵌**的框架写入落点并确保 CA 就绪。返回 (ok, 说明)。

    这是"安装"的权威实现：加速页与设置页的修复入口都走它，
    避免两处各写一份复制 + gen-ca 的顺序与报错文案。
    """
    src = _embedded_framework_path()
    if src is None:
        return False, "本体里没有内嵌加速框架，请重新安装 Yuhub"
    try:
        os.makedirs(framework_dir(), exist_ok=True)
        shutil.copy2(src, framework_path())
    except OSError as exc:
        return False, "写入失败：%s" % exc
    if progress:
        progress("正在生成本机根证书…")
    rc, out, err = run_framework(["gen-ca"], timeout=40)
    if rc != 0:
        return False, "框架已写入，但生成根证书失败：%s" % ((err or out).strip()[:120])
    return True, "框架已就位、根证书已生成"


def repair_framework(progress=None, should_cancel=None):
    """一键修复加速框架。

    修的是什么：
      ① 停掉可能残留的加速内核 —— 它在跑的时候 exe 被占用，覆盖写必然失败；
      ② 清掉 hosts 里的加速映射块 —— 否则文件修好了、流量还是被指到打不开的
         本地代理上，表现依旧是"加速坏了"；
      ③ 用本体里内嵌的那份覆盖写回落点，并做 sha256 校验；
      ④ gen-ca 重建/确认根证书；若证书被重建（指纹变了）而用户原本是信任
         状态，自动重新信任 —— 不然修完反而变成"证书不受信任"，比不修还差。

    返回 (ok, 人话说明)。progress(str) 用于回传进度。
    """
    def p(text):
        if progress and not (should_cancel and should_cancel()):
            progress(text)

    if not framework_installed():
        p("框架不在，正在安装…")
        return install_framework(progress=progress)

    src = _embedded_framework_path()
    if src is None:
        return False, "本体里没有内嵌加速框架，请重新安装 Yuhub"

    p("正在停止加速内核…")
    try:
        subprocess.run(["taskkill", "/IM", _FRAMEWORK_EXE, "/F"],
                       capture_output=True, timeout=15,
                       creationflags=_CREATE_NO_WINDOW)
    except Exception:
        pass

    p("正在清理 hosts 中的加速记录…")
    run_framework(["hosts-clean"], timeout=20)
    # 顺手把"脏退"标记抹掉：修复的语义就是"状态回到干净的起点"，
    # 标记留着会让下次进页面又报一遍"上次异常退出"。
    clear_dirty_flag()

    before = framework_status() or {}
    was_trusted = bool(before.get("trusted"))
    old_thumb = str(before.get("thumbprint") or "")

    p("正在重新写入加速框架文件…")
    try:
        src_hash = _sha256(src)
    except OSError as exc:
        return False, "读取内嵌框架失败：%s" % exc
    try:
        os.makedirs(framework_dir(), exist_ok=True)
        shutil.copy2(src, framework_path())
    except OSError as exc:
        return False, "写入失败（文件可能仍被占用，请关掉加速后重试）：%s" % exc
    try:
        if _sha256(framework_path()) != src_hash:
            return False, "写回后校验不一致，请重试"
    except OSError as exc:
        return False, "校验失败：%s" % exc

    p("正在重建根证书…")
    rc, out, err = run_framework(["gen-ca"], timeout=40)
    if rc != 0:
        return False, "框架文件已修复，但生成根证书失败：%s" % ((err or out).strip()[:120])

    after = framework_status() or {}
    new_thumb = str(after.get("thumbprint") or "")
    trusted = bool(after.get("trusted"))

    if was_trusted and not trusted and new_thumb and new_thumb != old_thumb:
        p("证书已重建，正在重新信任…")
        rc, _o, _e = run_framework(["install-ca"], timeout=40)
        trusted = rc == 0
        if not trusted:
            return True, "文件与证书已修复，但重新信任失败，请到网络加速页点「信任证书」"

    if not after.get("ca"):
        return True, "框架文件已重新写入并校验通过；证书状态未知，请到网络加速页确认"
    return True, "框架文件已重新写入并校验通过；根证书%s" % (
        "已信任" if trusted else "未信任（需要时到网络加速页点「信任证书」）")


_LOGO_CACHE = {}


def _logo_pixmap(name, size=18, color="#ffffff"):
    """把 `resources/logos/<name>.svg` 渲染成指定颜色的小位图。失败返回 None。"""
    key = (name, size, color)
    if key in _LOGO_CACHE:
        return _LOGO_CACHE[key]
    pm = None
    try:
        path = _resource_path(os.path.join("resources", "logos", name + ".svg"))
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                data = fh.read()
            renderer = QSvgRenderer(QByteArray(data))
            if renderer.isValid():
                pm = QPixmap(size, size)
                pm.fill(Qt.transparent)
                p = QPainter(pm)
                p.setRenderHint(QPainter.Antialiasing, True)
                renderer.render(p)
                # 染色：只保留 alpha，填成目标色
                p.setCompositionMode(QPainter.CompositionMode_SourceIn)
                p.fillRect(pm.rect(), QColor(color))
                p.end()
    except Exception:
        pm = None
    _LOGO_CACHE[key] = pm
    return pm


def _glyph_pixmap(kind, size=18, color="#ffffff"):
    """给"类别"项画通用矢量图标（它们不是单一品牌，不该套用某个 logo）。"""
    key = ("glyph:" + kind, size, color)
    if key in _LOGO_CACHE:
        return _LOGO_CACHE[key]
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color), max(1.4, size * 0.09))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    c = size / 2.0
    r = size * 0.42
    if kind == "globe":                      # 公共 CDN：地球
        p.drawEllipse(QPointF(c, c), r, r)
        p.drawEllipse(QPointF(c, c), r * 0.45, r)      # 经线
        p.drawLine(QPointF(c - r, c), QPointF(c + r, c))  # 赤道
    elif kind == "shield":                   # 验证码：盾牌 + 勾
        path = QPainterPath()
        path.moveTo(c, c - r)
        path.lineTo(c + r, c - r * 0.5)
        path.lineTo(c + r * 0.75, c + r * 0.6)
        path.lineTo(c, c + r)
        path.lineTo(c - r * 0.75, c + r * 0.6)
        path.lineTo(c - r, c - r * 0.5)
        path.closeSubpath()
        p.drawPath(path)
        p.drawPolyline(QPolygonF([QPointF(c - r * 0.34, c),
                                  QPointF(c - r * 0.06, c + r * 0.30),
                                  QPointF(c + r * 0.38, c - r * 0.30)]))
    elif kind == "image":                    # 图床：相框 + 山 + 太阳
        rect = QPointF(c - r, c - r * 0.82), QPointF(c + r, c + r * 0.82)
        p.drawRoundedRect(
            rect[0].x(), rect[0].y(),
            rect[1].x() - rect[0].x(), rect[1].y() - rect[0].y(),
            size * 0.14, size * 0.14)
        p.setBrush(QBrush(QColor(color)), )
        p.drawEllipse(QPointF(c - r * 0.34, c - r * 0.34), r * 0.16, r * 0.16)
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(QPolygonF([
            QPointF(c - r * 0.75, c + r * 0.62),
            QPointF(c - r * 0.10, c - r * 0.05),
            QPointF(c + r * 0.42, c + r * 0.62),
        ]))
    p.end()
    _LOGO_CACHE[key] = pm
    return pm


def _icon_pixmap(spec, size=18, color="#ffffff"):
    """优先真 logo → 其次通用矢量图标 → 都没有返回 None（调用方退回 emoji）。"""
    if spec.get("logo"):
        pm = _logo_pixmap(spec["logo"], size, color)
        if pm is not None:
            return pm
    if spec.get("glyph"):
        return _glyph_pixmap(spec["glyph"], size, color)
    return None

# ---------------------------------------------------------------------------
# 平台清单（详见模块头注释里说明的来源）
# ---------------------------------------------------------------------------
_SERVICES = [
    {
        "key": "steam", "name": "Steam 服务", "icon": "🎮", "tint": "tile_1",
        "logo": "steam",
        "desc": "商店、社区、登录、API、图片与下载 CDN",
        "items": ["商店", "社区", "图片", "静态资源", "更新", "讨论", "留言",
                  "解锁访问限制"],
    },
    {
        "key": "discord", "name": "Discord 语音聊天", "icon": "💬", "tint": "tile_7",
        "logo": "discord",
        "desc": "聊天、头像与静态资源加载",
        "items": ["聊天", "头像", "静态资源", "网页加载"],
    },
    {
        "key": "twitch", "name": "Twitch 直播", "icon": "📺", "tint": "tile_4",
        "logo": "twitch",
        "desc": "直播播放、掉宝与视频封面加载",
        "items": ["直播", "掉宝", "视频封面加载", "播放", "聊天"],
    },
    {
        "key": "origin", "name": "Origin", "icon": "🟠", "tint": "tile_3",
        "logo": "ea",
        "desc": "EA / Origin 客户端与商店",
        "items": ["官网", "商店", "游戏下载"],
    },
    {
        "key": "uplay", "name": "Uplay", "icon": "🎯", "tint": "tile_5",
        "logo": "ubisoft",
        "desc": "Ubisoft Connect 客户端与商店",
        "items": ["官网", "商店", "游戏下载"],
    },
    {
        "key": "cdn", "name": "公共 CDN", "icon": "🌐", "tint": "tile_8",
        "logo": "cloudflare",
        "desc": "被上述平台共用的大文件分发节点",
        "items": ["大文件", "第三方关联"],
    },
    {
        "key": "captcha", "name": "国外验证码平台", "icon": "🤖", "tint": "tile_6",
        "glyph": "shield",
        "desc": "hCaptcha / reCAPTCHA 等人机校验",
        "items": ["验证码", "静态资源"],
    },
    {
        "key": "github", "name": "Github", "icon": "🐙", "tint": "tile_2",
        "logo": "github",
        "desc": "主站、API、代码下载与页面资源",
        "items": ["主站", "API", "代码下载", "页面资源", "头像"],
    },
    {
        "key": "translate", "name": "Google 翻译", "icon": "🔤", "tint": "tile_1",
        "logo": "googletranslate",
        "desc": "translate.google.com 及其静态资源",
        "items": ["翻译", "静态资源"],
    },
    {
        "key": "imagehost", "name": "图床", "icon": "🖼️", "tint": "tile_8",
        "glyph": "image",
        "desc": "常用图片托管站点",
        "items": ["图床", "大文件"],
    },
]

#: 默认勾选（对齐 Steam++ 的默认三项）
_DEFAULT_ON = {"steam", "discord", "twitch"}


# ---------------------------------------------------------------------------
# 自绘控件（Yuhub 的主题色板里没有现成的"方框多选"，所以自己画）
# ---------------------------------------------------------------------------
class _ServiceCheck(QAbstractButton):
    """方形多选框：选中时填强调色 + 白色对勾。

    为什么不用 `ToggleSwitch`：这里是多选后点一键加速的语义，
    开关会让人以为"拨一下就生效"；Steam++ 用的也是方框多选。
    """

    SIZE = 18

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(bool(checked))
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setFocusPolicy(Qt.StrongFocus)

    def sizeHint(self):
        return QSize(self.SIZE, self.SIZE)

    def paintEvent(self, _ev):
        pal = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.SIZE - 1
        box = QPointF(0.5, 0.5)
        path = QPainterPath()
        path.addRoundedRect(box.x(), box.y(), r, r, 4.0, 4.0)
        if self.isChecked():
            p.fillPath(path, QColor(pal.get("accent", "#3b82f6")))
            # 对勾
            pen = QPen(QColor(pal.get("accent_text", "#ffffff")), 2.0)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(QPolygonF([
                QPointF(4.2, 9.2), QPointF(7.4, 12.4), QPointF(13.8, 5.6),
            ]))
        else:
            p.fillPath(path, QColor(pal.get("input_bg", "#0c0c10")))
            p.setPen(QPen(QColor(pal.get("border_strong", "#33333f")), 1.0))
            p.drawPath(path)
        if self.hasFocus():
            p.setPen(QPen(QColor(pal.get("accent", "#3b82f6")), 1.0, Qt.DotLine))
            p.drawPath(path)
        p.end()


class _Chevron(QAbstractButton):
    """展开/收起箭头（自绘三角，展开时旋转 90°）。"""

    SIZE = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setFocusPolicy(Qt.NoFocus)

    def sizeHint(self):
        return QSize(self.SIZE, self.SIZE)

    def paintEvent(self, _ev):
        pal = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        cx, cy = self.SIZE / 2.0, self.SIZE / 2.0
        if self.isChecked():                      # 展开：朝下
            pts = [QPointF(cx - 4.5, cy - 2.0), QPointF(cx + 4.5, cy - 2.0),
                   QPointF(cx, cy + 3.5)]
        else:                                     # 收起：朝右
            pts = [QPointF(cx - 2.0, cy - 4.5), QPointF(cx + 3.5, cy),
                   QPointF(cx - 2.0, cy + 4.5)]
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal.get("text_faint", "#8b93a3")))
        p.drawPolygon(QPolygonF(pts))
        p.end()


class _ServiceRow(QFrame):
    """一行平台：勾选框 + 色块图标 + 名称/说明 + 展开箭头 +（可展开的）条目列表。"""

    toggled = Signal(str, bool)          # (key, checked)

    def __init__(self, spec, checked, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.key = spec["key"]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 12, 10)
        outer.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(12)

        self.check = _ServiceCheck(checked)
        self.check.toggled.connect(
            lambda on: self.toggled.emit(self.key, bool(on)))
        head.addWidget(self.check, 0, Qt.AlignVCenter)

        self._spec = spec
        self.icon_tile = QFrame()
        self.icon_tile.setFixedSize(30, 30)
        icon_lay = QVBoxLayout(self.icon_tile)
        icon_lay.setContentsMargins(0, 0, 0, 0)
        self.icon_lbl = QLabel("")
        self.icon_lbl.setAlignment(Qt.AlignCenter)
        self.icon_lbl.setStyleSheet("background: transparent; border: none;")
        icon_lay.addWidget(self.icon_lbl)
        self._tint_key = spec.get("tint", "tile_1")
        head.addWidget(self.icon_tile, 0, Qt.AlignVCenter)

        name_box = QVBoxLayout()
        name_box.setSpacing(2)
        name = QLabel(spec["name"])
        name.setObjectName("CardTitle")
        desc = QLabel(spec["desc"])
        desc.setObjectName("CardDesc")
        desc.setWordWrap(True)
        name_box.addWidget(name)
        name_box.addWidget(desc)
        head.addLayout(name_box, 1)

        self.chevron = _Chevron()
        self.chevron.toggled.connect(self._on_expand)
        head.addWidget(self.chevron, 0, Qt.AlignVCenter)

        outer.addLayout(head)

        # 展开区：平台下的具体条目（对齐 Steam++ 展开后看到的东西）
        self.detail = QLabel("")
        self.detail.setObjectName("Faint")
        self.detail.setWordWrap(True)
        self.detail.setVisible(False)
        self.detail.setContentsMargins(60, 6, 0, 0)
        self.detail.setText("· " + "　· ".join(spec.get("items", []) or []))
        outer.addWidget(self.detail)

        theme.bus.changed.connect(lambda _=None: self._repaint_all())
        self._repaint_all()

    # ------------------------------------------------------------------
    def is_checked(self):
        return self.check.isChecked()

    def set_checked(self, on):
        blocked = self.check.blockSignals(True)
        self.check.setChecked(bool(on))
        self.check.blockSignals(blocked)
        self.check.update()

    def _on_expand(self, on):
        self.detail.setVisible(bool(on))
        self.chevron.update()

    def _repaint_all(self):
        pal = theme.current()
        color = pal.get(self._tint_key, pal.get("accent", "#3b82f6"))
        if theme.glass_on():
            self.icon_tile.setStyleSheet(
                "QFrame { %s }" % theme.glass_tile(color, radius=8))
        else:
            self.icon_tile.setStyleSheet(
                "QFrame { background: %s; border: none; border-radius: 8px; }"
                % color)
        # 图标：真 logo 优先（染成白色压在品牌色块上）；没有就退回 emoji
        pm = _icon_pixmap(self._spec, 18, "#ffffff")
        if pm is not None:
            self.icon_lbl.setPixmap(pm)
            self.icon_lbl.setText("")
        else:
            self.icon_lbl.setPixmap(QPixmap())      # 清掉上一张，避免叠影
            self.icon_lbl.setText(self._spec.get("icon", ""))
            self.icon_lbl.setStyleSheet(
                "font-size: 15px; background: transparent; border: none;"
                " color: #ffffff;")
        self.check.update()
        self.chevron.update()


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
class AccelPage(BasePage):
    TINT_KEY = "tile_5"
    BADGE = None            # 顶部右上角的「UI 骨架」徽标已删（用户 2026-10-11 要求）
    BADGE_STYLE = ""

    #: 启动内核的**工作线程**向 UI 线程回传结果（跨线程信号自动走队列投递）
    accelStarted = Signal(str)

    def __init__(self, notify=None, parent=None):
        super().__init__(
            "网络加速",
            "选择需要加速的平台，一键完成网络配置。"
            "加速由独立的框架承担（内嵌于本体，约 %s）。"
            % _FRAMEWORK_SIZE,
            icon="⚡",
            notify=notify,
            parent=parent,
        )
        self._settings = QSettings("Yuhub", "Yuhub")
        self._rows = {}
        self._state = ""            # no_framework / untrusted / ready / running / starting
        self._selected = self._load_selection()
        self.accelStarted.connect(self._on_accel_started)
        self._build_content()

    # ------------------------------------------------------------ 选中状态
    def _load_selection(self):
        raw = self._settings.value(_SETTINGS_KEY, "")
        if raw:
            try:
                got = {str(x) for x in json.loads(raw)}
                # 只保留还存在的平台（清单改过之后不让旧键残留）
                known = {s["key"] for s in _SERVICES}
                got = {k for k in got if k in known}
                # ⚠️ 空集合一律当"没设置过"处理，回落到默认三项。
                # 理由：这个功能里"一个平台都不选"是无意义状态（没东西可加速），
                # 而空值很容易被"自检/冒烟时构造过页面"之类的路径顺手写进来 ——
                # 让一次噪声把用户的默认勾选清空，代价远大于这一条兜底。
                if got:
                    return got
            except (ValueError, TypeError):
                pass
        return set(_DEFAULT_ON)

    def _save_selection(self):
        self._settings.setValue(_SETTINGS_KEY,
                                json.dumps(sorted(self._selected)))

    def _on_row_toggled(self, key, on):
        if self._state in ("running", "starting"):
            # 勾选框已被禁用，这里只是防键盘/程序性触发的第二道闸：
            # hosts 只在「一键加速」那一刻写一次，中途改勾选不会生效
            self._rows[key].check.setChecked(not on)
            self.toast("加速进行中，先「停止加速」才能改平台")
            return
        if on:
            self._selected.add(key)
        else:
            self._selected.discard(key)
        self._save_selection()
        self._refresh_toolbar()

    # ------------------------------------------------------------ 框架状态
    #
    # 三态状态机（界面据此换按钮 —— 绝不做"点了没反应"的假按钮）：
    #   未安装              → [安装加速框架]                [刷新]
    #   已装 · 证书未信任   → [信任证书]     [卸载框架]      [刷新]
    #   已装 · 证书已信任   → [一键加速]     [取消信任]      [刷新]
    @staticmethod
    def _framework_dir():
        return framework_dir()

    def _framework_path(self):
        return framework_path()

    def _framework_installed(self):
        return framework_installed()

    def _run_framework(self, args, timeout=10):
        """跑框架的一次性命令（委托模块级实现，见文件上方 run_framework）。"""
        return run_framework(args, timeout=timeout)

    def _ca_status(self):
        """读框架报的证书状态（`status --json`）。拿不到返回 None。"""
        return framework_status()

    # ------------------------------------------------------------ 构建
    def _build_content(self):
        # ---- 顶部工具条：状态 + 主/次按钮 + 刷新 + 加速设置 ----
        bar = QFrame()
        bar.setObjectName("Card")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 12, 16, 12)
        h.setSpacing(10)

        self._status = QLabel("")
        self._status.setObjectName("Muted")
        self._status.setWordWrap(True)
        h.addWidget(self._status, 1)

        # 主按钮：随状态换（安装加速框架 / 信任证书 / 一键加速）
        self._btn_primary = primary_button("安装加速框架")
        self._btn_primary.clicked.connect(self._on_primary)
        h.addWidget(self._btn_primary)

        # 次按钮：随状态换（卸载框架 / 取消信任）；未装框架时不显示
        self._btn_alt = ghost_button("")
        self._btn_alt.clicked.connect(self._on_alt)
        h.addWidget(self._btn_alt)

        self._btn_refresh = ghost_button("刷新")
        self._btn_refresh.clicked.connect(self._on_refresh)
        h.addWidget(self._btn_refresh)

        self.add(bar)

        # ---- 平台列表 ----
        for spec in _SERVICES:
            row = _ServiceRow(spec, spec["key"] in self._selected)
            row.toggled.connect(self._on_row_toggled)
            self.add(row)
            self._rows[spec["key"]] = row

        self.add(info_card(
            "一键加速是怎么工作的",
            "① 勾选你要加速的平台 —— 选择会记住，下次打开还是你选的那几个。"
            "② 点「一键加速」：框架把这些平台的域名写进 hosts 指向本机，"
            "并在 127.0.0.1:443 起一个本地反向代理 —— 按 SNI 现场签证书、"
            "终结 TLS，再把请求转发到预先解析好的真实 IP。"
            "③ 点「停止加速」结束内核，并把 hosts 里的加速记录整体清除。\n"
            "边界（如实说）：只转发规则表内的域名，规则外的 SNI 不会发证也"
            "不会被代理；WebSocket（如 Discord 语音网关）走裸流对拷，不解内容；"
            "我们不解密、不落盘、不上传任何流量。" ,
        ))
        self.add(info_card(
            "加速框架从哪来",
            "上一版（1.0.6 及以前）的加速走的是本地反向代理 + hosts，"
            "要改写 HTTPS 内容就必须在本机装一个自签名根证书（TLS 中间人）—— "
            "那等于把全机 HTTPS 的信任交出去，安全代价太高，不适合作为工具箱的"
            "默认能力。所以 1.0.7 把它整块移除，改由独立框架承担，"
            "框架本体已**内嵌**在本体 exe 里（%s）：进这个页面就自动就位，"
            "不需要下载、也不会选错文件。"
            "不想要加速，点「取消信任」收回对根证书的信任即可；框架本身内嵌、不占运行资源。" % _FRAMEWORK_SIZE,
        ))
        self.add_stretch()

        self._refresh_toolbar()

    # ------------------------------------------------------------ 刷新
    def _refresh_toolbar(self):
        # 「启动中」态由 _start_accel 自己绘制、由 _on_accel_started 收尾，
        # 刷新不打断它（否则会把"启动中…"覆盖回旧状态）
        if getattr(self, "_state", "") == "starting":
            return
        self._framework = self._framework_installed()
        # ---- 自动就位：框架随本体**内嵌**，未装 / 装的文件和内嵌那份对不上
        # （比如上一版手动选到的 151KB apphost 壳，跑起来报
        #   "YuhubAccel.dll 不存在"）→ 静默用内嵌那份盖掉，无感自愈。
        embedded = _embedded_framework_path()
        if embedded is not None:
            ip = self._framework_path()
            try:
                bad = (not os.path.isfile(ip)
                       or os.path.getsize(ip) != os.path.getsize(embedded))
            except OSError:
                bad = True
            if bad and self._install_framework(silent=True):
                self.toast("加速框架已自动就位（内嵌版 %s，随本体自带）"
                           % _FRAMEWORK_SIZE)
        self._framework = self._framework_installed()
        self._ca = self._ca_status() if self._framework else None
        trusted = bool(self._ca and self._ca.get("trusted"))
        running = self._accel_pid()

        # 脏退自愈（见本文件上方 recover_dirty_hosts 的长注释）：
        # 标记还在 且 内核确实没在跑 ⇒ 上次那一轮没走完收尾，此刻 hosts 正把所有
        # 加速域名指向一个没人监听的 443，浏览器/Steam 会直接打不开页面。
        # 主动还原 + 说人话，别让用户对着报错页猜。
        # ⚠️ 两道判活都要过（pid 文件 + 进程名）：pid 文件被清垃圾工具删掉而内核
        #    还在跑时误清 hosts，会把正在用的加速打断。
        if not running and not framework_running_by_name():
            did, dmsg = recover_dirty_hosts()
            if did:
                self.toast(dmsg)
                # hosts 刚被我们改过，框架状态重读一次更准
                self._ca = self._ca_status() if self._framework else self._ca
                trusted = bool(self._ca and self._ca.get("trusted"))

        n = len(self._selected)

        if not self._framework:
            self._state = "no_framework"
            self._status.setText(
                "加速框架：未安装（约 %s）　已选 %d 个平台\n"
                "装好框架之后还要把它的根证书装进系统信任库，加速才能真正生效。"
                % (_FRAMEWORK_SIZE, n))
            self._btn_primary.setText("安装加速框架")
            self._btn_alt.setVisible(False)
        elif trusted and running:
            self._state = "running"
            self._status.setText(
                "加速中：内核 pid %s 正在本地 127.0.0.1:443 接管所选平台\n"
                "hosts 已写入加速域名（带标记块，停止时整体清除）。\n"
                "已选 %d 个平台 —— 点「停止加速」结束并还原。" % (running, n))
            self._btn_primary.setText("停止加速")
            self._btn_alt.setVisible(False)
        elif not trusted:
            self._state = "untrusted"
            fp = str((self._ca or {}).get("thumbprint", ""))[:16]
            self._status.setText(
                "加速框架：已就绪　｜　根证书：未信任\n"
                "签发者指纹 %s… 　不装进信任库的话，加速会报证书错误。" % fp)
            self._btn_primary.setText("信任证书")
            # 框架随本体自带，"卸载框架"已随内嵌化移除（无东西可卸）
            self._btn_alt.setVisible(False)
        else:
            self._state = "ready"
            days = (self._ca or {}).get("expires_in_days", "?")
            self._status.setText(
                "加速框架：已就绪　｜　根证书：已信任（余 %s 天）\n"
                "已选 %d 个平台 —— 点「一键加速」开始。" % (days, n))
            self._btn_primary.setText("一键加速")
            self._btn_alt.setText("取消信任")
            self._btn_alt.setVisible(True)

        self._btn_primary.setVisible(True)
        # 只有"已就绪但一个平台都没勾"才该灰掉
        self._btn_primary.setEnabled(self._state != "ready" or n > 0)
        self._btn_refresh.setEnabled(True)
        # 加速中：锁住平台勾选（hosts 只在启动那一刻写一次）
        self._lock_rows(self._state in ("running", "starting"))

    # ------------------------------------------------------------ 动作
    def _on_primary(self):
        if self._state == "no_framework":
            self._install_framework()
            self._refresh_toolbar()
        elif self._state == "untrusted":
            self._trust_ca()
        elif self._state == "running":
            self._stop_accel()
        else:
            self._start_accel()

    def _on_alt(self):
        if self._state == "ready":
            self._untrust_ca()

    def _install_framework(self, silent=False):
        """装框架：用**内嵌在本体里**的那份（委托模块级 install_framework）。

        返回 True = 安装链路走完（复制 + gen-ca 成功）。
        silent 模式给自动安装用：不弹窗、不 toast，失败只返回 False。
        """
        if _embedded_framework_path() is None and not silent:
            # 兜底：本体没带上内嵌框架（开发者改了 spec / 目录被清），
            # 保留手动选文件的路径
            from PySide6.QtWidgets import QFileDialog
            path, _f = QFileDialog.getOpenFileName(
                self, "选择 YuhubAccel.exe（加速框架）", "",
                "加速框架 (YuhubAccel.exe);;可执行文件 (*.exe)")
            if not path:
                return False
            if os.path.basename(path).lower() != _FRAMEWORK_EXE.lower():
                self.toast("文件名不对：需要 %s" % _FRAMEWORK_EXE)
                return False
            try:
                os.makedirs(self._framework_dir(), exist_ok=True)
                shutil.copy2(path, self._framework_path())
            except OSError as exc:
                self.toast("安装失败：%s" % exc)
                return False
            rc, out, err = self._run_framework(["gen-ca"], timeout=40)
            if rc == 0:
                self.toast("框架已就位、根证书已生成 —— 下一步：信任证书")
                return True
            self.toast("框架已安装，但生成根证书失败：%s"
                       % (err or out).strip()[:80])
            return False

        ok, msg = install_framework(
            progress=None if silent else (lambda t: self.toast(t)))
        if not silent:
            self.toast(msg if ok else "安装失败：%s" % msg)
        return ok


    def _trust_ca(self):
        _rc, out, err = self._run_framework(["install-ca"], timeout=30)
        lines = (out or err).strip().splitlines()
        self.toast("信任证书：%s" % (lines[-1] if lines else "无输出"))
        self._refresh_toolbar()

    def _untrust_ca(self, silent=False):
        _rc, out, err = self._run_framework(["uninstall-ca"], timeout=30)
        if not silent:
            lines = (out or err).strip().splitlines()
            self.toast("取消信任：%s" % (lines[-1] if lines else "无输出"))
            self._refresh_toolbar()

    # ------------------------------------------------------------ 内核进程
    def _pid_path(self):
        return os.path.join(self._framework_dir(), _PID_NAME)

    def _log_path(self):
        return os.path.join(self._framework_dir(), _LOG_NAME)

    def _accel_pid(self):
        """内核在跑就返回 pid，否则 None。判据：pid 文件存在 + 进程真的活着。"""
        try:
            with open(self._pid_path(), "r", encoding="ascii") as fh:
                pid = int(fh.read().strip())
        except (OSError, ValueError):
            return None
        return pid if _process_alive(pid) else None

    def _start_accel(self):
        """一键加速：后台线程起内核 → 等就绪 → 信号回传刷新 UI。

        🔴 为什么必须丢进线程：内核启动前要做节点测速（每域名最多 6 个
        IP 的 TLS 握手，正常 2~5 秒、网络差更久），原先这段等待写在本方法
        里同步跑，事件循环整个被堵住 —— 表现就是"启动加速整个界面卡死"。
        """
        if self._accel_pid():
            self.toast("加速内核已经在运行")
            return
        services = ",".join(sorted(self._selected))

        # ---- UI 先行：立刻给「启动中」的反馈，按钮与勾选全部锁住 ----
        self._state = "starting"
        self._status.setText(
            "正在启动加速内核……\n"
            "正在对全部平台做节点测速（TLS 握手选优，约 3~10 秒），"
            "完成后会自动就绪。")
        self._btn_primary.setText("启动中…")
        self._btn_primary.setEnabled(False)
        self._btn_alt.setVisible(False)
        self._btn_refresh.setEnabled(False)
        self._lock_rows(True)

        import threading
        threading.Thread(
            target=self._accel_worker, args=(services,), daemon=True).start()

    def _accel_worker(self, services):
        """工作线程：Popen 内核 + 等就绪。**不碰任何 UI**，结果走信号。"""
        import socket
        import time

        # 先清扫僵尸：YuhubAccel.exe 是我们独有的进程名，凡是漏网的
        # 旧实例（崩溃残留 / pid 文件丢失）都会攥着 443 让新实例绑定
        # 失败——上一版"内核启动失败 + 恐吓弹窗"就是这么来的。
        subprocess.run(
            ["taskkill", "/IM", _FRAMEWORK_EXE, "/F"],
            capture_output=True, creationflags=_CREATE_NO_WINDOW)
        try:
            os.remove(self._pid_path())
        except OSError:
            pass

        log = open(self._log_path(), "w", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                [self._framework_path(), "serve", "--services", services],
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=_CREATE_NO_WINDOW, cwd=self._framework_dir())
        except OSError as exc:
            log.close()
            self.accelStarted.emit("启动失败：%s" % exc)
            return
        log.close()                     # 子进程已持有继承句柄，父进程这边可关

        # 等内核就绪：pid 文件出现 + 本地 443 能建 TCP 连接。
        # 启动前要对全部域名做节点测速（每域名最多 8 个 IP 并发 TLS 握手），
        # 正常 2~5 秒、网络差时更久；**首次启动还要更慢** —— 刚解包出来的
        # 11MB exe 会被杀软实时扫描一遍，远程规则也要在线拉一次。
        # 🔴 所以这里不能像旧版那样死写 25 秒：别的电脑上"第一次点加速就失败"
        #    多半就是它（机器慢/杀软卡 → 内核在就绪前被掐掉），而用户只看到
        #    一句"启动失败"，根本分不清是自己机器慢还是程序真出错。
        first_run = not os.path.isfile(
            os.path.join(self._framework_dir(), "rules-cache.json"))
        budget = 75 if first_run else 30
        deadline = time.time() + budget
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break                   # 进程死了 → 读日志告诉用户为什么
            try:
                with socket.create_connection(("127.0.0.1", 443), timeout=0.5):
                    ready = True
                    break
            except OSError:
                time.sleep(0.2)
        if proc.poll() is not None:
            try:
                with open(self._log_path(), encoding="utf-8", errors="replace") as fh:
                    tail = fh.read().strip()
            except OSError:
                tail = ""
            self.accelStarted.emit(
                "加速内核启动失败：%s" % tail[-140:].replace("\n", " / "))
        elif not ready:
            # 🔴 超时不能把半启动的进程留在后台：它可能稍后才就绪，
            #    下次点「一键加速」就会撞上"内核已经在运行"（上一版
            #    "第一次失败、第二次才成功"的真相就是它）。
            #    超时 = 干净收尾：杀进程 + 清 hosts，让下次从零开始。
            try:
                _terminate_pid(proc.pid)
            except Exception:
                pass
            self._run_framework(["hosts-clean"], timeout=10)
            # 内核是被 TerminateProcess 掐掉的，跑不到它自己的收尾，
            # 标记得我们这边抹 —— 刚清过 hosts，状态已回到干净起点。
            clear_dirty_flag()
            try:
                with open(self._log_path(), encoding="utf-8", errors="replace") as fh:
                    tail = fh.read().strip()
            except OSError:
                tail = ""
            self.accelStarted.emit(
                "%d 秒内没就绪（首次启动会更慢：杀软扫描新解包的 exe + 在线拉规则），"
                "已停止并还原 hosts —— 可直接重试。失败详情：%s"
                % (budget, tail[-120:].replace("\n", " / ")))
        else:
            # 就绪了也别急着报"已开启"：内核在跑 ≠ 流量会经过它。hosts 要是没
            # 写进去（被安全软件拦住、被别的加速器覆盖），加速等于一点没生效，
            # 而界面上一切正常 —— 那正是本项目最忌讳的"静默失效"。
            # 所以这里把 hosts 里到底有没有我们的标记块也验一遍。
            wrote, note = hosts_has_block()
            if wrote:
                self.accelStarted.emit(
                    "加速已开启：本地 127.0.0.1:443 正在接管所选平台的流量")
            else:
                self.accelStarted.emit(
                    "内核已就绪，但 hosts 里没有加速记录（%s）—— 加速不会生效，"
                    "请点「一键修复」" % note)

    def _on_accel_started(self, msg):
        """工作线程回传（跨线程信号，槽在 UI 线程内执行）。"""
        self._state = ""            # 解除 starting，让刷新能接管状态机
        self.toast(msg)
        self._refresh_toolbar()

    def _lock_rows(self, locked):
        """加速中/启动中锁住所有平台勾选：hosts 只在启动时写一次，
        中途改勾选既不会生效、还容易让人误以为改了就加速了。"""
        for row in self._rows.values():
            row.check.setEnabled(not locked)
            row.check.setToolTip("加速进行中，先「停止加速」才能改平台" if locked else "")

    def _stop_accel(self):
        """停止加速：结束内核进程 → 清 hosts 标记块。"""
        if self._state == "starting":
            self.toast("内核还在启动中，稍等几秒再停止")
            return
        pid = self._accel_pid()
        if pid:
            _terminate_pid(pid)
        else:
            # pid 文件丢失/已删时兜底：按进程名清（该名字被本框架独占）
            subprocess.run(
                ["taskkill", "/IM", _FRAMEWORK_EXE, "/F"],
                capture_output=True, creationflags=_CREATE_NO_WINDOW)
        _rc, out, err = self._run_framework(["hosts-clean"], timeout=10)
        clear_dirty_flag()          # 内核被强杀，标记得由这边抹（同上）
        msg = (out or err).strip().splitlines()
        self.toast("加速已停止：%s" % (msg[0] if msg else "hosts 已还原"))
        self._refresh_toolbar()

    def _on_refresh(self):
        self._refresh_toolbar()
        self.toast("已刷新框架与证书状态")

    def on_shown(self):
        self.refresh_state()

    def refresh_state(self):
        """重读框架/证书状态并刷新工具条（切页进来时、以及设置页修复完之后）。"""
        self._refresh_toolbar()
