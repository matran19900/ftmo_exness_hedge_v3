"""Step 4.8 — POST /api/orders/{id}/close (Path A) endpoint extension.

Verifies that the existing close endpoint's composed-status guard
covers all Phase 4 unclosable states + writes the Path A marker on
hedge orders.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from httpx import AsyncClient


async def _seed_order(
    redis: fakeredis.aioredis.FakeRedis,
    *,
    order_id: str = "ord_close_1",
    status: str = "filled",
    p_status: str = "filled",
    is_hedge: bool = False,
    p_broker_order_id: str = "9001",
) -> None:
    fields: dict[str, str] = {
        "order_id": order_id,
        "pair_id": "pair_001",
        "ftmo_account_id": "ftmo_001",
        "exness_account_id": "exness_001" if is_hedge else "",
        "symbol": "EURUSD",
        "side": "buy",
        "order_type": "market",
        "status": status,
        "p_status": p_status,
        "p_volume_lots": "0.10",
        "p_broker_order_id": p_broker_order_id,
        "s_status": "filled" if is_hedge else "pending_phase_4",
        "s_broker_order_id": "55001" if is_hedge else "",
        "s_volume_lots": "0.10" if is_hedge else "",
        "created_at": "1735000000000",
        "updated_at": "1735000000000",
    }
    await redis.hset(f"order:{order_id}", mapping=fields)  # type: ignore[misc]
    await redis.sadd(f"orders:by_status:{status}", order_id)  # type: ignore[misc]


async def _seed_ftmo_heartbeat(redis: fakeredis.aioredis.FakeRedis) -> None:
    await redis.set("client:ftmo:ftmo_001", "online", ex=30)


# ---------- happy path (criterion #33) ----------


@pytest.mark.asyncio
async def test_close_endpoint_single_leg_returns_202(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Phase 3 single-leg close returns 202 + order_id + request_id."""
    await _seed_order(fake_redis, is_hedge=False)
    await _seed_ftmo_heartbeat(fake_redis)
    resp = await authed_client.post("/api/orders/ord_close_1/close")
    assert resp.status_code == 202
    body = resp.json()
    assert body["order_id"] == "ord_close_1"
    assert body["status"] == "accepted"
    assert body["request_id"]


@pytest.mark.asyncio
async def test_close_endpoint_hedge_writes_path_a_marker(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Hedge order close writes ``close_trigger_initiated="A"`` on the
    order so event_handler:ftmo on the eventual position_closed event
    classifies the cascade with trigger_path="A"."""
    await _seed_order(fake_redis, is_hedge=True)
    await _seed_ftmo_heartbeat(fake_redis)
    resp = await authed_client.post("/api/orders/ord_close_1/close")
    assert resp.status_code == 202
    row = await fake_redis.hgetall("order:ord_close_1")  # type: ignore[misc]
    assert row["close_trigger_initiated"] == "A"
    assert row["close_trigger_at_ms"]


@pytest.mark.asyncio
async def test_close_endpoint_single_leg_no_path_a_marker(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Single-leg orders do not need the trigger marker (no cascade)."""
    await _seed_order(fake_redis, is_hedge=False)
    await _seed_ftmo_heartbeat(fake_redis)
    await authed_client.post("/api/orders/ord_close_1/close")
    row = await fake_redis.hgetall("order:ord_close_1")  # type: ignore[misc]
    assert "close_trigger_initiated" not in row


# ---------- 404 (criterion #34) ----------


@pytest.mark.asyncio
async def test_close_endpoint_404_on_missing_order(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.post("/api/orders/nonexistent/close")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "order_not_found"


# ---------- 400 not_closeable (criteria #35, #36) ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_status",
    [
        "pending",
        "primary_filled",
        "close_pending",
        "cascade_cancel_pending",
        "closed",
        "close_failed",
        "rejected",
        "cancelled",
        "secondary_failed",
    ],
)
async def test_close_endpoint_rejects_non_filled_status(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
    bad_status: str,
) -> None:
    """Only composed status='filled' is closeable. All transient + terminal
    statuses must reject with 400 ``order_not_closeable`` (per acceptance
    criteria §3 #35-36)."""
    await _seed_order(fake_redis, status=bad_status, p_status="filled")
    await _seed_ftmo_heartbeat(fake_redis)
    resp = await authed_client.post("/api/orders/ord_close_1/close")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "order_not_closeable"
