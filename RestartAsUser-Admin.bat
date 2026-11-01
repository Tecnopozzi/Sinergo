@echo off
:: Riavvia Localrouter come utente interattivo (NON come SYSTEM).
:: Eseguire "Come amministratore" se taskkill fallisce con Accesso negato.
chcp 65001 >nul
cd /d "%~dp0"

echo === Kill listener :8088 ===
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8088 " ^| findstr "LISTENING"') do (
    echo Termino PID %%a ...
    taskkill /PID %%a /F
)

echo Attendo 3s...
ping -n 4 127.0.0.1 >nul

echo === Avvio .venv server.py (utente corrente) ===
if not exist ".venv\Scripts\python.exe" (
    echo ERRORE: .venv mancante
    pause
    exit /b 1
)

:: Assicura che lms sia nel PATH della sessione
set "PATH=%USERPROFILE%\.lmstudio\bin;%PATH%"

if exist "server.py" (
    start "LocalRouter" /MIN ".venv\Scripts\python.exe" server.py
) else (
    start "LocalRouter" /MIN ".venv\Scripts\python.exe" run_server.py
)

echo Attendo 10s...
ping -n 11 127.0.0.1 >nul

curl -s http://127.0.0.1:8088/health
echo.
echo Fatto. Se health fallisce, ripeti come Amministratore.
pause
