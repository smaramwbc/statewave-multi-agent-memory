"""
Statewave Multi-Agent Memory — FastAPI + SSE backend.

Run with:
    python server.py
Then open http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx
import litellm

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from agents.analyst import run_analyst
from agents.candidates import build_competitor_candidates
from agents.base import AsyncStatewaveClient, StatewaveError

load_dotenv()

SUBJECT_ID = "market-intel"
SOURCES_DIR = Path(__file__).parent / "sources"

_APP_SECRET = os.environ.get("APP_SECRET", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_auth(key: str | None = Security(_api_key_header)) -> None:
    if not _APP_SECRET:
        return  # auth disabled when APP_SECRET is unset (local dev)
    if key != _APP_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

_AGENTS = [
    ("bloomberg",  "bloomberg.json"),
    ("techcrunch", "techcrunch.json"),
    ("earnings",   "earnings.json"),
]

# Per-connection SSE queues: session_id -> asyncio.Queue
_queues: dict[str, asyncio.Queue] = {}

app = FastAPI(title="Statewave Multi-Agent Demo")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── SSE helpers ───────────────────────────────────────────────────────────────

async def broadcast(event: dict) -> None:
    dead: list[str] = []
    for sid, q in list(_queues.items()):
        try:
            await q.put(event)
        except Exception:
            dead.append(sid)
    for sid in dead:
        _queues.pop(sid, None)


def _sw() -> AsyncStatewaveClient:
    return AsyncStatewaveClient(
        os.environ.get("STATEWAVE_URL", "http://localhost:8100"),
        os.environ.get("STATEWAVE_API_KEY"),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/events", dependencies=[Security(_require_auth)])
async def events():
    session_id = str(uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _queues[session_id] = queue

    async def generator():
        try:
            while True:
                event = await queue.get()
                yield {"data": json.dumps(event)}
        except asyncio.CancelledError:
            pass
        finally:
            _queues.pop(session_id, None)

    return EventSourceResponse(generator())


@app.post("/run", dependencies=[Security(_require_auth)])
async def run_agents():
    """Reset Statewave subject and launch all 3 analyst agents."""
    async with _sw() as sw:
        try:
            await sw.delete_subject(SUBJECT_ID)
            await broadcast({"type": "status", "msg": "Reset: prior subject cleared"})
        except (StatewaveError, httpx.RequestError):
            await broadcast({"type": "status", "msg": "Starting fresh (no prior data)"})

    llm_key = os.environ.get("LLM_API_KEY", "")
    llm_model = os.environ.get("LLM_MODEL", "groq/llama-3.3-70b-versatile")
    sw_url = os.environ.get("STATEWAVE_URL", "http://localhost:8100")
    sw_key = os.environ.get("STATEWAVE_API_KEY")

    def on_log(agent_id: str, msg: str) -> None:
        # Strip Rich markup tags for web display
        clean = _strip_markup(msg)
        asyncio.create_task(broadcast({"type": "agent_log", "agent": agent_id, "msg": clean}))

    def on_memory_update(agent_id: str, diff: dict) -> None:
        asyncio.create_task(broadcast({"type": "memory_update", "agent": agent_id, "diff": diff}))

    async def _seed_bloomberg_stripe() -> None:
        """Seed Bloomberg's stale Stripe pricing (3.5% + 35¢) as structured atomic
        candidates — no LLM. The pricing candidate carries the authoritative v2
        claim from the source, and positioning/differentiators are independent
        atomic facts. Guarantees the later 2.9% source always has something to
        supersede, while the independent Bloomberg facts survive that
        supersession (the whole point of atomic structured candidates)."""
        bloomberg = json.loads((SOURCES_DIR / "bloomberg.json").read_text(encoding="utf-8"))
        stripe = next(c for c in bloomberg["competitors"] if c.get("name") == "Stripe")
        published = bloomberg.get("published", "2026-05-16")
        raw_text, candidates = build_competitor_candidates(stripe, "bloomberg", published)
        async with AsyncStatewaveClient(sw_url, sw_key) as sw:
            before_ids: set[str] = {m["id"] for m in await sw.search_memories(SUBJECT_ID)}
            await sw.post_episode(
                subject_id=SUBJECT_ID,
                source="bloomberg",
                type="agent.analyst.findings",
                payload={
                    "text": raw_text,
                    "statewave": {"memory_candidates": candidates},
                    "competitor": "Stripe",
                    "source_label": "bloomberg",
                    "published": published,
                },
            )
            await sw.compile_memories(SUBJECT_ID)
            diff = await sw.get_memory_diff(SUBJECT_ID, before_ids)
        # Push seed memory to the Live Memory panel so it shows green before agents run
        if diff["new"]:
            await broadcast({"type": "memory_update", "agent": "bloomberg", "diff": diff})

    async def _run():
        await broadcast({"type": "run_started"})

        # Seed Bloomberg's stale Stripe fact directly (no LLM) so it's compiled
        # and active before any agent runs. This guarantees TechCrunch's 2.9% fact
        # will supersede it — the core conflict resolution demo moment.
        try:
            await _seed_bloomberg_stripe()
        except httpx.RequestError as exc:
            await broadcast({
                "type": "agent_log",
                "agent": "bloomberg",
                "msg": (
                    "ERROR: unable to reach the Statewave backend at "
                    f"{sw_url}. Start the Statewave service or set STATEWAVE_URL. "
                    f"Details: {exc}"
                ),
            })
            await broadcast({"type": "agents_done", "supersessions": 0})
            return
        await broadcast({
            "type": "agent_log", "agent": "bloomberg",
            "msg": "Seeded: Stripe pricing at 3.5% + 35¢ (stale Bloomberg fact, pre-reversal)",
        })
        await broadcast({
            "type": "agent_log", "agent": "bloomberg",
            "msg": "Waiting for TechCrunch and Earnings agents to commit contradicting facts...",
        })

        # One shared lock serializes each agent's post→compile→diff so concurrent
        # agents never double-compile the same uncompiled episode.
        compile_lock = asyncio.Lock()

        # All 3 agents run concurrently — no stagger needed since seed is already committed.
        # Bloomberg skips Stripe: that fact was already seeded above, so re-extracting it
        # from bloomberg.json would create a bloomberg→bloomberg supersession and make the
        # "1 conflict resolved" count non-deterministic.
        bloomberg_task = asyncio.create_task(run_analyst(
            agent_id="bloomberg",
            source_file=str(SOURCES_DIR / "bloomberg.json"),
            subject_id=SUBJECT_ID,
            llm_api_key=llm_key,
            llm_model=llm_model,
            statewave_url=sw_url,
            statewave_api_key=sw_key,
            on_log=on_log,
            on_memory_update=on_memory_update,
            skip_competitors={"Stripe"},
            compile_lock=compile_lock,
        ))
        tc_task = asyncio.create_task(run_analyst(
            agent_id="techcrunch",
            source_file=str(SOURCES_DIR / "techcrunch.json"),
            subject_id=SUBJECT_ID,
            llm_api_key=llm_key,
            llm_model=llm_model,
            statewave_url=sw_url,
            statewave_api_key=sw_key,
            on_log=on_log,
            on_memory_update=on_memory_update,
            compile_lock=compile_lock,
        ))
        ea_task = asyncio.create_task(run_analyst(
            agent_id="earnings",
            source_file=str(SOURCES_DIR / "earnings.json"),
            subject_id=SUBJECT_ID,
            llm_api_key=llm_key,
            llm_model=llm_model,
            statewave_url=sw_url,
            statewave_api_key=sw_key,
            on_log=on_log,
            on_memory_update=on_memory_update,
            compile_lock=compile_lock,
        ))
        results = await asyncio.gather(bloomberg_task, tc_task, ea_task, return_exceptions=True)

        total_supersessions = 0
        for (agent_id, _), result in zip(_AGENTS, results):
            if isinstance(result, Exception):
                await broadcast({"type": "agent_log", "agent": agent_id,
                                 "msg": f"ERROR: {result}"})
            elif isinstance(result, dict):
                total_supersessions += result.get("supersessions", 0)

        await broadcast({"type": "agents_done", "supersessions": total_supersessions})

    asyncio.create_task(_run())
    return {"status": "started"}


@app.post("/ask", dependencies=[Security(_require_auth)])
async def ask(body: dict):
    """Synthesis: recall from Statewave + stream LLM answer via SSE."""
    question = (body.get("question") or "").strip()
    if not question:
        return {"error": "no question"}

    await broadcast({"type": "synthesis_start", "question": question})

    async with _sw() as sw:
        try:
            ctx = await sw.get_context(SUBJECT_ID, task=question, max_tokens=4000)
        except StatewaveError as e:
            await broadcast({"type": "synthesis_error", "msg": str(e)})
            return {"error": str(e)}

    assembled = ctx.get("assembled_context", "")
    facts = ctx.get("facts", [])
    token_est = ctx.get("token_estimate", 0)

    if not assembled.strip():
        await broadcast({"type": "synthesis_error",
                         "msg": "No memories found. Run agents first."})
        return {"error": "no memories"}

    await broadcast({"type": "synthesis_context",
                     "fact_count": len(facts), "token_estimate": token_est})

    llm_key = os.environ.get("LLM_API_KEY", "")
    llm_model = os.environ.get("LLM_MODEL", "groq/llama-3.3-70b-versatile")
    try:
        stream = await litellm.acompletion(
            model=llm_model,
            api_key=llm_key,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a market intelligence analyst. Answer using ONLY the provided "
                        "memory context — facts compiled and conflict-resolved by Statewave. "
                        "Do not invent facts. Cite which source (bloomberg, techcrunch, earnings) "
                        "each claim comes from when known. Be concise: 3-5 sentences."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\n"
                        f"Memory context (active facts, conflicts resolved):\n{assembled}"
                    ),
                },
            ],
            temperature=0.2,
            stream=True,
            timeout=60.0,
        )
        async for chunk in stream:
            token = chunk.choices[0].delta.content
            if token:
                await broadcast({"type": "synthesis_token", "token": token})
        await broadcast({"type": "synthesis_done"})
    except Exception as e:
        await broadcast({"type": "synthesis_error", "msg": str(e)})

    return {"status": "ok"}


@app.get("/memories", dependencies=[Security(_require_auth)])
async def get_memories():
    """Return current active memories for the subject."""
    async with _sw() as sw:
        try:
            memories = await sw.search_memories(SUBJECT_ID)
            return {"memories": memories}
        except StatewaveError:
            return {"memories": []}


# ── Markup stripping ──────────────────────────────────────────────────────────

_MARKUP_RE = re.compile(r"\[/?[a-zA-Z #0-9_]+\]")

def _strip_markup(text: str) -> str:
    return _MARKUP_RE.sub("", text)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
