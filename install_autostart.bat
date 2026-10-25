@echo off
REM Registers the "MT5 Bot Supervisor" task in the Windows Task Scheduler (see README, section 12).
REM Optional: install_autostart.bat -DelayMinutes 2 -BotArgs "--dry-run"   (add -NoPanel to skip the web panel)
REM The supervisor starts the bot AND the CYBERBOX panel (panel.enabled in config.json).
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
if errorlevel 1 (
    echo.
    echo Registration FAILED. Read the message above.
    pause
    exit /b 1
)
echo.
echo Done. The supervisor (bot + CYBERBOX panel) will start automatically at your next logon.
pause
