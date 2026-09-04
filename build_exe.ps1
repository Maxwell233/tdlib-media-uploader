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

$FfmpegReleaseTag = "autobuild-2026-09-03-13-17"
$FfmpegArchiveName = "ffmpeg-N-126390-g9fc8c785e2-win64-lgpl.zip"
$FfmpegArchiveSha256 = "ba8bf7dec00022c2dbf2cbeb9a601d7e0d131990e276b8c5f88954775735ec8a"
$FfmpegArchiveUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/$FfmpegReleaseTag/$FfmpegArchiveName"

function Ensure-LgplFfmpeg {
    $ffmpegDirectory = Join-Path $PSScriptRoot "tools\ffmpeg"
    $ffmpegPath = Join-Path $ffmpegDirectory "ffmpeg.exe"
    $licensePath = Join-Path $ffmpegDirectory "LICENSE.txt"

    if ((Test-Path -LiteralPath $ffmpegPath) -and (Test-Path -LiteralPath $licensePath)) {
        return (Resolve-Path -LiteralPath $ffmpegPath).Path
    }

    Write-Host "→ 下载固定版本 LGPL FFmpeg（BtbN $FfmpegReleaseTag）" -ForegroundColor Yellow
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "tdlib-media-uploader-ffmpeg"
    $archivePath = Join-Path $tempRoot $FfmpegArchiveName
    $extractPath = Join-Path $tempRoot ("extract-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null

    try {
        $download = $true
        if (Test-Path -LiteralPath $archivePath) {
            $existingHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
            $download = $existingHash -ne $FfmpegArchiveSha256
            if ($download) {
                Remove-Item -LiteralPath $archivePath -Force
            }
        }

        if ($download) {
            Invoke-WebRequest -Uri $FfmpegArchiveUrl -OutFile $archivePath -UseBasicParsing
        }

        $archiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
        if ($archiveHash -ne $FfmpegArchiveSha256) {
            throw "FFmpeg 压缩包 SHA-256 校验失败：$archiveHash"
        }

        Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath -Force
        $sourceFfmpeg = Get-ChildItem -LiteralPath $extractPath -Filter "ffmpeg.exe" -File -Recurse | Select-Object -First 1
        $sourceLicense = Get-ChildItem -LiteralPath $extractPath -Filter "LICENSE.txt" -File -Recurse | Select-Object -First 1
        if ($null -eq $sourceFfmpeg -or $null -eq $sourceLicense) {
            throw "FFmpeg 压缩包缺少 ffmpeg.exe 或 LICENSE.txt"
        }

        New-Item -ItemType Directory -Force -Path $ffmpegDirectory | Out-Null
        Copy-Item -LiteralPath $sourceFfmpeg.FullName -Destination $ffmpegPath -Force
        Copy-Item -LiteralPath $sourceLicense.FullName -Destination $licensePath -Force
        return (Resolve-Path -LiteralPath $ffmpegPath).Path
    }
    finally {
        if (Test-Path -LiteralPath $extractPath) {
            Remove-Item -LiteralPath $extractPath -Recurse -Force
        }
    }
}

try {
    $python = Resolve-PythonCommand -RequestedPath $PythonPath
    Write-Host "TDLib Media Uploader V1.8.0 · Windows EXE 构建" -ForegroundColor Cyan
    Write-Host "使用 Python：$python" -ForegroundColor DarkGray

    $iconPath = Join-Path $PSScriptRoot "assets\tdlib_media_uploader_icon.ico"
    if (-not (Test-Path -LiteralPath $iconPath)) {
        throw "找不到应用图标：$iconPath"
    }

    if (-not $SkipInstall) {
        Write-Host "→ 安装运行与构建依赖" -ForegroundColor Yellow
        Invoke-NativeCommand -FilePath $python -Arguments @("-m", "pip", "install", "--upgrade", "pip")
        Invoke-NativeCommand -FilePath $python -Arguments @("-m", "pip", "install", "--no-cache-dir", "--upgrade", "--force-reinstall", "--no-binary", "imageio-ffmpeg", "-r", "requirements-build.txt")
    }

    $ffmpegPath = Ensure-LgplFfmpeg
    Write-Host "→ 检查 FFmpeg 构建许可标志：$ffmpegPath" -ForegroundColor Yellow
    $ffmpegVersion = (& $ffmpegPath "-version" 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取 FFmpeg 构建信息：$ffmpegPath"
    }
    if ($ffmpegVersion -match "--enable-gpl" -or $ffmpegVersion -match "--enable-nonfree") {
        throw "检测到 FFmpeg 启用了 GPL 或 nonfree 构建选项。请改用可再分发的 LGPL 构建后再发布。"
    }
    $ffmpegLicensePath = Join-Path $PSScriptRoot "tools\ffmpeg\LICENSE.txt"
    $ffmpegLicense = Get-Content -Raw -LiteralPath $ffmpegLicensePath
    if ($ffmpegLicense -notmatch "LESSER GENERAL PUBLIC LICENSE") {
        throw "FFmpeg 许可文件不是 LGPL 文本：$ffmpegLicensePath"
    }
    Write-Host "✓ 已验证 LGPL FFmpeg，未报告 GPL/nonfree 构建标志" -ForegroundColor Green

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

    # Keep the project license, author attribution and third-party index beside
    # the executable even if a future PyInstaller version changes how
    # extensionless data files are collected from the spec file.
    foreach ($noticeName in @("LICENSE", "ATTRIBUTION", "THIRD_PARTY_LICENSES.md")) {
        $noticeSource = Join-Path $PSScriptRoot $noticeName
        $noticePath = Join-Path $distDir $noticeName
        if (-not (Test-Path -LiteralPath $noticePath)) {
            Copy-Item -LiteralPath $noticeSource -Destination $noticePath -Force
        }
        if (-not (Test-Path -LiteralPath $noticePath)) {
            throw "构建完成但没有找到许可/署名清单文件：$noticePath"
        }
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
