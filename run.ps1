$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "          TDLib Media Uploader v5" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. 上传视频（按 EXIF / QuickTime 月份分 Album）"
Write-Host "2. 上传图片（每 10 张一个 Album，无 Caption）"
Write-Host "3. 打开配置文件 config.toml"
Write-Host "4. 退出"
Write-Host ""

$choice = Read-Host "请选择 1 / 2 / 3 / 4"

switch ($choice) {
    "1" {
        & "$PSScriptRoot\run_video.ps1"
    }

    "2" {
        & "$PSScriptRoot\run_image.ps1"
    }

    "3" {
        notepad "$PSScriptRoot\config.toml"
    }

    "4" {
        Write-Host "已退出。"
        exit 0
    }

    default {
        Write-Host ""
        Write-Host "输入无效，只能输入 1、2、3 或 4。" -ForegroundColor Yellow
        exit 1
    }
}
