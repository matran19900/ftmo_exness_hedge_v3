"""Unit tests for ``event_publisher.build_event_payload``.

Pure-function tests: each constructs a ProtoOAExecutionEvent stub and
asserts the resulting payload dict has the right shape + values. No
Redis or bridge needed.

Step 3.5a — close_reason inference rewritten to use structured order
metadata (``order.orderType`` + ``order.closingOrder`` +
``deal.closePositionDetail.grossProfit``) rather than the 1-pip
price-tolerance heuristic from step 3.5. The 8 tolerance-based tests
from step 3.5 are deleted; replacement tests below pin the new
structured-logic branches.
"""

from __future__ import annotations

import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType,
    ProtoOAOrderType,
)

from ftmo_client.event_publisher import (
    _infer_close_reason,
    build_event_payload,
)


def _exec_event(
    exec_type: int,
    *,
    position_id: int | None = None,
    order_id: int | None = None,
    order_type: int | None = None,
    closing_order: bool | None = None,
    deal_price: float = 0.0,
    deal_ts: int = 0,
    deal_commission: int | None = None,
    position_sl: float | None = None,
    position_tp: float | None = None,
    close_gross_profit: int | None = None,
    close_commission: int | None = None,
    close_swap: int | None = None,
    close_balance: int | None = None,
    close_money_digits: int | None = None,
    close_volume: int | None = None,
) -> ProtoOAExecutionEvent:
    """Build a ProtoOAExecutionEvent stub.

    Step 3.5a adds:
      - ``order_type`` / ``closing_order``: drive the new structured
        close_reason inference path.
      - ``close_commission`` / ``close_swap`` / ``close_balance`` /
        ``close_money_digits`` / ``close_volume``: populate the
        extended fields in ``closePositionDetail`` that the new
        ``_build_position_closed`` lifts into the event_stream payload.

    Setting any close_* field implies a close-side fill — the helper
    auto-populates ``closePositionDetail`` so the builder routes to
    ``position_closed`` (vs ``pending_filled``).
    """
    evt = ProtoOAExecutionEvent()
    evt.executionType = exec_type
    if position_id is not None or position_sl is not None or position_tp is not None:
        if position_id is not None:
            evt.position.positionId = position_id
        if position_sl is not None:
            evt.position.stopLoss = position_sl
        if position_tp is not None:
            evt.position.takeProfit = position_tp
    if order_id is not None or order_type is not None or closing_order is not None:
        if order_id is not None:
            evt.order.orderId = order_id
        if order_type is not None:
            evt.order.orderType = order_type
        if closing_order is not None:
            evt.order.closingOrder = closing_order
    if deal_price > 0:
        evt.deal.executionPrice = deal_price
        evt.deal.executionTimestamp = deal_ts
        if deal_commission is not None:
            evt.deal.commission = deal_commission
        if position_id is not None:
            evt.deal.positionId = position_id
        # Trigger closePositionDetail population if any close_* arg was passed.
        close_args_set = any(
            v is not None
            for v in (
                close_gross_profit,
                close_commission,
                close_swap,
                close_balance,
                close_money_digits,
                close_volume,
            )
        )
        if close_args_set:
            cd = evt.deal.closePositionDetail
            # closePositionDetail.grossProfit/commission/swap/balance are REQUIRED —
            # default any unset value to 0 so the proto stays valid.
            cd.grossProfit = close_gross_profit if close_gross_profit is not None else 0
            cd.commission = close_commission if close_commission is not None else 0
            cd.swap = close_swap if close_swap is not None else 0
            cd.balance = close_balance if close_balance is not None else 0
            cd.entryPrice = 0.0  # required field; value irrelevant to tests
            if close_money_digits is not None:
                cd.moneyDigits = close_money_digits
            if close_volume is not None:
                cd.closedVolume = close_volume
    return evt


# ---------- build_event_payload routing ----------


