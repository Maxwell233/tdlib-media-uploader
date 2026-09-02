$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$exitCode = 0
$mutex = $null
$hasLock = $false

try {
    if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
        throw "尚未安装运行环境，请先运行 setup.cmd 或 .\setup.ps1"
    }

    if (-not (Test-Path ".\config.toml")) {
        throw "找不到 config.toml，请先运行 .\run.ps1 创建配置。"
    }

    $mutexName = "Local\TDLibMediaUploader_Telegram"
    $mutex = New-Object System.Threading.Mutex($false, $mutexName)

    try {
        $hasLock = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $hasLock = $true
    }

    if (-not $hasLock) {
        throw "另一个 TDLib 上传任务正在运行。图片和视频脚本共享 tdlib_data / tdlib_files，不能同时运行。"
    }

    Write-Host ""
    Write-Host "╭────────────────────────────────────────────────────────────╮" -ForegroundColor Magenta
    Write-Host "│  IMAGE · 10 张一组 Album 上传 · V1.6                      │" -ForegroundColor Magenta
    Write-Host "╰────────────────────────────────────────────────────────────╯" -ForegroundColor Magenta
    Write-Host ""

    & ".\.venv\Scripts\python.exe" ".\tdlib_image_album_uploader.py"
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        throw "图片上传器异常退出，Python 退出代码：$exitCode"
    }
}
catch {
    Write-Host ""
    Write-Host "✗ 图片上传器运行失败" -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red

    if ($exitCode -eq 0) {
        $exitCode = 1
    }
}
finally {
    if ($hasLock -and $null -ne $mutex) {
        try { $mutex.ReleaseMutex() } catch {}
    }

    if ($null -ne $mutex) {
        $mutex.Dispose()
    }
}

exit $exitCode
