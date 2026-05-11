"""Tests for ``app.services.order_service.OrderService`` (step 3.6).

Direct service tests — no HTTP layer. Each test seeds fakeredis with
the minimum state the service needs (pair, account_meta, heartbeat,
active symbol, symbol_config, tick cache) and asserts the validation
branch returns the right ``OrderValidationError`` shape or, on the
happy path, that the order row + cmd_stream entry + side index were
written correctly.
"""

from __future__ import annotations

import json
from typing import Any

import fakeredis.aioredis
import pytest
from app.services.order_service import OrderService, OrderValidationError
from app.services.redis_service import RedisService

# ---------- fixtures ----------


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def svc(redis_client: fakeredis.aioredis.FakeRedis) -> OrderService:
    return OrderService(RedisService(redis_client))


# ---------- seeding helpers ----------


async def _seed_pair(
    rc: fakeredis.aioredis.FakeRedis,
    *,
    pair_id: str = "pair_001",
    ftmo_account_id: str = "ftmo_001",
    exness_account_id: str = "exness_001",
    enabled: bool | None = None,
) -> None:
    fields: dict[str, str] = {
        "pair_id": pair_id,
        "name": "test-pair",
        "ftmo_account_id": ftmo_account_id,
        "exness_account_id": exness_account_id,
        "ratio": "1.0",
        "created_at": "1735000000000",
        "updated_at": "1735000000000",
    }
    if enabled is not None:
        fields["enabled"] = "true" if enabled else "false"
    await rc.hset(f"pair:{pair_id}", mapping=fields)  # type: ignore[misc]


async def _seed_account(
    rc: fakeredis.aioredis.FakeRedis,
    *,
    broker: str = "ftmo",
    account_id: str = "ftmo_001",
    enabled: bool = True,
) -> None:
    await rc.sadd(f"accounts:{broker}", account_id)  # type: ignore[misc]
    await rc.hset(  # type: ignore[misc]
        f"account_meta:{broker}:{account_id}",
        mapping={
            "name": account_id,
            "created_at": "1735000000000",
            "enabled": "true" if enabled else "false",
        },
    )


async def _seed_heartbeat(
    rc: fakeredis.aioredis.FakeRedis,
    *,
    broker: str = "ftmo",
    account_id: str = "ftmo_001",
) -> None:
    await rc.set(f"client:{broker}:{account_id}", "online", ex=30)


async def _seed_symbol(
    rc: fakeredis.aioredis.FakeRedis,
    *,
    symbol: str = "EURUSD",
    lot_size: int = 100_000,
    min_volume: int = 1_000,
    max_volume: int = 1_000_000_000,
    step_volume: int = 1,
) -> None:
    await rc.sadd("symbols:active", symbol)  # type: ignore[misc]
    await rc.hset(  # type: ignore[misc]
        f"symbol_config:{symbol}",
        mapping={
            "lot_size": str(lot_size),
            "min_volume": str(min_volume),
            "max_volume": str(max_volume),
            "step_volume": str(step_volume),
            "ctrader_symbol_id": "1",
        },
    )


async def _seed_tick(
    rc: fakeredis.aioredis.FakeRedis,
    *,
    symbol: str = "EURUSD",
    bid: float = 1.08400,
    ask: float = 1.08420,
) -> None:
    await rc.set(
        f"tick:{symbol}",
        json.dumps({"bid": bid, "ask": ask, "ts": 1735000000000}),
        ex=60,
    )


async def _seed_happy(rc: fakeredis.aioredis.FakeRedis) -> None:
    """Seed the minimum state for a successful market BUY."""
    await _seed_pair(rc)
    await _seed_account(rc)
    await _seed_heartbeat(rc)
    await _seed_symbol(rc)
    await _seed_tick(rc)


# ---------- happy paths ----------


