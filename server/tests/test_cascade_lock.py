"""Step 4.8 — cascade_lock Lua + RedisService helpers.

Atomic single-trigger guard for the cascade close orchestrator. Tests
cover the SET-NX semantics, TTL auto-release, the audit value, and
multi-order independence.
"""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


@pytest.mark.asyncio
async def test_acquire_first_call_returns_true(svc: RedisService) -> None:
    """First caller wins the lock."""
    acquired = await svc.acquire_cascade_lock("ord_xyz", "A")
    assert acquired is True


@pytest.mark.asyncio
async def test_acquire_second_call_returns_false(svc: RedisService) -> None:
    """A second caller for the same order observes the existing lock."""
    first = await svc.acquire_cascade_lock("ord_xyz", "A")
    second = await svc.acquire_cascade_lock("ord_xyz", "D")
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_release_allows_re_acquire(svc: RedisService) -> None:
    await svc.acquire_cascade_lock("ord_xyz", "A")
    await svc.release_cascade_lock("ord_xyz")
    re_acquired = await svc.acquire_cascade_lock("ord_xyz", "B")
    assert re_acquired is True


@pytest.mark.asyncio
async def test_lock_value_records_trigger_path(
    svc: RedisService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """The lock's value is the trigger_path tag so a post-mortem reveals
    which path won."""
    await svc.acquire_cascade_lock("ord_audit", "D")
    val = await redis_client.get("cascade_lock:ord_audit")
    assert val == "D"
    audit = await svc.read_cascade_lock("ord_audit")
    assert audit == "D"


@pytest.mark.asyncio
async def test_lock_ttl_set_on_acquire(
    svc: RedisService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """TTL safeguard — auto-release after the configured window if the
    caller crashes."""
    await svc.acquire_cascade_lock("ord_ttl", "A", ttl_seconds=30)
    ttl = await redis_client.ttl("cascade_lock:ord_ttl")
    assert 25 <= ttl <= 30


@pytest.mark.asyncio
async def test_locks_independent_across_orders(svc: RedisService) -> None:
    """Two different orders can hold cascade locks simultaneously."""
    a = await svc.acquire_cascade_lock("ord_A", "A")
    b = await svc.acquire_cascade_lock("ord_B", "B")
    assert a is True
    assert b is True
    # Each can still release independently.
    await svc.release_cascade_lock("ord_A")
    audit_a = await svc.read_cascade_lock("ord_A")
    audit_b = await svc.read_cascade_lock("ord_B")
    assert audit_a is None
    assert audit_b == "B"


@pytest.mark.asyncio
async def test_release_on_missing_lock_no_error(svc: RedisService) -> None:
    """Idempotent release — releasing a non-existent lock is a no-op."""
    await svc.release_cascade_lock("ord_never_locked")
    # Subsequent acquire still succeeds.
    acquired = await svc.acquire_cascade_lock("ord_never_locked", "A")
    assert acquired is True


@pytest.mark.asyncio
async def test_concurrent_acquire_single_winner(svc: RedisService) -> None:
    """Two coroutines race for the same lock; the Lua SET-NX guarantees
    exactly one winner."""
    results = await asyncio.gather(
        svc.acquire_cascade_lock("ord_race", "A"),
        svc.acquire_cascade_lock("ord_race", "B"),
    )
    winners = [r for r in results if r is True]
    losers = [r for r in results if r is False]
    assert len(winners) == 1
    assert len(losers) == 1
