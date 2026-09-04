$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Show-Banner {
    Clear-Host
    $line = "─" * 64
    Write-Host $line -ForegroundColor Cyan
    Write-Host "  TDLib Media Uploader  V1.8.2" -ForegroundColor Cyan
    Write-Host "  Telegram 批量图片 / 视频 Album GUI" -ForegroundColor DarkCyan
    Write-Host "  作者署名：Maximum · 2026" -ForegroundColor DarkGray
    Write-Host $line -ForegroundColor Cyan
    Write-Host ""
}

try {
    Show-Banner

    if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
        Write-Host "✗ 尚未完成环境安装。" -ForegroundColor Red
        Write-Host "  请先双击 setup.cmd，或运行 .\setup.ps1" -ForegroundColor DarkGray
        throw "运行环境不存在：.venv\Scripts\python.exe"
    }

    if (-not (Test-Path ".\config.toml")) {
        if (-not (Test-Path ".\config.example.toml")) {
            throw "找不到 config.example.toml，项目文件不完整。"
        }

        Copy-Item ".\config.example.toml" ".\config.toml"
        Write-Host "✓ 已根据 config.example.toml 创建 config.toml" -ForegroundColor Green
        Write-Host "  首次运行请在 GUI 的设置页填写 Telegram API、群组、Topic 和目录。" -ForegroundColor DarkGray
    }

    Write-Host "→ 启动 PySide6 GUI" -ForegroundColor Yellow
    Write-Host ""
    & ".\.venv\Scripts\python.exe" ".\gui_app.py"
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        throw "GUI 异常退出，Python 退出代码：$exitCode"
    }
}
catch {
    Write-Host ""
    Write-Host ("─" * 64) -ForegroundColor Red
    Write-Host "GUI 启动失败：请查看上方错误信息" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ("─" * 64) -ForegroundColor Red
    exit 1
}

exit 0
