@echo off
:: Panoptic AI Swarm Cockpit — Windows batch launcher
:: Double-click to run, or call from cmd

cd /d "%~dp0backend"

echo.
echo  Panoptic AI Swarm Cockpit
echo  ──────────────────────────
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org
    pause
    exit /b 1
)

echo [INFO] Installing dependencies...
python -m pip install -r requirements.txt -q

echo [INFO] Starting at http://localhost:8000
echo [INFO] Open your browser to: http://localhost:8000
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
pause
