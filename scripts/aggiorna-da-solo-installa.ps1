# Registra l'attivita' pianificata che ogni 5 minuti lancia aggiorna-da-solo.ps1 (utente corrente, senza finestra).
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'aggiorna-da-solo.ps1'
if (-not (Test-Path $script)) { throw "manca $script" }
# conhost --headless: nessuna finestra che lampeggia ogni 5 minuti sul desktop di chi usa il PC.
$azione = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$quando = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$regole = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'localrouter-aggiorna' -Action $azione -Trigger $quando -Settings $regole -Force | Out-Null
Write-Host 'attivita localrouter-aggiorna registrata: ogni 5 minuti, segue origin/rilascio'
