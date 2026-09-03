[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipInstall,
    [string]$PythonPath = ""
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

function Resolve-PythonCommand {
    param([string]$RequestedPath)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $command = Get-Command $RequestedPath -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return $command.Source
        }
        if (Test-Path -LiteralPath $RequestedPath) {
            return (Resolve-Path -LiteralPath $RequestedPath).Path
        }
        throw "找不到指定的 Python：$RequestedPath"
    }

    $buildPython = Join-Path $PSScriptRoot ".build_venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $buildPython) {
        return $buildPython
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    $venvPath = Join-Path $PSScriptRoot ".build_venv"
    if ($null -ne $launcher) {
        Write-Host "→ 创建构建虚拟环境 .build_venv" -ForegroundColor Yellow
        Invoke-NativeCommand -FilePath $launcher.Source -Arguments @("-3.13", "-m", "venv", $venvPath)
    }
    else {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $python) {
            throw "找不到 Python 3.13。请安装 Python 3.13 x64，或使用 GitHub Actions 构建。"
        }
        Write-Host "→ 创建构建虚拟环境 .build_venv" -ForegroundColor Yellow
        Invoke-NativeCommand -FilePath $python.Source -Arguments @("-m", "venv", $venvPath)
    }

    if (-not (Test-Path -LiteralPath $buildPython)) {
        throw "构建虚拟环境创建失败：$buildPython"
    }
    return $buildPython
}

try {
    $python = Resolve-PythonCommand -RequestedPath $PythonPath
    Write-Host "TDLib Media Uploader V1.7.0 · Windows EXE 构建" -ForegroundColor Cyan
    Write-Host "使用 Python：$python" -ForegroundColor DarkGray

    if (-not $SkipInstall) {
        Write-Host "→ 安装运行与构建依赖" -ForegroundColor Yellow
        Invoke-NativeCommand -FilePath $python -Arguments @("-m", "pip", "install", "--upgrade", "pip")
        Invoke-NativeCommand -FilePath $python -Arguments @("-m", "pip", "install", "--no-cache-dir", "--upgrade", "-r", "requirements-build.txt")
    }

    $buildDir = Join-Path $PSScriptRoot "build\tdlib_media_uploader"
    $distDir = Join-Path $PSScriptRoot "dist\TDLib Media Uploader"
    if ($Clean) {
        foreach ($path in @($buildDir, $distDir)) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Recurse -Force
            }
        }
    }

    Write-Host "→ PyInstaller 生成无控制台窗口的 one-folder 应用" -ForegroundColor Yellow
    Invoke-NativeCommand -FilePath $python -Arguments @(
        "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", ".\dist", "--workpath", ".\build",
        ".\tdlib_media_uploader.spec"
    )

    $version = (Get-Content -Raw -LiteralPath ".\VERSION").Trim()
    $exePath = Join-Path $distDir "TDLib Media Uploader.exe"
    if (-not (Test-Path -LiteralPath $exePath)) {
        throw "构建完成但没有找到 EXE：$exePath"
    }

    $archivePath = Join-Path $PSScriptRoot "dist\TDLib Media Uploader-v$version-windows-x64.zip"
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    Compress-Archive -Path (Join-Path $distDir "*") -DestinationPath $archivePath -CompressionLevel Optimal

    Write-Host "✓ EXE 构建完成" -ForegroundColor Green
    Write-Host "  $exePath" -ForegroundColor Green
    Write-Host "✓ 便携 ZIP 已生成" -ForegroundColor Green
    Write-Host "  $archivePath" -ForegroundColor Green
}
catch {
    Write-Host ""
    Write-Host "✗ EXE 构建失败" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
