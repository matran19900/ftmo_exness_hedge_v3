"""HTTP tests for ``GET /api/history`` (step 3.9)."""

from __future__ import annotations

import time

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService
from httpx import AsyncClient


async def _seed_closed_order(
    fr: fakeredis.aioredis.FakeRedis,
    *,
    order_id: str,
    p_closed_at: int,
    symbol: str = "EURUSD",
    ftmo_account_id: str = "ftmo_001",
) -> None:
    svc = RedisService(fr)
    await svc.create_order(
        order_id,
        {
            "order_id": order_id,
            "pair_id": "pair_001",
            "ftmo_account_id": ftmo_account_id,
            "exness_account_id": "exness_001",
            "symbol": symbol,
            "side": "buy",
            "order_type": "market",
            "status": "closed",
            "p_status": "closed",
            "p_volume_lots": "0.01",
            "p_closed_at": str(p_closed_at),
            "p_realized_pnl": "150",
            "created_at": str(p_closed_at - 60_000),
            "updated_at": str(p_closed_at),
        },
    )


# ---------- auth ----------


@pytest.mark.asyncio
async def test_get_history_without_auth_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/api/history")
    assert resp.status_code == 401


# ---------- happy paths ----------


@pytest.mark.asyncio
async def test_get_history_default_window_returns_recent_orders(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """No from_ts/to_ts → trailing 7-day window. Orders within that
    window are returned; older ones are filtered out."""
    now_ms = int(time.time() * 1000)
    recent = now_ms - 60_000
    old = now_ms - 30 * 24 * 60 * 60 * 1000
    await _seed_closed_order(fake_redis, order_id="ord_recent", p_closed_at=recent)
    await _seed_closed_order(fake_redis, order_id="ord_old", p_closed_at=old)

    resp = await authed_client.get("/api/history")
    assert resp.status_code == 200
    ids = {h["order_id"] for h in resp.json()["history"]}
    assert ids == {"ord_recent"}


@pytest.mark.asyncio
async def test_get_history_explicit_time_range(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_closed_order(fake_redis, order_id="ord_a", p_closed_at=100)
    await _seed_closed_order(fake_redis, order_id="ord_b", p_closed_at=200)
    await _seed_closed_order(fake_redis, order_id="ord_c", p_closed_at=300)
    resp = await authed_client.get("/api/history?from_ts=150&to_ts=250")
    body = resp.json()
    assert {h["order_id"] for h in body["history"]} == {"ord_b"}
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_get_history_inverted_range_returns_400(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.get("/api/history?from_ts=200&to_ts=100")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "invalid_time_range"


@pytest.mark.asyncio
async def test_get_history_filter_by_symbol(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_closed_order(fake_redis, order_id="ord_e", p_closed_at=100, symbol="EURUSD")
    await _seed_closed_order(fake_redis, order_id="ord_g", p_closed_at=100, symbol="GBPUSD")
    resp = await authed_client.get("/api/history?from_ts=0&to_ts=999&symbol=EURUSD")
    assert {h["order_id"] for h in resp.json()["history"]} == {"ord_e"}


@pytest.mark.asyncio
async def test_get_history_filter_by_account_id(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_closed_order(
        fake_redis, order_id="ord_a1", p_closed_at=100, ftmo_account_id="ftmo_001"
    )
    await _seed_closed_order(
        fake_redis, order_id="ord_a2", p_closed_at=100, ftmo_account_id="ftmo_002"
    )
    resp = await authed_client.get("/api/history?from_ts=0&to_ts=999&account_id=ftmo_001")
    assert {h["order_id"] for h in resp.json()["history"]} == {"ord_a1"}


@pytest.mark.asyncio
async def test_get_history_pagination(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    for i in range(5):
        await _seed_closed_order(fake_redis, order_id=f"ord_{i}", p_closed_at=100 + i)
    resp = await authed_client.get("/api/history?from_ts=0&to_ts=9999&limit=2&offset=1")
    body = resp.json()
    # Sorted DESC: ord_4, ord_3, ord_2, ord_1, ord_0 → offset=1, limit=2.
    assert [h["order_id"] for h in body["history"]] == ["ord_3", "ord_2"]
    assert body["total"] == 5


@pytest.mark.asyncio
async def test_get_history_sorted_by_close_time_desc(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_closed_order(fake_redis, order_id="ord_old", p_closed_at=100)
    await _seed_closed_order(fake_redis, order_id="ord_new", p_closed_at=200)
    resp = await authed_client.get("/api/history?from_ts=0&to_ts=999")
    assert [h["order_id"] for h in resp.json()["history"]] == ["ord_new", "ord_old"]


@pytest.mark.asyncio
async def test_get_history_empty_when_no_closed_orders(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.get("/api/history")
    body = resp.json()
    assert body["history"] == []
    assert body["total"] == 0
