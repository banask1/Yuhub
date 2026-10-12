# -*- coding: utf-8 -*-
"""FFmpeg（H.264）组件的**按需下载**：单 exe 也能用上 H.264。

为什么是"按需下载"而不是打进 exe
--------------------------------
用户要"单 exe 就能实现全部功能"，同时要求体积小（项目历史上为 63MB vs
270MB 拉扯过）。FFmpeg 的极简自编译约 4~6MB，但**本机没有 C 工具链**编不出
来；现成通用包是 76~114MB，打进去 exe 会翻倍。所以采用项目里已有的成熟
模式 —— **和 EasyTier 组件一样：本体不背，首次使用时后台下载**：

    单 exe（47.7MB）→ 首次开共享 → 后台下载组件包 → 解压到
    %LOCALAPPDATA%\\Yuhub\\ffmpeg → 之后离线可用，H.264 生效

URL 清单按顺序尝试（镜像失败自动落到下一个），与 `etier._EASYTIER_...`
同一套路。**默认指向 BtbN 的 LGPL shared 包（约 77MB）**：它是目前最可靠
的公开源（含 `h264_mf`，LGPL 许可干净）。等用户按
`resources/ffmpeg/BUILD.md` 编出 4~6MB 的极简包并发布后，把这个列表换成
自己的 Release 地址即可 —— 下载体积从 77MB 缩到几 MB，**代码零改动**。

只解压需要的 DLL（avcodec/avutil/swscale/swresample，按文件名前缀），
其余（ffmpeg.exe、avformat、libavdevice…）原样丢弃 —— 通用包里 90% 的
内容我们都用不到。
"""

import os
import zipfile

from h264codec import candidate_dirs, pick_dir  # noqa: F401  (复用目录规则)

#: 组件包下载地址（按顺序尝试）。第一个是稳定可用的公开构建；
#: 用户自编译极简包后，把它放在列表**最前面**即可。
PACK_URLS = (
    # TODO(用户)：编出极简包后发布到 GitHub Release，把地址放这里
    # "https://github.com/<你的仓库>/releases/download/v1/yuhub-ffmpeg-min.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-n8.1-latest-win64-lgpl-shared-8.1.zip",
)

#: 组件安装目录（h264codec 的候选之一，优先级最高）
INSTALL_DIR = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub",
                           "ffmpeg")

#: 需要从包里取出的 DLL 前缀（大小写不敏感）。通用包里其余的
#: ffmpeg.exe / avformat / avdevice / 滤镜库……全部不要。
KEEP_PREFIXES = ("avcodec-", "avutil-", "swscale-", "swresample-")


def install_dir():
    return INSTALL_DIR


def installed():
    """组件是否已就位（等价于 h264codec.available()，但避免循环导入）。"""
    try:
        return pick_dir() != ""
    except Exception:
        return False


def extract_zip(zip_path, dest, on_progress=None):
    """从组件包里解出需要的 DLL 到 dest。返回解出的文件名列表。

    为什么不把整个包解压：BtbN 包里有 ffmpeg.exe（80MB+）、全部滤镜与
    封装格式 —— 我们只需要 4 个 DLL。按前缀挑出来，其余留在临时目录里
    随包一起删掉。
    """
    out = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        wanted = [n for n in names
                  if n.lower().endswith(".dll")
                  and os.path.basename(n).lower().startswith(KEEP_PREFIXES)]
        if not wanted:
            # 兼容"用户自己打的极简包"：万一打包时把 DLL 放在根目录以外
            # 或改了名，至少把根目录的 dll 全收进来
            wanted = [n for n in names
                      if "/" not in n and n.lower().endswith(".dll")]
        if not wanted:
            raise ValueError("组件包里没有可用的 DLL（%d 个条目）" % len(names))
        os.makedirs(dest, exist_ok=True)
        total = len(wanted)
        for i, name in enumerate(wanted):
            base = os.path.basename(name)
            with zf.open(name) as src, \
                    open(os.path.join(dest, base), "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            out.append(base)
            if on_progress:
                try:
                    on_progress("解压组件 %d/%d" % (i + 1, total))
                except Exception:
                    pass
    return out


def ensure_installed(on_progress=None, on_stage=None, dest=None):
    """确保组件就位：已装 → 直接返回；没装 → 下载 → 解压 → 验证。

    返回 (成功, 说明)。下载失败时如实报错（含尝试过的 URL），
    **绝不**假装成功 —— 分屏那边会继续用 JPEG。
    """
    dest = dest or INSTALL_DIR
    if pick_dir():
        return True, "组件已就位：%s" % pick_dir()
    os.makedirs(dest, exist_ok=True)
    errors = []
    for url in PACK_URLS:
        tmp = os.path.join(dest, "_pack.zip")
        if on_stage:
            on_stage("下载组件（约 77MB，一次性）")
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Yuhub"})
            with urllib.request.urlopen(req, timeout=60) as resp, \
                    open(tmp, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress and total:
                        on_progress("下载组件 %.0f/%.0f MB"
                                    % (done / 1048576.0, total / 1048576.0))
            if on_stage:
                on_stage("解压组件")
            got = extract_zip(tmp, dest, on_progress=on_progress)
        except Exception as exc:                        # noqa: BLE001
            errors.append("%s → %s" % (url, exc))
            continue
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        # 验证：解出来的 DLL 必须真的能被 h264codec 用起来
        from h264codec import _PROBE_CACHE
        _PROBE_CACHE["done"] = False
        _PROBE_CACHE["info"] = None
        if pick_dir():
            names = ", ".join(got)
            return True, "组件安装完成：%s" % names
        errors.append("%s → 解压出的 DLL 无法加载（缺依赖？）" % url)
    return False, "组件下载失败：%s" % "；".join(errors)
