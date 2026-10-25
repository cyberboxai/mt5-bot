@echo off
REM Starts the MT5 bot from this folder using the local virtual environment.
cd /d %~dp0
if not exist ".venv\Scripts\activate.bat" (
    echo Virtual environment .venv not found. See README, section 7.
    pause
    exit /b 2
)
call ".venv\Scripts\activate.bat"
python mt5_bot.py %*
set "BOT_EXIT=%errorlevel%"
if not "%BOT_EXIT%"=="0" (
    echo.
    echo The bot exited with code %BOT_EXIT%. Check bot.log for details.
    pause
)
exit /b %BOT_EXIT%
