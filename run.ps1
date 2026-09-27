<#
.SYNOPSIS
  One-command launcher for VOX.

.DESCRIPTION
  Creates the virtualenv on first run, installs dependencies, starts 9Router if
  it is not already listening, checks the configuration and the brain, then
  starts the agent and opens the operator console.

.EXAMPLE
  .\run.ps1              # normal start
  .\run.ps1 -Doctor      # configuration check only, then exit
  .\run.ps1 -NoBrowser   # do not open the console automatically
  .\run.ps1 -Orb         # also start the native always-on-top corner orb
  .\run.ps1 -Overlay     # ...the WebView2 variant instead, which cannot be transparent
#>
[CmdletBinding()]
param(
    [switch]$Doctor,
    [switch]$NoBrowser,
    [switch]$SkipInstall,
    [switch]$Orb,
    [switch]$Overlay
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Write-Ok($text)    { Write-Host "    $text" -ForegroundColor DarkGray }
function Write-Warn($text)  { Write-Host "    $text" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 1. venv ---
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Step "Creating the virtualenv"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is required but not installed. Install it: https://docs.astral.sh/uv/"
    }
    uv venv --python 3.11 | Out-Null
    $python = Join-Path $venv "Scripts\python.exe"
    $SkipInstall = $false
}

# ------------------------------------------------------------ 2. install ---
if (-not $SkipInstall) {
    Write-Step "Installing dependencies"
    uv pip install -e . --quiet 2>&1 | Out-Null
    uv pip install pytest pytest-asyncio --quiet 2>&1 | Out-Null
    Write-Ok "done"
}

# ------------------------------------------------------------- 3. .env ----
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Write-Step "Creating .env from .env.example"
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Warn "Edit $envFile and add your keys, then run this again."
    exit 1
}

# ---------------------------------------------------------- 4. 9router ----
function Test-Router {
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:20128/v1/models" -TimeoutSec 3 -UseBasicParsing
        return $true
    } catch { return $false }
}

if (Test-Router) {
    Write-Ok "9Router already listening on 127.0.0.1:20128"
} else {
    Write-Step "Starting 9Router on 127.0.0.1:20128"
    $router = Get-Command npx -ErrorAction SilentlyContinue
    if (-not $router) {
        Write-Warn "npx not found. Install 9Router with: npm install -g 9router"
    } else {
        # --host 127.0.0.1 matters: 9Router defaults to 0.0.0.0, which would
        # expose your model gateway (and its keys) to the whole network.
        Start-Process -FilePath "npx.cmd" `
            -ArgumentList "9router", "--no-browser", "--host", "127.0.0.1" `
            -WindowStyle Minimized | Out-Null
        $up = $false
        foreach ($i in 1..20) {
            Start-Sleep -Seconds 1
            if (Test-Router) { $up = $true; break }
        }
        if ($up) { Write-Ok "9Router is up" }
        else { Write-Warn "9Router did not start. Run 'npx 9router' in another window to see why." }
    }
}

# ----------------------------------------------------------- 5. doctor ----
Write-Step "Checking configuration"
$env:PYTHONPATH = Join-Path $root "src"
$doctorOutput = & $python -m voice_os doctor 2>&1
$doctorOutput | ForEach-Object { Write-Host $_ }
$doctorOk = $LASTEXITCODE -eq 0

if ($Doctor) {
    if (-not $doctorOk) { Write-Host "`nConfiguration is not complete yet." -ForegroundColor Red; exit 1 }
    Write-Host "`nConfiguration looks good." -ForegroundColor Green
    exit 0
}

if (-not $doctorOk) {
    # Do not just bail: the console is the only place that lists the missing
    # keys in a readable way, and it works without them. Serve it anyway.
    Write-Host "`nConfiguration is incomplete, so the agent itself will not start." -ForegroundColor Yellow
    Write-Host "Starting the console anyway so you can see what is missing...`n" -ForegroundColor Yellow
    if ($NoBrowser) { $env:VOICE_OS_NO_BROWSER = "1" }
    & $python -m voice_os client
    exit 1
}

# ------------------------------------------------------------ 6. agent ----
Write-Step "Starting the agent"
if ($NoBrowser) {
    $env:VOICE_OS_NO_BROWSER = "1"
    Write-Ok "Open http://127.0.0.1:8787 yourself when ready"
}

# The orb is a separate process so a crash in the always-on-top window cannot
# take the agent down with it. It waits for the control server, which the worker
# is about to bring up.
$orbProc = $null
$overlayProc = $null
if ($Orb) {
    Write-Step "Starting the native always-on-top orb"
    $orbProc = Start-Process -FilePath $python `
        -ArgumentList "-m", "voice_os", "orb" `
        -PassThru -WindowStyle Minimized
    Write-Ok "orb pid $($orbProc.Id), corner from OVERLAY_CORNER in .env"
}
if ($Overlay) {
    Write-Step "Starting the WebView2 overlay (cannot be transparent on Windows)"
    $overlayProc = Start-Process -FilePath $python `
        -ArgumentList "-m", "voice_os", "overlay" `
        -PassThru -WindowStyle Minimized
    Write-Ok "overlay pid $($overlayProc.Id)"
}

try {
    & $python -m voice_os worker
} finally {
    foreach ($proc in @($orbProc, $overlayProc)) {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "`nAgent stopped." -ForegroundColor Yellow
