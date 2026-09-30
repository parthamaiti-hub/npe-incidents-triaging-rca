<#
.SYNOPSIS
    Stops the app processes started by scripts\start.ps1 (API, worker,
    poller, rca_worker, sweeper) and, by default, the docker-compose
    services.

.DESCRIPTION
    Data in the mongodata/chromadata volumes is preserved unless
    -RemoveVolumes is passed -- that runs `docker compose down -v`, which
    deletes both. (Chroma is derived data; after a wipe, reload the catalogs
    and run `uv run python -m scripts.rebuild_vector_index`.)

.EXAMPLE
    .\scripts\stop.ps1
.EXAMPLE
    .\scripts\stop.ps1 -KeepDockerRunning   # stop app processes only
.EXAMPLE
    .\scripts\stop.ps1 -RemoveVolumes       # also wipe the mongo + chroma volumes
#>
param(
    [switch]$KeepDockerRunning,
    [switch]$RemoveVolumes
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$pidDir = Join-Path $root ".run"

foreach ($name in @("api", "worker", "poller", "rca_worker", "sweeper")) {
    $pidFile = Join-Path $pidDir "$name.pid"
    if (Test-Path $pidFile) {
        $procId = Get-Content $pidFile
        if ($procId -and (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
            # `uv run ...` spawns a child python.exe (and that child may spawn
            # its own child via uv's managed interpreter) -- Stop-Process on
            # just the captured PID kills uv.exe and orphans the real
            # uvicorn/worker process underneath it. /T kills the whole tree.
            Write-Host "Stopping $name (PID $procId, and its process tree)"
            taskkill /T /F /PID $procId 2>$null | Out-Null
        }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    } else {
        Write-Host "$name not running (no PID file)"
    }
}

if (-not $KeepDockerRunning) {
    if ($RemoveVolumes) {
        Write-Host "==> docker compose down -v (removes data volumes)"
        docker compose down -v
    } else {
        Write-Host "==> docker compose stop (data volumes preserved)"
        docker compose stop
    }
}

Write-Host "Stopped."