def test_build_event_payload_order_filled_with_close_detail_returns_position_closed() -> None:
    """Step 3.5a builder pulls all five extended fields from
    ``closePositionDetail`` and infers ``close_reason`` via structured
    metadata. This test uses a STOP_LOSS_TAKE_PROFIT closing order
    with positive grossProfit → TP hit."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=5451198,
        order_type=ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT,
        closing_order=True,
        deal_price=1.08600,
        deal_ts=1735000000456,
        deal_commission=5,
        close_gross_profit=1840,
        close_commission=-766,
        close_swap=0,
        close_balance=942589,
        close_money_digits=2,
        close_volume=13,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["event_type"] == "position_closed"
    assert payload["broker_order_id"] == "5451198"
    assert payload["position_id"] == "5451198"
    assert payload["close_price"] == "1.086"
    assert payload["close_time"] == "1735000000456"
    assert payload["realized_pnl"] == "1840"
    # Step 3.5a: commission comes from closePositionDetail, NOT deal.commission.
    assert payload["commission"] == "-766"
    assert payload["swap"] == "0"
    assert payload["balance_after_close"] == "942589"
    assert payload["money_digits"] == "2"
    assert payload["closed_volume"] == "13"
    assert payload["close_reason"] == "tp"
    assert "ts_published" in payload


def test_build_event_payload_order_filled_without_close_detail_returns_pending_filled() -> None:
    """Open-side fill: no closePositionDetail → pending order finally
    transitioned to fill. Payload carries both positionId (NEW) and
    order_id_old (the request-side orderId that the server's order
    row currently uses as broker_order_id)."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=5451300,
        order_id=8324918,
        deal_price=1.07000,
        deal_ts=1735000005000,
        deal_commission=3,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["event_type"] == "pending_filled"
    assert payload["broker_order_id"] == "5451300"
    assert payload["position_id"] == "5451300"
    assert payload["order_id_old"] == "8324918"
    assert payload["fill_price"] == "1.07"
    assert payload["fill_time"] == "1735000005000"
    assert payload["commission"] == "3"


def test_build_event_payload_order_replaced_returns_position_modified() -> None:
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_REPLACED,
        position_id=5451198,
        position_sl=1.07500,
        position_tp=1.09000,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["event_type"] == "position_modified"
    assert payload["broker_order_id"] == "5451198"
    assert payload["new_sl"] == "1.075"
    assert payload["new_tp"] == "1.09"


def test_build_event_payload_order_replaced_partial_clears() -> None:
    """Operator cleared the TP side only; SL still set."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_REPLACED,
        position_id=5451198,
        position_sl=1.07500,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["new_sl"] == "1.075"
    assert payload["new_tp"] == ""


def test_build_event_payload_order_cancelled_returns_order_cancelled() -> None:
    evt = _exec_event(ProtoOAExecutionType.ORDER_CANCELLED, order_id=8324918)
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["event_type"] == "order_cancelled"
    assert payload["broker_order_id"] == "8324918"


def test_build_event_payload_order_accepted_returns_none() -> None:
    """ACCEPTED is a transient state — not interesting for event_stream."""
    evt = _exec_event(ProtoOAExecutionType.ORDER_ACCEPTED, order_id=8324918)
    assert build_event_payload(evt) is None


def test_build_event_payload_order_rejected_returns_none() -> None:
    """Rejections from solicited paths go on resp_stream, not event_stream."""
    evt = _exec_event(ProtoOAExecutionType.ORDER_REJECTED, order_id=8324918)
    assert build_event_payload(evt) is None


def test_build_event_payload_unrelated_execution_type_returns_none() -> None:
    """SWAP / DEPOSIT_WITHDRAW etc. — not relevant for trading-event stream."""
    evt = _exec_event(ProtoOAExecutionType.SWAP)
    assert build_event_payload(evt) is None


# ---------- Step 3.5a: extended position_closed payload fields ----------


def test_build_position_closed_includes_extended_fields() -> None:
    """All five new fields (commission, swap, balance_after_close,
    money_digits, closed_volume) appear in the payload as stringified
    values pulled from closePositionDetail."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.MARKET,
        closing_order=True,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=500,
        close_commission=-50,
        close_swap=-2,
        close_balance=1_000_500,
        close_money_digits=2,
        close_volume=100_000,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["commission"] == "-50"
    assert payload["swap"] == "-2"
    assert payload["balance_after_close"] == "1000500"
    assert payload["money_digits"] == "2"
    assert payload["closed_volume"] == "100000"


