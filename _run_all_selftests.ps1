# 源码态跑全套自检（除 lan：它会弹 UAC 且需要虚拟网卡，只在动了联机代码时才跑）。
#   powershell -ExecutionPolicy Bypass -File _run_all_selftests.ps1
$py = "C:\Users\M1racle\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
Set-Location $PSScriptRoot

$suites = @("update", "theme", "uninstall", "gpu", "share", "node",
            "memory", "toast", "screenshare", "hosts")

$totalPass = 0
$totalAll = 0
$bad = @()
foreach ($s in $suites) {
    $out = "_st_$($s)_all.json"
    if (Test-Path $out) { Remove-Item $out -Force }
    & $py "main.py" "--$s-selftest" $out *> "_st_$($s)_all.log"
    $rc = $LASTEXITCODE
    if (-not (Test-Path $out)) {
        Write-Output ("{0,-12} 结果文件没生成 (rc={1})" -f $s, $rc)
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
Write-Output ("合计 {0}/{1}" -f $totalPass, $totalAll)
if ($bad.Count -gt 0) { Write-Output ("有失败的套件: " + ($bad -join ", ")) }
