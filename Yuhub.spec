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
    excludes=[],
    noarchive=False,
    optimize=0,
)
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
)
