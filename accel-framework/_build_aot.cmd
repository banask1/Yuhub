@echo off
rem ============================================================================
rem  Build the accel framework (.NET 8 + ASP.NET Core + Native AOT single file)
rem
rem  Output: accel-framework\out\YuhubAccel.exe
rem          Yuhub.spec embeds that file into Yuhub.exe, so it MUST exist first.
rem          accel-framework/out/ is gitignored, so a fresh clone does not have
rem          it -- run this script once before your first build.bat.
rem
rem  Requires: .NET 8 SDK + MSVC (C++ toolchain) + Windows SDK.
rem            Native AOT needs cl.exe / link.exe, so the SDK alone is not
rem            enough. Install "Visual Studio Build Tools" with the
rem            "Desktop development with C++" workload.
rem
rem  Usage: double-click, or from the repo root:
rem            accel-framework\_build_aot.cmd
rem
rem  NOTE: keep this file pure ASCII. cmd reads the batch file using the console
rem        codepage in effect at that moment, so non-ASCII bytes in comments can
rem        be mis-decoded and executed as commands.
rem  NOTE: every variable holding a path is dereferenced through delayed
rem        expansion (!VAR!), so a literal ")" inside the path -- e.g.
rem        "Program Files (x86)" -- can never terminate an if/for block early.
rem  NOTE: "C:\Program Files\dotnet\dotnet.exe" is frequently a runtime-only
rem        install. It resolves fine but "dotnet publish" then dies with
rem        "No .NET SDKs were found", so every candidate is probed with
rem        "--list-sdks" and only a non-empty result is accepted.
rem ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo  Building accel framework (Native AOT single file)
echo  Output: accel-framework\out\YuhubAccel.exe
echo ============================================================
echo.

rem ---- [1/4] locate a dotnet that actually ships an SDK --------------------
set "DOTNET="
call :probe_dotnet "!USERPROFILE!\dotnet-sdk\dotnet.exe"
call :probe_dotnet "D:\TOOL\dotnet-sdk\dotnet.exe"
call :probe_dotnet "C:\Program Files\dotnet\dotnet.exe"
for /f "delims=" %%i in ('where dotnet 2^>nul') do call :probe_dotnet "%%i"
if not defined DOTNET goto no_dotnet
echo [1/4] dotnet : !DOTNET!

rem ---- [2/4] locate Visual Studio / Build Tools ---------------------------
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "!VSWHERE!" goto no_vswhere
set "VSDIR="
for /f "usebackq delims=" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%i"
if not defined VSDIR goto no_vc
set "MSVCVER="
for /f "delims=" %%i in ('dir /b /o-n "!VSDIR!\VC\Tools\MSVC" 2^>nul') do if not defined MSVCVER set "MSVCVER=%%i"
if not defined MSVCVER goto no_vc
set "MSVC=!VSDIR!\VC\Tools\MSVC\!MSVCVER!"
echo [2/4] MSVC   : !MSVC!
goto find_sdk

:no_vswhere
echo [X] vswhere.exe not found.
echo     Install "Visual Studio Build Tools" with the C++ workload.
exit /b 1

:no_vc
echo [X] No MSVC C++ toolchain found in the latest VS installation.
echo     Need the "Desktop development with C++" workload.
exit /b 1

rem ---- [3/4] locate Windows SDK -------------------------------------------
:find_sdk
set "SDKROOT=%ProgramFiles(x86)%\Windows Kits\10"
set "SDKVER="
for /f "delims=" %%i in ('dir /b /o-n "!SDKROOT!\Lib\10.*" 2^>nul') do if not defined SDKVER set "SDKVER=%%i"
if not defined SDKVER goto no_sdk
set "SDKINC=!SDKROOT!\Include\!SDKVER!"
set "SDKLIB=!SDKROOT!\Lib\!SDKVER!"
echo [3/4] WinSDK : !SDKVER!
goto publish

:no_sdk
echo [X] No Windows SDK found under "!SDKROOT!\Lib"
exit /b 1

rem ---- [4/4] publish ------------------------------------------------------
:publish
set "PATH=!MSVC!\bin\Hostx64\x64;!PATH!"
set "LIB=!MSVC!\lib\x64;!SDKLIB!\um\x64;!SDKLIB!\ucrt\x64"
set "INCLUDE=!MSVC!\include;!SDKINC!\um;!SDKINC!\ucrt;!SDKINC!\shared;!SDKINC!\winrt"

echo [4/4] Publishing (Native AOT, first run takes 1-3 minutes)...
"!DOTNET!" publish -c Release -r win-x64 -o out
if errorlevel 1 goto pub_fail
if not exist "out\YuhubAccel.exe" goto no_output

echo.
echo [OK] Built: %CD%\out\YuhubAccel.exe
echo      Now run build.bat from the repository root.
exit /b 0

:no_dotnet
echo [X] No .NET SDK found.
echo     Install the .NET 8 SDK: https://dotnet.microsoft.com/download
echo     A runtime-only install is not enough -- Native AOT needs the SDK.
exit /b 1

:pub_fail
echo.
echo [X] publish failed.
echo     Common causes: missing C++ workload, or a Windows SDK version that is
echo     incompatible with the MSVC version detected above.
exit /b 1

:no_output
echo.
echo [X] publish reported success but out\YuhubAccel.exe is missing.
exit /b 1

rem ============================================================================
rem  Subroutines
rem ============================================================================

rem Keep the first candidate whose "--list-sdks" prints something.
:probe_dotnet
if defined DOTNET exit /b 0
set "CAND=%~1"
if not exist "!CAND!" exit /b 0
set "PROBE=!TEMP!\_yuhub_dotnet_probe.tmp"
"!CAND!" --list-sdks >"!PROBE!" 2>nul
for %%A in ("!PROBE!") do if not %%~zA==0 set "DOTNET=!CAND!"
del /q "!PROBE!" >nul 2>nul
exit /b 0
