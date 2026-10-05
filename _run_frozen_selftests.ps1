# Frozen-state selftests: run the packaged exe for every suite except lan
# (lan pops UAC and needs a virtual NIC; only run it when the P2P code changed).
#   powershell -ExecutionPolicy Bypass -File _run_frozen_selftests.ps1
#
# This script only RUNS the suites and records the exit codes. Parsing the
# JSON is left to `_summarize_selftests.py` (run with bash python) on purpose:
#   * Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so keep it ASCII.
#   * Calling python from inside the PowerShell tool swallows stdout and a
#     single stderr line aborts the whole command -- so no python here at all.
#
# Two traps already paid for here:
# 1) Yuhub.spec has console=False, so the exe is a GUI-subsystem binary.
#    `& $exe ...` does NOT wait for it and leaves $LASTEXITCODE empty -- the
#    first version of this script reported "no result file" for every suite
#    while the exe was still busy extracting itself to %TEMP%.
#    `cmd /c "<exe>" ...` waits properly and propagates the exit code.
# 2) Start-Process is unusable in this environment: with -RedirectStandardOutput
#    it walks ProcessStartInfo.EnvironmentVariables and dies with
#    "Item has already been added. Key in dictionary: 'http_proxy'
#     Key being added: 'HTTP_PROXY'" (the sandbox exports both spellings, and
#    .NET Framework's env dictionary is case-insensitive).
$ErrorActionPreference = "Continue"
$exe = Join-Path $PSScriptRoot "Yuhub.exe"
Set-Location $PSScriptRoot

if (-not (Test-Path $exe)) { Write-Output "missing exe: $exe"; exit 2 }

$suites = @("update", "theme", "uninstall", "gpu", "share", "node",
            "memory", "toast", "screenshare", "hosts")

$bad = @()
foreach ($s in $suites) {
    $out = Join-Path $PSScriptRoot "_fz_$s.json"
    $log = Join-Path $PSScriptRoot "_fz_$s.log"
    if (Test-Path $out) { Remove-Item $out -Force }
    if (Test-Path $log) { Remove-Item $log -Force }
    # A bare "Yuhub.exe" is enough because we already Set-Location to the root;
    # quoting the full path through -f was fragile, so keep it simple.
    $line = 'Yuhub.exe --{0}-selftest "{1}" > "{2}" 2>&1' -f $s, $out, $log
    cmd.exe /c $line | Out-Null
    $rc = $LASTEXITCODE
    if (-not (Test-Path $out)) { $bad += $s }
}

$rcs = @()
foreach ($s in $suites) {
    if (Test-Path (Join-Path $PSScriptRoot "_fz_$s.json")) { $rcs += "1" } else { $rcs += "0" }
}
# Leave a machine-readable marker so the caller can tell "ran" from "did not".
("suites=" + ($suites -join ",")) | Set-Content -Path (Join-Path $PSScriptRoot "_fz_which.txt") -Encoding ASCII
("present=" + ($rcs -join ",")) | Add-Content -Path (Join-Path $PSScriptRoot "_fz_which.txt") -Encoding ASCII

if ($bad.Count -gt 0) {
    ("missing result for: " + ($bad -join ", ")) | Set-Content -Path (Join-Path $PSScriptRoot "_fz_missing.txt") -Encoding ASCII
    exit 1
}
Remove-Item (Join-Path $PSScriptRoot "_fz_missing.txt") -Force -ErrorAction SilentlyContinue
exit 0
