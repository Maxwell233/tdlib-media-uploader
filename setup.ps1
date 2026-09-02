$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Clear-Host
Write-Host "╭────────────────────────────────────────────────────────────╮" -ForegroundColor Cyan
Write-Host "│        TDLib Media Uploader V1.6 · 初始环境安装            │" -ForegroundColor Cyan
Write-Host "╰────────────────────────────────────────────────────────────╯" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path ".venv")) {
    Write-Host "→ 创建 Python 虚拟环境 .venv" -ForegroundColor Yellow
    py -3.13 -m venv .venv
}
else {
    Write-Host "✓ 已存在 .venv，继续使用。" -ForegroundColor Green
}

$python = ".\.venv\Scripts\python.exe"

Write-Host "→ 更新 pip" -ForegroundColor Yellow
& $python -m pip install --upgrade pip

Write-Host "→ 安装固定版本 tdjson 1.8.64.post1" -ForegroundColor Yellow
& $python -m pip uninstall -y tdjson | Out-Null
& $python -m pip install --no-cache-dir --force-reinstall "tdjson==1.8.64.post1"

Write-Host "→ 安装 Pillow / imageio-ffmpeg / Rich" -ForegroundColor Yellow
& $python -m pip install "Pillow>=11.0" "imageio-ffmpeg>=0.6.0" "rich>=13.9.0"

if (-not (Test-Path ".\config.toml")) {
    Copy-Item ".\config.example.toml" ".\config.toml"
    Write-Host "✓ 已创建 config.toml。" -ForegroundColor Green
}
else {
    Write-Host "✓ 保留现有 config.toml，不会覆盖你的配置。" -ForegroundColor Green
}

Write-Host ""
Write-Host "╭────────────────────── 安装完成 ────────────────────────────╮" -ForegroundColor Green
Write-Host "│  1. 编辑 config.toml                                      │" -ForegroundColor Green
Write-Host "│  2. 如需读取视频 EXIF/QuickTime，可安装 tools\exiftool.exe │" -ForegroundColor Green
Write-Host "│  3. 运行 .\run.ps1                                        │" -ForegroundColor Green
Write-Host "╰────────────────────────────────────────────────────────────╯" -ForegroundColor Green
Write-Host ""
Write-Host "提示：默认 missing_date_policy = `"mtime`"，没有 ExifTool 也可上传视频。" -ForegroundColor DarkGray
