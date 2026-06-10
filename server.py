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

import httpx
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from groq import AsyncGroq
from sse_starlette.sse import EventSourceResponse

from agents.analyst import run_analyst
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
        except (StatewaveError, Exception):
            await broadcast({"type": "status", "msg": "Starting fresh (no prior data)"})

    groq_key = os.environ.get("LLM_API_KEY", "")
    sw_url = os.environ.get("STATEWAVE_URL", "http://localhost:8100")
    sw_key = os.environ.get("STATEWAVE_API_KEY")

    def on_log(agent_id: str, msg: str) -> None:
        # Strip Rich markup tags for web display
        clean = _strip_markup(msg)
        asyncio.create_task(broadcast({"type": "agent_log", "agent": agent_id, "msg": clean}))

    def on_memory_update(agent_id: str, diff: dict) -> None:
        asyncio.create_task(broadcast({"type": "memory_update", "agent": agent_id, "diff": diff}))

    async def _seed_bloomberg_stripe() -> None:
        """Commit Bloomberg's stale Stripe pricing directly — no LLM call needed.
        This guarantees TechCrunch's correction will always have something to supersede,
        regardless of Bloomberg's LLM response timing."""
        prose = (
            "Stripe pricing: 3.5% plus 35 cents per transaction for card payments, "
            "raised from 2.9% effective May 1 amid margin pressure. "
            "Market positioning: Premium pricing signals Stripe is moving upmarket "
            "toward high-volume enterprise merchants away from SMB developers. "
            "Key differentiators: Developer APIs, Global coverage, Instant payouts, Radar fraud tooling. "
            "Source: bloomberg, published 2026-05-16. "
            "Confidence: Bloomberg Intelligence estimates the increase adds roughly "
            "140 million dollars in annualized revenue at current transaction volumes."
        )
        async with AsyncStatewaveClient(sw_url, sw_key) as sw:
            before_ids: set[str] = {m["id"] for m in await sw.search_memories(SUBJECT_ID)}
            await sw.post_episode(
                subject_id=SUBJECT_ID,
                source="bloomberg",
                type="agent.analyst.findings",
                payload={
                    "text": prose,
                    "competitor": "Stripe",
                    "pricing_model": "3.5% plus 35 cents per transaction",
                    "source_label": "bloomberg",
                    "published": "2026-05-16",
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

        # All 3 agents run concurrently — no stagger needed since seed is already committed
        bloomberg_task = asyncio.create_task(run_analyst(
            agent_id="bloomberg",
            source_file=str(SOURCES_DIR / "bloomberg.json"),
            subject_id=SUBJECT_ID,
            groq_api_key=groq_key,
            statewave_url=sw_url,
            statewave_api_key=sw_key,
            on_log=on_log,
            on_memory_update=on_memory_update,
        ))
        tc_task = asyncio.create_task(run_analyst(
            agent_id="techcrunch",
            source_file=str(SOURCES_DIR / "techcrunch.json"),
            subject_id=SUBJECT_ID,
            groq_api_key=groq_key,
            statewave_url=sw_url,
            statewave_api_key=sw_key,
            on_log=on_log,
            on_memory_update=on_memory_update,
        ))
        ea_task = asyncio.create_task(run_analyst(
            agent_id="earnings",
            source_file=str(SOURCES_DIR / "earnings.json"),
            subject_id=SUBJECT_ID,
            groq_api_key=groq_key,
            statewave_url=sw_url,
            statewave_api_key=sw_key,
            on_log=on_log,
            on_memory_update=on_memory_update,
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

    groq = AsyncGroq(api_key=os.environ.get("LLM_API_KEY", ""), timeout=httpx.Timeout(60.0, connect=10.0))
    try:
        stream = await groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
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
        )
        async for chunk in stream:
            token = chunk.choices[0].delta.content
            if token:
                await broadcast({"type": "synthesis_token", "token": token})
        await broadcast({"type": "synthesis_done"})
    except Exception as e:
        await broadcast({"type": "synthesis_error", "msg": str(e)})
    finally:
        await groq.close()

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

import re

_MARKUP_RE = re.compile(r"\[/?[a-zA-Z #0-9_]+\]")

def _strip_markup(text: str) -> str:
    return _MARKUP_RE.sub("", text)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
