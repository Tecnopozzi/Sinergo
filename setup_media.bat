@echo off
chcp 65001 >nul
title 10b — Setup Media SUPIR
cd /d "%~dp0"

echo === Setup automatico modulo Media SUPIR ===
echo Richiede: Git, Python 3.10, GPU NVIDIA, ~25 GB disco libero
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_media.ps1"
if errorlevel 1 (
    echo.
    echo Setup fallito. Controllare i messaggi sopra.
    pause
    exit /b 1
)

echo.
echo Setup completato. Riavviare il server con start-gateway.cmd
pause
