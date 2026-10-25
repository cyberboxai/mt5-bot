@echo off
REM Starts the CYBERBOX Trading Office panel: http://127.0.0.1:8765/ (read-only by default).
REM   start_panel.bat          shows the bot running from this folder
REM   start_panel.bat --demo   synthetic simulation (no MT5, no broker, no API key)
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment .venv not found. See README, section 7.
    pause
    exit /b 2
)
".venv\Scripts\python.exe" -m panel %*
set "PANEL_EXIT=%errorlevel%"
if not "%PANEL_EXIT%"=="0" (
    echo.
    echo The panel exited with code %PANEL_EXIT%. Check panel.log.
    pause
)
exit /b %PANEL_EXIT%
