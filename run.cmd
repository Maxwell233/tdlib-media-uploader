@echo off
setlocal
cd /d "%~dp0"

echo.
echo ==========================================
echo        TDLib Media Uploader V1.6
echo ==========================================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
set "exitcode=%errorlevel%"

if not "%exitcode%"=="0" (
    echo.
    echo [ERROR] run.ps1 exited with code %exitcode%.
    echo Please check the error message above.
    echo.
    pause
)

exit /b %exitcode%
