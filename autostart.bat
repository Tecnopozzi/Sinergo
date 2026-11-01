@echo off
REM Launcher autostart Localrouter (Gateway OpenAI-compat + OCR).
REM Avviato dal Task Scheduler "LocalrouterServer" all'accesso utente (Interactive).
REM NON avviare come SYSTEM: serve la CLI lms nello stesso utente di LM Studio.
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
set "PATH=%USERPROFILE%\.lmstudio\bin;%PATH%"
REM server.py e' l'originale: run_server.py e' solo la copia di ripiego se
REM Alcuni antivirus mettono in quarantena le scorciatoie .lnk in Startup.
if exist "server.py" (
    ".venv\Scripts\python.exe" server.py >> "logs\server_autostart.log" 2>&1
) else (
    ".venv\Scripts\python.exe" run_server.py >> "logs\server_autostart.log" 2>&1
)
