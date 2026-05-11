"""HTTP tests for the step-3.9 mutation endpoints.

``POST /api/orders/{id}/close`` and ``POST /api/orders/{id}/modify``.
Each verifies:
  - 401 without auth.
  - happy-path 202 + cmd_stream entry + request_id side index.
  - validation branches mapped to the right 4xx / 5xx response.
"""

from __future__ import annotations

import json

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService
from httpx import AsyncClient


async def _seed_order(
    fr: fakeredis.aioredis.FakeRedis,
    *,
    order_id: str = "ord_a",
    status: str = "filled",
    side: str = "buy",
    p_broker_order_id: str = "5451198",
    p_volume_lots: str = "0.01",
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
            "symbol": "EURUSD",
            "side": side,
            "order_type": "market",
            "status": status,
            "p_status": status,
            "p_volume_lots": p_volume_lots,
            "p_broker_order_id": p_broker_order_id,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )


async def _seed_heartbeat(fr: fakeredis.aioredis.FakeRedis) -> None:
    await fr.set("client:ftmo:ftmo_001", "online", ex=30)


async def _seed_tick(fr: fakeredis.aioredis.FakeRedis) -> None:
    await fr.set(
        "tick:EURUSD",
        json.dumps({"bid": 1.08400, "ask": 1.08420, "ts": 1}),
        ex=60,
    )


# ---------- CLOSE: auth ----------


@pytest.mark.asyncio
async def test_close_without_auth_returns_401(client: AsyncClient) -> None:
    resp = await client.post("/api/orders/ord_a/close")
    assert resp.status_code == 401


# ---------- CLOSE: happy path ----------


@pytest.mark.asyncio
async def test_close_happy_path_pushes_cmd_stream(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis)
    await _seed_heartbeat(fake_redis)
    resp = await authed_client.post("/api/orders/ord_a/close")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["order_id"] == "ord_a"
    assert body["status"] == "accepted"
    assert len(body["request_id"]) == 32

    entries = await fake_redis.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["action"] == "close"
    assert fields["broker_order_id"] == "5451198"
    assert fields["volume_lots"] == "0.01"
    assert fields["request_id"] == body["request_id"]

    # request_id → order_id side-index linked.
    linked = await fake_redis.get(f"request_id_to_order:{body['request_id']}")
    assert linked == "ord_a"


