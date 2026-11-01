@echo off
REM Starts the bot on a DEMO account. It refuses a real account with exit code 3.
cd /d %~dp0
if not exist ".venv\Scripts\activate.bat" (
    echo Ambiente virtual .venv nao encontrado. Veja o README.
    pause
    exit /b 2
)
call ".venv\Scripts\activate.bat"
echo A arrancar em modo DEMO. Uma conta real e recusada.
python mt5_bot.py --account demo %*
set "BOT_EXIT=%errorlevel%"
if not "%BOT_EXIT%"=="0" (
    echo.
    echo O bot terminou com o codigo %BOT_EXIT%. Veja bot.log.
    pause
)
exit /b %BOT_EXIT%
