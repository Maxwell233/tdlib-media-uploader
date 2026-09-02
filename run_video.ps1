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
    Write-Host "→ VIDEO · 按月份 Album 上传" -ForegroundColor Cyan
    Write-Host ""

    # 视频断点固定写入 .video_state。
    # 不做旧 .state 自动迁移。
    $env:TDLIB_VIDEO_STATE_DIR = "$PSScriptRoot\.video_state"

    # 使用内嵌 Python 入口设置视频断点目录。
    # Ctrl+C 在这里捕获，避免 Python 输出 KeyboardInterrupt Traceback。
    $pythonCode = @'
import os
from pathlib import Path
import tdlib_video_album_uploader as core

core.STATE_DIR = Path(os.environ['TDLIB_VIDEO_STATE_DIR'])

import tdlib_video_app

try:
    tdlib_video_app.main()
except KeyboardInterrupt:
    core.UI.finish()
    core.UI.warning(
        '已手动停止。已完成 Album 下次自动跳过；当前未完成 Album 下次重新处理。'
    )
'@

    & ".\.venv\Scripts\python.exe" -c $pythonCode
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        throw "视频上传器异常退出，Python 退出代码：$exitCode"
    }
}
catch {
    Write-Host ""
    Write-Host "✗ 视频上传器运行失败" -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red

    if ($exitCode -eq 0) {
        $exitCode = 1
    }
}
finally {
    Remove-Item Env:TDLIB_VIDEO_STATE_DIR -ErrorAction SilentlyContinue

    if ($hasLock -and $null -ne $mutex) {
        try { $mutex.ReleaseMutex() } catch {}
    }

    if ($null -ne $mutex) {
        $mutex.Dispose()
    }
}

exit $exitCode
