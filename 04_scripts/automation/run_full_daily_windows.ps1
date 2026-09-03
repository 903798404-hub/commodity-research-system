[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('manual', 'scheduled')]
    [string]$TriggerSource
)

$ErrorActionPreference = 'Stop'
$CanonicalRepository = 'C:\Users\xx202\Desktop\codex自动更新\codex-projects\market-data'
$PythonExecutable = Join-Path $CanonicalRepository '.venv-py312\Scripts\python.exe'
$ApprovedCommit = $env:MARKET_DATA_FULL_DAILY_PRODUCTION_COMMIT

if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Exact market-data Python executable is unavailable."
}
if ($ApprovedCommit -notmatch '^[0-9a-f]{40}$') {
    throw "Approved production commit must be a full lowercase Git SHA."
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$BootstrapRoot = Join-Path $env:LOCALAPPDATA 'market-data-runtime\bootstrap'
$BootstrapName = "full-daily-windows-$ApprovedCommit-$([guid]::NewGuid().ToString('N')).py"
$Bootstrap = Join-Path $BootstrapRoot $BootstrapName
$BootstrapObject = "${ApprovedCommit}:04_scripts/automation/full_daily_windows_bootstrap.py"
New-Item -ItemType Directory -Path $BootstrapRoot -Force | Out-Null

try {
    & git -C $CanonicalRepository show $BootstrapObject |
        Set-Content -LiteralPath $Bootstrap -Encoding utf8NoBOM
    if ($LASTEXITCODE -ne 0) {
        throw "Approved FULL DAILY bootstrap object is unavailable."
    }
    & $PythonExecutable -I $Bootstrap --trigger-source $TriggerSource --source-repository $CanonicalRepository
    $WrapperExitCode = $LASTEXITCODE
}
finally {
    Remove-Item -LiteralPath $Bootstrap -Force -ErrorAction SilentlyContinue
}
exit $WrapperExitCode
