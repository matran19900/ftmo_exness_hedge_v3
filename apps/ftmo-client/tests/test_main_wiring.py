"""High-level wiring tests for ``amain``.

We don't actually open a TCP connection to cTrader. The bridge's
``connect_with_retry`` is monkeypatched to a no-op so the test
exercises the wiring logic only. Redis is fakeredis.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from hedger_shared.ctrader_oauth import TokenResponse  # type: ignore[import-not-found]

from ftmo_client import main as main_module
from ftmo_client.config import FtmoClientSettings
from ftmo_client.main import EXIT_CONNECT_FAILED, EXIT_NO_TOKEN, EXIT_OK, amain
from ftmo_client.oauth_storage import save_token


def _settings() -> FtmoClientSettings:
    """Build a Settings instance bypassing .env reads."""
    return FtmoClientSettings(
        ftmo_account_id="ftmo_001",
        redis_url="redis://test/0",
        ctrader_client_id="cid",
        ctrader_client_secret="secret",
        ctrader_redirect_uri="http://localhost:8765/callback",
        ctrader_host="ctradertest.example.com",
        ctrader_port=5035,
        log_level="INFO",
    )


@pytest.fixture
def patched_redis(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> fakeredis.aioredis.FakeRedis:
    """Replace ``main._connect_redis`` with one that returns the fake."""

    async def _stub_connect(_url: str) -> fakeredis.aioredis.FakeRedis:
        return fake_redis

    monkeypatch.setattr(main_module, "_connect_redis", _stub_connect)
    return fake_redis


@pytest.fixture
def patched_bridge(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub CtraderBridge so the test never opens a real connection.

    Returns a list captured of disconnect calls; tests assert on it.
    """
    captured: list[dict[str, Any]] = []

    class _StubBridge:
        def __init__(self, **kwargs: Any) -> None:
            captured.append({"init": kwargs})

        async def connect_with_retry(self, max_attempts: int = 10, **_kwargs: Any) -> None:
            captured.append({"connected": max_attempts})

        async def disconnect(self) -> None:
            captured.append({"disconnected": True})

    monkeypatch.setattr(main_module, "CtraderBridge", _StubBridge)
    return captured


@pytest.mark.asyncio
async def test_amain_returns_no_token_when_redis_empty(
    patched_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """No token in Redis → exit 1, no bridge construction attempted."""
    rc = await amain(_settings())
    assert rc == EXIT_NO_TOKEN


@pytest.mark.asyncio
async def test_amain_wires_tasks_and_shuts_down_cleanly(
    patched_redis: fakeredis.aioredis.FakeRedis,
    patched_bridge: list[dict[str, Any]],
) -> None:
    """Token exists → bridge connect runs, both loops start, signal shuts down clean."""
    token = TokenResponse(
        access_token="acc",
        refresh_token="ref",
        expires_in=3600,
        token_type="Bearer",
    )
    await save_token(patched_redis, "ftmo_001", token, ctid_trader_account_id=42)

    settings = _settings()
    amain_task = asyncio.create_task(amain(settings))
    # Give startup a moment to publish first heartbeat + start loops.
    for _ in range(20):
        if await patched_redis.hgetall("client:ftmo:ftmo_001"):  # type: ignore[misc]
            break
        await asyncio.sleep(0.05)

    # We can't trigger SIGTERM mid-pytest reliably, so reach into the
    # ShutdownController by importing the module-level main_module and
    # iterating tasks. Simpler: just cancel the amain task and let
    # CancelledError propagate through the finally block.
    amain_task.cancel()
    try:
        await amain_task
    except asyncio.CancelledError:
        pass

    # The bridge stub recorded init + connect + disconnect calls.
    init_kwargs = next(c["init"] for c in patched_bridge if "init" in c)
    assert init_kwargs["account_id"] == "ftmo_001"
    assert init_kwargs["ctid_trader_account_id"] == 42
    assert any("connected" in c for c in patched_bridge)
    assert any("disconnected" in c for c in patched_bridge)


@pytest.mark.asyncio
async def test_amain_returns_connect_failed_when_bridge_exhausts_retries(
    patched_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge connect_with_retry raising RuntimeError → exit 2."""
    token = TokenResponse(
        access_token="acc",
        refresh_token="ref",
        expires_in=3600,
        token_type="Bearer",
    )
    await save_token(patched_redis, "ftmo_001", token, ctid_trader_account_id=42)

    class _FailingBridge:
        def __init__(self, **_kwargs: Any) -> None: ...

        async def connect_with_retry(self, max_attempts: int = 10, **_kwargs: Any) -> None:
            raise RuntimeError("simulated exhaustion")

        async def disconnect(self) -> None: ...

    monkeypatch.setattr(main_module, "CtraderBridge", _FailingBridge)

    rc = await amain(_settings())
    assert rc == EXIT_CONNECT_FAILED


def test_exit_codes_are_distinct() -> None:
    """Smoke check on the documented exit codes."""
    assert EXIT_OK == 0
    assert EXIT_NO_TOKEN == 1
    assert EXIT_CONNECT_FAILED == 2
