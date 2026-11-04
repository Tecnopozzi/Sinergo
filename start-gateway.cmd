@echo off
REM Avvio gateway Localrouter al login (utente interattivo, Session >= 1).
REM NON mettere in Startup: alcuni antivirus mettono in quarantena i .lnk.
REM Installato via HKCU Run o task LMStudioServer.
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
set "PATH=%USERPROFILE%\.lmstudio\bin;%PATH%"
if exist "server.py" (
    start "LocalRouter" /MIN ".venv\Scripts\python.exe" server.py
) else if exist "run_server.py" (
    start "LocalRouter" /MIN ".venv\Scripts\python.exe" run_server.py
) else (
    echo ERRORE: manca server.py >> "logs\server_autostart.log"
    exit /b 1
)
REM Macchina abbinata a Kairos: stesso avvio lancia anche il collegato (ex Sinergo separato).
if exist "%LOCALAPPDATA%\LocalRouter\macchina.key" if exist "collegato\__main__.py" (
    start "LocalRouter-collegato" /MIN ".venv\Scripts\python.exe" -m collegato
)
