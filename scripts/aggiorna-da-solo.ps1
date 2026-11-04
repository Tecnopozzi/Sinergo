# Aggiornamento automatico di LocalRouter: segue origin/rilascio, riavvia gateway e collegato.
# Gira ogni 5 minuti da un'attivita' pianificata utente (aggiorna-da-solo-installa.ps1).
# Chi decide la versione e' chi pusha su `rilascio`: qui non si sceglie niente.
# Registro: logs\aggiorna.log (una riga per aggiornamento, riavvio o errore).
param([string]$Ramo = 'rilascio')
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
New-Item -ItemType Directory -Force (Join-Path $Root 'logs') | Out-Null
$Log = Join-Path $Root 'logs\aggiorna.log'
function Scrivi([string]$m) { Add-Content -LiteralPath $Log -Value ('{0:u} | {1}' -f (Get-Date), $m) -Encoding utf8 }
# git.exe esplicito: `git` risolverebbe a questa stessa funzione (nomi senza maiuscole) e ricorsione infinita.
# Continue: in PowerShell 5.1 lo stderr di git con 2>&1 diventa errore anche quando git riesce.
function Git { $ErrorActionPreference = 'Continue'; $o = & git.exe @args 2>&1; if ($LASTEXITCODE) { throw "git $($args -join ' '): $o" }; $o }

# Un solo giro alla volta: il lock e' la cartella, creata atomicamente.
$Lock = Join-Path $Root 'logs\aggiorna.lock'
try { New-Item -ItemType Directory $Lock -ErrorAction Stop | Out-Null } catch {
    if (((Get-Date) - (Get-Item $Lock).CreationTime).TotalMinutes -lt 30) { exit 0 }
    Scrivi 'lock vecchio di oltre 30 minuti: lo riprendo'
}
# Il giro e' una funzione: `return` esce dal giro e il finally (lock) gira di sicuro.
function Giro {
    Git fetch -q origin $Ramo | Out-Null
    $prima = (Git rev-parse HEAD).Trim()
    $dopo = (Git rev-parse "origin/$Ramo").Trim()
    if ($prima -eq $dopo) { return 0 }
    # Modifiche locali ai file tracciati: non le si schiaccia, le guarda un umano.
    $sporco = Git status --porcelain --untracked-files=no
    if ($sporco) { Scrivi "FERMO: file tracciati modificati, niente aggiornamento: $($sporco -join '; ')"; return 1 }
    $cambiati = Git diff --name-only $prima $dopo
    Git merge --ff-only -q "origin/$Ramo" | Out-Null
    Scrivi "aggiornato $($prima.Substring(0,7)) -> $($dopo.Substring(0,7))"

    # Gateway spento = spento apposta (es. libera-macchina Completo): file aggiornati, niente riavvio.
    $ascolto = Get-NetTCPConnection -LocalPort 8088 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ascolto) { Scrivi 'gateway spento: non lo riavvio, partira'' col codice nuovo'; return 0 }

    # ponytail: riavvio secco, le richieste in volo cadono; attesa dei job aperti se diventa un problema
    $mieiPy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.ExecutablePath -like "$Root\.venv\*" -and ($_.CommandLine -match 'server\.py' -or $_.CommandLine -match '-m collegato') }
    foreach ($p in $mieiPy) { Stop-Process -Id $p.ProcessId -Force; Scrivi "fermato PID $($p.ProcessId): $($p.CommandLine)" }
    Start-Sleep 2
    if ($cambiati -match '^(pyproject\.toml|uv\.lock)$') {
        $s = & uv sync 2>&1; Scrivi "uv sync: $LASTEXITCODE $(($s | Select-Object -Last 2) -join ' ')"
    }
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$Root\start-gateway.cmd`"" -WorkingDirectory $Root -WindowStyle Hidden
    $ok = $false
    for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
        Start-Sleep 2
        try { $ok = (Invoke-RestMethod http://127.0.0.1:8088/health -TimeoutSec 5).status -eq 'ok' } catch {}
    }
    Scrivi ($(if ($ok) { 'riavviato: /health ok' } else { 'ERRORE: /health non risponde dopo il riavvio' }))
    if ($ok) { 0 } else { 1 }
}
$codice = 1
try { $codice = Giro } catch { Scrivi "ERRORE: $_" } finally {
    Remove-Item -LiteralPath $Lock -Force -ErrorAction SilentlyContinue
}
exit $codice
