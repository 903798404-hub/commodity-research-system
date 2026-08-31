[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('manual', 'scheduled')]
    [string]$TriggerSource
)

$ErrorActionPreference = 'Stop'
$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$PythonExecutable = Join-Path $RepositoryRoot '.venv-py312\Scripts\python.exe'
$Runner = Join-Path $PSScriptRoot 'run_full_daily_windows.py'

if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Exact market-data Python executable is unavailable."
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
& $PythonExecutable $Runner --trigger-source $TriggerSource
exit $LASTEXITCODE
