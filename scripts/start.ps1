<#
.SYNOPSIS
    Starts the full NPE Incident Triage stack: docker-compose services
    (postgres/valkey/rabbitmq/opa), then the FastAPI API, classification
    worker, Teams/Jira poller, RCA worker, and pending-incident
    sweeper as local uv-managed background processes.

.DESCRIPTION
    Mirrors the manual steps in README.md's "Running pieces manually"
    section. Safe to re-run -- `docker compose up -d` is idempotent, and each
    app process is skipped if its PID file shows it's already running. PIDs
    and logs are written under .run\ (gitignored).

    rca_worker is harmless to run even with RCA_SYNTHESIS_LLM_ENABLED off --
    it just idles, blocked on an empty Redis Stream, until that flag is ever
    flipped on. Started by default so it's already running the moment
    someone does flip it, not a separate manual step to remember.

.EXAMPLE
    .\scripts\start.ps1
.EXAMPLE
    .\scripts\start.ps1 -NoWorker -NoPoller   # just the API
.EXAMPLE
    .\scripts\start.ps1 -SkipCatalogLoad       # catalogs already loaded
#>
param(
    [switch]$SkipCatalogLoad,
    [switch]$NoWorker,
    [switch]$NoPoller,
    [switch]$NoRcaWorker,
    [switch]$NoSweeper
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$pidDir = Join-Path $root ".run"
$logDir = Join-Path $pidDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Start-BackgroundProcess {
    param([string]$Name, [string[]]$ArgumentList)

    $pidFile = Join-Path $pidDir "$Name.pid"
    if (Test-Path $pidFile) {
        $existingPid = Get-Content $pidFile
        if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
            Write-Host "$Name already running (PID $existingPid), skipping."
            return
        }
    }

    $logFile = Join-Path $logDir "$Name.log"
    $errFile = Join-Path $logDir "$Name.err.log"
    $proc = Start-Process -FilePath "uv" -ArgumentList $ArgumentList -WorkingDirectory $root `
        -RedirectStandardOutput $logFile -RedirectStandardError $errFile -NoNewWindow -PassThru
    Set-Content -Path $pidFile -Value $proc.Id
    Write-Host "Started $Name (PID $($proc.Id)). Logs: $logFile"
}

Write-Host "==> docker compose up -d"
docker compose up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

Write-Host "==> waiting for postgres to be healthy"
$pgContainer = docker compose ps -q postgres
$deadline = (Get-Date).AddSeconds(60)
while ($true) {
    $status = docker inspect --format='{{.State.Health.Status}}' $pgContainer 2>$null
    if ($status -eq "healthy") { break }
    if ((Get-Date) -gt $deadline) { throw "postgres did not become healthy within 60s -- check docker compose logs postgres" }
    Start-Sleep -Seconds 2
}

if (-not $SkipCatalogLoad) {
    Write-Host "==> loading catalogs"
    uv run python -m dataloadscripts.load_catalog --file dataloadscripts/source_systems.yaml
    uv run python -m dataloadscripts.load_catalog --file dataloadscripts/npe_real_source_systems.yaml
    uv run python -m dataloadscripts.load_function_registry
    uv run python -m dataloadscripts.seed_functional_dummy_versions
    uv run python -m dataloadscripts.seed_rca_pattern_types
}

Write-Host "==> starting app processes"
Start-BackgroundProcess -Name "api" -ArgumentList @("run", "python", "-m", "scripts.run_dev_server")
if (-not $NoWorker) {
    Start-BackgroundProcess -Name "worker" -ArgumentList @("run", "python", "-m", "app.worker")
}
if (-not $NoPoller) {
    Start-BackgroundProcess -Name "poller" -ArgumentList @("run", "python", "-m", "app.poller")
}
if (-not $NoRcaWorker) {
    Start-BackgroundProcess -Name "rca_worker" -ArgumentList @("run", "python", "-m", "app.rca_worker")
}
if (-not $NoSweeper) {
    Start-BackgroundProcess -Name "sweeper" -ArgumentList @("run", "python", "-m", "app.sweeper")
}

Write-Host ""
Write-Host "==> waiting for API health check"
$healthy = $false
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -le $deadline) {
    try {
        $health = Invoke-RestMethod -Uri "http://localhost:8421/health" -TimeoutSec 3
        Write-Host ($health | ConvertTo-Json -Depth 5)
        $healthy = ($health.status -eq "ok")
        break
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $healthy) {
    Write-Warning "API not healthy yet at http://localhost:8421/health -- check .run\logs\api.log / api.err.log"
}

Write-Host ""
Write-Host "Stack is up. API docs: http://localhost:8421/docs"
Write-Host "Stop with: .\scripts\stop.ps1"
