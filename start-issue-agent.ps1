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
$appUrl = "http://127.0.0.1:8000/"
$healthUrl = "http://127.0.0.1:8000/health"

function Get-IssueAgentHealth {
    try {
        return Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Stop-LocalIssueAgent {
    $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @("127.0.0.1", "0.0.0.0", "::") } |
        Select-Object -First 1
    if ($null -eq $listener) {
        return
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $commandLine = [string]$process.CommandLine
    $isProjectServer = $commandLine.IndexOf($projectRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine.IndexOf("uvicorn app.main:app", [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not $isProjectServer) {
        throw "Port 8000 is occupied by another process. Stop it manually before starting Issue Agent."
    }

    Write-Host "Stopping stale Issue Agent process..." -ForegroundColor Yellow
    Stop-Process -Id $listener.OwningProcess
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) {
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

$health = Get-IssueAgentHealth
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
        Write-Host "Issue Agent is already running at $appUrl" -ForegroundColor Green
        if (-not $NoBrowser) {
            Start-Process $appUrl
        }
        exit 0
    }
    Stop-LocalIssueAgent
}

if ($CheckOnly) {
    Write-Host "Launcher check passed." -ForegroundColor Green
    exit 0
}

Write-Host "Starting Issue Agent..." -ForegroundColor Cyan
Write-Host "Open: $appUrl"
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
    } -ArgumentList $healthUrl, $appUrl
}

try {
    & $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
    exit $LASTEXITCODE
}
finally {
    if ($null -ne $browserJob) {
        Stop-Job -Job $browserJob -ErrorAction SilentlyContinue
        Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue
    }
}
