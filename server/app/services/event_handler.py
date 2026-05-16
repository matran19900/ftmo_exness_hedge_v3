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
from typing import TYPE_CHECKING

from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService

if TYPE_CHECKING:
    from app.services.alert_service import AlertService
    from app.services.hedge_service import HedgeService

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
    broker: str = "ftmo",
    block_ms: int = 1000,
    read_count: int = _READ_COUNT,
    alert_service: AlertService | None = None,
    hedge_service: HedgeService | None = None,
) -> None:
    """Run the event-consume loop for one (broker, account) pair.

    Lifespan cancels via ``Task.cancel()``; all other exceptions are
    logged and the loop continues. ACK policy matches
    ``response_handler``: only ACK after successful handle.

    Step 4.7b — ``broker`` parameter drives handler selection:
      - ``"ftmo"`` (default, Phase 3): the legacy 5-branch dispatcher
        for position_closed / pending_filled / position_modified /
        order_cancelled / reconcile_snapshot.
      - ``"exness"``: the new dispatcher for
        ``position_closed_external`` / ``position_modified`` (Exness
        position_monitor's vocabulary, distinct from FTMO's).

    ``alert_service`` is required when ``broker == "exness"`` for the
    WARNING-routing branches; FTMO Phase 3 handlers do not use it.

    ``block_ms`` defaults to 1000ms — short enough for prompt
    shutdown, long enough to avoid CPU-burn on an idle stream.
    """
    stream = f"event_stream:{broker}:{account_id}"
    logger.info(
        "event_handler_loop starting: broker=%s stream=%s", broker, stream
    )
    try:
        while True:
            try:
                entries = await redis_svc.read_events(
                    broker, account_id, count=read_count, block_ms=block_ms
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
                        await _handle_event_entry(
                            redis_svc, broadcast, account_id, fields,
                            broker=broker, alert_service=alert_service,
                            hedge_service=hedge_service,
                        )
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
        logger.info(
            "event_handler_loop cancelled: broker=%s account_id=%s",
            broker, account_id,
        )
        raise


async def _handle_event_entry(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    fields: dict[str, str],
    *,
    broker: str = "ftmo",
    alert_service: AlertService | None = None,
    hedge_service: HedgeService | None = None,
) -> None:
    """Route an event_stream entry by ``event_type`` + ``broker``."""
    event_type = fields.get("event_type", "")

    if broker == "exness":
        if event_type == "position_closed_external":
            await _handle_exness_position_closed_external(
                redis_svc, account_id, fields, alert_service,
                hedge_service=hedge_service,
            )
        elif event_type == "position_modified":
            await _handle_exness_position_modified(
                redis_svc, account_id, fields, alert_service,
            )
        elif event_type == "position_new":
            # position_new is informational on the Exness side — the
            # cascade orchestrator (step 4.7a HedgeService) issued the
            # open; this event just confirms the broker saw it. Phase
            # 4.7b: log + ACK. Future step 4.8 may use it for cascade
            # close re-correlation.
            logger.debug(
                "exness.position_new ticket=%s account=%s",
                fields.get("broker_position_id", ""), account_id,
            )
        else:
            logger.warning(
                "unknown exness event_type: %s account=%s", event_type, account_id
            )
        return

    # FTMO broker (Phase 3 vocabulary).
    if event_type == "position_closed":
        await _handle_position_closed(
            redis_svc, broadcast, account_id, fields,
            hedge_service=hedge_service,
        )
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
    *,
    hedge_service: HedgeService | None = None,
) -> None:
    """Update order on unsolicited or reconstructed close.

    Writes the extended close-detail fields (commission, swap,
    balance_after_close, money_digits, closed_volume per step 3.5a)
    alongside the close price + reason. ``realized_pnl`` is copied
    verbatim from the event — D-074 forbids recomputation.

    ``reconstructed=true`` marker (step 3.5b) is preserved on the
    order row so audit logs + UI can flag "Closed during offline
    window" rather than "Closed just now".

    Step 4.8 — for hedge orders (``exness_account_id`` populated), the
    composed status transition is NOT applied here. Instead we stamp
    the per-leg fields and invoke
    ``HedgeService.cascade_close_other_leg`` to drive composed status
    through ``close_pending`` to ``closed`` / ``close_failed``. The
    trigger_path is derived from ``close_trigger_initiated`` (Path A
    marker written by the close endpoint) and ``close_reason`` (FTMO
    enum sl / tp / manual / unknown):
      - flag=="A" → trigger_path="A".
      - close_reason in ("sl","tp") → trigger_path="D".
      - close_reason=="manual" → trigger_path="B".
      - close_reason=="stopout" → trigger_path="E".
      - close_reason=="unknown" → trigger_path="B" (defensive default).
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
    close_reason = fields.get("close_reason", "unknown")

    # Step 4.8 — hedge orders go through cascade_close_other_leg which
    # owns the composed status transition. Stamp per-leg fields here
    # (single-leg orders also need them), but only flip composed
    # ``status=closed`` for single-leg orders.
    order_row = await redis_svc.get_order(order_id)
    is_hedge = bool((order_row or {}).get("exness_account_id", "").strip())

    updates: dict[str, str] = {
        "p_status": "closed",
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
        "p_close_reason": close_reason,
        "updated_at": now_ms,
    }
    if not is_hedge:
        updates["status"] = "closed"
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
            "close_reason": close_reason,
            "reconstructed": reconstructed,
        },
    )

    # Step 4.8 — cascade close the Exness leg for hedge orders. Trigger
    # path classifier per docstring.
    if is_hedge and hedge_service is not None:
        trigger_path = _derive_cascade_trigger_path(
            {k: str(v) for k, v in (order_row or {}).items()},
            close_reason,
        )
        await hedge_service.cascade_close_other_leg(
            order_id,
            closed_leg="p",
            close_reason=close_reason,
            trigger_path=trigger_path,
        )
    elif is_hedge and hedge_service is None:
        logger.error(
            "ftmo_position_closed.no_hedge_service order_id=%s — "
            "cascade NOT fired; Exness leg may be orphaned",
            order_id,
        )


def _derive_cascade_trigger_path(
    order_row: dict[str, str], close_reason: str
) -> str:
    """Step 4.8 — map the (Path A flag, FTMO close_reason) pair to the
    cascade_lock trigger_path tag.

    The flag distinguishes operator-via-API (Path A) from operator-via-
    cTrader-UI (Path B) when both paths produce ``close_reason="manual"``
    on the event. SL/TP hits ride the same close_reason vocabulary the
    FTMO client publishes (``sl`` / ``tp`` / ``manual`` / ``unknown``;
    see apps/ftmo-client/ftmo_client/event_publisher.py::_infer_close_reason).
    """
    if order_row.get("close_trigger_initiated") == "A":
        return "A"
    if close_reason in ("sl", "tp"):
        return "D"
    if close_reason == "stopout":
        return "E"
    # manual / unknown / any future slug — external bucket default.
    return "B"


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


# ---------- step 4.7b: Exness event handlers ----------


_EXTERNAL_CLOSE_REASONS = {
    # 2-bucket classifier per step 4.3a + design §1.B:
    # ``server_initiated`` is the only "cascade FTMO" trigger; all
    # other values fall into the external bucket and surface as
    # WARNING alerts here. The explicit allowlist is defensive — a
    # future position_monitor enrichment that adds a new slug will
    # default into the external WARNING path (safer than silent drop).
    "external",
    "sl_hit",
    "tp_hit",
    "stop_out",
    "manual",
}


async def _resolve_pair_name(
    redis_svc: RedisService, pair_id: str
) -> str:
    """Best-effort pair name lookup for alert templates. Returns the
    pair name when present, else the raw pair_id, else "unknown".
    Orphan-order safe: deleted pair surfaces as pair_id.
    """
    if not pair_id:
        return "unknown"
    pair = await redis_svc.get_pair(pair_id)
    if pair and pair.get("name"):
        return pair["name"]
    return pair_id


async def _handle_exness_position_closed_external(
    redis_svc: RedisService,
    account_id: str,
    fields: dict[str, str],
    alert_service: AlertService | None,
    *,
    hedge_service: HedgeService | None = None,
) -> None:
    """Step 4.7b + 4.8 — route an Exness ``position_closed_external`` event.

    2-bucket classification (step 4.3a):
      - ``close_reason == "server_initiated"`` → Path C completion. The
        Exness ticket we ourselves issued a close cmd for finally landed
        on the broker. Step 4.8 calls ``HedgeService.complete_cascade_close``
        to stamp the order terminal ``closed`` + release the cascade lock.
        No WARNING fires (this is OUR cascade completing).
      - any other close_reason (``sl_hit`` / ``tp_hit`` / ``stop_out``
        / ``manual`` / ``external``) → WARNING alert (step 4.7b). NO
        cascade FTMO (R3 + design §1.B — secondary is passive).
    """
    ticket = fields.get("broker_position_id", "")
    close_reason = fields.get("close_reason", "external")

    if close_reason == "server_initiated":
        # Path C completion. Resolve order_id from the ticket index.
        order_id = await redis_svc.find_order_id_by_s_broker_order_id(ticket)
        if order_id is None:
            logger.warning(
                "event.closed_external.server_initiated_order_not_found "
                "ticket=%s account=%s",
                ticket, account_id,
            )
            return
        # Stamp the per-leg fields (close price / time) on the order row
        # before invoking the cascade completion so the audit trail
        # captures the broker-side fill data.
        await redis_svc.update_order(
            order_id,
            patch={
                "s_close_price": fields.get("close_price", ""),
                "s_closed_at": fields.get("close_time_ms", ""),
                "s_close_reason": "server_initiated",
                "updated_at": str(int(time.time() * 1000)),
            },
        )
        if hedge_service is None:
            logger.error(
                "event.closed_external.server_initiated_no_hedge_service "
                "order_id=%s — cascade completion NOT applied",
                order_id,
            )
            return
        await hedge_service.complete_cascade_close(
            order_id, closed_leg="s", close_reason="server_initiated",
        )
        return

    if close_reason not in _EXTERNAL_CLOSE_REASONS:
        logger.warning(
            "event.closed_external.unknown_close_reason "
            "ticket=%s account=%s close_reason=%s — defaulting to external bucket",
            ticket, account_id, close_reason,
        )

    order_id = await redis_svc.find_order_id_by_s_broker_order_id(ticket)
    if order_id is None:
        logger.warning(
            "event.closed_external.order_not_found ticket=%s account=%s "
            "close_reason=%s",
            ticket, account_id, close_reason,
        )
        return

    if alert_service is None:
        logger.error(
            "event.closed_external.no_alert_service ticket=%s account=%s "
            "— WARNING NOT emitted",
            ticket, account_id,
        )
        return

    order = await redis_svc.get_order(order_id) or {}
    pair_id = order.get("pair_id", "")
    pair_name = await _resolve_pair_name(redis_svc, pair_id)
    close_price = fields.get("close_price", "")

    # Step 4.8e — stamp the order HASH so composed status reflects broker
    # truth BEFORE emitting the WARNING. Pre-4.8e this branch only fired
    # the alert; ``s_status`` stayed "filled" while the broker had already
    # closed the position, producing two knock-on bugs:
    #   1. Phantom open row in PositionList (frontend drops via
    #      useWebSocket.ts:169 when status==="closed").
    #   2. Double-close hazard: API endpoint _CLOSEABLE_STATUSES rejects
    #      a second close attempt with order_not_closeable.
    # NO cascade FTMO is triggered (R3 + design §1.B — secondary passive
    # policy). The FTMO leg stays open as an orphan and the WARNING
    # below tells the operator to clean it up manually.
    #
    # Idempotent: a duplicate event (e.g. reconcile_state replay) finds
    # s_status == "closed" already and skips the stamp. The alert
    # cooldown_key=order_id suppresses the duplicate WARNING separately.
    if order.get("s_status") != "closed":
        await redis_svc.update_order(
            order_id,
            patch={
                "s_status": "closed",
                "s_close_price": close_price,
                "s_closed_at": fields.get("close_time_ms", ""),
                "s_close_reason": close_reason,
                "s_realized_pnl": fields.get("realized_profit", ""),
                "s_commission": fields.get("commission", ""),
                "status": "closed",
                "updated_at": str(int(time.time() * 1000)),
            },
        )

    title_vi = "⚠️ Lệnh Exness đóng ngoài hệ thống"
    body_vi = (
        f"Order {order_id} ({pair_name}): Exness leg ticket {ticket} "
        f"đã đóng với lý do '{close_reason}' (giá đóng {close_price}). "
        f"FTMO leg vẫn mở — cần kiểm tra thủ công."
    )

    await alert_service.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key=order_id,
        title_vi=title_vi,
        body_vi=body_vi,
        context={
            "order_id": order_id,
            "pair_id": pair_id,
            "exness_account_id": account_id,
            "exness_ticket": ticket,
            "close_reason": close_reason,
            "close_price": close_price,
            "close_time_ms": fields.get("close_time_ms", ""),
        },
    )
    # NO cascade FTMO. R3 + design §1.B — secondary passive policy means
    # the server does NOT auto-close the FTMO leg to mirror an external
    # Exness close. Operator manually closes the FTMO orphan via the
    # cTrader UI (Phase 5 backlog: server-side "force-close FTMO orphan"
    # endpoint).


async def _handle_exness_position_modified(
    redis_svc: RedisService,
    account_id: str,
    fields: dict[str, str],
    alert_service: AlertService | None,
) -> None:
    """Step 4.7b — route an Exness ``position_modified`` event.

    Filter on ``changed_fields`` (a comma-separated string from
    position_monitor):
      - ``["volume"]`` only → silent ignore. Phase 4 has no partial-close
        support so a volume-only change is operator-side bookkeeping
        beyond our state machine; DEBUG log + return.
      - SL or TP changed (with or without volume) → WARNING alert with
        the SL/TP delta in ``body_vi``. NO modify FTMO (R3 forbids
        server-issued modify on Exness).
      - Unknown change_fields → log warning + drop.
    """
    ticket = fields.get("broker_position_id", "")
    changed_raw = fields.get("changed_fields", "")
    changed_fields = [c.strip() for c in changed_raw.split(",") if c.strip()]

    if changed_fields == ["volume"]:
        logger.debug(
            "event.modified.volume_only_ignored ticket=%s account=%s "
            "old=%s new=%s",
            ticket, account_id,
            fields.get("old_volume", ""), fields.get("new_volume", ""),
        )
        return

    has_sl = "sl" in changed_fields
    has_tp = "tp" in changed_fields

    if not (has_sl or has_tp):
        logger.warning(
            "event.modified.unknown_change_fields ticket=%s account=%s fields=%s",
            ticket, account_id, changed_fields,
        )
        return

    order_id = await redis_svc.find_order_id_by_s_broker_order_id(ticket)
    if order_id is None:
        logger.warning(
            "event.modified.order_not_found ticket=%s account=%s fields=%s",
            ticket, account_id, changed_fields,
        )
        return

    if alert_service is None:
        logger.error(
            "event.modified.no_alert_service ticket=%s account=%s "
            "— WARNING NOT emitted",
            ticket, account_id,
        )
        return

    order = await redis_svc.get_order(order_id) or {}
    pair_id = order.get("pair_id", "")
    pair_name = await _resolve_pair_name(redis_svc, pair_id)

    old_sl = fields.get("old_sl", "?")
    new_sl = fields.get("new_sl", "?")
    old_tp = fields.get("old_tp", "?")
    new_tp = fields.get("new_tp", "?")
    sl_part = f"SL {old_sl}→{new_sl}" if has_sl else ""
    tp_part = f"TP {old_tp}→{new_tp}" if has_tp else ""
    changes_summary = ", ".join(p for p in (sl_part, tp_part) if p)

    title_vi = "⚠️ Lệnh Exness bị sửa SL/TP ngoài hệ thống"
    body_vi = (
        f"Order {order_id} ({pair_name}): Exness leg ticket {ticket} "
        f"có thay đổi: {changes_summary}. Phase 4 không tự revert; "
        f"kiểm tra MT5 thủ công."
    )

    # Cooldown key includes sorted SL/TP signal so an SL-only change
    # followed by a separate TP change in the same window emits BOTH
    # alerts (different keys), rather than the second one being
    # suppressed by the first.
    signal = ",".join(sorted(f for f in (("sl" if has_sl else ""), ("tp" if has_tp else "")) if f))

    await alert_service.emit(
        alert_type="hedge_leg_external_modify_warning",
        cooldown_key=f"{order_id}:{signal}",
        title_vi=title_vi,
        body_vi=body_vi,
        context={
            "order_id": order_id,
            "pair_id": pair_id,
            "exness_account_id": account_id,
            "exness_ticket": ticket,
            "changed_fields": json.dumps(changed_fields),
            "old_sl": old_sl,
            "new_sl": new_sl,
            "old_tp": old_tp,
            "new_tp": new_tp,
        },
    )
    # NO modify cmd published to cmd_stream:exness. Phase 4 R3 explicitly
    # forbids server-issued modify on the Exness hedge leg.
