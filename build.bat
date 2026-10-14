@echo off
cd /d "%~dp0"
chcp 65001 >nul

set "PY=C:\Users\M1racle\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem ---- optional clean build:  build.bat clean ------------------------------
rem PyInstaller's --clean is deliberately NOT used any more: it hangs at
rem   "Removing temporary files and cleaning cache in AppData\Local\pyinstaller"
rem (reproduced 3 times -- process alive, zero CPU, never returns).
rem Passing "clean" removes build\ and dist\ ourselves instead, which is the
rem recovery procedure that actually works. Without the argument the build is
rem incremental, which is fine for normal day-to-day packaging.
if /i "%~1"=="clean" (
    echo [0/3] Clean build requested - removing build\ and dist\ ...
    if exist "build" rmdir /s /q "build"
    if exist "dist" rmdir /s /q "dist"
)

echo [1/4] Ensuring accel framework is built...
rem Yuhub.spec needs accel-framework\out\YuhubAccel.exe -- that Native AOT
rem single file is embedded into the exe (accel\) and released to LOCALAPPDATA
rem when the user opens the "Network Acceleration" page.
rem accel-framework/out/ is gitignored, so a FRESH CLONE DOES NOT HAVE IT and
rem packaging would fail with a missing-file error. Run _build_aot.cmd once.
rem Requires: .NET 8 SDK + MSVC ("Desktop development with C++" workload).
rem NOTE: keep the comments in this file ASCII-only. cmd reads a batch file in
rem byte chunks and re-seeks per line, so a multi-byte character landing on a
rem chunk boundary gets mis-decoded and executed as a command. The Chinese
rem comments further down are pre-existing and may emit harmless noise.
if not exist "accel-framework\out\YuhubAccel.exe" (
    echo       missing, running accel-framework\_build_aot.cmd ...
    call "accel-framework\_build_aot.cmd"
    if errorlevel 1 (
        echo.
        echo [X] Accel framework build failed - cannot package Yuhub.exe.
        echo     Install .NET 8 SDK + the C++ desktop workload, or run
        echo     accel-framework\_build_aot.cmd manually to see the error.
        pause
        exit /b 1
    )
) else (
    echo       ok
)

echo [2/4] Checking PingFang font...
rem The subsetted font (8.85 MB) is committed under resources/fonts, so this
rem normally just says ok. The download below is only a fallback for the case
rem where the file was deleted -- without it the exe would SILENTLY fall back
rem to Microsoft YaHei UI (install_app_font verifies registration and would
rem otherwise look like "the font change did nothing").
if not exist "resources\fonts\PingFangSC-Regular.otf" (
    echo       missing, downloading from the jsDelivr mirror ...
    if not exist "resources\fonts" mkdir "resources\fonts"
    curl -sSL --max-time 300 -o "resources\fonts\PingFangSC-Regular.otf" https://cdn.jsdelivr.net/gh/ZWolken/PingFang@main/PingFangSC-Regular.otf
    if errorlevel 1 (
        echo [X] Font download failed - the packaged exe would fall back to
        echo     Microsoft YaHei UI without any error. Fix the network and re-run.
        pause
        exit /b 1
    )
    echo       note: this is the FULL font ^(~13 MB^), not the committed subset
    echo             ^(~8.85 MB^) - the exe will be about 4 MB larger.
) else (
    echo       ok
)

echo [3/4] Generating icon...
"%PY%" build_icon.py

echo [4/4] Building exe with PyInstaller...
rem 打包参数统一放在 Yuhub.spec 里（单一来源）。以前这里另抄了一份命令行
rem 参数，结果 --hidden-import 列表跟不上（gpu/share/node 三套自检都漏了，
rem 打包后 --*-selftest 会 ImportError），所以改成直接吃 spec。
rem
rem resources 必须打进去：app.ico 是图标，qtbase_zh_CN.qm 是 Qt 中文翻译
rem （没有它输入框右键菜单会退回 Qt 内置的英文串）。
rem --clean is intentionally absent -- it hangs randomly (see the note near the
rem top of this file). For a from-scratch build use:  build.bat clean
"%PY%" -m PyInstaller --noconfirm Yuhub.spec

echo.
echo Done! Output: dist\Yuhub.exe
pause
