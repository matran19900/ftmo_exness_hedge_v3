"""End-to-end tests for POST /api/orders (step 3.6).

Hits the FastAPI app over the ASGI transport with the `authed_client`
fixture. Verifies:
  - 401 without auth.
  - 202 + body shape on the happy path.
  - 422 on schema violations (missing fields, invalid literals).
  - 4xx mapping for the service-layer OrderValidationError branches.
  - Redis side effects (order row, cmd_stream entry, side index) on
    a successful POST.

Schema-only edge cases (pure Pydantic) also live here since the
schemas are defined inline in ``app.api.orders``.
"""

from __future__ import annotations

import json

import fakeredis.aioredis
import pytest
import pytest_asyncio
from app.services.redis_service import RedisService
from httpx import AsyncClient

# Use the shared fixtures from conftest: ``client`` (unauthed),
# ``authed_client`` (admin Bearer), ``fake_redis`` (per-test fakeredis
# already wired via _override_redis_service autouse fixture).


@pytest_asyncio.fixture
async def seeded_state(fake_redis: fakeredis.aioredis.FakeRedis) -> RedisService:
    """Seed minimum state for a valid market BUY order."""
    rc = fake_redis
    # Pair.
    await rc.hset(  # type: ignore[misc]
        "pair:pair_001",
        mapping={
            "pair_id": "pair_001",
            "name": "test",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "exness_001",
            "ratio": "1.0",
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )
    # Account.
    await rc.sadd("accounts:ftmo", "ftmo_001")  # type: ignore[misc]
    await rc.hset(  # type: ignore[misc]
        "account_meta:ftmo:ftmo_001",
        mapping={
            "name": "ftmo_001",
            "created_at": "1735000000000",
            "enabled": "true",
        },
    )
    # Heartbeat.
    await rc.set("client:ftmo:ftmo_001", "online", ex=30)
    # Symbol.
    await rc.sadd("symbols:active", "EURUSD")  # type: ignore[misc]
    await rc.hset(  # type: ignore[misc]
        "symbol_config:EURUSD",
        mapping={
            "lot_size": "100000",
            "min_volume": "1000",
            "max_volume": "1000000000",
            "step_volume": "1",
            "ctrader_symbol_id": "1",
        },
    )
    # Tick.
    await rc.set(
        "tick:EURUSD",
        json.dumps({"bid": 1.08400, "ask": 1.08420, "ts": 1735000000000}),
        ex=60,
    )
    return RedisService(rc)


# ---------- auth ----------


@pytest.mark.asyncio
async def test_post_orders_without_auth_returns_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 401


# ---------- happy path ----------


@pytest.mark.asyncio
async def test_post_orders_market_buy_returns_202(
    authed_client: AsyncClient,
    seeded_state: RedisService,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
            "sl": 1.08000,
            "tp": 1.09000,
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["order_id"].startswith("ord_")
    assert len(body["request_id"]) == 32

    # Verify Redis side effects.
    order = await fake_redis.hgetall(f"order:{body['order_id']}")  # type: ignore[misc]
    assert order["pair_id"] == "pair_001"
    assert order["status"] == "pending"

    entries = await fake_redis.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    assert len(entries) == 1
    assert entries[0][1]["action"] == "open"

    linked = await fake_redis.get(f"request_id_to_order:{body['request_id']}")
    assert linked == body["order_id"]


@pytest.mark.asyncio
async def test_post_orders_symbol_lowercase_normalizes_to_uppercase(
    authed_client: AsyncClient, seeded_state: RedisService
) -> None:
    """Pydantic validator upper-cases + strips the symbol so an
    operator who types 'eurusd' still hits the EURUSD whitelist."""
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "eurusd",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 202, resp.text


# ---------- schema validation (422) ----------


@pytest.mark.asyncio
async def test_post_orders_missing_pair_id_returns_422(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_orders_volume_zero_returns_422(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_orders_volume_negative_returns_422(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": -0.01,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_orders_invalid_side_returns_422(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "hold",
            "order_type": "market",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_orders_invalid_order_type_returns_422(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "trailing_stop",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_orders_negative_sl_returns_422(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
            "sl": -1.0,
        },
    )
    assert resp.status_code == 422


# ---------- service-layer errors (4xx) ----------


@pytest.mark.asyncio
async def test_post_orders_pair_not_found_returns_404(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "missing",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["detail"]["error_code"] == "pair_not_found"


@pytest.mark.asyncio
async def test_post_orders_client_offline_returns_409(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    # Seed pair + account but NO heartbeat.
    await fake_redis.hset(  # type: ignore[misc]
        "pair:pair_001",
        mapping={
            "pair_id": "pair_001",
            "name": "t",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "exness_001",
            "ratio": "1.0",
            "created_at": "1",
            "updated_at": "1",
        },
    )
    await fake_redis.sadd("accounts:ftmo", "ftmo_001")  # type: ignore[misc]
    await fake_redis.hset(  # type: ignore[misc]
        "account_meta:ftmo:ftmo_001",
        mapping={"name": "ftmo_001", "created_at": "1", "enabled": "true"},
    )
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "client_offline"


@pytest.mark.asyncio
async def test_post_orders_invalid_sl_direction_returns_400(
    authed_client: AsyncClient,
    seeded_state: RedisService,
) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
            "sl": 1.09000,  # above bid for BUY
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "invalid_sl_direction"


@pytest.mark.asyncio
async def test_post_orders_symbol_inactive_returns_400(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    # Seed everything except symbol whitelist.
    await fake_redis.hset(  # type: ignore[misc]
        "pair:pair_001",
        mapping={
            "pair_id": "pair_001",
            "name": "t",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "exness_001",
            "ratio": "1.0",
            "created_at": "1",
            "updated_at": "1",
        },
    )
    await fake_redis.sadd("accounts:ftmo", "ftmo_001")  # type: ignore[misc]
    await fake_redis.hset(  # type: ignore[misc]
        "account_meta:ftmo:ftmo_001",
        mapping={"name": "ftmo_001", "created_at": "1", "enabled": "true"},
    )
    await fake_redis.set("client:ftmo:ftmo_001", "online", ex=30)
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "symbol_inactive"


@pytest.mark.asyncio
async def test_post_orders_limit_without_entry_price_returns_400(
    authed_client: AsyncClient,
    seeded_state: RedisService,
) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "limit",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "missing_entry_price"


@pytest.mark.asyncio
async def test_post_orders_error_response_carries_message_and_code(
    authed_client: AsyncClient,
) -> None:
    """Pin the error-detail shape so the frontend's error parser can
    rely on both fields being present."""
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "missing",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
    assert body["detail"]["error_code"] == "pair_not_found"
    assert "missing" in body["detail"]["message"]


# ---------- response model ----------


@pytest.mark.asyncio
async def test_post_orders_response_fields_present(
    authed_client: AsyncClient,
    seeded_state: RedisService,
) -> None:
    resp = await authed_client.post(
        "/api/orders",
        json={
            "pair_id": "pair_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "volume_lots": 0.01,
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert set(body.keys()) == {"order_id", "request_id", "status", "message"}
    assert body["status"] == "accepted"
    assert isinstance(body["message"], str) and body["message"]


# ---------- schema-only ----------


def test_schema_normalizes_symbol_upper_and_strip() -> None:
    """Direct Pydantic validation: ' eurusd ' → 'EURUSD'."""
    from app.api.orders import OrderCreateRequest  # noqa: PLC0415

    req = OrderCreateRequest(
        pair_id="p",
        symbol=" eurusd ",
        side="buy",
        order_type="market",
        volume_lots=0.01,
    )
    assert req.symbol == "EURUSD"


def test_schema_defaults_sl_tp_entry_zero() -> None:
    from app.api.orders import OrderCreateRequest  # noqa: PLC0415

    req = OrderCreateRequest(
        pair_id="p",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=0.01,
    )
    assert req.sl == 0.0
    assert req.tp == 0.0
    assert req.entry_price == 0.0
