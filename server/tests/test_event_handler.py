"""Tests for ``app.services.event_handler`` (step 3.7).

Covers all 5 event_type branches + the reconcile-snapshot diff
flow, including:

  - D-074 invariant: ``realized_pnl`` copied verbatim from event,
    NOT recomputed.
  - D-061 pending → filled side-index migration.
  - D-080 ``order_cancelled`` noise filter for cTrader-internal
    STOP_LOSS_TAKE_PROFIT cleanup.
  - Race tolerance: live ``position_closed`` event then a
    reconcile_snapshot — Redis already closed, no double-dispatch.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import fakeredis.aioredis
import pytest
import pytest_asyncio
from app.services.broadcast import BroadcastService
from app.services.event_handler import (
    _handle_event_entry,
    event_handler_loop,
)
from app.services.redis_service import RedisService

# ---------- fixtures (mirror response_handler test rig) ----------


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


class _CapturingBroadcast(BroadcastService):
    def __init__(self) -> None:
        super().__init__(redis_svc=None)
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        self.published.append((channel, data))


@pytest.fixture
def broadcast() -> _CapturingBroadcast:
    return _CapturingBroadcast()


async def _seed_filled_order(
    redis_svc: RedisService,
    *,
    order_id: str = "ord_abc",
    p_broker_order_id: str = "5451198",
    p_status: str = "filled",
    status: str = "filled",
    ftmo_account_id: str = "ftmo_001",
    extra: dict[str, str] | None = None,
) -> None:
    # Step 4.8 (§2.11 fixture spirit): default to a single-leg FTMO order
    # (``exness_account_id=""``). The Phase 3 FTMO position_closed
    # handler stamps composed ``status="closed"`` for single-leg orders;
    # hedge orders go through cascade_close_other_leg instead. Tests
    # exercising the legacy single-leg flow keep the default empty;
    # hedge-flow tests override via ``extra``.
    fields: dict[str, str] = {
        "order_id": order_id,
        "pair_id": "pair_001",
        "ftmo_account_id": ftmo_account_id,
        "exness_account_id": "",
        "symbol": "EURUSD",
        "side": "buy",
        "order_type": "market",
        "status": status,
        "p_status": p_status,
        "p_volume_lots": "0.01",
        "p_broker_order_id": p_broker_order_id,
        "created_at": "1735000000000",
        "updated_at": "1735000000000",
    }
    if extra:
        fields.update(extra)
    await redis_svc.create_order(order_id, fields)
    await redis_svc.link_broker_order_id("p", p_broker_order_id, order_id)


# ---------- position_closed ----------


@pytest.mark.asyncio
async def test_position_closed_live_event_updates_extended_fields(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> None:
    """Step 3.5a extended payload: all 5 close fields persisted +
    close_reason recorded."""
    await _seed_filled_order(redis_svc)
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "position_closed",
            "position_id": "5451198",
            "broker_order_id": "5451198",
            "close_price": "1.08600",
            "close_time": "1735000100000",
            "realized_pnl": "1840",
            "commission": "-766",
            "swap": "0",
            "balance_after_close": "942589",
            "money_digits": "2",
            "closed_volume": "100000",
            "close_reason": "manual",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "closed"
    assert order["status"] == "closed"
    assert order["p_close_price"] == "1.08600"
    assert order["p_realized_pnl"] == "1840"
    assert order["p_commission"] == "-766"
    assert order["p_swap"] == "0"
    assert order["p_balance_after_close"] == "942589"
    assert order["p_money_digits"] == "2"
    assert order["p_closed_volume"] == "100000"
    assert order["p_close_reason"] == "manual"
    chan, data = broadcast.published[0]
    assert chan == "positions"
    assert data["type"] == "position_event"
    assert data["event_type"] == "closed"


@pytest.mark.asyncio
async def test_position_closed_d074_realized_pnl_verbatim(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> None:
    """D-074 invariant pin: realized_pnl on the order row matches
    the event field byte-for-byte; never recomputed from
    (close_price - entry_price) arithmetic."""
    await _seed_filled_order(redis_svc, extra={"sl_price": "1.07", "tp_price": "1.09"})
    # Deliberately make grossProfit a "weird" number that would NEVER
    # come from (close_price - entry_price) — proves no recomputation.
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "position_closed",
            "position_id": "5451198",
            "close_price": "1.08600",
            "close_time": "1735000100000",
            "realized_pnl": "424242",  # unrelated to price math
            "commission": "0",
            "swap": "0",
            "balance_after_close": "0",
            "close_reason": "tp",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_realized_pnl"] == "424242"
    chan, data = broadcast.published[0]
    assert data["realized_pnl"] == "424242"


@pytest.mark.asyncio
async def test_position_closed_reconstructed_sets_flag(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> None:
    """Step 3.5b: reconstructed=true flag preserved on the order row +
    WS message."""
    await _seed_filled_order(redis_svc)
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "position_closed",
            "position_id": "5451198",
            "close_price": "1.08600",
            "close_time": "1735000100000",
            "realized_pnl": "100",
            "commission": "0",
            "swap": "0",
            "balance_after_close": "0",
            "close_reason": "unknown",
            "reconstructed": "true",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_reconstructed"] == "true"
    _chan, data = broadcast.published[0]
    assert data["reconstructed"] is True


@pytest.mark.asyncio
async def test_position_closed_unknown_position_id_warns(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        await _handle_event_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {
                "event_type": "position_closed",
                "position_id": "9999999",
                "realized_pnl": "0",
            },
        )
    assert "unknown position_id" in caplog.text
    assert broadcast.published == []


@pytest.mark.asyncio
async def test_position_closed_missing_position_id_warns(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        await _handle_event_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {"event_type": "position_closed"},
        )
    assert "missing position_id" in caplog.text


# ---------- pending_filled ----------


@pytest.mark.asyncio
async def test_pending_filled_swaps_broker_order_id_and_migrates_index(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """D-061: pending order's orderId is replaced by the new
    positionId on fill. Both the order-row field AND the
    side-index entry must migrate."""
    await _seed_filled_order(
        redis_svc,
        p_broker_order_id="8324918",  # old orderId
        p_status="pending",
        status="pending",
        extra={"order_type": "limit"},
    )
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "pending_filled",
            "order_id_old": "8324918",
            "position_id": "5451300",
            "broker_order_id": "5451300",
            "fill_price": "1.07000",
            "fill_time": "1735000050000",
            "commission": "-5",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "filled"
    assert order["p_broker_order_id"] == "5451300"  # NEW
    assert order["p_fill_price"] == "1.07000"
    # Old index gone, new index in place.
    old = await redis_client.get("p_broker_order_id_to_order:8324918")
    new = await redis_client.get("p_broker_order_id_to_order:5451300")
    assert old is None
    assert new == "ord_abc"
    chan, data = broadcast.published[0]
    assert chan == "positions"
    assert data["event_type"] == "pending_filled"


@pytest.mark.asyncio
async def test_pending_filled_unknown_order_id_old_warns(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        await _handle_event_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {
                "event_type": "pending_filled",
                "order_id_old": "9999999",
                "position_id": "5451300",
            },
        )
    assert "unknown order_id_old" in caplog.text


@pytest.mark.asyncio
async def test_pending_filled_missing_ids_warns(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        await _handle_event_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {"event_type": "pending_filled", "position_id": "5451300"},  # missing order_id_old
        )
    assert "missing IDs" in caplog.text


# ---------- position_modified ----------


@pytest.mark.asyncio
async def test_position_modified_updates_sl_tp(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> None:
    await _seed_filled_order(redis_svc, extra={"sl_price": "1.07", "tp_price": "1.09"})
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "position_modified",
            "position_id": "5451198",
            "new_sl": "1.07500",
            "new_tp": "1.09500",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["sl_price"] == "1.07500"
    assert order["tp_price"] == "1.09500"
    chan, data = broadcast.published[0]
    assert chan == "positions"
    assert data["event_type"] == "modified"


@pytest.mark.asyncio
async def test_position_modified_clears_one_side(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> None:
    """Operator clears TP via cTrader UI → new_tp empty → tp_price=''."""
    await _seed_filled_order(redis_svc, extra={"sl_price": "1.07", "tp_price": "1.09"})
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "position_modified",
            "position_id": "5451198",
            "new_sl": "1.07500",
            "new_tp": "",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["sl_price"] == "1.07500"
    assert order["tp_price"] == ""


@pytest.mark.asyncio
async def test_position_modified_unknown_position_id_warns(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        await _handle_event_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {"event_type": "position_modified", "position_id": "9999"},
        )
    assert "unknown position_id" in caplog.text


# ---------- order_cancelled (D-080) ----------


@pytest.mark.asyncio
async def test_order_cancelled_unknown_broker_order_id_silently_ignored(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-080: cTrader-internal STOP_LOSS_TAKE_PROFIT cleanup events
    arrive with broker_order_ids that have no matching Redis row.
    Must drop silently — no warning log, no state mutation."""
    with caplog.at_level("DEBUG"):
        await _handle_event_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {"event_type": "order_cancelled", "broker_order_id": "internal_id"},
        )
    # Debug log allowed (not warning).
    assert "WARNING" not in caplog.text.upper().replace("ASSIGN", "")
    assert broadcast.published == []


