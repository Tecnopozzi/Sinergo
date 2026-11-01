#Requires -Version 5.1
[CmdletBinding()]
param()

Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Creo virtualenv .venv ..." -ForegroundColor Cyan
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

if (-not (Test-Path ".\.env")) {
    Copy-Item ".env.example" ".env"
    Write-Warning "Creato .env da template. Controlla le variabili prima di esporre il server pubblicamente."
}

$port = (Select-String -Path ".\.env" -Pattern "^PORT\s*=\s*(\d+)" | ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1)
if (-not $port) { $port = "8090" }

Write-Host "Avvio LocalRouter (Gateway + OCR) su http://127.0.0.1:$port ..." -ForegroundColor Green
& .\.venv\Scripts\python.exe server.py
