"""Tests for ``BroadcastService.publish_tick`` delta-coalescing (step 3.11b).

Phase 2's ``_handle_spot_event`` translates cTrader's partial
``ProtoOASpotEvent`` into a dict with ``bid=None`` or ``ask=None``
when the corresponding ``HasField`` returns false. Step 3.11b
merges those partials with the last cached full tick inside
``publish_tick`` so downstream consumers (order_service,
position_tracker, frontend) always see a complete tick.

Tests pin:
  - Full delta fast path (no cache read).
  - Partial delta + valid prev cache → merged result.
  - Initial state (partial + no prev) → drop publish + drop cache write.
  - Defensive handling of malformed cache JSON + redis read exceptions.
  - ``ts`` field semantics (delta wins; ``time.time()`` fallback).
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService

# ---------- fixtures ----------


@pytest.fixture
def fake_redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(fake_redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(fake_redis_client)


@pytest.fixture
def bs_with_redis(redis_svc: RedisService) -> BroadcastService:
    return BroadcastService(redis_svc=redis_svc)


# ---------- _coalesce_tick: fast path ----------


@pytest.mark.asyncio
async def test_coalesce_full_delta_returns_input_unchanged(
    bs_with_redis: BroadcastService,
) -> None:
    """Full delta (bid AND ask present) is the fast path — return the
    input unchanged without consulting the cache."""
    delta: dict[str, Any] = {
        "type": "tick",
        "symbol": "EURUSD",
        "bid": 1.17,
        "ask": 1.18,
        "ts": 1000,
    }
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result is delta  # identity — fast path doesn't allocate


@pytest.mark.asyncio
async def test_coalesce_full_delta_skips_cache_read(
    redis_svc: RedisService,
) -> None:
    """Verify the fast path doesn't call ``get_tick_cache``. We swap
    the underlying redis client for a MagicMock that fails any read,
    then confirm the full-delta path doesn't trip it."""
    bs = BroadcastService(redis_svc=redis_svc)

    # Track whether get_tick_cache was called by replacing it
    # with a tripwire mock.
    tripwire = AsyncMock(side_effect=AssertionError("cache must not be read"))
    redis_svc.get_tick_cache = tripwire  # type: ignore[method-assign]

    delta: dict[str, Any] = {"bid": 1.17, "ask": 1.18, "ts": 1000}
    result = await bs._coalesce_tick("EURUSD", delta)
    assert result is delta
    tripwire.assert_not_called()


# ---------- _coalesce_tick: partial deltas with prev cache ----------


@pytest.mark.asyncio
async def test_coalesce_bid_only_merges_with_cached_ask(
    bs_with_redis: BroadcastService,
    redis_svc: RedisService,
) -> None:
    """``bid`` updated, ``ask`` stays the same: merged result has
    delta's bid + prev cache's ask + delta's ts."""
    await redis_svc.set_tick_cache(
        "EURUSD",
        json.dumps({"type": "tick", "symbol": "EURUSD", "bid": 1.17, "ask": 1.18, "ts": 900}),
    )
    delta: dict[str, Any] = {"bid": 1.171, "ask": None, "ts": 1000}
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result == {
        "type": "tick",
        "symbol": "EURUSD",
        "bid": 1.171,
        "ask": 1.18,
        "ts": 1000,
    }


@pytest.mark.asyncio
async def test_coalesce_ask_only_merges_with_cached_bid(
    bs_with_redis: BroadcastService,
    redis_svc: RedisService,
) -> None:
    """Symmetric: ``ask`` updated, ``bid`` stays."""
    await redis_svc.set_tick_cache(
        "EURUSD",
        json.dumps({"type": "tick", "symbol": "EURUSD", "bid": 1.17, "ask": 1.18, "ts": 900}),
    )
    delta: dict[str, Any] = {"bid": None, "ask": 1.181, "ts": 1000}
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result == {
        "type": "tick",
        "symbol": "EURUSD",
        "bid": 1.17,
        "ask": 1.181,
        "ts": 1000,
    }


