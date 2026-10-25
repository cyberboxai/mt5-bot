@echo off
REM Starts the supervisor, which keeps mt5_bot.py running and restarts it after crashes.
REM Any options are passed to the bot, for example: start_supervisor.bat --confirm-real-money
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment .venv not found. See README, section 7.
    timeout /t 60
    exit /b 2
)
".venv\Scripts\python.exe" supervisor.py %*
set "SUP_EXIT=%errorlevel%"
if not "%SUP_EXIT%"=="0" (
    echo.
    echo The supervisor exited with code %SUP_EXIT%. Check supervisor.log and bot.log.
    timeout /t 60
)
exit /b %SUP_EXIT%
