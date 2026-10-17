"""Tests for GET /memories (issue #495) — no network calls.

The Statewave client is stubbed and the handler is called directly, so the app
lifespan never runs and no request leaves the process.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import server
from agents.base import StatewaveError


class _StubClient:
    """Minimal stand-in for agents.base.AsyncStatewaveClient."""

    def __init__(self, memories: list[dict] | None = None, error: Exception | None = None):
        self._memories = memories or []
        self._error = error

    async def search_memories(self, subject_id: str) -> list[dict]:
        if self._error is not None:
            raise self._error
        return self._memories

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _install(monkeypatch: pytest.MonkeyPatch, client: _StubClient) -> None:
    monkeypatch.setattr(server, "_sw", lambda: client)


def test_memories_returns_only_active(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _StubClient(memories=[
        {"id": "old", "status": "superseded", "content": "Stripe 3.5%"},
        {"id": "new", "status": "active", "content": "Stripe 2.9%"},
        {"id": "legacy", "content": "no status field means active"},
    ]))

    result = asyncio.run(server.get_memories())

    assert [m["id"] for m in result["memories"]] == ["new", "legacy"]


def test_memories_surfaces_statewave_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _StubClient(error=StatewaveError("unauthorized")))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_memories())

    assert exc_info.value.status_code == 502
