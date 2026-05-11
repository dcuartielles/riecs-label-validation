#Requires -Version 5.1
<#
.SYNOPSIS
    One-shot setup for the RIECS Label Validation workshop server on a new Windows PC.

.DESCRIPTION
    Installs all dependencies (Scoop, Git, GitHub CLI, Python, Node.js, NSSM, ngrok,
    Claude Code), clones the repository, creates a Python venv, writes .env, imports
    data, registers two Windows services (FastAPI server + ngrok tunnel), and registers
    a daily Task Scheduler job for the end-of-day export snapshot.

    Must be run from an elevated PowerShell session (Run as Administrator).

.PARAMETER InstallDir
    Where to clone the repository. Default: C:\riecs

.EXAMPLE
    .\setup.ps1
    .\setup.ps1 -InstallDir D:\workshop\riecs
#>

param(
    [string]$InstallDir = "C:\riecs"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Helpers ───────────────────────────────────────────────────────────────────

function Write-Step { param([string]$Msg)
    Write-Host "`n==> $Msg" -ForegroundColor Cyan }

function Write-OK { param([string]$Msg)
    Write-Host "    OK  $Msg" -ForegroundColor Green }

function Write-Warn { param([string]$Msg)
    Write-Host "    !!  $Msg" -ForegroundColor Yellow }

function Require-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = [Security.Principal.WindowsPrincipal]$id
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "ERROR: Run this script from an elevated (Administrator) PowerShell." -ForegroundColor Red
        exit 1
    }
}

function Scoop-Install { param([string]$Pkg)
    if (-not (Get-Command $Pkg -ErrorAction SilentlyContinue)) {
        Write-Host "    Installing $Pkg via Scoop..."
        scoop install $Pkg | Out-Null
    } else {
        Write-OK "$Pkg already present"
    }
}

# ── 0. Admin check ────────────────────────────────────────────────────────────
Require-Admin

# ── 1. Scoop ──────────────────────────────────────────────────────────────────
Write-Step "Scoop package manager"
if (-not (Get-Command scoop -ErrorAction SilentlyContinue)) {
    Write-Host "    Installing Scoop..."
    Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
    Invoke-RestMethod https://get.scoop.sh | Invoke-Expression
    # Reload PATH
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH","User")
}
Write-OK "Scoop ready"

# ── 2. Core packages ──────────────────────────────────────────────────────────
Write-Step "Core packages (git, gh, python, nodejs, nssm, ngrok)"
scoop bucket add extras 2>$null
foreach ($pkg in @("git", "gh", "python", "nodejs", "nssm", "ngrok")) {
    Scoop-Install $pkg
}
# Reload PATH after installs
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("PATH","User")

# ── 3. Claude Code (npm global) ───────────────────────────────────────────────
Write-Step "Claude Code CLI"
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "    Installing Claude Code..."
    npm install -g @anthropic-ai/claude-code | Out-Null
}
Write-OK "Claude Code ready"

# ── 4. GitHub auth + clone ────────────────────────────────────────────────────
Write-Step "GitHub authentication"
$ghCheck = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "    A browser window will open for GitHub login..."
    gh auth login --web
}
Write-OK "GitHub authenticated"

Write-Step "Cloning repository to $InstallDir"
if (Test-Path $InstallDir) {
    Write-Warn "$InstallDir already exists — skipping clone (will use as-is)"
} else {
    gh repo clone dcuartielles/riecs-label-validation $InstallDir
    Write-OK "Repository cloned"
}
Set-Location $InstallDir

# ── 5. Python virtual environment ────────────────────────────────────────────
Write-Step "Python virtual environment + dependencies"
$venvDir = Join-Path $InstallDir ".venv"
$pyExe   = Join-Path $venvDir "Scripts\python.exe"
$pipExe  = Join-Path $venvDir "Scripts\pip.exe"

if (-not (Test-Path $venvDir)) {
    python -m venv $venvDir
    Write-OK "venv created at $venvDir"
} else {
    Write-OK "venv already exists"
}
& $pipExe install --upgrade pip -q
& $pipExe install -r requirements.txt -q
Write-OK "Python dependencies installed"

# ── 6. .env config ────────────────────────────────────────────────────────────
Write-Step "Environment configuration (.env)"
$envFile = Join-Path $InstallDir ".env"

if (-not (Test-Path $envFile)) {
    # Generate a random 48-char secret key
    $chars     = (65..90) + (97..122) + (48..57)
    $secretKey = -join ($chars | Get-Random -Count 48 | ForEach-Object { [char]$_ })

    $adminEmail  = Read-Host "Admin e-mail address"
    $ngrokDomain = Read-Host "ngrok fixed domain  (e.g. your-name.ngrok-free.app)"
    $ngrokToken  = Read-Host "ngrok auth token"

    @"
SECRET_KEY=$secretKey
ADMIN_EMAIL=$adminEmail
NGROK_DOMAIN=$ngrokDomain
"@ | Set-Content $envFile -Encoding UTF8
    Write-OK ".env written"
} else {
    Write-Warn ".env already exists — reading existing values"
    $envLines    = Get-Content $envFile
    $adminEmail  = ($envLines | Where-Object { $_ -match "^ADMIN_EMAIL=" })  -replace "^ADMIN_EMAIL=",  ""
    $ngrokDomain = ($envLines | Where-Object { $_ -match "^NGROK_DOMAIN=" }) -replace "^NGROK_DOMAIN=", ""
    $ngrokToken  = Read-Host "ngrok auth token (needed for service registration)"
}

# ── 7. ngrok config ───────────────────────────────────────────────────────────
Write-Step "ngrok — fixed domain $ngrokDomain"
ngrok config add-authtoken $ngrokToken

