"""Tests for statewave_tools.recall caller identity (issue #404) — no network."""
from __future__ import annotations

import types

import pytest

import statewave_tools


class _StubClient:
    """Stand-in for statewave.StatewaveClient that records the call."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def get_context(self, subject_id: str, question: str, **kwargs: object):
        self.kwargs = {"subject_id": subject_id, "question": question, **kwargs}
        return types.SimpleNamespace(assembled_context="bundle")

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_recall_sends_a_default_caller_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient()
    monkeypatch.setattr(statewave_tools, "_client", lambda: stub)

    result = statewave_tools.recall("subject-1", "what changed?")

    assert result == "bundle"
    assert stub.kwargs is not None
    assert stub.kwargs["caller_id"] == "analyst"
    assert stub.kwargs["caller_type"] == "agent"
    assert stub.kwargs["max_tokens"] == 2000


def test_recall_forwards_an_explicit_caller_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient()
    monkeypatch.setattr(statewave_tools, "_client", lambda: stub)

    statewave_tools.recall("subject-1", "what changed?", max_tokens=50,
                           caller_id="svc-42", caller_type="worker")

    assert stub.kwargs is not None
    assert stub.kwargs["caller_id"] == "svc-42"
    assert stub.kwargs["caller_type"] == "worker"
    assert stub.kwargs["max_tokens"] == 50
