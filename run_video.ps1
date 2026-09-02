$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host ""
    Write-Host "还没有创建虚拟环境，请先运行：" -ForegroundColor Yellow
    Write-Host ".\setup.ps1"
    exit 1
}

# ------------------------------------------------------------
# 共享 TDLib 数据库互斥锁
#
# 视频脚本和图片脚本都使用同一个：
#   tdlib_data
#   tdlib_files
#
# 因此绝对不要同时启动。
#
# 这里使用同一个 Windows Named Mutex。
# 如果另一个上传脚本已经在运行，本脚本会直接退出。
# ------------------------------------------------------------

$mutexName = "Local\TDLibMediaUploader_Telegram"
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$hasLock = $false

try {
    try {
        $hasLock = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        # 上一次脚本被强制关闭后，Windows 会把锁判为 abandoned。
        # 此时当前进程已经获得锁，可以继续。
        $hasLock = $true
    }

    if (-not $hasLock) {
        Write-Host ""
        Write-Host "另一个 TDLib 上传脚本正在运行。" -ForegroundColor Red
        Write-Host "视频脚本和图片脚本不能同时使用同一个 tdlib_data / tdlib_files。"
        Write-Host "请先关闭另一个上传窗口，再运行。"
        exit 2
    }

    Write-Host ""
    Write-Host "=== 启动：视频按月 Album 上传 ===" -ForegroundColor Cyan
    Write-Host ""

    & ".\.venv\Scripts\python.exe" ".\tdlib_video_album_uploader.py"
}
finally {
    if ($hasLock) {
        try {
            $mutex.ReleaseMutex()
        }
        catch {
        }
    }

    $mutex.Dispose()
}
