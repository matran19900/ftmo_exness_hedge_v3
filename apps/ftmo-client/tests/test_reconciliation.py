"""Tests for the step 3.5b reconciliation infrastructure.

Three layers covered:

  - ``CtraderBridge.reconcile_state`` — pulls ProtoOAReconcileRes,
    publishes a ``reconcile_snapshot`` event_stream entry. Retry +
    failure-tolerance semantics verified.
  - ``CtraderBridge.fetch_position_close_history`` — pulls
    ProtoOADealListByPositionIdRes, picks the close deal, returns a
    reconstructed ``position_closed`` payload (with
    ``reconstructed=true``).
  - ``action_handlers.handle_fetch_close_history`` — wires the bridge
    call into the cmd_stream → event_stream / resp_stream paths.
"""

from __future__ import annotations

import json
from typing import Any

import fakeredis.aioredis
import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOADealListByPositionIdReq,
    ProtoOADealListByPositionIdRes,
    ProtoOAErrorRes,
    ProtoOAReconcileReq,
    ProtoOAReconcileRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOATradeSide,
)

from ftmo_client.action_handlers import (
    ACTION_HANDLERS,
    handle_fetch_close_history,
)
from ftmo_client.ctrader_bridge import CtraderBridge


def _make_bridge(redis: Any | None) -> CtraderBridge:
    return CtraderBridge(
        account_id="ftmo_001",
        access_token="acc",
        ctid_trader_account_id=42,
        client_id="cid",
        client_secret="sec",
        redis=redis,
    )


# ---------- ProtoOA stub builders ----------


