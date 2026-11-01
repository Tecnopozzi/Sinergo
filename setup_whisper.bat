@echo off
chcp 65001 >nul
title 10b — Setup Whisper (trascrizione audio)
cd /d "%~dp0"

echo === Setup automatico modulo Whisper ===
echo Richiede: Python 3.10/3.11/3.12. Nessuna GPU necessaria (profilo CPU int8).
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_whisper.ps1" %*
if errorlevel 1 (
    echo.
    echo Setup fallito. Controllare i messaggi sopra.
    pause
    exit /b 1
)

echo.
echo Setup completato. Riavviare il server con start-gateway.cmd
pause
