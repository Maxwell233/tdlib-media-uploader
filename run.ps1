$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Show-Banner {
    Clear-Host
    $line = "─" * 64
    Write-Host $line -ForegroundColor Cyan
    Write-Host "  TDLib Media Uploader  V1.6.3" -ForegroundColor Cyan
    Write-Host "  Telegram 批量图片 / 视频 Album 上传" -ForegroundColor DarkCyan
    Write-Host "  Copyright © 2026 Maximum. All rights reserved." -ForegroundColor DarkGray
    Write-Host $line -ForegroundColor Cyan
    Write-Host ""
}

function Ensure-Config {
    if (Test-Path ".\config.toml") {
        return
    }

    if (-not (Test-Path ".\config.example.toml")) {
        throw "找不到 config.example.toml，项目文件不完整。"
    }

    Copy-Item ".\config.example.toml" ".\config.toml"

    Write-Host "✓ 已根据 config.example.toml 创建 config.toml" -ForegroundColor Green
    Write-Host "  请填写 API_ID / API_HASH / CHAT_ID / FORUM_TOPIC_ID 和本地目录。" -ForegroundColor DarkGray
    Write-Host ""

    notepad "$PSScriptRoot\config.toml"

    Write-Host ""
    Write-Host "配置文件已创建。保存后可直接继续使用当前菜单。" -ForegroundColor Yellow
    [void](Read-Host "按 Enter 继续")
}

function Invoke-Uploader {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ScriptName,

        [Parameter(Mandatory = $true)]
        [string]$DisplayName
    )

    Write-Host ""
    Write-Host "→ 启动 $DisplayName" -ForegroundColor Yellow
    Write-Host ""

    # 上传脚本在独立 PowerShell 子进程中运行，避免子脚本 exit 关闭主菜单。
    & powershell.exe `
        -NoLogo `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File "$PSScriptRoot\$ScriptName"

    $code = $LASTEXITCODE

    Write-Host ""

    if ($code -eq 0) {
        Write-Host "✓ $DisplayName 已结束。" -ForegroundColor Green
    }
    else {
        Write-Host "✗ $DisplayName 异常结束，退出代码：$code" -ForegroundColor Red
        Write-Host "  上方错误信息已保留，不会再一闪而过。" -ForegroundColor DarkGray
    }

    Write-Host ""
    [void](Read-Host "按 Enter 返回主菜单")
}

try {
    if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
        Show-Banner
        Write-Host "✗ 尚未完成环境安装。" -ForegroundColor Red
        Write-Host "  请先双击 setup.cmd，或运行：" -ForegroundColor DarkGray
        Write-Host "  .\setup.ps1" -ForegroundColor Yellow
        throw "运行环境不存在：.venv\Scripts\python.exe"
    }

    Ensure-Config

    while ($true) {
        Show-Banner

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
                Invoke-Uploader `
                    -ScriptName "run_video.ps1" `
                    -DisplayName "视频上传器"
            }

            "2" {
                Invoke-Uploader `
                    -ScriptName "run_image.ps1" `
                    -DisplayName "图片上传器"
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
                Start-Sleep -Seconds 1
            }
        }
    }
}
catch {
    Write-Host ""
    Write-Host ("─" * 64) -ForegroundColor Red
    Write-Host "运行失败" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ("─" * 64) -ForegroundColor Red
    exit 1
}
