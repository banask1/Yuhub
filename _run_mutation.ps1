# Mutation test: break each implementation on purpose and confirm the
# corresponding assertion actually turns red.
#   powershell -ExecutionPolicy Bypass -File _run_mutation.ps1
#
# One python process per case (see the docstring of do_one in _probe_mutation.py:
# each case spins up a MainWindow with a background monitor thread; running a
# dozen of them in one process eventually kills it, and the log just stops).
#
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads a .ps1 without
# a BOM as ANSI, so non-ASCII comments get mangled and can break parsing
# (a mangled line silently swallows the $py assignment -> "BadExpression").
#
# Must run under PowerShell, not bash: theme_selftest starts MainWindow, whose
# hardware scan spawns a PowerShell child process. Inside the bash sandbox that
# trips the security policy and the process is killed (exit 127).
$py = "C:\Users\M1racle\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
Set-Location $PSScriptRoot
Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
$env:QT_QPA_PLATFORM = "windows"
# python's stdout defaults to the ANSI codepage (GBK here); the result lines
# contain a non-ASCII check mark, which makes print() raise UnicodeEncodeError
# and kills every case right after the selftest ran (the log then looks like
# "no assertion caught anything"). Force UTF-8 for stdout/stderr.
$env:PYTHONIOENCODING = "utf-8"
Remove-Item "_mutation_report.txt" -ErrorAction SilentlyContinue

# Tag names are lowercase identifiers only. Filter anything else out: if a
# stray line (a title, a warning on stderr) leaks into this list it gets run
# as a case, fails, and the whole suite is reported as "NOT CAUGHT".
$tags = & $py "_probe_mutation.py" --list | Where-Object { $_ -match '^[a-z][a-z0-9]*$' }
if ($LASTEXITCODE -ne 0) { Write-Output "cannot list cases"; exit 2 }
if (-not $tags) { Write-Output "case list is empty"; exit 2 }

Write-Output "=================================================================="
Write-Output "mutation test: break each implementation, confirm the assertion reds"
Write-Output "=================================================================="

& $py "_probe_mutation.py" base 2>&1 | Write-Output
if ($LASTEXITCODE -ne 0) { Write-Output "!! baseline is not clean"; exit 1 }

$allok = $true
$bad = @()
foreach ($t in $tags) {
    $t = $t.Trim()
    if (-not $t) { continue }
    $out = & $py "_probe_mutation.py" $t 2>&1
    $rc = $LASTEXITCODE
    $out | Write-Output
    if ($rc -ne 0) { $allok = $false; $bad += $t }
}

Write-Output ""
Write-Output "=================================================================="
if ($allok) {
    Write-Output "ALL mutations caught by their assertions"
} else {
    Write-Output ("NOT CAUGHT: " + ($bad -join ", ") + " -- those assertions are decoration")
}
Write-Output "=================================================================="
if ($allok) { exit 0 } else { exit 1 }
