#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  PANOPTIC AI SWARM COCKPIT — Unified Launcher (Linux / CachyOS)
#  Starts: Ollama → pulls models → FastFlowLM check → cockpit
# ══════════════════════════════════════════════════════════════════
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$SCRIPT_DIR/backend"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

# ── Recommended models (edit to match your setup) ──────────────────
GPU_MODELS=("qwen2.5-coder:7b" "deepseek-coder:6.7b" "mistral:7b")
# qwen2.5-coder:14b if you have the VRAM for it

# ── NPU config ─────────────────────────────────────────────────────
FASTFLOW_HOST="${FASTFLOW_HOST:-http://localhost:8080}"
FASTFLOW_MODEL="${FASTFLOW_MODEL:-fastflow-lm}"

# ── Colors ─────────────────────────────────────────────────────────
CY='\033[0;36m'; GN='\033[0;32m'; YL='\033[0;33m'
RD='\033[0;31m'; DM='\033[0;90m'; NC='\033[0m'

banner() {
  echo -e "${CY}"
  echo "  ╔══════════════════════════════════════════════════╗"
  echo "  ║   𒀭  PANOPTIC AI SWARM COCKPIT                  ║"
  echo "  ║   AN · ENLIL · ENKI · ENZU                       ║"
  echo "  ╚══════════════════════════════════════════════════╝${NC}"
  echo ""
}

ok()   { echo -e "  ${GN}[OK]${NC}   $1"; }
warn() { echo -e "  ${YL}[WARN]${NC} $1"; }
err()  { echo -e "  ${RD}[ERR]${NC}  $1"; }
info() { echo -e "  ${DM}[···]${NC}  $1"; }
step() { echo -e "\n  ${CY}▸ $1${NC}"; }

banner

# ── 1. Python check ────────────────────────────────────────────────
step "Checking Python"
if ! command -v python3 &>/dev/null; then
  err "Python 3 not found. Install via: sudo pacman -S python"
  exit 1
fi
PY_VER=$(python3 --version 2>&1)
ok "$PY_VER"

# ── 2. Install Python deps ─────────────────────────────────────────
step "Installing Python dependencies"
cd "$BACKEND"
pip install -r requirements.txt -q && ok "Dependencies ready"

# ── 3. Ollama ──────────────────────────────────────────────────────
step "Checking Ollama"
if ! command -v ollama &>/dev/null; then
  warn "Ollama not found. Install: curl -fsSL https://ollama.com/install.sh | sh"
  warn "Skipping model management — GPU agents may fail"
else
  # Start Ollama if not already running
  if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    info "Starting Ollama in background..."
    ollama serve >"$LOG_DIR/ollama.log" 2>&1 &
    OLLAMA_PID=$!
    echo $OLLAMA_PID > "$LOG_DIR/ollama.pid"

    # Wait up to 15s for Ollama to be ready
    for i in $(seq 1 15); do
      if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        ok "Ollama started (PID $OLLAMA_PID)"
        break
      fi
      sleep 1
      if [ $i -eq 15 ]; then
        warn "Ollama slow to start — check $LOG_DIR/ollama.log"
      fi
    done
  else
    ok "Ollama already running"
  fi

  # ── 4. Pull missing GPU models ───────────────────────────────────
  step "Checking GPU models"
  INSTALLED=$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')
  for MODEL in "${GPU_MODELS[@]}"; do
    BASE="${MODEL%%:*}"
    if echo "$INSTALLED" | grep -q "^${BASE}"; then
      ok "$MODEL"
    else
      warn "$MODEL not found — pulling now (this may take a while)..."
      if ollama pull "$MODEL" 2>&1 | tail -1; then
        ok "$MODEL pulled"
      else
        warn "Failed to pull $MODEL — agent may fall back to another model"
      fi
    fi
  done
fi

# ── 5. FastFlowLM / NPU check ──────────────────────────────────────
step "Checking NPU server (FastFlowLM)"
if curl -sf "${FASTFLOW_HOST}/v1/models" >/dev/null 2>&1; then
  ok "FastFlowLM online at $FASTFLOW_HOST"
  export FASTFLOW_HOST FASTFLOW_MODEL
else
  warn "FastFlowLM not detected at $FASTFLOW_HOST"
  warn "ENZU will default to GPU mode"
  warn "To use NPU: start FastFlowLM, then toggle in ENZU's config modal"
fi

# ── 6. Start cockpit ────────────────────────────────────────────────
step "Starting Cockpit"
echo ""
echo -e "  ${GN}→ Open browser:  http://localhost:8000${NC}"
echo -e "  ${DM}→ Logs:          $LOG_DIR/${NC}"
echo ""

cleanup() {
  echo ""
  info "Shutting down..."
  if [ -f "$LOG_DIR/ollama.pid" ]; then
    OPID=$(cat "$LOG_DIR/ollama.pid")
    kill "$OPID" 2>/dev/null && info "Ollama stopped (PID $OPID)"
    rm -f "$LOG_DIR/ollama.pid"
  fi
}
trap cleanup EXIT INT TERM

exec uvicorn main:app --host 0.0.0.0 --port 8000 --reload \
  2>&1 | tee "$LOG_DIR/cockpit.log"
