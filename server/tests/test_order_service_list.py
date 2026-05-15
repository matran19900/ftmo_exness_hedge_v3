"""Service-layer tests for the step-3.9 read + mutation methods.

Direct ``OrderService`` exercises — no HTTP layer. fakeredis is
seeded with orders + position_cache + tick rows + heartbeat keys as
appropriate. The HTTP-layer wrappers (``app/api/orders.py``,
``positions.py``, ``history.py``) are covered in the
``test_*_api.py`` files.
"""

from __future__ import annotations

import json
import time

import fakeredis.aioredis
import pytest
from app.services.order_service import OrderService, OrderValidationError
from app.services.redis_service import RedisService


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def svc(redis_client: fakeredis.aioredis.FakeRedis) -> OrderService:
    return OrderService(RedisService(redis_client))


# ---------- seeding helpers ----------


async def _seed_order(
    rc: fakeredis.aioredis.FakeRedis,
    *,
    order_id: str = "ord_a",
    status: str = "pending",
    p_status: str | None = None,
    symbol: str = "EURUSD",
    side: str = "buy",
    ftmo_account_id: str = "ftmo_001",
    created_at: int = 1735000000000,
    p_broker_order_id: str = "",
    p_volume_lots: str = "0.01",
    p_fill_price: str = "",
    p_closed_at: str = "",
    p_executed_at: str = "",
    sl_price: str = "",
    tp_price: str = "",
    extra: dict[str, str] | None = None,
) -> None:
    fields: dict[str, str] = {
        "order_id": order_id,
        "pair_id": "pair_001",
        "ftmo_account_id": ftmo_account_id,
        "exness_account_id": "exness_001",
        "symbol": symbol,
        "side": side,
        "order_type": "market",
        "status": status,
        "p_status": p_status if p_status is not None else status,
        "p_volume_lots": p_volume_lots,
        "p_broker_order_id": p_broker_order_id,
        "p_fill_price": p_fill_price,
        "p_executed_at": p_executed_at,
        "p_closed_at": p_closed_at,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "created_at": str(created_at),
        "updated_at": str(created_at),
    }
    if extra:
        fields.update(extra)
    # Use the same path as production code so the status index is
    # written via the pipeline (matches list_orders_by_status reads).
    svc = RedisService(rc)
    await svc.create_order(order_id, fields)


async def _seed_position_cache(
    rc: fakeredis.aioredis.FakeRedis,
    order_id: str,
    *,
    current_price: str = "1.18000",
    unrealized_pnl: str = "100",
    is_stale: str = "false",
) -> None:
    svc = RedisService(rc)
    await svc.set_position_cache(
        order_id,
        {
            "order_id": order_id,
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "entry_price": "1.17500",
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "money_digits": "2",
            "is_stale": is_stale,
            "tick_age_ms": "100",
            "computed_at": str(int(time.time() * 1000)),
        },
    )


async def _seed_heartbeat(
    rc: fakeredis.aioredis.FakeRedis,
    account_id: str = "ftmo_001",
) -> None:
    await rc.set(f"client:ftmo:{account_id}", "online", ex=30)


async def _seed_tick(
    rc: fakeredis.aioredis.FakeRedis,
    symbol: str = "EURUSD",
    bid: float = 1.08400,
    ask: float = 1.08420,
) -> None:
    await rc.set(
        f"tick:{symbol}",
        json.dumps({"type": "tick", "symbol": symbol, "bid": bid, "ask": ask, "ts": 1}),
        ex=60,
    )


# ---------- list_orders ----------


