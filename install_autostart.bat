@echo off
REM Registers the "MT5 Bot Supervisor" task in the Windows Task Scheduler (see README, section 12).
REM Optional: install_autostart.bat -DelayMinutes 2 -BotArgs "--dry-run"
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
if errorlevel 1 (
    echo.
    echo Registration FAILED. Read the message above.
    pause
    exit /b 1
)
echo.
echo Done. The supervisor will start automatically at your next logon.
pause
