@echo off
cd /d "%~dp0"
chcp 65001 >nul

set "PY=C:\Users\M1racle\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/3] Ensuring accel framework is built...
rem Yuhub.spec needs accel-framework\out\YuhubAccel.exe -- that Native AOT
rem single file is embedded into the exe (accel\) and released to LOCALAPPDATA
rem when the user opens the "Network Acceleration" page.
rem accel-framework/out/ is gitignored, so a FRESH CLONE DOES NOT HAVE IT and
rem packaging would fail with a missing-file error. Run _build_aot.cmd once.
rem Requires: .NET 8 SDK + MSVC ("Desktop development with C++" workload).
rem NOTE: keep comments in this file pure ASCII. cmd reads the batch file in
rem chunks and re-seeks per line; a multi-byte character landing on a chunk
rem boundary gets mis-decoded and executed as a command. Comments below that
rem are still Chinese predate this note and may emit harmless noise.
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

echo [2/3] Generating icon...
"%PY%" build_icon.py

echo [3/3] Building exe with PyInstaller...
rem 打包参数统一放在 Yuhub.spec 里（单一来源）。以前这里另抄了一份命令行
rem 参数，结果 --hidden-import 列表跟不上（gpu/share/node 三套自检都漏了，
rem 打包后 --*-selftest 会 ImportError），所以改成直接吃 spec。
rem
rem resources 必须打进去：app.ico 是图标，qtbase_zh_CN.qm 是 Qt 中文翻译
rem （没有它输入框右键菜单会退回 Qt 内置的英文串）。
"%PY%" -m PyInstaller --noconfirm --clean Yuhub.spec

echo.
echo Done! Output: dist\Yuhub.exe
pause