# Append the tunnel stanza only if it isn't already there
$ngrokCfgDir = Join-Path $env:USERPROFILE ".config\ngrok"
New-Item -ItemType Directory -Force -Path $ngrokCfgDir | Out-Null
$ngrokCfgFile = Join-Path $ngrokCfgDir "ngrok.yml"

$tunnelBlock = @"

tunnels:
  riecs:
    proto: http
    addr: 8000
    domain: $ngrokDomain
"@

if (-not (Test-Path $ngrokCfgFile) -or -not (Select-String -Path $ngrokCfgFile -Pattern "domain: $ngrokDomain" -Quiet)) {
    Add-Content -Path $ngrokCfgFile -Value $tunnelBlock
    Write-OK "Tunnel stanza added to $ngrokCfgFile"
} else {
    Write-OK "Tunnel stanza already present"
}

# ── 8. Import data ────────────────────────────────────────────────────────────
Write-Step "Data import"
$inputDir = Join-Path $InstallDir "input_data"
if (Test-Path $inputDir) {
    & $pyExe scripts\import_data.py
    Write-OK "Data imported"
} else {
    Write-Warn "input_data\ not found. Copy your source spreadsheets there, then run:"
    Write-Warn "  $pyExe scripts\import_data.py"
}

# ── 9. Admin user ─────────────────────────────────────────────────────────────
Write-Step "Admin user ($adminEmail)"
& $pyExe scripts\create_admin.py $adminEmail
Write-OK "Admin account ready"

# ── 10. Windows service — FastAPI server (NSSM) ───────────────────────────────
Write-Step "Windows service: RIECSServer"
$uvicornExe = Join-Path $venvDir "Scripts\uvicorn.exe"

$svcExists = (sc.exe query RIECSServer 2>$null) -ne $null -and $LASTEXITCODE -eq 0
if (-not $svcExists) {
    nssm install RIECSServer $uvicornExe "app.main:app --host 0.0.0.0 --port 8000"
    nssm set    RIECSServer AppDirectory        $InstallDir
    nssm set    RIECSServer AppEnvironmentExtra "PYTHONPATH=$InstallDir"
    nssm set    RIECSServer Start               SERVICE_AUTO_START
    nssm set    RIECSServer AppStdout           "$InstallDir\server.log"
    nssm set    RIECSServer AppStderr           "$InstallDir\server.log"
    nssm set    RIECSServer AppRotateFiles      1
    nssm set    RIECSServer AppRotateBytes      10485760   # 10 MB log rotation
    Write-OK "RIECSServer service installed"
} else {
    Write-Warn "RIECSServer already registered — skipping (run 'nssm edit RIECSServer' to reconfigure)"
}

# ── 11. Windows service — ngrok tunnel (NSSM) ────────────────────────────────
Write-Step "Windows service: RIECSNgrok"
$ngrokExe = (Get-Command ngrok).Source

$ngrokSvcExists = (sc.exe query RIECSNgrok 2>$null) -ne $null -and $LASTEXITCODE -eq 0
if (-not $ngrokSvcExists) {
    nssm install RIECSNgrok $ngrokExe "start riecs"
    nssm set    RIECSNgrok Start    SERVICE_AUTO_START
    nssm set    RIECSNgrok AppStdout "$InstallDir\ngrok.log"
    nssm set    RIECSNgrok AppStderr "$InstallDir\ngrok.log"
    nssm set    RIECSNgrok AppRotateFiles 1
    nssm set    RIECSNgrok AppRotateBytes 5242880   # 5 MB
    Write-OK "RIECSNgrok service installed"
} else {
    Write-Warn "RIECSNgrok already registered — skipping"
}

# ── 12. Task Scheduler — daily export at 23:50 ────────────────────────────────
Write-Step "Task Scheduler: daily export"
$taskName = "RIECSDailyExport"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $existing) {
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
} else {
    Write-Warn "Task '$taskName' already exists — skipping"
}

# ── 13. Start services ────────────────────────────────────────────────────────
Write-Step "Starting services"
nssm start RIECSServer 2>$null
nssm start RIECSNgrok  2>$null
Start-Sleep -Seconds 3

$serverRunning = (nssm status RIECSServer) -match "SERVICE_RUNNING"
$ngrokRunning  = (nssm status RIECSNgrok)  -match "SERVICE_RUNNING"
if ($serverRunning) { Write-OK "RIECSServer is running" } else { Write-Warn "RIECSServer did not start — check $InstallDir\server.log" }
if ($ngrokRunning)  { Write-OK "RIECSNgrok  is running" } else { Write-Warn "RIECSNgrok did not start  — check $InstallDir\ngrok.log" }

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║  Setup complete                                      ║" -ForegroundColor Green
Write-Host "╠══════════════════════════════════════════════════════╣" -ForegroundColor Green
Write-Host "║  Local :  http://localhost:8000                      ║" -ForegroundColor Green
Write-Host "║  Tunnel:  https://$($ngrokDomain.PadRight(38))║" -ForegroundColor Green
Write-Host "║  Admin :  $($adminEmail.PadRight(42))║" -ForegroundColor Green
Write-Host "║  Logs  :  $InstallDir\server.log" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "Service management:"
Write-Host "  nssm start|stop|restart RIECSServer"
Write-Host "  nssm start|stop|restart RIECSNgrok"
Write-Host "  Get-ScheduledTask RIECSDailyExport | Start-ScheduledTask  # run export now"
Write-Host ""
Write-Host "To update the app after a git pull:"
Write-Host "  nssm stop RIECSServer"
Write-Host "  git -C $InstallDir pull"
Write-Host "  nssm start RIECSServer"
