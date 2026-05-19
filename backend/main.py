"""
Panoptic AI Swarm Cockpit — FastAPI Backend
Run: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import asyncio
import json
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agents import AGENTS
from orchestrator import SwarmOrchestrator

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger("cockpit")

app = FastAPI(title="Panoptic AI Swarm Cockpit", version="1.0.0")
orchestrator = SwarmOrchestrator()

FRONTEND = Path(__file__).parent.parent / "frontend"

# ── Static files ───────────────────────────────────────────────────────────────

if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index = FRONTEND / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Frontend not found. Run from project root.</h1>", status_code=404)


# FastAPI doesn't auto-serve JS/CSS at root — wire them explicitly
@app.get("/style.css")
async def serve_css():
    from fastapi.responses import Response
    f = FRONTEND / "style.css"
    return Response(f.read_text(), media_type="text/css")


@app.get("/app.js")
async def serve_js():
    from fastapi.responses import Response
    f = FRONTEND / "app.js"
    return Response(f.read_text(), media_type="application/javascript")


# ── WebSocket endpoints ────────────────────────────────────────────────────────

@app.websocket("/ws/{channel}")
async def ws_endpoint(websocket: WebSocket, channel: str):
    await orchestrator.connect(channel, websocket)
    logger.info(f"WS open: {channel}")
    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=25.0)
                if msg == "pong":
                    pass  # heartbeat response
            except asyncio.TimeoutError:
                # Keepalive ping to the client
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        logger.info(f"WS closed: {channel}")
    except Exception as exc:
        logger.warning(f"WS error on {channel}: {exc}")
    finally:
        await orchestrator.disconnect(channel, websocket)


# ── REST API ───────────────────────────────────────────────────────────────────

@app.get("/models")
async def list_models():
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get("http://localhost:11434/api/tags")
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
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
    return {"status": f"Swarm loop initiated for: {prompt[:60]}"}


@app.get("/agents")
async def get_agents():
    return AGENTS


@app.post("/agents/{agent_id}/config")
async def update_agent(agent_id: str, config: dict):
    if agent_id not in AGENTS:
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    for key in ("model", "role"):
        if key in config:
            AGENTS[agent_id][key] = config[key]
    return {"status": "updated", "agent": AGENTS[agent_id]}


@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(AGENTS.keys())}
