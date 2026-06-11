"""Deterministic structured memory-candidate construction.

The analyst agents already hold their facts in machine-readable form (the source
JSON). Rather than ask an LLM to re-derive structure, we build Statewave
structured memory candidates DIRECTLY from that source data — separate atomic
facts (pricing, positioning, differentiators) with authoritative claim fields
(entity, qualifiers, value) taken verbatim from the source's ``claim`` block.

The LLM is never asked to invent entity identity, claim keys, qualifiers,
cardinality, normalized numeric values, or effective dates. It is still free to
produce the human-readable synthesis (the chat answer).
"""

from __future__ import annotations

from typing import Any


def build_competitor_candidates(
    competitor: dict, source_label: str, published: str
) -> tuple[str, list[dict]]:
    """Return ``(raw_episode_text, candidates)`` for one competitor.

    Emits up to three independent atomic candidates so a stale pricing fact can
    be superseded without touching positioning/differentiator facts. Only the
    pricing candidate carries a claim, and only when the source supplies a
    machine-readable ``claim`` block (otherwise it is an unkeyed fact).
    """
    name = competitor.get("name", "unknown")
    pricing = competitor.get("pricing_model", "")
    positioning = competitor.get("market_positioning", "")
    diffs = competitor.get("key_differentiators", []) or []
    confidence = competitor.get("confidence_notes", "")
    claim = competitor.get("claim")  # authoritative structured fields, from source

    candidates: list[dict] = []

    pricing_candidate: dict[str, Any] = {
        "kind": "domain_fact",
        "text": f"{name} pricing ({source_label}, {published}): {pricing}",
        "metadata": {"source": source_label, "competitor": name, "fact": "pricing"},
    }
    if isinstance(claim, dict):
        # Deterministic, authoritative — copied verbatim from the source data.
        pricing_candidate["claim"] = claim
    candidates.append(pricing_candidate)

    if positioning:
        candidates.append({
            "kind": "domain_fact",
            "text": f"{name} market positioning ({source_label}): {positioning}",
            "metadata": {"source": source_label, "competitor": name, "fact": "positioning"},
        })

    if diffs:
        candidates.append({
            "kind": "domain_fact",
            "text": f"{name} key differentiators ({source_label}): {', '.join(diffs)}",
            "metadata": {"source": source_label, "competitor": name, "fact": "differentiators"},
        })

    raw_text = (
        f"{source_label} ({published}) on {name} — pricing: {pricing}; "
        f"positioning: {positioning}; differentiators: {', '.join(diffs)}."
        + (f" Confidence: {confidence}." if confidence else "")
    )
    return raw_text, candidates
