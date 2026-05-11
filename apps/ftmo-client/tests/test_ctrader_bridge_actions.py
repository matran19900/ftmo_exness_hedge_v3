"""Bridge action tests — assert protobuf construction + response parsing.

We never open a real cTrader socket. ``_send_and_wait`` is monkeypatched
to capture the outgoing protobuf message and return a synthesized
``ProtoOAExecutionEvent`` / ``ProtoOAErrorRes`` / ``ProtoOAOrderErrorEvent``.
The tests assert both halves of the round-trip: outgoing message has
the right fields, parsed result matches the right TypedDict shape.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAmendPositionSLTPReq,
    ProtoOAClosePositionReq,
    ProtoOAErrorRes,
    ProtoOAExecutionEvent,
    ProtoOANewOrderReq,
    ProtoOAOrderErrorEvent,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType,
    ProtoOAOrderType,
    ProtoOATradeSide,
)

from ftmo_client.ctrader_bridge import CtraderBridge

CTID = 12345


@pytest.fixture
def bridge() -> CtraderBridge:
    """A bridge instance with no real connection — every method we call
    in these tests goes through the patched ``_send_and_wait``.
    """
    return CtraderBridge(
        account_id="ftmo_001",
        access_token="acc",
        ctid_trader_account_id=CTID,
        client_id="cid",
        client_secret="sec",
        host="x.example.com",
        port=5035,
    )


def _make_execution_event(
    exec_type: int,
    *,
    position_id: int | None = None,
    order_id: int | None = None,
    deal_price: float = 0.0,
    deal_ts: int = 0,
    deal_commission: int = 0,
    error_code: str | None = None,
    position_sl: float | None = None,
    position_tp: float | None = None,
) -> ProtoOAExecutionEvent:
    """Build a minimal but valid ProtoOAExecutionEvent for parser tests."""
    evt = ProtoOAExecutionEvent()
    evt.ctidTraderAccountId = CTID
    evt.executionType = exec_type
    if position_id is not None or position_sl is not None or position_tp is not None:
        # ProtoOAPosition needs at least the tradeData submessage to be valid,
        # but the parsers only look at the fields they need, so we can leave
        # tradeData unset on these stubs.
        if position_id is not None:
            evt.position.positionId = position_id
        if position_sl is not None:
            evt.position.stopLoss = position_sl
        if position_tp is not None:
            evt.position.takeProfit = position_tp
    if order_id is not None:
        evt.order.orderId = order_id
    if deal_price > 0:
        evt.deal.executionPrice = deal_price
        evt.deal.executionTimestamp = deal_ts
        evt.deal.commission = deal_commission
        if position_id is not None:
            evt.deal.positionId = position_id
    if error_code is not None:
        evt.errorCode = error_code
    return evt


# ---------- place_market_order ----------


@pytest.mark.asyncio
async def test_place_market_order_builds_protobuf(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def stub_send(message: Any, timeout: float, client_msg_id: str | None = None) -> Any:
        captured["message"] = message
        captured["timeout"] = timeout
        captured["client_msg_id"] = client_msg_id
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=987654321,
            deal_price=1.08412,
            deal_ts=1735000000123,
            deal_commission=5,
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)

    result = await bridge.place_market_order(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=1.08000,
        tp_price=1.09000,
        client_msg_id="req_abc",
    )

    msg = captured["message"]
    assert isinstance(msg, ProtoOANewOrderReq)
    assert msg.ctidTraderAccountId == CTID
    assert msg.symbolId == 1
    assert msg.orderType == ProtoOAOrderType.MARKET
    assert msg.tradeSide == ProtoOATradeSide.BUY
    # 0.01 lot * 10_000_000 lot_size = 100_000 (cTrader cents-of-base).
    assert msg.volume == 100_000
    assert msg.stopLoss == pytest.approx(1.08000)
    assert msg.takeProfit == pytest.approx(1.09000)
    # Market orders never set limitPrice / stopPrice.
    assert not msg.HasField("limitPrice")
    assert not msg.HasField("stopPrice")
    assert captured["client_msg_id"] == "req_abc"

    assert result["success"] is True
    assert result["broker_order_id"] == "987654321"
    assert result["fill_price"] == "1.08412"
    assert result["fill_time"] == "1735000000123"
    assert result["commission"] == "5"
    assert result["error_code"] == ""


@pytest.mark.asyncio
async def test_place_market_order_sell_side_maps_to_proto_sell(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def stub_send(message: Any, **_kw: Any) -> Any:
        captured["message"] = message
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=1,
            deal_price=1.0,
            deal_ts=1,
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    await bridge.place_market_order(
        symbol_id=1,
        side="sell",
        volume_lots=0.5,
        lot_size=10_000_000,
        sl_price=0,
        tp_price=0,
        client_msg_id="r",
    )
    assert captured["message"].tradeSide == ProtoOATradeSide.SELL


@pytest.mark.asyncio
async def test_place_market_order_zero_sl_tp_skipped(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SL=0 and TP=0 must NOT set the protobuf fields (treated as 'unset')."""
    captured: dict[str, Any] = {}

    async def stub_send(message: Any, **_kw: Any) -> Any:
        captured["message"] = message
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=1,
            deal_price=1.0,
            deal_ts=1,
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    await bridge.place_market_order(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=0,
        tp_price=0,
        client_msg_id="r",
    )
    msg = captured["message"]
    assert not msg.HasField("stopLoss")
    assert not msg.HasField("takeProfit")


