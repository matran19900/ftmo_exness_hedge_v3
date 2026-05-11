"""Unit tests for ``event_publisher.build_event_payload``.

Pure-function tests: each constructs a ProtoOAExecutionEvent stub and
asserts the resulting payload dict has the right shape + values. No
Redis or bridge needed.
"""

from __future__ import annotations

import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAExecutionType

from ftmo_client.event_publisher import (
    _infer_close_reason,
    build_event_payload,
)


def _exec_event(
    exec_type: int,
    *,
    position_id: int | None = None,
    order_id: int | None = None,
    deal_price: float = 0.0,
    deal_ts: int = 0,
    deal_commission: int | None = None,
    position_sl: float | None = None,
    position_tp: float | None = None,
    close_gross_profit: int | None = None,
) -> ProtoOAExecutionEvent:
    evt = ProtoOAExecutionEvent()
    evt.executionType = exec_type
    if position_id is not None or position_sl is not None or position_tp is not None:
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
        if deal_commission is not None:
            evt.deal.commission = deal_commission
        if position_id is not None:
            evt.deal.positionId = position_id
        if close_gross_profit is not None:
            evt.deal.closePositionDetail.grossProfit = close_gross_profit
    return evt


# ---------- build_event_payload routing ----------


def test_build_event_payload_order_filled_with_close_detail_returns_position_closed() -> None:
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=5451198,
        deal_price=1.08600,
        deal_ts=1735000000456,
        deal_commission=5,
        close_gross_profit=1840,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["event_type"] == "position_closed"
    assert payload["broker_order_id"] == "5451198"
    assert payload["position_id"] == "5451198"
    assert payload["close_price"] == "1.086"
    assert payload["close_time"] == "1735000000456"
    assert payload["realized_pnl"] == "1840"
    assert payload["commission"] == "5"
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


def test_build_event_payload_commission_omitted_when_deal_missing_commission() -> None:
    """deal.commission is OPTIONAL on the protobuf — payload tolerates absence."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=5451198,
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=100,
    )
    # Don't set commission. (HasField returns False.)
    assert not evt.deal.HasField("commission")
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["commission"] == ""


# ---------- close_reason inference ----------


def test_close_reason_matches_stop_loss_within_tolerance() -> None:
    """Close price within 1 pip of stopLoss → 'sl'."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        position_sl=1.07000,
        position_tp=1.09000,
        deal_price=1.07005,  # 5 points = 0.5 pip below SL — within tolerance
        deal_ts=1,
        close_gross_profit=-100,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["close_reason"] == "sl"


def test_close_reason_matches_take_profit_within_tolerance() -> None:
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        position_sl=1.07000,
        position_tp=1.09000,
        deal_price=1.08998,  # just under TP — within tolerance
        deal_ts=1,
        close_gross_profit=100,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["close_reason"] == "tp"


def test_close_reason_manual_when_no_sl_no_tp_set() -> None:
    """Position had neither SL nor TP — close must be manual or external."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        deal_price=1.08000,
        deal_ts=1,
        close_gross_profit=0,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["close_reason"] == "manual"


def test_close_reason_manual_when_close_doesnt_match_sl_or_tp() -> None:
    """Position had SL and TP, but close price matches NEITHER → manual."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        position_sl=1.07000,
        position_tp=1.09000,
        deal_price=1.08500,  # in the middle — manual close
        deal_ts=1,
        close_gross_profit=50,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["close_reason"] == "manual"


def test_close_reason_sl_priority_over_tp_when_equal() -> None:
    """Edge case: SL and TP both set to the same price (degenerate
    operator config). SL is checked first; result is 'sl'. Just pinning
    the order so a refactor doesn't silently change behavior."""
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        position_sl=1.08000,
        position_tp=1.08000,
        deal_price=1.08000,
        deal_ts=1,
        close_gross_profit=0,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["close_reason"] == "sl"


def test_close_reason_only_sl_set_close_far_from_sl() -> None:
    evt = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        position_sl=1.07000,  # only SL
        deal_price=1.08500,  # far from SL
        deal_ts=1,
        close_gross_profit=100,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    # has_sl=True, has_tp=False, no SL match → default "manual" branch.
    assert payload["close_reason"] == "manual"


def test_infer_close_reason_returns_unknown_branch_inaccessible() -> None:
    """Document that the 'unknown' value in the protocol doc is reserved
    for cases the heuristic *can't* classify. With the current branches
    (sl / tp / manual), no code path returns 'unknown' — every close
    either matches SL/TP or falls into 'manual'. Test pins the
    contract: if you later add a stricter branch (e.g. requires
    HasField close_reason proto), make sure the 'unknown' default is
    reachable + emitted as the protocol value."""
    # Build a position stub manually.

    class _PosStub:
        def HasField(self, name: str) -> bool:
            return False

    # The function with no SL or no TP returns "manual" (NOT "unknown").
    result = _infer_close_reason(_PosStub(), close_price=1.08)
    assert result == "manual"


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
        deal_price=1.08,
        deal_ts=1,
        close_gross_profit=0,
        position_sl=1.07,
        position_tp=1.09,
    )
    payload = build_event_payload(evt)
    assert payload is not None
    assert payload["event_type"] == expected_event_type
