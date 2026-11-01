@echo off
REM Starts the bot on the REAL account (the default). Any extra options are passed to the bot.
cd /d %~dp0
if not exist ".venv\Scripts\activate.bat" (
    echo Ambiente virtual .venv nao encontrado. Veja o README.
    pause
    exit /b 2
)
call ".venv\Scripts\activate.bat"
echo A arrancar em modo REAL. Dinheiro real em risco. Para testar use start_bot_demo.bat.
python mt5_bot.py --account real %*
set "BOT_EXIT=%errorlevel%"
if not "%BOT_EXIT%"=="0" (
    echo.
    echo O bot terminou com o codigo %BOT_EXIT%. Veja bot.log.
    pause
)
exit /b %BOT_EXIT%
