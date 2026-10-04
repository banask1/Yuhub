# Rebuild Yuhub.exe from Yuhub.spec.
#   powershell -ExecutionPolicy Bypass -File _run_build.ps1
$py = "C:\Users\M1racle\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
Set-Location $PSScriptRoot
& $py -m PyInstaller --noconfirm --clean Yuhub.spec *> "_build.log"
Write-Output ("build_rc=" + $LASTEXITCODE)
if (Test-Path "dist\Yuhub.exe") {
    Copy-Item "dist\Yuhub.exe" "Yuhub.exe" -Force
    $f = Get-Item "Yuhub.exe"
    Write-Output ("exe=" + $f.Length + " bytes  " + $f.LastWriteTime)
} else {
    Write-Output "no exe produced"
}
