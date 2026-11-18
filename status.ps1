param(
    [int]$Port = 0
)

$ErrorActionPreference = 'Stop'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Python environment .venv\Scripts\python.exe was not found. Run .\install.ps1 first.'
}

$argsList = @('-m', 'copilot_app_proxy', 'status')
if ($Port -gt 0) { $argsList += @('--port', [string]$Port) }
& $venvPython @argsList
exit $LASTEXITCODE
