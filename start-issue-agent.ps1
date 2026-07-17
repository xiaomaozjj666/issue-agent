param(
    [switch]$NoBrowser,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envFile = Join-Path $projectRoot ".env"
$envExample = Join-Path $projectRoot ".env.example"
$appUrl = "http://127.0.0.1:8000/"
$healthUrl = "http://127.0.0.1:8000/health"

function Test-IssueAgentRunning {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        return $response.status -eq "ok"
    }
    catch {
        return $false
    }
}

Set-Location $projectRoot

if (Test-IssueAgentRunning) {
    Write-Host "Issue Agent is already running at $appUrl" -ForegroundColor Green
    if (-not $NoBrowser -and -not $CheckOnly) {
        Start-Process $appUrl
    }
    exit 0
}

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
                if ($response.status -eq "ok") {
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
