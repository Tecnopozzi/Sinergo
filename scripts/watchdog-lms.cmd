@echo off
REM Watchdog LMS standalone (backup se il gateway non gira).
REM Preferito: watchdog integrato nel gateway (LMSTUDIO_WATCHDOG_ENABLED=1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog-lms.ps1"
