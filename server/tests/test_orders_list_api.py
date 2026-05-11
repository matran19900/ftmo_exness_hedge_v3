"""HTTP tests for ``GET /api/orders`` + ``GET /api/orders/{id}`` (step 3.9)."""

from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio
from app.services.redis_service import RedisService
from httpx import AsyncClient


async def _seed_order(
    fr: fakeredis.aioredis.FakeRedis,
    *,
    order_id: str,
    status: str = "filled",
    symbol: str = "EURUSD",
    ftmo_account_id: str = "ftmo_001",
    created_at: int = 1735000000000,
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
            "status": status,
            "p_status": status,
            "p_volume_lots": "0.01",
            "created_at": str(created_at),
            "updated_at": str(created_at),
        },
    )


@pytest_asyncio.fixture
async def seeded(fake_redis: fakeredis.aioredis.FakeRedis) -> fakeredis.aioredis.FakeRedis:
    await _seed_order(fake_redis, order_id="ord_a", status="filled", created_at=3)
    await _seed_order(fake_redis, order_id="ord_b", status="pending", symbol="GBPUSD", created_at=2)
    await _seed_order(
        fake_redis,
        order_id="ord_c",
        status="closed",
        ftmo_account_id="ftmo_002",
        created_at=1,
    )
    return fake_redis


# ---------- auth ----------


@pytest.mark.asyncio
async def test_get_orders_without_auth_returns_401(client: AsyncClient) -> None:
    resp = await client.get("/api/orders")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_order_detail_without_auth_returns_401(
    client: AsyncClient,
) -> None:
    resp = await client.get("/api/orders/ord_a")
    assert resp.status_code == 401


# ---------- happy paths ----------


@pytest.mark.asyncio
async def test_get_orders_empty_returns_zero_total(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.get("/api/orders")
    assert resp.status_code == 200
    body = resp.json()
    assert body["orders"] == []
    assert body["total"] == 0
    assert body["limit"] == 50
    assert body["offset"] == 0


@pytest.mark.asyncio
async def test_get_orders_returns_all_sorted_desc(
    authed_client: AsyncClient,
    seeded: fakeredis.aioredis.FakeRedis,
) -> None:
    resp = await authed_client.get("/api/orders")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    ids = [o["order_id"] for o in body["orders"]]
    # created_at: ord_a=3, ord_b=2, ord_c=1 → DESC.
    assert ids == ["ord_a", "ord_b", "ord_c"]


@pytest.mark.asyncio
async def test_get_orders_status_filled_only(
    authed_client: AsyncClient,
    seeded: fakeredis.aioredis.FakeRedis,
) -> None:
    resp = await authed_client.get("/api/orders?status=filled")
    body = resp.json()
    assert [o["order_id"] for o in body["orders"]] == ["ord_a"]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_get_orders_symbol_filter(
    authed_client: AsyncClient,
    seeded: fakeredis.aioredis.FakeRedis,
) -> None:
    resp = await authed_client.get("/api/orders?symbol=GBPUSD")
    body = resp.json()
    assert [o["order_id"] for o in body["orders"]] == ["ord_b"]


@pytest.mark.asyncio
async def test_get_orders_account_id_filter(
    authed_client: AsyncClient,
    seeded: fakeredis.aioredis.FakeRedis,
) -> None:
    resp = await authed_client.get("/api/orders?account_id=ftmo_002")
    body = resp.json()
    assert [o["order_id"] for o in body["orders"]] == ["ord_c"]


@pytest.mark.asyncio
async def test_get_orders_pagination_limit_offset(
    authed_client: AsyncClient,
    seeded: fakeredis.aioredis.FakeRedis,
) -> None:
    resp = await authed_client.get("/api/orders?limit=1&offset=1")
    body = resp.json()
    # 3 total, DESC ord_a, ord_b, ord_c → offset=1, limit=1 → ord_b.
    assert [o["order_id"] for o in body["orders"]] == ["ord_b"]
    assert body["total"] == 3
    assert body["limit"] == 1
    assert body["offset"] == 1


@pytest.mark.asyncio
async def test_get_orders_invalid_status_returns_empty(
    authed_client: AsyncClient,
    seeded: fakeredis.aioredis.FakeRedis,
) -> None:
    """Unknown status string → empty result, not a 400."""
    resp = await authed_client.get("/api/orders?status=telepathy")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_get_orders_limit_above_max_returns_422(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.get("/api/orders?limit=999")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_orders_negative_offset_returns_422(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.get("/api/orders?offset=-1")
    assert resp.status_code == 422


# ---------- detail ----------


@pytest.mark.asyncio
async def test_get_order_detail_returns_full_order(
    authed_client: AsyncClient,
    seeded: fakeredis.aioredis.FakeRedis,
) -> None:
    resp = await authed_client.get("/api/orders/ord_a")
    assert resp.status_code == 200
    order = resp.json()["order"]
    assert order["order_id"] == "ord_a"
    assert order["symbol"] == "EURUSD"


@pytest.mark.asyncio
async def test_get_order_detail_not_found_returns_404(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.get("/api/orders/missing")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "order_not_found"
