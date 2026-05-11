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


class _FakeWrapper:
    """Minimal stand-in for a ``ProtoMessage`` wire envelope. Has just
    enough surface (``clientMsgId`` attribute + ``payloadType`` so
    ``Protobuf.extract`` can do its work) — but we never actually call
    extract on it; we monkeypatch ``Protobuf.extract`` to return the
    inner event when ``_on_message`` is invoked in tests.
    Real ``ProtoOAExecutionEvent`` serialization requires populating
    every nested required field, which is orthogonal to what we test.
    """

    def __init__(self, inner: Any, *, client_msg_id: str | None = None) -> None:
        self._inner = inner
        self.clientMsgId = client_msg_id
        self.payloadType = inner.payloadType


def _patch_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``Protobuf.extract`` a no-op identity when given a
    ``_FakeWrapper`` — returns the inner protobuf the test stashed."""
    real_extract = None
    try:
        from ctrader_open_api import Protobuf  # noqa: PLC0415

        real_extract = Protobuf.extract

        def extract(message: Any) -> Any:
            if isinstance(message, _FakeWrapper):
                return message._inner
            return real_extract(message)

        monkeypatch.setattr("ftmo_client.ctrader_bridge.Protobuf.extract", staticmethod(extract))
    except ImportError:
        pass


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
    # Step 3.4a: market orders MUST NOT carry absolute SL/TP — cTrader
    # rejects them with "SL/TP in absolute values are allowed only for
    # order types: [LIMIT, STOP, STOP_LIMIT]". The values supplied in the
    # kwargs are intentionally ignored on the bare-market path; the
    # ``place_market_order_with_sltp`` composite handles them post-fill.
    assert not msg.HasField("stopLoss")
    assert not msg.HasField("takeProfit")
    # Market orders also never set limitPrice / stopPrice.
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


# ---------- place_market_order_with_sltp (step 3.4a composite) ----------


@pytest.mark.asyncio
async def test_place_market_order_with_sltp_happy_path_fills_then_amends(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fill OK + amend OK → result is the fill, no sl_tp_attach_* fields."""
    calls: list[tuple[str, Any]] = []

    async def stub_send(message: Any, **kw: Any) -> Any:
        cmid = kw.get("client_msg_id")
        calls.append((type(message).__name__, cmid))
        if isinstance(message, ProtoOANewOrderReq):
            return _make_execution_event(
                ProtoOAExecutionType.ORDER_FILLED,
                position_id=987654321,
                deal_price=1.08412,
                deal_ts=1735000000123,
            )
        if isinstance(message, ProtoOAAmendPositionSLTPReq):
            return _make_execution_event(
                ProtoOAExecutionType.ORDER_REPLACED,
                position_id=987654321,
                position_sl=1.08000,
                position_tp=1.09000,
            )
        raise AssertionError(f"unexpected message {message!r}")

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_market_order_with_sltp(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=1.08000,
        tp_price=1.09000,
        client_msg_id="req_abc",
    )

    # Two distinct cTrader calls, with distinct client_msg_ids.
    assert len(calls) == 2
    assert calls[0] == ("ProtoOANewOrderReq", "req_abc")
    assert calls[1] == ("ProtoOAAmendPositionSLTPReq", "req_abc_amend")

    assert result["success"] is True
    assert result["broker_order_id"] == "987654321"
    assert result["fill_price"] == "1.08412"
    assert "sl_tp_attach_failed" not in result


