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

from agents import AGENTS
from orchestrator import OUTPUT_BASE, SwarmOrchestrator, list_output_files, _safe_name

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger("cockpit")

app = FastAPI(title="Panoptic AI Swarm Cockpit", version="1.1.0")
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


# ── Swarm API ──────────────────────────────────────────────────────────────────

@app.get("/models")
async def list_models():
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp   = await client.get("http://localhost:11434/api/tags")
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"models": models}
        except Exception as exc:
            return {"models": [], "error": str(exc)}


@app.post("/broadcast")
async def broadcast(payload: dict):
    prompt  = (payload.get("prompt") or "").strip()
    project = (payload.get("project") or "New Project").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    asyncio.create_task(orchestrator.run_swarm_loop(prompt, project))
    return {"status": f"Swarm loop initiated: {prompt[:60]}"}


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


# ── Output files API ───────────────────────────────────────────────────────────

@app.get("/files/{project}")
async def get_project_files(project: str):
    files = list_output_files(project)
    return {"project": project, "files": files}


@app.get("/files/{project}/{filename}")
async def download_file(project: str, filename: str):
    # Sanitize: strip any directory components to prevent path traversal
    safe_file = Path(filename).name
    proj_dir  = OUTPUT_BASE / _safe_name(project)
    filepath  = proj_dir / safe_file

    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(404, "File not found")

    media = "text/plain"
    if safe_file.endswith(".html"):
        media = "text/html"
    elif safe_file.endswith(".css"):
        media = "text/css"
    elif safe_file.endswith((".js", ".ts")):
        media = "application/javascript"
    elif safe_file.endswith(".json"):
        media = "application/json"

    return FileResponse(filepath, media_type=media, filename=safe_file)


@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(AGENTS.keys())}
