#Requires -Version 5.1
<#
.SYNOPSIS
通过已有公钥和可信主机记录打开远端 VibeNovel Studio。
.DESCRIPTION
复制 config/deployment.example.json 为 config/deployment.local.json 后填写连接参数。
只建立本机回环 SSH 隧道，不安装服务、修改 SSH 配置或启动付费任务。
已存在的隧道仅在配置、进程身份、监听归属和 Studio 响应均吻合时复用。
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File tools/studio_remote.ps1
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File tools/studio_remote.ps1 -NoBrowser
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [Alias('ssh_host')][string]$SshHost,
    [Alias('ssh_port')][Nullable[int]]$SshPort,
    [Alias('ssh_user')][string]$SshUser,
    [Alias('local_port')][Nullable[int]]$LocalPort,
    [Alias('remote_port')][Nullable[int]]$RemotePort,
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-SettingValue($Settings, [string]$Name) {
    if ($Settings.PSObject.Properties.Name -notcontains $Name) {
        throw '部署配置缺少必填字段，请对照 deployment.example.json 检查。'
    }
    return $Settings.$Name
}

function Get-ValidatedPort($Value, [string]$Label) {
    if ($null -eq $Value -or $Value -is [bool] -or $Value -is [string]) {
        throw "$Label 必须是 1 到 65535 的整数。"
    }
    $number = 0
    if (-not [int]::TryParse([string]$Value, [ref]$number) -or $number -lt 1 -or $number -gt 65535) {
        throw "$Label 必须是 1 到 65535 的整数。"
    }
    return $number
}

function Get-ConfigFingerprint($Settings) {
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Settings | ConvertTo-Json -Compress))
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose() }
}

function Get-PortListeners([int]$Port) {
    # A failure to inspect ownership must not be mistaken for an unused port.
    return @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq $Port })
}

function Test-TunnelProcess($State, [string]$Fingerprint, [string]$SshExecutable, [string]$ExpectedArguments) {
    try {
        if ([string]$State.config_sha256 -ne $Fingerprint) { return $false }
        $tunnelProcessId = [int]$State.process_id
        if ($tunnelProcessId -le 0) { return $false }
        $process = Get-Process -Id $tunnelProcessId -ErrorAction Stop
        $details = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$tunnelProcessId" -ErrorAction Stop
        if ($null -eq $details -or [string]::IsNullOrWhiteSpace($details.ExecutablePath)) { return $false }
        if (-not [string]::Equals([IO.Path]::GetFullPath($details.ExecutablePath), $SshExecutable, [StringComparison]::OrdinalIgnoreCase)) { return $false }
        $started = $process.StartTime.ToUniversalTime().ToString('o')
        if ($started -ne [string]$State.started_utc) { return $false }
        $command = [string]$details.CommandLine
        # Start-Process may quote the executable; every argument below is a single
        # validated, whitespace-free token. Never execute the stored command line.
        if ($command -notmatch '^(?:"[^"]+"|\S+)\s+(.+)$') { return $false }
        return $Matches[1].Trim() -ceq $ExpectedArguments
    }
    catch { return $false }
}

function Test-Studio([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri ($Url + 'api/bootstrap') -UseBasicParsing -TimeoutSec 3 -MaximumRedirection 0 -ErrorAction Stop
        if ([int]$response.StatusCode -ne 200 -or [string]$response.Headers['Content-Type'] -notmatch '^application/json') { return $false }
        $bootstrap = $response.Content | ConvertFrom-Json -ErrorAction Stop
        if ($bootstrap.version -isnot [string] -or $bootstrap.version -notmatch '^3\.') { return $false }
        if ($bootstrap.csrf_token -isnot [string] -or $bootstrap.csrf_token -notmatch '^[A-Za-z0-9_-]{32,128}$') { return $false }
        if ($bootstrap.capabilities.default_provider -ne 'api') { return $false }
        if ($bootstrap.capabilities.tasks -notcontains 'draft' -or $bootstrap.capabilities.tasks -notcontains 'review') { return $false }
        $page = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 -MaximumRedirection 0 -ErrorAction Stop
        return ([int]$page.StatusCode -eq 200 -and [string]$page.Headers['Content-Type'] -match '^text/html' -and $page.Content -match '<title>VibeNovel Studio(?:\s|[<·])')
    }
    catch { return $false }
}

