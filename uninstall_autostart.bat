@echo off
REM Removes the "MT5 Bot Supervisor" task from the Windows Task Scheduler.
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_autostart.ps1" %*
if errorlevel 1 (
    echo Removal FAILED. Read the message above.
    pause
    exit /b 1
)
pause
