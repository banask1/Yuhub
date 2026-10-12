# 内置 FFmpeg（H.264）—— 构建与放置说明

分屏的 H.264 编解码靠 **`avcodec` + `avutil` + `swscale`** 三个 DLL——不过
**现成的完整构建**里 `avcodec` 常常还依赖 `swresample`，所以下载/解压链路
（`ffmpeg_pack.KEEP_PREFIXES`）会**一并带上这四个**。
**不需要** `avformat`（Yuhub 有自己的 TCP 协议，H.264 直接传裸 NAL）。
极简自编译后**约 4~6MB**；官方完整包是 30~120MB，差的就是这些用不到的模块。

## 放在哪

按**优先级**依次找（第一个可用的胜出）：

1. `%LOCALAPPDATA%\Yuhub\ffmpeg\` —— **用户可写**：不放包里也能试一版新 DLL
2. 本目录（源码态 `resources/ffmpeg/`；打包后在 `_MEIPASS/resources/ffmpeg`）
3. 环境变量 `YUHUB_FFMPEG_DIR` 指向的目录

```
resources/ffmpeg/
    avcodec-63.dll      ← 版本号随 FFmpeg 版本变，代码按前缀匹配
    avutil-61.dll
    swscale-10.dll
    swresample-7.dll    ← 完整包里 avcodec 常依赖它；自编译极简版可省
```

**没有这些 DLL 也不会坏，但也不会"退回 JPEG"**：`h264codec.available()`
返回 False，于是**发送端不产画面帧、只发心跳保活**（界面会提示组件正在下载）；
`ffmpeg_pack.ensure_installed` 会在首次开启共享时后台把组件拉到候选目录 1，
装好后**下一帧自动切 H.264，不用重启、也不用重开共享**。

⚠️ 但**观看端的兼容性确实变差了**：自从只保留 H.264（JPEG 编码已从发送端
删除）之后，握手时不声明 `h264` 能力的**旧版客户端会被直接回绝**（`ERR codec`），
没有"退回去用 JPEG"这条兼容路。所以屏幕共享要求双方版本一致。

### ⚠️ 用现成完整包时，别只拷三个

自编译极简版（下面的参数）确实只要三个。但**现成的完整构建**可能额外依赖
`swresample-N.dll` 等 —— 实测某个 avcodec-63 就依赖 `swresample-7.dll`，
只拷三个的话加载会失败，而 Windows 的报错只有一句"找不到模块或它的某个依赖"。

本项目的加载器会**自己解析导入表并把缺的依赖名报出来**（见 `h264codec._missing_deps`），
所以遇到这种情况直接看日志/自检输出即可，不用猜。

## 怎么编（一次性的，约 5~15 分钟）

在 **MSYS2 (MINGW64)** 或 **WSL/Linux 交叉编译**里：

```sh
./configure \
  --disable-everything \
  --disable-programs \
  --disable-doc --disable-htmlpages --disable-manpages \
  --disable-avdevice --disable-avfilter --disable-postproc \
  --disable-avformat --disable-swresample --disable-network \
  --disable-autodetect \
  --enable-shared --disable-static \
  --enable-swscale \
  --enable-decoder=h264 \
  --enable-parser=h264 \
  --enable-encoder=h264_mf \
  --enable-encoder=h264_nvenc \
  --enable-encoder=h264_qsv \
  --enable-encoder=h264_amf \
  --enable-encoder=libopenh264 \
  --enable-muxer=null --enable-demuxer=h264 \
  --enable-protocol=file \
  --enable-cross-compile            # 仅交叉编译时加
```

要点（这几条决定了是 5MB 还是 30MB）：

| 参数 | 作用 |
|---|---|
| `--disable-everything` | 从零开始，只要列出来的东西 |
| `--disable-avformat` | 砍掉封装格式（省约 9MB，我们用不到） |
| `--disable-programs` | 不带 `ffmpeg.exe`/`ffplay`/`ffprobe`（省约 5MB） |
| `--enable-encoder=h264_mf` | **零体积首选**：用 Windows 自带 Media Foundation 编码器，不额外带 x264 |
| 不加 `--enable-libx264` | 不带 GPL 的软件编码器；要带就得整体按 GPL 分发 |
| `--enable-encoder=libopenh264` | 软件兜底（可选）。注意 Cisco 的专利覆盖只对其官方二进制且在"单独下载"条件下有效，**自编译版专利自负** |

编完只拷这三个（版本号跟着你的 FFmpeg 走）：

```sh
cp libavcodec/avcodec-*.dll libavutil/avutil-*.dll libswscale/swscale-*.dll  <本目录>
```

Windows 本地也可以用 MSVC 工具链（`--toolchain=msvc`）避开 MinGW 的
`libgcc/libwinpthread` 运行时 DLL；用 MinGW 时记得 `-static-libgcc`
或把那几个小 DLL 一起拷过来（否则加载会失败）。

## 许可（LGPL 合规）

- 上面这套参数**不含 GPL 组件**，按 **LGPL-2.1+** 使用
- 保持**动态链接**（.dll 单独文件，不要静态链进 exe）+ 附 LGPL 文本 +
  说明"可替换这些 DLL"，即满足 LGPL 的分发要求
- 若换成 `--enable-libx264`，整个分发要按 **GPLv2+** 处理

## 已验证的运行环境

- `avcodec 63.17.100`（FFmpeg 9.x 系）+ `h264_mf` 编码器 + `h264` 解码器，
  在 1920×1080 上完成"编码→解码"往返（`screenshare_selftest` 有这条检查，
  DLL 不在时会如实标 skip）
- `h264_mf` 的**必需**参数：输入像素格式 **NV12** + `hw_encoding=1`
  （见 `h264codec.ENCODER_CANDIDATES` 的注释，缺任一个都打不开）
