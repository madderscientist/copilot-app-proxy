$ErrorActionPreference = 'Stop'

$repoRoot = $PSScriptRoot
$localDir = Join-Path $repoRoot '.local'
$pidsFile = Join-Path $localDir 'pids.json'

if (Test-Path -LiteralPath $pidsFile) {
    $pids = Get-Content -Raw -LiteralPath $pidsFile | ConvertFrom-Json
    foreach ($procId in @($pids.gatewayPid, $pids.mitmPid)) {
        if ($procId) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $pidsFile -Force -ErrorAction SilentlyContinue
}

$thumbFile = Join-Path $localDir 'ca-thumbprint.txt'
if (Test-Path -LiteralPath $thumbFile) {
    $thumb = (Get-Content -Raw -LiteralPath $thumbFile).Trim()
    if ($thumb) {
        $storePath = 'Cert:\CurrentUser\Root\' + $thumb
        if (Test-Path -LiteralPath $storePath) {
            Remove-Item -LiteralPath $storePath -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $thumbFile -Force -ErrorAction SilentlyContinue
}

$caDir = Join-Path $localDir 'mitm-ca'
if (Test-Path -LiteralPath $caDir) {
    Remove-Item -LiteralPath $caDir -Recurse -Force -ErrorAction SilentlyContinue
}

$copilotApps = @(Get-CimInstance Win32_Process | Where-Object Name -eq 'copilotapp.exe')
$appIds = @($copilotApps.ProcessId)
$roots = @($copilotApps | Where-Object ParentProcessId -notin $appIds)
$copilotExe = ($roots | Select-Object -First 1).ExecutablePath
if (-not $copilotExe) {
    $copilotExe = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Copilot\Application\copilotapp.exe'
}
foreach ($root in $roots) {
    & taskkill.exe /PID $root.ProcessId /T /F | Out-Null
}
$waitEnd = (Get-Date).AddSeconds(8)
while ((Get-Process copilotapp -ErrorAction SilentlyContinue) -and (Get-Date) -lt $waitEnd) {
    Start-Sleep -Milliseconds 250
}

$cpPsi = New-Object System.Diagnostics.ProcessStartInfo
$cpPsi.FileName = $copilotExe
$cpPsi.UseShellExecute = $false
foreach ($proxyName in @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')) {
    $cpPsi.EnvironmentVariables.Remove($proxyName)
}
$null = [Diagnostics.Process]::Start($cpPsi)
Write-Host 'Transparent proxy stopped, temporary CA removed, and Copilot App restored.' -ForegroundColor Green