@pytest.mark.asyncio
async def test_list_orders_empty(svc: OrderService) -> None:
    page, total = await svc.list_orders()
    assert page == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_orders_all_union(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """status='all' (default) → union of every per-status SET."""
    await _seed_order(redis_client, order_id="ord_p", status="pending")
    await _seed_order(redis_client, order_id="ord_f", status="filled")
    await _seed_order(redis_client, order_id="ord_c", status="closed")
    page, total = await svc.list_orders()
    ids = {o["order_id"] for o in page}
    assert ids == {"ord_p", "ord_f", "ord_c"}
    assert total == 3


@pytest.mark.asyncio
async def test_list_orders_filter_by_status_filled(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_p", status="pending")
    await _seed_order(redis_client, order_id="ord_f", status="filled")
    page, total = await svc.list_orders(status="filled")
    assert {o["order_id"] for o in page} == {"ord_f"}
    assert total == 1


@pytest.mark.asyncio
async def test_list_orders_unknown_status_returns_empty(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_f", status="filled")
    page, total = await svc.list_orders(status="nonsense")
    assert page == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_orders_filter_by_symbol(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_eu", symbol="EURUSD", status="filled")
    await _seed_order(redis_client, order_id="ord_gbp", symbol="GBPUSD", status="filled")
    page, total = await svc.list_orders(symbol="EURUSD")
    assert {o["order_id"] for o in page} == {"ord_eu"}
    assert total == 1


@pytest.mark.asyncio
async def test_list_orders_symbol_normalized_to_upper(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Caller passes lowercase 'eurusd' → matches stored 'EURUSD'."""
    await _seed_order(redis_client, order_id="ord_a", symbol="EURUSD", status="filled")
    page, total = await svc.list_orders(symbol="eurusd")
    assert total == 1


@pytest.mark.asyncio
async def test_list_orders_filter_by_account_id(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_a1", ftmo_account_id="ftmo_001", status="filled")
    await _seed_order(redis_client, order_id="ord_a2", ftmo_account_id="ftmo_002", status="filled")
    page, total = await svc.list_orders(account_id="ftmo_001")
    assert {o["order_id"] for o in page} == {"ord_a1"}
    assert total == 1


@pytest.mark.asyncio
async def test_list_orders_sort_created_at_desc(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_old", created_at=1, status="filled")
    await _seed_order(redis_client, order_id="ord_mid", created_at=2, status="filled")
    await _seed_order(redis_client, order_id="ord_new", created_at=3, status="filled")
    page, _ = await svc.list_orders()
    assert [o["order_id"] for o in page] == ["ord_new", "ord_mid", "ord_old"]


@pytest.mark.asyncio
async def test_list_orders_pagination(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    for i in range(5):
        await _seed_order(redis_client, order_id=f"ord_{i}", created_at=i, status="filled")
    page, total = await svc.list_orders(limit=2, offset=1)
    # Sorted DESC by created_at: ord_4, ord_3, ord_2, ord_1, ord_0.
    # offset=1, limit=2 → ord_3, ord_2.
    assert [o["order_id"] for o in page] == ["ord_3", "ord_2"]
    assert total == 5  # total = unpaged count


# ---------- get_order_by_id ----------


@pytest.mark.asyncio
async def test_get_order_by_id_happy(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_a", status="filled")
    order = await svc.get_order_by_id("ord_a")
    assert order["order_id"] == "ord_a"


@pytest.mark.asyncio
async def test_get_order_by_id_not_found_raises_404(svc: OrderService) -> None:
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.get_order_by_id("nope")
    assert exc_info.value.http_status == 404
    assert exc_info.value.error_code == "order_not_found"


# ---------- list_positions ----------


@pytest.mark.asyncio
async def test_list_positions_empty(svc: OrderService) -> None:
    assert await svc.list_positions() == []


@pytest.mark.asyncio
async def test_list_positions_enriches_with_position_cache(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(
        redis_client,
        order_id="ord_a",
        status="filled",
        p_fill_price="1.17500",
        p_executed_at="1735000050000",
        sl_price="1.07",
        tp_price="1.09",
    )
    await _seed_position_cache(redis_client, "ord_a", unrealized_pnl="100")
    positions = await svc.list_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["order_id"] == "ord_a"
    # Live fields from the cache.
    assert pos["current_price"] == "1.18000"
    assert pos["unrealized_pnl"] == "100"
    assert pos["is_stale"] == "false"
    # Static overlay from the order row.
    assert pos["sl_price"] == "1.07"
    assert pos["tp_price"] == "1.09"
    assert pos["p_executed_at"] == "1735000050000"


@pytest.mark.asyncio
async def test_list_positions_missing_cache_marks_stale(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Just-filled race: order is filled, position_cache not yet
    written. Position is still returned with empty live fields +
    is_stale=true so the row renders."""
    await _seed_order(redis_client, order_id="ord_a", status="filled", p_fill_price="1.17500")
    # No position_cache.
    positions = await svc.list_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["current_price"] == ""
    assert pos["unrealized_pnl"] == ""
    assert pos["is_stale"] == "true"


@pytest.mark.asyncio
async def test_list_positions_only_filled(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Pending / closed orders excluded."""
    await _seed_order(redis_client, order_id="ord_p", status="pending")
    await _seed_order(redis_client, order_id="ord_c", status="closed")
    await _seed_order(redis_client, order_id="ord_f", status="filled")
    positions = await svc.list_positions()
    assert {p["order_id"] for p in positions} == {"ord_f"}


@pytest.mark.asyncio
async def test_list_positions_filter_by_account_id(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_a1", status="filled", ftmo_account_id="ftmo_001")
    await _seed_order(redis_client, order_id="ord_a2", status="filled", ftmo_account_id="ftmo_002")
    positions = await svc.list_positions(account_id="ftmo_001")
    assert {p["order_id"] for p in positions} == {"ord_a1"}


@pytest.mark.asyncio
async def test_list_positions_filter_by_symbol(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_eu", status="filled", symbol="EURUSD")
    await _seed_order(redis_client, order_id="ord_gbp", status="filled", symbol="GBPUSD")
    positions = await svc.list_positions(symbol="EURUSD")
    assert {p["order_id"] for p in positions} == {"ord_eu"}


@pytest.mark.asyncio
async def test_list_positions_sort_by_executed_at_desc(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_old", status="filled", p_executed_at="1")
    await _seed_order(redis_client, order_id="ord_new", status="filled", p_executed_at="2")
    positions = await svc.list_positions()
    assert [p["order_id"] for p in positions] == ["ord_new", "ord_old"]


# ---------- list_history ----------


@pytest.mark.asyncio
async def test_list_history_filter_by_time_range(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_a", status="closed", p_closed_at="100")
    await _seed_order(redis_client, order_id="ord_b", status="closed", p_closed_at="200")
    await _seed_order(redis_client, order_id="ord_c", status="closed", p_closed_at="300")
    page, total = await svc.list_history(from_ts=150, to_ts=250)
    assert {o["order_id"] for o in page} == {"ord_b"}
    assert total == 1


@pytest.mark.asyncio
async def test_list_history_inclusive_bounds(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Orders exactly at from_ts and to_ts are INCLUDED."""
    await _seed_order(redis_client, order_id="ord_a", status="closed", p_closed_at="100")
    await _seed_order(redis_client, order_id="ord_b", status="closed", p_closed_at="200")
    page, total = await svc.list_history(from_ts=100, to_ts=200)
    assert total == 2


@pytest.mark.asyncio
async def test_list_history_filter_by_symbol(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(
        redis_client, order_id="ord_e", status="closed", symbol="EURUSD", p_closed_at="1"
    )
    await _seed_order(
        redis_client, order_id="ord_g", status="closed", symbol="GBPUSD", p_closed_at="1"
    )
    page, _ = await svc.list_history(from_ts=0, to_ts=999, symbol="EURUSD")
    assert {o["order_id"] for o in page} == {"ord_e"}


@pytest.mark.asyncio
async def test_list_history_skips_orders_without_p_closed_at(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, order_id="ord_a", status="closed", p_closed_at="")
    page, total = await svc.list_history(from_ts=0, to_ts=9999999999)
    assert page == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_history_pagination(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    for i in range(5):
        await _seed_order(
            redis_client,
            order_id=f"ord_{i}",
            status="closed",
            p_closed_at=str(100 + i),
        )
    page, total = await svc.list_history(from_ts=0, to_ts=999, limit=2, offset=1)
    # Sorted DESC: ord_4, ord_3, ord_2, ord_1, ord_0 → offset=1, limit=2.
    assert [o["order_id"] for o in page] == ["ord_3", "ord_2"]
    assert total == 5


# ---------- close_order ----------


@pytest.mark.asyncio
async def test_close_order_not_found_raises_404(svc: OrderService) -> None:
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.close_order("nope")
    assert exc_info.value.http_status == 404
    assert exc_info.value.error_code == "order_not_found"


@pytest.mark.asyncio
async def test_close_order_pending_raises_not_closeable(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """Step 4.8 — composed-status guard fires before the per-leg
    ``p_status != filled`` check. A pending order rejects with the new
    ``order_not_closeable`` error_code (more specific than the legacy
    ``invalid_state`` slug, per acceptance criterion §3 #35)."""
    await _seed_order(redis_client, status="pending")
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.close_order("ord_a")
    assert exc_info.value.error_code == "order_not_closeable"
    assert exc_info.value.http_status == 400


@pytest.mark.asyncio
async def test_close_order_partial_volume_raises(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(
        redis_client,
        status="filled",
        p_broker_order_id="5451198",
        p_volume_lots="0.10",
    )
    await _seed_heartbeat(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.close_order("ord_a", volume_lots=0.05)
    assert exc_info.value.error_code == "partial_close_unsupported"


@pytest.mark.asyncio
async def test_close_order_client_offline_raises_409(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, status="filled", p_broker_order_id="5451198")
    # No heartbeat.
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.close_order("ord_a")
    assert exc_info.value.error_code == "client_offline"
    assert exc_info.value.http_status == 409


@pytest.mark.asyncio
async def test_close_order_missing_broker_order_id_raises_500(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, status="filled", p_broker_order_id="")
    await _seed_heartbeat(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.close_order("ord_a")
    assert exc_info.value.error_code == "order_corrupt"
    assert exc_info.value.http_status == 500


@pytest.mark.asyncio
async def test_close_order_happy_path_pushes_cmd_stream_and_links_index(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(
        redis_client,
        status="filled",
        p_broker_order_id="5451198",
        p_volume_lots="0.01",
    )
    await _seed_heartbeat(redis_client)

    oid, rid = await svc.close_order("ord_a")
    assert oid == "ord_a"
    assert len(rid) == 32  # uuid4 hex

    # cmd_stream entry pushed.
    entries = await redis_client.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["action"] == "close"
    assert fields["broker_order_id"] == "5451198"
    assert fields["volume_lots"] == "0.01"
    assert fields["request_id"] == rid

    # request_id → order_id side-index.
    linked = await redis_client.get(f"request_id_to_order:{rid}")
    assert linked == "ord_a"


@pytest.mark.asyncio
async def test_close_order_with_matching_volume_accepted(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(
        redis_client,
        status="filled",
        p_broker_order_id="5451198",
        p_volume_lots="0.01",
    )
    await _seed_heartbeat(redis_client)
    oid, _ = await svc.close_order("ord_a", volume_lots=0.01)
    assert oid == "ord_a"


# ---------- modify_order ----------


@pytest.mark.asyncio
async def test_modify_order_neither_sl_nor_tp_raises(svc: OrderService) -> None:
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.modify_order("ord_a", sl=None, tp=None)
    assert exc_info.value.error_code == "missing_field"


@pytest.mark.asyncio
async def test_modify_order_not_found_raises_404(svc: OrderService) -> None:
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.modify_order("nope", sl=1.0, tp=None)
    assert exc_info.value.http_status == 404
    assert exc_info.value.error_code == "order_not_found"


@pytest.mark.asyncio
async def test_modify_order_pending_raises_invalid_state(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, status="pending")
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.modify_order("ord_a", sl=1.0, tp=None)
    assert exc_info.value.error_code == "invalid_state"


@pytest.mark.asyncio
async def test_modify_order_sl_above_bid_for_buy_raises(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(
        redis_client,
        status="filled",
        side="buy",
        p_broker_order_id="5451198",
    )
    await _seed_heartbeat(redis_client)
    await _seed_tick(redis_client)  # bid=1.084, ask=1.0842
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.modify_order("ord_a", sl=1.090, tp=None)
    assert exc_info.value.error_code == "invalid_sl_direction"


@pytest.mark.asyncio
async def test_modify_order_remove_sl_zero_skips_direction_check(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """sl=0 means 'remove SL' → no tick needed for direction validation."""
    await _seed_order(
        redis_client,
        status="filled",
        side="buy",
        p_broker_order_id="5451198",
    )
    await _seed_heartbeat(redis_client)
    # No tick seeded.
    oid, _ = await svc.modify_order("ord_a", sl=0.0, tp=None)
    assert oid == "ord_a"


@pytest.mark.asyncio
async def test_modify_order_no_tick_data_raises_409(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, status="filled", side="buy", p_broker_order_id="5451198")
    await _seed_heartbeat(redis_client)
    # No tick seeded.
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.modify_order("ord_a", sl=1.07, tp=None)
    assert exc_info.value.error_code == "no_tick_data"


@pytest.mark.asyncio
async def test_modify_order_happy_path_pushes_cmd_stream(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(
        redis_client,
        status="filled",
        side="buy",
        p_broker_order_id="5451198",
        sl_price="1.07",
        tp_price="1.09",
    )
    await _seed_heartbeat(redis_client)
    await _seed_tick(redis_client)
    oid, rid = await svc.modify_order("ord_a", sl=1.075, tp=None)
    assert oid == "ord_a"
    entries = await redis_client.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["action"] == "modify_sl_tp"
    assert fields["sl"] == "1.075"
    # tp unchanged from order's tp_price.
    assert fields["tp"] == "1.09"


@pytest.mark.asyncio
async def test_modify_order_client_offline_raises_409(
    svc: OrderService, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    await _seed_order(redis_client, status="filled", side="buy", p_broker_order_id="5451198")
    # No heartbeat.
    await _seed_tick(redis_client)
    with pytest.raises(OrderValidationError) as exc_info:
        await svc.modify_order("ord_a", sl=1.07, tp=None)
    assert exc_info.value.error_code == "client_offline"
