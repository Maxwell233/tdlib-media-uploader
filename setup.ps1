$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

Write-Host "=== TDLib Media Uploader v5 Setup ==="
Write-Host ""

if (-not (Test-Path ".venv")) {
    py -3.13 -m venv .venv
}

$python = ".\.venv\Scripts\python.exe"

& $python -m pip install --upgrade pip

# 固定 tdjson 版本，避免 InputFile is not specified 回归问题
& $python -m pip uninstall -y tdjson
& $python -m pip install --no-cache-dir --force-reinstall "tdjson==1.8.64.post1"

# 公共依赖
& $python -m pip install "Pillow>=11.0" "imageio-ffmpeg>=0.6.0" "rich>=13.9.0"

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host ""
Write-Host "以后只需要："
Write-Host "1. 修改 config.toml"
Write-Host "2. 运行 .\run.ps1"
