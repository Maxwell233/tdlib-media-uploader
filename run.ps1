$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Write-Line {
    param([string]$Text = "")
    Write-Host $Text
}

function Ensure-Config {
    if (-not (Test-Path ".\config.toml")) {
        if (-not (Test-Path ".\config.example.toml")) {
            Write-Host "✗ 找不到 config.example.toml，项目文件不完整。" -ForegroundColor Red
            exit 1
        }

        Copy-Item ".\config.example.toml" ".\config.toml"
        Write-Host ""
        Write-Host "✓ 已根据 config.example.toml 创建 config.toml" -ForegroundColor Green
        Write-Host "  请填写 API_ID / API_HASH / CHAT_ID / FORUM_TOPIC_ID 和本地目录。" -ForegroundColor DarkGray
        Write-Host ""
        notepad "$PSScriptRoot\config.toml"
        Write-Host ""
        Write-Host "保存配置后，请重新运行 .\run.ps1。" -ForegroundColor Yellow
        exit 0
    }
}

Clear-Host
Write-Host "╭────────────────────────────────────────────────────────────╮" -ForegroundColor Cyan
Write-Host "│              TDLib Media Uploader  V1.6                    │" -ForegroundColor Cyan
Write-Host "│           Telegram 批量图片 / 视频 Album 上传              │" -ForegroundColor DarkCyan
Write-Host "╰────────────────────────────────────────────────────────────╯" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "✗ 尚未完成环境安装。" -ForegroundColor Red
    Write-Host "  请先运行：" -ForegroundColor DarkGray
    Write-Host "  .\setup.ps1" -ForegroundColor Yellow
    exit 1
}

Ensure-Config

Write-Host "  [1]  上传视频   " -NoNewline -ForegroundColor Cyan
Write-Host "按月份组成 Album，首条显示 yy-m"
Write-Host "  [2]  上传图片   " -NoNewline -ForegroundColor Magenta
Write-Host "每 10 张一个 Album，无 Caption"
Write-Host "  [3]  编辑配置   " -NoNewline -ForegroundColor Yellow
Write-Host "打开 config.toml"
Write-Host "  [4]  退出"
Write-Host ""

$choice = Read-Host "请选择"

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
        Write-Host ""
        Write-Host "已退出。" -ForegroundColor DarkGray
        exit 0
    }
    default {
        Write-Host ""
        Write-Host "✗ 输入无效，请输入 1、2、3 或 4。" -ForegroundColor Red
        exit 1
    }
}
