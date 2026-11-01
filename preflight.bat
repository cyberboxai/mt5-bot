@echo off
REM Read-only check of the account: nothing is sent. Add --account demo to check a demo account.
cd /d %~dp0
if not exist ".venv\Scripts\activate.bat" (
    echo Ambiente virtual .venv nao encontrado. Veja o README.
    pause
    exit /b 2
)
call ".venv\Scripts\activate.bat"
echo Verificacao previa (apenas leitura). Nenhuma ordem e enviada.
python mt5_bot.py --preflight %*
set "BOT_EXIT=%errorlevel%"
echo.
echo Resultado da verificacao: codigo %BOT_EXIT%.
pause
exit /b %BOT_EXIT%
