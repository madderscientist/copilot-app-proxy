param(
    [int]$GatewayPort = 19324,
    [int]$ProxyPort = 19323
)

$ErrorActionPreference = 'Stop'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Python environment .venv\Scripts\python.exe was not found. Run .\install.ps1 first.'
}

& $venvPython -m copilot_app_proxy doctor --gateway-port $GatewayPort --proxy-port $ProxyPort
exit $LASTEXITCODE