def _make_reconcile_res(
    *,
    positions: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> ProtoOAReconcileRes:
    """Build a ProtoOAReconcileRes with arbitrary position + order lists.

    Each dict supplies enough fields to populate the required protobuf
    REQUIRED labels for ``ProtoOAPosition`` and ``ProtoOAOrder``;
    optional fields are skipped when the dict doesn't carry them.
    """
    res = ProtoOAReconcileRes()
    res.ctidTraderAccountId = 42
    for p in positions or []:
        pos = res.position.add()
        pos.positionId = p["position_id"]
        pos.tradeData.symbolId = p["symbol_id"]
        pos.tradeData.volume = p["volume"]
        pos.tradeData.tradeSide = (
            ProtoOATradeSide.BUY if p["side"] == "buy" else ProtoOATradeSide.SELL
        )
        if "open_timestamp" in p:
            pos.tradeData.openTimestamp = p["open_timestamp"]
        pos.positionStatus = 1  # POSITION_STATUS_OPEN
        pos.swap = 0
        if "price" in p:
            pos.price = p["price"]
        if "stop_loss" in p:
            pos.stopLoss = p["stop_loss"]
        if "take_profit" in p:
            pos.takeProfit = p["take_profit"]
        if "used_margin" in p:
            pos.usedMargin = p["used_margin"]
    for o in orders or []:
        order = res.order.add()
        order.orderId = o["order_id"]
        order.tradeData.symbolId = o["symbol_id"]
        order.tradeData.volume = o["volume"]
        order.tradeData.tradeSide = (
            ProtoOATradeSide.BUY if o["side"] == "buy" else ProtoOATradeSide.SELL
        )
        if "open_timestamp" in o:
            order.tradeData.openTimestamp = o["open_timestamp"]
        order.orderType = o["order_type"]
        order.orderStatus = 1  # ORDER_STATUS_ACCEPTED
        if "limit_price" in o:
            order.limitPrice = o["limit_price"]
        if "stop_price" in o:
            order.stopPrice = o["stop_price"]
        if "stop_loss" in o:
            order.stopLoss = o["stop_loss"]
        if "take_profit" in o:
            order.takeProfit = o["take_profit"]
    return res


def _make_deal_list_res(
    *,
    deals: list[dict[str, Any]] | None = None,
) -> ProtoOADealListByPositionIdRes:
    """Build a ProtoOADealListByPositionIdRes. Each dict can either
    describe an open-side deal (no ``close_*`` keys) or a close-side
    deal (with at least ``close_gross_profit`` set, which auto-populates
    ``closePositionDetail``)."""
    res = ProtoOADealListByPositionIdRes()
    res.ctidTraderAccountId = 42
    for d in deals or []:
        deal = res.deal.add()
        deal.dealId = d.get("deal_id", 1)
        deal.orderId = d.get("order_id", 1)
        deal.positionId = d.get("position_id", 1)
        deal.volume = d.get("volume", 100_000)
        deal.filledVolume = d.get("filled_volume", 100_000)
        deal.symbolId = d.get("symbol_id", 1)
        deal.createTimestamp = d.get("create_timestamp", 0)
        deal.executionTimestamp = d.get("execution_timestamp", 0)
        deal.tradeSide = (
            ProtoOATradeSide.BUY if d.get("side", "buy") == "buy" else ProtoOATradeSide.SELL
        )
        deal.dealStatus = 2  # FILLED (ProtoOADealStatus.FILLED = 2)
        if "execution_price" in d:
            deal.executionPrice = d["execution_price"]
        if "commission" in d:
            deal.commission = d["commission"]
        # If close_* keys present, populate closePositionDetail.
        close_keys = (
            "close_gross_profit",
            "close_commission",
            "close_swap",
            "close_balance",
            "close_money_digits",
            "close_volume",
        )
        if any(k in d for k in close_keys):
            cd = deal.closePositionDetail
            cd.grossProfit = d.get("close_gross_profit", 0)
            cd.commission = d.get("close_commission", 0)
            cd.swap = d.get("close_swap", 0)
            cd.balance = d.get("close_balance", 0)
            cd.entryPrice = d.get("close_entry_price", 0.0)
            if "close_money_digits" in d:
                cd.moneyDigits = d["close_money_digits"]
            if "close_volume" in d:
                cd.closedVolume = d["close_volume"]
    return res


# ---------- bridge.reconcile_state ----------


@pytest.mark.asyncio
async def test_reconcile_state_publishes_snapshot_with_positions_and_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    sent: list[Any] = []

    async def stub_send(msg: Any, **_kw: Any) -> Any:
        sent.append(msg)
        return _make_reconcile_res(
            positions=[
                {
                    "position_id": 5451198,
                    "symbol_id": 1,
                    "volume": 100_000,
                    "side": "buy",
                    "price": 1.08412,
                    "stop_loss": 1.07000,
                    "take_profit": 1.09000,
                    "open_timestamp": 1735000000000,
                    "used_margin": 10_000,
                },
                {
                    "position_id": 5451300,
                    "symbol_id": 1,
                    "volume": 50_000,
                    "side": "sell",
                    "open_timestamp": 1735000005000,
                },
            ],
            orders=[
                {
                    "order_id": 8324918,
                    "symbol_id": 1,
                    "volume": 100_000,
                    "side": "buy",
                    "order_type": ProtoOAOrderType.LIMIT,
                    "limit_price": 1.07000,
                    "stop_loss": 1.06000,
                    "take_profit": 1.08000,
                    "open_timestamp": 1735000010000,
                },
            ],
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)

    await bridge.reconcile_state()

    # Exactly one ReconcileReq sent.
    assert len(sent) == 1
    assert isinstance(sent[0], ProtoOAReconcileReq)
    assert sent[0].ctidTraderAccountId == 42

    entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    assert fields["event_type"] == "reconcile_snapshot"
    assert fields["position_count"] == "2"
    assert fields["order_count"] == "1"

    positions = json.loads(fields["positions"])
    assert len(positions) == 2
    assert positions[0]["position_id"] == "5451198"
    assert positions[0]["side"] == "buy"
    assert positions[0]["volume"] == "100000"
    assert positions[0]["entry_price"] == "1.08412"
    assert positions[0]["stop_loss"] == "1.07"
    assert positions[0]["take_profit"] == "1.09"
    assert positions[0]["open_timestamp"] == "1735000000000"
    assert positions[0]["used_margin"] == "10000"
    # Second position: optional fields blank.
    assert positions[1]["entry_price"] == ""
    assert positions[1]["stop_loss"] == ""
    assert positions[1]["used_margin"] == ""

    pending_orders = json.loads(fields["pending_orders"])
    assert len(pending_orders) == 1
    assert pending_orders[0]["order_id"] == "8324918"
    assert pending_orders[0]["order_type"] == "limit"
    assert pending_orders[0]["limit_price"] == "1.07"
    assert pending_orders[0]["stop_price"] == ""


@pytest.mark.asyncio
async def test_reconcile_state_empty_publishes_zero_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_reconcile_res()

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    await bridge.reconcile_state()

    entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["position_count"] == "0"
    assert fields["order_count"] == "0"
    assert json.loads(fields["positions"]) == []
    assert json.loads(fields["pending_orders"]) == []


@pytest.mark.asyncio
async def test_reconcile_state_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First 2 attempts raise (transient TimeoutError); 3rd attempt
    returns a valid response. Single snapshot published.

    asyncio.sleep is patched so the test doesn't pay the 1s+2s
    backoff."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)
    attempt_count = 0

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            raise TimeoutError(f"simulated attempt {attempt_count}")
        return _make_reconcile_res(
            positions=[
                {
                    "position_id": 1,
                    "symbol_id": 1,
                    "volume": 100_000,
                    "side": "buy",
                }
            ],
        )

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    monkeypatch.setattr("ftmo_client.ctrader_bridge.asyncio.sleep", fake_sleep)

    await bridge.reconcile_state()

    assert attempt_count == 3
    # Exponential backoff: 1s after attempt 1, 2s after attempt 2.
    assert sleeps == [1, 2]

    entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
    assert len(entries) == 1
    assert entries[0][1]["position_count"] == "1"


@pytest.mark.asyncio
async def test_reconcile_state_all_retries_fail_no_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 3 attempts fail → no XADD, no exception escapes. Startup
    continues."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        raise TimeoutError("simulated outage")

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    monkeypatch.setattr("ftmo_client.ctrader_bridge.asyncio.sleep", fake_sleep)

    await bridge.reconcile_state()  # must not raise
    entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
    assert entries == []


@pytest.mark.asyncio
async def test_reconcile_state_with_redis_none_does_not_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests that instantiate the bridge without a Redis fixture must
    not crash when reconcile is invoked — bridge logs and returns."""
    bridge = _make_bridge(None)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_reconcile_res()

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    # Should not raise.
    await bridge.reconcile_state()


@pytest.mark.asyncio
async def test_reconcile_state_unexpected_response_type_skips_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cTrader returns ProtoOAErrorRes instead of ProtoOAReconcileRes
    (auth lapsed mid-call), the bridge logs and skips publish rather
    than serializing garbage to the stream."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        err = ProtoOAErrorRes()
        err.ctidTraderAccountId = 42
        err.errorCode = "AUTH_FAILED"
        err.description = "stale"
        return err

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    monkeypatch.setattr("ftmo_client.ctrader_bridge.asyncio.sleep", fake_sleep)

    await bridge.reconcile_state()
    entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
    assert entries == []


# ---------- bridge.fetch_position_close_history ----------


@pytest.mark.asyncio
async def test_fetch_position_close_history_returns_reconstructed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deal list with one open-side deal + one close deal → bridge
    returns the close payload with all five extended fields plus
    ``reconstructed=true`` and ``close_reason=unknown``."""
    bridge = _make_bridge(None)
    sent: list[Any] = []

    async def stub_send(msg: Any, **_kw: Any) -> Any:
        sent.append(msg)
        return _make_deal_list_res(
            deals=[
                {  # open-side deal — no closePositionDetail
                    "deal_id": 1,
                    "order_id": 8324918,
                    "position_id": 5451198,
                    "execution_price": 1.08000,
                    "execution_timestamp": 1735000000000,
                    "commission": 5,
                },
                {  # close-side deal — closePositionDetail set
                    "deal_id": 2,
                    "order_id": 8324919,
                    "position_id": 5451198,
                    "execution_price": 1.08600,
                    "execution_timestamp": 1735000050000,
                    "commission": 5,
                    "close_gross_profit": 1840,
                    "close_commission": -766,
                    "close_swap": 0,
                    "close_balance": 942589,
                    "close_money_digits": 2,
                    "close_volume": 100_000,
                },
            ],
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)

    payload = await bridge.fetch_position_close_history(
        position_id=5451198, client_msg_id="req_xyz"
    )

    assert len(sent) == 1
    req = sent[0]
    assert isinstance(req, ProtoOADealListByPositionIdReq)
    assert req.positionId == 5451198
    assert req.ctidTraderAccountId == 42
    assert req.fromTimestamp == 0
    assert req.toTimestamp > 0  # set to now_ms

    assert payload is not None
    assert payload["event_type"] == "position_closed"
    assert payload["broker_order_id"] == "5451198"
    assert payload["position_id"] == "5451198"
    assert payload["close_price"] == "1.086"
    assert payload["close_time"] == "1735000050000"
    assert payload["realized_pnl"] == "1840"
    assert payload["commission"] == "-766"
    assert payload["swap"] == "0"
    assert payload["balance_after_close"] == "942589"
    assert payload["money_digits"] == "2"
    assert payload["closed_volume"] == "100000"
    assert payload["close_reason"] == "unknown"
    assert payload["reconstructed"] == "true"
    assert "ts_published" in payload


@pytest.mark.asyncio
async def test_fetch_position_close_history_no_close_deal_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Position still open on cTrader — deal list has only the open
    deal, no closePositionDetail. Bridge returns None; caller will
    publish ``not_found`` to resp_stream."""
    bridge = _make_bridge(None)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_deal_list_res(
            deals=[
                {
                    "deal_id": 1,
                    "order_id": 8324918,
                    "position_id": 5451198,
                    "execution_price": 1.08000,
                    "execution_timestamp": 1735000000000,
                }
            ],
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    payload = await bridge.fetch_position_close_history(position_id=5451198, client_msg_id="r")
    assert payload is None


@pytest.mark.asyncio
async def test_fetch_position_close_history_empty_response_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deal list returned but empty (positionId unknown to cTrader)."""
    bridge = _make_bridge(None)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_deal_list_res(deals=[])

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    assert await bridge.fetch_position_close_history(position_id=99, client_msg_id="r") is None


@pytest.mark.asyncio
async def test_fetch_position_close_history_send_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge timeout → returns None, error logged."""
    bridge = _make_bridge(None)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        raise TimeoutError("simulated")

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    assert await bridge.fetch_position_close_history(position_id=5451198, client_msg_id="r") is None


@pytest.mark.asyncio
async def test_fetch_position_close_history_unexpected_response_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: unexpected response type (e.g. ProtoOAErrorRes) →
    returns None."""
    bridge = _make_bridge(None)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        err = ProtoOAErrorRes()
        err.errorCode = "POSITION_NOT_FOUND"
        return err

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    assert await bridge.fetch_position_close_history(position_id=99, client_msg_id="r") is None


# ---------- action_handlers.handle_fetch_close_history ----------


@pytest.mark.asyncio
async def test_action_handlers_registers_fetch_close_history() -> None:
    """Dispatch table must include the new action so command_loop
    routes it. Pinned alongside open/close/modify_sl_tp."""
    assert "fetch_close_history" in ACTION_HANDLERS
    assert ACTION_HANDLERS["fetch_close_history"] is handle_fetch_close_history


@pytest.mark.asyncio
async def test_handle_fetch_close_history_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge returns payload → handler XADDs the reconstructed event
    to event_stream AND ACKs success on resp_stream."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    reconstructed_payload = {
        "event_type": "position_closed",
        "broker_order_id": "5451198",
        "position_id": "5451198",
        "close_price": "1.086",
        "close_time": "1735000050000",
        "realized_pnl": "1840",
        "commission": "-766",
        "swap": "0",
        "balance_after_close": "942589",
        "money_digits": "2",
        "closed_volume": "100000",
        "close_reason": "unknown",
        "reconstructed": "true",
        "ts_published": "1735000050100",
    }

    async def fake_fetch(*, position_id: int, client_msg_id: str) -> dict[str, str] | None:
        assert position_id == 5451198
        assert client_msg_id == "req_x"
        return reconstructed_payload

    monkeypatch.setattr(bridge, "fetch_position_close_history", fake_fetch)

    await handle_fetch_close_history(
        redis,
        bridge,
        "ftmo_001",
        {
            "request_id": "req_x",
            "order_id": "ord_abc",
            "broker_order_id": "5451198",
        },
    )

    # event_stream got the reconstructed close.
    event_entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
    assert len(event_entries) == 1
    assert event_entries[0][1]["reconstructed"] == "true"
    assert event_entries[0][1]["realized_pnl"] == "1840"

    # resp_stream got the ACK.
    resp_entries = await redis.xrange("resp_stream:ftmo:ftmo_001", "-", "+")
    assert len(resp_entries) == 1
    resp = resp_entries[0][1]
    assert resp["status"] == "success"
    assert resp["action"] == "fetch_close_history"
    assert resp["order_id"] == "ord_abc"
    assert resp["request_id"] == "req_x"
    assert resp["position_id"] == "5451198"


