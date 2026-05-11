#Requires -Version 5.1
<#
.SYNOPSIS
    Step 2 of 2: register Windows services and the daily export task.
    Must be run as Administrator.

.DESCRIPTION
    Registers two NSSM auto-start services (RIECSServer + RIECSNgrok)
    and a daily Task Scheduler job (RIECSDailyExport at 23:50).
    Run this after setup.ps1 has completed successfully.

.PARAMETER InstallDir
    Installation directory used in setup.ps1. Default: C:\riecs

.EXAMPLE
    .\setup_services.ps1
    .\setup_services.ps1 -InstallDir D:\workshop\riecs
#>

param(
    [string]$InstallDir = "C:\riecs"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# -- Helpers ------------------------------------------------------------------

function Write-Step { param([string]$Msg)
    Write-Host "`n==> $Msg" -ForegroundColor Cyan }

function Write-OK { param([string]$Msg)
    Write-Host "    OK  $Msg" -ForegroundColor Green }

function Write-Warn { param([string]$Msg)
    Write-Host "    !!  $Msg" -ForegroundColor Yellow }

# Must be Administrator
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$p  = [Security.Principal.WindowsPrincipal]$id
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: Run this script from an elevated (Administrator) PowerShell." -ForegroundColor Red
    exit 1
}

# Derive paths
$venvDir    = Join-Path $InstallDir ".venv"
$pyExe      = Join-Path $venvDir "Scripts\python.exe"
$uvicornExe = Join-Path $venvDir "Scripts\uvicorn.exe"

if (-not (Test-Path $uvicornExe)) {
    Write-Host "ERROR: $uvicornExe not found." -ForegroundColor Red
    Write-Host "       Run setup.ps1 first (as a normal user)." -ForegroundColor Red
    exit 1
}

# Read .env to get values for the summary
$envFile  = Join-Path $InstallDir ".env"
$envLines = Get-Content $envFile -ErrorAction SilentlyContinue
$adminEmail  = ($envLines | Where-Object { $_ -match "^ADMIN_EMAIL=" })  -replace "^ADMIN_EMAIL=",  ""
$ngrokDomain = ($envLines | Where-Object { $_ -match "^NGROK_DOMAIN=" }) -replace "^NGROK_DOMAIN=", ""

# -- 1. Windows service: FastAPI server (NSSM) --------------------------------
Write-Step "Windows service: RIECSServer"

sc.exe query RIECSServer 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Warn "RIECSServer already registered - skipping (run 'nssm edit RIECSServer' to reconfigure)"
} else {
    nssm install RIECSServer $uvicornExe "app.main:app --host 0.0.0.0 --port 8000"
    nssm set    RIECSServer AppDirectory        $InstallDir
    nssm set    RIECSServer AppEnvironmentExtra "PYTHONPATH=$InstallDir"
    nssm set    RIECSServer Start               SERVICE_AUTO_START
    nssm set    RIECSServer AppStdout           "$InstallDir\server.log"
    nssm set    RIECSServer AppStderr           "$InstallDir\server.log"
    nssm set    RIECSServer AppRotateFiles      1
    nssm set    RIECSServer AppRotateBytes      10485760
    Write-OK "RIECSServer service installed"
}

# -- 2. Windows service: ngrok tunnel (NSSM) ----------------------------------
Write-Step "Windows service: RIECSNgrok"
$ngrokExe = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
if (-not $ngrokExe) {
    Write-Warn "ngrok not found in PATH - skipping RIECSNgrok service"
} else {
    sc.exe query RIECSNgrok 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Warn "RIECSNgrok already registered - skipping"
    } else {
        nssm install RIECSNgrok $ngrokExe "start riecs"
        nssm set    RIECSNgrok Start         SERVICE_AUTO_START
        nssm set    RIECSNgrok AppStdout     "$InstallDir\ngrok.log"
        nssm set    RIECSNgrok AppStderr     "$InstallDir\ngrok.log"
        nssm set    RIECSNgrok AppRotateFiles 1
        nssm set    RIECSNgrok AppRotateBytes 5242880
        Write-OK "RIECSNgrok service installed"
    }
}

# -- 3. Task Scheduler: daily export at 23:50 ---------------------------------
Write-Step "Task Scheduler: RIECSDailyExport"
$taskName = "RIECSDailyExport"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Warn "Task '$taskName' already exists - skipping"
} else {
    $action   = New-ScheduledTaskAction `
                    -Execute $pyExe `
                    -Argument "scripts\daily_export.py" `
                    -WorkingDirectory $InstallDir
    $trigger  = New-ScheduledTaskTrigger -Daily -At "23:50"
    $settings = New-ScheduledTaskSettingsSet `
                    -StartWhenAvailable `
                    -RunOnlyIfNetworkAvailable:$false `
                    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action   $action `
        -Trigger  $trigger `
        -Settings $settings `
        -RunLevel Highest `
        -Force | Out-Null
    Write-OK "Task '$taskName' registered (runs daily at 23:50)"
}

# -- 4. Start services --------------------------------------------------------
Write-Step "Starting services"
nssm start RIECSServer 2>$null
nssm start RIECSNgrok  2>$null
Start-Sleep -Seconds 3

$serverRunning = (nssm status RIECSServer) -match "SERVICE_RUNNING"
$ngrokRunning  = (nssm status RIECSNgrok)  -match "SERVICE_RUNNING"
if ($serverRunning) { Write-OK "RIECSServer is running" } else { Write-Warn "RIECSServer did not start - check $InstallDir\server.log" }
if ($ngrokRunning)  { Write-OK "RIECSNgrok  is running" } else { Write-Warn "RIECSNgrok did not start  - check $InstallDir\ngrok.log" }

# -- Summary ------------------------------------------------------------------
Write-Host ""
Write-Host "+----------------------------------------------------------+" -ForegroundColor Green
Write-Host "|  Setup complete.                                         |" -ForegroundColor Green
Write-Host "+----------------------------------------------------------+" -ForegroundColor Green
Write-Host "|  Local :  http://localhost:8000"                           -ForegroundColor Green
Write-Host "|  Tunnel:  https://$ngrokDomain"                           -ForegroundColor Green
Write-Host "|  Admin :  $adminEmail"                                     -ForegroundColor Green
Write-Host "+----------------------------------------------------------+" -ForegroundColor Green
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  nssm stop|start|restart RIECSServer"
Write-Host "  nssm stop|start|restart RIECSNgrok"
Write-Host "  Get-ScheduledTask RIECSDailyExport | Start-ScheduledTask"
Write-Host ""
Write-Host "To update after a git pull:"
Write-Host "  nssm stop RIECSServer"
Write-Host "  git -C $InstallDir pull"
Write-Host "  nssm start RIECSServer"
