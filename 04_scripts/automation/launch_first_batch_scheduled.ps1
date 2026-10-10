param([Parameter(Mandatory=$true)][string]$ConfigPath)
$ErrorActionPreference = 'Stop'
try {
    $taskConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskControlRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    & $taskConfig.delivery.python -I -B -X utf8 (Join-Path $taskControlRoot '04_scripts\automation\run_first_batch_scheduled.py') --config $ConfigPath --publish
    exit $LASTEXITCODE
} catch {
    Write-Error $_
    exit 1
}