def test_build_position_closed_realized_pnl_equals_gross_profit() -> None:
    """``realized_pnl`` carries ``closePositionDetail.grossProfit``
    verbatim as a string (raw int per D-053)."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.MARKET,
        closing_order=True,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=12345,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["realized_pnl"] == "12345"


def test_build_position_closed_uses_close_detail_commission_not_deal_commission() -> None:
    """Per CEO direction (step 3.5a): for close events, the canonical
    commission is ``closePositionDetail.commission`` (close-side fee),
    NOT ``deal.commission`` (deal-level fee — separate ledger line).
    Tests that the two values diverge AND that the payload picks the
    closePositionDetail one."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.MARKET,
        closing_order=True,
        deal_price=1.08,
        deal_ts=1,
        deal_commission=-100,  # deal-level
        close_gross_profit=0,
        close_commission=-766,  # close-side — should win
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["commission"] == "-766"


def test_build_position_closed_money_digits_empty_when_proto_unset() -> None:
    """``moneyDigits`` is OPTIONAL in the protobuf. When the broker
    omits it, payload carries empty string rather than "0" (which
    would imply scale=1 — different semantically)."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.MARKET,
        closing_order=True,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=0,
        # close_money_digits intentionally not passed
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["money_digits"] == ""


def test_build_position_closed_closed_volume_empty_when_proto_unset() -> None:
    """``closedVolume`` is OPTIONAL. Empty string on absence."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.MARKET,
        closing_order=True,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=0,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["closed_volume"] == ""


# ---------- Step 3.5a: structured close_reason inference ----------


def test_infer_close_reason_market_closing_order_returns_manual() -> None:
    """``orderType=MARKET`` + ``closingOrder=true`` → ``"manual"``.
    Covers user UI close AND broker forced closes (both use MARKET
    internally)."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.MARKET,
        closing_order=True,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=100,
    )
    assert _infer_close_reason(evt) == "manual"


def test_infer_close_reason_stop_loss_take_profit_with_positive_pnl_returns_tp() -> None:
    """STOP_LOSS_TAKE_PROFIT + grossProfit > 0 → TP hit. CEO's sample
    event has grossProfit=98 (positive) for a TP hit; we use the same
    shape here."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT,
        closing_order=True,
        deal_price=90640.27,
        deal_ts=1,
        close_gross_profit=98,
    )
    assert _infer_close_reason(evt) == "tp"


def test_infer_close_reason_stop_loss_take_profit_with_negative_pnl_returns_sl() -> None:
    """STOP_LOSS_TAKE_PROFIT + grossProfit < 0 → SL hit."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT,
        closing_order=True,
        deal_price=1.07,
        deal_ts=1,
        close_gross_profit=-150,
    )
    assert _infer_close_reason(evt) == "sl"


def test_infer_close_reason_stop_loss_take_profit_with_zero_pnl_returns_unknown() -> None:
    """SL/TP triggered at exact entry price (rare edge) → ``"unknown"``.
    Avoids false-classifying a degenerate zero-PnL close as TP or SL."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT,
        closing_order=True,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=0,
    )
    assert _infer_close_reason(evt) == "unknown"


def test_infer_close_reason_no_closing_order_returns_unknown() -> None:
    """``closingOrder=false`` (or unset) → event isn't a position close →
    ``"unknown"`` regardless of orderType."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.MARKET,
        closing_order=False,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=100,
    )
    assert _infer_close_reason(evt) == "unknown"


def test_infer_close_reason_no_order_field_returns_unknown() -> None:
    """Event with no ``order`` sub-message → ``"unknown"``."""
    evt = ProtoOAExecutionEvent()
    evt.executionType = ProtoOAExecutionType.ORDER_FILLED
    # Don't set evt.order at all.
    assert not evt.HasField("order")
    assert _infer_close_reason(evt) == "unknown"


def test_infer_close_reason_unknown_order_type_returns_unknown() -> None:
    """LIMIT (or any non-MARKET / non-STOP_LOSS_TAKE_PROFIT) +
    closingOrder=true → ``"unknown"``. Not observed in smoke; the
    conservative default catches future broker behavior the parser
    hasn't been taught."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=ProtoOAOrderType.LIMIT,
        closing_order=True,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=100,
    )
    assert _infer_close_reason(evt) == "unknown"


