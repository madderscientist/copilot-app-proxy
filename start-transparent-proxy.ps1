param(
    [int]$GatewayPort = 19324,
    [int]$ProxyPort = 19323,
    [string]$MitmproxyPath,
    [string]$CopilotPath,
    [switch]$CopyKey
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security

if ($GatewayPort -lt 1 -or $GatewayPort -gt 65535 -or $ProxyPort -lt 1 -or $ProxyPort -gt 65535) {
    throw 'GatewayPort and ProxyPort must be between 1 and 65535.'
}
if ($GatewayPort -eq $ProxyPort) {
    throw "GatewayPort and ProxyPort must be different (both set to $GatewayPort)."
}

$repoRoot = $PSScriptRoot
$localDir = Join-Path $repoRoot '.local'
$logsDir = Join-Path $localDir 'logs'
New-Item -ItemType Directory -Path $localDir, $logsDir -Force | Out-Null

$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Python environment .venv\Scripts\python.exe was not found. Run .\install.ps1 first.'
}

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
        Start-Sleep -Milliseconds 200
    } catch {}
}

foreach ($portToCheck in @($GatewayPort, $ProxyPort)) {
    $inUse = Get-NetTCPConnection -LocalPort $portToCheck -State Listen -ErrorAction SilentlyContinue
    if ($inUse) {
        $ownerPid = ($inUse | Select-Object -First 1).OwningProcess
        throw "Port $portToCheck is already in use by PID $ownerPid. Stop it or specify another port."
    }
}

