#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$SCRIPT_DIR/backend"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   PANOPTIC AI SWARM COCKPIT                  ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Verify Python 3
if ! command -v python3 &>/dev/null; then
  echo "[ERROR] Python 3 is required but not found."
  exit 1
fi

# Warn if Ollama isn't reachable
if ! curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
  echo "[WARN]  Ollama not detected at localhost:11434"
  echo "        To start Ollama: ollama serve"
  echo "        Recommended models to pull:"
  echo "          ollama pull qwen2.5-coder:7b      (Agent 1 fallback)"
  echo "          ollama pull deepseek-coder:6.7b   (Agent 2)"
  echo "          ollama pull mistral:7b             (Agent 4)"
  echo ""
fi

cd "$BACKEND"

echo "[INFO]  Installing / verifying Python dependencies..."
pip install -r requirements.txt -q

echo "[INFO]  Starting backend at http://localhost:8000"
echo "[INFO]  Open your browser to: http://localhost:8000"
echo ""

exec uvicorn main:app --host 0.0.0.0 --port 8000 --reload