@pytest.mark.asyncio
async def test_coalesce_result_is_new_dict_not_input_mutation(
    bs_with_redis: BroadcastService,
    redis_svc: RedisService,
) -> None:
    """Partial-coalesce must return a NEW dict — never mutate the
    delta or the cached prev."""
    prev = {"type": "tick", "symbol": "EURUSD", "bid": 1.17, "ask": 1.18, "ts": 900}
    await redis_svc.set_tick_cache("EURUSD", json.dumps(prev))
    delta: dict[str, Any] = {"bid": 1.171, "ask": None, "ts": 1000}
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result is not delta
    assert result is not prev
    # Inputs unmutated.
    assert delta == {"bid": 1.171, "ask": None, "ts": 1000}


# ---------- _coalesce_tick: initial-state drop ----------


@pytest.mark.asyncio
async def test_coalesce_initial_state_bid_only_no_prev_returns_none(
    bs_with_redis: BroadcastService,
) -> None:
    """First spot event after startup: only bid, no cache. Returns
    None so publish_tick drops without poisoning the cache."""
    delta: dict[str, Any] = {"bid": 1.17, "ask": None, "ts": 1000}
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result is None


@pytest.mark.asyncio
async def test_coalesce_initial_state_ask_only_no_prev_returns_none(
    bs_with_redis: BroadcastService,
) -> None:
    delta: dict[str, Any] = {"bid": None, "ask": 1.18, "ts": 1000}
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result is None


@pytest.mark.asyncio
async def test_coalesce_initial_state_both_none_returns_none(
    bs_with_redis: BroadcastService,
) -> None:
    """Degenerate: both sides None on the delta + no prev → drop."""
    delta: dict[str, Any] = {"bid": None, "ask": None, "ts": 1000}
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result is None


@pytest.mark.asyncio
async def test_coalesce_partial_with_prev_also_partial_returns_none(
    bs_with_redis: BroadcastService,
    redis_svc: RedisService,
) -> None:
    """Defensive: if the cache somehow contains a half-tick (e.g.
    from a pre-3.11b run), and the new delta doesn't complete the
    missing side, the coalesce returns None rather than emit a
    half-tick."""
    await redis_svc.set_tick_cache(
        "EURUSD",
        json.dumps({"type": "tick", "symbol": "EURUSD", "bid": 1.17, "ask": None, "ts": 900}),
    )
    delta: dict[str, Any] = {"bid": 1.171, "ask": None, "ts": 1000}
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result is None


# ---------- _coalesce_tick: cache failure modes ----------


