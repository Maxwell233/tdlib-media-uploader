@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
set "exitcode=%errorlevel%"

if not "%exitcode%"=="0" (
    echo.
    echo run.ps1 运行异常，退出代码：%exitcode%
    echo 请查看上方错误信息。
    echo.
    pause
)

exit /b %exitcode%
