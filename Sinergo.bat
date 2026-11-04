@echo off
title Sinergo
cd /d "%~dp0"
REM Un solo avvio: gateway (se /health non risponde) + collegato. Niente cloudflared.
if exist ".venv\Scripts\python.exe" goto avvia
python installa.py --sinergo
exit /b %errorlevel%
:avvia
".venv\Scripts\python.exe" -c "import installa; installa.avvia_runtime_sinergo()"
