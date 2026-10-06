# Frozen-state LAN selftest only: enters a real cross-network room, starts the
# real EasyTier core, drives the share page, and writes _fz_lan.json.
#   powershell -ExecutionPolicy Bypass -File _run_frozen_lan.ps1
#
# Kept separate from _run_frozen_selftests.ps1 on purpose: this one takes ~30s
# on top of the ~90s of the other ten suites, and background shell tasks in this
# environment get killed around the 2-minute mark -- running both from one script
# reliably loses the lan result.
#
# Why __COMPAT_LAYER: see _run_frozen_selftests.ps1. Same reason here -- Yuhub.exe
# asks for admin, and we do not want a UAC prompt per invocation.
#
# ASCII only (Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI).
$ErrorActionPreference = "Continue"
$env:__COMPAT_LAYER = "RunAsInvoker"
Set-Location $PSScriptRoot

$out = Join-Path $PSScriptRoot "_fz_lan.json"
$log = Join-Path $PSScriptRoot "_fz_lan.log"
if (Test-Path $out) { Remove-Item $out -Force }
if (Test-Path $log) { Remove-Item $log -Force }

cmd.exe /c 'Yuhub.exe --lan-selftest "_fz_lan.json" > "_fz_lan.log" 2>&1' | Out-Null
("lan_rc=" + $LASTEXITCODE) | Set-Content (Join-Path $PSScriptRoot "_fz_lan_rc.txt") -Encoding ASCII
if (-not (Test-Path $out)) {
    Write-Output "missing result: $out"
    exit 1
}
exit 0
