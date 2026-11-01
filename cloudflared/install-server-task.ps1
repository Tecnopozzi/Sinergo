#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    [DEPRECATO] Non usare: avviava il gateway come SYSTEM.

    Usare invece:
      cloudflared\install-localrouter-user-task.ps1

    Localrouter DEVE girare come utente interattivo (stesso di LM Studio),
    altrimenti `lms load` fallisce e le chat a freddo tornano 502.
#>

[CmdletBinding()]
param(
    [string]$TaskName   = "PersonalOpenRouter",
    [string]$ProjectDir = "C:\Localrouter"
)

$ErrorActionPreference = "Stop"

Write-Host "DEPRECATO: questo script registrava il gateway come SYSTEM." -ForegroundColor Red
Write-Host "Avvio install-localrouter-user-task.ps1 (utente interattivo)..." -ForegroundColor Cyan

$replacement = Join-Path $PSScriptRoot "install-localrouter-user-task.ps1"
if (-not (Test-Path $replacement)) {
    throw "Manca $replacement"
}

# Rimuovi il vecchio task SYSTEM legacy se presente
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Rimuovo task SYSTEM legacy '$TaskName'" -ForegroundColor Yellow
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

& $replacement -TaskName "LocalrouterServer" -ProjectDir $ProjectDir -StartNow
