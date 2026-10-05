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
Remove-Item "_mut_verdict_*.txt" -ErrorAction SilentlyContinue
Remove-Item "_mut_case_*.log" -ErrorAction SilentlyContinue

# Tag names are lowercase identifiers only. Filter anything else out: if a
# stray line (a title, a warning on stderr) leaks into this list it gets run
# as a case, fails, and the whole suite is reported as "NOT CAUGHT".
$tags = & $py "_probe_mutation.py" --list | Where-Object { $_ -match '^[a-z][a-z0-9]*$' }
if ($LASTEXITCODE -ne 0) { Write-Output "cannot list cases"; exit 2 }
if (-not $tags) { Write-Output "case list is empty"; exit 2 }

Write-Output "=================================================================="
Write-Output "mutation test: break each implementation, confirm the assertion reds"
Write-Output "=================================================================="

& $py "_probe_mutation.py" base > "_mut_base.log" 2>&1
$baseRc = $LASTEXITCODE
Get-Content "_mut_base.log" -ErrorAction SilentlyContinue | Write-Output
if ($baseRc -ne 0) { Write-Output "!! baseline is not clean"; exit 1 }

$allok = $true
$bad = @()
foreach ($t in $tags) {
    $t = $t.Trim()
    if (-not $t) { continue }
    # 判定用 verdict 文件，不看退出码 / 不吞 stderr。
    # 原因：每个用例进程会起 MainWindow，其后台监视线程偶尔往 stderr 抛
    # "RuntimeError: Signal source has been deleted"，而 PowerShell 的
    # `$out = & $py ... 2>&1` 遇到 native 命令写 stderr 会抛
    # NativeCommandError，$out 与 $LASTEXITCODE 双双失真 —— 用例明明抓住了
    # 却被记成 NOT CAUGHT（v1.0.2 时 hoverqss/hoverleak/hoverchk/edgeguard
    # 四个就这么被冤枉过）。verdict 文件是进程自己写的，最可靠。
    $vf = Join-Path $PSScriptRoot "_mut_verdict_$t.txt"
    $lf = Join-Path $PSScriptRoot "_mut_case_$t.log"
    # 判定用 verdict 文件，不拿退出码/不吞 stderr。
    # 输出走**重定向操作符**写文件（不要用 `2>&1` 管道）：每个用例都会起
    # MainWindow，其后台监视线程退出瞬间偶尔往 stderr 抛
    # "RuntimeError: Signal source has been deleted"；管道一遇到 native
    # 命令写 stderr 就抛 NativeCommandError，让 $out/$LASTEXITCODE 双双失真。
    #
    # 重试一次：MainWindow 相关用例在连续批量执行时会偶发进程崩溃
    # （v1.0.2 实测 edgeguard/hoverqss/hoverleak/hoverchk 四个在批量里
    # MISSED，单独跑全部 CAUGHT）。崩了就让它们单独重跑 —— 判定仍以
    # verdict 文件为准，绝不看退出码。
    $verdict = ""
    # native 命令往 stderr 写任何东西（实测最常见的两个来源：
    # ① Yuhub 自己的 MainWindow 后台线程抛
    #    "RuntimeError: Signal source has been deleted"；
    # ② 宿主环境的删除拦截钩子打印 [safe-delete]...）
    # 都会让 PowerShell 生成 NativeCommandError。默认 $ErrorActionPreference
    # 是 Continue，但 native 命令的 error record 会让**这一行的调用被提前
    # 中断**，python 进程还没写 verdict 就被掐掉 → MIS(SED)=process-died。
    # 所以这里把错误动作临时降级为 SilentlyContinue，跑完立刻还原。
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    for ($try = 1; $try -le 3; $try++) {
        if (Test-Path $vf) { Remove-Item $vf -Force }
        & $py "_probe_mutation.py" $t > $lf 2>&1
        $ErrorActionPreference = $eap
        if (Test-Path $vf) {
            # -like 兜底：万一文件带 BOM，首行会变成 "\ufeffCAUGHT"，
            # 用 -eq 比较会失败（PS 5.1 按 ANSI 读 UTF-8 的老坑）。
            $first = (Get-Content $vf -TotalCount 1)
            if ($first -like "*CAUGHT*") { $verdict = "CAUGHT"; break }
            elseif ($first -like "*MISSED*") { $verdict = "MISSED" }
        }
        $ErrorActionPreference = "SilentlyContinue"
    }
    $ErrorActionPreference = $eap
    Get-Content $lf -ErrorAction SilentlyContinue | Write-Output
    if ($verdict -eq "MISSED") {
        Write-Output "        (retried once; still not caught)"
    }
    if ($verdict -ne "CAUGHT") {
        $allok = $false
        $bad += $t
        if ($verdict -eq "") {
            Write-Output ("        (no verdict file -- process died before judging)")
        }
    }
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
