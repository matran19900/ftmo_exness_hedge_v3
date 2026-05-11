"""Response handler — consume ``resp_stream:ftmo:{acc}`` (step 3.7).

One background task per FTMO account, started in the FastAPI lifespan
(``app/main.py``). The loop XREADGROUPs the account's resp_stream,
routes each entry by ``action`` field to the matching handler, updates
the order row in Redis, and publishes a structured ``order_updated``
message over the WS ``orders`` channel.

Correlation: ``request_id_to_order:{request_id}`` (set by
``OrderService.create_order`` in step 3.6) maps the response back to
the originating order_id. The handler is idempotent on the Redis side
— ``RedisService.update_order`` applies via Lua so concurrent updates
don't race.

ACK policy: an entry is ACKed ONLY after its handler returns without
exception. Exceptions are logged + the entry stays in the pending list
so the next ``XREADGROUP > `` cycle does NOT re-deliver it (per Redis
Streams semantics; `>` only returns never-delivered messages). A
follow-up step would add a dead-letter sweeper for pending entries
older than a threshold — out of scope for 3.7.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)

# Channel name the frontend subscribes to for solicited-response
# updates (POST /api/orders fills, manual close ACKs, modify ACKs,
# fetch_close_history ACKs). Reserved by ``BroadcastService``'s
# docstring for Phase 3 traffic.
ORDERS_CHANNEL = "orders"

# How many entries to drain per XREADGROUP call. Production traffic is
# low (operator-issued, not algorithmic), so 10 is generous; the value
# matters mostly for startup catch-up after a server restart.
_READ_COUNT = 10


async def response_handler_loop(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    *,
    block_ms: int = 1000,
    read_count: int = _READ_COUNT,
) -> None:
    """Run the response-consume loop for one FTMO account.

    The loop exits when ``asyncio.Task.cancel()`` is called on it from
    the lifespan shutdown handler. All other errors are logged and the
    loop continues — a flaky Redis or a single bad entry should not
    take down the consumer.

    ``block_ms`` is exposed so unit tests can pass a small value (or
    0) to avoid blocking. Production uses 1000 ms which is short
    enough for prompt shutdown response.

    ``read_count`` is similarly tunable for tests; defaults to 10.
    """
    stream = f"resp_stream:ftmo:{account_id}"
    logger.info("response_handler_loop starting: stream=%s", stream)
    try:
        while True:
            try:
                entries = await redis_svc.read_responses(
                    "ftmo", account_id, count=read_count, block_ms=block_ms
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("response_handler_loop XREADGROUP error; continuing")
                await asyncio.sleep(1.0)
                continue

            for _stream_name, stream_entries in entries:
                for entry_id, fields in stream_entries:
                    try:
                        await _handle_response_entry(redis_svc, broadcast, account_id, fields)
                    except Exception:
                        logger.exception(
                            "response_handler entry processing failed: entry_id=%s fields=%s",
                            entry_id,
                            fields,
                        )
                        # Do NOT ACK — leave in pending list for a future
                        # dead-letter sweep (Phase 5).
                        continue
                    try:
                        await redis_svc.ack(stream, "server", entry_id)
                    except Exception:
                        # ACK failure is non-fatal: the entry stays pending
                        # and the next pass will re-process it. Log loudly
                        # so an operator can investigate.
                        logger.exception("response_handler XACK failed: entry_id=%s", entry_id)
    except asyncio.CancelledError:
        logger.info("response_handler_loop cancelled: account_id=%s", account_id)
        raise


async def _handle_response_entry(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Route a resp_stream entry by ``action`` field.

    Common shape: every entry MUST carry ``action`` + ``request_id``
    (FTMO client guarantees this — step 3.4's ``_publish_response`` /
    ``_publish_error`` always set both). Without ``request_id`` we
    can't correlate back to an order row; the entry is logged and
    silently dropped (still ACKed by caller).
    """
    action = fields.get("action", "")
    request_id = fields.get("request_id", "")

    if not request_id:
        logger.warning(
            "resp_stream entry missing request_id: account=%s action=%s",
            account_id,
            action,
        )
        return

    order_id = await redis_svc.find_order_by_request_id(request_id)
    if order_id is None:
        # Request_id index TTL is 24h (step 3.1). A stale response
        # past that window simply can't be routed — the order row
        # has already been processed via reconciliation by now.
        logger.warning(
            "resp_stream entry for unknown request_id=%s "
            "(TTL expired? wrong account?): account=%s action=%s",
            request_id,
            account_id,
            action,
        )
        return

    if action == "open":
        await _handle_open_response(redis_svc, broadcast, order_id, fields)
    elif action == "close":
        await _handle_close_response(redis_svc, broadcast, order_id, fields)
    elif action == "modify_sl_tp":
        await _handle_modify_response(redis_svc, broadcast, order_id, fields)
    elif action == "fetch_close_history":
        await _handle_fetch_close_history_response(redis_svc, broadcast, order_id, fields)
    else:
        logger.warning("unknown action in resp_stream: action=%s order_id=%s", action, order_id)


