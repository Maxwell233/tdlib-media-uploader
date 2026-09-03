param(
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $false)]
        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments

    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败（退出码 $LASTEXITCODE）：$FilePath $($Arguments -join ' ')"
    }
}

$setupFailed = $false

try {
    Clear-Host

    $line = "─" * 64
    Write-Host $line -ForegroundColor Cyan
    Write-Host "  TDLib Media Uploader V1.6.4 · 初始环境安装" -ForegroundColor Cyan
    Write-Host $line -ForegroundColor Cyan
    Write-Host ""

    if (-not (Test-Path ".venv")) {
        Write-Host "→ 创建 Python 3.13 虚拟环境 .venv" -ForegroundColor Yellow
        Invoke-NativeCommand -FilePath "py" -Arguments @("-3.13", "-m", "venv", ".venv")
    }
    else {
        Write-Host "✓ 已存在 .venv，继续使用。" -ForegroundColor Green
    }

    $python = ".\.venv\Scripts\python.exe"

    if (-not (Test-Path $python)) {
        throw "没有找到 $python。请确认已安装 Python 3.13 x64；如 .venv 已损坏，可删除 .venv 后重新运行 setup.ps1。"
    }

    Write-Host "→ 更新 pip" -ForegroundColor Yellow
    Invoke-NativeCommand -FilePath $python -Arguments @("-m", "pip", "install", "--upgrade", "pip")

    Write-Host "→ 安装项目依赖" -ForegroundColor Yellow
    Write-Host "  tdjson 1.8.64.post1 / Pillow / imageio-ffmpeg / Rich" -ForegroundColor DarkGray
    Invoke-NativeCommand -FilePath $python -Arguments @("-m", "pip", "install", "--no-cache-dir", "--upgrade", "-r", "requirements.txt")

    Write-Host "→ 检查 tdjson 固定版本" -ForegroundColor Yellow
    $tdjsonVersion = & $python -c "import importlib.metadata; print(importlib.metadata.version('tdjson'))"

    if ($LASTEXITCODE -ne 0) {
        throw "无法读取 tdjson 版本。"
    }

    $tdjsonVersion = $tdjsonVersion.Trim()

    if ($tdjsonVersion -ne "1.8.64.post1") {
        Write-Host "  当前 tdjson=$tdjsonVersion，正在强制修正为 1.8.64.post1" -ForegroundColor Yellow
        Invoke-NativeCommand -FilePath $python -Arguments @("-m", "pip", "install", "--no-cache-dir", "--force-reinstall", "tdjson==1.8.64.post1")
    }
    else {
        Write-Host "✓ tdjson 版本正确：1.8.64.post1" -ForegroundColor Green
    }

    if (-not (Test-Path ".\config.toml")) {
        if (-not (Test-Path ".\config.example.toml")) {
            throw "找不到 config.example.toml，无法创建配置文件。"
        }

        Copy-Item ".\config.example.toml" ".\config.toml"
        Write-Host "✓ 已创建 config.toml。" -ForegroundColor Green
    }
    else {
        Write-Host "✓ 保留现有 config.toml，不会覆盖你的配置。" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host ("─" * 64) -ForegroundColor Green
    Write-Host "安装完成" -ForegroundColor Green
    Write-Host "  1. 编辑 config.toml"
    Write-Host "  2. 如需读取 EXIF/QuickTime，可安装 tools\exiftool.exe"
    Write-Host "  3. 双击 run.cmd，或运行 .\run.ps1"
    Write-Host ("─" * 64) -ForegroundColor Green
    Write-Host ""
    Write-Host "提示：默认 missing_date_policy = `"mtime`"，没有 ExifTool 也可上传视频。" -ForegroundColor DarkGray
}
catch {
    $setupFailed = $true

    Write-Host ""
    Write-Host ("─" * 64) -ForegroundColor Red
    Write-Host "安装失败" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ("─" * 64) -ForegroundColor Red
    Write-Host ""
    Write-Host "常见处理：确认 Python 3.13 x64 已安装，或删除损坏的 .venv 后重试。" -ForegroundColor DarkYellow
}
finally {
    if (-not $NoPause) {
        Write-Host ""
        [void](Read-Host "按 Enter 键关闭此窗口")
    }
}

if ($setupFailed) {
    exit 1
}
