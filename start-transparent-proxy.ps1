param(
    [int]$GatewayPort = 19324,
    [int]$ProxyPort = 19323,
    [string]$MitmproxyPath,
    [string]$CopilotPath,
    [switch]$CopyKey
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security

$repoRoot = $PSScriptRoot
$localDir = Join-Path $repoRoot '.local'
New-Item -ItemType Directory -Path $localDir -Force | Out-Null

$mitmExe = if ($MitmproxyPath) { $MitmproxyPath } else { Join-Path $repoRoot '.venv\Scripts\mitmdump.exe' }
if (-not (Test-Path -LiteralPath $mitmExe)) {
    throw 'mitmdump.exe was not found. Run .\install.ps1 first, or pass -MitmproxyPath.'
}

$pidsFile = Join-Path $localDir 'pids.json'
if (Test-Path -LiteralPath $pidsFile) {
    try {
        $oldPids = Get-Content -Raw -LiteralPath $pidsFile | ConvertFrom-Json
        foreach ($procId in @($oldPids.gatewayPid, $oldPids.mitmPid)) {
            if ($procId) {
                $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
                if ($proc) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
            }
        }
    } catch {}
}

$keyFile = Join-Path $localDir 'gateway-key.dpapi'
if (Test-Path -LiteralPath $keyFile) {
    $enc = (Get-Content -Raw -LiteralPath $keyFile).Trim()
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($enc), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    $localGatewayKey = [Text.Encoding]::UTF8.GetString($bytes)
} else {
    $localGatewayKey = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
    $bytes = [Text.Encoding]::UTF8.GetBytes($localGatewayKey)
    $enc = [Convert]::ToBase64String([Security.Cryptography.ProtectedData]::Protect($bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser))
    [IO.File]::WriteAllText($keyFile, $enc, [Text.Encoding]::UTF8)
}

if ($CopyKey) {
    $localGatewayKey | Set-Clipboard
    Write-Host 'Copied LOCAL_GATEWAY_KEY to clipboard.' -ForegroundColor Green
}

$relaySecret = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')

$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Python environment .venv\Scripts\python.exe was not found. Run .\install.ps1 first.'
}

$gwPsi = New-Object System.Diagnostics.ProcessStartInfo
$gwPsi.FileName = $venvPython
$gwPsi.Arguments = "-m copilot_app_proxy --relay --port $GatewayPort"
$gwPsi.WorkingDirectory = $repoRoot
$gwPsi.UseShellExecute = $false
$gwPsi.CreateNoWindow = $true
$gwPsi.EnvironmentVariables['PORT'] = [string]$GatewayPort
$gwPsi.EnvironmentVariables['LOCAL_GATEWAY_KEY'] = $localGatewayKey
$gwPsi.EnvironmentVariables['RELAY_INGEST_SECRET'] = $relaySecret
$gwProc = [Diagnostics.Process]::Start($gwPsi)

$caDir = Join-Path $localDir 'mitm-ca'
New-Item -ItemType Directory -Path $caDir -Force | Out-Null
$mitmPsi = New-Object System.Diagnostics.ProcessStartInfo
$mitmPsi.FileName = $mitmExe
$mitmPsi.Arguments = "--listen-host 127.0.0.1 --listen-port $ProxyPort --set confdir=`"$caDir`" --set flow_detail=0 --set termlog_verbosity=error --scripts `"$(Join-Path $repoRoot 'relay_addon.py')`""
$mitmPsi.WorkingDirectory = $repoRoot
$mitmPsi.UseShellExecute = $false
$mitmPsi.CreateNoWindow = $true
$mitmPsi.EnvironmentVariables['RELAY_INGEST_URL'] = "http://127.0.0.1:$GatewayPort/_internal/token"
$mitmPsi.EnvironmentVariables['RELAY_INGEST_SECRET'] = $relaySecret
$mitmProc = [Diagnostics.Process]::Start($mitmPsi)

@{ gatewayPid = $gwProc.Id; mitmPid = $mitmProc.Id } | ConvertTo-Json | Set-Content -LiteralPath $pidsFile -Encoding UTF8

$deadline = (Get-Date).AddSeconds(15)
$certPath = Join-Path $caDir 'mitmproxy-ca-cert.cer'
while ((Get-Date) -lt $deadline) {
    $gwListen = Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction SilentlyContinue
    $mitmListen = Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue
    if ($gwListen -and $mitmListen -and (Test-Path -LiteralPath $certPath)) { break }
    Start-Sleep -Milliseconds 250
}

if (-not (Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction SilentlyContinue)) {
    throw 'Gateway failed to listen.'
}
if (-not (Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue)) {
    throw 'mitmdump failed to listen.'
}

$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($certPath)
$thumbFile = Join-Path $localDir 'ca-thumbprint.txt'
Set-Content -LiteralPath $thumbFile -Value $cert.Thumbprint -Encoding ASCII
$storePath = 'Cert:\CurrentUser\Root\' + $cert.Thumbprint
if (-not (Test-Path -LiteralPath $storePath)) {
    $proc = Start-Process -FilePath 'certutil.exe' -ArgumentList @('-user', '-addstore', 'Root', $certPath) -Wait -PassThru
    if ($proc.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $storePath)) {
        throw 'Temporary CA certificate was not trusted in Cert:\CurrentUser\Root.'
    }
}

$copilotApps = @(Get-CimInstance Win32_Process | Where-Object Name -eq 'copilotapp.exe')
$appIds = @($copilotApps.ProcessId)
$roots = @($copilotApps | Where-Object ParentProcessId -notin $appIds)
$copilotExe = if ($CopilotPath) { $CopilotPath } else { ($roots | Select-Object -First 1).ExecutablePath }
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
if (Get-Process copilotapp -ErrorAction SilentlyContinue) {
    throw 'Existing Copilot processes did not exit in time.'
}

$cpPsi = New-Object System.Diagnostics.ProcessStartInfo
$cpPsi.FileName = $copilotExe
$cpPsi.Arguments = "--proxy-server=http://127.0.0.1:$ProxyPort --disable-quic"
$cpPsi.UseShellExecute = $false
$cpPsi.EnvironmentVariables['HTTP_PROXY'] = "http://127.0.0.1:$ProxyPort"
$cpPsi.EnvironmentVariables['HTTPS_PROXY'] = "http://127.0.0.1:$ProxyPort"
$cpPsi.EnvironmentVariables['NO_PROXY'] = 'localhost,127.0.0.1,::1'
$cpProc = [Diagnostics.Process]::Start($cpPsi)

Write-Host "Transparent proxy started." -ForegroundColor Cyan
Write-Host "Gateway: http://127.0.0.1:$GatewayPort/v1/messages (PID: $($gwProc.Id))"
Write-Host "MITM Proxy: http://127.0.0.1:$ProxyPort (PID: $($mitmProc.Id))"
Write-Host "Copilot App restarted (PID: $($cpProc.Id))."
