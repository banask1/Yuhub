# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # memopt.py 单独带上：它的自检要做 AST 静态扫描，确认代码里没有任何
    # "结束进程"的调用 —— 这是"内存优化绝不关掉用户程序"这条承诺的静态
    # 证据。PyInstaller 默认只把 .pyc 收进 PYZ，源码不可读，扫描会失效。
    datas=[('resources', 'resources'), ('memopt.py', '.')],
    hiddenimports=['theme_selftest', 'uninstaller_selftest', 'lan_selftest', 'update_selftest', 'gpu_selftest', 'share_selftest', 'node_selftest', 'memopt_selftest', 'toast_selftest'],
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
