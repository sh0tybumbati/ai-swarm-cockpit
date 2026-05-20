"""
Panoptic AI Swarm Cockpit — FastAPI Backend
Run: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import json
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from agents import AGENTS, NPU_CONFIG, CLAUDE_CONFIG, CLAUDE_CLI_CONFIG
from orchestrator import (OUTPUT_BASE, SwarmOrchestrator,
                          list_output_files, load_context, save_context,
                          _safe_name)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger("cockpit")

app = FastAPI(title="Panoptic AI Swarm Cockpit", version="1.2.0")
orchestrator = SwarmOrchestrator()

FRONTEND = Path(__file__).parent.parent / "frontend"


# ── Frontend static files ──────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index = FRONTEND / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>Frontend not found.</h1>", status_code=404)
    return HTMLResponse(index.read_text(encoding="utf-8"))


@app.get("/style.css")
async def serve_css():
    return Response((FRONTEND / "style.css").read_text(), media_type="text/css")


@app.get("/app.js")
async def serve_js():
    return Response((FRONTEND / "app.js").read_text(), media_type="application/javascript")


# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws/{channel}")
async def ws_endpoint(websocket: WebSocket, channel: str):
    await orchestrator.connect(channel, websocket)
    logger.info(f"WS open: {channel}")
    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=25.0)
                if msg == "pong":
                    pass
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        logger.info(f"WS closed: {channel}")
    except Exception as exc:
        logger.warning(f"WS error on {channel}: {exc}")
    finally:
        await orchestrator.disconnect(channel, websocket)


# ── Swarm control ──────────────────────────────────────────────────────────────

@app.post("/broadcast")
async def broadcast(payload: dict):
    prompt      = (payload.get("prompt") or "").strip()
    project     = (payload.get("project") or "New Project").strip()
    max_iters   = int(payload.get("max_iterations") or 12)
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    task = asyncio.create_task(orchestrator.run_swarm_loop(prompt, project, max_iters))
    orchestrator._active_task = task
    return {"status": f"Swarm loop initiated: {prompt[:60]}"}


@app.post("/stop")
async def stop_loop():
    await orchestrator.stop()
    return {"status": "stopped"}


# ── Agent config & solo run ────────────────────────────────────────────────────

@app.get("/agents")
async def get_agents():
    return AGENTS


@app.post("/agents/{agent_id}/config")
async def update_agent(agent_id: str, config: dict):
    if agent_id not in AGENTS:
        raise HTTPException(404, "Agent not found")
    for key in ("model", "role"):
        if key in config:
            AGENTS[agent_id][key] = config[key]
    return {"status": "updated", "agent": AGENTS[agent_id]}


@app.post("/agents/{agent_id}/run")
async def run_single_agent(agent_id: str, payload: dict):
    if agent_id not in AGENTS:
        raise HTTPException(404, "Agent not found")
    prompt  = (payload.get("prompt") or "").strip()
    project = (payload.get("project") or "New Project").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    task = asyncio.create_task(orchestrator.run_single_agent(agent_id, prompt, project))
    orchestrator._active_task = task
    return {"status": f"{agent_id} solo run started"}


# ── Models ─────────────────────────────────────────────────────────────────────

@app.get("/models")
async def list_models():
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp   = await client.get("http://localhost:11434/api/tags")
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"models": models}
        except Exception as exc:
            return {"models": [], "error": str(exc)}


# ── Project context ────────────────────────────────────────────────────────────

@app.get("/context/{project}")
async def get_context(project: str):
    content = load_context(project)
    return {"project": project, "context": content, "exists": bool(content)}


@app.post("/context/{project}")
async def update_context_endpoint(project: str, payload: dict):
    content = payload.get("content", "")
    proj_dir = OUTPUT_BASE / _safe_name(project)
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "context.md").write_text(content, encoding="utf-8")
    return {"status": "saved"}


# ── Output files ───────────────────────────────────────────────────────────────

@app.get("/files/{project}")
async def get_project_files(project: str):
    return {"project": project, "files": list_output_files(project)}


@app.get("/files/{project}/{filename}")
async def download_file(project: str, filename: str):
    safe_file = Path(filename).name
    proj_dir  = OUTPUT_BASE / _safe_name(project)
    filepath  = proj_dir / safe_file
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(404, "File not found")
    media_types = {".html": "text/html", ".css": "text/css",
                   ".js": "application/javascript", ".ts": "application/javascript",
                   ".json": "application/json"}
    media = media_types.get(filepath.suffix, "text/plain")
    # inline display types render in the iframe; others trigger download
    inline = {".html", ".css", ".js", ".ts", ".json", ".txt", ".md"}
    if filepath.suffix in inline:
        return FileResponse(filepath, media_type=media)
    return FileResponse(filepath, media_type=media, filename=safe_file)


# ── NPU config & health ───────────────────────────────────────────────────────

@app.get("/npu/config")
async def get_npu_config():
    return {**NPU_CONFIG, "backend": AGENTS["agent4"].get("backend", "gpu")}


@app.post("/npu/config")
async def set_npu_config(payload: dict):
    if "host"  in payload: NPU_CONFIG["host"]  = payload["host"].rstrip("/")
    if "model" in payload: NPU_CONFIG["model"] = payload["model"]
    return {"status": "updated", "config": NPU_CONFIG}


@app.post("/npu/backend")
async def set_agent4_backend(payload: dict):
    backend = payload.get("backend", "gpu")
    if backend not in ("gpu", "npu"):
        return JSONResponse({"error": "backend must be 'gpu' or 'npu'"}, status_code=400)
    AGENTS["agent4"]["backend"] = backend
    return {"status": "updated", "agent4_backend": backend}


@app.get("/npu/health")
async def npu_health():
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            resp = await client.get(f"{NPU_CONFIG['host']}/v1/models")
            models = [m.get("id", "") for m in resp.json().get("data", [])]
            return {"online": True, "host": NPU_CONFIG["host"], "models": models}
        except Exception as exc:
            return {"online": False, "host": NPU_CONFIG["host"], "error": str(exc)}


@app.get("/services")
async def services_status():
    """Single endpoint showing health of all dependent services."""
    async with httpx.AsyncClient(timeout=3.0) as client:
        results = {}

        # Ollama
        try:
            r = await client.get("http://localhost:11434/api/tags")
            models = [m["name"] for m in r.json().get("models", [])]
            results["ollama"] = {"online": True, "models": models, "count": len(models)}
        except Exception as e:
            results["ollama"] = {"online": False, "error": str(e)}

        # FastFlowLM / NPU
        try:
            r = await client.get(f"{NPU_CONFIG['host']}/v1/models")
            npu_models = [m.get("id", "") for m in r.json().get("data", [])]
            results["fastflow"] = {"online": True, "host": NPU_CONFIG["host"],
                                   "models": npu_models}
        except Exception as e:
            results["fastflow"] = {"online": False, "host": NPU_CONFIG["host"],
                                   "error": str(e)}

        results["agent4_backend"] = AGENTS["agent4"].get("backend", "gpu")
        results["agent_backends"] = {aid: AGENTS[aid].get("backend", "gpu") for aid in AGENTS}
        results["claude"] = {
            "has_key": bool(CLAUDE_CONFIG["api_key"]),
            "model": CLAUDE_CONFIG["model"],
        }
        import shutil as _shutil, os as _os
        cli_bin  = CLAUDE_CLI_CONFIG.get("bin", "claude")
        resolved = _shutil.which(cli_bin) or any(
            _os.path.isfile(c) for c in [
                "/data/data/com.termux/files/usr/bin/claude",
                "/usr/local/bin/claude",
                _os.path.expanduser("~/.npm-global/bin/claude"),
            ]
        )
        results["claude_cli"] = {
            "available": bool(resolved),
            "model": CLAUDE_CLI_CONFIG["model"],
        }
        return results


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.2.0", "agents": list(AGENTS.keys())}


# ── Claude API config & health ─────────────────────────────────────────────────

@app.get("/claude/config")
async def get_claude_config():
    return {"model": CLAUDE_CONFIG["model"], "has_key": bool(CLAUDE_CONFIG["api_key"])}


@app.post("/claude/config")
async def set_claude_config(payload: dict):
    if "api_key" in payload and payload["api_key"].strip():
        CLAUDE_CONFIG["api_key"] = payload["api_key"].strip()
    if "model" in payload and payload["model"].strip():
        CLAUDE_CONFIG["model"] = payload["model"].strip()
    return {"status": "updated", "model": CLAUDE_CONFIG["model"],
            "has_key": bool(CLAUDE_CONFIG["api_key"])}


VALID_BACKENDS = ("gpu", "npu", "claude", "cli")

@app.post("/claude/backend")
async def set_agent_backend(payload: dict):
    agent_id = payload.get("agent_id", "agent1")
    backend  = payload.get("backend", "gpu")
    if agent_id not in AGENTS:
        raise HTTPException(404, "Agent not found")
    if backend not in VALID_BACKENDS:
        return JSONResponse(
            {"error": f"backend must be one of: {', '.join(VALID_BACKENDS)}"},
            status_code=400
        )
    AGENTS[agent_id]["backend"] = backend
    return {"status": "updated", "agent_id": agent_id, "backend": backend}


@app.get("/claude/health")
async def claude_health():
    has_key = bool(CLAUDE_CONFIG["api_key"])
    if not has_key:
        return {"online": False, "reason": "No API key configured"}
    try:
        import anthropic
        client  = anthropic.AsyncAnthropic(api_key=CLAUDE_CONFIG["api_key"])
        models  = await client.models.list()
        ids     = [m.id for m in models.data]
        return {"online": True, "model": CLAUDE_CONFIG["model"], "available_models": ids[:6]}
    except Exception as exc:
        return {"online": False, "error": str(exc)}


# ── Claude Code CLI config & health ───────────────────────────────────────────

@app.get("/claude-cli/config")
async def get_claude_cli_config():
    return {
        "model":   CLAUDE_CLI_CONFIG["model"],
        "bin":     CLAUDE_CLI_CONFIG["bin"],
        "timeout": CLAUDE_CLI_CONFIG["timeout"],
    }


@app.post("/claude-cli/config")
async def set_claude_cli_config(payload: dict):
    if "model"   in payload: CLAUDE_CLI_CONFIG["model"]   = payload["model"].strip()
    if "bin"     in payload: CLAUDE_CLI_CONFIG["bin"]     = payload["bin"].strip()
    if "timeout" in payload: CLAUDE_CLI_CONFIG["timeout"] = int(payload["timeout"])
    return {"status": "updated", "config": CLAUDE_CLI_CONFIG}


@app.get("/claude-cli/health")
async def claude_cli_health():
    import shutil, asyncio as _aio, os

    cli_bin  = CLAUDE_CLI_CONFIG.get("bin", "claude")
    resolved = shutil.which(cli_bin)

    # Also scan common install paths (important on Termux / Linux without global npm)
    if not resolved:
        candidates = [
            "/data/data/com.termux/files/usr/bin/claude",
            "/usr/local/bin/claude",
            "/usr/bin/claude",
            os.path.expanduser("~/.npm-global/bin/claude"),
            os.path.expanduser("~/.local/bin/claude"),
            os.path.expanduser("~/node_modules/.bin/claude"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                resolved = c
                break

    if not resolved:
        return {
            "online": False,
            "reason": f"'{cli_bin}' not found — install: npm install -g @anthropic-ai/claude-code",
        }

    # Run `claude --version` to confirm it works
    try:
        proc = await _aio.create_subprocess_exec(
            resolved, "--version",
            stdout=_aio.subprocess.PIPE,
            stderr=_aio.subprocess.PIPE,
        )
        stdout, stderr = await _aio.wait_for(proc.communicate(), timeout=10)
        version = (stdout or stderr).decode("utf-8", errors="replace").strip().splitlines()[0]
        return {
            "online":  True,
            "bin":     resolved,
            "version": version,
            "model":   CLAUDE_CLI_CONFIG["model"],
        }
    except _aio.TimeoutError:
        return {"online": False, "reason": "claude --version timed out"}
    except Exception as exc:
        return {"online": False, "reason": str(exc)}
