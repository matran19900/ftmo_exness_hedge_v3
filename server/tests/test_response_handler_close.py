"""Step 4.8 — response_handler close-action symmetric tests (FTMO + Exness).

The close response is an ACK from the broker (resp_stream). Per-leg
fields are stamped, but the composed status transition is owned by
the cascade orchestrator (event_handler -> HedgeService) for hedge
orders. Single-leg orders still transition composed status here.
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
        super().__init__(redis_svc=None)
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        self.published.append((channel, data))


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


@pytest.fixture
def broadcast() -> _CapturingBroadcast:
    return _CapturingBroadcast()


async def _seed_hedge_close_pending(
    redis_svc: RedisService, *, order_id: str = "ord_hedge_1"
) -> None:
    """Hedge order mid-cascade-close (composed status=close_pending)."""
    await redis_svc.create_order(
        order_id,
        {
            "order_id": order_id,
            "pair_id": "pair_001",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "exness_001",
            "symbol": "EURUSD",
            "side": "buy",
            "status": "close_pending",
            "p_status": "close_pending",
            "p_broker_order_id": "9001",
            "s_status": "close_pending",
            "s_broker_order_id": "55001",
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )
    await redis_svc.link_request_to_order("req_close_p", order_id)
    await redis_svc.link_request_to_order("req_close_s", order_id)


async def _seed_single_leg_close_pending(
    redis_svc: RedisService, *, order_id: str = "ord_single"
) -> None:
    """Single-leg FTMO order awaiting close ack — Phase 3 path."""
    await redis_svc.create_order(
        order_id,
        {
            "order_id": order_id,
            "pair_id": "pair_x",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "",
            "symbol": "EURUSD",
            "side": "buy",
            "status": "filled",  # Phase 3 stays filled until close ack
            "p_status": "filled",
            "p_broker_order_id": "9099",
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )
    await redis_svc.link_request_to_order("req_close_single", order_id)


# ---------- Exness close filled (criterion #50) ----------


@pytest.mark.asyncio
async def test_exness_close_filled_stamps_s_fields(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_close_pending(redis_svc)
    await _handle_response_entry(
        redis_svc, broadcast, "exness_001",
        {
            "action": "close",
            "request_id": "req_close_s",
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


# ---------- Exness close rejected -> transient signal (criterion #51) ----------


@pytest.mark.asyncio
async def test_exness_close_rejected_marks_s_status_rejected(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_close_pending(redis_svc)
    await _handle_response_entry(
        redis_svc, broadcast, "exness_001",
        {
            "action": "close",
            "request_id": "req_close_s",
            "status": "rejected",
            "reason": "broker_error",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "rejected"
    assert row["s_close_error_msg"] == "broker_error"
    # Composed status untouched (cascade orchestrator owns it).
    assert row["status"] == "close_pending"


@pytest.mark.asyncio
async def test_exness_close_error_marks_s_status_rejected(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_close_pending(redis_svc)
    await _handle_response_entry(
        redis_svc, broadcast, "exness_001",
        {
            "action": "close",
            "request_id": "req_close_s",
            "status": "error",
            "reason": "trade_disabled",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "rejected"
    assert row["s_close_error_msg"] == "trade_disabled"


@pytest.mark.asyncio
async def test_exness_close_requote_marks_s_status_rejected(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Exness requote (broker price changed) — cascade orchestrator
    retries the next attempt."""
    await _seed_hedge_close_pending(redis_svc)
    await _handle_response_entry(
        redis_svc, broadcast, "exness_001",
        {
            "action": "close",
            "request_id": "req_close_s",
            "status": "requote",
            "reason": "price_changed",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "rejected"


# ---------- Exness close unknown status -> warning log only ----------


@pytest.mark.asyncio
async def test_exness_close_unknown_status_log_only(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_close_pending(redis_svc)
    await _handle_response_entry(
        redis_svc, broadcast, "exness_001",
        {
            "action": "close",
            "request_id": "req_close_s",
            "status": "garbage",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    # Untouched.
    assert row["s_status"] == "close_pending"


# ---------- FTMO close success — hedge order does NOT flip composed status ----------


@pytest.mark.asyncio
async def test_ftmo_close_success_hedge_order_leaves_composed_status(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.8 — for hedge orders the FTMO close response stamps
    per-leg fields but leaves composed status to the cascade
    orchestrator."""
    await _seed_hedge_close_pending(redis_svc)
    await _handle_response_entry(
        redis_svc, broadcast, "ftmo_001",
        {
            "action": "close",
            "request_id": "req_close_p",
            "status": "success",
            "close_price": "1.08500",
            "close_time": "1735000100000",
            "realized_pnl": "100",
        },
        broker="ftmo",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["p_status"] == "closed"
    assert row["p_close_price"] == "1.08500"
    # CRITICAL: composed status NOT flipped to "closed" here.
    assert row["status"] == "close_pending"


# ---------- FTMO close success — single-leg DOES flip composed status ----------


@pytest.mark.asyncio
async def test_ftmo_close_success_single_leg_flips_composed_status(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Phase 3 single-leg behaviour preserved: status -> closed on
    close ack."""
    await _seed_single_leg_close_pending(redis_svc)
    await _handle_response_entry(
        redis_svc, broadcast, "ftmo_001",
        {
            "action": "close",
            "request_id": "req_close_single",
            "status": "success",
            "close_price": "1.08500",
            "close_time": "1735000100000",
            "realized_pnl": "100",
        },
        broker="ftmo",
    )
    row = await redis_client.hgetall("order:ord_single")  # type: ignore[misc]
    assert row["p_status"] == "closed"
    assert row["status"] == "closed"


# ---------- FTMO close error hedge order -> p_status=rejected transient ----------


@pytest.mark.asyncio
async def test_ftmo_close_error_hedge_close_pending_flips_p_status(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """An error close response on a hedge order mid-cascade signals the
    HedgeService poller via p_status=rejected so it advances to the
    next retry."""
    await _seed_hedge_close_pending(redis_svc)
    await _handle_response_entry(
        redis_svc, broadcast, "ftmo_001",
        {
            "action": "close",
            "request_id": "req_close_p",
            "status": "error",
            "error_code": "POSITION_NOT_FOUND",
            "error_msg": "ctrader_position_not_found",
        },
        broker="ftmo",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["p_status"] == "rejected"
    assert row["p_close_error_msg"] == "ctrader_position_not_found"


# ---------- unknown request_id (TTL expired) ----------


@pytest.mark.asyncio
async def test_exness_close_unknown_request_id_warns_no_crash(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    await _handle_response_entry(
        redis_svc, broadcast, "exness_001",
        {
            "action": "close",
            "request_id": "req_ghost",
            "status": "closed",
            "close_price": "1.0",
            "ts_ms": "1",
        },
        broker="exness",
    )
    assert broadcast.published == []


# ---------- s_broker_order_id index update on cascade close fill ----------


@pytest.mark.asyncio
async def test_exness_close_does_not_overwrite_broker_order_id(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """s_broker_order_id stays pinned to the open ticket; close handler
    only writes close fields."""
    await _seed_hedge_close_pending(redis_svc)
    await _handle_response_entry(
        redis_svc, broadcast, "exness_001",
        {
            "action": "close",
            "request_id": "req_close_s",
            "status": "closed",
            "close_price": "1.082",
            "ts_ms": "1735000200000",
        },
        broker="exness",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    # Original open ticket preserved.
    assert row["s_broker_order_id"] == "55001"
