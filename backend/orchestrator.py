"""
Swarm Orchestrator — WebSocket manager + agent loop.

Loop:  AN (Architect) → [ENLIL, ENKI as routed by AN] → ENZU (Sentinel)
       AN emits ROUTE: directive to select downstream agents.
       Sentinel marks COMPLETE or routes back to responsible agent.
       Circuit breaker at MAX_ITERATIONS.
       Code blocks extracted and saved to output/<project>/.
       Project context persisted in output/<project>/context.md.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import shutil
import tempfile

from anthropic import AsyncAnthropic
from fastapi import WebSocket
from agents import AGENTS, NPU_CONFIG, CPU_CONFIG, CLAUDE_CONFIG, CLAUDE_CLI_CONFIG
import nisaba as _nisaba

logger = logging.getLogger(__name__)

OLLAMA_BASE    = "http://localhost:11434"
MAX_ITERATIONS = 12
OLLAMA_TIMEOUT = 180.0
MAX_CTX_CHARS  = 12000
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
    "agent1": "architect",
    "agent2": "renderer",
    "agent3": "integration",
    "agent4": "sentinel",
    "agent5": "nisaba",
}


# ── Utilities ──────────────────────────────────────────────────────────────────

def _safe_name(project: str) -> str:
    return re.sub(r"[^\w\-]", "_", project).lower().strip("_") or "project"


def extract_code_blocks(text: str) -> List[Dict]:
    # Supports ```lang:filename.ext or plain ```lang
    pattern = re.compile(r"```(\w*)(?::([^\n]+))?\n(.*?)```", re.DOTALL)
    blocks = []
    for lang, filename, code in pattern.findall(text):
        lang = lang or "js"
        blocks.append({
            "lang": lang,
            "ext": LANG_EXT.get(lang.lower(), "txt"),
            "code": code.strip(),
            "filename": filename.strip() if filename else None,
        })
    return blocks


def save_agent_output(project: str, agent_id: str, iteration: int, text: str) -> List[Dict]:
    blocks = extract_code_blocks(text)
    if not blocks:
        return []
    prefix   = AGENT_FILE_PREFIX.get(agent_id, agent_id)
    proj_dir = OUTPUT_BASE / _safe_name(project)
    proj_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    seen: set = set()
    for i, block in enumerate(blocks):
        if block.get("filename"):
            filename = block["filename"]
        else:
            suffix   = f"_{i + 1}" if len(blocks) > 1 else ""
            filename = f"{prefix}{suffix}.{block['ext']}"
        # Deduplicate within one agent's output
        if filename in seen:
            base, _, ext = filename.rpartition(".")
            filename = f"{base}_{i + 1}.{ext}"
        seen.add(filename)
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

## Architectural Notes (from AN — {AGENTS['agent1']['name']})
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

    async def send_loop_update(self, iteration: int, max_iter: int = MAX_ITERATIONS):
        await self._broadcast("console", {"type": "loop_update", "iteration": iteration, "max": max_iter})

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
        for aid in ["agent1", "agent2", "agent3", "agent4", "agent5"]:
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
        """Route to GPU/CPU (Ollama), NPU (FastFlowLM), Claude API, or Claude CLI."""
        backend = AGENTS[agent_id].get("backend", "gpu")
        if backend == "cpu":
            return await self._run_cpu(agent_id, messages)
        if backend == "npu":
            return await self._run_npu(agent_id, messages)
        if backend == "claude":
            return await self._run_claude(agent_id, messages)
        if backend == "cli":
            return await self._run_claude_cli(agent_id, messages)
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
                    tok_count  = 0
                    start_time = time.monotonic()
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
                                tok_count += 1
                                await self.send_token(agent_id, token)
                                if tok_count % 8 == 0:
                                    elapsed = time.monotonic() - start_time
                                    if elapsed > 0.05:
                                        await self._broadcast(agent_id, {"type": "tps", "value": round(tok_count / elapsed, 1)})
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

        await self._broadcast(agent_id, {"type": "tps", "value": 0})
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

    # ── Claude Code CLI — subprocess streaming ─────────────────────────────────

    async def _run_claude_cli(self, agent_id: str, messages: list) -> str:
        agent   = AGENTS[agent_id]
        model   = CLAUDE_CLI_CONFIG.get("model", "claude-opus-4-7")
        cli_bin = CLAUDE_CLI_CONFIG.get("bin", "claude")
        timeout = CLAUDE_CLI_CONFIG.get("timeout", 300)
        output  = ""

        await self.send_status(agent_id, "WORKING")
        await self._broadcast(agent_id, {"type": "backend", "backend": "cli"})
        await self.sys_log(f"[{agent.get('deity', agent['name'])}] ▸ Claude CLI: {model}")

        # Resolve the binary path — check PATH then common install locations
        resolved = shutil.which(cli_bin)
        if not resolved:
            for candidate in [
                "/data/data/com.termux/files/usr/bin/claude",
                "/usr/local/bin/claude",
                "/usr/bin/claude",
                f"{__import__('os').path.expanduser('~')}/.npm-global/bin/claude",
                f"{__import__('os').path.expanduser('~')}/.local/bin/claude",
            ]:
                if __import__('os').path.isfile(candidate):
                    resolved = candidate
                    break

        if not resolved:
            msg = (
                "claude CLI not found — install Claude Code:\n"
                "  npm install -g @anthropic-ai/claude-code\n"
                "  or set CLAUDE_BIN env var to the full path"
            )
            await self.send_token(agent_id, f"\n[ERROR] {msg}\n")
            await self.sys_log(msg.split("\n")[0], "error")
            await self.send_status(agent_id, "ERROR")
            return ""

        # Build a single prompt: system instructions + conversation
        system_block = f"[SYSTEM INSTRUCTIONS]\n{agent['system']}\n\n"
        conv_lines   = []
        for m in messages:
            role = "Assistant" if m.get("role") == "assistant" else "User"
            conv_lines.append(f"{role}: {m['content']}")
        full_prompt = system_block + "\n\n".join(conv_lines)

        # Write prompt to a temp file to avoid shell argument length limits
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                          delete=False, encoding="utf-8")
        tmp.write(full_prompt)
        tmp.flush()
        tmp.close()
        tmp_path = tmp.name

        proc = None
        try:
            import os as _os
            env = _os.environ.copy()

            # stream-json gives us JSONL token-by-token; --verbose is required for that mode
            proc = await asyncio.create_subprocess_exec(
                resolved, "--print",
                "--output-format", "stream-json",
                "--verbose",
                "--model", model,
                "--input-format", "text",
                stdin=open(tmp_path, "rb"),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            await self.sys_log(f"[CLI] spawned pid={proc.pid} bin={resolved} model={model}")

            stderr_chunks: list = []

            async def _read_stderr():
                while True:
                    chunk = await proc.stderr.read(256)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)

            async def _stream_jsonl():
                nonlocal output
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    mtype = msg.get("type", "")
                    if mtype == "assistant":
                        # Extract text content blocks and relay as tokens
                        for block in msg.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                text = block["text"]
                                output += text
                                await self.send_token(agent_id, text)
                    elif mtype == "result":
                        # Fallback: if we somehow got no text from assistant messages
                        if not output:
                            output = msg.get("result", "")
                            if output:
                                await self.send_token(agent_id, output)

            try:
                await asyncio.wait_for(
                    asyncio.gather(_stream_jsonl(), _read_stderr()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                await self.send_token(agent_id, "\n[ERROR] Claude CLI timed out\n")
                await self.sys_log(f"Claude CLI timed out after {timeout}s", "error")

            await proc.wait()

            stderr_txt = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
            if stderr_txt:
                level = "error" if proc.returncode != 0 else "warn"
                await self.sys_log(f"[CLI stderr rc={proc.returncode}] {stderr_txt[:400]}", level)
                if proc.returncode != 0:
                    await self.send_token(agent_id, f"\n[CLI stderr] {stderr_txt[:400]}\n")

        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
            await self.send_token(agent_id, "\n[ABORTED]\n")
            raise
        except FileNotFoundError:
            msg = f"claude binary not executable at: {resolved}"
            await self.send_token(agent_id, f"\n[ERROR] {msg}\n")
            await self.sys_log(msg, "error")
        except Exception as exc:
            await self.send_token(agent_id, f"\n[ERROR] Claude CLI: {exc}\n")
            await self.sys_log(f"Claude CLI error: {exc}", "error")
        finally:
            __import__('os').unlink(tmp_path)

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
                    tok_count  = 0
                    start_time = time.monotonic()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data  = json.loads(line)
                            token = data.get("message", {}).get("content", "")
                            if token:
                                output += token
                                tok_count += 1
                                await self.send_token(agent_id, token)
                                if tok_count % 8 == 0:
                                    elapsed = time.monotonic() - start_time
                                    if elapsed > 0.05:
                                        await self._broadcast(agent_id, {"type": "tps", "value": round(tok_count / elapsed, 1)})
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

        await self._broadcast(agent_id, {"type": "tps", "value": 0})
        await self.send_status(agent_id, "DONE")
        await self.send_token(agent_id, "\n")
        return output

    # ── CPU streaming — second Ollama daemon (OLLAMA_NUM_GPU=0) ──────────────

    async def _run_cpu(self, agent_id: str, messages: list) -> str:
        agent      = AGENTS[agent_id]
        cpu_host   = CPU_CONFIG["host"]
        model      = agent["model"]
        output     = ""
        tok_count  = 0
        start_time = time.monotonic()

        await self.send_status(agent_id, "WORKING")
        await self._broadcast(agent_id, {"type": "backend", "backend": "cpu"})
        await self.sys_log(f"[{agent.get('deity', agent['name'])}] ▸ CPU: {model}")

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": agent["system"]}] + messages,
            "stream": True,
            "options": {"temperature": 0.25, "num_ctx": 4096},
        }

        try:
            async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
                async with client.stream("POST", f"{cpu_host}/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        err = f"CPU Ollama HTTP {resp.status_code}"
                        await self.send_token(agent_id, f"\n[ERROR] {err}\n")
                        await self.send_status(agent_id, "ERROR")
                        return ""
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data  = json.loads(line)
                            token = data.get("message", {}).get("content", "")
                            if token:
                                output    += token
                                tok_count += 1
                                await self.send_token(agent_id, token)
                                if tok_count % 8 == 0:
                                    elapsed = time.monotonic() - start_time
                                    if elapsed > 0.05:
                                        await self._broadcast(agent_id, {"type": "tps", "value": round(tok_count / elapsed, 1)})
                            if data.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
        except asyncio.CancelledError:
            await self.send_token(agent_id, "\n[ABORTED]\n")
            raise
        except httpx.ConnectError:
            msg = f"Cannot reach CPU Ollama at {cpu_host} — start with: OLLAMA_HOST=127.0.0.1:11435 OLLAMA_NUM_GPU=0 ollama serve"
            await self.send_token(agent_id, f"\n[ERROR] {msg}\n")
            await self.sys_log(msg, "error")
        except httpx.ReadTimeout:
            await self.send_token(agent_id, "\n[ERROR] CPU model timed out\n")
        except Exception as exc:
            await self.send_token(agent_id, f"\n[ERROR] {exc}\n")

        await self._broadcast(agent_id, {"type": "tps", "value": 0})
        await self.send_status(agent_id, "DONE")
        await self.send_token(agent_id, "\n")
        return output

    # ── Deliverable parser ────────────────────────────────────────────────────

    @staticmethod
    def _parse_deliverable(text: str) -> dict:
        """Extract declared filenames and run command from AN's DELIVERABLE block."""
        files, run_cmd, in_block = [], "", False
        for line in text.splitlines():
            s = line.strip()
            if "## DELIVERABLE" in s.upper():
                in_block = True
                continue
            if in_block:
                if s.startswith("##"):
                    break
                if s.lower().startswith("files:"):
                    raw = s.split(":", 1)[1].strip()
                    files = [f.strip().strip(",") for f in re.split(r"[\s,]+", raw) if f.strip()]
                elif s.lower().startswith("run:"):
                    run_cmd = s.split(":", 1)[1].strip()
        return {"files": files, "run": run_cmd}

    # ── Route parser ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_route(text: str) -> List[str]:
        """Parse AN's ROUTE: directive to determine which agents run next."""
        for line in reversed(text.splitlines()):  # scan from bottom — ROUTE: is last
            stripped = line.strip()
            if stripped.upper().startswith("ROUTE:"):
                raw = stripped.split(":", 1)[1].strip().lower()
                if not raw or raw == "none":
                    return []
                route = []
                if any(tok in raw for tok in ("agent2", "2", "enlil", "render")):
                    route.append("agent2")
                if any(tok in raw for tok in ("agent3", "3", "enki", "ui", "integrat")):
                    route.append("agent3")
                return route
        return ["agent2", "agent3"]  # default: run all if no ROUTE: tag

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
                    if any(tok in raw for tok in ("2", "render", "enlil", "visual", "css")):
                        route_to = "agent2"
                    elif any(tok in raw for tok in ("3", "dom", "input", "enki", "ui", "integrat")):
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
            if any(w in upper for w in ("RENDER", "SHADER", "CANVAS", "WEBGL", "ENLIL", "CSS", "VISUAL")):
                route_to = "agent2"
            elif any(w in upper for w in ("DOM", "INPUT", "EVENT", "ENKI", "UI", "INTEGRATION", "FETCH", "ROUTING")):
                route_to = "agent3"
            return {"decision": "route_back", "route_to": route_to,
                    "reason": "Issues detected (inferred from unstructured verdict)"}

        return {"decision": "complete", "reason": "No explicit verdict — treated as complete"}

    # ── Main swarm loop ────────────────────────────────────────────────────────

    async def _swarm_loop_inner(self, feature_request: str, project: str, max_iterations: int = MAX_ITERATIONS):
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

        while iteration < max_iterations:
            iteration += 1
            await self.send_loop_update(iteration, max_iterations)
            await self.sys_log(f"── Iteration {iteration}/{max_iterations} ──")

            # Context prefix injected into Agent 1
            ctx_prefix = f"Project context from prior sessions:\n{prior_context}\n\n" if prior_context else ""

            # ── Nisaba: Librarian query (before AN, iteration 1 only) ─────────
            nisaba_ctx = ""
            if iteration == 1:
                try:
                    await self.send_status("agent5", "WORKING")
                    await self._broadcast("agent5", {"type": "mode", "mode": "librarian"})
                    await self.send_token("agent5", f"📖 Query: {feature_request[:120]}\n")
                    result = await _nisaba.librarian_query(
                        project, feature_request,
                        stream_cb=lambda t: self.send_token("agent5", t),
                    )
                    if result["citations"]:
                        nisaba_ctx = f"\nNisaba retrieved context:\n{result['answer']}\nSources: {', '.join(result['citations'][:3])}\n\n"
                        await self.send_token("agent5", f"\n└ Sources: {', '.join(result['citations'][:5])}\n")
                    else:
                        await self.send_token("agent5", "\n└ No prior context found.\n")
                    await self.send_status("agent5", "IDLE")
                except Exception as e:
                    logger.warning("Nisaba librarian failed: %s", e)
                    await self.send_status("agent5", "IDLE")

            # Deployment context always injected so AN knows where output lands
            deploy_note = (
                "DEPLOYMENT TARGET: Output is previewed in a browser iframe. "
                "Produce a complete self-contained index.html (CSS in <style>, JS in <script>). "
                "Do not use TypeScript, npm, or build tools unless explicitly requested.\n\n"
            )

            # ── AN: App Architect ─────────────────────────────────────────────
            a1_msgs = list(base_ctx)
            if last.get("agent1") and feedback.get("agent1"):
                a1_msgs = [
                    {"role": "user", "content": deploy_note + ctx_prefix + feature_request},
                    {"role": "assistant", "content": last["agent1"][-MAX_CTX_CHARS:]},
                    {"role": "user", "content": f"ENZU feedback: {feedback['agent1']}. Fix these issues."},
                ]
            else:
                a1_msgs = [{"role": "user", "content": deploy_note + ctx_prefix + nisaba_ctx + feature_request}]

            resp1 = await self._run_agent("agent1", a1_msgs)
            last["agent1"] = all_out["agent1"] = resp1
            saved = save_agent_output(project, "agent1", iteration, resp1)
            all_saved.extend(f["file"] for f in saved)
            if saved: await self.notify_files(saved)

            # Parse AN's routing decision and file manifest
            route      = self._parse_route(resp1)
            deliverable = self._parse_deliverable(resp1)
            await self.sys_log(
                f"[AN] ROUTE → {', '.join(route).upper() or 'ENZU only (self-contained)'}", "info"
            )
            if deliverable["files"]:
                await self.sys_log(f"[AN] FILES → {', '.join(deliverable['files'])}", "info")
            file_note = (
                f"\nFile manifest declared by AN: {', '.join(deliverable['files'])}. "
                "Use these exact filenames in your code fences (e.g. ```css:style.css)."
                if deliverable["files"] else ""
            )

            # Read the HTML file AN saved — base for both ENLIL and ENKI
            proj_dir = OUTPUT_BASE / _safe_name(project)
            an_html  = ""
            for s in saved:
                if s["file"].endswith(".html"):
                    fp = proj_dir / s["file"]
                    if fp.exists():
                        an_html = fp.read_text(encoding="utf-8")
                        break
            if not an_html:
                an_html = resp1

            # ── ENLIL + ENKI: run in parallel, both from AN's base ────────────
            resp2, resp3 = "", ""
            run2 = "agent2" in route
            run3 = "agent3" in route

            async def _run_enlil():
                nonlocal resp2
                if not run2:
                    await self.sys_log("[ENLIL] Skipped", "info")
                    await self.send_status("agent2", "IDLE")
                    return
                a2_note = f"\nENZU feedback: {feedback['agent2']}" if feedback.get("agent2") else ""
                resp2 = await self._run_agent("agent2", [{"role": "user", "content": (
                    f"Task: {feature_request}\n\n"
                    f"AN's implementation:\n```html\n{an_html}\n```\n\n"
                    f"Rewrite with dramatically better visual design. "
                    f"Preserve ALL JS logic and DOM element IDs exactly — only improve CSS and layout. "
                    f"Output the complete index.html.{a2_note}"
                )}])

            async def _run_enki():
                nonlocal resp3
                if not run3:
                    await self.sys_log("[ENKI] Skipped", "info")
                    await self.send_status("agent3", "IDLE")
                    return
                a3_note = f"\nENZU feedback: {feedback['agent3']}" if feedback.get("agent3") else ""
                resp3 = await self._run_agent("agent3", [{"role": "user", "content": (
                    f"Task: {feature_request}\n\n"
                    f"AN's implementation:\n```html\n{an_html}\n```\n\n"
                    f"Fix all bugs and UX gaps: missing win/lose conditions, broken events, "
                    f"missing restart button, score display. "
                    f"Output the complete, fully working index.html.{a3_note}"
                )}])

            await asyncio.gather(_run_enlil(), _run_enki())

            if resp2:
                last["agent2"] = all_out["agent2"] = resp2
                saved2 = save_agent_output(project, "agent2", iteration, resp2)
                all_saved.extend(f["file"] for f in saved2)
                if saved2: await self.notify_files(saved2)
            if resp3:
                last["agent3"] = all_out["agent3"] = resp3
                # ENKI is the integrator — save last so it wins any filename collision
                saved3 = save_agent_output(project, "agent3", iteration, resp3)
                all_saved.extend(f["file"] for f in saved3)
                if saved3: await self.notify_files(saved3)

            # ── ENZU: Sentinel ─────────────────────────────────────────────────
            sections = [f"Feature: {feature_request}\n\n=== AN ({AGENTS['agent1']['name']}) ===\n{resp1[:MAX_CTX_CHARS]}"]
            if resp2:
                sections.append(f"=== ENLIL ({AGENTS['agent2']['name']}) ===\n{resp2[:MAX_CTX_CHARS]}")
            if resp3:
                sections.append(f"=== ENKI ({AGENTS['agent3']['name']}) ===\n{resp3[:MAX_CTX_CHARS]}")
            combined = "\n\n".join(sections)
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
                await self.send_loop_update(iteration, max_iterations)
                await self.notify_files([{"file": "context.md", "size": 0, "agent": "sentinel"}])
                for aid in ["agent1", "agent2", "agent3", "agent4"]:
                    await self.send_status(aid, "IDLE")
                await self.send_loop_state(False)

                # ── Nisaba: Scribe mode (background, non-blocking) ────────────
                async def _nisaba_scribe():
                    try:
                        await self.send_status("agent5", "WORKING")
                        await self._broadcast("agent5", {"type": "mode", "mode": "scribe"})
                        await self.send_token("agent5", f"\n✍️  Scribing: {feature_request[:80]}\n")
                        result = await _nisaba.scribe(
                            project, feature_request, all_out,
                            list(dict.fromkeys(all_saved)),
                            stream_cb=lambda t: self.send_token("agent5", t),
                        )
                        await self.send_token("agent5",
                            f"\n└ Commit: {result.get('commit','')}\n"
                            f"└ Tags: {', '.join(result.get('tags',[]))}\n"
                        )
                        # Index generated output files for future librarian queries
                        proj_dir = OUTPUT_BASE / _safe_name(project)
                        file_records = [
                            {"name": fn, "path": str(proj_dir / fn)}
                            for fn in dict.fromkeys(all_saved) if (proj_dir / fn).exists()
                        ]
                        indexed = await _nisaba.index_files(project, file_records)
                        if indexed:
                            await self.send_token("agent5", f"└ Indexed {indexed} chunks\n")
                    except Exception as e:
                        logger.warning("Nisaba scribe failed: %s", e)
                    finally:
                        await self.send_status("agent5", "IDLE")

                asyncio.create_task(_nisaba_scribe())
                return

            route_to = verdict.get("route_to", "agent1")
            reason   = verdict.get("reason", "Revision needed")
            await self.sys_log(f"[ENZU] → {route_to}: {reason}", "warn")
            feedback = {route_to: reason}
            base_ctx = [{"role": "user", "content": feature_request}]

        # Circuit breaker
        save_session_log(project, feature_request, max_iterations, all_out)
        save_context(project, feature_request, max_iterations, all_out,
                     "circuit_breaker", list(dict.fromkeys(all_saved)))
        await self.sys_log(f"[!] CIRCUIT BREAKER — {max_iterations} iterations. Context saved.", "error")
        for aid in ["agent1", "agent2", "agent3", "agent4", "agent5"]:
            await self.send_status(aid, "IDLE")
        await self.send_loop_state(False)

    async def run_swarm_loop(self, feature_request: str, project: str, max_iterations: int = MAX_ITERATIONS):
        try:
            await self._swarm_loop_inner(feature_request, project, max_iterations)
        except asyncio.CancelledError:
            await self.sys_log("[!] Swarm loop cancelled", "warn")
            for aid in ["agent1", "agent2", "agent3", "agent4", "agent5"]:
                await self.send_status(aid, "IDLE")
            await self.send_loop_state(False)
