# Verifica installazione modulo Whisper (post setup_whisper.ps1)
param(
    [switch]$SkipServerCheck
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
. (Join-Path $PSScriptRoot "_dotenv.ps1")

$fail = 0
function Test-Ok($label, $cond, $hint = "") {
    if ($cond) {
        Write-Host "[OK] $label" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] $label" -ForegroundColor Red
        if ($hint) { Write-Host "       $hint" -ForegroundColor Yellow }
        $script:fail++
    }
}

Write-Host "=== Verifica Whisper (trascrizione audio) ===" -ForegroundColor Cyan

$venvPy = Join-Path $Root ".venv-whisper\Scripts\python.exe"
$worker = Join-Path $Root "transcribe\whisper_worker.py"
$modelsDir = Resolve-WhisperModelsDir -Root $Root

Test-Ok "Python venv .venv-whisper" (Test-Path $venvPy) "Eseguire scripts\setup_whisper.ps1"
Test-Ok "Worker whisper_worker.py" (Test-Path $worker) "File mancante nel repo"

if (Test-Path $venvPy) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $venvPy -c "import faster_whisper, ctranslate2" *> $null
    $ErrorActionPreference = $prevEap
    Test-Ok "Import faster-whisper/ctranslate2" ($LASTEXITCODE -eq 0) "pip install -r requirements-whisper.txt"
}

$modelCached = $false
if (Test-Path $modelsDir) {
    $modelCached = ([bool](Get-ChildItem $modelsDir -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "*faster-whisper*" }))
}
Test-Ok "Modello in cache locale ($modelsDir)" $modelCached "Verra scaricato al primo uso (serve rete)"

$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    $envText = Get-Content $envFile -Raw
    Test-Ok "WHISPER_ENABLED=1 in .env" ($envText -match '(?m)^WHISPER_ENABLED=1\s*$') "Impostare WHISPER_ENABLED=1"
} else {
    Test-Ok ".env presente" $false "copy .env.example .env poi setup_whisper.ps1"
}

if (-not $SkipServerCheck) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8088/health" -TimeoutSec 5
        Test-Ok "Server /health raggiungibile" ($health.status -in @("ok", "degraded"))
        Test-Ok "whisper_ok=true" ($health.whisper_ok -eq $true) "Riavviare con start-gateway.cmd dopo setup"
    } catch {
        Test-Ok "Server /health (opzionale)" $true
        Write-Host "       Server non in ascolto, salto check whisper_ok" -ForegroundColor DarkGray
    }
}

Write-Host ""
if ($fail -eq 0) {
    Write-Host "Verifica completata: tutto OK" -ForegroundColor Green
    exit 0
}
Write-Host "Verifica fallita: $fail controlli" -ForegroundColor Red
Write-Host 'Eseguire: .\scripts\setup_whisper.ps1' -ForegroundColor Yellow
exit 1
