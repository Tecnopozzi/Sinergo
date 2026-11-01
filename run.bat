@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creo virtualenv .venv ...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

if not exist ".env" (
    copy .env.example .env
    echo ATTENZIONE: creato .env da template. Controlla le variabili prima di esporre il server.
)

echo Avvio 10b (Gateway + OCR) ...
.venv\Scripts\python.exe server.py
