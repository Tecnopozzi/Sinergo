# Helper minimo per leggere variabili da .env (usato da setup/verify whisper).

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Key,
        [string]$Default = ""
    )
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) { return $Default }
    foreach ($row in Get-Content $envFile -ErrorAction SilentlyContinue) {
        if ($row -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*)$") {
            $val = $Matches[1].Trim()
            if ($val.StartsWith('"') -and $val.EndsWith('"')) {
                $val = $val.Substring(1, $val.Length - 2)
            }
            return $val
        }
    }
    return $Default
}

function Resolve-WhisperModelsDir {
    param([Parameter(Mandatory = $true)][string]$Root)
    $raw = Get-DotEnvValue -Root $Root -Key "WHISPER_MODELS_DIR" -Default "D:\Model\whisper"
    if ([System.IO.Path]::IsPathRooted($raw)) {
        return $raw
    }
    return (Join-Path $Root ($raw -replace '/', '\'))
}

function Resolve-MediaModelsDir {
    param([Parameter(Mandatory = $true)][string]$Root)
    $raw = Get-DotEnvValue -Root $Root -Key "MEDIA_MODELS_DIR" -Default "D:\Model\media"
    if ([System.IO.Path]::IsPathRooted($raw)) {
        return $raw
    }
    return (Join-Path $Root ($raw -replace '/', '\'))
}