@pytest.mark.asyncio
async def test_create_order_market_buy_happy_path(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_happy(redis_client)
    order_id, request_id = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=0.01,
        sl=1.08000,
        tp=1.09000,
        entry_price=0.0,
    )

    # Returned identifiers have the right shape.
    assert order_id.startswith("ord_")
    assert len(order_id) == 12  # "ord_" + 8 hex
    assert len(request_id) == 32  # uuid4 hex

    # Order row exists in Redis with correct fields.
    row = await redis_client.hgetall(f"order:{order_id}")  # type: ignore[misc]
    assert row["pair_id"] == "pair_001"
    assert row["ftmo_account_id"] == "ftmo_001"
    assert row["exness_account_id"] == "exness_001"
    assert row["symbol"] == "EURUSD"
    assert row["side"] == "buy"
    assert row["order_type"] == "market"
    assert row["p_volume_lots"] == "0.01"
    assert row["sl_price"] == "1.08"
    assert row["tp_price"] == "1.09"
    assert row["status"] == "pending"
    assert row["p_status"] == "pending"
    assert row["s_status"] == "pending_phase_4"
    assert row["s_volume_lots"] == ""

    # Order indexed in by_status:pending.
    by_status = await redis_client.smembers("orders:by_status:pending")  # type: ignore[misc]
    assert order_id in by_status

    # cmd_stream entry pushed.
    entries = await redis_client.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["order_id"] == order_id
    assert fields["action"] == "open"
    assert fields["symbol"] == "EURUSD"
    assert fields["side"] == "buy"
    assert fields["order_type"] == "market"
    assert fields["volume_lots"] == "0.01"
    assert fields["sl"] == "1.08"
    assert fields["tp"] == "1.09"
    assert fields["request_id"] == request_id
    assert "created_at" in fields

    # request_id → order_id side index.
    linked = await redis_client.get(f"request_id_to_order:{request_id}")
    assert linked == order_id

    # pending_cmds zset includes request_id.
    pending = await redis_client.zrange("pending_cmds:ftmo:ftmo_001", 0, -1)
    assert request_id in pending


@pytest.mark.asyncio
async def test_create_order_market_sell_happy_path(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """SELL side: SL above ask, TP below bid."""
    await _seed_happy(redis_client)
    order_id, _request_id = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="sell",
        order_type="market",
        volume_lots=0.01,
        sl=1.09000,
        tp=1.08000,
        entry_price=0.0,
    )
    row = await redis_client.hgetall(f"order:{order_id}")  # type: ignore[misc]
    assert row["side"] == "sell"


@pytest.mark.asyncio
async def test_create_order_limit_buy_happy_path(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """LIMIT BUY: entry_price below market, SL below entry, TP above entry."""
    await _seed_happy(redis_client)
    order_id, _ = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="limit",
        volume_lots=0.01,
        sl=1.07000,
        tp=1.09000,
        entry_price=1.08000,
    )
    row = await redis_client.hgetall(f"order:{order_id}")  # type: ignore[misc]
    assert row["order_type"] == "limit"
    assert row["entry_price"] == "1.08"


@pytest.mark.asyncio
async def test_create_order_stop_sell_happy_path(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """STOP SELL: entry below market, SL above entry, TP below entry."""
    await _seed_happy(redis_client)
    await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="sell",
        order_type="stop",
        volume_lots=0.01,
        sl=1.09000,
        tp=1.07000,
        entry_price=1.08000,
    )


@pytest.mark.asyncio
async def test_create_order_no_sl_no_tp_allowed(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """SL=0 and TP=0 → both unset; direction checks skipped."""
    await _seed_happy(redis_client)
    order_id, _ = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=0.01,
        sl=0,
        tp=0,
        entry_price=0,
    )
    row = await redis_client.hgetall(f"order:{order_id}")  # type: ignore[misc]
    # str(0) == "0" and str(0.0) == "0.0"; accept either since the
    # bridge's parser handles both.
    assert row["sl_price"] in ("0", "0.0")
    assert row["tp_price"] in ("0", "0.0")


# ---------- pair branch ----------


@pytest.mark.asyncio
async def test_pair_not_found_raises_404(svc: OrderService) -> None:
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="missing",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.http_status == 404
    assert exc_info.value.error_code == "pair_not_found"


