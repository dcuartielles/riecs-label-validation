#Requires -Version 5.1
<#
.SYNOPSIS
    Step 1 of 2: user-level setup for the RIECS Label Validation server.
    Run as a normal user (NOT as Administrator).

.DESCRIPTION
    Installs Scoop, Git, GitHub CLI, Python, Node.js, NSSM, ngrok, and
    Claude Code. Clones the repository, creates a Python venv, writes .env,
    configures the ngrok fixed domain, and imports data.

    After this script completes, run setup_services.ps1 as Administrator
    to register the Windows services and the daily export Task Scheduler job.

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

# -- Helpers ------------------------------------------------------------------

function Write-Step { param([string]$Msg)
    Write-Host "`n==> $Msg" -ForegroundColor Cyan }

function Write-OK { param([string]$Msg)
    Write-Host "    OK  $Msg" -ForegroundColor Green }

function Write-Warn { param([string]$Msg)
    Write-Host "    !!  $Msg" -ForegroundColor Yellow }

function Scoop-Install { param([string]$Pkg)
    # Use scoop list, not Get-Command: Windows 11 ships python/node Store stubs
    # that satisfy Get-Command but are not real installations.
    $installed = scoop list $Pkg 2>$null | Select-String "^$Pkg\s"
    if (-not $installed) {
        Write-Host "    Installing $Pkg via Scoop..."
        scoop install $Pkg | Out-Null
    } else {
        Write-OK "$Pkg already present (Scoop)"
    }
}

# Refuse to run as Administrator - Scoop requires a normal user session
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$p  = [Security.Principal.WindowsPrincipal]$id
if ($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: Run this script as a normal user, NOT as Administrator." -ForegroundColor Red
    Write-Host "       Scoop refuses to install under an elevated session." -ForegroundColor Red
    Write-Host "       Open a regular (non-admin) PowerShell and try again." -ForegroundColor Red
    exit 1
}

# -- 1. Scoop -----------------------------------------------------------------
Write-Step "Scoop package manager"
if (-not (Get-Command scoop -ErrorAction SilentlyContinue)) {
    Write-Host "    Installing Scoop..."
    Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
    Invoke-RestMethod https://get.scoop.sh | Invoke-Expression
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH","User")
}
Write-OK "Scoop ready"

# -- 2. Core packages ---------------------------------------------------------
Write-Step "Core packages (git, gh, python, nodejs, nssm, ngrok)"
scoop bucket add extras 2>$null
foreach ($pkg in @("git", "gh", "python", "nodejs", "nssm", "ngrok")) {
    Scoop-Install $pkg
}
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("PATH","User")

# -- 3. Claude Code -----------------------------------------------------------
Write-Step "Claude Code CLI"
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "    Installing Claude Code..."
    npm install -g @anthropic-ai/claude-code | Out-Null
}
Write-OK "Claude Code ready"

# -- 4. GitHub auth + clone ---------------------------------------------------
Write-Step "GitHub authentication"
$ghCheck = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "    A browser window will open for GitHub login..."
    gh auth login --web
}
Write-OK "GitHub authenticated"

Write-Step "Cloning repository to $InstallDir"
if (Test-Path $InstallDir) {
    Write-Warn "$InstallDir already exists - skipping clone (will use as-is)"
} else {
    gh repo clone dcuartielles/riecs-label-validation $InstallDir
    Write-OK "Repository cloned"
}
Set-Location $InstallDir

# -- 5. Python virtual environment + dependencies -----------------------------
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

# -- 6. .env config -----------------------------------------------------------
Write-Step "Environment configuration (.env)"
$envFile = Join-Path $InstallDir ".env"

if (-not (Test-Path $envFile)) {
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
    Write-Warn ".env already exists - reading existing values"
    $envLines    = Get-Content $envFile
    $adminEmail  = ($envLines | Where-Object { $_ -match "^ADMIN_EMAIL=" })  -replace "^ADMIN_EMAIL=",  ""
    $ngrokDomain = ($envLines | Where-Object { $_ -match "^NGROK_DOMAIN=" }) -replace "^NGROK_DOMAIN=", ""
    $ngrokToken  = Read-Host "ngrok auth token (needed for ngrok config)"
}

# -- 7. ngrok fixed-domain config ---------------------------------------------
Write-Step "ngrok - fixed domain $ngrokDomain"
ngrok config add-authtoken $ngrokToken

$ngrokCfgDir  = Join-Path $env:USERPROFILE ".config\ngrok"
New-Item -ItemType Directory -Force -Path $ngrokCfgDir | Out-Null
$ngrokCfgFile = Join-Path $ngrokCfgDir "ngrok.yml"

$tunnelBlock = @"

tunnels:
  riecs:
    proto: http
    addr: 8000
    domain: $ngrokDomain
"@

if (-not (Test-Path $ngrokCfgFile) -or
    -not (Select-String -Path $ngrokCfgFile -Pattern "domain: $ngrokDomain" -Quiet)) {
    Add-Content -Path $ngrokCfgFile -Value $tunnelBlock
    Write-OK "Tunnel stanza added to $ngrokCfgFile"
} else {
    Write-OK "Tunnel stanza already present"
}

# -- 8. Import data -----------------------------------------------------------
Write-Step "Data import"
$inputDir = Join-Path $InstallDir "input_data"
if (Test-Path $inputDir) {
    & $pyExe scripts\import_data.py
    Write-OK "Data imported"
} else {
    Write-Warn "input_data\ not found. Copy your source spreadsheets there, then run:"
    Write-Warn "  $pyExe scripts\import_data.py"
}

# -- 9. Admin user ------------------------------------------------------------
Write-Step "Admin user ($adminEmail)"
& $pyExe scripts\create_admin.py $adminEmail
Write-OK "Admin account ready"

# -- Done - prompt for next step ----------------------------------------------
Write-Host ""
Write-Host "+----------------------------------------------------------+" -ForegroundColor Green
Write-Host "|  User-level setup complete.                              |" -ForegroundColor Green
Write-Host "+----------------------------------------------------------+" -ForegroundColor Green
Write-Host ""
Write-Host "Next: register Windows services (needs Administrator)." -ForegroundColor Cyan
Write-Host "  1. Open a NEW PowerShell as Administrator" -ForegroundColor Cyan
Write-Host "  2. Run:" -ForegroundColor Cyan
Write-Host "       cd $InstallDir" -ForegroundColor White
Write-Host "       .\setup_services.ps1" -ForegroundColor White
