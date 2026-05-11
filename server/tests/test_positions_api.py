"""HTTP tests for ``GET /api/positions`` (step 3.9)."""

from __future__ import annotations

import time

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService
from httpx import AsyncClient


async def _seed_order(
    fr: fakeredis.aioredis.FakeRedis,
    *,
    order_id: str,
    status: str = "filled",
    symbol: str = "EURUSD",
    side: str = "buy",
    p_executed_at: str = "1735000050000",
    p_fill_price: str = "1.17500",
    ftmo_account_id: str = "ftmo_001",
    sl_price: str = "1.07",
    tp_price: str = "1.09",
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
            "side": side,
            "order_type": "market",
            "status": status,
            "p_status": status,
            "p_volume_lots": "0.01",
            "p_fill_price": p_fill_price,
            "p_executed_at": p_executed_at,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )


async def _seed_position_cache(
    fr: fakeredis.aioredis.FakeRedis,
    order_id: str,
    *,
    unrealized_pnl: str = "100",
    is_stale: str = "false",
) -> None:
    svc = RedisService(fr)
    await svc.set_position_cache(
        order_id,
        {
            "order_id": order_id,
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "entry_price": "1.17500",
            "current_price": "1.18000",
            "unrealized_pnl": unrealized_pnl,
            "money_digits": "2",
            "is_stale": is_stale,
            "tick_age_ms": "100",
            "computed_at": str(int(time.time() * 1000)),
        },
    )


# ---------- auth ----------


@pytest.mark.asyncio
async def test_get_positions_without_auth_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/api/positions")
    assert resp.status_code == 401


# ---------- happy paths ----------


@pytest.mark.asyncio
async def test_get_positions_empty(authed_client: AsyncClient) -> None:
    resp = await authed_client.get("/api/positions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["positions"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_get_positions_enriched_with_cache(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, order_id="ord_a")
    await _seed_position_cache(fake_redis, "ord_a", unrealized_pnl="100")
    resp = await authed_client.get("/api/positions")
    body = resp.json()
    assert body["total"] == 1
    pos = body["positions"][0]
    assert pos["order_id"] == "ord_a"
    assert pos["unrealized_pnl"] == "100"
    assert pos["is_stale"] == "false"
    # Static overlay from order row.
    assert pos["sl_price"] == "1.07"
    assert pos["tp_price"] == "1.09"


@pytest.mark.asyncio
async def test_get_positions_pending_excluded(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, order_id="ord_f", status="filled")
    await _seed_position_cache(fake_redis, "ord_f")
    await _seed_order(fake_redis, order_id="ord_p", status="pending")
    resp = await authed_client.get("/api/positions")
    assert {p["order_id"] for p in resp.json()["positions"]} == {"ord_f"}


@pytest.mark.asyncio
async def test_get_positions_missing_cache_returns_stale_row(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Just-filled order with no cache yet → still in list with empty
    live fields + is_stale=true."""
    await _seed_order(fake_redis, order_id="ord_a")
    # No cache.
    resp = await authed_client.get("/api/positions")
    pos = resp.json()["positions"][0]
    assert pos["current_price"] == ""
    assert pos["unrealized_pnl"] == ""
    assert pos["is_stale"] == "true"


@pytest.mark.asyncio
async def test_get_positions_filter_by_account_id(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, order_id="ord_a1", ftmo_account_id="ftmo_001")
    await _seed_order(fake_redis, order_id="ord_a2", ftmo_account_id="ftmo_002")
    resp = await authed_client.get("/api/positions?account_id=ftmo_001")
    assert {p["order_id"] for p in resp.json()["positions"]} == {"ord_a1"}


@pytest.mark.asyncio
async def test_get_positions_filter_by_symbol(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, order_id="ord_eu", symbol="EURUSD")
    await _seed_order(fake_redis, order_id="ord_gbp", symbol="GBPUSD")
    resp = await authed_client.get("/api/positions?symbol=EURUSD")
    assert {p["order_id"] for p in resp.json()["positions"]} == {"ord_eu"}


@pytest.mark.asyncio
async def test_get_positions_sorted_by_executed_at_desc(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, order_id="ord_old", p_executed_at="1")
    await _seed_order(fake_redis, order_id="ord_new", p_executed_at="2")
    resp = await authed_client.get("/api/positions")
    ids = [p["order_id"] for p in resp.json()["positions"]]
    assert ids == ["ord_new", "ord_old"]


@pytest.mark.asyncio
async def test_get_positions_total_matches_list_length(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """No pagination on positions endpoint — total always equals
    the response array length."""
    await _seed_order(fake_redis, order_id="ord_a")
    await _seed_order(fake_redis, order_id="ord_b")
    resp = await authed_client.get("/api/positions")
    body = resp.json()
    assert body["total"] == len(body["positions"]) == 2
