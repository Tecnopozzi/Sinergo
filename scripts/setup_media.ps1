# Setup modulo Media SUPIR per LocalRouter
# Auto-installazione su macchina nuova con GPU NVIDIA (profilo 12 GB VRAM).
#
# Uso:
#   .\scripts\setup_media.ps1
#   .\scripts\setup_media.ps1 -SkipModels      # solo venv + repo, no download pesi
#   .\scripts\setup_media.ps1 -SkipClipPrefetch # salta prefetch CLIP (~10 GB)
param(
    [switch]$SkipModels,
    [switch]$SkipClipPrefetch
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
. (Join-Path $PSScriptRoot "_dotenv.ps1")

$MinFreeGb = 25
$RequiredPython = "3.10"

function Write-Step($msg) {
    Write-Host ""
    Write-Host ">> $msg" -ForegroundColor Cyan
}

function Get-FreeDiskGb([string]$Path) {
    $drive = (Split-Path $Path -Qualifier).TrimEnd(':')
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='${drive}:'"
    if ($disk) { return [math]::Round($disk.FreeSpace / 1GB, 1) }
    return 0.0
}

function Find-Python310 {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "C:\Python310\python.exe",
        "C:\Program Files\Python310\python.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) {
            $ver = & $p -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
            if ($ver -eq $RequiredPython) { return $p }
        }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $p310 = & py -3.10 -c "import sys; print(sys.executable)" 2>$null
        if ($p310 -and (Test-Path $p310)) { return $p310.Trim() }
    }
    return $null
}

function Set-EnvLine {
    param(
        [string]$File,
        [string]$Key,
        [string]$Value
    )
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

function Install-SupirYaml {
    param(
        [string]$SdxlPath,
        [string]$SupirPath,
        [string]$DestYaml
    )
    $tpl = Join-Path $Root "scripts\SUPIR_v0_12gb.yaml.template"
    if (-not (Test-Path $tpl)) {
        throw "Template mancante: $tpl"
    }
    $sdxlUnix = ($SdxlPath -replace '\\', '/')
    $supirUnix = ($SupirPath -replace '\\', '/')
    $yaml = Get-Content $tpl -Raw
    $yaml = $yaml.Replace('PLACEHOLDER_SDXL', $sdxlUnix)
    $yaml = $yaml.Replace('PLACEHOLDER_SUPIR_F', $supirUnix)
    $destDir = Split-Path $DestYaml -Parent
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    Set-Content -Path $DestYaml -Value $yaml -Encoding UTF8
}

Write-Host "=== Setup Media SUPIR ===" -ForegroundColor Cyan
Write-Host "Root: $Root"

Write-Step "Prerequisiti"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "ERRORE: Git non trovato. Installare Git for Windows." -ForegroundColor Red
    Write-Host "  winget install --id Git.Git" -ForegroundColor Yellow
    exit 1
}

if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Host "ERRORE: nvidia-smi non trovato. Serve GPU NVIDIA + driver CUDA." -ForegroundColor Red
    exit 1
}
$gpuName = (nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
Write-Host "GPU: $gpuName"

$freeGb = Get-FreeDiskGb $Root
Write-Host "Spazio libero su disco progetto: ${freeGb} GB (min consigliato ${MinFreeGb} GB)"
if ($freeGb -lt $MinFreeGb -and -not $SkipModels) {
    Write-Host "AVVISO: spazio disco basso. Il download modelli potrebbe fallire." -ForegroundColor Yellow
}

$py310 = Find-Python310
if (-not $py310) {
    Write-Host "ERRORE: Python $RequiredPython non trovato." -ForegroundColor Red
    Write-Host "Installare Python 3.10.x (64-bit), poi rilanciare setup:" -ForegroundColor Yellow
    Write-Host "  winget install Python.Python.3.10" -ForegroundColor Yellow
    exit 1
}
Write-Host "Python: $py310"

Write-Step "Virtual environment .venv-media"
$venv = Join-Path $Root ".venv-media"
$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $py310 -m venv $venv
}

& $venvPy -m pip install --upgrade pip wheel
& $venvPy -m pip install "setuptools>=65,<81"

Write-Host "Installo PyTorch 2.6.0 CUDA 12.4 ..."
& $venvPy -m pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124

Write-Host "Installo xformers 0.0.29.post3 ..."
& $venvPy -m pip install xformers==0.0.29.post3

Write-Host "Installo dipendenze SUPIR (requirements-media.txt) ..."
& $venvPy -m pip install -r (Join-Path $Root "requirements-media.txt")

$torchVer = (& $venvPy -c "import torch; print(torch.__version__)" 2>$null)
$xfVer = (& $venvPy -c "import xformers; print(xformers.__version__)" 2>$null)
if ($torchVer -ne "2.6.0+cu124" -or $xfVer -ne "0.0.29.post3") {
    Write-Host "Ripristino pin torch/xformers ..."
    & $venvPy -m pip install --force-reinstall torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
    & $venvPy -m pip install --force-reinstall xformers==0.0.29.post3
} else {
    Write-Host "torch/xformers gia corretti ($torchVer, xformers $xfVer)"
}

