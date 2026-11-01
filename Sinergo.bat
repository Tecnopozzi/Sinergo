@echo off
title Sinergo
cd /d "%~dp0"
REM Sinergo usa il proprio ambiente; la prima installazione apre anche il browser.
if exist ".venv\Scripts\python.exe" goto avvia
python installa.py --sinergo
exit /b %errorlevel%
:avvia
".venv\Scripts\python.exe" -m collegato
