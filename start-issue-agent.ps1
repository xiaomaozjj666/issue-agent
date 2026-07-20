param(
    [switch]$NoBrowser,
    [switch]$CheckOnly,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envFile = Join-Path $projectRoot ".env"
$envExample = Join-Path $projectRoot ".env.example"

# 候选端口列表：优先 8000，被其他应用占用时自动降级到 9123/9124/9125
$CandidatePorts = @(8000, 9123, 9124, 9125)
$script:appPort = $null
$script:appUrl = $null
$script:healthUrl = $null

function Test-PortListening {
    param([int]$Port)
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @("127.0.0.1", "0.0.0.0", "::") } |
        Select-Object -First 1
    return $null -ne $listener
}

function Get-PortListenerProcess {
    param([int]$Port)
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @("127.0.0.1", "0.0.0.0", "::") } |
        Select-Object -First 1
    if ($null -eq $listener) { return $null }
    return $listener.OwningProcess
}

function Test-IsIssueAgentOnPort {
    param([int]$Port)
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        return $null -ne $response -and $response.status -eq "ok" -and $response.app -eq "issue-agent"
    }
    catch {
        return $false
    }
}

function Select-AvailablePort {
    foreach ($port in $CandidatePorts) {
        $listenerPid = Get-PortListenerProcess -Port $port
        if ($null -eq $listenerPid) {
            return $port
        }
        # 端口被占用，检查是否是本项目的旧进程
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction SilentlyContinue
        $commandLine = [string]$process.CommandLine
        $isProjectServer = $commandLine.IndexOf($projectRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $commandLine.IndexOf("uvicorn app.main:app", [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        if ($isProjectServer) {
            return $port
        }
        # 被其他应用占用，尝试下一个候选端口
    }
    throw "No available port in candidates: $($CandidatePorts -join ', '). All are occupied by other processes."
}

function Stop-LocalIssueAgent {
    param([int]$Port)
    $listenerPid = Get-PortListenerProcess -Port $Port
    if ($null -eq $listenerPid) { return }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid"
    $commandLine = [string]$process.CommandLine
    $isProjectServer = $commandLine.IndexOf($projectRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine.IndexOf("uvicorn app.main:app", [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not $isProjectServer) {
        return
    }

    Write-Host "Stopping stale Issue Agent process on port $Port..." -ForegroundColor Yellow
    Stop-Process -Id $listenerPid
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (-not (Test-PortListening -Port $Port)) {
            return
        }
        Start-Sleep -Milliseconds 150
    }
    throw "The previous Issue Agent process did not stop in time."
}

Set-Location $projectRoot

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python environment not found. Run the Local Setup commands in README.md first."
}

if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    if (Test-Path -LiteralPath $envExample -PathType Leaf) {
        Copy-Item -LiteralPath $envExample -Destination $envFile
        Write-Host "Created .env from .env.example." -ForegroundColor Yellow
        Write-Host "Add your OPENAI_API_KEY, save the file, then double-click the launcher again."
        if (-not $NoBrowser -and -not $CheckOnly) {
            Start-Process notepad.exe -ArgumentList $envFile
        }
        exit 2
    }
    throw "Missing .env and .env.example."
}

& $python -c "import uvicorn; from app.main import app"
if ($LASTEXITCODE -ne 0) {
    throw "Issue Agent dependencies could not be loaded."
}

$localBuildId = (& $python -c "from app.build import calculate_build_id; print(calculate_build_id())").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($localBuildId)) {
    throw "Issue Agent build identity could not be calculated."
}

# 选择端口：先找已有 issue-agent 进程；否则找空闲端口
$script:appPort = $null
foreach ($port in $CandidatePorts) {
    if (Test-IsIssueAgentOnPort -Port $port) {
        $script:appPort = $port
        break
    }
}
if ($null -eq $script:appPort) {
    $script:appPort = Select-AvailablePort
}
$script:appUrl = "http://127.0.0.1:$($script:appPort)/"
$script:healthUrl = "http://127.0.0.1:$($script:appPort)/health"

$health = $null
if (Test-IsIssueAgentOnPort -Port $script:appPort) {
    try {
        $health = Invoke-RestMethod -Uri $script:healthUrl -TimeoutSec 2
    } catch { $health = $null }
}

if ($null -ne $health -and $health.status -eq "ok") {
    $isCurrentBuild = $health.app -eq "issue-agent" -and $health.build_id -eq $localBuildId
    if ($CheckOnly) {
        if (-not $isCurrentBuild) {
            throw "An outdated Issue Agent process is running. Start again with -Restart."
        }
        Write-Host "Launcher check passed; the running service matches the current build." -ForegroundColor Green
        exit 0
    }
    if ($isCurrentBuild -and -not $Restart) {
        Write-Host "Issue Agent is already running at $($script:appUrl)" -ForegroundColor Green
        if (-not $NoBrowser) {
            Start-Process $script:appUrl
        }
        exit 0
    }
    Stop-LocalIssueAgent -Port $script:appPort
}

if ($CheckOnly) {
    Write-Host "Launcher check passed." -ForegroundColor Green
    exit 0
}

Write-Host "Starting Issue Agent on port $($script:appPort)..." -ForegroundColor Cyan
Write-Host "Open: $($script:appUrl)"
Write-Host "Keep this window open. Press Ctrl+C to stop the service." -ForegroundColor DarkGray

$browserJob = $null
if (-not $NoBrowser) {
    $browserJob = Start-Job -ScriptBlock {
        param($HealthUrl, $AppUrl)
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            try {
                $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
                if ($response.status -eq "ok" -and $response.app -eq "issue-agent") {
                    Start-Process $AppUrl
                    return
                }
            }
            catch {
                Start-Sleep -Milliseconds 250
            }
        }
    } -ArgumentList $script:healthUrl, $script:appUrl
}

try {
    & $python -m uvicorn app.main:app --host 127.0.0.1 --port $script:appPort
    exit $LASTEXITCODE
}
finally {
    if ($null -ne $browserJob) {
        Stop-Job -Job $browserJob -ErrorAction SilentlyContinue
        Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue
    }
}