$copilotApps = @(Get-CimInstance Win32_Process | Where-Object Name -eq 'copilotapp.exe')
$appIds = @($copilotApps.ProcessId)
$roots = @($copilotApps | Where-Object ParentProcessId -notin $appIds)
$copilotExe = if ($CopilotPath) { $CopilotPath } else { ($roots | Select-Object -First 1).ExecutablePath }
if (-not $copilotExe) {
    $copilotCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Copilot\Application\copilotapp.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Copilot\Application\copilotapp.exe'),
        (Join-Path $env:LocalAppData 'Microsoft\Copilot\Application\copilotapp.exe'),
        (Join-Path $env:LocalAppData 'Programs\Microsoft Copilot\copilotapp.exe')
    )
    $copilotExe = $copilotCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (-not $copilotExe -or -not (Test-Path -LiteralPath $copilotExe)) {
    throw 'copilotapp.exe was not found. Open Copilot App first or pass -CopilotPath.'
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
$gwOutLog = Join-Path $logsDir 'gateway.log'
$gwErrLog = Join-Path $logsDir 'gateway.err.log'
$mitmOutLog = Join-Path $logsDir 'mitm.log'
$mitmErrLog = Join-Path $logsDir 'mitm.err.log'

$gwProc = $null
$mitmProc = $null

try {
    $gwPsi = New-Object System.Diagnostics.ProcessStartInfo
    $gwPsi.FileName = $venvPython
    $gwPsi.Arguments = "-m copilot_app_proxy --relay --port $GatewayPort"
    $gwPsi.WorkingDirectory = $repoRoot
    $gwPsi.UseShellExecute = $false
    $gwPsi.CreateNoWindow = $true
    $gwPsi.RedirectStandardOutput = $true
    $gwPsi.RedirectStandardError = $true
    $gwPsi.EnvironmentVariables['PORT'] = [string]$GatewayPort
    $gwPsi.EnvironmentVariables['LOCAL_GATEWAY_KEY'] = $localGatewayKey
    $gwPsi.EnvironmentVariables['RELAY_INGEST_SECRET'] = $relaySecret
    $gwProc = [Diagnostics.Process]::Start($gwPsi)
    $null = Register-ObjectEvent -InputObject $gwProc -EventName OutputDataReceived -Action {
        if ($EventArgs.Data) { [IO.File]::AppendAllText($using:gwOutLog, $EventArgs.Data + [Environment]::NewLine) }
    }
    $null = Register-ObjectEvent -InputObject $gwProc -EventName ErrorDataReceived -Action {
        if ($EventArgs.Data) { [IO.File]::AppendAllText($using:gwErrLog, $EventArgs.Data + [Environment]::NewLine) }
    }
    [IO.File]::WriteAllText($gwOutLog, '')
    [IO.File]::WriteAllText($gwErrLog, '')
    $gwProc.BeginOutputReadLine()
    $gwProc.BeginErrorReadLine()

    $caDir = Join-Path $localDir 'mitm-ca'
    New-Item -ItemType Directory -Path $caDir -Force | Out-Null
    $mitmPsi = New-Object System.Diagnostics.ProcessStartInfo
    $mitmPsi.FileName = $mitmExe
    $mitmPsi.Arguments = "--listen-host 127.0.0.1 --listen-port $ProxyPort --set confdir=`"$caDir`" --set flow_detail=0 --set termlog_verbosity=error --scripts `"$(Join-Path $repoRoot 'relay_addon.py')`""
    $mitmPsi.WorkingDirectory = $repoRoot
    $mitmPsi.UseShellExecute = $false
    $mitmPsi.CreateNoWindow = $true
    $mitmPsi.RedirectStandardOutput = $true
    $mitmPsi.RedirectStandardError = $true
    $mitmPsi.EnvironmentVariables['RELAY_INGEST_URL'] = "http://127.0.0.1:$GatewayPort/_internal/token"
    $mitmPsi.EnvironmentVariables['RELAY_INGEST_SECRET'] = $relaySecret
    $mitmProc = [Diagnostics.Process]::Start($mitmPsi)
    $null = Register-ObjectEvent -InputObject $mitmProc -EventName OutputDataReceived -Action {
        if ($EventArgs.Data) { [IO.File]::AppendAllText($using:mitmOutLog, $EventArgs.Data + [Environment]::NewLine) }
    }
    $null = Register-ObjectEvent -InputObject $mitmProc -EventName ErrorDataReceived -Action {
        if ($EventArgs.Data) { [IO.File]::AppendAllText($using:mitmErrLog, $EventArgs.Data + [Environment]::NewLine) }
    }
    [IO.File]::WriteAllText($mitmOutLog, '')
    [IO.File]::WriteAllText($mitmErrLog, '')
    $mitmProc.BeginOutputReadLine()
    $mitmProc.BeginErrorReadLine()

    $deadline = (Get-Date).AddSeconds(15)
    $certPath = Join-Path $caDir 'mitmproxy-ca-cert.cer'
    while ((Get-Date) -lt $deadline) {
        if ($gwProc.HasExited) {
            $errText = if (Test-Path -LiteralPath $gwErrLog) { Get-Content -Raw -LiteralPath $gwErrLog } else { '' }
            throw "Gateway exited early (code $($gwProc.ExitCode)). $errText"
        }
        if ($mitmProc.HasExited) {
            $errText = if (Test-Path -LiteralPath $mitmErrLog) { Get-Content -Raw -LiteralPath $mitmErrLog } else { '' }
            throw "mitmdump exited early (code $($mitmProc.ExitCode)). $errText"
        }
        $gwListen = Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction SilentlyContinue
        $mitmListen = Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue
        if ($gwListen -and $mitmListen -and (Test-Path -LiteralPath $certPath)) { break }
        Start-Sleep -Milliseconds 250
    }

    if (-not (Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction SilentlyContinue)) {
        throw "Gateway failed to listen on port $GatewayPort. Check $gwErrLog"
    }
    if (-not (Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue)) {
        throw "mitmdump failed to listen on port $ProxyPort. Check $mitmErrLog"
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

    @{
        gatewayPid  = $gwProc.Id
        mitmPid     = $mitmProc.Id
        gatewayPort = $GatewayPort
        proxyPort   = $ProxyPort
    } | ConvertTo-Json | Set-Content -LiteralPath $pidsFile -Encoding UTF8

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

    Write-Host 'Transparent proxy started.' -ForegroundColor Cyan
    Write-Host "Gateway:    http://127.0.0.1:$GatewayPort/v1/messages (PID: $($gwProc.Id))"
    Write-Host "MITM Proxy: http://127.0.0.1:$ProxyPort (PID: $($mitmProc.Id))"
    Write-Host "Copilot App restarted (PID: $($cpProc.Id))."
    Write-Host ''
    Write-Host 'Next steps:' -ForegroundColor Yellow
    Write-Host '  1. Send one message in Copilot App (Code tab) so the relay captures the upstream token.'
    Write-Host '  2. Run .\status.ps1 anytime to verify token readiness.'
} catch {
    foreach ($p in @($gwProc, $mitmProc)) {
        if ($p -and -not $p.HasExited) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}
