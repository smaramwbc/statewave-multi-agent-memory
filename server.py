"""
Statewave Multi-Agent Memory — FastAPI + SSE backend.

Run with:
    python server.py
Then open http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import litellm

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from statewave import StatewaveConnectionError

from agents.analyst import run_analyst
from agents.candidates import build_competitor_candidates
from agents.base import AsyncStatewaveClient, StatewaveError

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SUBJECT_ID = os.environ.get("SUBJECT_ID", "market-intel")
SOURCES_DIR = Path(__file__).parent / "sources"

_DEFAULT_SYNTHESIS_PROMPT = (
    "You are a market intelligence analyst. Answer using ONLY the provided "
    "memory context — facts compiled and conflict-resolved by Statewave. "
    "Do not invent facts. Cite which source (bloomberg, techcrunch, earnings) "
    "each claim comes from when known. Be concise: 3-5 sentences."
)
_SYNTHESIS_PROMPT = os.environ.get("SYNTHESIS_SYSTEM_PROMPT", _DEFAULT_SYNTHESIS_PROMPT)
_DEMO_SEED = os.environ.get("DEMO_SEED_BLOOMBERG_STRIPE", "true").lower() == "true"


def _discover_agents(sources_dir: Path) -> list[tuple[str, str]]:
    """Return [(agent_id, filename)] for every JSON file in sources_dir, sorted."""
    return [(p.stem, p.name) for p in sorted(sources_dir.glob("*.json"))]


@asynccontextmanager
async def lifespan(app: FastAPI):
    llm_key = os.environ.get("LLM_API_KEY", "")
    if not llm_key:
        raise RuntimeError(
            "LLM_API_KEY is not set. Copy .env.example to .env and add your API key."
        )
    sw_url = os.environ.get("STATEWAVE_URL", "http://localhost:8100")
    sw_key = os.environ.get("STATEWAVE_API_KEY")
    try:
        # Lightweight reachability probe: get_timeline returns {} on a 404
        # (server up), and raises StatewaveConnectionError only when the
        # backend can't be reached.
        async with AsyncStatewaveClient(sw_url, sw_key) as sw:
            await sw.get_timeline("healthcheck")
    except StatewaveConnectionError:
        logger.warning("Statewave not reachable at %s — start it before running agents.", sw_url)
    yield

_APP_SECRET = os.environ.get("APP_SECRET", "")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_auth(key: str | None = Security(_api_key_header)) -> None:
    if not _APP_SECRET:
        return  # auth disabled when APP_SECRET is unset (local dev)
    if key != _APP_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

# Per-connection SSE queues: session_id -> asyncio.Queue
_queues: dict[str, asyncio.Queue] = {}

app = FastAPI(title="Statewave Multi-Agent Demo", lifespan=lifespan)
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
    """Reset Statewave subject and launch analyst agents for each source file."""
    async with _sw() as sw:
        try:
            await sw.delete_subject(SUBJECT_ID)
            await broadcast({"type": "status", "msg": "Reset: prior subject cleared"})
        except StatewaveError:
            await broadcast({"type": "status", "msg": "Starting fresh (no prior data)"})

    agents = _discover_agents(SOURCES_DIR)
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

        # Demo mode: pre-seed Bloomberg's stale Stripe pricing so TechCrunch's
        # 2.9% fact will supersede it — the core conflict resolution demo moment.
        # Disable by setting DEMO_SEED_BLOOMBERG_STRIPE=false in .env when using
        # your own source files.
        if _DEMO_SEED and (SOURCES_DIR / "bloomberg.json").exists():
            try:
                await _seed_bloomberg_stripe()
            except StatewaveConnectionError as exc:
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

        # Bloomberg skips Stripe only in demo mode (fact already seeded above).
        # All other agents run without skipping.
        tasks = []
        for agent_id, source_filename in agents:
            skip = {"Stripe"} if (_DEMO_SEED and agent_id == "bloomberg") else None
            tasks.append(asyncio.create_task(run_analyst(
                agent_id=agent_id,
                source_file=str(SOURCES_DIR / source_filename),
                subject_id=SUBJECT_ID,
                llm_api_key=llm_key,
                llm_model=llm_model,
                statewave_url=sw_url,
                statewave_api_key=sw_key,
                on_log=on_log,
                on_memory_update=on_memory_update,
                skip_competitors=skip,
                compile_lock=compile_lock,
            )))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_supersessions = 0
        for (agent_id, _), result in zip(agents, results):
            if isinstance(result, Exception):
                logger.error("Agent %s failed: %s", agent_id, result)
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
                {"role": "system", "content": _SYNTHESIS_PROMPT},
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
            content = chunk.choices[0].delta.content
            if content:
                await broadcast({"type": "synthesis_token", "token": content})
        await broadcast({"type": "synthesis_done"})
    except (litellm.APIError, litellm.AuthenticationError, litellm.RateLimitError) as e:
        logger.error("LLM error during synthesis: %s", e)
        await broadcast({"type": "synthesis_error", "msg": str(e)})
    except Exception as e:
        logger.error("Unexpected synthesis error: %s", e)
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