@pytest.mark.asyncio
async def test_place_market_order_with_sltp_fill_fails_no_amend(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fill rejected → returns fill error verbatim, amend never sent."""
    calls: list[str] = []

    async def stub_send(message: Any, **_kw: Any) -> Any:
        calls.append(type(message).__name__)
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_REJECTED, error_code="MARKET_CLOSED"
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_market_order_with_sltp(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=1.08,
        tp_price=1.09,
        client_msg_id="r",
    )

    assert calls == ["ProtoOANewOrderReq"]  # amend not attempted
    assert result["success"] is False
    assert result["error_code"] == "market_closed"
    assert "sl_tp_attach_failed" not in result


@pytest.mark.asyncio
async def test_place_market_order_with_sltp_amend_fail_marks_result(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fill OK + amend rejected → fill result + sl_tp_attach_failed=True."""

    async def stub_send(message: Any, **_kw: Any) -> Any:
        if isinstance(message, ProtoOANewOrderReq):
            return _make_execution_event(
                ProtoOAExecutionType.ORDER_FILLED,
                position_id=987654321,
                deal_price=1.08412,
                deal_ts=1735000000123,
            )
        if isinstance(message, ProtoOAAmendPositionSLTPReq):
            evt = ProtoOAOrderErrorEvent()
            evt.ctidTraderAccountId = CTID
            evt.errorCode = "INVALID_STOPS_LEVEL"
            evt.description = "SL too close"
            return evt
        raise AssertionError(f"unexpected message {message!r}")

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_market_order_with_sltp(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=1.08000,
        tp_price=1.09000,
        client_msg_id="r",
    )

    # Fill itself succeeded — position is open at the broker.
    assert result["success"] is True
    assert result["broker_order_id"] == "987654321"
    assert result["fill_price"] == "1.08412"
    # Amend warning propagates so resp_stream sees it.
    assert result["sl_tp_attach_failed"] is True
    assert result["sl_tp_attach_error_code"] == "invalid_sl_distance"
    assert "SL too close" in result["sl_tp_attach_error_msg"]


@pytest.mark.asyncio
async def test_place_market_order_with_sltp_no_sl_no_tp_skips_amend(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SL=0 AND TP=0 → caller wanted a bare position; no amend round-trip."""
    calls: list[str] = []

    async def stub_send(message: Any, **_kw: Any) -> Any:
        calls.append(type(message).__name__)
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=987,
            deal_price=1.08,
            deal_ts=1,
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_market_order_with_sltp(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=0,
        tp_price=0,
        client_msg_id="r",
    )

    assert calls == ["ProtoOANewOrderReq"]  # amend skipped
    assert result["success"] is True
    assert "sl_tp_attach_failed" not in result


@pytest.mark.asyncio
async def test_place_market_order_with_sltp_amend_uses_suffix_msg_id(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amend MUST use a distinct client_msg_id so cTrader doesn't dedup it
    against the fill. Suffix ``_amend`` is the agreed convention."""
    seen_msg_ids: list[str | None] = []

    async def stub_send(message: Any, **kw: Any) -> Any:
        seen_msg_ids.append(kw.get("client_msg_id"))
        if isinstance(message, ProtoOANewOrderReq):
            return _make_execution_event(
                ProtoOAExecutionType.ORDER_FILLED,
                position_id=42,
                deal_price=1.0,
                deal_ts=1,
            )
        # amend
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_REPLACED,
            position_id=42,
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    await bridge.place_market_order_with_sltp(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=0.9,
        tp_price=1.1,
        client_msg_id="my_req_id",
    )

    assert seen_msg_ids == ["my_req_id", "my_req_id_amend"]


@pytest.mark.asyncio
async def test_place_market_order_with_sltp_only_sl_set_triggers_amend(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when only SL is set (TP=0), the amend still fires — the
    'skip' branch requires BOTH to be 0."""
    calls: list[str] = []

    async def stub_send(message: Any, **_kw: Any) -> Any:
        calls.append(type(message).__name__)
        if isinstance(message, ProtoOANewOrderReq):
            return _make_execution_event(
                ProtoOAExecutionType.ORDER_FILLED,
                position_id=99,
                deal_price=1.0,
                deal_ts=1,
            )
        return _make_execution_event(ProtoOAExecutionType.ORDER_REPLACED, position_id=99)

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    await bridge.place_market_order_with_sltp(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=0.95,
        tp_price=0,
        client_msg_id="r",
    )
    assert calls == ["ProtoOANewOrderReq", "ProtoOAAmendPositionSLTPReq"]


# ---------- Step 3.4b: 2-event sequence + positionId vs orderId ----------


@pytest.mark.asyncio
async def test_place_market_order_waits_for_filled_after_accepted(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bridge sees ORDER_ACCEPTED first; must wait for the FOLLOWING
    ORDER_FILLED (delivered through the ``_pending_market_fills``
    side channel) before returning. The returned result MUST carry
    ``position.positionId`` from the FILLED event, NOT
    ``order.orderId`` from the intermediate ACCEPTED event.

    We resolve the fill-future directly instead of round-tripping
    through the ``ProtoMessage`` wrapper + ``Protobuf.extract`` path,
    because serializing a ProtoOAExecutionEvent requires every nested
    required field (position.tradeData, deal.dealId, deal.symbolId,
    deal.dealStatus, ...) — orthogonal to what we're verifying.
    """

    async def stub_send(_msg: Any, **kw: Any) -> Any:
        # Mimic the unsolicited ORDER_FILLED arriving moments after the
        # Deferred-resolved ACCEPTED. Schedule the fill-future to fire
        # asynchronously so the bridge actually awaits on it.
        cmid = kw.get("client_msg_id")
        filled_event = _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=5451198,  # the real positionId from cTrader UI
            deal_price=1.08412,
            deal_ts=1735000000123,
            deal_commission=5,
        )

        async def fire_fill() -> None:
            # Yield once so the bridge has time to register and await.
            await asyncio.sleep(0)
            future = bridge._pending_market_fills.get(str(cmid))
            if future is not None and not future.done():
                future.set_result(filled_event)

        asyncio.get_running_loop().create_task(fire_fill())

        # The Deferred-resolved value is ACCEPTED, with the WRONG id
        # (request-side, not position-side). Bridge must IGNORE it.
        return _make_execution_event(ProtoOAExecutionType.ORDER_ACCEPTED, order_id=8324917)

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_market_order(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=0,
        tp_price=0,
        client_msg_id="req_2event",
    )

    # The fix in action: broker_order_id is the positionId (5451198),
    # NOT the orderId (8324917) from the intermediate ACCEPTED event.
    assert result["success"] is True
    assert result["broker_order_id"] == "5451198"
    assert result["fill_price"] == "1.08412"
    assert result["fill_time"] == "1735000000123"
    assert result["commission"] == "5"


@pytest.mark.asyncio
async def test_place_market_order_fast_path_filled_first(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some cTrader installs fill so fast that the Deferred resolves
    directly on ORDER_FILLED (no intermediate ACCEPTED). The bridge
    must parse + return from the first response."""

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=5451198,
            deal_price=1.08,
            deal_ts=1735000000000,
        )

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
    assert result["success"] is True
    assert result["broker_order_id"] == "5451198"


@pytest.mark.asyncio
async def test_place_market_order_accepted_then_timeout(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACCEPTED arrives but ORDER_FILLED never follows → bridge returns
    a ``timeout`` error result rather than hanging forever."""

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        # Note: do NOT call _on_message with an ORDER_FILLED follow-up.
        return _make_execution_event(ProtoOAExecutionType.ORDER_ACCEPTED, order_id=999)

    # Shorten the timeout so the test doesn't wait 30s. Monkeypatch
    # asyncio.wait_for inside the bridge to use a tiny timeout.
    real_wait_for = asyncio.wait_for

    async def fast_wait_for(coro: Any, timeout: float) -> Any:
        return await real_wait_for(coro, timeout=0.1)

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    monkeypatch.setattr("ftmo_client.ctrader_bridge.asyncio.wait_for", fast_wait_for)

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
    assert result["error_code"] == "timeout"


@pytest.mark.asyncio
async def test_limit_order_accepted_returns_order_id_no_fill_data(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Limit orders pending in the book: broker_order_id MUST be
    ``order.orderId`` (a request-side id valid for cancel/replace), NOT
    a positionId (none exists yet). Step 3.4b kept this behavior
    untouched — pin the contract."""

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_execution_event(ProtoOAExecutionType.ORDER_ACCEPTED, order_id=8324917)

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    result = await bridge.place_limit_order(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        entry_price=1.07000,
        sl_price=1.06,
        tp_price=1.08,
        client_msg_id="r",
    )
    assert result["success"] is True
    # orderId 8324917 is the correct ID for a pending limit.
    assert result["broker_order_id"] == "8324917"
    assert result["fill_price"] == ""
    assert result["fill_time"] == ""


@pytest.mark.asyncio
async def test_filled_market_extracts_position_id_not_deal_position_id(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-braces: even when both ``event.position.positionId`` AND
    ``event.deal.positionId`` are set, the parser pulls the open
    position ID from ``event.position.positionId`` (the authoritative
    one). Step 3.4's parser read deal.positionId which can match but
    is less semantically clean; 3.4b switched to the position path."""

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        evt = _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=5451198,
            deal_price=1.08,
            deal_ts=1,
        )
        # If a future cTrader revision sets a different deal.positionId
        # (legacy compat / sub-deals), our parser should still use
        # event.position.positionId. Force the divergence here.
        evt.deal.positionId = 9999999  # different value!
        return evt

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
    # Position-level id wins.
    assert result["broker_order_id"] == "5451198"


@pytest.mark.asyncio
async def test_composite_sleeps_100ms_between_fill_and_amend(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composite must call ``asyncio.sleep(0.1)`` between the fill
    and the amend (step 3.4b settling delay) so cTrader has time to
    publish the position internally. Mock ``asyncio.sleep`` so the test
    doesn't actually wait."""
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        # Do not actually wait — keep the test fast.

    async def stub_send(message: Any, **_kw: Any) -> Any:
        if isinstance(message, ProtoOANewOrderReq):
            return _make_execution_event(
                ProtoOAExecutionType.ORDER_FILLED,
                position_id=5451198,
                deal_price=1.08,
                deal_ts=1,
            )
        return _make_execution_event(ProtoOAExecutionType.ORDER_REPLACED, position_id=5451198)

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    monkeypatch.setattr("ftmo_client.ctrader_bridge.asyncio.sleep", fake_sleep)

    result = await bridge.place_market_order_with_sltp(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=1.07,
        tp_price=1.09,
        client_msg_id="r",
    )

    # The sleep was called exactly once with 0.1s between fill + amend.
    assert sleep_calls == [0.1]
    assert result["success"] is True
    assert "sl_tp_attach_failed" not in result


@pytest.mark.asyncio
async def test_composite_no_sleep_when_no_amend(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both SL=0 and TP=0, the composite skips the amend AND the
    settling delay — there's nothing to settle for."""
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=5451198,
            deal_price=1.08,
            deal_ts=1,
        )

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    monkeypatch.setattr("ftmo_client.ctrader_bridge.asyncio.sleep", fake_sleep)

    await bridge.place_market_order_with_sltp(
        symbol_id=1,
        side="buy",
        volume_lots=0.01,
        lot_size=10_000_000,
        sl_price=0,
        tp_price=0,
        client_msg_id="r",
    )

    assert sleep_calls == []


@pytest.mark.asyncio
async def test_on_message_dispatches_unsolicited_filled_to_pending_future(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end exercise of the ``_on_message`` side-channel: register
    a pending fill future by clientMsgId, fire ``_on_message`` with a
    matching wrapper, assert the future resolves with the inner event.
    Mirrors the production path where the cTrader library invokes
    ``_messageReceivedCallback`` for the unsolicited ORDER_FILLED that
    the Deferred path already popped."""
    _patch_extract(monkeypatch)

    # Bridge's _on_message uses self._loop.call_soon_threadsafe; populate it
    # with the running asyncio loop so the test exercises the production path.
    bridge._loop = asyncio.get_running_loop()

    inner_filled = _make_execution_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=5451198,
        deal_price=1.08412,
        deal_ts=1735000000123,
    )
    wrapper = _FakeWrapper(inner_filled, client_msg_id="req_xyz")

    fill_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    bridge._pending_market_fills["req_xyz"] = fill_future

    bridge._on_message(None, wrapper)

    # call_soon_threadsafe schedules — yield once for it to fire.
    resolved = await asyncio.wait_for(fill_future, timeout=1.0)
    assert int(resolved.position.positionId) == 5451198


@pytest.mark.asyncio
async def test_on_message_ignores_non_matching_client_msg_id(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrapper with a clientMsgId NOT in ``_pending_market_fills``
    must NOT touch any future. Logs at debug, returns silently."""
    _patch_extract(monkeypatch)
    bridge._loop = asyncio.get_running_loop()

    inner_filled = _make_execution_event(
        ProtoOAExecutionType.ORDER_FILLED, position_id=42, deal_price=1.0, deal_ts=1
    )
    wrapper = _FakeWrapper(inner_filled, client_msg_id="some_other_msg")

    # Register one pending future under a DIFFERENT clientMsgId.
    other_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    bridge._pending_market_fills["req_in_flight"] = other_future

    bridge._on_message(None, wrapper)
    await asyncio.sleep(0)  # allow any scheduled callbacks to run

    # The in-flight future was NOT resolved by an unrelated message.
    assert not other_future.done()


@pytest.mark.asyncio
async def test_pending_market_fills_cleaned_up_after_return(
    bridge: CtraderBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_pending_market_fills`` entry is removed in the ``finally`` block
    regardless of success / error. Otherwise a long-running process
    accumulates orphan futures and leaks memory."""

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return _make_execution_event(
            ProtoOAExecutionType.ORDER_FILLED,
            position_id=42,
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
        client_msg_id="req_cleanup",
    )
    assert "req_cleanup" not in bridge._pending_market_fills
