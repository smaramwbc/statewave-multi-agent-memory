"""Unit tests for the memory diff logic in AsyncStatewaveClient.get_memory_diff."""
import pytest


def _apply_diff(all_memories: list[dict], before_ids: set[str]) -> dict:
    """Pure extraction of the diff logic from AsyncStatewaveClient.get_memory_diff."""
    new, superseded, unchanged = [], [], []
    for mem in all_memories:
        mem_id = mem.get("id", "")
        status = mem.get("status", "active")
        if status == "active":
            if mem_id not in before_ids:
                new.append(mem)
            else:
                unchanged.append(mem)
        elif status == "superseded" and mem_id in before_ids:
            superseded.append(mem)
    return {"new": new, "superseded": superseded, "unchanged": unchanged}


def _mem(id: str, status: str = "active") -> dict:
    return {"id": id, "status": status, "text": f"memory {id}"}


def test_new_memory_detected():
    before = {"a"}
    after = [_mem("a"), _mem("b")]
    diff = _apply_diff(after, before)
    assert [m["id"] for m in diff["new"]] == ["b"]
    assert [m["id"] for m in diff["unchanged"]] == ["a"]
    assert diff["superseded"] == []


def test_superseded_memory_detected():
    before = {"a", "b"}
    after = [_mem("a"), _mem("b", "superseded")]
    diff = _apply_diff(after, before)
    assert [m["id"] for m in diff["superseded"]] == ["b"]
    assert [m["id"] for m in diff["unchanged"]] == ["a"]
    assert diff["new"] == []


def test_superseded_memory_not_in_before_ignored():
    # A memory that was already superseded before our snapshot shouldn't surface.
    before = {"a"}
    after = [_mem("a"), _mem("x", "superseded")]
    diff = _apply_diff(after, before)
    assert diff["superseded"] == []
    assert diff["new"] == []


def test_empty_before_all_new():
    before: set[str] = set()
    after = [_mem("a"), _mem("b")]
    diff = _apply_diff(after, before)
    assert {m["id"] for m in diff["new"]} == {"a", "b"}
    assert diff["superseded"] == []
    assert diff["unchanged"] == []


def test_empty_after():
    before = {"a"}
    diff = _apply_diff([], before)
    assert diff == {"new": [], "superseded": [], "unchanged": []}


def test_conflict_resolution_scenario():
    # Simulate: bloomberg committed 'stripe-pricing', then techcrunch superseded it.
    before = {"stripe-pricing-bloomberg"}
    after = [
        _mem("stripe-pricing-bloomberg", "superseded"),
        _mem("stripe-pricing-techcrunch", "active"),
    ]
    diff = _apply_diff(after, before)
    assert [m["id"] for m in diff["superseded"]] == ["stripe-pricing-bloomberg"]
    assert [m["id"] for m in diff["new"]] == ["stripe-pricing-techcrunch"]