@pytest.mark.asyncio
async def test_order_cancelled_matching_pending_order_marks_cancelled(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """User cancels their own pending limit/stop order via cTrader UI
    → broker_order_id matches our Redis side-index → status flips to
    cancelled, side-index dropped."""
    await _seed_filled_order(
        redis_svc,
        p_broker_order_id="8324918",
        p_status="pending",
        status="pending",
        extra={"order_type": "limit"},
    )
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {"event_type": "order_cancelled", "broker_order_id": "8324918"},
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "cancelled"
    assert order["status"] == "cancelled"
    # Side-index dropped.
    assert await redis_client.get("p_broker_order_id_to_order:8324918") is None
    chan, data = broadcast.published[0]
    assert chan == "orders"
    assert data["p_status"] == "cancelled"


@pytest.mark.asyncio
async def test_order_cancelled_missing_broker_order_id_drops(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> None:
    """Defensive — bridge always sets the field, but if absent the
    handler must drop without crashing."""
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {"event_type": "order_cancelled"},
    )
    assert broadcast.published == []


# ---------- reconcile_snapshot ----------


async def _seed_two_filled_orders_one_pending(redis_svc: RedisService) -> None:
    """Setup state: 2 filled orders + 1 pending order for ftmo_001."""
    await _seed_filled_order(redis_svc, order_id="ord_a", p_broker_order_id="5451100")
    await _seed_filled_order(redis_svc, order_id="ord_b", p_broker_order_id="5451200")
    await _seed_filled_order(
        redis_svc,
        order_id="ord_c",
        p_broker_order_id="8324900",
        p_status="pending",
        status="pending",
        extra={"order_type": "limit"},
    )


@pytest.mark.asyncio
async def test_reconcile_all_open_orders_present_no_dispatch(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Snapshot covers every Redis open order → no fetch_close_history
    dispatched + no pending→unknown transitions."""
    await _seed_two_filled_orders_one_pending(redis_svc)
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "reconcile_snapshot",
            "positions": json.dumps(
                [
                    {"position_id": "5451100", "symbol_id": "1"},
                    {"position_id": "5451200", "symbol_id": "1"},
                ]
            ),
            "pending_orders": json.dumps([{"order_id": "8324900", "symbol_id": "1"}]),
        },
    )
    # No cmd_stream entries dispatched.
    assert (await redis_client.xlen("cmd_stream:ftmo:ftmo_001")) == 0
    # Orders unchanged.
    a = await redis_svc.get_order("ord_a")
    c = await redis_svc.get_order("ord_c")
    assert a is not None and a["p_status"] == "filled"
    assert c is not None and c["p_status"] == "pending"


@pytest.mark.asyncio
async def test_reconcile_filled_missing_dispatches_fetch_close_history(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """One filled Redis order missing from snapshot → fetch_close_history
    command pushed to cmd_stream:ftmo:ftmo_001."""
    await _seed_two_filled_orders_one_pending(redis_svc)
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "reconcile_snapshot",
            "positions": json.dumps(
                [{"position_id": "5451100", "symbol_id": "1"}]
            ),  # ord_b's 5451200 missing
            "pending_orders": json.dumps([{"order_id": "8324900", "symbol_id": "1"}]),
        },
    )
    entries = await redis_client.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    assert len(entries) == 1
    _entry_id, cmd_fields = entries[0]
    assert cmd_fields["action"] == "fetch_close_history"
    assert cmd_fields["order_id"] == "ord_b"
    assert cmd_fields["broker_order_id"] == "5451200"


@pytest.mark.asyncio
async def test_reconcile_pending_missing_marks_unknown(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Pending Redis order missing from snapshot → status=unknown +
    WS broadcast, no fetch_close_history."""
    await _seed_two_filled_orders_one_pending(redis_svc)
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "reconcile_snapshot",
            "positions": json.dumps(
                [
                    {"position_id": "5451100", "symbol_id": "1"},
                    {"position_id": "5451200", "symbol_id": "1"},
                ]
            ),
            "pending_orders": json.dumps([]),  # ord_c's 8324900 missing
        },
    )
    order = await redis_svc.get_order("ord_c")
    assert order is not None
    assert order["p_status"] == "unknown"
    # No fetch_close_history dispatched for pending orders.
    assert (await redis_client.xlen("cmd_stream:ftmo:ftmo_001")) == 0
    # WS broadcast with reason.
    chan, data = broadcast.published[-1]
    assert chan == "orders"
    assert data["p_status"] == "unknown"
    assert data["reason"] == "missing_after_reconcile"


