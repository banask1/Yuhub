# -*- coding: utf-8 -*-
"""H.264 编解码：ctypes 直调 libavcodec。

为什么要自己写这一层
--------------------
要拿到"同画质码率降 5~10 倍"，H.264 是唯一解；但官方 FFmpeg 包（含
`avformat` 与命令行工具）动辄 30~120MB，而分屏只需要**三个模块**：
`avcodec`（H.264 编解码）+ `avutil` + `swscale`（BGRA↔YUV420P）。
极简自编译后约 4~6MB（构建方法与参数见 `resources/ffmpeg/BUILD.md`）。
不引入 PyAV：那会再背 numpy 与 30MB 依赖，而我们需要的就是几个函数。

DLL 从哪来（按顺序找，找不到就如实说明，**不假装可用**）
----------------------------------------------------
1. `resources/ffmpeg/`（随包内置，PyInstaller 下是 `_MEIPASS/ffmpeg`）
2. 环境变量 `YUHUB_FFMPEG_DIR`（开发/调试用）
找不到时 `available()` 返回 False → 分屏自动退回 JPEG（旧版对端也照样能看）。

编码器优先级（为什么是这个顺序）
------------------------------
    h264_mf    —— Windows 自带 Media Foundation 编码器。**零体积**、自动
                  用硬件（NVIDIA/Intel/AMD 的 MFT 会被系统选中）、
                  且 H.264 专利由系统侧承担 → 首选
    h264_nvenc / h264_qsv / h264_amf —— 各家显卡的硬件编码器（若 FFmpeg 编进）
    libx264    —— 纯软件，画质最好但吃 CPU，且是 GPL（自用无妨，分发要注意）
    libopenh264—— 软件兜底，BSD
解码统一用 FFmpeg 自带的 `h264` 解码器（LGPL、多线程、兼容 Annex-B）。

传输格式：**Annex-B**（起始码分隔），发送端在每个 IDR 前面重复 SPS/PPS。
这样接收端**不需要** extradata 就能直接解（省掉一整类握手与状态同步）。

线程模型：一个 Encoder / Decoder 实例只能在**同一个线程**里使用（与 FFmpeg
的上下文语义一致）。分屏里编码器归编码线程、解码器归接收线程。
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys

# ---------------------------------------------------------------------------
# 常量（取自 libavutil / libavcodec 公共头，数值是 ABI 的一部分）
# ---------------------------------------------------------------------------
AV_PIX_FMT_YUV420P = 0
AV_PIX_FMT_NV12 = 23
AV_PIX_FMT_BGRA = 28
AV_CODEC_ID_H264 = 27

AV_CODEC_FLAG_GLOBAL_HEADER = 1 << 22
AV_CODEC_FLAG_LOW_DELAY = 1 << 19

AV_OPT_SEARCH_CHILDREN = 1 << 0

#: PKT_FLAG_KEY：包内含关键帧（IDR）
AV_PKT_FLAG_KEY = 1

#: 编码器候选表：(名字, 必需的输入像素格式, 额外参数)。
#:
#: 这里每一项都是**实测**出来的，不是照文档抄的（本机 avcodec 63.17）：
#:   * `h264_mf` 必须 **NV12** 且 `hw_encoding=1` —— 缺任一个都 open2 失败：
#:     不带 hw_encoding 报 AVERROR_EXTERNAL（-542398533），带 yuv420p 直接
#:     报参数非法。它是"零体积首选"：编码器来自 Windows 自带 Media Foundation，
#:     不用往包里塞任何编解码器。
#:   * `h264_qsv` 同样只吃 NV12（日志里明说 Supported pixel formats: nv12/qsv）。
#:   * 软件编码器（libx264 / libopenh264）用 YUV420P；libx264 还要
#:     `preset=veryfast + tune=zerolatency`（直播场景：不要 B 帧、不要前瞻）。
#: NV12 与 YUV420P 都由 swscale 从 BGRA 直接转，成本一样。
ENCODER_CANDIDATES = (
    ("h264_mf", AV_PIX_FMT_NV12, {"hw_encoding": "1",
                                  "scenario": "display_remoting"}),
    ("h264_nvenc", AV_PIX_FMT_NV12, {"preset": "p1", "tune": "ll"}),
    ("h264_qsv", AV_PIX_FMT_NV12, {}),
    ("h264_amf", AV_PIX_FMT_NV12, {"usage": "ultralowlatency"}),
    # profile/preset 只对软件编码器有效：h264_mf 不认 "profile=main"，
    # 设了只会在 stderr 刷一条 Unable to parse（无害但吵），所以按后端分开写
    ("libx264", AV_PIX_FMT_YUV420P, {"preset": "veryfast",
                                     "tune": "zerolatency",
                                     "profile": "main"}),
    ("libopenh264", AV_PIX_FMT_YUV420P, {"profile": "main"}),
)

#: 名字 → (像素格式, 额外参数)，供 `prefer=` 直接指定时使用
ENCODER_INDEX = {n: (f, o) for n, f, o in ENCODER_CANDIDATES}

#: 兼容旧名（模块早期版本里用过）
ENCODER_ORDER = tuple(n for n, _f, _o in ENCODER_CANDIDATES)

_DLL_DIRS = []


# ---------------------------------------------------------------------------
# 动态库加载
# ---------------------------------------------------------------------------
def _pe_imports(path):
    """读 PE 导入表，返回它直接依赖的 DLL 名列表。

    为什么要自己解析：Windows 加载失败时只说"找不到模块或它的某个依赖"，
    **不说缺哪个** —— 用户按文档放了三个 DLL 却仍然不可用时，这句话帮不上
    任何忙。实测就踩到过：某个 avcodec 构建额外依赖 `swresample-7.dll`。
    """
    import struct
    with open(path, "rb") as f:
        data = f.read()
    try:
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        optsz = struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        dd = opt + (96 if magic == 0x10B else 112)
        rva_imp = struct.unpack_from("<I", data, dd + 8)[0]
        secs = []
        so = pe + 24 + optsz
        for i in range(nsec):
            va, _vsz, praw, rawsz = struct.unpack_from("<IIII", data, so + i * 40 + 12)
            secs.append((va, praw, max(_vsz, rawsz)))

        def off(rva):
            for va, praw, size in secs:
                if va <= rva < va + size:
                    return praw + (rva - va)
            return None

        out = []
        o = off(rva_imp)
        if o is None:
            return out
        while True:
            ent = data[o:o + 20]
            if len(ent) < 20 or ent == b"\x00" * 20:
                break
            name_rva = struct.unpack_from("<I", ent, 12)[0]
            if not name_rva:
                break
            no = off(name_rva)
            if no is None:
                break
            end = data.index(b"\x00", no)
            out.append(data[no:end].decode("ascii", "replace"))
            o += 20
        return out
    except Exception:
        return []


def _missing_deps(path, directory):
    """列出 path 依赖、但目录里没有、也不在系统里的 DLL。

    只做"目录里有没有"这一层粗判（系统 DLL 一律当作有）—— 目的是把
    "少了 swresample-7.dll"这种事直接说出来，而不是让人对着
    "找不到模块或它的某个依赖"发呆。
    """
    have = set()
    try:
        have = {n.lower() for n in os.listdir(directory)}
    except OSError:
        pass
    system_like = ("kernel32", "user32", "gdi32", "advapi32", "ole32", "shell32",
                   "shlwapi", "ws2_32", "ntdll", "rpcrt4", "bcrypt", "dnsapi",
                   "iphlpapi", "userenv", "usp10", "msimg32", "d2d1", "dwrite",
                   "cfgmgr32", "bcryptprimitives", "api-ms-win", "vcruntime",
                   "msvcp", "ucrtbase", "powrprof", "secur32", "crypt32",
                   "comdlg32", "comctl32", "winmm", "oleaut32", "propsys")
    missing = []
    for dep in _pe_imports(path):
        low = dep.lower()
        if low in have:
            continue
        if any(low.startswith(s) for s in system_like):
            continue
        if low.endswith(".dll") and low not in missing:
            missing.append(dep)
    return missing


def candidate_dirs():
    """DLL 候选目录（**用户可覆盖** → 随包内置 → 环境变量）。"""
    out = []
    # ① 用户可写目录：用来"不重新打包就试一版 DLL"（或者以后做组件一键修复
    #    时下载到这里）。优先级最高，方便测试新版本。
    local = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "Yuhub",
                         "ffmpeg")
    out.append(local)
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "resources", "ffmpeg"),
              os.path.join(here, "ffmpeg")):
        out.append(p)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        # 打包后 resources 是按原目录结构收进 _MEIPASS 的（见 Yuhub.spec 的
        # os.walk），所以真实路径是 _MEIPASS/resources/ffmpeg。
        out.append(os.path.join(meipass, "resources", "ffmpeg"))
        out.append(os.path.join(meipass, "ffmpeg"))
    env = os.environ.get("YUHUB_FFMPEG_DIR", "")
    if env:
        out.append(env)
    return out


def pick_dir():
    """返回第一个**同时**含 avcodec 与 avutil 的目录（没有返回 ""）。"""
    for d in candidate_dirs():
        if not os.path.isdir(d):
            continue
        names = [n.lower() for n in os.listdir(d)]
        has_codec = any(n.startswith("avcodec-") and n.endswith(".dll")
                        for n in names)
        has_util = any(n.startswith("avutil-") and n.endswith(".dll")
                       for n in names)
        if has_codec and has_util:
            return d
    return ""


def available():
    """H.264 是否真的能用（DLL 在 + 解码器/编码器至少各有一个）。"""
    try:
        return _probe() is not None
    except Exception:
        return False


def describe():
    """给人看的一句话说明（界面/日志用）。"""
    info = _probe()
    if info is None:
        return "不可用（未内置 FFmpeg 组件）"
    return "可用：%s（avcodec %d.%d.%d，来自 %s）" % (
        info["encoder"], info["version"][0], info["version"][1],
        info["version"][2], info["dir"])


# ---------------------------------------------------------------------------
# 结构体前缀（只定义我们会读写的**前导字段**）
# ---------------------------------------------------------------------------
class _AVRational(ctypes.Structure):
    _fields_ = [("num", ctypes.c_int), ("den", ctypes.c_int)]


class _AVFramePrefix(ctypes.Structure):
    """AVFrame 的前导字段（data/linesize/宽高/格式都在前缀里）。

    只需要前缀：编码时我们要填 format/width/height 与三个平面指针，
    解码时读同样这几个字段。后面的 pts/pkt_dts 之流我们不碰。
    """
    _fields_ = [
        ("data", ctypes.POINTER(ctypes.c_uint8) * 8),
        ("linesize", ctypes.c_int * 8),
        ("extended_data", ctypes.c_void_p),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("nb_samples", ctypes.c_int),
        ("format", ctypes.c_int),
        ("key_frame", ctypes.c_int),
        ("pict_type", ctypes.c_int),
        ("sample_aspect_ratio", _AVRational),
        ("pts", ctypes.c_int64),
        ("pkt_dts", ctypes.c_int64),
    ]


class _AVPacketPrefix(ctypes.Structure):
    """AVPacket 的前导字段（data/size/flags）。"""
    _fields_ = [
        ("buf", ctypes.c_void_p),
        ("pts", ctypes.c_int64),
        ("dts", ctypes.c_int64),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("size", ctypes.c_int),
        ("stream_index", ctypes.c_int),
        ("flags", ctypes.c_int),
    ]


class _AVCodecContextPrefix(ctypes.Structure):
    """AVCodecContext 的前导字段（**只用于读回校验，不参与写入**）。

    ⚠️ 血泪：一开始想用它写 width/height/time_base，结果发现这些参数
    **全都有 AVOption 名**（`video_size` / `time_base` / `pixel_format`），
    根本不需要碰结构体偏移 —— 少一个 ABI 变化就崩的地方。这个结构体现在
    只在 open() 之后做一次"尺寸是否与请求一致"的读回校验；真伪由往返测试
    （编码→解码→比像素）兜底。
    """
    _fields_ = [
        ("av_class", ctypes.c_void_p),        # 0
        ("log_level_offset", ctypes.c_int),   # 8
        ("codec_type", ctypes.c_int),         # 12
        ("codec", ctypes.c_void_p),           # 16
        ("codec_id", ctypes.c_int),           # 24
        ("codec_tag", ctypes.c_uint),         # 28
        ("priv_data", ctypes.c_void_p),       # 32
        ("internal", ctypes.c_void_p),        # 40
        ("opaque", ctypes.c_void_p),          # 48
        ("bit_rate", ctypes.c_int64),         # 56
        ("flags", ctypes.c_int),              # 64
        ("flags2", ctypes.c_int),             # 68
        ("extradata", ctypes.c_void_p),       # 72
        ("extradata_size", ctypes.c_int),     # 80
        ("time_base", _AVRational),           # 84
        ("ticks_per_frame", ctypes.c_int),    # 92
        ("delay", ctypes.c_int),              # 96
        ("width", ctypes.c_int),              # 100
        ("height", ctypes.c_int),             # 104
        ("coded_width", ctypes.c_int),        # 108
        ("coded_height", ctypes.c_int),       # 112
    ]


# ---------------------------------------------------------------------------
# 库句柄与探测（只做一次，进程内缓存）
# ---------------------------------------------------------------------------
_LIB = {}
_PROBE_CACHE = {"done": False, "info": None}


def _load_libs():
    """加载 avcodec/avutil/swscale 并声明函数签名。失败抛 OSError。"""
    if _LIB:
        return _LIB
    d = pick_dir()
    if not d:
        raise OSError("未找到 FFmpeg 组件目录")
    # avcodec 会按名字去加载 avutil/swscale，得先把目录加进搜索路径
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AddDllDirectory.argtypes = [ctypes.c_wchar_p]
        k32.AddDllDirectory.restype = ctypes.c_void_p
        k32.SetDefaultDllDirectories.argtypes = [ctypes.c_uint32]
        # USER_DIRS | APPLICATION_DIRS | DEFAULT_DIRS
        k32.SetDefaultDllDirectories(0x00000400 | 0x00000200 | 0x00001000)
        k32.AddDllDirectory(d)
    except Exception:
        pass
    _DLL_DIRS.append(d)

    def _find(prefix):
        for fn in sorted(os.listdir(d)):
            low = fn.lower()
            if low.startswith(prefix) and low.endswith(".dll"):
                return os.path.join(d, fn)
        raise OSError("目录 %s 里没有 %s*.dll" % (d, prefix))

    os.add_dll_directory(d) if hasattr(os, "add_dll_directory") else None

    def _load_one(prefix):
        path = _find(prefix)
        try:
            return ctypes.WinDLL(path)
        except OSError as exc:
            # 把"缺哪个依赖"说清楚（见 _missing_deps 的注释）
            miss = _missing_deps(path, d)
            hint = ("，缺少依赖：%s" % ", ".join(miss)) if miss else ""
            raise OSError("加载 %s 失败：%s%s"
                          % (os.path.basename(path), exc, hint)) from None

    avutil = _load_one("avutil-")
    swscale = _load_one("swscale-")
    avcodec = _load_one("avcodec-")

    # ---- avutil / swscale ----
    avutil.av_frame_alloc.restype = ctypes.c_void_p
    avutil.av_frame_free.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    avutil.av_frame_get_buffer.argtypes = [ctypes.c_void_p, ctypes.c_int]
    avutil.av_frame_get_buffer.restype = ctypes.c_int
    avutil.av_frame_unref.argtypes = [ctypes.c_void_p]
    swscale.sws_getContext.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    swscale.sws_getContext.restype = ctypes.c_void_p
    swscale.sws_scale.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int)]
    swscale.sws_scale.restype = ctypes.c_int
    swscale.sws_freeContext.argtypes = [ctypes.c_void_p]

    # ---- avcodec ----
    avcodec.avcodec_version.restype = ctypes.c_uint
    avcodec.avcodec_find_encoder_by_name.argtypes = [ctypes.c_char_p]
    avcodec.avcodec_find_encoder_by_name.restype = ctypes.c_void_p
    avcodec.avcodec_find_decoder.argtypes = [ctypes.c_int]
    avcodec.avcodec_find_decoder.restype = ctypes.c_void_p
    avcodec.avcodec_alloc_context3.argtypes = [ctypes.c_void_p]
    avcodec.avcodec_alloc_context3.restype = ctypes.c_void_p
    avcodec.avcodec_free_context.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    avcodec.avcodec_open2.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p]
    avcodec.avcodec_open2.restype = ctypes.c_int
    avcodec.avcodec_send_frame.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    avcodec.avcodec_send_frame.restype = ctypes.c_int
    avcodec.avcodec_receive_packet.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    avcodec.avcodec_receive_packet.restype = ctypes.c_int
    avcodec.avcodec_send_packet.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    avcodec.avcodec_send_packet.restype = ctypes.c_int
    avcodec.avcodec_receive_frame.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    avcodec.avcodec_receive_frame.restype = ctypes.c_int
    # ⚠️ 每个要调的函数都必须声明签名：不声明时 ctypes 按 C int 传参，
    #    64 位指针会被截断成 32 位 → 直接把进程带崩（这里差点踩到）。
    avcodec.avcodec_flush_buffers.argtypes = [ctypes.c_void_p]
    avcodec.avcodec_flush_buffers.restype = None
    avcodec.av_new_packet.argtypes = [ctypes.c_void_p, ctypes.c_int]
    avcodec.av_new_packet.restype = ctypes.c_int
    avcodec.av_packet_alloc.restype = ctypes.c_void_p
    avcodec.av_packet_free.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    avcodec.av_packet_unref.argtypes = [ctypes.c_void_p]
    # av_opt_* 在 libavutil 里，但签名不变；用 avutil 句柄调用
    avutil.av_opt_set_int.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.c_int64, ctypes.c_int]
    avutil.av_opt_set_int.restype = ctypes.c_int
    avutil.av_opt_set.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                  ctypes.c_char_p, ctypes.c_int]
    avutil.av_opt_set.restype = ctypes.c_int
    # av_opt_set_q：设置 RATIONAL 型选项（time_base）
    avutil.av_opt_set_q.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                    ctypes.c_void_p, ctypes.c_int]
    avutil.av_opt_set_q.restype = ctypes.c_int
    avutil.av_opt_get_int.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_int64)]
    avutil.av_opt_get_int.restype = ctypes.c_int
    avutil.av_log_set_level.argtypes = [ctypes.c_int]
    avutil.av_log_set_level(16)          # AV_LOG_ERROR：别往 stderr 刷 INFO
    _LIB.update({"avcodec": avcodec, "avutil": avutil, "swscale": swscale,
                 "dir": d})
    return _LIB


def _probe():
    """探测一次：能用返回 dict，不能用返回 None（结果缓存）。"""
    if _PROBE_CACHE["done"]:
        return _PROBE_CACHE["info"]
    _PROBE_CACHE["done"] = True
    info = None
    try:
        lib = _load_libs()
        avcodec = lib["avcodec"]
        dec = avcodec.avcodec_find_decoder(AV_CODEC_ID_H264)
        enc_name = ""
        for name in ENCODER_ORDER:
            if avcodec.avcodec_find_encoder_by_name(name.encode()):
                enc_name = name
                break
        if dec and enc_name:
            v = int(avcodec.avcodec_version())
            info = {"dir": lib["dir"], "encoder": enc_name,
                    "version": (v >> 16, (v >> 8) & 0xFF, v & 0xFF)}
    except Exception:
        info = None
    _PROBE_CACHE["info"] = info
    return info


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _even(n, align=2):
    """H.264 要求宽高为偶数（yuv420p）。硬件编码器更喜欢 8/16 对齐。"""
    n = int(n)
    return max(align, (n // align) * align)


def _annexb_split(avcc_or_annexb):
    """把（可能带长度前缀的）数据统一成 Annex-B 起始码形式。

    FFmpeg 的编码器输出**取决于是否设了 GLOBAL_HEADER**：
      * 设了 → 裸 NAL，带 4 字节长度前缀（AVCC 风格，但没 avcC 头）
      * 没设 → 已含起始码（Annex-B）
    我们统一发 Annex-B：接收端不需要 extradata 就能解（见模块注释）。
    """
    data = bytes(avcc_or_annexb or b"")
    if not data:
        return b""
    if data[:4] == b"\x00\x00\x00\x01" or data[:3] == b"\x00\x00\x01":
        return data
    out = bytearray()
    i = 0
    n = len(data)
    while i + 4 <= n:
        ln = int.from_bytes(data[i:i + 4], "big")
        i += 4
        if ln <= 0 or i + ln > n:
            return data                     # 不是长度前缀格式，原样返回
        out += b"\x00\x00\x00\x01" + data[i:i + ln]
        i += ln
    return bytes(out) if out else data


def _extradata_to_annexb(extradata):
    """AVCC 形式的 extradata（含 avcC 头）→ Annex-B 的 SPS+PPS。

    发送端在**每个 IDR 前**重复 SPS/PPS，接收端因此永远不需要 extradata。
    """
    ed = bytes(extradata or b"")
    if len(ed) < 7 or ed[0] != 1:            # 1 = configurationVersion
        return b""
    p = 5
    num_sps = ed[p] & 0x1F
    p += 1
    out = bytearray()
    for _ in range(num_sps):
        if p + 2 > len(ed):
            break
        ln = int.from_bytes(ed[p:p + 2], "big")
        p += 2
        out += b"\x00\x00\x00\x01" + ed[p:p + ln]
        p += ln
    if p >= len(ed):
        return bytes(out)
    num_pps = ed[p]
    p += 1
    for _ in range(num_pps):
        if p + 2 > len(ed):
            break
        ln = int.from_bytes(ed[p:p + 2], "big")
        p += 2
        out += b"\x00\x00\x00\x01" + ed[p:p + ln]
        p += ln
    return bytes(out)


def _is_keyframe_annexb(data):
    """Annex-B 里是否含 IDR（type 5）——用于给关键帧打标。"""
    i = 0
    n = len(data)
    while i + 4 < n:
        if data[i:i + 3] == b"\x00\x00\x01":
            i += 3
        elif data[i:i + 4] == b"\x00\x00\x00\x01":
            i += 4
        else:
            i += 1
            continue
        if i < n and (data[i] & 0x1F) == 5:
            return True
    return False


# ---------------------------------------------------------------------------
# 编码器
# ---------------------------------------------------------------------------
class H264Encoder(object):
    """把 BGRA 帧编成 H.264（Annex-B）。**单线程使用**。

    典型用法：
        enc = H264Encoder(1920, 1080, fps=30, bitrate=4_000_000)
        enc.set_bitrate(2_500_000)          # 自适应码率就靠它
        for pkt, is_key in enc.encode(bgra, stride):
            ...
    """

    def __init__(self, width, height, fps=30, bitrate=4_000_000,
                 prefer=None, gop_seconds=2.0, threads=2, log=None):
        lib = _load_libs()
        self._lib = lib
        self._codec = lib["avcodec"]
        self._util = lib["avutil"]
        self._sws = lib["swscale"]
        self._log = log or (lambda msg: None)
        self.w = _even(width)
        self.h = _even(height)
        self.fps = max(1, int(fps))
        self.bitrate = int(bitrate)
        self._closed = False
        self._enc_name = ""
        self._sws_ctx = None
        self._frame = None
        self._pkt = None
        self._pts = 0
        self._gop = 0
        self._last_key = -1
        self._sps_pps = b""

        names = list(prefer or ()) or list(ENCODER_ORDER)
        last = ""
        for name in names:
            codec = self._codec.avcodec_find_encoder_by_name(name.encode())
            if not codec:
                last = "%s 不可用" % name
                continue
            pix_fmt, extra = ENCODER_INDEX.get(name, (AV_PIX_FMT_YUV420P, {}))
            try:
                self._open(codec, name, threads, gop_seconds, pix_fmt, extra)
                self._enc_name = name
                break
            except Exception as exc:                    # noqa: BLE001
                last = "%s 打开失败：%s" % (name, exc)
                self._teardown()
                continue
        else:
            raise OSError("没有可用的 H.264 编码器（%s）" % (last or "全部失败"))
        self._log("H.264 编码器：%s（%dx%d @%dfps，%.1fMbps，输入 %s）"
                  % (self._enc_name, self.w, self.h, self.fps,
                     self.bitrate / 1e6,
                     "NV12" if self.pix_fmt == AV_PIX_FMT_NV12 else "YUV420P"))

    # ---------------------------------------------------------------- 内部
    def _open(self, codec, name, threads, gop_seconds, pix_fmt,
              extra=None):
        self._ctx = self._codec.avcodec_alloc_context3(codec)
        if not self._ctx:
            raise OSError("avcodec_alloc_context3 失败")
        ctx = self._ctx
        self.pix_fmt = int(pix_fmt)
        # ---- 全部参数走 AVOption（实测这些名字在本版本都存在）----
        #  尺寸/时基/像素格式都有正式选项名，**不要**去写结构体偏移：
        #  `pix_fmt` 在新版已改名为 `pixel_format`（旧名会返回 OPTION_NOT_FOUND，
        #  错误码 -1414549496 就是 'O','P','T','I' 四个字母的 tag）。
        if not self._set_size(self.w, self.h):
            raise OSError("设置 video_size 失败")
        self._set_q("time_base", 1, self.fps)
        if self._set_int("pixel_format", self.pix_fmt, soft=True) < 0:
            self._set_int("pix_fmt", self.pix_fmt, "像素格式（旧名）",
                          soft=True)
        self._set_int("b", self.bitrate, "码率")
        self._set_int("g", int(self.fps * max(0.5, gop_seconds)), "GOP")
        self._set_int("bf", 0, "B 帧（直播必须 0，否则延迟与重排都难受）")
        self._set_int("threads", max(1, int(threads)), "线程数")
        self._set_int("flags", AV_CODEC_FLAG_LOW_DELAY, "低延迟标志",
                      soft=True)
        # 先设该编码器**必需**的参数（open2 的成败取决于它们），再设通用的
        # "画质/延迟档"。顺序很重要：h264_mf 缺 hw_encoding 直接打不开。
        for key, val in (extra or {}).items():
            self._set_str(key, val, "%s=%s" % (key, val), soft=False)
        # 码率控制：各家名字不同，能设就设（cbr 让"目标码率"真的等于实际码率，
        # 自适应才有意义；设不上也还能跑，只是码率更飘）
        for key, val in (("rate_control", "cbr"), ("rc", "cbr"),
                         ("async_depth", "1")):
            self._set_str(key, val, "%s=%s" % (key, val), soft=True)
        rc = self._codec.avcodec_open2(ctx, codec, None)
        if rc < 0:
            raise OSError("avcodec_open2 返回 %d" % rc)
        # 读回校验（只读，不写）：尺寸与请求差太多说明 ABI/参数出了问题
        try:
            prefix = ctypes.cast(ctx, ctypes.POINTER(_AVCodecContextPrefix))
            w = int(prefix.contents.width)
            h = int(prefix.contents.height)
            if w > 0 and h > 0 and (abs(w - self.w) > 1 or abs(h - self.h) > 1):
                self._log("注意：编码器把尺寸从 %dx%d 调成了 %dx%d"
                          % (self.w, self.h, w, h))
                self.w, self.h = _even(w), _even(h)
        except Exception:
            pass
        ed = b""
        try:
            prefix = ctypes.cast(ctx, ctypes.POINTER(_AVCodecContextPrefix))
            if prefix.contents.extradata and prefix.contents.extradata_size > 0:
                ed = ctypes.string_at(prefix.contents.extradata,
                                      prefix.contents.extradata_size)
        except Exception:
            ed = b""
        self._sps_pps = _extradata_to_annexb(ed)
        if self._sps_pps:
            self._log("已提取 SPS/PPS（%d 字节），将在每个关键帧前重复发送"
                      % len(self._sps_pps))
        # swscale：BGRA → 该编码器要求的输入格式（NV12 或 YUV420P）
        self._sws_ctx = self._sws.sws_getContext(
            self.w, self.h, AV_PIX_FMT_BGRA,
            self.w, self.h, self.pix_fmt, 2, None, None, None)
        if not self._sws_ctx:
            raise OSError("sws_getContext 失败")
        self._frame = self._alloc_frame()
        self._pkt = self._codec.av_packet_alloc()
        if not self._pkt:
            raise OSError("av_packet_alloc 失败")

    def _alloc_frame(self):
        frame = self._util.av_frame_alloc()
        if not frame:
            raise OSError("av_frame_alloc 失败")
        p = ctypes.cast(frame, ctypes.POINTER(_AVFramePrefix))
        p.contents.format = int(getattr(self, "pix_fmt", AV_PIX_FMT_YUV420P))
        p.contents.width = self.w
        p.contents.height = self.h
        rc = self._util.av_frame_get_buffer(frame, 32)
        if rc < 0:
            raise OSError("av_frame_get_buffer 返回 %d" % rc)
        return frame

    def _set_int(self, key, val, what="", soft=False):
        rc = self._util.av_opt_set_int(self._ctx, key.encode(), int(val),
                                       AV_OPT_SEARCH_CHILDREN)
        if rc < 0 and not soft:
            raise OSError("设置 %s（%s）失败：%d" % (what or key, key, rc))
        return rc

    def _set_str(self, key, val, what, soft=True):
        rc = self._util.av_opt_set(self._ctx, key.encode(), str(val).encode(),
                                   AV_OPT_SEARCH_CHILDREN)
        if rc < 0 and not soft:
            raise OSError("设置 %s（%s）失败：%d" % (what, key, rc))
        return rc

    def _set_size(self, w, h):
        """尺寸用 `video_size` 字符串选项（`width`/`height` 不是 AVOption）。"""
        return self._set_str("video_size", "%dx%d" % (int(w), int(h)),
                             "尺寸", soft=False) >= 0

    def _set_q(self, key, num, den):
        """RATIONAL 型选项（time_base）。

        ⚠️ 必须**先只在本对象上找**（flags=0）：`AV_OPT_SEARCH_CHILDREN` 会
        先命中编码器私有上下文里的同名选项，而那个的类型可能是别的 ——
        实测 h264_mf 会因此收到一段被当成 double 解释的垃圾值，日志里报
        `Value -2584082.576271 for parameter 'time_base' out of range`。
        """
        val = _AVRational(int(num), int(den))
        rc = self._util.av_opt_set_q(self._ctx, key.encode(),
                                     ctypes.byref(val), 0)
        if rc < 0:
            rc = self._util.av_opt_set_q(self._ctx, key.encode(),
                                         ctypes.byref(val),
                                         AV_OPT_SEARCH_CHILDREN)
        if rc < 0:
            self._set_int(key, int(num), key, soft=True)
        return rc

    def _teardown(self):
        for attr in ("_sws_ctx",):
            ctx = getattr(self, attr, None)
            if ctx:
                try:
                    self._sws.sws_freeContext(ctx)
                except Exception:
                    pass
                setattr(self, attr, None)
        if getattr(self, "_frame", None):
            holder = ctypes.c_void_p(self._frame)
            self._util.av_frame_free(ctypes.byref(holder))
            self._frame = None
        if getattr(self, "_pkt", None):
            holder = ctypes.c_void_p(self._pkt)
            self._codec.av_packet_free(ctypes.byref(holder))
            self._pkt = None
        ctx = getattr(self, "_ctx", None)
        if ctx:
            holder = ctypes.c_void_p(ctx)
            self._codec.avcodec_free_context(ctypes.byref(holder))
            self._ctx = None

    # ---------------------------------------------------------------- 对外
    @property
    def name(self):
        return self._enc_name

    def set_bitrate(self, bps):
        """动态改码率 —— 自适应码率的核心动作。

        绝大多数编码器支持运行时改码率（不需要重开上下文、不需要关键帧），
        这正是"带宽变窄就降码率、变宽就升回去"能做成的前提。
        返回是否设置成功（设不上时调用方应退化为"改分辨率"）。
        """
        bps = max(120_000, int(bps))
        if bps == self.bitrate:
            return True
        if self._closed:
            return False
        # 同时写 AVOption 与结构体字段：有的编码器只认其中一个
        rc = self._util.av_opt_set_int(self._ctx, b"b", bps,
                                       AV_OPT_SEARCH_CHILDREN)
        try:
            prefix = ctypes.cast(self._ctx,
                                 ctypes.POINTER(_AVCodecContextPrefix))
            prefix.contents.bit_rate = bps
        except Exception:
            pass
        if rc >= 0:
            self.bitrate = bps
            return True
        return False

    def encode(self, bgra, stride=None):
        """编一帧。返回 [(annexb_bytes, is_keyframe), ...]（通常 1 个包）。

        bgra 必须是 w×h 的 BGRA（顶层向下）—— 与 GDI 抓屏的输出一致。
        返回空列表表示编码器还在缓冲（低延迟模式下基本不会发生）。
        """
        if self._closed:
            raise RuntimeError("编码器已关闭")
        w, h = self.w, self.h
        stride = int(stride or (w * 4))
        need = stride * h
        if len(bgra) < need:
            raise ValueError("帧数据不足：%d < %d" % (len(bgra), need))
        # 把 BGRA 指针喂给 swscale，直接写进 AVFrame 的 YUV 平面
        src_buf = (ctypes.c_char * len(bgra)).from_buffer_copy(
            bgra[:need] if len(bgra) > need else bgra)
        src_planes = (ctypes.c_void_p * 4)()
        src_planes[0] = ctypes.cast(src_buf, ctypes.c_void_p)
        src_lines = (ctypes.c_int * 4)()
        src_lines[0] = stride
        fp = ctypes.cast(self._frame, ctypes.POINTER(_AVFramePrefix))
        dst_planes = (ctypes.c_void_p * 4)()
        dst_lines = (ctypes.c_int * 4)()
        for i in range(4):
            dst_planes[i] = ctypes.cast(fp.contents.data[i], ctypes.c_void_p) \
                if fp.contents.data[i] else None
            dst_lines[i] = fp.contents.linesize[i]
        n = self._sws.sws_scale(self._sws_ctx, src_planes, src_lines, 0, h,
                                dst_planes, dst_lines)
        if n <= 0:
            raise OSError("sws_scale 转换失败（返回 %d）" % n)
        fp.contents.pts = self._pts
        self._pts += 1
        rc = self._codec.avcodec_send_frame(self._ctx, self._frame)
        if rc < 0:
            raise OSError("avcodec_send_frame 返回 %d" % rc)
        return self._drain()

    def _drain(self):
        out = []
        while True:
            rc = self._codec.avcodec_receive_packet(self._ctx, self._pkt)
            if rc == -11 or rc == -541478725:        # EAGAIN / EOF
                break
            if rc < 0:
                raise OSError("avcodec_receive_packet 返回 %d" % rc)
            pp = ctypes.cast(self._pkt, ctypes.POINTER(_AVPacketPrefix))
            size = int(pp.contents.size)
            if size > 0 and pp.contents.data:
                raw = ctypes.string_at(pp.contents.data, size)
                data = _annexb_split(raw)
                is_key = bool(int(pp.contents.flags) & AV_PKT_FLAG_KEY) \
                    or _is_keyframe_annexb(data)
                if is_key and self._sps_pps and not data.startswith(
                        self._sps_pps):
                    data = self._sps_pps + data
                    is_key = True
                out.append((data, is_key or self._last_key < 0))
                if is_key:
                    self._last_key = self._pts
            self._codec.av_packet_unref(self._pkt)
        return out

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._teardown()


# ---------------------------------------------------------------------------
# 解码器
# ---------------------------------------------------------------------------
class H264Decoder(object):
    """把 Annex-B 的 H.264 解成 BGRA。**单线程使用**（接收线程里）。"""

    def __init__(self, threads=2, log=None):
        lib = _load_libs()
        self._lib = lib
        self._codec = lib["avcodec"]
        self._util = lib["avutil"]
        self._sws = lib["swscale"]
        self._log = log or (lambda msg: None)
        self._closed = False
        codec = self._codec.avcodec_find_decoder(AV_CODEC_ID_H264)
        if not codec:
            raise OSError("FFmpeg 里没有 h264 解码器")
        self._ctx = self._codec.avcodec_alloc_context3(codec)
        if not self._ctx:
            raise OSError("avcodec_alloc_context3 失败")
        self._util.av_opt_set_int(self._ctx, b"threads", max(1, int(threads)),
                                  AV_OPT_SEARCH_CHILDREN)
        self._util.av_opt_set_int(self._ctx, b"flags",
                                  AV_CODEC_FLAG_LOW_DELAY,
                                  AV_OPT_SEARCH_CHILDREN)
        rc = self._codec.avcodec_open2(self._ctx, codec, None)
        if rc < 0:
            raise OSError("avcodec_open2 返回 %d" % rc)
        self._frame = self._util.av_frame_alloc()
        self._pkt = self._codec.av_packet_alloc()
        self._sws_ctx = None
        self._sws_size = (0, 0)
        self.w = 0
        self.h = 0
        self.decoded = 0

    def _ensure_sws(self, w, h, fmt):
        if self._sws_ctx and self._sws_size == (w, h, fmt):
            return
        if self._sws_ctx:
            try:
                self._sws.sws_freeContext(self._sws_ctx)
            except Exception:
                pass
        # 解码输出的像素格式由解码器决定（多数是 yuv420p，硬解可能是 nv12），
        # 所以按 AVFrame 实际报的 format 建转换器，而不是写死。
        self._sws_ctx = self._sws.sws_getContext(
            w, h, int(fmt), w, h, AV_PIX_FMT_BGRA, 2, None, None, None)
        if not self._sws_ctx:
            raise OSError("sws_getContext（解码侧）失败")
        self._sws_size = (w, h, fmt)

    def decode(self, data):
        """喂一个 Annex-B 包。返回 (w, h, bgra_bytes) 或 None（还没出图）。"""
        if self._closed:
            raise RuntimeError("解码器已关闭")
        data = bytes(data or b"")
        if not data:
            return None
        # ⚠️ 必须让**FFmpeg 拥有**这块内存（av_new_packet 分配 + memmove）。
        # 第一版是把 Python 临时缓冲的指针塞进 AVPacket 就 send_packet —— 
        # 而 FFmpeg 可能仍持有引用（延迟帧/B 帧重排），那块缓冲在 decode()
        # 返回后就被回收了 → use-after-free，表现为"自检全过、进程退出时
        # 访问违例（0xC0000005）"。多一次 memcpy（几百 KB，可忽略）换确定性。
        rc = self._codec.av_new_packet(self._pkt, len(data))
        if rc < 0:
            self._log("分配包缓冲失败（%d）" % rc)
            return None
        pp = ctypes.cast(self._pkt, ctypes.POINTER(_AVPacketPrefix))
        try:
            if pp.contents.data:
                ctypes.memmove(pp.contents.data, data, len(data))
            pp.contents.flags = 0
            rc = self._codec.avcodec_send_packet(self._ctx, self._pkt)
        finally:
            # 送完就还：FFmpeg 内部已经 av_packet_ref 过，可以安全释放
            self._codec.av_packet_unref(self._pkt)
        if rc < 0:
            # 坏包不该弄死整条流：清掉错误状态，等下一个关键帧
            self._log("送包失败（%d），已跳过" % rc)
            try:
                self._codec.avcodec_flush_buffers(self._ctx)
            except Exception:
                pass
            return None
        rc = self._codec.avcodec_receive_frame(self._ctx, self._frame)
        if rc == -11 or rc == -541478725:
            return None
        if rc < 0:
            self._log("收帧失败（%d）" % rc)
            return None
        fp = ctypes.cast(self._frame, ctypes.POINTER(_AVFramePrefix))
        w = int(fp.contents.width)
        h = int(fp.contents.height)
        if w <= 0 or h <= 0:
            return None
        self.w, self.h = w, h
        fmt = int(fp.contents.format)
        self._ensure_sws(w, h, fmt)
        out = (ctypes.c_char * (w * h * 4))()
        dst_planes = (ctypes.c_void_p * 4)()
        dst_planes[0] = ctypes.cast(out, ctypes.c_void_p)
        dst_lines = (ctypes.c_int * 4)()
        dst_lines[0] = w * 4
        src_planes = (ctypes.c_void_p * 4)()
        src_lines = (ctypes.c_int * 4)()
        for i in range(4):
            src_planes[i] = ctypes.cast(fp.contents.data[i], ctypes.c_void_p) \
                if fp.contents.data[i] else None
            src_lines[i] = fp.contents.linesize[i]
        n = self._sws.sws_scale(self._sws_ctx, src_planes, src_lines, 0, h,
                                dst_planes, dst_lines)
        try:
            self._util.av_frame_unref(self._frame)
        except Exception:
            pass
        if n <= 0:
            return None
        self.decoded += 1
        return (w, h, bytes(out))

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._sws_ctx:
            try:
                self._sws.sws_freeContext(self._sws_ctx)
            except Exception:
                pass
            self._sws_ctx = None
        if self._frame:
            holder = ctypes.c_void_p(self._frame)
            self._util.av_frame_free(ctypes.byref(holder))
            self._frame = None
        if self._pkt:
            holder = ctypes.c_void_p(self._pkt)
            self._codec.av_packet_free(ctypes.byref(holder))
            self._pkt = None
        if self._ctx:
            holder = ctypes.c_void_p(self._ctx)
            self._codec.avcodec_free_context(ctypes.byref(holder))
            self._ctx = None
