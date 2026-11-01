@echo off
REM Avvia il Local Server LM Studio (porta 1234) al login utente.
cd /d "%~dp0.."
"%USERPROFILE%\.lmstudio\bin\lms.exe" server start >> "logs\lmstudio_server.log" 2>&1
