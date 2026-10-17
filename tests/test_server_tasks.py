"""Tests for the background task registry and SSE broadcast (issue #498).

Handlers are exercised directly, so the app lifespan never runs and no request
leaves the process.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server


def test_spawn_keeps_a_reference_until_the_task_finishes() -> None:
    async def scenario() -> tuple[bool, bool]:
        started = asyncio.Event()
        release = asyncio.Event()

        async def work() -> None:
            started.set()
            await release.wait()

        task = server._spawn(work())
        await started.wait()
        held = task in server._background_tasks
        release.set()
        await task
        await asyncio.sleep(0)
        return held, task in server._background_tasks

    held, still_held = asyncio.run(scenario())

    assert held is True
    assert still_held is False


def test_broadcast_delivers_to_every_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> list[dict[str, Any]]:
        q1: asyncio.Queue = asyncio.Queue()
        q2: asyncio.Queue = asyncio.Queue()
        monkeypatch.setitem(server._queues, "a", q1)
        monkeypatch.setitem(server._queues, "b", q2)

        await server.broadcast({"type": "agent_log", "agent": "x", "msg": "hi"})

        return [await q1.get(), await q2.get()]

    events = asyncio.run(scenario())

    assert [event["type"] for event in events] == ["agent_log", "agent_log"]
