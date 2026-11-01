# Verifica installazione modulo Media SUPIR (post setup_media.ps1)
param(
    [switch]$SkipServerCheck
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

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

Write-Host "=== Verifica Media SUPIR ===" -ForegroundColor Cyan

$venvPy = Join-Path $Root ".venv-media\Scripts\python.exe"
$supir = Join-Path $Root "vendor\SUPIR"
$models = Join-Path $Root "models\media"
$sdxl = Join-Path $models "sd_xl_base_1.0_0.9vae.safetensors"
$supirF = Join-Path $models "SUPIR-v0F.ckpt"
$yaml = Join-Path $supir "options\SUPIR_v0_12gb.yaml"

Test-Ok "Git installato" (Get-Command git -ErrorAction SilentlyContinue) "Installare Git for Windows"
Test-Ok "NVIDIA GPU (nvidia-smi)" {
    $null -ne (Get-Command nvidia-smi -ErrorAction SilentlyContinue) -and
    (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null)
} "Serve driver NVIDIA + GPU dedicata"

Test-Ok "Python venv media" (Test-Path $venvPy) "Eseguire scripts\setup_media.ps1"
Test-Ok "Repo SUPIR" (Test-Path $supir) "setup_media.ps1 clona vendor\SUPIR"
Test-Ok "YAML config" (Test-Path $yaml) "setup_media.ps1 genera options\SUPIR_v0_12gb.yaml"
Test-Ok "Peso SDXL" (Test-Path $sdxl) "~6.5 GB, scaricato da setup"
Test-Ok "Peso SUPIR-v0F" (Test-Path $supirF) "~5.3 GB, scaricato da setup"

if (Test-Path $venvPy) {
    $env:SUPIR_ROOT = $supir
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $venvPy scripts\test_media_imports.py *> $null
    $ErrorActionPreference = $prevEap
    Test-Ok "Import torch/xformers/SUPIR" ($LASTEXITCODE -eq 0) "Controllare test_media_imports.py"
}

$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    $envText = Get-Content $envFile -Raw
    Test-Ok "MEDIA_ENABLED=1 in .env" ($envText -match '(?m)^MEDIA_ENABLED=1\s*$') "Impostare MEDIA_ENABLED=1"
} else {
    Test-Ok ".env presente" $false "copy .env.example .env poi setup_media.ps1"
}

if (-not $SkipServerCheck) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8088/health" -TimeoutSec 5
        Test-Ok "Server /health raggiungibile" ($health.status -in @("ok", "degraded"))
        Test-Ok "media_ok=true" ($health.media_ok -eq $true) "Riavviare con start-gateway.cmd dopo setup"
    } catch {
        Test-Ok "Server /health (opzionale)" $true
        Write-Host "       Server non in ascolto, salto check media_ok" -ForegroundColor DarkGray
    }
}

Write-Host ""
if ($fail -eq 0) {
    Write-Host "Verifica completata: tutto OK" -ForegroundColor Green
    exit 0
}
Write-Host "Verifica fallita: $fail controlli" -ForegroundColor Red
Write-Host 'Eseguire: .\scripts\setup_media.ps1' -ForegroundColor Yellow
exit 1
