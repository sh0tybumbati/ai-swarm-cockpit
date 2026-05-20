"""
Swarm Orchestrator — WebSocket manager + agent loop.

Loop:  AN (Engine) → ENLIL (Renderer) → ENKI (DOM) → ENZU (Sentinel)
       Sentinel marks COMPLETE or routes back to responsible agent.
       Circuit breaker at MAX_ITERATIONS.
       Code blocks extracted and saved to output/<project>/.
       Project context persisted in output/<project>/context.md.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from anthropic import AsyncAnthropic
from fastapi import WebSocket
from agents import AGENTS, NPU_CONFIG, CLAUDE_CONFIG

logger = logging.getLogger(__name__)

OLLAMA_BASE    = "http://localhost:11434"
MAX_ITERATIONS = 12
OLLAMA_TIMEOUT = 180.0
MAX_CTX_CHARS  = 2500
OUTPUT_BASE    = Path(__file__).parent.parent / "output"

LANG_EXT: Dict[str, str] = {
    "javascript": "js", "js": "js",
    "typescript": "ts", "ts": "ts",
    "css": "css", "html": "html",
    "glsl": "glsl", "wgsl": "wgsl",
    "python": "py", "py": "py",
    "json": "json", "sh": "sh", "bash": "sh",
    "": "js",
}

AGENT_FILE_PREFIX = {
    "agent1": "engine",
    "agent2": "renderer",
    "agent3": "dom_bridge",
    "agent4": "sentinel",
}


# ── Utilities ──────────────────────────────────────────────────────────────────

def _safe_name(project: str) -> str:
    return re.sub(r"[^\w\-]", "_", project).lower().strip("_") or "project"


def extract_code_blocks(text: str) -> List[Dict]:
    pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
    return [
        {"lang": lang or "js", "ext": LANG_EXT.get(lang.lower(), "txt"), "code": code.strip()}
        for lang, code in pattern.findall(text)
    ]


def save_agent_output(project: str, agent_id: str, iteration: int, text: str) -> List[Dict]:
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
        saved.append({"file": filename, "size": len(block["code"]), "lang": block["lang"], "agent": agent_id})
    return saved


def list_output_files(project: str) -> List[Dict]:
    proj_dir = OUTPUT_BASE / _safe_name(project)
    if not proj_dir.exists():
        return []
    return [
        {"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime}
        for f in sorted(proj_dir.iterdir()) if f.is_file()
    ]


# ── Project context ────────────────────────────────────────────────────────────

def load_context(project: str) -> str:
    path = OUTPUT_BASE / _safe_name(project) / "context.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def save_context(project: str, feature: str, iteration: int,
                 outputs: Dict[str, str], verdict: str, saved_files: List[str]):
    proj_dir = OUTPUT_BASE / _safe_name(project)
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / "context.md"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Extract a short architectural summary from Agent 1's output
    a1 = outputs.get("agent1", "")
    # Pull first non-empty comment or headline from the code
    decision_lines = [
        ln.strip().lstrip("/#* ")
        for ln in a1.splitlines()
        if ln.strip().startswith(("//", "#", "/*", "*")) and len(ln.strip()) > 5
    ][:5]
    decisions = "\n".join(f"- {d}" for d in decision_lines) if decision_lines else "- (no inline comments found)"

    # Sentinel last feedback
    sentinel_text = outputs.get("agent4", "")
    reason_line = ""
    for line in sentinel_text.splitlines():
        if "REASON:" in line.upper():
            reason_line = line.split(":", 1)[1].strip()
            break

    content = f"""# Project Context: {project}

## Last Updated
{now} — Iteration {iteration} — {verdict.upper()}

## Feature Worked On
{feature}

## Architectural Notes (from AN — Engine Architect)
{decisions}

## Generated Files
{chr(10).join(f"- {f}" for f in saved_files) if saved_files else "- (none)"}

## Sentinel Last Verdict
{verdict.upper()}{f" — {reason_line}" if reason_line else ""}

