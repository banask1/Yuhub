# Frozen-state selftests: run the packaged exe for every suite except lan
# (lan pops UAC and needs a virtual NIC; only run it when the P2P code changed).
#   powershell -ExecutionPolicy Bypass -File _run_frozen_selftests.ps1
#
# Keep this file pure ASCII: Windows PowerShell 5.1 reads a BOM-less .ps1 as
# ANSI, so non-ASCII comments get mangled and can silently break parsing.
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
$py = "C:\Users\M1racle\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
$exe = Join-Path $PSScriptRoot "Yuhub.exe"
Set-Location $PSScriptRoot

if (-not (Test-Path $exe)) { Write-Output "missing exe: $exe"; exit 2 }

$suites = @("update", "theme", "uninstall", "gpu", "share", "node",
            "memory", "toast", "screenshare", "hosts")

$totalPass = 0
$totalAll = 0
$bad = @()
foreach ($s in $suites) {
    $out = Join-Path $PSScriptRoot "_fz_$s.json"
    $log = Join-Path $PSScriptRoot "_fz_$s.log"
    if (Test-Path $out) { Remove-Item $out -Force }
    $line = '"{0}" "--{1}-selftest" "{2}" > "{3}" 2>&1' -f $exe, $s, $out, $log
    & cmd.exe /c $line | Out-Null
    $rc = $LASTEXITCODE
    if (-not (Test-Path $out)) {
        Write-Output ("{0,-12} no result file (rc={1})" -f $s, $rc)
        $bad += $s
        continue
    }
    $j = & $py -c "import json,sys;d=json.load(open(r'$out',encoding='utf-8'));print(len(d['checks']), sum(1 for c in d['checks'] if c['pass']), 1 if d['ok'] else 0)"
    $parts = $j -split '\s+'
    $all = [int]$parts[0]; $pass = [int]$parts[1]; $ok = [int]$parts[2]
    $totalAll += $all
    $totalPass += $pass
    $mark = if ($ok -eq 1) { "OK" } else { "FAIL" }
    Write-Output ("{0,-12} {1,4}/{2,-4} {3}" -f $s, $pass, $all, $mark)
    if ($ok -ne 1) { $bad += $s }
}
Write-Output ("-" * 40)
Write-Output ("total {0}/{1}" -f $totalPass, $totalAll)
if ($bad.Count -gt 0) { Write-Output ("failed suites: " + ($bad -join ", ")) }
if ($bad.Count -gt 0) { exit 1 } else { exit 0 }
