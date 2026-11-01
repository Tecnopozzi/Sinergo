param(
    [string]$Root = "C:\KairosDecisori",
    [string]$ModelsDir = "D:\modelli\decisori",
    [int]$Port = 8098,
    [string]$ApiKey = "",
    [switch]$SkipDownload
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $Root | Out-Null
New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null
Set-Location $Root

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv non trovato. Installarlo prima sulla macchina Windows."
}

uv venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
# Laya su CPU; Decision-4B via llama-cpp (GGUF Q4). n_gpu_layers=0 di default.
uv pip install "fastapi>=0.110" "uvicorn[standard]>=0.30" "laya[serve]>=0.3.20" "huggingface_hub>=0.26" "llama-cpp-python>=0.3.0"

Copy-Item "$PSScriptRoot\server_decisore_fastapi.py" "$Root\server_decisore_fastapi.py" -Force

if (-not $SkipDownload) {
    Write-Host "Scarico checkpoint Laya multilingual e GGUF Decision-4B in $ModelsDir ..."
    & .\.venv\Scripts\python.exe -c @"
from huggingface_hub import snapshot_download, hf_hub_download
import os
root = r'$ModelsDir'
os.makedirs(root, exist_ok=True)
snapshot_download('convaiinnovations/laya', allow_patterns=['multilingual/**'], local_dir=os.path.join(root, 'laya'))
hf_hub_download('evalengine/decision-4b-gguf', 'decision-4b-Q4_K_M.gguf', local_dir=root)
print('ok', root)
"@
}

$gguf = Join-Path $ModelsDir "decision-4b-Q4_K_M.gguf"
$envFile = Join-Path $Root ".env"
@"
DECISORE_API_KEY=$ApiKey
DECISORE_MODELS_DIR=$ModelsDir
DECISION4B_GGUF=$gguf
DECISION4B_N_GPU_LAYERS=0
DECISION4B_N_CTX=2048
LAYA_MAX_LEN=8192
LAYA_HEAD_MAX_LEN=256
DECISORE_CODA_TIMEOUT_S=90
DECISORE_INFER_TIMEOUT_S=120
"@ | Set-Content -Encoding ascii $envFile

$cmd = @"
`$ErrorActionPreference = 'Stop'
Get-Content `"$Root\.env`" | ForEach-Object {
  if (`$_ -match '^\s*#' -or `$_ -notmatch '=') { return }
  `$k,`$v = `$_.Split('=',2)
  Set-Item -Path `"Env:`$k`" -Value `$v
}
Set-Location `"$Root`"
& `"$Root\.venv\Scripts\python.exe`" -m uvicorn server_decisore_fastapi:app --host 127.0.0.1 --port $Port
"@
$cmd | Set-Content -Encoding ascii (Join-Path $Root "avvia_decisore.ps1")

Write-Host "Pronto. Avvio:"
Write-Host "powershell -ExecutionPolicy Bypass -File $Root\avvia_decisore.ps1"
Write-Host "Endpoint: http://127.0.0.1:$Port/v1/decide"
Write-Host "Modelli: laya, decision-4b (CPU; VRAM del cavallo intatta)"
