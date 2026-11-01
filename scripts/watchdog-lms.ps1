#Requires -Version 5.1
<#
.SYNOPSIS
  Watchdog LM Studio: se :1234 non risponde, `lms server start` + reload modello produzione.
  Uso: backup se il gateway e' spento. Il gateway ha gia' un loop interno equivalente.
#>
param(
  [int]$IntervalSec = 30,
  [int]$FailsBeforeRestart = 3,
  [string]$WarmupModel = "gemma-4-26b-a4b-it",
  [string]$LogPath = "C:\Localrouter\logs\watchdog-lms.log"
)

$ErrorActionPreference = "Continue"
$env:PATH = "$env:USERPROFILE\.lmstudio\bin;$env:PATH"
New-Item -ItemType Directory -Path (Split-Path $LogPath) -Force | Out-Null

function Log([string]$m) {
  $line = "{0:u} | {1}" -f (Get-Date), $m
  Add-Content -LiteralPath $LogPath -Value $line -Encoding utf8
  Write-Host $line
}

function Probe {
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:1234/v1/models" -UseBasicParsing -TimeoutSec 5
    return ($r.StatusCode -lt 500)
  } catch { return $false }
}

Log "watchdog start interval=$IntervalSec fails=$FailsBeforeRestart model=$WarmupModel"
$fails = 0
while ($true) {
  Start-Sleep -Seconds $IntervalSec
  if (Probe) {
    if ($fails -gt 0) { Log "recovered after fails=$fails" }
    $fails = 0
    continue
  }
  $fails++
  Log "probe fail $fails/$FailsBeforeRestart"
  if ($fails -lt $FailsBeforeRestart) { continue }
  Log "reviving: lms server start"
  & lms server start -p 1234 2>&1 | Out-String | ForEach-Object { Log $_ }
  Start-Sleep 3
  if (-not (Probe)) {
    Log "stop+start retry"
    & lms server stop 2>&1 | Out-Null
    Start-Sleep 2
    & lms server start -p 1234 2>&1 | Out-Null
    Start-Sleep 3
  }
  if (Probe -and $WarmupModel) {
    Log "reload $WarmupModel"
    & lms load $WarmupModel -c 16384 --gpu max --parallel 1 --ttl 86400 -y 2>&1 | Out-String | ForEach-Object { Log $_.Substring(0, [Math]::Min(200, $_.Length)) }
  }
  $fails = 0
}
