"""Step 4.7a — response_handler.py Exness broker dispatch tests.

Covers:
  - action=open status=filled -> s_status=filled + composed status=filled
    (CAS-guarded transition from primary_filled).
  - action=open status=rejected -> s_status=rejected + s_error_msg.
  - action=open status=error / requote -> same as rejected.
  - Unknown order_id (TTL expired side index) -> warning log, no crash.
  - request_id -> order_id lookup integration.
  - secondary_filled WS broadcast.
  - Side-index ``s_broker_order_id_to_order`` populated on fill.
  - Composed status transition primary_filled -> filled atomic.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService
from app.services.response_handler import _handle_response_entry


class _CapturingBroadcast(BroadcastService):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        self.published.append((channel, message))


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


@pytest.fixture
def broadcast() -> _CapturingBroadcast:
    return _CapturingBroadcast()


async def _seed_primary_filled_hedge(
    redis_svc: RedisService,
    *,
    order_id: str = "ord_hedge_1",
    request_id: str = "req_secondary_1",
) -> None:
    """Seed a hedge order in the primary_filled transient state."""
    await redis_svc.create_order(
        order_id,
        {
            "order_id": order_id,
            "pair_id": "pair_001",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "exness_001",
            "symbol": "EURUSD",
            "side": "buy",
            "status": "primary_filled",
            "p_status": "filled",
            "s_status": "pending_open",
            "s_volume_lots": "0.10",
            "s_exness_symbol": "EURUSDz",
            "s_risk_ratio": "1.0",
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )
    await redis_svc.link_request_to_order(request_id, order_id)


@pytest.mark.asyncio
async def test_exness_open_filled_updates_s_fields_and_composes_status(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """status=filled (Exness vocab) -> s_status=filled + status composes
    to filled via primary_filled CAS."""
    await _seed_primary_filled_hedge(redis_svc)
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "exness_001",
        {
            "action": "open",
            "request_id": "req_secondary_1",
            "status": "filled",
            "reason": "request_completed",
            "broker_order_id": "55001",
            "broker_position_id": "55001",
            "fill_price": "1.08425",
            "filled_volume": "0.10",
            "ts_ms": "1735000050100",
            "retcode": "10009",
            "cascade_trigger": "false",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "filled"
    assert row["status"] == "filled"
    assert row["s_broker_order_id"] == "55001"
    assert row["s_fill_price"] == "1.08425"
    assert row["s_executed_at"] == "1735000050100"
    # Side index populated.
    linked = await redis_client.get("s_broker_order_id_to_order:55001")
    assert linked == "ord_hedge_1"
    # WS broadcast.
    types = [m.get("type") for _ch, m in broadcast.published]
    assert "secondary_filled" in types


@pytest.mark.asyncio
async def test_exness_open_rejected_sets_s_status_rejected(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """status=rejected -> s_status=rejected + s_error_msg populated."""
    await _seed_primary_filled_hedge(redis_svc)
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "exness_001",
        {
            "action": "open",
            "request_id": "req_secondary_1",
            "status": "rejected",
            "reason": "symbol_not_found",
            "retcode": "10013",
            "cascade_trigger": "false",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "rejected"
    assert row["s_error_msg"] == "symbol_not_found"
    # Composed status untouched — HedgeService decides whether to retry
    # or finalize secondary_failed.
    assert row["status"] == "primary_filled"


@pytest.mark.asyncio
async def test_exness_open_error_sets_s_status_rejected(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """status=error (broker retcode mapping) -> treated same as rejected."""
    await _seed_primary_filled_hedge(redis_svc)
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "exness_001",
        {
            "action": "open",
            "request_id": "req_secondary_1",
            "status": "error",
            "reason": "trade_disabled",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "rejected"
    assert row["s_error_msg"] == "trade_disabled"


@pytest.mark.asyncio
async def test_exness_open_requote_sets_s_status_rejected(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """status=requote -> treated as rejected so HedgeService re-pushes."""
    await _seed_primary_filled_hedge(redis_svc)
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "exness_001",
        {
            "action": "open",
            "request_id": "req_secondary_1",
            "status": "requote",
            "reason": "price_changed",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "rejected"


@pytest.mark.asyncio
async def test_exness_response_unknown_request_id_logs_warning_no_crash(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown request_id (TTL expired) -> warning, no exception, no
    state mutation."""
    # Do NOT seed the side index.
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "exness_001",
        {
            "action": "open",
            "request_id": "req_ghost",
            "status": "filled",
            "broker_order_id": "55001",
            "fill_price": "1.08425",
            "ts_ms": "1735000050100",
        },
        broker="exness",
    )
    # No broadcasts.
    assert broadcast.published == []


@pytest.mark.asyncio
async def test_exness_response_unknown_status_logs_warning(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Status value outside the documented vocab -> warning, no mutation."""
    await _seed_primary_filled_hedge(redis_svc)
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "exness_001",
        {
            "action": "open",
            "request_id": "req_secondary_1",
            "status": "garbage_status",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    # s_status untouched.
    assert row["s_status"] == "pending_open"
    assert row["status"] == "primary_filled"


@pytest.mark.asyncio
async def test_exness_close_action_filled_stamps_s_close_fields(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.8 — Exness close response handler stamps per-leg close
    fields and broadcasts ``secondary_closed``. (Pre-4.8 the handler
    was a log+drop placeholder; 4.8 wires the real cascade close ACK
    path.)"""
    await _seed_primary_filled_hedge(redis_svc)
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "exness_001",
        {
            "action": "close",
            "request_id": "req_secondary_1",
            "status": "closed",
            "close_price": "1.08200",
            "ts_ms": "1735000200000",
            "reason": "request_completed",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "closed"
    assert row["s_close_price"] == "1.08200"
    assert row["s_closed_at"] == "1735000200000"
    assert row["s_close_reason"] == "server_initiated"
    types = [m.get("type") for _ch, m in broadcast.published]
    assert "secondary_closed" in types


@pytest.mark.asyncio
async def test_exness_response_request_id_index_resolves_correctly(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """The handler routes via request_id_to_order side index — multiple
    concurrent hedge orders are kept separate."""
    await _seed_primary_filled_hedge(
        redis_svc, order_id="ord_hedge_A", request_id="req_A"
    )
    await _seed_primary_filled_hedge(
        redis_svc, order_id="ord_hedge_B", request_id="req_B"
    )
    # Fill order B.
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "exness_001",
        {
            "action": "open",
            "request_id": "req_B",
            "status": "filled",
            "broker_order_id": "55002",
            "fill_price": "1.08440",
            "ts_ms": "1735000060000",
        },
        broker="exness",
    )
    row_a = await redis_client.hgetall("order:ord_hedge_A")  # type: ignore[misc]
    row_b = await redis_client.hgetall("order:ord_hedge_B")  # type: ignore[misc]
    assert row_a["s_status"] == "pending_open"  # untouched
    assert row_b["s_status"] == "filled"
    assert row_b["status"] == "filled"
