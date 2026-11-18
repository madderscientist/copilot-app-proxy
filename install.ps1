param(
    [string]$PythonExecutable,
    [string]$PythonIndexUrl
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot

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

$cliArgs = @('-m', 'copilot_app_proxy.cli', 'install')
if ($PythonIndexUrl) { $cliArgs += @('--index-url', $PythonIndexUrl) }
Push-Location -LiteralPath $repoRoot
try {
    & $python @cliArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}