from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Callable

from agents.base import AsyncStatewaveClient, StatewaveError
from agents.candidates import build_competitor_candidates

SUBJECT_ID = "market-intel"

_SYSTEM_PROMPT = """\
You are a competitive intelligence analyst. You have been given a source document about payment processors.
Extract findings for each competitor mentioned. You also have access to existing memory context from other
agents — use it to understand what is already known, but your job is to faithfully extract what YOUR source says.

Return a JSON object with key "findings" containing an array. Each item must have:
  - competitor: string (company name)
  - pricing_model: string (full sentence describing current pricing)
  - market_positioning: string (full sentence)
  - key_differentiators: array of strings
  - confidence_notes: string (source and date)
  - prose_summary: string (2-3 natural sentences combining all facts — this is stored as memory text)
"""


async def run_analyst(
    agent_id: str,
    source_file: str,
    subject_id: str,
    llm_api_key: str,
    llm_model: str,
    statewave_url: str,
    statewave_api_key: str | None,
    on_log: Callable[[str, str], None],
    on_memory_update: Callable[[str, dict], None],
    skip_competitors: set[str] | None = None,
    compile_lock: asyncio.Lock | None = None,
) -> dict:
    on_log(agent_id, f"Starting — source: [bold]{Path(source_file).name}[/bold]")

    source_data = json.loads(Path(source_file).read_text(encoding="utf-8"))
    source_label = source_data.get("source", agent_id)
    published = source_data.get("published", "unknown date")
    competitors = source_data.get("competitors", [])

    on_log(agent_id, f"Loaded {len(competitors)} competitors from [dim]{source_label}[/dim] ({published})")

    if skip_competitors:
        competitors = [c for c in competitors if c.get("name", "") not in skip_competitors]
        # The LLM prompt is built from source_data, not the `competitors` list, so
        # filtering the list alone has no effect — mirror the filter into source_data
        # so the skipped competitor never reaches the model.
        source_data = {**source_data, "competitors": competitors}
        on_log(agent_id, f"Skipping pre-seeded competitors: {', '.join(skip_competitors)}")

    episodes_written = 0
    supersessions_total = 0

    async with AsyncStatewaveClient(statewave_url, statewave_api_key) as sw:
        # Recall existing memory context
        on_log(agent_id, "Recalling existing memory context...")
        try:
            ctx = await sw.get_context(
                subject_id,
                task=f"Competitive intelligence about payment processors — source: {source_label}",
                max_tokens=3000,
            )
            assembled = ctx.get("assembled_context", "")
            facts = ctx.get("facts", [])
            fact_count = len(facts)
            if assembled.strip():
                on_log(agent_id, f"Recalled [green]{fact_count} existing facts[/green] from memory:")
                for f in facts:
                    text = f.get("content") or f.get("text") or ""
                    src = f.get("source", "")
                    if text:
                        snippet = text.replace("\n", " ").strip()[:120]
                        on_log(agent_id, f"  [dim]← {src}: {snippet}…[/dim]")
            else:
                on_log(agent_id, "No prior memories — starting fresh")
        except StatewaveError:
            assembled = ""
            on_log(agent_id, "No prior memories — starting fresh")

        # Build structured candidates DETERMINISTICALLY from the source data —
        # no LLM extraction. Each competitor becomes one episode carrying
        # separate atomic candidates (pricing / positioning / differentiators);
        # the pricing candidate carries the authoritative v2 claim verbatim from
        # the source. Statewave compiles them into atomic memories and resolves
        # the Stripe pricing contradiction by entity-qualified claim identity.
        on_log(agent_id, f"Building structured candidates for [green]{len(competitors)} competitors[/green]...")
        for competitor in competitors:
            competitor_name = competitor.get("name", "unknown")
            # Defense-in-depth: skip pre-seeded competitors so we never double-
            # commit (e.g. a bloomberg→bloomberg supersession of the seed).
            if skip_competitors and competitor_name in skip_competitors:
                continue

            raw_text, candidates = build_competitor_candidates(
                competitor, source_label, published
            )
            keyed = sum(1 for c in candidates if "claim" in c)
            on_log(
                agent_id,
                f"Committing [bold]{competitor_name}[/bold] "
                f"({len(candidates)} atomic facts, {keyed} structured claim)...",
            )

            # Serialize post→compile→diff across the concurrent agents. Each
            # compile_memories() call compiles ALL of the subject's uncompiled
            # episodes, so without this lock two agents racing on the same
            # uncompiled episode would double-compile it (duplicate memories and
            # a spurious dedup supersession). The snapshot/diff is also atomic.
            lock_cm = compile_lock if compile_lock is not None else contextlib.nullcontext()
            async with lock_cm:
                before_ids = {m["id"] for m in await sw.search_memories(subject_id)}
                try:
                    await sw.post_episode(
                        subject_id=subject_id,
                        source=agent_id,
                        type="agent.analyst.findings",
                        payload={
                            "text": raw_text,
                            "statewave": {"memory_candidates": candidates},
                            "competitor": competitor_name,
                            "source_label": source_label,
                            "published": published,
                        },
                    )
                    await sw.compile_memories(subject_id)
                    diff = await sw.get_memory_diff(subject_id, before_ids)
                except StatewaveError as e:
                    on_log(agent_id, f"[red]Statewave error for {competitor_name}: {e}[/red]")
                    continue

            episodes_written += 1
            new_count = len(diff["new"])
            sup_count = len(diff["superseded"])
            supersessions_total += sup_count

            on_log(agent_id, f"  [green]+{new_count} new[/green]  [red]-{sup_count} superseded[/red]")
            if sup_count > 0:
                on_log(agent_id, f"  [yellow bold]⚠ Conflict resolved — {sup_count} older memory superseded[/yellow bold]")

            on_memory_update(agent_id, diff)

    on_log(agent_id, f"[green bold]Done.[/green bold] {episodes_written} episodes, {supersessions_total} supersessions.")
    return {"episodes": episodes_written, "supersessions": supersessions_total}
