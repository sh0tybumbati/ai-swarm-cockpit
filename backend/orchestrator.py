"""
Swarm Orchestrator — WebSocket connection manager + agent loop.

Loop:  Agent1 (Engine) → Agent2 (Renderer) → Agent3 (DOM Bridge) → Agent4 (Sentinel)
       Sentinel marks COMPLETE or routes back to the responsible agent.
       Circuit breaker terminates at MAX_ITERATIONS.
       All code blocks are extracted and saved to output/<project>/.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import WebSocket
from agents import AGENTS

logger = logging.getLogger(__name__)

OLLAMA_BASE    = "http://localhost:11434"
MAX_ITERATIONS = 12
OLLAMA_TIMEOUT = 180.0
MAX_CTX_CHARS  = 2500
OUTPUT_BASE    = Path(__file__).parent.parent / "output"

# Map language identifiers → file extensions
LANG_EXT: Dict[str, str] = {
    "javascript": "js", "js": "js",
    "typescript": "ts", "ts": "ts",
    "css": "css", "html": "html",
    "glsl": "glsl", "wgsl": "wgsl",
    "python": "py",  "py": "py",
    "json": "json",  "sh": "sh", "bash": "sh",
    "": "js",  # default to js for unlabelled blocks in a game context
}

# Agent display names for file prefixes
AGENT_FILE_PREFIX = {
    "agent1": "engine",
    "agent2": "renderer",
    "agent3": "dom_bridge",
    "agent4": "sentinel",
}


# ── Code extraction & file saving ─────────────────────────────────────────────

def extract_code_blocks(text: str) -> List[Dict]:
    pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
    blocks = []
    for lang, code in pattern.findall(text):
        ext = LANG_EXT.get(lang.lower(), "txt")
        blocks.append({"lang": lang or "js", "ext": ext, "code": code.strip()})
    return blocks


def save_agent_output(project: str, agent_id: str, iteration: int, text: str) -> List[Dict]:
    """Write extracted code blocks to output/<project>/. Returns list of saved file info."""
    blocks = extract_code_blocks(text)
    if not blocks:
        return []

    prefix   = AGENT_FILE_PREFIX.get(agent_id, agent_id)
    proj_dir = OUTPUT_BASE / _safe_name(project)
    proj_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for i, block in enumerate(blocks):
        suffix   = f"_{i + 1}" if len(blocks) > 1 else ""
        filename = f"{prefix}{suffix}.{block['ext']}"
        (proj_dir / filename).write_text(block["code"], encoding="utf-8")
        saved.append({
            "file": filename,
            "size": len(block["code"]),
            "lang": block["lang"],
            "agent": agent_id,
        })
    return saved


def save_session_log(project: str, feature: str, iterations: int, outputs: Dict[str, str]):
    """Write a full markdown session log to output/<project>/session_<ts>.md."""
    proj_dir = OUTPUT_BASE / _safe_name(project)
    proj_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = proj_dir / f"session_{ts}.md"

    lines = [
        f"# Swarm Session — {project}",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Feature:** {feature}",
        f"**Iterations:** {iterations}",
        "",
    ]
    for agent_id, text in outputs.items():
        name = AGENTS.get(agent_id, {}).get("name", agent_id)
        lines += [f"## {name}\n", text.strip(), ""]

    path.write_text("\n".join(lines), encoding="utf-8")


def _safe_name(project: str) -> str:
    return re.sub(r"[^\w\-]", "_", project).lower().strip("_") or "project"


def list_output_files(project: str) -> List[Dict]:
    proj_dir = OUTPUT_BASE / _safe_name(project)
    if not proj_dir.exists():
        return []
    return [
        {"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime}
        for f in sorted(proj_dir.iterdir())
        if f.is_file()
    ]


# ── Connection manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self._conns: Dict[str, List[WebSocket]] = {}

    async def connect(self, channel: str, ws: WebSocket):
        await ws.accept()
        self._conns.setdefault(channel, []).append(ws)

    async def disconnect(self, channel: str, ws: WebSocket):
        if channel in self._conns:
            self._conns[channel] = [c for c in self._conns[channel] if c is not ws]

    async def _broadcast(self, channel: str, payload: dict):
        dead: List[WebSocket] = []
        for ws in self._conns.get(channel, []):
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(channel, ws)

    async def send_token(self, channel: str, text: str):
        await self._broadcast(channel, {"type": "token", "content": text})

    async def send_status(self, channel: str, status: str):
        await self._broadcast(channel, {"type": "status", "status": status})

    async def send_loop_update(self, iteration: int):
        await self._broadcast("console", {
            "type": "loop_update",
            "iteration": iteration,
            "max": MAX_ITERATIONS,
        })

    async def sys_log(self, msg: str, level: str = "sys"):
        await self._broadcast("console", {
            "type": "console_line",
            "message": msg,
            "level": level,
        })

    async def notify_files(self, files: List[Dict]):
        """Push a file-saved notification to the console channel."""
        for f in files:
            await self._broadcast("console", {
                "type": "file_saved",
                "file": f["file"],
                "size": f["size"],
                "agent": f.get("agent", ""),
            })


# ── Swarm orchestrator ─────────────────────────────────────────────────────────

class SwarmOrchestrator(ConnectionManager):

    async def _run_agent(self, agent_id: str, messages: list) -> str:
        agent  = AGENTS[agent_id]
        model  = agent["model"]
        output = ""

        await self.send_status(agent_id, "WORKING")
        await self.sys_log(f"[{agent['name']}] ▸ {model}")

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": agent["system"]}] + messages,
            "stream": True,
            "options": {"temperature": 0.25, "num_ctx": 4096},
        }

        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                async with client.stream("POST", f"{OLLAMA_BASE}/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        err = f"Ollama HTTP {resp.status_code}"
                        await self.send_token(agent_id, f"\n[ERROR] {err}\n")
                        await self.send_status(agent_id, "ERROR")
                        await self.sys_log(err, "error")
                        return ""

                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data  = json.loads(line)
                            token = data.get("message", {}).get("content", "")
                            if token:
                                output += token
                                await self.send_token(agent_id, token)
                            if data.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue

        except httpx.ConnectError:
            msg = f"Cannot reach Ollama at {OLLAMA_BASE} — run: ollama serve"
            await self.send_token(agent_id, f"\n[ERROR] {msg}\n")
            await self.sys_log(msg, "error")
        except httpx.ReadTimeout:
            await self.send_token(agent_id, "\n[ERROR] Model timed out\n")
            await self.sys_log(f"[{agent['name']}] timed out", "error")
        except Exception as exc:
            await self.send_token(agent_id, f"\n[ERROR] {exc}\n")
            await self.sys_log(str(exc), "error")

        await self.send_status(agent_id, "DONE")
        await self.send_token(agent_id, "\n")
        return output

    # ── Sentinel verdict parsing ───────────────────────────────────────────────

    @staticmethod
    def _parse_verdict(text: str) -> dict:
        upper = text.upper()

        # Structured verdict — preferred path
        if "VERDICT: COMPLETE" in upper:
            reason = ""
            for line in text.splitlines():
                if "REASON:" in line.upper():
                    reason = line.split(":", 1)[1].strip()
            return {"decision": "complete", "reason": reason}

        if "VERDICT: ROUTE_BACK" in upper:
            route_to = "agent1"
            reason   = "Revision needed"
            for line in text.splitlines():
                lu = line.upper()
                if "ROUTE_TO:" in lu:
                    raw = line.split(":", 1)[1].strip().lower()
                    if "2" in raw or "render" in raw:
                        route_to = "agent2"
                    elif "3" in raw or "dom" in raw or "input" in raw:
                        route_to = "agent3"
                    else:
                        route_to = "agent1"
                if "REASON:" in lu:
                    reason = line.split(":", 1)[1].strip()
            return {"decision": "route_back", "route_to": route_to, "reason": reason}

        # Heuristic fallback — smaller models sometimes ignore the format
        bug_signals   = {"bug", "error", "broken", "missing", "incorrect", "wrong",
                         "fail", "crash", "undefined", "null", "leak", "fix"}
        route_signals = {"route", "back", "rework", "revise", "redo", "retry", "agent"}

        words = set(re.findall(r"\b\w+\b", upper.lower()))
        has_bugs   = len(words & bug_signals)   >= 2
        has_route  = len(words & route_signals) >= 1

        if has_bugs and has_route:
            # Try to infer target from context
            route_to = "agent1"
            if any(w in upper for w in ("RENDER", "SHADER", "CANVAS", "WEBGL", "AGENT 2", "AGENT2")):
                route_to = "agent2"
            elif any(w in upper for w in ("DOM", "INPUT", "EVENT", "AGENT 3", "AGENT3")):
                route_to = "agent3"
            return {
                "decision": "route_back",
                "route_to": route_to,
                "reason": "Issues detected (inferred from unstructured verdict)",
            }

        # Default: treat as complete to avoid infinite loops
        return {"decision": "complete", "reason": "No explicit verdict — treated as complete"}

    # ── Main swarm loop ────────────────────────────────────────────────────────

    async def run_swarm_loop(self, feature_request: str, project: str):
        await self._broadcast("console", {"type": "project", "name": project})
        await self.sys_log(f"══ SWARM START: {project} ══")
        await self.sys_log(f"Feature: {feature_request}")

        for aid in ["agent1", "agent2", "agent3", "agent4"]:
            await self._broadcast(aid, {"type": "clear"})

        iteration    = 0
        base_context = [{"role": "user", "content": feature_request}]
        last: Dict[str, str]          = {}
        feedback: Dict[str, str]      = {}
        all_outputs: Dict[str, str]   = {}

        while iteration < MAX_ITERATIONS:
            iteration += 1
            await self.send_loop_update(iteration)
            await self.sys_log(f"── Iteration {iteration}/{MAX_ITERATIONS} ──")

            # ── Agent 1: Engine Architect ──────────────────────────────────────
            a1_msgs = list(base_context)
            if last.get("agent1") and feedback.get("agent1"):
                a1_msgs = [
                    {"role": "user", "content": feature_request},
                    {"role": "assistant", "content": last["agent1"][-MAX_CTX_CHARS:]},
                    {"role": "user", "content": f"Sentinel feedback: {feedback['agent1']}. Fix these issues."},
                ]
            resp1 = await self._run_agent("agent1", a1_msgs)
            last["agent1"]        = resp1
            all_outputs["agent1"] = resp1
            saved = save_agent_output(project, "agent1", iteration, resp1)
            if saved: await self.notify_files(saved)

            # ── Agent 2: Renderer ──────────────────────────────────────────────
            a2_user = "Implement the rendering layer for the engine structure above."
            if feedback.get("agent2"):
                a2_user += f" Sentinel feedback to address: {feedback['agent2']}"
            resp2 = await self._run_agent("agent2", [
                {"role": "user",      "content": feature_request},
                {"role": "assistant", "content": f"Engine structure (Agent 1):\n{resp1[:MAX_CTX_CHARS]}"},
                {"role": "user",      "content": a2_user},
            ])
            last["agent2"]        = resp2
            all_outputs["agent2"] = resp2
            saved = save_agent_output(project, "agent2", iteration, resp2)
            if saved: await self.notify_files(saved)

            # ── Agent 3: DOM & Input Bridge ────────────────────────────────────
            a3_user = "Implement the DOM layer and input handling for the game above."
            if feedback.get("agent3"):
                a3_user += f" Sentinel feedback to address: {feedback['agent3']}"
            resp3 = await self._run_agent("agent3", [
                {"role": "user",      "content": feature_request},
                {"role": "assistant", "content": (
                    f"Engine (Agent 1):\n{resp1[:MAX_CTX_CHARS // 2]}\n\n"
                    f"Renderer (Agent 2):\n{resp2[:MAX_CTX_CHARS // 2]}"
                )},
                {"role": "user",      "content": a3_user},
            ])
            last["agent3"]        = resp3
            all_outputs["agent3"] = resp3
            saved = save_agent_output(project, "agent3", iteration, resp3)
            if saved: await self.notify_files(saved)

            # ── Agent 4: Sentinel QA ───────────────────────────────────────────
            combined = (
                f"Feature Request: {feature_request}\n\n"
                f"=== AGENT 1 — ENGINE ARCHITECT ===\n{resp1[:MAX_CTX_CHARS]}\n\n"
                f"=== AGENT 2 — RENDERER ===\n{resp2[:MAX_CTX_CHARS]}\n\n"
                f"=== AGENT 3 — DOM & INPUT BRIDGE ===\n{resp3[:MAX_CTX_CHARS]}"
            )
            sentinel_resp = await self._run_agent("agent4", [
                {"role": "user", "content": f"Review this implementation:\n{combined}"}
            ])
            all_outputs["agent4"] = sentinel_resp

            verdict = self._parse_verdict(sentinel_resp)
            await self.sys_log(
                f"[Sentinel] {verdict['decision'].upper()}"
                + (f": {verdict['reason']}" if verdict.get("reason") else "")
            )

            if verdict["decision"] == "complete":
                save_session_log(project, feature_request, iteration, all_outputs)
                await self.sys_log(f"══ COMPLETE — {iteration} iteration(s) — session log saved ══")
                await self.send_loop_update(iteration)
                # Notify frontend of the session log file
                session_files = list_output_files(project)
                md_files = [f for f in session_files if f["name"].startswith("session_")]
                for f in md_files[-1:]:
                    await self.notify_files([{**f, "agent": "sentinel"}])
                for aid in ["agent1", "agent2", "agent3", "agent4"]:
                    await self.send_status(aid, "IDLE")
                return

            # Route back — store targeted feedback, clear other agent feedback
            route_to = verdict.get("route_to", "agent1")
            reason   = verdict.get("reason", "Revision needed")
            await self.sys_log(f"[Sentinel] → {route_to}: {reason}", "warn")
            feedback = {route_to: reason}  # Only carry feedback for the targeted agent

        # Circuit breaker
        save_session_log(project, feature_request, MAX_ITERATIONS, all_outputs)
        await self.sys_log(
            f"[!] CIRCUIT BREAKER — {MAX_ITERATIONS} iterations reached. Session log saved.",
            "error",
        )
        for aid in ["agent1", "agent2", "agent3", "agent4"]:
            await self.send_status(aid, "IDLE")
