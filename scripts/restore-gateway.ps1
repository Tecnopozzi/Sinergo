#Requires -Version 5.1
<#
.SYNOPSIS
    Ripristino gateway + autostart sicuro (no schtasks/wscript). Idempotente.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\Localrouter\scripts\restore-gateway.ps1
#>
param([string]$ProjectDir = "C:\Localrouter")

$ErrorActionPreference = "Stop"
$server = Join-Path $ProjectDir "server.py"
if (-not (Test-Path $server)) { $server = Join-Path $ProjectDir "run_server.py" }
$serverArg = Split-Path $server -Leaf
$python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$lms = Join-Path $env:USERPROFILE ".lmstudio\bin\lms.exe"
if (-not (Test-Path $python)) { throw "Manca $python" }
if (-not (Test-Path $server)) { throw "Manca server.py (e la copia di ripiego run_server.py)" }
$log = Join-Path $ProjectDir "logs\restore-gateway.log"
New-Item -ItemType Directory -Path (Split-Path $log) -Force | Out-Null

function Log([string]$m) {
    $line = "{0:u} | {1}" -f (Get-Date), $m
    Add-Content -LiteralPath $log -Value $line -Encoding utf8
    Write-Host $m
}

Log "=== restore-gateway ==="

# Rimuovi shortcut pericolosi (wscript/vbs)
$startup = [Environment]::GetFolderPath("Startup")
$bad = Join-Path $startup "LocalrouterServer.lnk"
if (Test-Path $bad) {
    $w = New-Object -ComObject WScript.Shell
    $s = $w.CreateShortcut($bad)
    if ($s.TargetPath -match "wscript") {
        Remove-Item $bad -Force
        Log "Rimosso $bad (wscript)"
    }
}

try {
    & (Join-Path $ProjectDir "scripts\install-autostart-user.ps1") -ProjectDir $ProjectDir
    Log "Autostart shortcut OK"
} catch {
    Log "Autostart shortcut SKIP (antivirus?): $($_.Exception.Message)"
}

# Kill listener :8088
Get-NetTCPConnection -LocalPort 8088 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

$env:PATH = "$(Split-Path $lms);$env:PATH"
Start-Process -FilePath $python -ArgumentList $serverArg -WorkingDirectory $ProjectDir -WindowStyle Minimized
Log "Avviato $serverArg"

$deadline = (Get-Date).AddMinutes(2)
do {
    Start-Sleep -Seconds 3
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8088/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { Log "health 200"; break }
    } catch { }
} while ((Get-Date) -lt $deadline)

# Fix gemma context via gateway (unload + chat)
if (Test-Path $lms) {
    & $lms unload gemma-4-26b-a4b-it 2>&1 | Out-Null
    Start-Sleep -Seconds 3
    Log "gemma unloaded"
}

$apiKey = (Get-Content (Join-Path $ProjectDir ".env") -Raw | Select-String 'API_KEY=(.+)').Matches.Groups[1].Value.Trim()
$body = @{
    model = "lmstudio:gemma-4-26b-a4b-it"
    messages = @(@{ role = "user"; content = "OK" })
    max_tokens = 5
} | ConvertTo-Json -Depth 5

try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $chat = Invoke-WebRequest -Uri "http://127.0.0.1:8088/v1/chat/completions" -Method POST `
        -Headers @{ Authorization = "Bearer $apiKey"; "Content-Type" = "application/json" } `
        -Body $body -UseBasicParsing -TimeoutSec 600
    $sw.Stop()
    Log "chat $($chat.StatusCode) in $($sw.Elapsed.TotalSeconds)s"
} catch {
    Log "chat FAIL: $($_.Exception.Message)"
}

if (Test-Path $lms) {
    $ps = & $lms ps 2>&1 | Out-String
    Log "lms ps:`n$ps"
}

Log "=== fine ==="