@pytest.mark.asyncio
async def test_coalesce_malformed_prev_json_returns_none_when_delta_partial(
    bs_with_redis: BroadcastService,
    fake_redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """Malformed JSON in the cache → treat as no prev → drop the
    partial delta to avoid emitting bad data."""
    await fake_redis_client.set("tick:EURUSD", '{"bid":}')  # invalid JSON
    delta: dict[str, Any] = {"bid": 1.17, "ask": None, "ts": 1000}
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result is None


@pytest.mark.asyncio
async def test_coalesce_cache_read_raises_returns_none_when_partial(
    redis_svc: RedisService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Redis read exception → log + treat as no prev → drop publish."""
    bs = BroadcastService(redis_svc=redis_svc)

    async def boom(_sym: str) -> str | None:
        raise RuntimeError("simulated redis outage")

    redis_svc.get_tick_cache = boom  # type: ignore[method-assign,assignment]

    delta: dict[str, Any] = {"bid": 1.17, "ask": None, "ts": 1000}
    with caplog.at_level("ERROR"):
        result = await bs._coalesce_tick("EURUSD", delta)
    assert result is None
    assert "tick cache read failed" in caplog.text


@pytest.mark.asyncio
async def test_coalesce_full_delta_with_broken_cache_still_works(
    redis_svc: RedisService,
) -> None:
    """Fast path doesn't touch the cache, so a broken cache read
    doesn't impact the full-delta case. ``publish_tick``'s cache
    WRITE may still fail later but that's a separate concern (and
    is already logged + swallowed by publish_tick's own try/except)."""
    bs = BroadcastService(redis_svc=redis_svc)

    async def boom(_sym: str) -> str | None:
        raise RuntimeError("simulated redis outage")

    redis_svc.get_tick_cache = boom  # type: ignore[method-assign,assignment]

    delta: dict[str, Any] = {"bid": 1.17, "ask": 1.18, "ts": 1000}
    result = await bs._coalesce_tick("EURUSD", delta)
    assert result is delta  # fast path, no exception


# ---------- _coalesce_tick: ts handling ----------


@pytest.mark.asyncio
async def test_coalesce_delta_ts_preserved_on_merge(
    bs_with_redis: BroadcastService,
    redis_svc: RedisService,
) -> None:
    """Merged tick uses delta's ts (newer), NOT prev cache's ts."""
    await redis_svc.set_tick_cache(
        "EURUSD",
        json.dumps({"bid": 1.17, "ask": 1.18, "ts": 900}),
    )
    delta: dict[str, Any] = {"bid": 1.171, "ask": None, "ts": 1000}
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    assert result is not None
    assert result["ts"] == 1000


@pytest.mark.asyncio
async def test_coalesce_missing_delta_ts_fallback_to_wall_clock(
    bs_with_redis: BroadcastService,
    redis_svc: RedisService,
) -> None:
    """Defensive: if the spot-event handler omits ``ts``, fall back
    to ``time.time() * 1000``. Sanity-check the result is within a
    few seconds of ``now``."""
    await redis_svc.set_tick_cache(
        "EURUSD",
        json.dumps({"bid": 1.17, "ask": 1.18, "ts": 900}),
    )
    delta: dict[str, Any] = {"bid": 1.171, "ask": None}  # no ts
    before_ms = int(time.time() * 1000)
    result = await bs_with_redis._coalesce_tick("EURUSD", delta)
    after_ms = int(time.time() * 1000)
    assert result is not None
    assert before_ms <= result["ts"] <= after_ms + 10


# ---------- publish_tick: end-to-end with coalesce ----------


@pytest.mark.asyncio
async def test_publish_tick_full_delta_caches_and_broadcasts(
    bs_with_redis: BroadcastService,
    redis_svc: RedisService,
) -> None:
    """Full delta → cache write + broadcast (no subscribers in this
    test, so broadcast is a no-op but the cache write side effect
    is observable)."""
    delta: dict[str, Any] = {
        "type": "tick",
        "symbol": "EURUSD",
        "bid": 1.17,
        "ask": 1.18,
        "ts": 1000,
    }
    await bs_with_redis.publish_tick("EURUSD", delta)
    cached = await redis_svc.get_tick_cache("EURUSD")
    assert cached is not None
    decoded = json.loads(cached)
    assert decoded["bid"] == 1.17
    assert decoded["ask"] == 1.18


@pytest.mark.asyncio
async def test_publish_tick_partial_delta_with_prev_merges_and_caches(
    bs_with_redis: BroadcastService,
    redis_svc: RedisService,
) -> None:
    """Partial delta + cached prev → cache holds the COALESCED full
    tick (not the raw partial)."""
    await redis_svc.set_tick_cache(
        "EURUSD",
        json.dumps({"type": "tick", "symbol": "EURUSD", "bid": 1.17, "ask": 1.18, "ts": 900}),
    )
    await bs_with_redis.publish_tick("EURUSD", {"bid": 1.171, "ask": None, "ts": 1000})
    cached = await redis_svc.get_tick_cache("EURUSD")
    assert cached is not None
    decoded = json.loads(cached)
    assert decoded["bid"] == 1.171
    assert decoded["ask"] == 1.18  # preserved from prev
    assert decoded["ts"] == 1000


@pytest.mark.asyncio
async def test_publish_tick_initial_partial_drops_no_cache_no_broadcast(
    redis_svc: RedisService,
    fake_redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """Partial delta + no prev cache → cache write and broadcast
    are BOTH skipped. We assert by wrapping ``set_tick_cache``
    + ``publish`` to count calls."""
    bs = BroadcastService(redis_svc=redis_svc)
    set_cache_spy = AsyncMock(wraps=redis_svc.set_tick_cache)
    redis_svc.set_tick_cache = set_cache_spy  # type: ignore[method-assign]
    publish_spy = AsyncMock(wraps=bs.publish)
    bs.publish = publish_spy  # type: ignore[method-assign]

    await bs.publish_tick("EURUSD", {"bid": 1.17, "ask": None, "ts": 1000})

    set_cache_spy.assert_not_called()
    publish_spy.assert_not_called()
    # No key written.
    assert await fake_redis_client.get("tick:EURUSD") is None


@pytest.mark.asyncio
async def test_publish_tick_no_redis_svc_still_handles_full_delta(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BroadcastService can be instantiated without a redis_svc (e.g.
    in unit tests). Full deltas should still broadcast — only the
    cache write is skipped. Partial deltas with no cache MUST drop
    (same as the initial-state path)."""
    bs = BroadcastService(redis_svc=None)
    # Full delta: should publish (no subscribers → no-op broadcast).
    await bs.publish_tick("EURUSD", {"bid": 1.17, "ask": 1.18, "ts": 1000})
    # Partial delta: should drop silently (no cache to merge against).
    await bs.publish_tick("EURUSD", {"bid": 1.171, "ask": None, "ts": 1001})
    # No exceptions raised, no error logs.
    assert "tick cache" not in caplog.text


@pytest.mark.asyncio
async def test_publish_tick_full_delta_with_failing_cache_still_broadcasts(
    fake_redis_client: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the cache WRITE fails, the broadcast still happens. The
    write exception is logged + swallowed (existing Phase 2 behavior;
    step 3.11b preserves it for the post-coalesce write)."""
    redis_svc = RedisService(fake_redis_client)
    bs = BroadcastService(redis_svc=redis_svc)
    redis_svc.set_tick_cache = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("simulated SETEX failure")
    )
    publish_spy = AsyncMock()
    bs.publish = publish_spy  # type: ignore[method-assign]

    with caplog.at_level("ERROR"):
        await bs.publish_tick(
            "EURUSD",
            {"type": "tick", "symbol": "EURUSD", "bid": 1.17, "ask": 1.18, "ts": 1000},
        )

    assert "tick cache write failed" in caplog.text
    # Broadcast still happens despite the cache failure.
    publish_spy.assert_awaited_once()


# ---------- regression: existing test_broadcast_publish_tick_caches_to_redis still works ----------


@pytest.mark.asyncio
async def test_regression_existing_full_tick_cache_path(
    bs_with_redis: BroadcastService,
    redis_svc: RedisService,
) -> None:
    """The original step-2.3 test ``test_broadcast_publish_tick_caches_to_redis``
    sends a full tick. Pin that same flow here in the dedicated
    coalesce test file so regressions can't slip through."""
    tick: dict[str, Any] = {"type": "tick", "bid": 1.05, "ask": 1.0501, "ts": 1}
    await bs_with_redis.publish_tick("EURUSD", tick)
    cached = await redis_svc.get_tick_cache("EURUSD")
    assert cached is not None
    assert "1.05" in cached
    assert "1.0501" in cached


# ---------- mock-free unit-style smoke (no redis at all) ----------


@pytest.mark.asyncio
async def test_coalesce_no_redis_svc_partial_returns_none() -> None:
    """A BroadcastService without redis can never coalesce — every
    partial delta drops."""
    bs = BroadcastService(redis_svc=None)
    result = await bs._coalesce_tick("EURUSD", {"bid": 1.17, "ask": None})
    assert result is None


@pytest.mark.asyncio
async def test_coalesce_no_redis_svc_full_returns_input() -> None:
    """The fast path doesn't depend on redis."""
    bs = BroadcastService(redis_svc=None)
    delta: dict[str, Any] = {"bid": 1.17, "ask": 1.18, "ts": 1}
    assert await bs._coalesce_tick("EURUSD", delta) is delta


# ---------- sanity: MagicMock fixture not needed; sanity on imports ----------


def test_broadcast_service_constructible_without_redis() -> None:
    """Trivial: pins the no-redis constructor remains valid (it's
    used by the unit-style tests above)."""
    bs = BroadcastService(redis_svc=None)
    assert isinstance(bs, BroadcastService)


def test_magicmock_import_smoke() -> None:
    """Imports check — MagicMock is unused at module scope but
    available for future tests that need a richer mock."""
    m = MagicMock()
    m.foo = 42
    assert m.foo == 42
