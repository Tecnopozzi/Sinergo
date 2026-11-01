#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Fix del servizio Windows cloudflared che non parte perche' il binario
    sta in %LOCALAPPDATA%\...\WinGet (inaccessibile a LocalSystem).

    Copia cloudflared.exe in C:\Program Files\cloudflared\ e reinstalla
    il servizio da li', poi lo avvia.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$TargetDir  = "C:\Program Files\cloudflared"
$TargetExe  = Join-Path $TargetDir "cloudflared.exe"
$WinGetExe  = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"

Write-Host "==> Disinstallo il servizio esistente (se presente)" -ForegroundColor Cyan
$svc = Get-Service cloudflared -ErrorAction SilentlyContinue
if ($svc) {
    try { Stop-Service cloudflared -Force -ErrorAction SilentlyContinue } catch {}
    try { & "$WinGetExe" service uninstall 2>&1 | Out-Null } catch {}
    Start-Sleep -Seconds 3
    # Ultima spiaggia se uninstall non ha funzionato
    $svc = Get-Service cloudflared -ErrorAction SilentlyContinue
    if ($svc) {
        sc.exe delete cloudflared | Out-Null
        Start-Sleep -Seconds 2
    }
}

Write-Host "==> Creo $TargetDir" -ForegroundColor Cyan
New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null

if (-not (Test-Path $WinGetExe)) {
    throw "Non trovo cloudflared.exe in $WinGetExe. Reinstalla con: winget install Cloudflare.cloudflared"
}

Write-Host "==> Copio cloudflared.exe in Program Files" -ForegroundColor Cyan
Copy-Item -Path $WinGetExe -Destination $TargetExe -Force

# Concedo permessi di lettura ed esecuzione a Everyone (utile per SYSTEM e futuri aggiornamenti)
$acl = Get-Acl $TargetDir
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    "Everyone",
    "ReadAndExecute",
    "ContainerInherit,ObjectInherit",
    "None",
    "Allow"
)
$acl.AddAccessRule($rule)
Set-Acl -Path $TargetDir -AclObject $acl

Write-Host "==> Installo il servizio dalla nuova posizione" -ForegroundColor Cyan
& $TargetExe service install

Start-Sleep -Seconds 2

Write-Host "==> Avvio il servizio" -ForegroundColor Cyan
Start-Service cloudflared
Start-Sleep -Seconds 3

$svc = Get-Service cloudflared
Write-Host ""
Write-Host "==> Stato servizio:" -ForegroundColor Green
Write-Host "    Nome: $($svc.Name)"
Write-Host "    Status: $($svc.Status)"
Write-Host "    StartType: $($svc.StartType)"
Write-Host ""

if ($svc.Status -eq "Running") {
    Write-Host "OK: il servizio gira. Attendi ~10 secondi e prova:" -ForegroundColor Green
    Write-Host "    curl http://127.0.0.1:8088/health" -ForegroundColor DarkGray
} else {
    Write-Host "Il servizio non gira. Controlla l'event log con:" -ForegroundColor Yellow
    Write-Host '    Get-WinEvent -FilterHashtable @{LogName="Application"; StartTime=(Get-Date).AddMinutes(-5)} | Where-Object {$_.Message -like "*cloudflared*"}' -ForegroundColor DarkGray
}