@pytest.mark.asyncio
async def test_handle_fetch_close_history_bridge_returns_none_publishes_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge returns None (no close deal found) → resp_stream gets
    status=error error_code=not_found; event_stream untouched."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    async def fake_fetch(*, position_id: int, client_msg_id: str) -> dict[str, str] | None:
        return None

    monkeypatch.setattr(bridge, "fetch_position_close_history", fake_fetch)

    await handle_fetch_close_history(
        redis,
        bridge,
        "ftmo_001",
        {
            "request_id": "r",
            "order_id": "ord_abc",
            "broker_order_id": "99",
        },
    )

    assert await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+") == []
    resp = (await redis.xrange("resp_stream:ftmo:ftmo_001", "-", "+"))[0][1]
    assert resp["status"] == "error"
    assert resp["error_code"] == "not_found"
    assert "99" in resp["error_msg"]


@pytest.mark.asyncio
async def test_handle_fetch_close_history_missing_broker_order_id() -> None:
    """Defensive: cmd_stream entry without ``broker_order_id`` →
    invalid_request, bridge not called."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    await handle_fetch_close_history(
        redis,
        bridge,
        "ftmo_001",
        {
            "request_id": "r",
            "order_id": "ord_abc",
            # broker_order_id missing
        },
    )

    resp = (await redis.xrange("resp_stream:ftmo:ftmo_001", "-", "+"))[0][1]
    assert resp["status"] == "error"
    assert resp["error_code"] == "invalid_request"
    assert resp["error_msg"] == "broker_order_id missing"


@pytest.mark.asyncio
async def test_handle_fetch_close_history_non_numeric_broker_order_id() -> None:
    """``broker_order_id="not_an_int"`` → invalid_request."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    await handle_fetch_close_history(
        redis,
        bridge,
        "ftmo_001",
        {
            "request_id": "r",
            "order_id": "ord_abc",
            "broker_order_id": "not_an_int",
        },
    )

    resp = (await redis.xrange("resp_stream:ftmo:ftmo_001", "-", "+"))[0][1]
    assert resp["status"] == "error"
    assert resp["error_code"] == "invalid_request"
    assert "not an int" in resp["error_msg"]