@pytest.mark.asyncio
async def test_pair_disabled_raises_400(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_pair(redis_client, enabled=False)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.http_status == 400
    assert exc_info.value.error_code == "pair_disabled"


# ---------- account branch ----------


@pytest.mark.asyncio
async def test_account_not_found_raises_404(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_pair(redis_client)
    # No _seed_account → account_meta missing.
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.http_status == 404
    assert exc_info.value.error_code == "account_not_found"


@pytest.mark.asyncio
async def test_account_disabled_raises_400(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_pair(redis_client)
    await _seed_account(redis_client, enabled=False)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.http_status == 400
    assert exc_info.value.error_code == "account_disabled"


# ---------- client status ----------


@pytest.mark.asyncio
async def test_client_offline_raises_409(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """No heartbeat key → status=offline → 409 client_offline."""
    await _seed_pair(redis_client)
    await _seed_account(redis_client)
    # _seed_heartbeat skipped.
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.http_status == 409
    assert exc_info.value.error_code == "client_offline"


# ---------- symbol branch ----------


@pytest.mark.asyncio
async def test_symbol_inactive_raises_400(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_pair(redis_client)
    await _seed_account(redis_client)
    await _seed_heartbeat(redis_client)
    # symbol NOT in active whitelist.
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.http_status == 400
    assert exc_info.value.error_code == "symbol_inactive"


@pytest.mark.asyncio
async def test_symbol_config_missing_raises_404(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Symbol is in active set but symbol_config:{sym} is absent."""
    await _seed_pair(redis_client)
    await _seed_account(redis_client)
    await _seed_heartbeat(redis_client)
    await redis_client.sadd("symbols:active", "EURUSD")  # type: ignore[misc]
    # symbol_config NOT seeded.
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.http_status == 404
    assert exc_info.value.error_code == "symbol_not_synced"


# ---------- volume bounds ----------


@pytest.mark.asyncio
async def test_volume_below_min_raises_invalid_volume(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_pair(redis_client)
    await _seed_account(redis_client)
    await _seed_heartbeat(redis_client)
    await _seed_symbol(redis_client, min_volume=100_000)  # 0.01 lots = 1000 < 100k
    await _seed_tick(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.error_code == "invalid_volume"
    assert "too small" in str(exc_info.value)


@pytest.mark.asyncio
async def test_volume_above_max_raises_invalid_volume(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_pair(redis_client)
    await _seed_account(redis_client)
    await _seed_heartbeat(redis_client)
    await _seed_symbol(redis_client, max_volume=500)
    await _seed_tick(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.error_code == "invalid_volume"
    assert "too large" in str(exc_info.value)


@pytest.mark.asyncio
async def test_volume_not_multiple_of_step_raises_invalid_volume(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_pair(redis_client)
    await _seed_account(redis_client)
    await _seed_heartbeat(redis_client)
    # lot_size=100_000, step_volume=1000 → ctrader_volume must be % 1000.
    # 0.005 lots → 500 wire units → 500 % 1000 != 0.
    await _seed_symbol(redis_client, step_volume=1000, min_volume=100)
    await _seed_tick(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.005,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.error_code == "invalid_volume"
    assert "multiple of step" in str(exc_info.value)


@pytest.mark.asyncio
async def test_volume_max_unset_in_config_no_upper_bound(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Some symbols have unbounded max_volume; service treats empty
    ``max_volume`` as "no upper limit"."""
    await _seed_pair(redis_client)
    await _seed_account(redis_client)
    await _seed_heartbeat(redis_client)
    await redis_client.sadd("symbols:active", "EURUSD")  # type: ignore[misc]
    await redis_client.hset(  # type: ignore[misc]
        "symbol_config:EURUSD",
        mapping={
            "lot_size": "100000",
            "min_volume": "1000",
            "max_volume": "",  # empty
            "step_volume": "1",
            "ctrader_symbol_id": "1",
        },
    )
    await _seed_tick(redis_client)
    # 100 lots × 100_000 = 10_000_000 wire units — no exception even
    # though this would normally be huge.
    await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=100.0,
        sl=0,
        tp=0,
        entry_price=0,
    )


# ---------- entry_price ----------


@pytest.mark.asyncio
async def test_limit_without_entry_price_raises_400(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_happy(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="limit",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.http_status == 400
    assert exc_info.value.error_code == "missing_entry_price"


@pytest.mark.asyncio
async def test_stop_without_entry_price_raises_400(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_happy(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="sell",
            order_type="stop",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.error_code == "missing_entry_price"


# ---------- SL/TP direction (D-045) ----------


@pytest.mark.asyncio
async def test_market_buy_sl_above_bid_raises_invalid_sl_direction(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_happy(redis_client)  # bid=1.08400, ask=1.08420
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=1.09000,  # above bid
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.error_code == "invalid_sl_direction"


@pytest.mark.asyncio
async def test_market_buy_tp_below_ask_raises_invalid_tp_direction(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_happy(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=1.08000,  # below ask
            entry_price=0,
        )
    assert exc_info.value.error_code == "invalid_tp_direction"


@pytest.mark.asyncio
async def test_market_sell_sl_below_ask_raises_invalid_sl_direction(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_happy(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="sell",
            order_type="market",
            volume_lots=0.01,
            sl=1.08000,  # below ask
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.error_code == "invalid_sl_direction"


@pytest.mark.asyncio
async def test_market_sell_tp_above_bid_raises_invalid_tp_direction(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_happy(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="sell",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=1.09000,  # above bid
            entry_price=0,
        )
    assert exc_info.value.error_code == "invalid_tp_direction"


@pytest.mark.asyncio
async def test_limit_buy_sl_above_entry_raises_invalid_sl_direction(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Limit/stop: reference is the requested entry_price, not bid/ask."""
    await _seed_happy(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="limit",
            volume_lots=0.01,
            sl=1.08100,  # above entry
            tp=0,
            entry_price=1.08000,
        )
    assert exc_info.value.error_code == "invalid_sl_direction"


# ---------- tick freshness ----------


@pytest.mark.asyncio
async def test_no_tick_data_raises_409(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_pair(redis_client)
    await _seed_account(redis_client)
    await _seed_heartbeat(redis_client)
    await _seed_symbol(redis_client)
    # tick NOT seeded.
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.http_status == 409
    assert exc_info.value.error_code == "no_tick_data"


@pytest.mark.asyncio
async def test_malformed_tick_raises_409(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """A tick cache entry that isn't valid JSON should fail closed
    (we can't validate direction safely without parseable bid/ask)."""
    await _seed_pair(redis_client)
    await _seed_account(redis_client)
    await _seed_heartbeat(redis_client)
    await _seed_symbol(redis_client)
    await redis_client.set("tick:EURUSD", "{not-json", ex=60)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.error_code == "no_tick_data"


# ---------- idempotence-by-construction sanity ----------


@pytest.mark.asyncio
async def test_two_creates_produce_distinct_order_ids(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Each call mints a fresh uuid4-hex-suffixed order_id + request_id."""
    await _seed_happy(redis_client)
    a, ra = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=0.01,
        sl=0,
        tp=0,
        entry_price=0,
    )
    b, rb = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=0.01,
        sl=0,
        tp=0,
        entry_price=0,
    )
    assert a != b
    assert ra != rb
    assert (await redis_client.xlen("cmd_stream:ftmo:ftmo_001")) == 2


@pytest.mark.asyncio
async def test_order_id_format(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_happy(redis_client)
    order_id, _ = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=0.01,
        sl=0,
        tp=0,
        entry_price=0,
    )
    # Strict shape: prefix + 8 lowercase hex.
    assert order_id[:4] == "ord_"
    rest = order_id[4:]
    assert len(rest) == 8
    assert all(c in "0123456789abcdef" for c in rest)


@pytest.mark.asyncio
async def test_validation_failure_does_not_write_any_redis_state(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Validation errors must short-circuit BEFORE any HSET / XADD —
    otherwise a rejected order leaves orphaned rows around."""
    await _seed_happy(redis_client)
    with pytest.raises(OrderValidationError):
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=1.09000,  # invalid direction
            tp=0,
            entry_price=0,
        )
    assert (await redis_client.xlen("cmd_stream:ftmo:ftmo_001")) == 0
    keys: list[Any] = await redis_client.keys("order:*")
    assert keys == []


@pytest.mark.asyncio
async def test_validation_order_is_pair_first_then_account(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """If pair AND account are both missing, the operator should see
    'pair_not_found' (the most actionable error). Pinning the order so
    a future refactor doesn't quietly swap checks."""
    # No pair, no account, no symbol — every check would fail.
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="missing",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.error_code == "pair_not_found"


@pytest.mark.asyncio
async def test_validation_order_is_account_before_client_status(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Pair exists, account missing, client offline → account error
    wins (404 > 409 in actionability ranking)."""
    await _seed_pair(redis_client)
    # account_meta missing, heartbeat absent.
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.01,
            sl=0,
            tp=0,
            entry_price=0,
        )
    assert exc_info.value.error_code == "account_not_found"
