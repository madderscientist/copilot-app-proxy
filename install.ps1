param(
    [string]$PythonExecutable,
    [string]$PythonIndexUrl,
    [string]$NpmRegistry
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$node = (Get-Command node.exe -ErrorAction Stop).Source
$nodeVersion = (& $node --version).TrimStart('v')
if (([version]$nodeVersion).Major -lt 22) {
    throw 'Node.js 22 or later is required.'
}

$python = $null
$candidates = if ($PythonExecutable) { @($PythonExecutable) } else { @('py.exe', 'python.exe', 'python3.exe') }
foreach ($candidate in $candidates) {
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    $arguments = if ($command.Name -eq 'py.exe') { @('-3') } else { @() }
    $arguments += @('-c', 'import sys; assert sys.version_info >= (3, 12); print(sys.executable)')
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & $command.Source @arguments 2>$null
    $pythonExit = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($pythonExit -eq 0 -and $output) {
        $python = ([string]($output | Select-Object -Last 1)).Trim()
        break
    }
}
if (-not $python) {
    throw 'Python 3.12 or later is required. Install Python or pass -PythonExecutable with its full path.'
}

$venv = Join-Path $repoRoot '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project Python environment.' }
}
$pipArguments = @('-m', 'pip', 'install', '--disable-pip-version-check', '--progress-bar', 'off', '-r', (Join-Path $repoRoot 'requirements.txt'))
if ($PythonIndexUrl) { $pipArguments += @('--index-url', $PythonIndexUrl) }
& $venvPython @pipArguments
if ($LASTEXITCODE -ne 0) { throw 'Could not install the Python dependencies.' }

Push-Location $repoRoot
try {
    $npmArguments = @('ci', '--ignore-scripts', '--no-audit', '--no-fund')
    if ($NpmRegistry) { $npmArguments += @('--registry', $NpmRegistry) }
    & npm.cmd @npmArguments
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the Node.js dependencies.' }
} finally {
    Pop-Location
}

Write-Host 'Installation complete. No certificates, proxy settings, or accounts were changed.' -ForegroundColor Green
Write-Host 'Start with: .\start-transparent-proxy.ps1 -CopyKey'