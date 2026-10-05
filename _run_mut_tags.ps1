# Re-run a subset of mutation cases by tag.
#   powershell -ExecutionPolicy Bypass -File _run_mut_tags.ps1 -Tags "a,b,c"
#
# Same verdict-file judging as _run_mutation.ps1 (never look at exit codes or
# stderr: every theme case starts a MainWindow whose background thread sometimes
# writes "Signal source has been deleted" to stderr, which makes PowerShell
# raise NativeCommandError and corrupt both $out and $LASTEXITCODE).
#
# Use this when you had to touch source files while a full run was in flight:
# only the cases that started BEFORE the edit are trustworthy.
param([string]$Tags)
$py = "C:\Users\M1racle\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
Set-Location $PSScriptRoot
Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
$env:QT_QPA_PLATFORM = "windows"
$env:PYTHONIOENCODING = "utf-8"

$allok = $true
$bad = @()
foreach ($t in ($Tags -split ',')) {
    $t = $t.Trim()
    if (-not $t) { continue }
    $vf = Join-Path $PSScriptRoot "_mut_verdict_$t.txt"
    $lf = Join-Path $PSScriptRoot "_mut_case_$t.log"
    $verdict = ""
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    for ($try = 1; $try -le 3; $try++) {
        if (Test-Path $vf) { Remove-Item $vf -Force }
        & $py "_probe_mutation.py" $t > $lf 2>&1
        $ErrorActionPreference = $eap
        if (Test-Path $vf) {
            $first = (Get-Content $vf -TotalCount 1)
            if ($first -like "*CAUGHT*") { $verdict = "CAUGHT"; break }
            elseif ($first -like "*MISSED*") { $verdict = "MISSED" }
        }
        $ErrorActionPreference = "SilentlyContinue"
    }
    $ErrorActionPreference = $eap
    Get-Content $lf -ErrorAction SilentlyContinue | Write-Output
    Write-Output ("VERDICT " + $t + " = " + $verdict)
    if ($verdict -ne "CAUGHT") { $allok = $false; $bad += $t }
}
if ($allok) {
    Write-Output "SUBSET: ALL CAUGHT"
    exit 0
} else {
    Write-Output ("SUBSET NOT CAUGHT: " + ($bad -join ", "))
    exit 1
}
