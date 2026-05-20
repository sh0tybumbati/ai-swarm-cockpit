# ══════════════════════════════════════════════════════════════════
#  PANOPTIC AI SWARM COCKPIT — Unified Launcher (Windows)
#  Starts: Ollama check → pulls models → FastFlowLM check → cockpit
# ══════════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$Logs    = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

# ── Recommended models ─────────────────────────────────────────────
$GpuModels    = @("qwen2.5-coder:7b", "deepseek-coder:6.7b", "mistral:7b")
$FastflowHost = if ($env:FASTFLOW_HOST)  { $env:FASTFLOW_HOST  } else { "http://localhost:8080" }
$FastflowModel= if ($env:FASTFLOW_MODEL) { $env:FASTFLOW_MODEL } else { "fastflow-lm" }

function Write-Ok   { param($m) Write-Host "  [OK]   $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host "  [ERR]  $m" -ForegroundColor Red }
function Write-Info { param($m) Write-Host "  [···]  $m" -ForegroundColor DarkGray }
function Write-Step { param($m) Write-Host "`n  > $m" -ForegroundColor Cyan }

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║   𒀭  PANOPTIC AI SWARM COCKPIT                  ║" -ForegroundColor Cyan
Write-Host "  ║   AN · ENLIL · ENKI · ENZU                       ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ── 1. Python ──────────────────────────────────────────────────────
Write-Step "Checking Python"
try {
    $pyver = & python --version 2>&1
    Write-Ok $pyver
} catch {
    Write-Err "Python not found. Install from https://python.org"
    Read-Host "Press Enter to exit"; exit 1
}

# ── 2. Python deps ─────────────────────────────────────────────────
Write-Step "Installing Python dependencies"
Set-Location $Backend
& python -m pip install -r requirements.txt -q
Write-Ok "Dependencies ready"

# ── 3. Ollama ──────────────────────────────────────────────────────
Write-Step "Checking Ollama"
$ollamaOk = $false
try {
    $null = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 3
    Write-Ok "Ollama running"
    $ollamaOk = $true
} catch {
    # On Windows, Ollama is a system service — try starting it
    $ollamaExe = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollamaExe) {
        Write-Info "Starting Ollama..."
        Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Logs "ollama.log") -PassThru | Out-Null
        Start-Sleep 4
        try {
            $null = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5
            Write-Ok "Ollama started"
            $ollamaOk = $true
        } catch {
            Write-Warn "Ollama slow to start — GPU agents may fail. Check $Logs\ollama.log"
        }
    } else {
        Write-Warn "Ollama not found. Install from https://ollama.com"
        Write-Warn "Skipping model management — GPU agents may fail"
    }
}

# ── 4. Pull missing models ─────────────────────────────────────────
if ($ollamaOk) {
    Write-Step "Checking GPU models"
    try {
        $installed = & ollama list 2>$null | Select-Object -Skip 1 |
                     ForEach-Object { ($_ -split '\s+')[0] }
        foreach ($model in $GpuModels) {
            $base = $model.Split(':')[0]
            $found = $installed | Where-Object { $_ -like "$base*" }
            if ($found) {
                Write-Ok $model
            } else {
                Write-Warn "$model not found — pulling..."
                & ollama pull $model
                Write-Ok "$model pulled"
            }
        }
    } catch {
        Write-Warn "Could not check model list: $_"
    }
}

# ── 5. FastFlowLM / NPU ────────────────────────────────────────────
Write-Step "Checking NPU server (FastFlowLM)"
try {
    $null = Invoke-RestMethod -Uri "$FastflowHost/v1/models" -TimeoutSec 3
    Write-Ok "FastFlowLM online at $FastflowHost"
    $env:FASTFLOW_HOST  = $FastflowHost
    $env:FASTFLOW_MODEL = $FastflowModel
} catch {
    Write-Warn "FastFlowLM not detected at $FastflowHost"
    Write-Warn "ENZU will default to GPU. Toggle NPU in ENZU's config modal once server is running."
}

# ── 6. Start cockpit ────────────────────────────────────────────────
Write-Step "Starting Cockpit"
Write-Host ""
Write-Host "  --> Open browser:  http://localhost:8000" -ForegroundColor Green
Write-Host "  --> Logs:          $Logs\" -ForegroundColor DarkGray
Write-Host ""

& python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload |
    Tee-Object -FilePath (Join-Path $Logs "cockpit.log")
