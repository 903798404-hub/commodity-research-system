param([string]$PythonPath, [int]$Port = 8512)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw '请通过 -PythonPath 指定已经预检的 Python 3.12 虚拟环境解释器。'
}
if ($env:MARKET_DATA_GIT_HEAD -or $env:MARKET_DATA_EXECUTION_GRANT -or (Test-Path -LiteralPath (Join-Path $repoRoot 'RELEASE.json'))) {
    throw '该入口仅用于本地开发。'
}
$previousFlag = $env:COMMODITY_IMPORT_LOCAL_PREVIEW
$previousEncoding = $env:PYTHONIOENCODING
try {
    $env:COMMODITY_IMPORT_LOCAL_PREVIEW = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    & $PythonPath -m streamlit run (Join-Path $repoRoot '05_apps/import_margin_preview.py') --server.address 127.0.0.1 --server.port $Port --server.headless true --browser.gatherUsageStats false
    if ($LASTEXITCODE -ne 0) { throw "本地页面退出码：$LASTEXITCODE" }
} finally {
    $env:COMMODITY_IMPORT_LOCAL_PREVIEW = $previousFlag
    $env:PYTHONIOENCODING = $previousEncoding
}