@pytest.mark.asyncio
async def test_reconcile_invalid_json_logs_error_no_crash(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("ERROR"):
        await _handle_event_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {
                "event_type": "reconcile_snapshot",
                "positions": "{not-json",
                "pending_orders": "[]",
            },
        )
    assert "invalid JSON" in caplog.text


@pytest.mark.asyncio
async def test_reconcile_other_account_orders_not_diffed(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Orders belonging to a different account_id must NOT be diffed
    by this snapshot — otherwise multi-account setups would
    cross-contaminate."""
    await _seed_filled_order(
        redis_svc,
        order_id="ord_other_acc",
        p_broker_order_id="9999",
        ftmo_account_id="ftmo_002",
    )
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "reconcile_snapshot",
            "positions": json.dumps([]),
            "pending_orders": json.dumps([]),
        },
    )
    # No dispatches — the ord_other_acc belongs to ftmo_002.
    assert (await redis_client.xlen("cmd_stream:ftmo:ftmo_001")) == 0
    assert (await redis_client.xlen("cmd_stream:ftmo:ftmo_002")) == 0


@pytest.mark.asyncio
async def test_reconcile_race_live_close_first_then_snapshot(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Race tolerance: if position_closed live event processed FIRST
    (Redis order → status=closed) and reconcile_snapshot processed
    LATER without the position, the snapshot must NOT re-dispatch
    fetch_close_history. Verifies serial single-loop event handling
    handles the race correctly (smoke 3.5b Q2)."""
    await _seed_filled_order(redis_svc)
    # Live close arrives first.
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "position_closed",
            "position_id": "5451198",
            "close_price": "1.086",
            "close_time": "1735000100000",
            "realized_pnl": "100",
            "commission": "0",
            "swap": "0",
            "balance_after_close": "0",
            "close_reason": "manual",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "closed"
    # Then a snapshot WITHOUT this position arrives.
    await _handle_event_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "event_type": "reconcile_snapshot",
            "positions": json.dumps([]),
            "pending_orders": json.dumps([]),
        },
    )
    # No fetch_close_history dispatched — the order is in
    # orders:by_status:closed, not :pending/:filled, so the diff
    # doesn't see it.
    assert (await redis_client.xlen("cmd_stream:ftmo:ftmo_001")) == 0


