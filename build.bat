@echo off
cd /d "%~dp0"
chcp 65001 >nul

set "PY=C:\Users\M1racle\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

echo [1/2] Generating icon...
"%PY%" build_icon.py

echo [2/2] Building exe with PyInstaller...
rem resources must be bundled: app.ico is the icon, qtbase_zh_CN.qm is the
rem Qt Chinese translation (without it the input right-click menu falls back
rem to Qt's built-in English strings).
rem
rem --hidden-import is required for modules only imported inside functions:
rem theme_selftest / uninstaller_selftest / lan_selftest / update_selftest
rem are pulled in lazily by main.py's --*-selftest modes, so PyInstaller's
rem static scan misses them.
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name Yuhub ^
  --icon resources\app.ico ^
  --add-data "resources;resources" ^
  --hidden-import theme_selftest ^
  --hidden-import uninstaller_selftest ^
  --hidden-import lan_selftest ^
  --hidden-import update_selftest ^
  main.py

echo.
echo Done! Output: dist\Yuhub.exe
pause
