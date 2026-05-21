"""
Nisaba — Scribe of the Gods (Agent 5)

Dual-mode RAG agent:
  📖 Librarian mode — query project files/history, return cited chunks
  ✍️  Scribe mode   — post-completion: summarize, write commit msg, index feature
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional

import httpx
import chromadb
from chromadb.config import Settings

logger = logging.getLogger(__name__)

CPU_HOST    = "http://localhost:11435"
EMBED_MODEL = "nomic-embed-text"
SCRIBE_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
STORE_PATH  = Path(__file__).parent.parent / "state" / "nisaba"

LIBRARIAN_SYSTEM = (
    "You are Nisaba in Librarian mode. "
    "Answer strictly from the retrieved source chunks provided. "
    "Cite source IDs in every response using [source_id] notation. "
    "If no chunk supports the answer, say 'No relevant sources found.' "
    "Be concise and precise — no hallucination."
)

SCRIBE_SYSTEM = (
    "You are Nisaba in Scribe mode. Summarize the completed feature and produce structured output.\n\n"
    "Respond EXACTLY in this format:\n"
    "COMMIT: <conventional commit message, e.g. 'feat(ui): add minesweeper game'>\n"
    "SUMMARY: <2-3 sentences describing what was built and why>\n"
    "FILES: <comma-separated list of generated files>\n"
    "TAGS: <comma-separated keywords for future retrieval>"
)


def _get_chroma() -> chromadb.ClientAPI:
    STORE_PATH.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(STORE_PATH),
        settings=Settings(anonymized_telemetry=False),
    )


async def _embed(texts: List[str]) -> List[List[float]]:
    """Get embeddings from nomic-embed-text via CPU Ollama."""
    results = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for text in texts:
            resp = await client.post(
                f"{CPU_HOST}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text},
            )
            if resp.status_code == 200:
                results.append(resp.json()["embedding"])
            else:
                logger.warning("Embed failed for text (len=%d): HTTP %d", len(text), resp.status_code)
                results.append([0.0] * 768)  # nomic default dim
    return results


async def _chat(system: str, user: str, stream_cb=None) -> str:
    """Single chat turn against CPU Ollama, optionally streaming tokens."""
    output = ""
    payload = {
        "model": SCRIBE_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream": stream_cb is not None,
        "options": {"temperature": 0.2, "num_ctx": 4096},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        if stream_cb:
            async with client.stream("POST", f"{CPU_HOST}/api/chat", json=payload) as resp:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data  = json.loads(line)
                        token = data.get("message", {}).get("content", "")
                        if token:
                            output += token
                            await stream_cb(token)
                        if data.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue
        else:
            resp = await client.post(f"{CPU_HOST}/api/chat", json={**payload, "stream": False})
            output = resp.json()["message"]["content"]
    return output


# ── Public API ──────────────────────────────────────────────────────────────────

async def index_files(project: str, files: List[Dict]) -> int:
    """Index generated output files into the project collection. Returns chunk count."""
    if not files:
        return 0
    coll = _get_chroma().get_or_create_collection(name=f"project_{_safe(project)}")
    docs, ids, metas = [], [], []
    for f in files:
        path = Path(f["path"])
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # Chunk by 800-char windows with 100-char overlap
        for i, chunk in enumerate(_chunk(text, 800, 100)):
            cid = f"{project}::{f['name']}::{i}"
            docs.append(chunk)
            ids.append(cid)
            metas.append({"source": f["name"], "project": project, "chunk": i})

    if not docs:
        return 0

    embeddings = await _embed(docs)
    # Upsert in batches of 100
    for start in range(0, len(docs), 100):
        sl = slice(start, start + 100)
        coll.upsert(documents=docs[sl], ids=ids[sl],
                    embeddings=embeddings[sl], metadatas=metas[sl])
    logger.info("Nisaba indexed %d chunks for project %s", len(docs), project)
    return len(docs)


async def librarian_query(project: str, question: str, k: int = 5,
                          stream_cb=None) -> Dict:
    """
    Librarian mode: retrieve top-k chunks, answer with citations.
    Returns {"answer": str, "citations": [source_id, ...], "chunks": [...]}
    """
    try:
        coll = _get_chroma().get_or_create_collection(name=f"project_{_safe(project)}")
    except Exception as e:
        return {"answer": f"[Nisaba] Collection error: {e}", "citations": [], "chunks": []}

    embeds = await _embed([question])
    results = coll.query(query_embeddings=embeds, n_results=min(k, coll.count() or 1))

    chunks    = results.get("documents",  [[]])[0]
    metadatas = results.get("metadatas",  [[]])[0]
    if not chunks:
        return {"answer": "No relevant sources found.", "citations": [], "chunks": []}

    # Build prompt with numbered chunks
    chunk_block = "\n\n".join(
        f"[{meta.get('source','?')}#{meta.get('chunk',i)}]\n{doc}"
        for i, (doc, meta) in enumerate(zip(chunks, metadatas))
    )
    user_prompt = f"Retrieved sources:\n{chunk_block}\n\nQuestion: {question}"
    answer = await _chat(LIBRARIAN_SYSTEM, user_prompt, stream_cb)

    citations = [f"{m.get('source','?')}#{m.get('chunk',i)}" for i, m in enumerate(metadatas)]
    return {"answer": answer, "citations": citations, "chunks": chunks}


async def scribe(project: str, feature: str, outputs: Dict[str, str],
                 saved_files: List[str], stream_cb=None) -> Dict:
    """
    Scribe mode: summarize completed feature, write commit msg, index into vector store.
    Returns parsed scribe output dict.
    """
    # Build a compact summary of what was produced
    file_list = ", ".join(saved_files) if saved_files else "(none)"
    agent_summary = ""
    for aid, resp in outputs.items():
        if resp:
            preview = resp[:600].replace("\n", " ")
            agent_summary += f"\n[{aid}]: {preview}...\n"

    user_prompt = (
        f"Project: {project}\n"
        f"Feature completed: {feature}\n"
        f"Files generated: {file_list}\n"
        f"Agent outputs (preview):{agent_summary}"
    )

    raw = await _chat(SCRIBE_SYSTEM, user_prompt, stream_cb)
    parsed = _parse_scribe(raw)
    parsed["raw"] = raw

    # Index the feature description and files into persistent store
    asyncio.create_task(_index_feature(project, feature, parsed, saved_files))

    return parsed


# ── Internal helpers ────────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    import re
    return re.sub(r"[^\w]", "_", name).lower().strip("_") or "default"


def _chunk(text: str, size: int, overlap: int) -> List[str]:
    chunks = []
    start  = 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += size - overlap
    return chunks


def _parse_scribe(raw: str) -> Dict:
    result = {"commit": "", "summary": "", "files": [], "tags": []}
    for line in raw.splitlines():
        s = line.strip()
        if s.upper().startswith("COMMIT:"):
            result["commit"] = s.split(":", 1)[1].strip()
        elif s.upper().startswith("SUMMARY:"):
            result["summary"] = s.split(":", 1)[1].strip()
        elif s.upper().startswith("FILES:"):
            result["files"] = [f.strip() for f in s.split(":", 1)[1].split(",") if f.strip()]
        elif s.upper().startswith("TAGS:"):
            result["tags"] = [t.strip() for t in s.split(":", 1)[1].split(",") if t.strip()]
    return result


async def _index_feature(project: str, feature: str, scribed: Dict, saved_files: List[str]):
    """Background task: embed and store the feature summary for future retrieval."""
    try:
        coll = _get_chroma().get_or_create_collection(name=f"project_{_safe(project)}")
        text = (
            f"Feature: {feature}\n"
            f"Commit: {scribed.get('commit','')}\n"
            f"Summary: {scribed.get('summary','')}\n"
            f"Tags: {', '.join(scribed.get('tags',[]))}\n"
            f"Files: {', '.join(saved_files)}"
        )
        embeds = await _embed([text])
        import time
        fid = f"feature::{_safe(project)}::{int(time.time())}"
        coll.upsert(
            documents=[text], ids=[fid], embeddings=embeds,
            metadatas=[{"source": "feature_history", "project": project,
                        "commit": scribed.get("commit", ""), "chunk": 0}],
        )
        logger.info("Nisaba indexed feature '%s' as %s", feature[:50], fid)
    except Exception as e:
        logger.warning("Nisaba feature indexing failed: %s", e)
