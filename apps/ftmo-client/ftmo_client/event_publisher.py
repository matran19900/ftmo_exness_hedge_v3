"""Build event_stream payloads from cTrader unsolicited execution events.

Step 3.5. A "solicited" execution event is one whose ``clientMsgId``
matches a pending request the bridge sent (handled by
``_pending_executions`` since step 3.4b/3.4c). An "unsolicited" event
is one that arrives without correlation — user action on the cTrader
UI, SL/TP hit, margin call, pending order fill. These events are
broadcast to every connected client; the FTMO client lifts them into
``event_stream:ftmo:{account_id}`` so the server's event_handler (step
3.7) can update order state.

This module owns only the **shape** of the payload: given a
``ProtoOAExecutionEvent``, return the dict that gets XADD'd. The
bridge does the I/O. Splitting the two keeps the parser logic
synchronous and easy to test without Redis fixtures.

Close-reason heuristic
----------------------
cTrader's protobuf does NOT expose a structured close-reason field —
verified empirically via DESCRIPTOR inspection of
``ProtoOADeal`` and ``ProtoOAClosePositionDetail`` (step 3.5). We
infer the reason from the close price vs the position's stopLoss /
takeProfit echoed in the event:

  - close ≈ stopLoss  → ``"sl"``
  - close ≈ takeProfit → ``"tp"``
  - clientMsgId empty (always true for unsolicited) → ``"manual"``
    (operator likely closed via the cTrader UI; could also be stop-out
    or expiry, both of which we treat as ``"manual"`` until a future
    smoke surfaces a distinct signature).
  - all else (no SL/TP set + no match) → ``"unknown"``

Values match the protocol enum in ``docs/05-redis-protocol.md §5``:
``sl | tp | manual | stopout | unknown``. We do NOT emit ``stopout``
here — distinguishing stop-out from manual close requires a separate
signal (``ProtoOAMarginCallTriggerEvent`` or a follow-up balance
change), out of scope for step 3.5.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAExecutionType

logger = logging.getLogger(__name__)

# Tolerance for matching close_price against stopLoss / takeProfit.
# cTrader fills can drift from the requested SL/TP by a few points due
# to spread + slippage at the trigger moment. 0.00010 (1 pip on a
# 5-digit forex pair) is roomy enough to catch the common case
# without false-matching genuine manual closes that happen to land
# near the SL/TP price. For non-forex symbols (indices, metals), the
# absolute tolerance is still meaningful relative to the price scale
# we'd see at the broker.
_SL_TP_PRICE_TOLERANCE = 1e-4


def build_event_payload(event: ProtoOAExecutionEvent) -> dict[str, str] | None:
    """Map a ProtoOAExecutionEvent to an event_stream payload.

    Returns ``None`` when the event is not one we publish — the
    ``_on_message`` dispatcher should skip the XADD in that case.

    Branches:
      * ORDER_FILLED with ``deal.closePositionDetail`` → ``position_closed``.
      * ORDER_FILLED without closePositionDetail (open-side fill) →
        ``pending_filled`` (the unsolicited path: a pending LIMIT/STOP
        finally hit the trigger).
      * ORDER_REPLACED → ``position_modified`` (user changed SL/TP via
        the cTrader UI, or some external system did).
      * ORDER_CANCELLED → ``order_cancelled`` (pending order cancelled).
      * Anything else → ``None``.
    """
    execution_type = int(event.executionType)

    if execution_type == ProtoOAExecutionType.ORDER_FILLED:
        if event.deal.HasField("closePositionDetail"):
            return _build_position_closed(event)
        return _build_pending_filled(event)

    if execution_type == ProtoOAExecutionType.ORDER_REPLACED:
        return _build_position_modified(event)

    if execution_type == ProtoOAExecutionType.ORDER_CANCELLED:
        return _build_order_cancelled(event)

    return None


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _build_position_closed(event: ProtoOAExecutionEvent) -> dict[str, str]:
    pos = event.position
    deal = event.deal
    close_price = float(deal.executionPrice)
    close_reason = _infer_close_reason(pos, close_price)
    return {
        "event_type": "position_closed",
        "broker_order_id": str(int(pos.positionId)),
        "position_id": str(int(pos.positionId)),
        "close_price": str(close_price),
        "close_time": str(int(deal.executionTimestamp)),
        "realized_pnl": str(int(deal.closePositionDetail.grossProfit)),
        "commission": str(int(deal.commission)) if deal.HasField("commission") else "",
        "close_reason": close_reason,
        "ts_published": _now_ms(),
    }


def _build_pending_filled(event: ProtoOAExecutionEvent) -> dict[str, str]:
    """Pending LIMIT/STOP order finally hit its trigger and produced a
    fill. The original order_id is now superseded by a positionId — the
    server's event_handler swaps ``broker_order_id`` on the order row.

    ``order_id_old`` carries the cTrader order.orderId so the server
    can look up the existing order row (which still has orderId as
    its broker_order_id) and migrate it.
    """
    pos = event.position
    deal = event.deal
    order = event.order
    return {
        "event_type": "pending_filled",
        "broker_order_id": str(int(pos.positionId)),
        "position_id": str(int(pos.positionId)),
        "order_id_old": str(int(order.orderId)),
        "fill_price": str(float(deal.executionPrice)),
        "fill_time": str(int(deal.executionTimestamp)),
        "commission": str(int(deal.commission)) if deal.HasField("commission") else "",
        "ts_published": _now_ms(),
    }


def _build_position_modified(event: ProtoOAExecutionEvent) -> dict[str, str]:
    """User (or some other external client) changed the position's
    SL/TP via the cTrader UI. cTrader echoes the new values in
    ``event.position.stopLoss`` / ``event.position.takeProfit``.
    Either may be unset if the operator cleared one side.
    """
    pos = event.position
    new_sl = str(float(pos.stopLoss)) if pos.HasField("stopLoss") else ""
    new_tp = str(float(pos.takeProfit)) if pos.HasField("takeProfit") else ""
    return {
        "event_type": "position_modified",
        "broker_order_id": str(int(pos.positionId)),
        "position_id": str(int(pos.positionId)),
        "new_sl": new_sl,
        "new_tp": new_tp,
        "ts_published": _now_ms(),
    }


def _build_order_cancelled(event: ProtoOAExecutionEvent) -> dict[str, str]:
    """Pending LIMIT/STOP order cancelled (user via UI, expiry,
    insufficient margin, etc.). Server's event_handler can mark the
    order row as cancelled / detached.
    """
    return {
        "event_type": "order_cancelled",
        "broker_order_id": str(int(event.order.orderId)),
        "ts_published": _now_ms(),
    }


def _infer_close_reason(position: Any, close_price: float) -> str:
    """Heuristic for unsolicited closes. See module docstring for the
    reasoning. Returns one of the protocol enum values from
    ``docs/05-redis-protocol.md §5``: ``sl | tp | manual | unknown``
    (``stopout`` is not emitted by this path — needs a separate
    signal).

    ``position`` is a ProtoOAPosition proto; we accept ``Any`` so this
    helper can be exercised with stubs.
    """
    has_sl = position.HasField("stopLoss")
    has_tp = position.HasField("takeProfit")

    if has_sl and abs(close_price - float(position.stopLoss)) <= _SL_TP_PRICE_TOLERANCE:
        return "sl"
    if has_tp and abs(close_price - float(position.takeProfit)) <= _SL_TP_PRICE_TOLERANCE:
        return "tp"
    if not has_sl and not has_tp:
        # Position had no SL/TP set at all; close must be manual or
        # external. Distinguishable from "unknown" because we *know*
        # the trigger wasn't an SL/TP hit.
        return "manual"
    # Has SL or TP, but close_price didn't match either within
    # tolerance → operator closed manually (cTrader UI), or a margin
    # call closed the position at market. We default to "manual"; a
    # future step can layer on ProtoOAMarginCallTriggerEvent
    # correlation to upgrade specific closes to "stopout".
    return "manual"
