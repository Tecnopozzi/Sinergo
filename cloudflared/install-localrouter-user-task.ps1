#Requires -Version 5.1
<#
.SYNOPSIS
    Registra il Task Scheduler "LocalrouterServer" che avvia Localrouter
    all'accesso dell'utente Windows, in sessione interattiva — MAI come SYSTEM.

.DESCRIPTION
    Il gateway deve girare come lo stesso utente di LM Studio, altrimenti
    `lms load` fallisce in ~1s e le chat a freddo tornano 502.

    Trigger: AtLogon (utente corrente / -UserName).
    Principal: Interactive, RunLevel Limited (o Highest se -Highest).

.EXAMPLE
    # Come utente corrente (consigliato, senza Admin):
    powershell -ExecutionPolicy Bypass -File .\cloudflared\install-localrouter-user-task.ps1

    # Oppure Admin forzando un utente:
    powershell -ExecutionPolicy Bypass -File .\cloudflared\install-localrouter-user-task.ps1 -UserName NomeUtente
#>

[CmdletBinding()]
param(
    [string]$TaskName = "LocalrouterServer",
    [string]$ProjectDir = "C:\Localrouter",
    [string]$UserName = $env:USERNAME,
    [switch]$Highest,
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Server = Join-Path $ProjectDir "server.py"

foreach ($p in @($Python, $Server)) {
    if (-not (Test-Path $p)) { throw "Manca $p" }
}

# Avvio diretto python.exe (MAI wscript/vbs: alcuni antivirus li bloccano come falso positivo).
$Execute = $Python
$Argument = "server.py"

Write-Host "==> Rimuovo task SYSTEM legacy pericolosi (se presenti)..." -ForegroundColor Cyan
foreach ($legacy in @("PersonalOpenRouter", "Jarvis10bServer", "10bServer")) {
    $t = Get-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue
    if ($t) {
        $uid = $t.Principal.UserId
        Write-Host "    trovato $legacy (UserId=$uid) -> stop + unregister" -ForegroundColor Yellow
        Stop-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $legacy -Confirm:$false -ErrorAction SilentlyContinue
    }
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Aggiorno task esistente $TaskName" -ForegroundColor Cyan
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

Write-Host "==> Registro $TaskName come utente '$UserName' (Interactive, AtLogon)" -ForegroundColor Cyan

$action = New-ScheduledTaskAction `
    -Execute $Execute `
    -Argument $Argument `
    -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogon -User $UserName

$runLevel = if ($Highest) { "Highest" } else { "Limited" }
$principal = New-ScheduledTaskPrincipal `
    -UserId $UserName `
    -LogonType Interactive `
    -RunLevel $runLevel

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Localrouter gateway: avvio al logon come utente interattivo (mai SYSTEM)' `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

$info = Get-ScheduledTask -TaskName $TaskName
Write-Host "==> OK: State=$($info.State) User=$($info.Principal.UserId) Logon=$($info.Principal.LogonType) RunLevel=$($info.Principal.RunLevel)" -ForegroundColor Green

if ($StartNow) {
    Write-Host "==> StartNow: avvio task..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8088/health" -UseBasicParsing -TimeoutSec 10
        Write-Host "    /health $($r.StatusCode)" -ForegroundColor Green
    } catch {
        Write-Host "    /health non ancora pronto: $_" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Verifica: Get-ScheduledTask LocalrouterServer"
Write-Host "Dopo reboot: Get-Process python | Select Id,SessionId  -- SessionId deve essere >= 1 (non 0)."
