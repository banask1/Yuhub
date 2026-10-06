# -*- mode: python ; coding: utf-8 -*-

import os

# resources 整体随包，但**排除只用于生成图标的两张源图**：
# app_source.png + app_source_alpha.png 合计 5MB，只在 build_icon.py 里
# 被读一次（那是打包前的构建步骤，读的是源目录），运行时一次都用不到。
# PyInstaller 的 ('resources', 'resources') 是整个目录照单全收，所以这里
# 改成逐文件列出，把这两张挑出去。
_ICON_SOURCES = {"app_source.png", "app_source_alpha.png"}
_datas = [
    # memopt.py 单独带上：它的自检要做 AST 静态扫描，确认代码里没有任何
    # "结束进程"的调用 —— 这是"内存优化绝不关掉用户程序"这条承诺的静态
    # 证据。PyInstaller 默认只把 .pyc 收进 PYZ，源码不可读，扫描会失效。
    ("memopt.py", "."),
    # hostsaccel.py 同理：自检要 AST 扫描"提权进程只写 hosts 和结果文件、
    # 绝不碰注册表 / 杀进程 / 联网"，并校验域名清单与清洗逻辑的一致性。
    ("hostsaccel.py", "."),
    # hostssniproxy.py：自检要 AST 扫描"纯标准库、不 import ssl（透传不解密）、
    # 不碰子进程/注册表"。
    ("hostssniproxy.py", "."),
    # accel_page.py：自检要 AST 扫描"后台线程回调只 emit"的界面纪律。
    ("ui/pages/accel_page.py", "ui/pages"),
    # main.py / single_instance.py / winadmin.py / accel_page.py：
    # 「常驻管理员启动 + 重启链路已删净」这两条都是**读源码才验得了**的
    # 结构性约束 —— 自检会 AST / 文本扫描这四个文件，确认：
    #   ① single_instance._probe 真的在泵事件循环（同进程时 waitFor* 不保证
    #      把消息推出去，写完立刻 disconnect 会把字节丢掉）；
    #   ② 代码里不再残留 relaunch / takeover 的任何痕迹（谁加回来就红）。
    # 源码没随包 → 扫描**空转**、断言照样绿 —— 那正是"它绿了≠它测到了"，
    # 所以这四个文件必须进 datas。
    ("main.py", "."),
    ("single_instance.py", "."),
    # winadmin.py：提权判定（TokenElevation）—— 不能用 IsUserAnAdmin 判组。
    ("winadmin.py", "."),
]
for _root, _dirs, _files in os.walk("resources"):
    for _name in _files:
        if _name in _ICON_SOURCES:
            continue
        _path = os.path.join(_root, _name)
        _datas.append((_path, _root))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=['theme_selftest', 'uninstaller_selftest', 'lan_selftest', 'update_selftest', 'gpu_selftest', 'share_selftest', 'node_selftest', 'memopt_selftest', 'toast_selftest', 'screenshare_selftest', 'hostsaccel_selftest'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明确用不到的 Qt 子模块 —— 它们**没有**被任何源码 import，列在这里是
    # 防止 PySide6 的 hook 或某个间接依赖把它们整个拖进来（每个都是几十 MB
    # 量级的 DLL 群）。列出的都是本程序一次都没用到的：
    #   Qml/Quick（纯 QWidget 界面）、WebEngine（无内嵌浏览器）、
    #   Multimedia（无音视频）、Charts/DataVisualization/3D（图表全是 QPainter 手绘）、
    #   Sql（不碰数据库）、Designer/Help、Positioning/Bluetooth/Nfc、Pdf。
    # ⚠️ 两条不能顺手排掉的（v1.0.6 各自踩过一次）：
    #   · `unittest` —— lan_selftest 用 `unittest.mock` 打桩；
    #   · `PySide6.QtTest` —— lan_selftest._check_share_live 里
    #     `from PySide6.QtTest import QTest` 驱动分享页点击。排掉它不会让任何
    #     自检变红，只会让**冻结态的 lan 自检**炸 ModuleNotFoundError，并且被
    #     main 的兜底吞成 rc=5（GUI 子系统没有 stderr，毫无线索）。
    #     hostsaccel_selftest 现在有一条断言专门守这个对应关系。
    excludes=[
        'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets',
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebChannel', 'PySide6.QtWebSockets',
        'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
        'PySide6.QtCharts', 'PySide6.QtDataVisualization',
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput',
        'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning',
        'PySide6.QtSql', 'PySide6.QtDesigner',
        'PySide6.QtHelp', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        'tkinter',
    ],
    noarchive=False,
    optimize=0,
)

