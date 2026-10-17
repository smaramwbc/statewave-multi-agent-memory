"""Tests for POST /run concurrency handling (issue #497) — no network calls.

The Statewave client is stubbed and the handler is called directly, so the app
lifespan never runs and no request leaves the process.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import server


class _StubClient:
    """Minimal stand-in for agents.base.AsyncStatewaveClient."""

    def __init__(self) -> None:
        self.deleted = False

    async def delete_subject(self, subject_id: str) -> None:
        self.deleted = True

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def test_run_returns_409_while_a_run_is_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_run_in_progress", True)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.run_agents())

    assert exc_info.value.status_code == 409


def test_run_claims_the_flag_before_any_await(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second call is rejected until the first run's task finishes."""
    monkeypatch.setattr(server, "_run_in_progress", False)
    monkeypatch.setattr(server, "_discover_agents", lambda _directory: [])
    monkeypatch.setattr(server, "_DEMO_SEED", False)
    monkeypatch.setattr(server, "_sw", _StubClient)

    async def scenario() -> None:
        await server.run_agents()
        assert server._run_in_progress is True

        with pytest.raises(HTTPException) as exc_info:
            await server.run_agents()
        assert exc_info.value.status_code == 409

        for _ in range(50):
            if not server._run_in_progress:
                break
            await asyncio.sleep(0)

        assert server._run_in_progress is False

    asyncio.run(scenario())


def test_clear_run_flag_releases_the_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_run_in_progress", True)

    server._clear_run_flag(None)  # type: ignore[arg-type]

    assert server._run_in_progress is False