@pytest.mark.asyncio
async def test_place_market_order_rejected_returns_error_result(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_REJECTED,
            error_code="MARKET_CLOSED",
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_market_order(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=1.0,
        tp_price=1.1,
        client_msg_id="r",
    )
    assert result["success"] is False
    assert result["error_code"] == "market_closed"
    assert "MARKET_CLOSED" in result["error_msg"]


@pytest.mark.asyncio
async def test_place_market_order_protooaerrorres(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transport-level errors (e.g. auth lost mid-session) → ProtoOAErrorRes."""

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        err = ProtoOAErrorRes()
        err.ctidTraderAccountId = CTID
        err.errorCode = "AUTH_FAILED"
        err.description = "Account auth expired"
        return err

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_market_order(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=0,
        tp_price=0,
        client_msg_id="r",
    )
    assert result["success"] is False
    assert result["error_code"] == "auth_failed"
    assert "Account auth expired" in result["error_msg"]


@pytest.mark.asyncio
async def test_place_market_order_protooaordererror(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        evt = ProtoOAOrderErrorEvent()
        evt.ctidTraderAccountId = CTID
        evt.errorCode = "NOT_ENOUGH_MONEY"
        evt.description = "Free margin insufficient"
        return evt

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_market_order(
        symbol_id=1,
        side="buy",
        volume_lots=1.0,
        lot_size=10_000_000,
        sl_price=0,
        tp_price=0,
        client_msg_id="r",
    )
    assert result["success"] is False
    assert result["error_code"] == "not_enough_money"
    assert "Free margin insufficient" in result["error_msg"]


# ---------- place_limit_order / place_stop_order ----------


@pytest.mark.asyncio
async def test_place_limit_order_sets_limit_price_and_accepted_returns_order_id(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def stub_send(message: Any, **_kw: Any) -> Any:
        captured["message"] = message
        return _make_execution_event(ProtoOAExecutionType.ORDER_ACCEPTED, order_id=555)

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_limit_order(
        symbol_id=2,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        entry_price=1.07500,
        sl_price=1.07000,
        tp_price=1.08500,
        client_msg_id="r",
    )

    msg = captured["message"]
    assert msg.orderType == ProtoOAOrderType.LIMIT
    assert msg.limitPrice == pytest.approx(1.07500)
    assert not msg.HasField("stopPrice")

    # Pending order: success but no fill_price.
    assert result["success"] is True
    assert result["broker_order_id"] == "555"
    assert result["fill_price"] == ""
    assert result["fill_time"] == ""


@pytest.mark.asyncio
async def test_place_stop_order_sets_stop_price(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def stub_send(message: Any, **_kw: Any) -> Any:
        captured["message"] = message
        return _make_execution_event(ProtoOAExecutionType.ORDER_ACCEPTED, order_id=777)

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    await bridge.place_stop_order(
        symbol_id=2,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        entry_price=1.10000,
        sl_price=0,
        tp_price=0,
        client_msg_id="r",
    )
    msg = captured["message"]
    assert msg.orderType == ProtoOAOrderType.STOP
    assert msg.stopPrice == pytest.approx(1.10000)
    assert not msg.HasField("limitPrice")


# ---------- close_position ----------


@pytest.mark.asyncio
async def test_close_position_builds_protobuf(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def stub_send(message: Any, timeout: float, client_msg_id: str | None = None) -> Any:
        captured["message"] = message
        captured["client_msg_id"] = client_msg_id
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=987654321,
            deal_price=1.08600,
            deal_ts=1735000000456,
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.close_position(
        position_id=987654321,
        volume_lots=0.01,
        lot_size=10_000_000,
        client_msg_id="req_close",
    )

    msg = captured["message"]
    assert isinstance(msg, ProtoOAClosePositionReq)
    assert msg.positionId == 987654321
    assert msg.volume == 100_000  # 0.01 * 10_000_000
    assert captured["client_msg_id"] == "req_close"
    assert result["success"] is True
    assert result["close_price"] == "1.086"
    assert result["close_time"] == "1735000000456"


@pytest.mark.asyncio
async def test_close_position_rejected_returns_error(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        err = ProtoOAErrorRes()
        err.ctidTraderAccountId = CTID
        err.errorCode = "POSITION_NOT_FOUND"
        err.description = "no such position"
        return err

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.close_position(
        position_id=99999999,
        volume_lots=0.01,
        lot_size=10_000_000,
        client_msg_id="r",
    )
    assert result["success"] is False
    assert result["error_code"] == "position_not_found"


# ---------- modify_sl_tp ----------


@pytest.mark.asyncio
async def test_modify_sl_tp_builds_protobuf_skips_zero(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def stub_send(message: Any, **_kw: Any) -> Any:
        captured["message"] = message
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_REPLACED,
            position_id=987,
            position_sl=1.07000,
            position_tp=1.09000,
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.modify_sl_tp(
        position_id=987,
        sl_price=1.07000,
        tp_price=0,  # clearing TP — should NOT set the proto field
        client_msg_id="r",
    )

    msg = captured["message"]
    assert isinstance(msg, ProtoOAAmendPositionSLTPReq)
    assert msg.positionId == 987
    assert msg.stopLoss == pytest.approx(1.07000)
    assert not msg.HasField("takeProfit")
    assert result["success"] is True
    assert result["new_sl"] == "1.07"
    # cTrader echoed position.takeProfit even though we didn't set it on the
    # request — our parser preserves that echo.
    assert result["new_tp"] == "1.09"


@pytest.mark.asyncio
async def test_modify_sl_tp_rejection_returns_error(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        evt = ProtoOAOrderErrorEvent()
        evt.ctidTraderAccountId = CTID
        evt.errorCode = "INVALID_STOPS_LEVEL"
        evt.description = "SL too close to price"
        return evt

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.modify_sl_tp(
        position_id=987,
        sl_price=1.08400,
        tp_price=1.09000,
        client_msg_id="r",
    )
    assert result["success"] is False
    assert result["error_code"] == "invalid_sl_distance"


# ---------- timeout / unexpected response ----------


@pytest.mark.asyncio
async def test_bridge_passes_timeout_exception_through(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asyncio.TimeoutError from ``_send_and_wait`` propagates; the action
    handler is responsible for turning it into a ``timeout`` resp entry."""

    async def stub_send(*_args: Any, **_kw: Any) -> Any:
        raise TimeoutError("simulated wait_for timeout")

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        await bridge.place_market_order(
            symbol_id=1,
            side="buy",
            volume_lots=0.01,
            lot_size=10_000_000,
            sl_price=0,
            tp_price=0,
            client_msg_id="r",
        )


@pytest.mark.asyncio
async def test_unexpected_response_type_returns_broker_error(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return "garbage"  # something neither ExecEvent / ErrorRes / OrderErrorEvent

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_market_order(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=0,
        tp_price=0,
        client_msg_id="r",
    )
    assert result["success"] is False
    assert result["error_code"] == "broker_error"
    assert "unexpected response" in result["error_msg"]
