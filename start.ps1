<#
    Covasant · WealthGate KYC — demo launcher.

    Checks the database is loaded (loading it on first run), then starts the
    web application.

        .\start.ps1           normal run
        .\start.ps1 -Reload   auto-reload on code changes
        .\start.ps1 -Load     force a full reload of the workbook
#>
[CmdletBinding()]
param(
    [switch]$Reload,
    [switch]$Load
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Get-EnvValue([string]$key, [string]$fallback) {
    if (-not (Test-Path '.env')) { return $fallback }
    $line = Select-String -Path '.env' -Pattern "^\s*$key\s*=" -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($null -eq $line) { return $fallback }
    return $line.Line.Split('=', 2)[1].Trim().Trim('"').Trim("'")
}

$port = Get-EnvValue 'APP_PORT' '8010'
$host_ = Get-EnvValue 'APP_HOST' '127.0.0.1'

Write-Host ''
Write-Host '  Covasant - WealthGate Onboarding & KYC' -ForegroundColor Blue
Write-Host ''

# --- dependencies ---------------------------------------------------------
python -c "import fastapi, uvicorn, psycopg2, openpyxl, multipart, rapidocr_onnxruntime, pymupdf" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '  installing Python dependencies...' -ForegroundColor Yellow
    python -m pip install -r backend/requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}

# --- database -------------------------------------------------------------
$needsLoad = $Load.IsPresent
if (-not $needsLoad) {
    python backend/etl/load_excel_to_pg.py --verify-only *>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  database not ready - loading the workbook...' -ForegroundColor Yellow
        $needsLoad = $true
    }
}
if ($needsLoad) {
    python backend/etl/load_excel_to_pg.py
    if ($LASTEXITCODE -ne 0) { throw 'data load failed' }
}

# --- port in use? ---------------------------------------------------------
$busy = Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "  port $port is already in use (PID $($busy[0].OwningProcess))." -ForegroundColor Red
    Write-Host "  Change APP_PORT in .env, or stop that process first." -ForegroundColor Red
    exit 1
}

# --- go -------------------------------------------------------------------
Write-Host ''
Write-Host "  starting on http://${host_}:${port}" -ForegroundColor Green
Write-Host "  API docs at  http://${host_}:${port}/docs"
Write-Host '  press Ctrl+C to stop'
Write-Host ''

$args_ = @('-m', 'uvicorn', 'backend.app:app', '--host', $host_, '--port', $port)
if ($Reload) { $args_ += '--reload' }
& python @args_
