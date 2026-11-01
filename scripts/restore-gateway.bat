@echo off
REM Ripristino gateway senza PowerShell (alcuni antivirus bloccano ps-script in %%TEMP%%).
chcp 65001 >nul
cd /d "%~dp0.."
set "LOG=%~dp0..\logs\restore-gateway.log"
if not exist "logs" mkdir logs
echo %DATE% %TIME% ^| === restore-gateway.bat === >> "%LOG%"

call "%~dp0install-autostart-user.bat" >> "%LOG%" 2>&1

for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8088 " ^| findstr "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
ping -n 3 127.0.0.1 >nul

set "PATH=%USERPROFILE%\.lmstudio\bin;%PATH%"
if exist "server.py" (
    start "LocalRouter" /MIN ".venv\Scripts\python.exe" server.py
) else (
    start "LocalRouter" /MIN ".venv\Scripts\python.exe" run_server.py
)
echo %DATE% %TIME% ^| avviato gateway >> "%LOG%"

ping -n 11 127.0.0.1 >nul
curl.exe -s http://127.0.0.1:8088/health >> "%LOG%" 2>&1
echo. >> "%LOG%"
lms.exe ps >> "%LOG%" 2>&1
echo %DATE% %TIME% ^| fine >> "%LOG%"
echo Fatto. Log: %LOG%
