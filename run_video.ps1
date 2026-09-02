$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "✗ 尚未安装运行环境，请先运行 .\setup.ps1" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".\config.toml")) {
    Write-Host "✗ 找不到 config.toml，请先运行 .\run.ps1 创建配置。" -ForegroundColor Red
    exit 1
}

$mutexName = "Local\TDLibMediaUploader_Telegram"
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$hasLock = $false

try {
    try {
        $hasLock = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $hasLock = $true
    }

    if (-not $hasLock) {
        Write-Host ""
        Write-Host "✗ 另一个 TDLib 上传任务正在运行。" -ForegroundColor Red
        Write-Host "  图片和视频脚本共享 tdlib_data / tdlib_files，不能同时运行。" -ForegroundColor DarkGray
        exit 2
    }

    Write-Host ""
    Write-Host "╭────────────────────────────────────────────────────────────╮" -ForegroundColor Cyan
    Write-Host "│  VIDEO · 按月份 Album 上传 · V1.6                               │" -ForegroundColor Cyan
    Write-Host "╰────────────────────────────────────────────────────────────╯" -ForegroundColor Cyan
    Write-Host ""

    & ".\.venv\Scripts\python.exe" ".\tdlib_video_album_uploader.py"
}
finally {
    if ($hasLock) {
        try { $mutex.ReleaseMutex() } catch {}
    }
    $mutex.Dispose()
}