# ---------- action handlers ----------


async def _handle_open_response(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    order_id: str,
    fields: dict[str, str],
) -> None:
    """Handle an ``open`` response.

    Branches on ``status``:
      - ``"success"`` with ``fill_price`` populated → market fill;
        order goes to ``status=filled``, ``p_status=filled``.
      - ``"success"`` with empty ``fill_price`` → pending limit/stop
        sitting in the cTrader order book; ``p_status=pending``
        (already pending on creation; this confirms the broker
        accepted it).
      - ``"error"`` → ``status=rejected``, ``p_status=rejected``,
        error code + msg copied across.

    Step 3.4a SL/TP attach failure: if the response carries
    ``sl_tp_attach_failed=true`` (market fill succeeded but the
    follow-up amend was rejected), we set ``p_sl_tp_warning=true``
    + a human-readable message so the frontend can surface a toast.
    The position IS open at the broker — operator still needs to
    attach SL/TP manually.
    """
    status = fields.get("status", "")
    now_ms = str(int(time.time() * 1000))

    if status == "success":
        fill_price = fields.get("fill_price", "")
        broker_order_id = fields.get("broker_order_id", "")
        p_status = "filled" if fill_price else "pending"
        order_status = "filled" if fill_price else "pending"

        updates: dict[str, str] = {
            "p_status": p_status,
            "status": order_status,
            "p_broker_order_id": broker_order_id,
            "p_fill_price": fill_price,
            "p_executed_at": fields.get("fill_time", ""),
            "p_commission": fields.get("commission", ""),
            "updated_at": now_ms,
        }

        sl_tp_attach_failed = fields.get("sl_tp_attach_failed", "").lower() in (
            "true",
            "1",
        )
        if sl_tp_attach_failed:
            updates["p_sl_tp_warning"] = "true"
            updates["p_sl_tp_warning_msg"] = fields.get("sl_tp_attach_error_msg", "")

        await redis_svc.update_order(order_id, updates)
        if broker_order_id:
            await redis_svc.link_broker_order_id("p", broker_order_id, order_id)

        await broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "order_updated",
                "order_id": order_id,
                "p_status": p_status,
                "status": order_status,
                "p_broker_order_id": broker_order_id,
                "p_fill_price": fill_price,
                "p_sl_tp_warning": sl_tp_attach_failed,
                "p_sl_tp_warning_msg": updates.get("p_sl_tp_warning_msg", ""),
            },
        )
        return

    if status == "error":
        await redis_svc.update_order(
            order_id,
            {
                "p_status": "rejected",
                "status": "rejected",
                "p_error_code": fields.get("error_code", ""),
                "p_error_msg": fields.get("error_msg", ""),
                "updated_at": now_ms,
            },
        )
        await broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "order_updated",
                "order_id": order_id,
                "p_status": "rejected",
                "status": "rejected",
                "error_code": fields.get("error_code", ""),
                "error_msg": fields.get("error_msg", ""),
            },
        )
        return

    logger.warning(
        "unknown status in open response: status=%s order_id=%s",
        status,
        order_id,
    )


