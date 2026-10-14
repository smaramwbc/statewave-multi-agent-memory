from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import server
from agents.base import StatewaveError

client = TestClient(server.app)


class StubStatewaveClient:
    def __init__(self, *, context=None, error=None):
        self.context = context
        self.error = error

    async def get_context(self, subject_id, *, task, max_tokens):
        if self.error:
            raise self.error
        return self.context


def install_statewave_stub(monkeypatch, *, context=None, error=None):
    @asynccontextmanager
    async def stub_client():
        yield StubStatewaveClient(context=context, error=error)

    monkeypatch.setattr(server, "_sw", stub_client)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": 42},
    ],
)
def test_ask_rejects_invalid_questions(payload):
    response = client.post("/ask", json=payload)

    assert response.status_code == 422


def test_ask_returns_bad_gateway_when_statewave_fails(monkeypatch):
    install_statewave_stub(monkeypatch, error=StatewaveError("Statewave unavailable"))

    response = client.post("/ask", json={"question": "What changed?"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Statewave unavailable"}


def test_ask_returns_conflict_when_no_memories_exist(monkeypatch):
    install_statewave_stub(
        monkeypatch,
        context={"assembled_context": "", "facts": [], "token_estimate": 0},
    )

    response = client.post("/ask", json={"question": "What changed?"})

    assert response.status_code == 409
    assert response.json() == {"detail": "No memories found. Run agents first."}