$workspacePath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$mutex = $null
$mutexHeld = $false
$createdProcess = $null
$stateFilePath = $null
$stateWritten = $false
try {
    if ($env:OS -ne 'Windows_NT') { throw '此启动器需要 Windows；其他系统请使用文档中的 SSH 隧道命令。' }
    if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $workspacePath 'config/deployment.local.json' }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw '未找到部署配置。请将 config/deployment.example.json 复制为 config/deployment.local.json，再填写服务器参数。'
    }
    $configFile = Get-Item -LiteralPath $ConfigPath -ErrorAction Stop
    if ($configFile.Length -gt 16384) { throw '部署配置超过 16 KiB，请检查是否误选了其他文件。' }
    try { $settings = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw '部署配置不是有效 JSON，请对照 deployment.example.json 检查。' }
    if ($null -eq $settings -or $settings -is [Array] -or $settings -isnot [pscustomobject]) { throw '部署配置必须是一个 JSON 对象。' }
    $fieldNames = @('schema_version', 'ssh_host', 'ssh_port', 'ssh_user', 'local_port', 'remote_port')
    if (@($settings.PSObject.Properties.Name | Where-Object { $fieldNames -notcontains $_ }).Count -gt 0) {
        throw '部署配置包含未知字段，请只保留示例中的六个字段。'
    }
    $schema = Get-SettingValue $settings 'schema_version'
    if ($schema -is [string] -or $schema -is [bool] -or $schema -ne 1) { throw '部署配置 schema_version 必须为数字 1。' }
    $resolvedHost = Get-SettingValue $settings 'ssh_host'
    $resolvedUser = Get-SettingValue $settings 'ssh_user'
    $resolvedSshPort = Get-SettingValue $settings 'ssh_port'
    $resolvedLocalPort = Get-SettingValue $settings 'local_port'
    $resolvedRemotePort = Get-SettingValue $settings 'remote_port'
    if ($PSBoundParameters.ContainsKey('SshHost')) { $resolvedHost = $SshHost }
    if ($PSBoundParameters.ContainsKey('SshUser')) { $resolvedUser = $SshUser }
    if ($PSBoundParameters.ContainsKey('SshPort')) { $resolvedSshPort = $SshPort }
    if ($PSBoundParameters.ContainsKey('LocalPort')) { $resolvedLocalPort = $LocalPort }
    if ($PSBoundParameters.ContainsKey('RemotePort')) { $resolvedRemotePort = $RemotePort }
    if ($resolvedHost -isnot [string] -or $resolvedHost.Length -gt 253 -or $resolvedHost -notmatch '^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$' -or $resolvedHost -match '\.\.|\.-|-\.') {
        throw 'ssh_host 只接受不含空格、用户或命令的 ASCII 域名 / IPv4 地址。'
    }
    if ($resolvedUser -isnot [string] -or $resolvedUser -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$') {
        throw 'ssh_user 只能包含字母、数字、下划线、点和短横线，且不能以短横线开头。'
    }
    if ($resolvedHost -eq 'HOST' -or $resolvedUser -eq 'USER') { throw '请先把部署配置中的 HOST、USER 占位符替换为实际连接参数。' }
    $sshPortValue = Get-ValidatedPort $resolvedSshPort 'ssh_port'
    $localPortValue = Get-ValidatedPort $resolvedLocalPort 'local_port'
    $remotePortValue = Get-ValidatedPort $resolvedRemotePort 'remote_port'
    $normalized = [ordered]@{ schema_version = 1; ssh_host = $resolvedHost.ToLowerInvariant(); ssh_port = $sshPortValue; ssh_user = $resolvedUser; local_port = $localPortValue; remote_port = $remotePortValue }
    $fingerprint = Get-ConfigFingerprint $normalized
    $sshCommand = Get-Command ssh.exe -CommandType Application -ErrorAction Stop
    $sshExecutable = [IO.Path]::GetFullPath($sshCommand.Source)
    # Ignore SSH config directives that could add other listeners or local commands.
    # Default identity files / agent and known_hosts remain available with -F NUL.
    $sshArguments = @('-F', 'NUL', '-N', '-L', "127.0.0.1:${localPortValue}:127.0.0.1:${remotePortValue}", '-p', [string]$sshPortValue,
        '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
        '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=no', '-o', 'GatewayPorts=no',
        '-o', 'PermitLocalCommand=no', '-o', 'ProxyCommand=none', '-o', 'ProxyJump=none',
        '-o', 'RemoteCommand=none', '-o', 'RequestTTY=no', "${resolvedUser}@${resolvedHost}")
    $argumentText = $sshArguments -join ' '
    $url = "http://127.0.0.1:${localPortValue}/"
    $stateDirectory = Join-Path $workspacePath '.runtime-build/studio-remote'
    [void][IO.Directory]::CreateDirectory($stateDirectory)
    $stateFilePath = Join-Path $stateDirectory "tunnel-${localPortValue}.json"
    $mutex = New-Object Threading.Mutex($false, "Local\VibeNovelStudioRemote-${localPortValue}")
    try { $mutexHeld = $mutex.WaitOne(1000) } catch [Threading.AbandonedMutexException] { $mutexHeld = $true }
    if (-not $mutexHeld) { throw '另一个启动器正在处理此端口，请稍后重试。' }
    $listeners = @(Get-PortListeners $localPortValue)
    if ($listeners.Count -gt 0) {
        $state = $null
        if (Test-Path -LiteralPath $stateFilePath -PathType Leaf) {
            try { $state = Get-Content -LiteralPath $stateFilePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop } catch { $state = $null }
        }
        $matches = $null -ne $state -and (Test-TunnelProcess $state $fingerprint $sshExecutable $argumentText)
        if ($matches) {
            $matches = @($listeners | Where-Object { $_.LocalAddress -ne '127.0.0.1' -or $_.OwningProcess -ne [int]$state.process_id }).Count -eq 0
        }
        if (-not $matches) { throw "本机端口 $localPortValue 已被占用，且无法确认为当前配置的 SSH 隧道。未结束任何已有进程；请核对占用者或修改 local_port。" }
        if (-not (Test-Studio $url)) { throw '已确认原 SSH 隧道，但远端 Studio 暂时没有返回有效页面。保留隧道，请检查服务器服务状态后重试；不要重复发送生成任务。' }
        Write-Host "已复用验证通过的 Studio 隧道：$url"
    }
    else {
        $runId = [Guid]::NewGuid().ToString('N')
        $errorLog = Join-Path $stateDirectory "ssh-${localPortValue}-${runId}.stderr.log"
        $outputLog = Join-Path $stateDirectory "ssh-${localPortValue}-${runId}.stdout.log"
        $createdProcess = Start-Process -FilePath $sshExecutable -ArgumentList $sshArguments -WindowStyle Hidden -PassThru -RedirectStandardError $errorLog -RedirectStandardOutput $outputLog
        $newState = [ordered]@{ schema_version = 1; config_sha256 = $fingerprint; process_id = $createdProcess.Id; started_utc = $createdProcess.StartTime.ToUniversalTime().ToString('o'); url = $url; stderr_log = $errorLog }
        $ready = $false
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ($timer.Elapsed.TotalSeconds -lt 30) {
            $createdProcess.Refresh()
            if ($createdProcess.HasExited) { throw "SSH 隧道启动失败。请检查公钥、可信主机记录和网络；诊断日志：$errorLog" }
            $liveListeners = @(Get-PortListeners $localPortValue)
            $ownedListeners = @($liveListeners | Where-Object { $_.LocalAddress -eq '127.0.0.1' -and $_.OwningProcess -eq $createdProcess.Id })
            if ($ownedListeners.Count -gt 0 -and $ownedListeners.Count -eq $liveListeners.Count -and (Test-Studio $url)) { $ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) { throw "隧道未在等待时间内连接到有效 Studio 页面。请检查远端服务；诊断日志：$errorLog" }
        if (-not (Test-TunnelProcess ([pscustomobject]$newState) $fingerprint $sshExecutable $argumentText)) { throw '新隧道的进程身份无法复核，未保留此次连接。' }
        $temporaryState = Join-Path $stateDirectory "state-${runId}.tmp"
        [IO.File]::WriteAllText($temporaryState, ($newState | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temporaryState -Destination $stateFilePath -Force
        $stateWritten = $true
        Write-Host "Studio 已连接：$url"
    }
    if (-not $NoBrowser) {
        try { Start-Process -FilePath $url | Out-Null }
        catch { Write-Warning "隧道已就绪，但无法自动打开浏览器，请手动访问 $url" }
    }
    Write-Host '关闭浏览器不会结束服务器任务；断线后重新运行此启动器即可。'
}
catch {
    if ($null -ne $createdProcess) {
        try {
            $createdProcess.Refresh()
            if (-not $createdProcess.HasExited) { Stop-Process -InputObject $createdProcess -ErrorAction SilentlyContinue }
            if ($stateWritten -and $null -ne $stateFilePath) { Remove-Item -LiteralPath $stateFilePath -Force -ErrorAction SilentlyContinue }
        } catch { }
    }
    Write-Error ("远端 Studio 启动失败：" + $_.Exception.Message) -ErrorAction Continue
    exit 1
}
finally {
    if ($mutexHeld) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
