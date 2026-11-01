#Requires -Version 5.1
<#
.SYNOPSIS
    [DEPRECATO] Reindirizza a install-autostart-user.ps1 (python diretto, no wscript).

    wscript.exe + .vbs e' un pattern che alcuni antivirus bloccano come falso positivo.
#>

param([string]$ProjectDir = "C:\Localrouter")

Write-Host "DEPRECATO: uso install-autostart-user.ps1 (no wscript/vbs)." -ForegroundColor Yellow
$replacement = Join-Path $ProjectDir "scripts\install-autostart-user.ps1"
if (-not (Test-Path $replacement)) { throw "Manca $replacement" }
& $replacement -ProjectDir $ProjectDir