async def _handle_close_response(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    order_id: str,
    fields: dict[str, str],
) -> None:
    """Handle a ``close`` response.

    Success path uses ``realized_pnl`` from the FTMO client's response
    verbatim — that field is the ``deal.closePositionDetail.grossProfit``
    raw int (D-074). We do NOT recompute from
    ``(close_price - entry_price) * volume`` because cTrader handles
    slippage, contract-size scaling, and quote-to-deposit conversion
    internally; the broker's bookkeeping is the source of truth.

    The unsolicited ``position_closed`` event that follows on the
    event_stream carries the same ``realized_pnl`` plus the close
    reason and the extended close-detail fields (commission, swap,
    balance_after_close, etc.). ``event_handler`` writes those —
    here we set only what the response gives us. If the events are
    processed out-of-order (event before response), both writes are
    idempotent and the final state is consistent.
    """
    status = fields.get("status", "")
    now_ms = str(int(time.time() * 1000))

    if status == "success":
        await redis_svc.update_order(
            order_id,
            {
                "p_status": "closed",
                "status": "closed",
                "p_close_price": fields.get("close_price", ""),
                "p_closed_at": fields.get("close_time", ""),
                "p_realized_pnl": fields.get("realized_pnl", ""),
                "updated_at": now_ms,
            },
        )
        await broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "order_updated",
                "order_id": order_id,
                "p_status": "closed",
                "status": "closed",
                "p_close_price": fields.get("close_price", ""),
                "p_realized_pnl": fields.get("realized_pnl", ""),
            },
        )
        return

    if status == "error":
        await redis_svc.update_order(
            order_id,
            {
                "p_close_error_code": fields.get("error_code", ""),
                "p_close_error_msg": fields.get("error_msg", ""),
                "updated_at": now_ms,
            },
        )
        await broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "order_updated",
                "order_id": order_id,
                "close_error_code": fields.get("error_code", ""),
                "close_error_msg": fields.get("error_msg", ""),
            },
        )
        return

    logger.warning(
        "unknown status in close response: status=%s order_id=%s",
        status,
        order_id,
    )


async def _handle_modify_response(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    order_id: str,
    fields: dict[str, str],
) -> None:
    """Handle a ``modify_sl_tp`` response.

    On success the new SL/TP prices echoed by cTrader are written to
    ``sl_price`` / ``tp_price`` on the order row. On error we leave
    the existing SL/TP and surface the error via WS so the frontend
    can show the operator why the amend was rejected (e.g. SL too
    close, position already closed).
    """
    status = fields.get("status", "")
    now_ms = str(int(time.time() * 1000))

    if status == "success":
        await redis_svc.update_order(
            order_id,
            {
                "sl_price": fields.get("new_sl", ""),
                "tp_price": fields.get("new_tp", ""),
                "updated_at": now_ms,
            },
        )
        await broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "order_updated",
                "order_id": order_id,
                "sl_price": fields.get("new_sl", ""),
                "tp_price": fields.get("new_tp", ""),
            },
        )
        return

    if status == "error":
        await redis_svc.update_order(
            order_id,
            {
                "p_modify_error_code": fields.get("error_code", ""),
                "p_modify_error_msg": fields.get("error_msg", ""),
                "updated_at": now_ms,
            },
        )
        await broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "order_updated",
                "order_id": order_id,
                "modify_error_code": fields.get("error_code", ""),
                "modify_error_msg": fields.get("error_msg", ""),
            },
        )


async def _handle_fetch_close_history_response(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    order_id: str,
    fields: dict[str, str],
) -> None:
    """Handle a ``fetch_close_history`` ACK.

    The actual reconstructed ``position_closed`` event is published
    by the FTMO client to event_stream — ``event_handler`` consumes
    it and writes the order row updates. The resp_stream ACK only
    confirms the bridge call completed (or failed for ``not_found``);
    no state change here.

    Logged at info on success + warning on error so an operator can
    see reconciliation progress without grepping for the position-
    side event.
    """
    status = fields.get("status", "")
    if status == "success":
        logger.info(
            "fetch_close_history success ACK: order_id=%s position_id=%s",
            order_id,
            fields.get("position_id", ""),
        )
    elif status == "error":
        logger.warning(
            "fetch_close_history failed: order_id=%s error_code=%s msg=%s",
            order_id,
            fields.get("error_code", ""),
            fields.get("error_msg", ""),
        )
        # Notify the operator via WS so they can investigate stuck
        # reconciled orders. No order-row update — the broker doesn't
        # know about this position any more, and our Redis state is
        # already correct (open).
        await broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "order_updated",
                "order_id": order_id,
                "fetch_close_history_error_code": fields.get("error_code", ""),
                "fetch_close_history_error_msg": fields.get("error_msg", ""),
            },
        )


# Re-export for tests that want to invoke the entry-router directly
# without spinning up the loop. Underscore prefix kept on the real
# symbol; this alias signals "test-only entry point" by convention.
_handle_response_entry_for_test: Any = _handle_response_entry
