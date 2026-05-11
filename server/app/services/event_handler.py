"""Event handler — consume ``event_stream:ftmo:{acc}`` (step 3.7).

One background task per FTMO account, lifespan-managed. The loop
XREADGROUPs the account's event_stream, routes each entry by
``event_type`` field to the matching handler, updates the order row
in Redis, and publishes a structured ``position_event`` (or
``order_updated``) message over the WS ``positions`` /``orders``
channel.

Event types (per step 3.5 / 3.5a / 3.5b emissions):
  - ``position_closed``    — unsolicited (manual close, SL hit, TP
    hit) or reconstructed-from-deal-history (reconcile follow-up).
  - ``pending_filled``     — pending limit/stop became a filled
    position. The bridge swaps ``broker_order_id`` from orderId to
    positionId (D-061); event_handler migrates the side-index.
  - ``position_modified``  — user changed SL/TP via cTrader UI.
  - ``order_cancelled``    — pending order cancelled OR (per D-080)
    a cTrader-internal STOP_LOSS_TAKE_PROFIT cleanup we silently
    ignore when the broker_order_id has no matching Redis order.
  - ``reconcile_snapshot`` — emitted ONCE per client startup; the
    handler diffs against Redis and dispatches
    ``fetch_close_history`` commands for positions that closed
    during the offline window.

Correlation: ``p_broker_order_id_to_order:{broker_order_id}``
side-index (set by ``response_handler`` on open-fill, swapped by this
handler on ``pending_filled``).

Per D-074, ``realized_pnl`` on ``position_closed`` events comes from
``deal.closePositionDetail.grossProfit`` raw — we copy it verbatim,
no recomputation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)

# Channel names per BroadcastService docstring (Phase 3 reservation).
ORDERS_CHANNEL = "orders"
POSITIONS_CHANNEL = "positions"

_READ_COUNT = 10


async def event_handler_loop(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    *,
    block_ms: int = 1000,
    read_count: int = _READ_COUNT,
) -> None:
    """Run the event-consume loop for one FTMO account.

    Lifespan cancels via ``Task.cancel()``; all other exceptions are
    logged and the loop continues. ACK policy matches
    ``response_handler``: only ACK after successful handle.

    ``block_ms`` defaults to 1000ms — short enough for prompt
    shutdown, long enough to avoid CPU-burn on an idle stream.
    """
    stream = f"event_stream:ftmo:{account_id}"
    logger.info("event_handler_loop starting: stream=%s", stream)
    try:
        while True:
            try:
                entries = await redis_svc.read_events(
                    "ftmo", account_id, count=read_count, block_ms=block_ms
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("event_handler_loop XREADGROUP error; continuing")
                await asyncio.sleep(1.0)
                continue

            for _stream_name, stream_entries in entries:
                for entry_id, fields in stream_entries:
                    try:
                        await _handle_event_entry(redis_svc, broadcast, account_id, fields)
                    except Exception:
                        logger.exception(
                            "event_handler entry processing failed: entry_id=%s fields=%s",
                            entry_id,
                            fields,
                        )
                        continue
                    try:
                        await redis_svc.ack(stream, "server", entry_id)
                    except Exception:
                        logger.exception("event_handler XACK failed: entry_id=%s", entry_id)
    except asyncio.CancelledError:
        logger.info("event_handler_loop cancelled: account_id=%s", account_id)
        raise


async def _handle_event_entry(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Route an event_stream entry by ``event_type``."""
    event_type = fields.get("event_type", "")

    if event_type == "position_closed":
        await _handle_position_closed(redis_svc, broadcast, account_id, fields)
    elif event_type == "pending_filled":
        await _handle_pending_filled(redis_svc, broadcast, account_id, fields)
    elif event_type == "position_modified":
        await _handle_position_modified(redis_svc, broadcast, account_id, fields)
    elif event_type == "order_cancelled":
        await _handle_order_cancelled(redis_svc, broadcast, account_id, fields)
    elif event_type == "reconcile_snapshot":
        await _handle_reconcile_snapshot(redis_svc, broadcast, account_id, fields)
    else:
        logger.warning("unknown event_type: %s account=%s", event_type, account_id)