@pytest.mark.asyncio
async def test_handle_fetch_close_history_bridge_raises_returns_broker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge raises an unexpected exception → handler catches +
    publishes broker_error rather than crashing the command loop."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    async def fake_fetch(*, position_id: int, client_msg_id: str) -> dict[str, str] | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(bridge, "fetch_position_close_history", fake_fetch)

    await handle_fetch_close_history(
        redis,
        bridge,
        "ftmo_001",
        {
            "request_id": "r",
            "order_id": "ord_abc",
            "broker_order_id": "5451198",
        },
    )

    resp = (await redis.xrange("resp_stream:ftmo:ftmo_001", "-", "+"))[0][1]
    assert resp["status"] == "error"
    assert resp["error_code"] == "broker_error"
    assert "boom" in resp["error_msg"]


# Sanity: helper module exposes things we expect; saves headache when refactoring.


def test_action_handlers_keys_complete() -> None:
    """Pin every action this client understands so a future refactor
    (e.g. accidentally dropping an entry from the dict) gets caught."""
    expected = {"open", "close", "modify_sl_tp", "fetch_close_history"}
    assert set(ACTION_HANDLERS) == expected


@pytest.mark.asyncio
async def test_reconcile_state_then_immediate_command_no_interference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end shape: reconcile publishes once on event_stream;
    next ad-hoc unsolicited event would land alongside without
    overwriting. Pinned via XLEN."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_reconcile_res(
            positions=[
                {
                    "position_id": 1,
                    "symbol_id": 1,
                    "volume": 100_000,
                    "side": "buy",
                }
            ],
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    await bridge.reconcile_state()

    # Add a synthetic second event (mimicking a live unsolicited close).
    await redis.xadd(
        "event_stream:ftmo:ftmo_001",
        {"event_type": "position_closed", "broker_order_id": "1"},
    )
    assert (await redis.xlen("event_stream:ftmo:ftmo_001")) == 2
