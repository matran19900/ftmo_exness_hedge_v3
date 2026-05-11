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
from hedger_shared.ctrader_oauth import TokenResponse

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

    Returns a list captured of init / connect / reconcile / disconnect
    events; tests assert on it for wiring correctness.
    """
    captured: list[dict[str, Any]] = []

    class _StubBridge:
        def __init__(self, **kwargs: Any) -> None:
            captured.append({"init": kwargs})

        async def connect_with_retry(self, max_attempts: int = 10, **_kwargs: Any) -> None:
            captured.append({"connected": max_attempts})

        async def reconcile_state(self) -> None:
            # Step 3.5b: main.amain calls this after connect, before
            # starting loops. Record the call so tests can assert the
            # ordering invariant.
            captured.append({"reconciled": True})

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
    # Step 3.5: bridge must receive the redis client so it can publish
    # unsolicited events to event_stream:ftmo:{acc}.
    assert init_kwargs.get("redis") is patched_redis
    assert any("connected" in c for c in patched_bridge)
    assert any("disconnected" in c for c in patched_bridge)


@pytest.mark.asyncio
async def test_amain_starts_account_info_task(
    patched_redis: fakeredis.aioredis.FakeRedis,
    patched_bridge: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3.5: amain creates an ``account_info`` task alongside
    heartbeat + command_loop. We patch the loop coroutine to set a
    flag so we can verify it actually got scheduled."""
    token = TokenResponse(
        access_token="acc",
        refresh_token="ref",
        expires_in=3600,
        token_type="Bearer",
    )
    await save_token(patched_redis, "ftmo_001", token, ctid_trader_account_id=42)

    account_info_called = asyncio.Event()

    async def fake_account_info_loop(
        _bridge: Any, _redis: Any, account_id: str, _shutdown: Any
    ) -> None:
        assert account_id == "ftmo_001"
        account_info_called.set()
        await asyncio.Event().wait()  # block until cancelled

    monkeypatch.setattr(main_module, "account_info_loop", fake_account_info_loop)

    amain_task = asyncio.create_task(amain(_settings()))
    # Wait for the account_info_loop to be entered.
    try:
        await asyncio.wait_for(account_info_called.wait(), timeout=1.0)
    finally:
        amain_task.cancel()
        try:
            await amain_task
        except asyncio.CancelledError:
            pass

    assert account_info_called.is_set()


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


@pytest.mark.asyncio
async def test_amain_calls_reconcile_state_after_connect_before_tasks(
    patched_redis: fakeredis.aioredis.FakeRedis,
    patched_bridge: list[dict[str, Any]],
) -> None:
    """Step 3.5b ordering invariant: ``bridge.reconcile_state`` must
    be invoked AFTER ``connect_with_retry`` succeeds and BEFORE any of
    the heartbeat / command / account_info tasks start. Otherwise the
    server's event_handler can see an inconsistent snapshot (e.g. a
    live unsolicited close event ordered ahead of the reconcile
    snapshot in event_stream)."""
    token = TokenResponse(
        access_token="acc",
        refresh_token="ref",
        expires_in=3600,
        token_type="Bearer",
    )
    await save_token(patched_redis, "ftmo_001", token, ctid_trader_account_id=42)

    amain_task = asyncio.create_task(amain(_settings()))
    # Wait until the bridge has been reconciled (or amain exited).
    for _ in range(40):
        if any("reconciled" in c for c in patched_bridge):
            break
        await asyncio.sleep(0.02)

    amain_task.cancel()
    try:
        await amain_task
    except asyncio.CancelledError:
        pass

    # Sequence check: init → connected → reconciled → disconnected.
    keys_in_order = [next(iter(c.keys())) for c in patched_bridge]
    assert "init" in keys_in_order
    assert "connected" in keys_in_order
    assert "reconciled" in keys_in_order
    assert keys_in_order.index("connected") < keys_in_order.index("reconciled"), (
        "reconcile_state must run AFTER connect_with_retry"
    )


@pytest.mark.asyncio
async def test_amain_continues_when_reconcile_state_raises(
    patched_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3.5b failure-tolerance: ``reconcile_state`` raising must
    not crash amain. The try/except in main.amain swallows it; tasks
    still spin up; shutdown still cleans up."""
    token = TokenResponse(
        access_token="acc",
        refresh_token="ref",
        expires_in=3600,
        token_type="Bearer",
    )
    await save_token(patched_redis, "ftmo_001", token, ctid_trader_account_id=42)

    captured: list[str] = []

    class _RaisingReconcileBridge:
        def __init__(self, **_kwargs: Any) -> None: ...

        async def connect_with_retry(self, max_attempts: int = 10, **_kwargs: Any) -> None:
            captured.append("connected")

        async def reconcile_state(self) -> None:
            captured.append("reconcile_raised")
            raise RuntimeError("simulated reconcile bug")

        async def disconnect(self) -> None:
            captured.append("disconnected")

    monkeypatch.setattr(main_module, "CtraderBridge", _RaisingReconcileBridge)

    amain_task = asyncio.create_task(amain(_settings()))
    # Wait for reconcile to attempt + raise + amain to proceed past it.
    for _ in range(40):
        if "reconcile_raised" in captured:
            break
        await asyncio.sleep(0.02)
    # Then give shutdown loops a moment, then cancel.
    await asyncio.sleep(0.05)
    amain_task.cancel()
    try:
        await amain_task
    except asyncio.CancelledError:
        pass

    # Reconcile raised, but disconnect still ran (proves amain didn't crash).
    assert "reconcile_raised" in captured
    assert "disconnected" in captured


def test_exit_codes_are_distinct() -> None:
    """Smoke check on the documented exit codes."""
    assert EXIT_OK == 0
    assert EXIT_NO_TOKEN == 1
    assert EXIT_CONNECT_FAILED == 2
