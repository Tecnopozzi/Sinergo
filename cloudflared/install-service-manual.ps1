#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installa il servizio Windows cloudflared con binPath completo
    (cloudflared service install di 2025.x non piu' fa questo da solo).
#>

[CmdletBinding()]
param(
    [string]$TunnelName = "localrouter"
)

$ErrorActionPreference = "Continue"

$Exe    = "C:\Program Files\cloudflared\cloudflared.exe"
$SysCfg = "C:\Windows\System32\config\systemprofile\.cloudflared\config.yml"

if (-not (Test-Path $Exe))    { throw "Manca $Exe" }
if (-not (Test-Path $SysCfg)) { throw "Manca $SysCfg (rilancia prima diag-service.ps1)" }

Write-Host "==> Stop e delete servizio esistente" -ForegroundColor Cyan
sc.exe stop Cloudflared 2>&1 | Out-Null
Start-Sleep -Seconds 2
sc.exe delete Cloudflared 2>&1 | Out-Null
Start-Sleep -Seconds 2
Remove-Item "HKLM:\SYSTEM\CurrentControlSet\Services\EventLog\Application\Cloudflared" -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "==> Creo il servizio con New-Service (gestisce meglio gli spazi)" -ForegroundColor Cyan
$binPath = "`"$Exe`" --config `"$SysCfg`" --no-autoupdate tunnel run $TunnelName"
Write-Host "    binPath: $binPath" -ForegroundColor DarkGray
New-Service -Name "Cloudflared" `
            -BinaryPathName $binPath `
            -DisplayName "Cloudflared agent" `
            -Description "Runs the cloudflared tunnel for $TunnelName" `
            -StartupType Automatic | Out-Null

& sc.exe failure Cloudflared reset= 86400 actions= restart/10000/restart/10000/restart/60000 | Out-Null

Write-Host "==> binPath effettivamente registrato:" -ForegroundColor Cyan
& sc.exe qc Cloudflared | Select-String "BINARY_PATH_NAME" | ForEach-Object { "    $_" }

Write-Host "==> Avvio il servizio" -ForegroundColor Cyan
Start-Service Cloudflared
Start-Sleep -Seconds 4

$svc = Get-Service Cloudflared
Write-Host ""
Write-Host "==> Stato finale:" -ForegroundColor Green
Write-Host "    Name:      $($svc.Name)"
Write-Host "    Status:    $($svc.Status)"
Write-Host "    StartType: $($svc.StartType)"
Write-Host ""

if ($svc.Status -eq "Running") {
    Write-Host "SERVIZIO ATTIVO. Verifica HTTPS:" -ForegroundColor Green
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8088/health" -UseBasicParsing -TimeoutSec 15
        Write-Host "    /health -> $($r.StatusCode): $($r.Content)" -ForegroundColor Green
    } catch {
        Write-Host "    Tunnel probabilmente ancora in warm-up, riprova tra 10 secondi: $_" -ForegroundColor Yellow
    }
} else {
    Write-Host "Servizio ancora stopped. Verifico eventi..." -ForegroundColor Yellow
    Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddMinutes(-2)} -MaxEvents 10 -ErrorAction SilentlyContinue |
        Where-Object { $_.ProviderName -like "*loudflared*" } |
        Select-Object TimeCreated, LevelDisplayName, Message | Format-List
}
