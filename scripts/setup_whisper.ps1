# Setup modulo Whisper (trascrizione audio) per LocalRouter.
# Auto-installazione su macchina nuova: crea .venv-whisper (Python 3.10/3.11/3.12).
#
# Profilo GPU (default se nvidia-smi): large-v3 + cuda + float16 (3060 12GB OK dopo unload LLM)
# Profilo CPU (-CpuProfile): base + cpu + int8
#
# Uso:
#   .\scripts\setup_whisper.ps1
#   .\scripts\setup_whisper.ps1 -Model small -CpuProfile
#   .\scripts\setup_whisper.ps1 -SkipModel
param(
    [string]$Model = "",
    [switch]$SkipModel,
    [switch]$CpuProfile
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
. (Join-Path $PSScriptRoot "_dotenv.ps1")

function Write-Step($msg) {
    Write-Host ""
    Write-Host ">> $msg" -ForegroundColor Cyan
}

function Find-Python {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        foreach ($ver in @("3.11", "3.12", "3.10")) {
            $tag = $ver -replace '\.', ''
            $candidates = @(
                "$env:LOCALAPPDATA\Programs\Python\Python$tag\python.exe",
                "C:\Python$tag\python.exe",
                "C:\Program Files\Python$tag\python.exe"
            )
            foreach ($p in $candidates) {
                if (Test-Path $p) { return $p }
            }
            if (Get-Command py -ErrorAction SilentlyContinue) {
                $exe = (& py "-$ver" -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
                if ($exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
            }
        }
    } finally {
        $ErrorActionPreference = $prev
    }
    return $null
}

function Set-EnvLine {
    param([string]$File, [string]$Key, [string]$Value)
    $line = "$Key=$Value"
    if (-not (Test-Path $File)) {
        if (Test-Path (Join-Path $Root ".env.example")) {
            Copy-Item (Join-Path $Root ".env.example") $File
        } else {
            New-Item -ItemType File -Path $File | Out-Null
        }
    }
    $content = Get-Content $File -ErrorAction SilentlyContinue
    $found = $false
    $out = foreach ($row in $content) {
        if ($row -match "^\s*$([regex]::Escape($Key))\s*=") {
            $found = $true
            $line
        } else {
            $row
        }
    }
    if (-not $found) { $out += $line }
    Set-Content -Path $File -Value $out -Encoding UTF8
}

Write-Host "=== Setup Whisper (trascrizione audio) ===" -ForegroundColor Cyan
Write-Host "Root: $Root"

Write-Step "Prerequisiti"
$hasGpu = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpuName = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
    if ($gpuName) {
        $hasGpu = $true
        Write-Host "GPU: $gpuName"
    }
}

if ($CpuProfile -or -not $hasGpu) {
    if (-not $Model) { $Model = "base" }
    $Device = "cpu"
    $Compute = "int8"
    Write-Host "Profilo: CPU ($Model / $Device / $Compute)"
} else {
    if (-not $Model) { $Model = "large-v3" }
    $Device = "cuda"
    $Compute = "float16"
    Write-Host "Profilo: GPU ($Model / $Device / $Compute) - consigliato unload LLM prima della trascrizione"
}

$py = Find-Python
if (-not $py) {
    Write-Host "ERRORE: Python 3.10/3.11/3.12 non trovato." -ForegroundColor Red
    Write-Host "  winget install Python.Python.3.11" -ForegroundColor Yellow
    exit 1
}
$pyVer = & $py -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "Python: $py (v$pyVer)"

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpeg) {
    Write-Host "ffmpeg: $($ffmpeg.Source)"
} else {
    Write-Host "AVVISO: ffmpeg non sul PATH (PyAV incluso come fallback)" -ForegroundColor Yellow
}

Write-Step "Virtual environment .venv-whisper"
$venv = Join-Path $Root ".venv-whisper"
$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $py -m venv $venv
} else {
    Write-Host "venv gia presente: $venv"
}

& $venvPy -m pip install --upgrade pip wheel | Out-Null
Write-Host "Installo faster-whisper (requirements-whisper.txt) ..."
& $venvPy -m pip install -r (Join-Path $Root "requirements-whisper.txt")

if ($Device -eq "cuda") {
    Write-Host "Installo librerie CUDA (cublas + cudnn, ~1.3 GB) ..."
    & $venvPy -m pip install -r (Join-Path $Root "requirements-whisper-gpu.txt")
}

Write-Step "Test import faster-whisper"
& $venvPy (Join-Path $Root "scripts\test_whisper_imports.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERRORE: import faster_whisper fallito." -ForegroundColor Red
    exit 1
}

if ($Device -eq "cuda") {
    & $venvPy (Join-Path $Root "scripts\test_whisper_imports.py") --cuda
    if ($LASTEXITCODE -ne 0) {
        Write-Host "AVVISO: CUDA non disponibile in ctranslate2, fallback CPU int8" -ForegroundColor Yellow
        $Device = "cpu"
        $Compute = "int8"
        if ($Model -eq "large-v3") { $Model = "base" }
    }
}

$modelsDir = Resolve-WhisperModelsDir -Root $Root
New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null

if (-not $SkipModel) {
    Write-Step "Prefetch modello Whisper '$Model' ($Device / $Compute) in $modelsDir"
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
    & $venvPy (Join-Path $Root "scripts\prefetch_whisper_model.py") `
        --model $Model --device $Device --compute-type $Compute --download-root $modelsDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "AVVISO: prefetch fallito - usare POST /whisper/download-model o rilanciare setup" -ForegroundColor Yellow
    }
} else {
    Write-Host "Skip prefetch modello (-SkipModel)"
}

Write-Step "Configurazione .env"
$envFile = Join-Path $Root ".env"
Set-EnvLine -File $envFile -Key "WHISPER_ENABLED" -Value "1"
Set-EnvLine -File $envFile -Key "WHISPER_VENV_PYTHON" -Value $venvPy
Set-EnvLine -File $envFile -Key "WHISPER_MODELS_DIR" -Value $modelsDir
Set-EnvLine -File $envFile -Key "WHISPER_MODEL" -Value $Model
Set-EnvLine -File $envFile -Key "WHISPER_DEVICE" -Value $Device
Set-EnvLine -File $envFile -Key "WHISPER_COMPUTE_TYPE" -Value $Compute
Set-EnvLine -File $envFile -Key "WHISPER_AUTO_UNLOAD_LLMS" -Value "1"
Write-Host "Aggiornato: $envFile (WHISPER_MODEL=$Model, DEVICE=$Device)"

Write-Host ""
Write-Host "=== Setup Whisper completato ===" -ForegroundColor Green
Write-Host ""
Write-Host "Prossimi passi:"
Write-Host "  1. Verifica:  .\scripts\verify_whisper.ps1"
Write-Host "  2. Riavvia:   .\start-gateway.cmd"
Write-Host "  3. Health:    curl http://127.0.0.1:8088/health"
Write-Host ""
Write-Host "Scaricare modello da remoto (se non prefetchato):"
Write-Host '  curl -X POST http://127.0.0.1:8088/whisper/download-model -H "Authorization: Bearer <API_KEY>"'
Write-Host ""
Write-Host "Liberare VRAM prima di trascrivere su GPU:"
Write-Host '  curl -X POST http://127.0.0.1:8088/whisper/unload-llms -H "Authorization: Bearer <API_KEY>"'