# ---- 剔除 PySide6 钩子"整包收集"拖进来、本程序一次都用不到的 Qt 文件 ----
# 这是 58MB 里唯一真正削得动的一块（其余大头是 Qt6Core/Gui/Widgets + OpenSSL
# + 24MB 的 EasyTier 联机核心，都是要用的）。
#
# 判据不是"猜"，是两条实测：
#   ① `hook-PySide6.py` → `QtLibraryInfo.collect_extra_binaries()` 会**无条件**
#      收 `opengl32sw.dll`（19.7MB 软件 OpenGL 渲染器）；而整个仓库
#      OpenGL / QQuick / Qml 的用法是 **grep 零命中**，Qt 也只在显式请求软件 GL
#      时才动态加载它 —— 它在任何 DLL 的 PE 导入表里都不出现。
#   ② Quick / Qml / Pdf / VirtualKeyboard 这几个 DLL 的导入方**全是没被收进来的
#      插件**（designer / qmltooling / webview / platforminputcontexts）。核心链路
#      （qwindows 平台插件 + Qt6Core/Gui/Widgets/Network/Svg/Test + pyside6 +
#      shiboken6）逐个体扫过 PE 导入表，**确认一个都不依赖它们**。
#
# ⚠️ 这几样**不能**剔（各自都有实际用途，剔了当场出问题）：
#    · `qoffscreen.dll` / `qminimal.dll` —— 多个自检用 `QT_QPA_PLATFORM=offscreen`；
#    · `plugins/tls/*` —— QtNetwork 的 HTTPS 后端；
#    · `qsvgicon` / `qsvg` —— QSS 与图标可能走 SVG；
#    · `qtbase_zh_CN.qm` —— main.py 的 QTranslator 唯一会加载的名字。
_DROP_QT_FILES = {
    # 软件 OpenGL 回退：没有任何模块引用它
    "opengl32sw.dll",
    # 只有 QML / Quick 才需要（本程序纯 QWidget）
    "Qt6Qml.dll", "Qt6QmlModels.dll", "Qt6Quick.dll", "Qt6OpenGL.dll",
    # 只有 QtPdf / 虚拟键盘才需要
    "Qt6Pdf.dll", "Qt6VirtualKeyboard.dll",
    # 上面两者的插件：留着会因缺 DLL 加载失败、往日志里刷警告
    "qpdf.dll", "qtvirtualkeyboardplugin.dll",
    # 仅当显式指定 direct2d 平台插件时才加载；项目里零命中
    "qdirect2d.dll",
    # 触摸输入插件（界面没有触摸交互设计）
    "qtuiotouchplugin.dll",
}

# 96 个 Qt 自带翻译（6.4MB）里只留中文：main.py 只装
# `resources/qtbase_zh_CN.qm`，加载失败时的回退名也只有 `qtbase_zh_CN.qm`。
_KEEP_QT_TRANSLATIONS = {"qtbase_zh_CN.qm", "qt_zh_CN.qm"}


def _drop_qt_junk(entries):
    """按 basename 过滤 PyInstaller 的 (src, dst) 列表，保持条目原样。"""
    kept = []
    for entry in entries:
        src = entry[0]
        base = os.path.basename(src)
        norm = src.replace("\\", "/")
        if base in _DROP_QT_FILES:
            continue
        if "/PySide6/translations/" in norm and base not in _KEEP_QT_TRANSLATIONS:
            continue
        kept.append(entry)
    return kept


a.binaries = _drop_qt_junk(a.binaries)
a.datas = _drop_qt_junk(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Yuhub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['resources/app.ico'],
    # ---- 常驻管理员启动（对标 Steam++ 的 app.manifest）----
    #    <requestedExecutionLevel level="requireAdministrator" />
    # 写进 exe 清单后，双击只弹**一次** UAC，之后 hosts 写入 / 卸载残留清理 /
    # 内存优化 / 进房间拉虚拟网卡全都在同一个已提权进程里完成 —— 不再每次
    # 操作都 runas 拉一个新进程（那正是"火绒反复拦、提示 hosts 未被改动"的
    # 根因：HIPS 会把每个"新来的实例"重新审视一遍）。
    uac_admin=True,
)
