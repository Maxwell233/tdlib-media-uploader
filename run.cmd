@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title TDLib Media Uploader V1.8.2

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
set "exitcode=%errorlevel%"

if not "%exitcode%"=="0" (
    echo.
    echo ==========================================
    echo  TDLib Media Uploader exited unexpectedly
    echo ==========================================
    echo Exit code: %exitcode%
    echo.
    echo The window will stay open so you can read the error above.
    echo.
    pause
)

exit /b %exitcode%