Write-Step "Repo SUPIR (vendor/SUPIR)"
$supir = Join-Path $Root "vendor\SUPIR"
if (-not (Test-Path $supir)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $supir) | Out-Null
    git clone --depth 1 https://github.com/Fanghua-Yu/SUPIR.git $supir
} else {
    Write-Host "Repo gia presente: $supir"
}

Write-Host "Patch CKPT_PTH.py (CLIP via HuggingFace locale) ..."
$ckpt = Join-Path $supir "CKPT_PTH.py"
@'
LLAVA_CLIP_PATH = None
LLAVA_MODEL_PATH = None
SDXL_CLIP1_PATH = None
SDXL_CLIP2_CKPT_PTH = None
'@ | Set-Content -Path $ckpt -Encoding UTF8

$models = Resolve-MediaModelsDir -Root $Root
$hfCache = Join-Path $models "hf_cache"
New-Item -ItemType Directory -Force -Path $models, $hfCache | Out-Null
$env:HF_HOME = $hfCache
$env:HUGGINGFACE_HUB_CACHE = Join-Path $hfCache "hub"

$sdxl = Join-Path $models "sd_xl_base_1.0_0.9vae.safetensors"
$supirF = Join-Path $models "SUPIR-v0F.ckpt"

if (-not $SkipModels) {
    Write-Step "Download pesi modelli (~12 GB, non in git)"
    if (-not (Test-Path $sdxl)) {
        Write-Host "SDXL base 0.9vae (~6.5 GB) ..."
        & $venvPy -m pip install -q huggingface_hub
        & $venvPy -c @"
from huggingface_hub import hf_hub_download
import shutil
p = hf_hub_download('stabilityai/stable-diffusion-xl-base-1.0', 'sd_xl_base_1.0_0.9vae.safetensors')
shutil.copy2(p, r'$sdxl')
print('SDXL OK')
"@
    } else {
        Write-Host "SDXL gia presente"
    }

    if (-not (Test-Path $supirF)) {
        Write-Host "SUPIR-v0F (~5.3 GB) ..."
        & $venvPy -c @"
from huggingface_hub import hf_hub_download
import shutil
p = hf_hub_download('camenduru/SUPIR', 'SUPIR-v0F.ckpt')
shutil.copy2(p, r'$supirF')
print('SUPIR-v0F OK')
"@
    } else {
        Write-Host "SUPIR-v0F gia presente"
    }
} else {
    Write-Host "Skip download modelli (-SkipModels)"
}

$yamlDest = Join-Path $supir "options\SUPIR_v0_12gb.yaml"
Install-SupirYaml -SdxlPath $sdxl -SupirPath $supirF -DestYaml $yamlDest
Write-Host "YAML config: $yamlDest"

if (-not $SkipClipPrefetch -and -not $SkipModels) {
    $clipMarker = Join-Path $hfCache ".clip_prefetch_done"
    if (-not (Test-Path $clipMarker)) {
        Write-Step "Prefetch CLIP/OpenCLIP in $hfCache (~10 GB, una tantum)"
        $env:SUPIR_ROOT = $supir
        & $venvPy (Join-Path $Root "scripts\prefetch_clip_weights.py")
        if ($LASTEXITCODE -ne 0) {
            Write-Host "AVVISO: prefetch CLIP fallito, verra scaricato al primo job" -ForegroundColor Yellow
        } else {
            Set-Content -Path $clipMarker -Value (Get-Date -Format o) -Encoding UTF8
        }
    } else {
        Write-Host "CLIP gia prefetchato (marker presente)"
    }
}

Write-Step "Configurazione .env"
$envFile = Join-Path $Root ".env"
Set-EnvLine -File $envFile -Key "MEDIA_ENABLED" -Value "1"
Set-EnvLine -File $envFile -Key "MEDIA_VENV_PYTHON" -Value $venvPy
Set-EnvLine -File $envFile -Key "MEDIA_MODELS_DIR" -Value $models
Write-Host "Aggiornato: $envFile (MEDIA_ENABLED=1, MEDIA_MODELS_DIR=$models)"

Write-Step "Test import"
$env:SUPIR_ROOT = $supir
& $venvPy (Join-Path $Root "scripts\test_media_imports.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "=== Setup completato ===" -ForegroundColor Green
Write-Host ""
Write-Host "Prossimi passi:"
Write-Host "  1. Verifica:  .\scripts\verify_media.ps1"
Write-Host "  2. Riavvia:   .\start-gateway.cmd"
Write-Host "  3. Health:    curl http://127.0.0.1:8088/health  (media_ok: true)"
Write-Host ""
Write-Host "Note GPU 12 GB:"
Write-Host "  - Scaricare modelli LM Studio prima dei job media (libera VRAM)"
Write-Host "  - Primo job ~15-20 min; upscale x2 consigliato"
