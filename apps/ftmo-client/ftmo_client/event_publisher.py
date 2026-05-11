"""Build event_stream payloads from cTrader unsolicited execution events.

Step 3.5 / 3.5a. A "solicited" execution event is one whose
``clientMsgId`` matches a pending request the bridge sent (handled by
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

Close-reason inference (step 3.5a)
----------------------------------
cTrader does NOT expose a dedicated ``closeReason`` enum field —
verified empirically via DESCRIPTOR inspection of ``ProtoOADeal`` /
``ProtoOAClosePositionDetail`` in step 3.5 and re-confirmed in 3.5a.
Step 3.5 first attempted a price-tolerance heuristic (``close_price ≈
stopLoss/takeProfit`` within one pip) which CEO flagged as brittle:

  - A single pip-sized absolute tolerance worked for 5-digit EURUSD.
  - It was much narrower than one pip on 3-digit USDJPY pairs, so
    SL/TP hits there misclassified as "manual".
  - For non-forex (BTC, indices, commodities) an absolute tolerance
    has no consistent meaning across the price scale.

Step 3.5a replaces the heuristic with **structured order metadata**
that cTrader DOES expose:

  - ``order.orderType``      — enum: MARKET / LIMIT / STOP /
                               STOP_LOSS_TAKE_PROFIT / MARKET_RANGE /
                               STOP_LIMIT.
  - ``order.closingOrder``   — bool: true when this order is closing
                               a position (vs opening a new one).
  - ``deal.closePositionDetail.grossProfit`` — sign distinguishes SL
    vs TP for STOP_LOSS_TAKE_PROFIT closes (negative = SL, positive
    = TP).

Mapping table (see ``docs/ctrader-execution-events.md §3.6``):

  | orderType              | closingOrder | grossProfit | reason  |
  | ---------------------- | ------------ | ----------- | ------- |
  | MARKET                 | true         | (any)       | manual  |
  | STOP_LOSS_TAKE_PROFIT  | true         | > 0         | tp      |
  | STOP_LOSS_TAKE_PROFIT  | true         | < 0         | sl      |
  | STOP_LOSS_TAKE_PROFIT  | true         | == 0        | unknown |
  | any                    | false        | n/a         | unknown |
  | LIMIT / STOP / other   | true         | n/a         | unknown |

CEO's canonical sample (TP hit on a BUY position, BTC-style symbol):

  order.orderType      = STOP_LOSS_TAKE_PROFIT (=4)
  order.closingOrder   = true
  order.executionPrice = 90640.27 (slippage 1.56 vs limitPrice 90638.71)
  deal.closePositionDetail.grossProfit = 98  → POSITIVE → "tp"

The 1.56-point gap between ``executionPrice`` and ``limitPrice`` is
broker slippage at the trigger moment; the price-tolerance heuristic
would have missed this match. The grossProfit-sign method is robust.

Limitations:

  - "stopout" reason (margin call / forced close) is currently lumped
    into "manual". cTrader uses ``orderType=MARKET`` + an additional
    ``order.isStopOut=true`` bool for margin-call closes. Step 3.5a
    leaves ``isStopOut`` for a future step to wire (would refine
    "manual" → "stopout" when the flag is set).
  - "unknown" reserved for: (a) non-classifiable orderType,
    (b) grossProfit=0 edge case (SL/TP exactly at entry), (c) missing
    required protobuf fields.
"""

from __future__ import annotations

import logging
import time

from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType,
    ProtoOAOrderType,
)

logger = logging.getLogger(__name__)


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
    """Build the ``position_closed`` event_stream payload.

    Step 3.5a — extended payload. Five new fields lifted out of
    ``deal.closePositionDetail`` so the server's response_handler /
    consumer doesn't have to re-query cTrader for the close
    accounting:

      - ``commission``         : close-side fee (from
                                 ``closePositionDetail.commission``,
                                 NOT ``deal.commission`` — they're
                                 separate ledger lines per CEO).
      - ``swap``               : accumulated swap charges on the
                                 position over its lifetime.
      - ``balance_after_close``: account balance after this close
                                 settles (raw int per D-053).
      - ``money_digits``       : exponent for grossProfit / swap /
                                 commission / balance_after_close
                                 (per-account, typically 2).
      - ``closed_volume``      : volume that actually closed (cTrader
                                 wire units; consumer scales by
                                 ``lot_size`` for the symbol).

    All money fields are raw cTrader integers — the consumer scales
    by ``money_digits``. Volume is raw cTrader wire units.
    """
    pos = event.position
    deal = event.deal
    close_detail = deal.closePositionDetail
    return {
        "event_type": "position_closed",
        "broker_order_id": str(int(pos.positionId)),
        "position_id": str(int(pos.positionId)),
        "close_price": str(float(deal.executionPrice)),
        "close_time": str(int(deal.executionTimestamp)),
        # realized_pnl == closePositionDetail.grossProfit (raw int, D-053)
        "realized_pnl": str(int(close_detail.grossProfit)),
        # Step 3.5a: prefer closePositionDetail.commission over
        # deal.commission for close events — they're distinct ledger
        # lines (deal-level vs position-close-level). CEO direction.
        "commission": str(int(close_detail.commission)),
        "swap": str(int(close_detail.swap)),
        "balance_after_close": str(int(close_detail.balance)),
        "money_digits": str(int(close_detail.moneyDigits))
        if close_detail.HasField("moneyDigits")
        else "",
        "closed_volume": str(int(close_detail.closedVolume))
        if close_detail.HasField("closedVolume")
        else "",
        "close_reason": _infer_close_reason(event),
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


def _infer_close_reason(event: ProtoOAExecutionEvent) -> str:
    """Classify a close via cTrader's structured order metadata.

    Returns one of the protocol enum values per
    ``docs/05-redis-protocol.md §5``: ``sl | tp | manual | unknown``.
    ``stopout`` is not emitted here — see module docstring for the
    ``isStopOut`` follow-up path.

    Step 3.5a contract:
      1. ``event.order`` must be present AND ``order.closingOrder ==
         true``. Otherwise the event isn't a position close (open-side
         fill, modify, etc.) → ``"unknown"``.
      2. ``order.orderType == MARKET`` → ``"manual"`` (user UI close
         OR broker forced close; both use MARKET internally).
      3. ``order.orderType == STOP_LOSS_TAKE_PROFIT`` →
         ``deal.closePositionDetail.grossProfit`` sign decides:
            > 0 → ``"tp"``, < 0 → ``"sl"``, == 0 → ``"unknown"``.
         The grossProfit method is preferred over comparing
         ``order.executionPrice`` to ``order.stopPrice`` /
         ``order.limitPrice`` because (a) cTrader fills can slip
         several points off the SL/TP trigger price, (b) grossProfit
         comes from cTrader's own bookkeeping (single source of truth).
      4. Other orderType (LIMIT, STOP, MARKET_RANGE, STOP_LIMIT) with
         ``closingOrder=true`` → ``"unknown"``. Not observed in
         smoke; conservative default.
    """
    if not event.HasField("order"):
        return "unknown"
    order = event.order
    if not (order.HasField("closingOrder") and order.closingOrder):
        return "unknown"

    order_type = int(order.orderType)
    if order_type == ProtoOAOrderType.MARKET:
        return "manual"
    if order_type == ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT:
        if not event.deal.HasField("closePositionDetail"):
            return "unknown"
        gross_profit = int(event.deal.closePositionDetail.grossProfit)
        if gross_profit > 0:
            return "tp"
        if gross_profit < 0:
            return "sl"
        return "unknown"
    return "unknown"
