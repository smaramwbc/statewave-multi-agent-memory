"""The README's audit-inspector example must match what the inspector prints.

The README used to show a "SUPERSESSIONS" section with Jaccard similarity
scores and a `superseded_by` link. None of that exists: Statewave records a
supersession as `status: superseded` plus a `valid_to` boundary on the losing
memory, and the inspector never computes a similarity score. These tests keep
the documented example tied to the inspector source, so a future claim in the
README has to be backed by code that actually emits it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
INSPECTOR_SRC = ROOT / "inspector" / "src" / "index.ts"

# Section labels the inspector prints for memories. Agent blocks are labelled
# with the uppercased episode source, so they are checked separately.
MEMORY_SECTIONS = {"ACTIVE MEMORIES", "SUPERSEDED MEMORIES"}


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _inspector_source() -> str:
    return INSPECTOR_SRC.read_text(encoding="utf-8")


def _example_block() -> str:
    """The fenced code block that follows the 'Example output' line."""
    text = _readme()
    marker = text.index("Example output after a demo run")
    fence_open = text.index("```", marker)
    body_start = text.index("\n", fence_open) + 1
    fence_close = text.index("```", body_start)
    block = text[body_start:fence_close]
    assert block.strip(), "the audit-inspector example block is empty"
    return block


def _source_labels() -> set[str]:
    return {p.stem.upper() for p in (ROOT / "sources").glob("*.json")}


def test_inspector_still_prints_the_documented_sections():
    src = _inspector_source()
    for label in MEMORY_SECTIONS:
        assert label in src, f"README documents a {label} section the inspector no longer prints"


def test_example_block_invents_no_sections():
    allowed = MEMORY_SECTIONS | _source_labels()
    headers = re.findall(r"^── ([A-Z ]+?)\s{2}\(", _example_block(), flags=re.MULTILINE)
    assert headers, "the example block shows no inspector sections at all"
    unknown = [h for h in headers if h not in allowed]
    assert not unknown, f"example shows sections the inspector never prints: {unknown}"


def test_example_block_shows_no_scores_the_inspector_cannot_compute():
    src = _inspector_source().lower()
    block = _example_block().lower()
    for term in ("jaccard", "similarity", "superseded_by"):
        if term not in src:
            assert term not in block, (
                f"the example shows {term!r}, but the inspector never produces it"
            )
