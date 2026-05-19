# Panoptic AI Swarm Cockpit — Windows launcher
# Run: Right-click → "Run with PowerShell"  OR  pwsh -File start.ps1

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"

Write-Host ""
Write-Host "╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   PANOPTIC AI SWARM COCKPIT                  ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# Check Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] Python not found. Install from https://python.org" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# Check Ollama (Windows runs it as a background service — no need to start manually)
try {
    $null = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 3
    Write-Host "[OK]    Ollama detected at localhost:11434" -ForegroundColor Green
} catch {
    Write-Host "[WARN]  Ollama not detected. Install from https://ollama.com" -ForegroundColor Yellow
    Write-Host "        Recommended models:" -ForegroundColor Yellow
    Write-Host "          ollama pull qwen2.5-coder:7b" -ForegroundColor DarkYellow
    Write-Host "          ollama pull deepseek-coder:6.7b" -ForegroundColor DarkYellow
    Write-Host "          ollama pull mistral:7b" -ForegroundColor DarkYellow
    Write-Host ""
}

Set-Location $Backend

Write-Host "[INFO]  Installing Python dependencies..." -ForegroundColor Cyan
python -m pip install -r requirements.txt -q

Write-Host "[INFO]  Starting backend at http://localhost:8000" -ForegroundColor Cyan
Write-Host "[INFO]  Open your browser to: http://localhost:8000" -ForegroundColor Green
Write-Host ""

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