@pytest.mark.asyncio
async def test_close_with_matching_volume_accepted(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Explicit full-volume close = full close (Phase 3 supported)."""
    await _seed_order(fake_redis)
    await _seed_heartbeat(fake_redis)
    resp = await authed_client.post(
        "/api/orders/ord_a/close",
        json={"volume_lots": 0.01},
    )
    assert resp.status_code == 202


# ---------- CLOSE: error branches ----------


@pytest.mark.asyncio
async def test_close_order_not_found_returns_404(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.post("/api/orders/missing/close")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "order_not_found"


@pytest.mark.asyncio
async def test_close_pending_order_returns_400(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, status="pending")
    await _seed_heartbeat(fake_redis)
    resp = await authed_client.post("/api/orders/ord_a/close")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "invalid_state"


@pytest.mark.asyncio
async def test_close_already_closed_order_returns_400(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, status="closed")
    await _seed_heartbeat(fake_redis)
    resp = await authed_client.post("/api/orders/ord_a/close")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_close_offline_client_returns_409(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis)
    # No heartbeat → offline.
    resp = await authed_client.post("/api/orders/ord_a/close")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "client_offline"


@pytest.mark.asyncio
async def test_close_partial_volume_returns_400(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, p_volume_lots="0.10")
    await _seed_heartbeat(fake_redis)
    resp = await authed_client.post(
        "/api/orders/ord_a/close",
        json={"volume_lots": 0.05},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "partial_close_unsupported"


@pytest.mark.asyncio
async def test_close_missing_broker_order_id_returns_500(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Corrupt order (filled but no broker_order_id): surfaces as 500."""
    await _seed_order(fake_redis, p_broker_order_id="")
    await _seed_heartbeat(fake_redis)
    resp = await authed_client.post("/api/orders/ord_a/close")
    assert resp.status_code == 500
    assert resp.json()["detail"]["error_code"] == "order_corrupt"


# ---------- MODIFY: auth ----------


@pytest.mark.asyncio
async def test_modify_without_auth_returns_401(client: AsyncClient) -> None:
    resp = await client.post("/api/orders/ord_a/modify", json={"sl": 1.07})
    assert resp.status_code == 401


# ---------- MODIFY: happy paths ----------


@pytest.mark.asyncio
async def test_modify_sl_and_tp_happy_path(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis)
    await _seed_heartbeat(fake_redis)
    await _seed_tick(fake_redis)  # bid=1.084, ask=1.0842
    resp = await authed_client.post(
        "/api/orders/ord_a/modify",
        json={"sl": 1.075, "tp": 1.090},
    )
    assert resp.status_code == 202, resp.text
    entries = await fake_redis.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    fields = entries[0][1]
    assert fields["action"] == "modify_sl_tp"
    assert fields["sl"] == "1.075"
    assert fields["tp"] == "1.09"


@pytest.mark.asyncio
async def test_modify_only_sl_keeps_existing_tp(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, sl_price="1.07", tp_price="1.09")
    await _seed_heartbeat(fake_redis)
    await _seed_tick(fake_redis)
    resp = await authed_client.post(
        "/api/orders/ord_a/modify",
        json={"sl": 1.075},
    )
    assert resp.status_code == 202
    fields = (await fake_redis.xrange("cmd_stream:ftmo:ftmo_001", "-", "+"))[0][1]
    assert fields["sl"] == "1.075"
    assert fields["tp"] == "1.09"  # unchanged from order row


@pytest.mark.asyncio
async def test_modify_only_tp_keeps_existing_sl(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, sl_price="1.07", tp_price="1.09")
    await _seed_heartbeat(fake_redis)
    await _seed_tick(fake_redis)
    resp = await authed_client.post(
        "/api/orders/ord_a/modify",
        json={"tp": 1.095},
    )
    assert resp.status_code == 202
    fields = (await fake_redis.xrange("cmd_stream:ftmo:ftmo_001", "-", "+"))[0][1]
    assert fields["sl"] == "1.07"  # unchanged
    assert fields["tp"] == "1.095"


@pytest.mark.asyncio
async def test_modify_remove_sl_via_zero_skips_tick_check(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """sl=0 means 'remove SL' — no tick needed for validation."""
    await _seed_order(fake_redis)
    await _seed_heartbeat(fake_redis)
    # NO tick seeded — but modify should still succeed.
    resp = await authed_client.post(
        "/api/orders/ord_a/modify",
        json={"sl": 0},
    )
    assert resp.status_code == 202


# ---------- MODIFY: error branches ----------


@pytest.mark.asyncio
async def test_modify_neither_sl_nor_tp_returns_422(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.post("/api/orders/ord_a/modify", json={})
    # Pydantic model_validator rejects → 422.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_modify_order_not_found_returns_404(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.post(
        "/api/orders/missing/modify",
        json={"sl": 1.07},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "order_not_found"


@pytest.mark.asyncio
async def test_modify_pending_order_returns_400(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, status="pending")
    await _seed_heartbeat(fake_redis)
    await _seed_tick(fake_redis)
    resp = await authed_client.post(
        "/api/orders/ord_a/modify",
        json={"sl": 1.07},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "invalid_state"


@pytest.mark.asyncio
async def test_modify_invalid_sl_direction_buy_returns_400(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, side="buy")
    await _seed_heartbeat(fake_redis)
    await _seed_tick(fake_redis)  # bid=1.084
    resp = await authed_client.post(
        "/api/orders/ord_a/modify",
        json={"sl": 1.09},  # above bid for BUY
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "invalid_sl_direction"


@pytest.mark.asyncio
async def test_modify_invalid_tp_direction_buy_returns_400(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis, side="buy")
    await _seed_heartbeat(fake_redis)
    await _seed_tick(fake_redis)  # ask=1.0842
    resp = await authed_client.post(
        "/api/orders/ord_a/modify",
        json={"tp": 1.080},  # below ask for BUY
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "invalid_tp_direction"


@pytest.mark.asyncio
async def test_modify_no_tick_data_returns_409(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis)
    await _seed_heartbeat(fake_redis)
    # No tick.
    resp = await authed_client.post(
        "/api/orders/ord_a/modify",
        json={"sl": 1.07},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "no_tick_data"


@pytest.mark.asyncio
async def test_modify_client_offline_returns_409(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_order(fake_redis)
    await _seed_tick(fake_redis)
    # No heartbeat → offline.
    resp = await authed_client.post(
        "/api/orders/ord_a/modify",
        json={"sl": 1.07},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "client_offline"