def test_infer_close_reason_stop_loss_take_profit_no_close_detail_returns_unknown() -> None:
    """STOP_LOSS_TAKE_PROFIT + closingOrder=true but no
    ``closePositionDetail`` sub-message → ``"unknown"``. Defensive
    guard for malformed events."""
    evt = ProtoOAExecutionEvent()
    evt.executionType = ProtoOAExecutionType.ORDER_FILLED
    evt.order.orderId = 1
    evt.order.orderType = ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT
    evt.order.closingOrder = True
    evt.deal.executionPrice = 1.08
    evt.deal.executionTimestamp = 1
    # Don't touch evt.deal.closePositionDetail.
    assert not evt.deal.HasField("closePositionDetail")
    assert _infer_close_reason(evt) == "unknown"


# ---------- Parameterized matrix per docs §3.6 ----------


@pytest.mark.parametrize(
    ("order_type", "closing", "gross_profit", "expected"),
    [
        (ProtoOAOrderType.MARKET, True, 100, "manual"),
        (ProtoOAOrderType.MARKET, True, -100, "manual"),
        (ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT, True, 98, "tp"),
        (ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT, True, -150, "sl"),
        (ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT, True, 0, "unknown"),
        (ProtoOAOrderType.LIMIT, True, 0, "unknown"),
        (ProtoOAOrderType.STOP, True, 0, "unknown"),
        (ProtoOAOrderType.STOP_LIMIT, True, 0, "unknown"),
        (ProtoOAOrderType.MARKET_RANGE, True, 0, "unknown"),
        (ProtoOAOrderType.MARKET, False, 100, "unknown"),  # closingOrder=false
    ],
    ids=[
        "market-closing-profit",
        "market-closing-loss",
        "sltp-closing-tp",
        "sltp-closing-sl",
        "sltp-closing-zero",
        "limit-closing",
        "stop-closing",
        "stop-limit-closing",
        "market-range-closing",
        "market-non-closing",
    ],
)
def test_close_reason_matrix(
    order_type: int, closing: bool, gross_profit: int, expected: str
) -> None:
    """Tabulated coverage of every row in
    ``docs/ctrader-execution-events.md §3.6`` mapping table."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        order_type=order_type,
        closing_order=closing,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=gross_profit,
    )
    assert _infer_close_reason(evt) == expected


# ---------- Integration: build_event_payload routes close_reason ----------


def test_build_event_payload_position_closed_carries_inferred_close_reason() -> None:
    """End-to-end: build_event_payload on a STOP_LOSS_TAKE_PROFIT close
    with negative grossProfit emits a ``position_closed`` payload with
    ``close_reason="sl"``. This is the production call path."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=5451198,
        order_type=ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT,
        closing_order=True,
        deal_price=1.07,
        deal_ts=1,
        close_gross_profit=-150,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["event_type"] == "position_closed"
    assert payload["close_reason"] == "sl"


# ---------- ts_published presence ----------


def test_ts_published_is_epoch_ms_string() -> None:
    """Every payload carries a ts_published epoch-ms string so the
    server can sort late-arriving events."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_REPLACED,
        position_id=5451198,
        position_sl=1.07,
        position_tp=1.09,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    ts = int(payload["ts_published"])
    # Sanity: within the year 2026 epoch-ms range.
    assert ts > 1_700_000_000_000  # >= ~2023-11
    assert ts < 2_500_000_000_000  # < ~2049 (sanity ceiling)


@pytest.mark.parametrize(
    ("exec_type", "expected_event_type"),
    [
        (ProtoOAExecutionType.ORDER_FILLED, "position_closed"),
        (ProtoOAExecutionType.ORDER_REPLACED, "position_modified"),
        (ProtoOAExecutionType.ORDER_CANCELLED, "order_cancelled"),
    ],
)
def test_event_type_routing_parametrized(exec_type: int, expected_event_type: str) -> None:
    """Parametrized smoke that the executionType → event_type routing
    holds at the entry of every branch. The ORDER_FILLED case here
    routes to position_closed because we set closePositionDetail."""
    evt = _exec_event(
        exec_type,
        position_id=1,
        order_id=8324918,
        order_type=ProtoOAOrderType.MARKET,
        closing_order=True,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=0,
        position_sl=1.07,
        position_tp=1.09,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["event_type"] == expected_event_type
