"""
statewave_tools.py — Drop this file into any project to add Statewave memory.

Three functions cover the full loop:
  remember()  — commit a finding to shared memory
  compile()   — detect conflicts and supersede stale facts
  recall()    — retrieve ranked, conflict-resolved context for a prompt

Works standalone; no other files from this repo are required.
Requires: statewave  (pip install statewave)

Quick start:
    from statewave_tools import configure, remember, compile, recall

    configure("http://localhost:8100")          # point at your Statewave instance
    remember("my-subject", "agent-a", "Stripe charges 2.9% + 30¢ per card transaction.")
    compile("my-subject")
    context = recall("my-subject", "What does Stripe charge?")
    # → pass context directly into your LLM prompt
"""
from __future__ import annotations

from statewave import StatewaveClient

_SW_URL: str = "http://localhost:8100"
_SW_KEY: str | None = None


def configure(url: str, api_key: str | None = None) -> None:
    """Set the Statewave server URL and optional API key for all subsequent calls."""
    global _SW_URL, _SW_KEY
    _SW_URL = url.rstrip("/")
    _SW_KEY = api_key


def _client() -> StatewaveClient:
    return StatewaveClient(_SW_URL, api_key=_SW_KEY)


def remember(
    subject_id: str,
    source: str,
    text: str,
    event_type: str = "agent.findings",
) -> dict:
    """Commit a finding as a raw episode to the shared Statewave subject.

    Call compile() after ingesting to make it retrievable via recall().

    Args:
        subject_id: The shared namespace (e.g. "market-intel", "my-project").
        source:     Which agent or system produced this finding (e.g. "agent-a").
        text:       The finding in natural language.
        event_type: Episode type label (default "agent.findings").

    Returns:
        The episode object returned by Statewave.
    """
    with _client() as sw:
        episode = sw.create_episode(subject_id, source, event_type, {"text": text})
    return episode.model_dump(mode="json")


def compile(subject_id: str) -> dict:
    """Run Statewave's conflict detector against all uncompiled episodes.

    Memories that exceed the Jaccard similarity threshold are automatically
    superseded — the older one is marked stale with a provenance link.
    This is idempotent; calling it multiple times is safe. It waits for the
    compile to finish so the results are retrievable via recall() right away.

    Args:
        subject_id: The shared namespace to compile.

    Returns:
        The compile result object from Statewave.
    """
    with _client() as sw:
        result = sw.compile_memories_wait(subject_id)
    return result.model_dump(mode="json")


def recall(subject_id: str, question: str, max_tokens: int = 2000) -> str:
    """Retrieve ranked, conflict-resolved context ready to drop into a prompt.

    Only active (non-superseded) memories are returned. The result is
    token-bounded to max_tokens and ranked by relevance to question.

    Args:
        subject_id: The shared namespace to query.
        question:   The task or question driving the context retrieval.
        max_tokens: Maximum tokens in the returned context bundle.

    Returns:
        assembled_context string — pass this directly to your LLM.
    """
    with _client() as sw:
        ctx = sw.get_context(subject_id, question, max_tokens=max_tokens)
    return ctx.assembled_context