# ---------- routing edge cases ----------


@pytest.mark.asyncio
async def test_unknown_event_type_warns(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        await _handle_event_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {"event_type": "telepathy"},
        )
    assert "unknown event_type" in caplog.text


# ---------- loop semantics ----------


@pytest_asyncio.fixture
async def consumer_groups(redis_svc: RedisService) -> None:
    await redis_svc.add_account("ftmo", "ftmo_001", name="t")
    await redis_svc.setup_consumer_groups()


@pytest.mark.asyncio
async def test_loop_acks_processed_entries(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
    consumer_groups: None,
) -> None:
    await _seed_filled_order(redis_svc)
    await redis_client.xadd(
        "event_stream:ftmo:ftmo_001",
        {
            "event_type": "position_closed",
            "position_id": "5451198",
            "close_price": "1.086",
            "close_time": "1735000100000",
            "realized_pnl": "100",
            "commission": "0",
            "swap": "0",
            "balance_after_close": "0",
            "close_reason": "manual",
        },
    )
    task = asyncio.create_task(event_handler_loop(redis_svc, broadcast, "ftmo_001", block_ms=50))
    for _ in range(50):
        order = await redis_svc.get_order("ord_abc")
        if order and order.get("p_status") == "closed":
            break
        await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    pending = await redis_client.xpending("event_stream:ftmo:ftmo_001", "server")
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_loop_does_not_ack_on_handler_exception(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
    consumer_groups: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_args: Any, **_kw: Any) -> None:
        raise RuntimeError("simulated handler crash")

    monkeypatch.setattr("app.services.event_handler._handle_event_entry", boom)
    await redis_client.xadd(
        "event_stream:ftmo:ftmo_001",
        {"event_type": "position_closed"},
    )
    task = asyncio.create_task(event_handler_loop(redis_svc, broadcast, "ftmo_001", block_ms=50))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    pending = await redis_client.xpending("event_stream:ftmo:ftmo_001", "server")
    assert pending["pending"] == 1


@pytest.mark.asyncio
async def test_loop_exits_on_cancel(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    consumer_groups: None,
) -> None:
    task = asyncio.create_task(event_handler_loop(redis_svc, broadcast, "ftmo_001", block_ms=50))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
