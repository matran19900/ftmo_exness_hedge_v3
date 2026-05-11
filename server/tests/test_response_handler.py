"""Tests for ``app.services.response_handler`` (step 3.7).

Direct service tests — invoke the entry-router or the loop with a
real ``RedisService`` (fakeredis) + a capturing ``BroadcastService``
stand-in. No FastAPI app, no real network.

Tests cover:
  - Each action branch (open, close, modify_sl_tp, fetch_close_history)
    × status (success / error) → correct order-row update + WS publish.
  - Unknown request_id / unknown action paths (warn-and-drop).
  - Loop ACK semantics (ACK after success, no ACK on handler exception).
  - Loop shutdown via Task.cancel.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
import pytest_asyncio
from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService
from app.services.response_handler import (
    _handle_response_entry,
    response_handler_loop,
)

# ---------- fixtures ----------


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


class _CapturingBroadcast(BroadcastService):
    """``BroadcastService`` subclass that records every publish() call
    instead of actually fanning out to WebSocket connections.

    Tests assert on ``.published`` (list of ``(channel, data)`` tuples)
    to verify the right messages were emitted with the right fields.
    """

    def __init__(self) -> None:
        super().__init__(redis_svc=None)
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        self.published.append((channel, data))


@pytest.fixture
def broadcast() -> _CapturingBroadcast:
    return _CapturingBroadcast()


# ---------- helpers ----------


async def _seed_order_and_link(
    redis_svc: RedisService,
    *,
    order_id: str = "ord_abc",
    request_id: str = "req_xyz",
    status: str = "pending",
    p_status: str = "pending",
    extra: dict[str, str] | None = None,
) -> None:
    """Create a minimal order row + request_id → order_id side index."""
    fields: dict[str, str] = {
        "order_id": order_id,
        "pair_id": "pair_001",
        "ftmo_account_id": "ftmo_001",
        "exness_account_id": "exness_001",
        "symbol": "EURUSD",
        "side": "buy",
        "order_type": "market",
        "status": status,
        "p_status": p_status,
        "p_volume_lots": "0.01",
        "created_at": "1735000000000",
        "updated_at": "1735000000000",
    }
    if extra:
        fields.update(extra)
    await redis_svc.create_order(order_id, fields)
    await redis_svc.link_request_to_order(request_id, order_id)


# ---------- open response branches ----------


@pytest.mark.asyncio
async def test_open_success_market_fill_updates_order_and_links_index(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_order_and_link(redis_svc)
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "action": "open",
            "request_id": "req_xyz",
            "status": "success",
            "broker_order_id": "5451198",
            "fill_price": "1.08412",
            "fill_time": "1735000050000",
            "commission": "-5",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "filled"
    assert order["status"] == "filled"
    assert order["p_broker_order_id"] == "5451198"
    assert order["p_fill_price"] == "1.08412"
    # Side-index created.
    linked = await redis_client.get("p_broker_order_id_to_order:5451198")
    assert linked == "ord_abc"
    # WS publish on orders channel.
    assert len(broadcast.published) == 1
    chan, data = broadcast.published[0]
    assert chan == "orders"
    assert data["type"] == "order_updated"
    assert data["p_status"] == "filled"


@pytest.mark.asyncio
async def test_open_success_pending_limit_has_no_fill_price(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Limit/stop accepted-but-not-filled response: empty fill_price
    → p_status=pending, status=pending."""
    await _seed_order_and_link(redis_svc, extra={"order_type": "limit"})
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "action": "open",
            "request_id": "req_xyz",
            "status": "success",
            "broker_order_id": "8324918",  # orderId (pending phase)
            "fill_price": "",
            "fill_time": "",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "pending"
    assert order["status"] == "pending"
    assert order["p_broker_order_id"] == "8324918"