---
*This file is auto-updated each loop. Agents read it at the start of the next session.*
"""
    path.write_text(content, encoding="utf-8")


def save_session_log(project: str, feature: str, iterations: int, outputs: Dict[str, str]):
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
        await self._broadcast("console", {"type": "loop_update", "iteration": iteration, "max": MAX_ITERATIONS})

    async def send_loop_state(self, running: bool):
        await self._broadcast("console", {"type": "loop_state", "running": running})

    async def sys_log(self, msg: str, level: str = "sys"):
        await self._broadcast("console", {"type": "console_line", "message": msg, "level": level})

    async def notify_files(self, files: List[Dict]):
        for f in files:
            await self._broadcast("console", {"type": "file_saved", "file": f["file"],
                                               "size": f["size"], "agent": f.get("agent", "")})


# ── Swarm orchestrator ─────────────────────────────────────────────────────────

class SwarmOrchestrator(ConnectionManager):

    def __init__(self):
        super().__init__()
        self._active_task: Optional[asyncio.Task] = None

    # ── Stop ──────────────────────────────────────────────────────────────────

    async def stop(self):
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
        # Reset all agent statuses
        for aid in ["agent1", "agent2", "agent3", "agent4"]:
            await self.send_status(aid, "IDLE")
        await self.send_loop_state(False)
        await self.sys_log("[!] Loop aborted by user", "warn")

    # ── Single agent run ───────────────────────────────────────────────────────

    async def run_single_agent(self, agent_id: str, prompt: str, project: str):
        context = load_context(project)
        messages = [{"role": "user", "content": prompt}]
        if context:
            messages = [
                {"role": "user", "content": f"Project context:\n{context}\n\nTask: {prompt}"}
            ]
        await self.send_loop_state(True)
        try:
            resp = await self._run_agent(agent_id, messages)
            saved = save_agent_output(project, agent_id, 0, resp)
            if saved:
                await self.notify_files(saved)
        except asyncio.CancelledError:
            await self.sys_log(f"[{agent_id}] Solo run cancelled", "warn")
        finally:
            await self.send_loop_state(False)

    # ── Inference dispatch ─────────────────────────────────────────────────────

    async def _run_agent(self, agent_id: str, messages: list) -> str:
        """Route to GPU (Ollama), NPU (FastFlowLM), or Claude API based on agent backend."""
        backend = AGENTS[agent_id].get("backend", "gpu")
        if backend == "npu":
            return await self._run_npu(agent_id, messages)
        if backend == "claude":
            return await self._run_claude(agent_id, messages)
        return await self._run_gpu(agent_id, messages)

    # ── NPU streaming — FastFlowLM (OpenAI-compatible) ─────────────────────────

    async def _run_npu(self, agent_id: str, messages: list) -> str:
        agent     = AGENTS[agent_id]
        npu_host  = NPU_CONFIG["host"]
        npu_model = NPU_CONFIG["model"]
        output    = ""

        await self.send_status(agent_id, "WORKING")
        await self._broadcast(agent_id, {"type": "backend", "backend": "npu"})
        await self.sys_log(f"[{agent.get('deity', agent['name'])}] ▸ NPU: {npu_model}")

        payload = {
            "model": npu_model,
            "messages": [{"role": "system", "content": agent["system"]}] + messages,
            "stream": True,
            "temperature": 0.25,
            "max_tokens": 4096,
        }

        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                async with client.stream(
                    "POST", f"{npu_host}/v1/chat/completions", json=payload
                ) as resp:
                    if resp.status_code != 200:
                        err = f"FastFlowLM HTTP {resp.status_code}"
                        await self.send_token(agent_id, f"\n[ERROR] {err}\n")
                        await self.send_status(agent_id, "ERROR")
                        await self.sys_log(err, "error")
                        return ""
                    async for raw in resp.aiter_lines():
                        raw = raw.strip()
                        if not raw or raw == "data: [DONE]":
                            continue
                        line = raw.removeprefix("data: ")
                        try:
                            data  = json.loads(line)
                            token = (data.get("choices") or [{}])[0] \
                                        .get("delta", {}).get("content", "")
                            if token:
                                output += token
                                await self.send_token(agent_id, token)
                        except json.JSONDecodeError:
                            continue
        except asyncio.CancelledError:
            await self.send_token(agent_id, "\n[ABORTED]\n")
            raise
        except httpx.ConnectError:
            msg = f"Cannot reach FastFlowLM at {npu_host} — is the NPU server running?"
            await self.send_token(agent_id, f"\n[ERROR] {msg}\n")
            await self.sys_log(msg, "error")
        except httpx.ReadTimeout:
            await self.send_token(agent_id, "\n[ERROR] NPU inference timed out\n")
        except Exception as exc:
            await self.send_token(agent_id, f"\n[ERROR] {exc}\n")

        await self.send_status(agent_id, "DONE")
        await self.send_token(agent_id, "\n")
        return output

    # ── Claude API streaming — Anthropic ──────────────────────────────────────

    async def _run_claude(self, agent_id: str, messages: list) -> str:
        agent  = AGENTS[agent_id]
        model  = CLAUDE_CONFIG.get("model", "claude-opus-4-7")
        api_key = CLAUDE_CONFIG.get("api_key", "")
        output = ""

        await self.send_status(agent_id, "WORKING")
        await self._broadcast(agent_id, {"type": "backend", "backend": "claude"})
        await self.sys_log(f"[{agent.get('deity', agent['name'])}] ▸ Claude: {model}")

        if not api_key:
            msg = "ANTHROPIC_API_KEY not set — set it via /claude/config or env var"
            await self.send_token(agent_id, f"\n[ERROR] {msg}\n")
            await self.sys_log(msg, "error")
            await self.send_status(agent_id, "ERROR")
            return ""

        # Convert Ollama-style messages to Anthropic format (strip system role)
        anthropic_messages = [m for m in messages if m.get("role") != "system"]
        if not anthropic_messages:
            anthropic_messages = [{"role": "user", "content": "Begin."}]

        try:
            client = AsyncAnthropic(api_key=api_key)
            async with client.messages.stream(
                model=model,
                max_tokens=8096,
                thinking={"type": "adaptive"},
                system=[{
                    "type": "text",
                    "text": agent["system"],
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=anthropic_messages,
            ) as stream:
                async for text in stream.text_stream:
                    output += text
                    await self.send_token(agent_id, text)
        except asyncio.CancelledError:
            await self.send_token(agent_id, "\n[ABORTED]\n")
            raise
        except Exception as exc:
            err = str(exc)
            await self.send_token(agent_id, f"\n[ERROR] Claude: {err}\n")
            await self.sys_log(f"Claude error: {err}", "error")

        await self.send_status(agent_id, "DONE")
        await self.send_token(agent_id, "\n")
        return output

    # ── GPU streaming — Ollama ─────────────────────────────────────────────────

    async def _run_gpu(self, agent_id: str, messages: list) -> str:
        agent  = AGENTS[agent_id]
        model  = agent["model"]
        output = ""

        await self.send_status(agent_id, "WORKING")
        await self._broadcast(agent_id, {"type": "backend", "backend": "gpu"})
        await self.sys_log(f"[{agent.get('deity', agent['name'])}] ▸ GPU: {model}")

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
        except asyncio.CancelledError:
            await self.send_token(agent_id, "\n[ABORTED]\n")
            raise
        except httpx.ConnectError:
            msg = f"Cannot reach Ollama at {OLLAMA_BASE} — run: ollama serve"
            await self.send_token(agent_id, f"\n[ERROR] {msg}\n")
            await self.sys_log(msg, "error")
        except httpx.ReadTimeout:
            await self.send_token(agent_id, "\n[ERROR] Model timed out\n")
        except Exception as exc:
            await self.send_token(agent_id, f"\n[ERROR] {exc}\n")

        await self.send_status(agent_id, "DONE")
        await self.send_token(agent_id, "\n")
        return output

    # ── Sentinel verdict parser ────────────────────────────────────────────────

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
                    if "2" in raw or "render" in raw or "enlil" in raw:
                        route_to = "agent2"
                    elif "3" in raw or "dom" in raw or "input" in raw or "enki" in raw:
                        route_to = "agent3"
                    else:
                        route_to = "agent1"
                if "REASON:" in lu:
                    reason = line.split(":", 1)[1].strip()
            return {"decision": "route_back", "route_to": route_to, "reason": reason}

        # Heuristic fallback for models that ignore the format
        words = set(re.findall(r"\b\w+\b", upper.lower()))
        bug_signals   = {"bug", "error", "broken", "missing", "incorrect", "wrong",
                         "fail", "crash", "undefined", "null", "leak", "fix"}
        route_signals = {"route", "back", "rework", "revise", "redo", "retry", "agent"}

        if len(words & bug_signals) >= 2 and len(words & route_signals) >= 1:
            route_to = "agent1"
            if any(w in upper for w in ("RENDER", "SHADER", "CANVAS", "WEBGL", "ENLIL")):
                route_to = "agent2"
            elif any(w in upper for w in ("DOM", "INPUT", "EVENT", "ENKI")):
                route_to = "agent3"
            return {"decision": "route_back", "route_to": route_to,
                    "reason": "Issues detected (inferred from unstructured verdict)"}

        return {"decision": "complete", "reason": "No explicit verdict — treated as complete"}

    # ── Main swarm loop ────────────────────────────────────────────────────────

    async def _swarm_loop_inner(self, feature_request: str, project: str):
        await self._broadcast("console", {"type": "project", "name": project})
        await self.send_loop_state(True)
        await self.sys_log(f"══ SWARM START: {project} ══")
        await self.sys_log(f"Feature: {feature_request}")

        for aid in ["agent1", "agent2", "agent3", "agent4"]:
            await self._broadcast(aid, {"type": "clear"})

        # Load prior context
        prior_context = load_context(project)
        if prior_context:
            await self.sys_log("[CTX] Prior project context loaded", "info")

        iteration  = 0
        base_ctx   = [{"role": "user", "content": feature_request}]
        last:      Dict[str, str] = {}
        feedback:  Dict[str, str] = {}
        all_out:   Dict[str, str] = {}
        all_saved: List[str]      = []

        while iteration < MAX_ITERATIONS:
            iteration += 1
            await self.send_loop_update(iteration)
            await self.sys_log(f"── Iteration {iteration}/{MAX_ITERATIONS} ──")

            # Context prefix injected into Agent 1
            ctx_prefix = f"Project context from prior sessions:\n{prior_context}\n\n" if prior_context else ""

            # ── AN: Engine Architect ───────────────────────────────────────────
            a1_msgs = list(base_ctx)
            if last.get("agent1") and feedback.get("agent1"):
                a1_msgs = [
                    {"role": "user", "content": ctx_prefix + feature_request},
                    {"role": "assistant", "content": last["agent1"][-MAX_CTX_CHARS:]},
                    {"role": "user", "content": f"ENZU feedback: {feedback['agent1']}. Fix these issues."},
                ]
            elif ctx_prefix:
                a1_msgs = [{"role": "user", "content": ctx_prefix + feature_request}]

            resp1 = await self._run_agent("agent1", a1_msgs)
            last["agent1"] = all_out["agent1"] = resp1
            saved = save_agent_output(project, "agent1", iteration, resp1)
            all_saved.extend(f["file"] for f in saved)
            if saved: await self.notify_files(saved)

            # ── ENLIL: Renderer ────────────────────────────────────────────────
            a2_note = f" ENZU feedback: {feedback['agent2']}" if feedback.get("agent2") else ""
            resp2 = await self._run_agent("agent2", [
                {"role": "user",      "content": feature_request},
                {"role": "assistant", "content": f"Engine structure (AN):\n{resp1[:MAX_CTX_CHARS]}"},
                {"role": "user",      "content": f"Implement the rendering layer.{a2_note}"},
            ])
            last["agent2"] = all_out["agent2"] = resp2
            saved = save_agent_output(project, "agent2", iteration, resp2)
            all_saved.extend(f["file"] for f in saved)
            if saved: await self.notify_files(saved)

            # ── ENKI: DOM & Input Bridge ───────────────────────────────────────
            a3_note = f" ENZU feedback: {feedback['agent3']}" if feedback.get("agent3") else ""
            resp3 = await self._run_agent("agent3", [
                {"role": "user",      "content": feature_request},
                {"role": "assistant", "content": (
                    f"Engine (AN):\n{resp1[:MAX_CTX_CHARS // 2]}\n\n"
                    f"Renderer (ENLIL):\n{resp2[:MAX_CTX_CHARS // 2]}"
                )},
                {"role": "user", "content": f"Implement DOM layer and input handling.{a3_note}"},
            ])
            last["agent3"] = all_out["agent3"] = resp3
            saved = save_agent_output(project, "agent3", iteration, resp3)
            all_saved.extend(f["file"] for f in saved)
            if saved: await self.notify_files(saved)

            # ── ENZU: Sentinel ─────────────────────────────────────────────────
            combined = (
                f"Feature: {feature_request}\n\n"
                f"=== AN (Engine) ===\n{resp1[:MAX_CTX_CHARS]}\n\n"
                f"=== ENLIL (Renderer) ===\n{resp2[:MAX_CTX_CHARS]}\n\n"
                f"=== ENKI (DOM Bridge) ===\n{resp3[:MAX_CTX_CHARS]}"
            )
            sentinel_resp = await self._run_agent("agent4", [
                {"role": "user", "content": f"Review this implementation:\n{combined}"}
            ])
            all_out["agent4"] = sentinel_resp

            verdict = self._parse_verdict(sentinel_resp)
            verdict_str = verdict["decision"]
            await self.sys_log(
                f"[ENZU] {verdict_str.upper()}" +
                (f": {verdict['reason']}" if verdict.get("reason") else "")
            )

            if verdict_str == "complete":
                save_session_log(project, feature_request, iteration, all_out)
                save_context(project, feature_request, iteration, all_out,
                             "complete", list(dict.fromkeys(all_saved)))
                await self.sys_log(f"══ COMPLETE — {iteration} iteration(s) — context + log saved ══")
                await self.send_loop_update(iteration)
                await self.notify_files([{"file": "context.md", "size": 0, "agent": "sentinel"}])
                for aid in ["agent1", "agent2", "agent3", "agent4"]:
                    await self.send_status(aid, "IDLE")
                await self.send_loop_state(False)
                return

            route_to = verdict.get("route_to", "agent1")
            reason   = verdict.get("reason", "Revision needed")
            await self.sys_log(f"[ENZU] → {route_to}: {reason}", "warn")
            feedback = {route_to: reason}
            base_ctx = [{"role": "user", "content": feature_request}]

        # Circuit breaker
        save_session_log(project, feature_request, MAX_ITERATIONS, all_out)
        save_context(project, feature_request, MAX_ITERATIONS, all_out,
                     "circuit_breaker", list(dict.fromkeys(all_saved)))
        await self.sys_log(f"[!] CIRCUIT BREAKER — {MAX_ITERATIONS} iterations. Context saved.", "error")
        for aid in ["agent1", "agent2", "agent3", "agent4"]:
            await self.send_status(aid, "IDLE")
        await self.send_loop_state(False)

    async def run_swarm_loop(self, feature_request: str, project: str):
        try:
            await self._swarm_loop_inner(feature_request, project)
        except asyncio.CancelledError:
            await self.sys_log("[!] Swarm loop cancelled", "warn")
            for aid in ["agent1", "agent2", "agent3", "agent4"]:
                await self.send_status(aid, "IDLE")
            await self.send_loop_state(False)
