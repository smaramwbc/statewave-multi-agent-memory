from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import httpx
from groq import AsyncGroq

from agents.base import AsyncStatewaveClient, StatewaveError

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
    groq_api_key: str,
    statewave_url: str,
    statewave_api_key: str | None,
    on_log: Callable[[str, str], None],
    on_memory_update: Callable[[str, dict], None],
) -> dict:
    on_log(agent_id, f"Starting — source: [bold]{Path(source_file).name}[/bold]")

    source_data = json.loads(Path(source_file).read_text(encoding="utf-8"))
    source_label = source_data.get("source", agent_id)
    published = source_data.get("published", "unknown date")
    competitors = source_data.get("competitors", [])

    on_log(agent_id, f"Loaded {len(competitors)} competitors from [dim]{source_label}[/dim] ({published})")

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

        # Call LLM to extract findings
        on_log(agent_id, "Calling LLM to extract findings...")
        groq = AsyncGroq(api_key=groq_api_key, timeout=httpx.Timeout(60.0, connect=10.0))
        try:
            user_msg = (
                f"Source document ({source_label}, {published}):\n"
                f"{json.dumps(source_data, indent=2)}\n\n"
                f"Existing memory context (from other agents — may be empty):\n"
                f"{assembled.strip() if assembled.strip() else '(no memories yet)'}\n\n"
                "Extract findings for each competitor in this source document."
            )
            resp = await groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            raw = resp.choices[0].message.content or "{}"
            parsed = json.loads(raw)
            findings = parsed.get("findings", [])
            if not findings and isinstance(parsed, list):
                findings = parsed
        except Exception as e:
            on_log(agent_id, f"[red]LLM error: {e}[/red]")
            await groq.close()
            return {"episodes": 0, "supersessions": 0, "error": str(e)}
        finally:
            await groq.close()

        on_log(agent_id, f"LLM returned [green]{len(findings)} findings[/green]")

        # Commit each finding to Statewave
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            competitor = finding.get("competitor", "unknown")
            pricing = finding.get("pricing_model", "")
            positioning = finding.get("market_positioning", "")
            differentiators = finding.get("key_differentiators", [])
            confidence = finding.get("confidence_notes", "")
            prose_summary = finding.get("prose_summary", "")

            # Build prose text for Statewave's heuristic compiler
            prose = (
                f"{competitor} pricing: {pricing}. "
                f"Market positioning: {positioning}. "
                f"Key differentiators: {', '.join(differentiators)}. "
                f"Source: {source_label}, published {published}. "
                f"Confidence: {confidence}. "
                f"{prose_summary}"
            ).strip()

            on_log(agent_id, f"Committing [bold]{competitor}[/bold]...")

            # Snapshot before commit
            before_ids = {m["id"] for m in await sw.search_memories(subject_id)}

            try:
                await sw.post_episode(
                    subject_id=subject_id,
                    source=agent_id,
                    type="agent.analyst.findings",
                    payload={
                        "text": prose,
                        "competitor": competitor,
                        "pricing_model": pricing,
                        "market_positioning": positioning,
                        "key_differentiators": differentiators,
                        "confidence_notes": confidence,
                        "source_label": source_label,
                        "published": published,
                    },
                )
                await sw.compile_memories(subject_id)
                diff = await sw.get_memory_diff(subject_id, before_ids)
            except StatewaveError as e:
                on_log(agent_id, f"[red]Statewave error for {competitor}: {e}[/red]")
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