@pytest.mark.asyncio
async def test_open_success_with_sl_tp_attach_failed_flags_warning(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 3.4a: fill succeeded but the post-fill amend was rejected."""
    await _seed_order_and_link(redis_svc)
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "action": "open",
            "request_id": "req_xyz",
            "status": "success",
            "broker_order_id": "5451198",
            "fill_price": "1.08412",
            "fill_time": "1735000050000",
            "sl_tp_attach_failed": "true",
            "sl_tp_attach_error_msg": "SL too close",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "filled"
    assert order["p_sl_tp_warning"] == "true"
    assert order["p_sl_tp_warning_msg"] == "SL too close"
    _chan, data = broadcast.published[0]
    assert data["p_sl_tp_warning"] is True
    assert data["p_sl_tp_warning_msg"] == "SL too close"


@pytest.mark.asyncio
async def test_open_error_marks_order_rejected(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_order_and_link(redis_svc)
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "action": "open",
            "request_id": "req_xyz",
            "status": "error",
            "error_code": "not_enough_money",
            "error_msg": "Free margin insufficient",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "rejected"
    assert order["status"] == "rejected"
    assert order["p_error_code"] == "not_enough_money"
    chan, data = broadcast.published[0]
    assert chan == "orders"
    assert data["p_status"] == "rejected"
    assert data["error_code"] == "not_enough_money"


@pytest.mark.asyncio
async def test_open_unknown_status_logged_and_dropped(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _seed_order_and_link(redis_svc)
    with caplog.at_level("WARNING"):
        await _handle_response_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {"action": "open", "request_id": "req_xyz", "status": "weird"},
        )
    assert "unknown status in open" in caplog.text
    # No update, no publish.
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "pending"  # unchanged
    assert broadcast.published == []


# ---------- close response branches ----------


@pytest.mark.asyncio
async def test_close_success_sets_realized_pnl_verbatim(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """D-074: realized_pnl is copied from response field, NOT
    recomputed from price arithmetic."""
    await _seed_order_and_link(redis_svc, p_status="filled", status="filled")
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "action": "close",
            "request_id": "req_xyz",
            "status": "success",
            "close_price": "1.08600",
            "close_time": "1735000100000",
            "realized_pnl": "1840",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "closed"
    assert order["status"] == "closed"
    assert order["p_close_price"] == "1.08600"
    assert order["p_realized_pnl"] == "1840"  # verbatim — NOT recomputed
    chan, data = broadcast.published[0]
    assert chan == "orders"
    assert data["p_realized_pnl"] == "1840"


@pytest.mark.asyncio
async def test_close_error_sets_close_error_fields(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_order_and_link(redis_svc, p_status="filled", status="filled")
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "action": "close",
            "request_id": "req_xyz",
            "status": "error",
            "error_code": "position_not_found",
            "error_msg": "no such position",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    # Status unchanged on error — operator can investigate.
    assert order["p_status"] == "filled"
    assert order["p_close_error_code"] == "position_not_found"
    assert order["p_close_error_msg"] == "no such position"


# ---------- modify_sl_tp response branches ----------


@pytest.mark.asyncio
async def test_modify_success_updates_sl_tp(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_order_and_link(redis_svc, p_status="filled", status="filled")
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "action": "modify_sl_tp",
            "request_id": "req_xyz",
            "status": "success",
            "new_sl": "1.07500",
            "new_tp": "1.09000",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["sl_price"] == "1.07500"
    assert order["tp_price"] == "1.09000"
    chan, data = broadcast.published[0]
    assert chan == "orders"
    assert data["sl_price"] == "1.07500"


@pytest.mark.asyncio
async def test_modify_error_sets_error_fields(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_order_and_link(redis_svc, p_status="filled", status="filled")
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "action": "modify_sl_tp",
            "request_id": "req_xyz",
            "status": "error",
            "error_code": "invalid_sl_distance",
            "error_msg": "SL too close",
        },
    )
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_modify_error_code"] == "invalid_sl_distance"
    assert "modify_error_code" in broadcast.published[0][1]


# ---------- fetch_close_history response branches ----------


@pytest.mark.asyncio
async def test_fetch_close_history_success_logs_no_state_change(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _seed_order_and_link(redis_svc, p_status="filled", status="filled")
    with caplog.at_level("INFO"):
        await _handle_response_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {
                "action": "fetch_close_history",
                "request_id": "req_xyz",
                "status": "success",
                "position_id": "5451198",
            },
        )
    assert "fetch_close_history success" in caplog.text
    order = await redis_svc.get_order("ord_abc")
    assert order is not None
    assert order["p_status"] == "filled"  # unchanged — close event will set
    assert broadcast.published == []  # no WS message — the reconstructed close event handles it


@pytest.mark.asyncio
async def test_fetch_close_history_error_publishes_ws(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_order_and_link(redis_svc, p_status="filled", status="filled")
    await _handle_response_entry(
        redis_svc,
        broadcast,
        "ftmo_001",
        {
            "action": "fetch_close_history",
            "request_id": "req_xyz",
            "status": "error",
            "error_code": "not_found",
            "error_msg": "no close deal for position 5451198",
        },
    )
    chan, data = broadcast.published[0]
    assert chan == "orders"
    assert data["fetch_close_history_error_code"] == "not_found"


# ---------- routing edge cases ----------


@pytest.mark.asyncio
async def test_response_missing_request_id_warns_no_state_change(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        await _handle_response_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {"action": "open", "status": "success"},
        )
    assert "missing request_id" in caplog.text
    assert broadcast.published == []


@pytest.mark.asyncio
async def test_response_unknown_request_id_warns_drops(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """request_id with no matching side index entry (e.g. TTL expired)."""
    with caplog.at_level("WARNING"):
        await _handle_response_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {
                "action": "open",
                "request_id": "req_never_seen",
                "status": "success",
            },
        )
    assert "unknown request_id" in caplog.text
    assert broadcast.published == []


@pytest.mark.asyncio
async def test_response_unknown_action_warns(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _seed_order_and_link(redis_svc)
    with caplog.at_level("WARNING"):
        await _handle_response_entry(
            redis_svc,
            broadcast,
            "ftmo_001",
            {
                "action": "telepathy",
                "request_id": "req_xyz",
                "status": "success",
            },
        )
    assert "unknown action" in caplog.text


# ---------- loop ACK semantics ----------


@pytest_asyncio.fixture
async def consumer_groups(redis_svc: RedisService) -> None:
    """Pre-create consumer groups on resp_stream + event_stream for ftmo_001."""
    # Seed the account so setup_consumer_groups picks it up.
    await redis_svc.add_account("ftmo", "ftmo_001", name="t")
    await redis_svc.setup_consumer_groups()


@pytest.mark.asyncio
async def test_loop_acks_processed_entries(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
    consumer_groups: None,
) -> None:
    """Drive one resp_stream entry through the loop + assert ACK by
    checking the pending-list length goes to 0 after one iteration."""
    await _seed_order_and_link(redis_svc)

    # Push a success response.
    await redis_client.xadd(
        "resp_stream:ftmo:ftmo_001",
        {
            "action": "open",
            "request_id": "req_xyz",
            "status": "success",
            "broker_order_id": "5451198",
            "fill_price": "1.08412",
            "fill_time": "1735000050000",
        },
    )

    task = asyncio.create_task(response_handler_loop(redis_svc, broadcast, "ftmo_001", block_ms=50))
    # Wait for the order to flip to filled — observable side effect
    # that the loop completed one iteration successfully.
    for _ in range(50):
        order = await redis_svc.get_order("ord_abc")
        if order and order.get("p_status") == "filled":
            break
        await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Pending Entries List (PEL) for the consumer group should be
    # empty after ACK — XPENDING returns count=0.
    pending = await redis_client.xpending("resp_stream:ftmo:ftmo_001", "server")
    assert pending["pending"] == 0  # pending count


@pytest.mark.asyncio
async def test_loop_does_not_ack_on_handler_exception(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
    consumer_groups: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the entry-handler raises, the entry stays in PEL (no ACK)."""

    async def boom(*_args: Any, **_kw: Any) -> None:
        raise RuntimeError("simulated handler crash")

    monkeypatch.setattr("app.services.response_handler._handle_response_entry", boom)
    await redis_client.xadd(
        "resp_stream:ftmo:ftmo_001",
        {"action": "open", "request_id": "req_xyz", "status": "success"},
    )

    task = asyncio.create_task(response_handler_loop(redis_svc, broadcast, "ftmo_001", block_ms=50))
    await asyncio.sleep(0.15)  # let the loop pick up the entry + crash
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    pending = await redis_client.xpending("resp_stream:ftmo:ftmo_001", "server")
    # Entry is still pending (not ACKed).
    assert pending["pending"] == 1


@pytest.mark.asyncio
async def test_loop_exits_on_cancel(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    consumer_groups: None,
) -> None:
    """Task.cancel() drops the loop out cleanly."""
    task = asyncio.create_task(response_handler_loop(redis_svc, broadcast, "ftmo_001", block_ms=50))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loop_continues_after_xread_error(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    consumer_groups: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If read_responses raises mid-loop, the next iteration retries
    rather than crashing the task. Pin the resilience contract."""
    call_count = 0
    real_read = redis_svc.read_responses

    async def flaky_read(*args: Any, **kw: Any) -> list[Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated XREADGROUP failure")
        return await real_read(*args, **kw)

    monkeypatch.setattr(redis_svc, "read_responses", flaky_read)

    task = asyncio.create_task(response_handler_loop(redis_svc, broadcast, "ftmo_001", block_ms=50))
    # Give the loop time to fail once, sleep, then read normally.
    await asyncio.sleep(1.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Both attempts ran → loop survived the first exception.
    assert call_count >= 2
