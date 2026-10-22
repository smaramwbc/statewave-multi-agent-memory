"""Exercise the server's auth dependency without starting agents or services."""

import asyncio
import hmac
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

import server


@pytest.mark.parametrize("key", [None, "", "wrong-secret", "s\u00e9cret"])
def test_auth_rejects_missing_or_wrong_key(monkeypatch, key):
    monkeypatch.setattr(server, "_APP_SECRET", "test-secret")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server._require_auth(key))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or missing X-API-Key"


@pytest.mark.parametrize("secret", ["test-secret", "s\u00e9cret"])
def test_auth_accepts_correct_key(monkeypatch, secret):
    monkeypatch.setattr(server, "_APP_SECRET", secret)

    assert asyncio.run(server._require_auth(secret)) is None


@pytest.mark.parametrize("key", [None, "", "any-key"])
def test_auth_is_disabled_without_secret(monkeypatch, key):
    monkeypatch.setattr(server, "_APP_SECRET", "")

    assert asyncio.run(server._require_auth(key)) is None


def test_auth_uses_constant_time_comparison_result(monkeypatch):
    monkeypatch.setattr(server, "_APP_SECRET", "test-secret")
    compare = Mock(return_value=False)
    monkeypatch.setattr(hmac, "compare_digest", compare)

    # Matching strings must still be denied when the secure comparator rejects.
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server._require_auth("test-secret"))

    assert exc_info.value.status_code == 401
    compare.assert_called_once_with(b"test-secret", b"test-secret")
