@echo off
:: Panoptic AI Swarm Cockpit — Windows batch launcher
:: For full features use start.ps1 (PowerShell) instead

cd /d "%~dp0backend"

echo.
echo  Panoptic AI Swarm Cockpit
echo  AN . ENLIL . ENKI . ENZU
echo  ──────────────────────────
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERR] Python not found. Install from https://python.org
    pause & exit /b 1
)

echo [···] Installing dependencies...
python -m pip install -r requirements.txt -q

echo [···] Checking Ollama...
curl -sf http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [WARN] Ollama not detected. Install from https://ollama.com
) else (
    echo [OK]  Ollama running
)

echo.
echo [OK]  Starting cockpit at http://localhost:8000
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
pause
