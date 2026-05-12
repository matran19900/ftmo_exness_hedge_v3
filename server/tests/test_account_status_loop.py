"""Tests for ``app.services.account_status.account_status_loop`` (step 3.12).

Mirrors the ``test_position_tracker.py`` loop-testing idiom: a real
``RedisService`` over fakeredis + a capturing ``BroadcastService``
stand-in, no FastAPI app, no real network. The loop's shutdown
contract is cancellation-based (``Task.cancel`` → ``CancelledError``),
matching the other Phase 3 background loops.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from app.services.account_status import (
    ACCOUNTS_CHANNEL,
    account_status_loop,
)
from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


class _CapturingBroadcast(BroadcastService):
    def __init__(self) -> None:
        super().__init__(redis_svc=None)
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        self.published.append((channel, data))


@pytest.fixture
def broadcast() -> _CapturingBroadcast:
    return _CapturingBroadcast()


async def _seed_ftmo_account_online(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    account_id: str = "ftmo_001",
) -> None:
    await redis_svc.add_account("ftmo", account_id, name="t1")
    await redis_client.set(f"client:ftmo:{account_id}", "1", ex=30)


# ---------- publish to "accounts" channel ----------


@pytest.mark.asyncio
async def test_loop_publishes_to_accounts_channel(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """One cycle (50 ms interval, ~120 ms wait) produces at least one
    publish on the ``accounts`` channel — pins the channel name + that
    the loop actually fires within its cadence."""
    await _seed_ftmo_account_online(redis_svc, redis_client)

    task = asyncio.create_task(account_status_loop(redis_svc, broadcast, interval_seconds=0.05))
    await asyncio.sleep(0.12)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(broadcast.published) >= 1
    channels = {ch for ch, _ in broadcast.published}
    assert channels == {ACCOUNTS_CHANNEL}


# ---------- payload shape ----------


@pytest.mark.asyncio
async def test_loop_payload_shape(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Each publish's data has ``type=account_status``, an int ``ts``,
    and an ``accounts`` list whose entries match the shape produced
    by ``get_all_accounts_with_status``."""
    await _seed_ftmo_account_online(redis_svc, redis_client)

    task = asyncio.create_task(account_status_loop(redis_svc, broadcast, interval_seconds=0.05))
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert broadcast.published
    _, data = broadcast.published[0]
    assert data["type"] == "account_status"
    assert isinstance(data["ts"], int)
    assert data["ts"] > 0
    assert isinstance(data["accounts"], list)
    assert len(data["accounts"]) == 1
    entry = data["accounts"][0]
    assert entry["broker"] == "ftmo"
    assert entry["account_id"] == "ftmo_001"
    assert entry["status"] == "online"


# ---------- resilience ----------


@pytest.mark.asyncio
async def test_loop_survives_cycle_exception(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One cycle raises → loop logs and continues. The next cycle
    succeeds. A transient Redis error must not take down account-status
    broadcasting for the whole process."""
    await _seed_ftmo_account_online(redis_svc, redis_client)

    call_count = 0
    real = redis_svc.get_all_accounts_with_status

    async def flaky() -> list[dict[str, str]]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated transient redis failure")
        return await real()

    monkeypatch.setattr(redis_svc, "get_all_accounts_with_status", flaky)

    task = asyncio.create_task(account_status_loop(redis_svc, broadcast, interval_seconds=0.03))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # First cycle raised → no publish for it.
    # Subsequent cycle(s) succeeded → at least one publish.
    assert call_count >= 2
    assert len(broadcast.published) >= 1


# ---------- shutdown ----------


@pytest.mark.asyncio
async def test_loop_exits_on_cancel(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """``Task.cancel`` propagates ``CancelledError`` out of the loop —
    the lifespan ``finally`` block relies on this to coalesce shutdown."""
    task = asyncio.create_task(account_status_loop(redis_svc, broadcast, interval_seconds=0.05))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------- step 3.13a: broadcast payload shape (typed entries) ----------


@pytest.mark.asyncio
async def test_broadcast_payload_enabled_is_boolean(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 3.13a: each per-account entry in the broadcast must ship
    ``enabled`` as a Python bool (``True``/``False``), not the
    HASH-string literal ``"true"``/``"false"``. Pre-3.13a the WS
    payload passed raw rows through, and the frontend's
    ``Boolean("false") === true`` evaluation made disabled accounts
    render as enabled."""
    await _seed_ftmo_account_online(redis_svc, redis_client)

    task = asyncio.create_task(account_status_loop(redis_svc, broadcast, interval_seconds=0.05))
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert broadcast.published
    _, data = broadcast.published[0]
    entry = data["accounts"][0]
    assert entry["enabled"] is True  # not "true"


@pytest.mark.asyncio
async def test_broadcast_payload_disabled_account(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Disabled account → ``enabled: False`` AND ``status: "disabled"``
    in the WS payload. The two fields together ensure the frontend
    can distinguish an operator-paused account from a crashed FTMO
    client (step 3.13a OrderForm tooltip needs this)."""
    await redis_svc.add_account("ftmo", "ftmo_001", name="paused", enabled=False)
    # Heartbeat still alive — but operator override beats it.
    await redis_client.set("client:ftmo:ftmo_001", "1", ex=30)

    task = asyncio.create_task(account_status_loop(redis_svc, broadcast, interval_seconds=0.05))
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert broadcast.published
    _, data = broadcast.published[0]
    entry = data["accounts"][0]
    assert entry["enabled"] is False
    assert entry["status"] == "disabled"
