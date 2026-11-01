#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installa cloudflared come servizio Windows per il tunnel LocalRouter.

.DESCRIPTION
    - Copia cert.pem, credenziali e config.yml nel profilo SYSTEM (%WINDIR%\System32\config\systemprofile\.cloudflared\).
    - Registra il servizio Windows "cloudflared" con partenza automatica al boot.
    - Avvia subito il servizio.

.NOTES
    Esegui come amministratore.
    Usa config.yml dalla stessa cartella dello script (cloudflared\config.yml).
    Passa -TunnelId e -TunnelName del tuo tunnel Cloudflare.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TunnelId,
    [string]$TunnelName = "localrouter"
)

$ErrorActionPreference = "Stop"

$UserCfDir    = Join-Path $env:USERPROFILE ".cloudflared"
$SystemCfDir  = Join-Path $env:WINDIR "System32\config\systemprofile\.cloudflared"
$ProjectCfg   = Join-Path $PSScriptRoot "config.yml"

Write-Host "==> Verifica prerequisiti" -ForegroundColor Cyan

$cloudflared = Get-Command cloudflared -ErrorAction SilentlyContinue
if (-not $cloudflared) {
    throw "cloudflared non trovato nel PATH. Installalo con: winget install Cloudflare.cloudflared"
}
Write-Host "    cloudflared: $($cloudflared.Source)"

foreach ($f in @("cert.pem", "$TunnelId.json")) {
    $p = Join-Path $UserCfDir $f
    if (-not (Test-Path $p)) { throw "Manca il file $p" }
}
if (-not (Test-Path $ProjectCfg)) { throw "Manca $ProjectCfg" }

Write-Host "==> Preparo cartella SYSTEM $SystemCfDir" -ForegroundColor Cyan
New-Item -ItemType Directory -Path $SystemCfDir -Force | Out-Null

Write-Host "==> Copio cert, credenziali e config" -ForegroundColor Cyan
Copy-Item (Join-Path $UserCfDir "cert.pem")          (Join-Path $SystemCfDir "cert.pem")          -Force
Copy-Item (Join-Path $UserCfDir "$TunnelId.json")    (Join-Path $SystemCfDir "$TunnelId.json")    -Force

$cfg = Get-Content $ProjectCfg -Raw
$userCred = Join-Path $UserCfDir "$TunnelId.json"
$cfg = $cfg -replace [regex]::Escape($userCred), (Join-Path $SystemCfDir "$TunnelId.json")
$cfg = $cfg -replace [regex]::Escape(($userCred -replace '\\', '/')), ((Join-Path $SystemCfDir "$TunnelId.json") -replace '\\', '/')
Set-Content -Path (Join-Path $SystemCfDir "config.yml") -Value $cfg -Encoding UTF8

Write-Host "==> Se esiste gia' un servizio cloudflared, lo rimuovo" -ForegroundColor Cyan
$svc = Get-Service cloudflared -ErrorAction SilentlyContinue
if ($svc) {
    try { & cloudflared service uninstall 2>&1 | Out-Null } catch {}
    Start-Sleep -Seconds 2
}

Write-Host "==> Registro il servizio Windows" -ForegroundColor Cyan
& cloudflared service install

Start-Sleep -Seconds 2

Write-Host "==> Avvio il servizio" -ForegroundColor Cyan
Start-Service cloudflared
Start-Sleep -Seconds 2

$svc = Get-Service cloudflared
Write-Host ""
Write-Host "==> Stato servizio:" -ForegroundColor Green
Write-Host "    Nome: $($svc.Name)"
Write-Host "    Status: $($svc.Status)"
Write-Host "    StartType: $($svc.StartType)"
Write-Host ""
Write-Host "Logs in: C:\Windows\System32\config\systemprofile\.cloudflared\*.log" -ForegroundColor DarkGray
Write-Host "Gestione: Start-Service / Stop-Service / Restart-Service cloudflared" -ForegroundColor DarkGray
