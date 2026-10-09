param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
try {
    $taskConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskControlRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $taskEntry = Join-Path $taskControlRoot '04_scripts\automation\run_production_data_delta_windows.py'
    & $taskConfig.python -I -B -X utf8 $taskEntry --config $ConfigPath --domain foreign_fx --publish --retry-if-needed
    exit $LASTEXITCODE
} catch {
    Write-Error $_
    exit 1
}
