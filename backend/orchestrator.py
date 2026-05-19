"""
Swarm Orchestrator — manages WebSocket connections and the agent loop.

Loop:  Agent1 (Engine) → Agent2 (Renderer) → Agent4 (Sentinel)
       Sentinel either marks COMPLETE or routes back with a reason.
       Circuit breaker terminates at MAX_ITERATIONS.
"""

import asyncio
import json
import logging
from typing import Dict, List

import httpx
from fastapi import WebSocket
from agents import AGENTS

logger = logging.getLogger(__name__)

OLLAMA_BASE    = "http://localhost:11434"
MAX_ITERATIONS = 12
OLLAMA_TIMEOUT = 180.0
MAX_CTX_CHARS  = 2000   # truncation limit when passing context between agents


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

    # ── Convenience helpers ─────────────────────────────

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


class SwarmOrchestrator(ConnectionManager):

    # ── Ollama streaming ────────────────────────────────

    async def _run_agent(self, agent_id: str, messages: list) -> str:
        agent  = AGENTS[agent_id]
        model  = agent["model"]
        output = ""

        await self.send_status(agent_id, "WORKING")
        await self.sys_log(f"[{agent['name']}] → {model}")

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": agent["system"]}] + messages,
            "stream": True,
            "options": {"temperature": 0.3, "num_ctx": 4096},
        }

        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_BASE}/api/chat",
                    json=payload,
                ) as resp:
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

    # ── Sentinel verdict parser ─────────────────────────

    @staticmethod
    def _parse_verdict(text: str) -> dict:
        upper = text.upper()

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

        # Ambiguous — treat as complete to avoid infinite loops
        return {"decision": "complete", "reason": "No explicit verdict; treating as complete"}

    # ── Main swarm loop ─────────────────────────────────

    async def run_swarm_loop(self, feature_request: str, project: str):
        await self._broadcast("console", {"type": "project", "name": project})
        await self.sys_log(f"══ SWARM START: {project} ══")
        await self.sys_log(f"Feature: {feature_request}")

        # Clear all agent streams at loop start
        for aid in ["agent1", "agent2", "agent3", "agent4"]:
            await self._broadcast(aid, {"type": "clear"})

        iteration       = 0
        context_msgs    = [{"role": "user", "content": feature_request}]
        last_outputs: Dict[str, str] = {}

        while iteration < MAX_ITERATIONS:
            iteration += 1
            await self.send_loop_update(iteration)
            await self.sys_log(f"── Iteration {iteration}/{MAX_ITERATIONS} ──")

            # ── Agent 1: Engine Architect ──────────────
            a1_msgs = list(context_msgs)
            if last_outputs.get("agent1"):
                a1_msgs[-1] = {
                    "role": "user",
                    "content": (
                        f"{feature_request}\n\n"
                        f"Previous implementation to revise:\n"
                        f"{last_outputs['agent1'][-MAX_CTX_CHARS:]}"
                    )
                }
            resp1 = await self._run_agent("agent1", a1_msgs)
            last_outputs["agent1"] = resp1

            # ── Agent 2: Renderer ──────────────────────
            resp2 = await self._run_agent("agent2", [
                {"role": "user", "content": feature_request},
                {
                    "role": "assistant",
                    "content": f"Engine structure from Agent 1:\n{resp1[:MAX_CTX_CHARS]}"
                },
                {
                    "role": "user",
                    "content": (
                        "Implement the rendering layer for the above engine structure. "
                        + (f"Note: {last_outputs.get('agent2_feedback', '')}" if last_outputs.get('agent2_feedback') else "")
                    )
                },
            ])
            last_outputs["agent2"] = resp2

            # ── Agent 4: Sentinel ──────────────────────
            combined = (
                f"Feature Request: {feature_request}\n\n"
                f"=== AGENT 1 — ENGINE ARCHITECT ===\n{resp1[:MAX_CTX_CHARS]}\n\n"
                f"=== AGENT 2 — RENDERER ===\n{resp2[:MAX_CTX_CHARS]}"
            )
            sentinel_resp = await self._run_agent("agent4", [
                {"role": "user", "content": f"Review the following implementation:\n{combined}"}
            ])

            verdict = self._parse_verdict(sentinel_resp)
            await self.sys_log(f"[Sentinel] {verdict['decision'].upper()}: {verdict.get('reason', '')}")

            if verdict["decision"] == "complete":
                await self.sys_log(f"══ LOOP COMPLETE — {iteration} iteration(s) ══")
                await self.send_loop_update(iteration)
                for aid in ["agent1", "agent2", "agent3", "agent4"]:
                    await self.send_status(aid, "IDLE")
                return

            # Route back — update context with Sentinel feedback
            route_to = verdict.get("route_to", "agent1")
            reason   = verdict.get("reason", "Revision needed")
            await self.sys_log(f"[Sentinel] Routing → {route_to}: {reason}", "warn")

            if route_to == "agent2":
                last_outputs["agent2_feedback"] = reason
            else:
                context_msgs = [
                    {"role": "user", "content": feature_request},
                    {"role": "assistant", "content": last_outputs.get(route_to, "")[:MAX_CTX_CHARS]},
                    {"role": "user", "content": f"The Sentinel found issues: {reason}. Fix these problems."},
                ]

        # Circuit breaker
        await self.sys_log(
            f"[!] CIRCUIT BREAKER — max {MAX_ITERATIONS} iterations reached. Manual review required.",
            "error",
        )
        for aid in ["agent1", "agent2", "agent3", "agent4"]:
            await self.send_status(aid, "IDLE")
