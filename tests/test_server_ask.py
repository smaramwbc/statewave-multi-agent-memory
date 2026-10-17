"""Tests for POST /ask (issue #496) — no network calls.

The Statewave client is stubbed and the handler is called directly, so the app
lifespan never runs and no request leaves the process.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import server
from agents.base import StatewaveError


class _StubClient:
    """Minimal stand-in for agents.base.AsyncStatewaveClient."""

    def __init__(self, bundle: dict | None = None, error: Exception | None = None):
        self._bundle = bundle or {}
        self._error = error

    async def get_context(self, subject_id: str, task: str, max_tokens: int = 3000) -> dict:
        if self._error is not None:
            raise self._error
        return self._bundle

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _install(monkeypatch: pytest.MonkeyPatch, client: _StubClient) -> None:
    monkeypatch.setattr(server, "_sw", lambda: client)


def test_ask_trims_the_question() -> None:
    assert server.AskRequest(question="  what changed?  ").question == "what changed?"


def test_ask_rejects_a_blank_question() -> None:
    with pytest.raises(ValidationError):
        server.AskRequest(question="   ")


def test_ask_rejects_a_non_string_question() -> None:
    with pytest.raises(ValidationError):
        server.AskRequest(question=123)


def test_ask_returns_409_when_there_are_no_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _StubClient(
        bundle={"assembled_context": "", "facts": [], "token_estimate": 0},
    ))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.ask(server.AskRequest(question="hi")))

    assert exc_info.value.status_code == 409


def test_ask_returns_502_on_a_statewave_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _StubClient(error=StatewaveError("boom")))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.ask(server.AskRequest(question="hi")))

    assert exc_info.value.status_code == 502