# ---------- event handlers ----------


async def _handle_position_closed(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Update order on unsolicited or reconstructed close.

    Writes the extended close-detail fields (commission, swap,
    balance_after_close, money_digits, closed_volume per step 3.5a)
    alongside the close price + reason. ``realized_pnl`` is copied
    verbatim from the event — D-074 forbids recomputation.

    ``reconstructed=true`` marker (step 3.5b) is preserved on the
    order row so audit logs + UI can flag "Closed during offline
    window" rather than "Closed just now".
    """
    position_id = fields.get("position_id") or fields.get("broker_order_id", "")
    if not position_id:
        logger.warning(
            "position_closed event missing position_id: account=%s fields=%s",
            account_id,
            fields,
        )
        return

    order_id = await redis_svc.find_order_id_by_p_broker_order_id(position_id)
    if order_id is None:
        # Possible causes:
        #   - Position opened directly on cTrader UI (out of band) —
        #     we don't have a Redis row to update; ignore.
        #   - Race: live close arrived before the open response's
        #     side-index write completed. response_handler will
        #     eventually run and create the row; this event is lost
        #     to that race but the order row's eventual state will
        #     come from a later reconcile_snapshot's fetch_close_history.
        #   - request_id_to_order TTL expired (24h+).
        logger.warning(
            "position_closed for unknown position_id=%s (out-of-band? race? expired?): account=%s",
            position_id,
            account_id,
        )
        return

    reconstructed = fields.get("reconstructed", "").lower() in ("true", "1")
    now_ms = str(int(time.time() * 1000))

    updates: dict[str, str] = {
        "p_status": "closed",
        "status": "closed",
        "p_close_price": fields.get("close_price", ""),
        "p_closed_at": fields.get("close_time", ""),
        # D-074: copy realized_pnl from the event verbatim; do NOT
        # recompute from price arithmetic.
        "p_realized_pnl": fields.get("realized_pnl", ""),
        "p_commission": fields.get("commission", ""),
        "p_swap": fields.get("swap", ""),
        "p_balance_after_close": fields.get("balance_after_close", ""),
        "p_money_digits": fields.get("money_digits", ""),
        "p_closed_volume": fields.get("closed_volume", ""),
        "p_close_reason": fields.get("close_reason", "unknown"),
        "updated_at": now_ms,
    }
    if reconstructed:
        updates["p_reconstructed"] = "true"

    await redis_svc.update_order(order_id, updates)

    await broadcast.publish(
        POSITIONS_CHANNEL,
        {
            "type": "position_event",
            "event_type": "closed",
            "order_id": order_id,
            "position_id": position_id,
            "close_price": fields.get("close_price", ""),
            "close_time": fields.get("close_time", ""),
            "realized_pnl": fields.get("realized_pnl", ""),
            "close_reason": fields.get("close_reason", "unknown"),
            "reconstructed": reconstructed,
        },
    )


async def _handle_pending_filled(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Migrate a pending order to a filled position (D-061).

    The pending phase used cTrader's ``order.orderId`` as
    ``p_broker_order_id``. When the order finally trips its trigger
    price and creates a position, the bridge publishes
    ``pending_filled`` carrying both IDs. We:
      1. Resolve our order via the OLD orderId.
      2. Overwrite ``p_broker_order_id`` with the NEW positionId.
      3. Drop the orderId → order_id index entry and create the new
         positionId → order_id entry, so future position-side events
         (close, modify) can resolve correctly.
    """
    order_id_old = fields.get("order_id_old", "")
    position_id_new = fields.get("position_id") or fields.get("broker_order_id", "")
    if not order_id_old or not position_id_new:
        logger.warning(
            "pending_filled event missing IDs: order_id_old=%r position_id=%r",
            order_id_old,
            position_id_new,
        )
        return

    order_id = await redis_svc.find_order_id_by_p_broker_order_id(order_id_old)
    if order_id is None:
        logger.warning(
            "pending_filled for unknown order_id_old=%s (out-of-band? expired?)",
            order_id_old,
        )
        return

    now_ms = str(int(time.time() * 1000))
    await redis_svc.update_order(
        order_id,
        {
            "p_status": "filled",
            "status": "filled",
            "p_broker_order_id": position_id_new,
            "p_fill_price": fields.get("fill_price", ""),
            "p_executed_at": fields.get("fill_time", ""),
            "p_commission": fields.get("commission", ""),
            "updated_at": now_ms,
        },
    )

    # Side-index migration: orderId → positionId.
    await redis_svc.unlink_broker_order_id("p", order_id_old)
    await redis_svc.link_broker_order_id("p", position_id_new, order_id)

    await broadcast.publish(
        POSITIONS_CHANNEL,
        {
            "type": "position_event",
            "event_type": "pending_filled",
            "order_id": order_id,
            "order_id_old": order_id_old,
            "position_id_new": position_id_new,
            "fill_price": fields.get("fill_price", ""),
        },
    )


async def _handle_position_modified(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """User modified SL/TP via cTrader UI — sync our SL/TP fields.

    Empty ``new_sl`` / ``new_tp`` means the operator cleared that
    side. We write the empty string through to the order row so the
    frontend can render "—" instead of a stale price.
    """
    position_id = fields.get("position_id") or fields.get("broker_order_id", "")
    order_id = await redis_svc.find_order_id_by_p_broker_order_id(position_id)
    if order_id is None:
        logger.warning("position_modified for unknown position_id=%s; ignoring", position_id)
        return

    new_sl = fields.get("new_sl", "")
    new_tp = fields.get("new_tp", "")
    now_ms = str(int(time.time() * 1000))
    await redis_svc.update_order(
        order_id,
        {
            "sl_price": new_sl,
            "tp_price": new_tp,
            "updated_at": now_ms,
        },
    )

    await broadcast.publish(
        POSITIONS_CHANNEL,
        {
            "type": "position_event",
            "event_type": "modified",
            "order_id": order_id,
            "position_id": position_id,
            "new_sl": new_sl,
            "new_tp": new_tp,
        },
    )


async def _handle_order_cancelled(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Handle ``order_cancelled`` events (D-080).

    cTrader emits ``ORDER_CANCELLED`` for the internal
    STOP_LOSS_TAKE_PROFIT synthetic order whenever a position with
    SL/TP closes. These events arrive with the cTrader-internal
    orderId that has no matching Redis row — we silently drop them.

    A LEGITIMATE cancel (user cancelled a pending limit/stop order
    from the cTrader UI) carries a ``broker_order_id`` that DOES
    match our side-index — we update the order row to ``cancelled``
    and drop the index entry so a stale event doesn't accidentally
    route to a future order with the same id.
    """
    broker_order_id = fields.get("broker_order_id", "")
    if not broker_order_id:
        # Defensive — should never happen since the bridge always
        # sets this field for ``order_cancelled``.
        return

    order_id = await redis_svc.find_order_id_by_p_broker_order_id(broker_order_id)
    if order_id is None:
        # D-080: cTrader internal STOP_LOSS_TAKE_PROFIT cleanup OR a
        # pending order from before this server instance / expired
        # side-index. Silent ignore.
        logger.debug(
            "order_cancelled for unknown broker_order_id=%s "
            "(cTrader internal cleanup or out-of-band)",
            broker_order_id,
        )
        return

    now_ms = str(int(time.time() * 1000))
    await redis_svc.update_order(
        order_id,
        {
            "p_status": "cancelled",
            "status": "cancelled",
            "updated_at": now_ms,
        },
    )
    await redis_svc.unlink_broker_order_id("p", broker_order_id)

    await broadcast.publish(
        ORDERS_CHANNEL,
        {
            "type": "order_updated",
            "order_id": order_id,
            "p_status": "cancelled",
            "status": "cancelled",
        },
    )


async def _handle_reconcile_snapshot(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Diff cTrader's view against Redis state and dispatch
    ``fetch_close_history`` for any positions that closed offline.

    Race tolerance: if a live ``position_closed`` event for an open
    Redis order was already consumed earlier in this event-loop pass,
    the Redis row is now ``status=closed`` and not in
    ``orders:by_status:open`` — it doesn't get diffed against the
    snapshot, so no double-dispatch.

    Phase 3 limitation for PENDING orders: if a pending Redis order
    is absent from the snapshot, we mark it ``p_status=unknown``
    without trying to figure out whether it filled or was cancelled.
    The cTrader Open API doesn't expose a single "order history"
    endpoint that distinguishes the two cleanly; Phase 4+ can layer
    that in.
    """
    positions_json = fields.get("positions", "[]")
    pending_orders_json = fields.get("pending_orders", "[]")

    try:
        ctrader_positions = json.loads(positions_json)
        ctrader_pending = json.loads(pending_orders_json)
    except json.JSONDecodeError:
        logger.error(
            "reconcile_snapshot has invalid JSON: account=%s positions=%r pending=%r",
            account_id,
            positions_json[:200],
            pending_orders_json[:200],
        )
        return

    ctrader_position_ids = {str(p["position_id"]) for p in ctrader_positions}
    ctrader_order_ids = {str(o["order_id"]) for o in ctrader_pending}

    open_orders = await redis_svc.list_open_orders_by_account("ftmo", account_id)

    dispatched_close_history = 0
    marked_unknown = 0
    now_ms = str(int(time.time() * 1000))

    for order in open_orders:
        order_id = order.get("order_id", "")
        p_status = order.get("p_status", "")
        p_broker_order_id = order.get("p_broker_order_id", "")

        if not p_broker_order_id:
            # An order without a broker id can't be reconciled — it
            # never got past pre-fill. Leave it; a later close ACK
            # will resolve.
            continue

        if p_status == "filled":
            if p_broker_order_id not in ctrader_position_ids:
                logger.info(
                    "reconcile: order=%s position_id=%s closed during offline "
                    "window; dispatching fetch_close_history",
                    order_id,
                    p_broker_order_id,
                )
                await redis_svc.push_command(
                    "ftmo",
                    account_id,
                    {
                        "order_id": order_id,
                        "action": "fetch_close_history",
                        "broker_order_id": p_broker_order_id,
                        "symbol": order.get("symbol", ""),
                    },
                )
                dispatched_close_history += 1
            # If present, the position is still open — nothing to do.
        elif p_status == "pending":
            if p_broker_order_id not in ctrader_order_ids:
                logger.warning(
                    "reconcile: pending order=%s order_id=%s missing from "
                    "snapshot (filled or cancelled offline?); marking unknown",
                    order_id,
                    p_broker_order_id,
                )
                await redis_svc.update_order(
                    order_id,
                    {
                        "p_status": "unknown",
                        "updated_at": now_ms,
                    },
                )
                await broadcast.publish(
                    ORDERS_CHANNEL,
                    {
                        "type": "order_updated",
                        "order_id": order_id,
                        "p_status": "unknown",
                        "reason": "missing_after_reconcile",
                    },
                )
                marked_unknown += 1

    logger.info(
        "reconcile_snapshot processed: account=%s ctrader_positions=%d "
        "ctrader_pending=%d redis_open=%d dispatched=%d marked_unknown=%d",
        account_id,
        len(ctrader_position_ids),
        len(ctrader_order_ids),
        len(open_orders),
        dispatched_close_history,
        marked_unknown,
    )
