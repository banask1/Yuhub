@echo off
cd /d "%~dp0"
chcp 65001 >nul

set "PY=C:\Users\M1racle\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

echo [1/2] Generating icon...
"%PY%" build_icon.py

echo [2/2] Building exe with PyInstaller...
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
