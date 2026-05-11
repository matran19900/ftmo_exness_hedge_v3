"""Real action handlers for the FTMO client (step 3.4).

``command_loop`` looks up the handler for each incoming command's
``action`` field, awaits it with ``(redis, bridge, account_id, fields)``,
then XACKs the message. The handler is responsible for:

1. Reading auxiliary state (``symbol_config:{sym}`` for symbol_id +
   lot_size) from Redis.
2. Dispatching to the right ``CtraderBridge`` method.
3. Publishing exactly one entry to ``resp_stream:ftmo:{account_id}`` —
   success or error — so the server-side ``response_handler`` can update
   the order hash + broadcast WS events.

Client-side validation is intentionally narrow: structural checks only
(missing required fields, unknown order_type). Business-rule validation
(SL distance, volume rounding, market-open) is the broker's job; we
surface its rejection back to the server.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis_asyncio

from ftmo_client.ctrader_bridge import CtraderBridge

logger = logging.getLogger(__name__)

# Per docs/06-data-models.md §11 — keep cmd/resp streams under 10k entries
# so a stuck consumer can't blow up Redis memory.
_RESP_STREAM_MAXLEN = 10000

# A handler reads the command fields + drives the bridge + publishes the
# response. The return type is None — XACK is the caller's job, run
# regardless of whether the handler succeeded or raised.
ActionHandler = Callable[
    [redis_asyncio.Redis, CtraderBridge, str, dict[str, str]],
    Awaitable[None],
]


async def handle_open(
    redis: redis_asyncio.Redis,
    bridge: CtraderBridge,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Dispatch open to the right bridge method (market/limit/stop), publish response."""
    request_id = fields.get("request_id", "")
    order_id = fields.get("order_id", "")
    action = "open"
    order_type = fields.get("order_type", "market")
    symbol = fields.get("symbol", "")

    if not symbol:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            "symbol missing",
        )
        return

    symbol_config = await redis.hgetall(f"symbol_config:{symbol}")  # type: ignore[misc]
    if not symbol_config:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "symbol_not_synced",
            f"symbol_config:{symbol} not in Redis — run sync_symbols on server first",
        )
        return

    try:
        symbol_id = int(symbol_config["ctrader_symbol_id"])
        lot_size = int(symbol_config["lot_size"])
    except (KeyError, ValueError) as exc:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "symbol_not_synced",
            f"symbol_config:{symbol} missing or malformed: {exc}",
        )
        return

    side = fields.get("side", "")
    if side not in ("buy", "sell"):
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            f"side must be buy|sell, got {side!r}",
        )
        return

    try:
        volume_lots = float(fields.get("volume_lots", "0"))
    except ValueError:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            "volume_lots not a float",
        )
        return
    if volume_lots <= 0:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            "volume_lots must be > 0",
        )
        return

    # SL/TP/entry default to 0 (means "unset" per docs/05-redis-protocol.md §4.2);
    # the bridge skips zero values when building the protobuf.
    sl_price = _safe_float(fields.get("sl", "0"))
    tp_price = _safe_float(fields.get("tp", "0"))
    entry_price = _safe_float(fields.get("entry_price", "0"))

    try:
        if order_type == "market":
            result: dict[str, Any] = dict(
                await bridge.place_market_order(
                    symbol_id=symbol_id,
                    side=side,  # type: ignore[arg-type]  # validated above
                    volume_lots=volume_lots,
                    lot_size=lot_size,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    client_msg_id=request_id,
                )
            )
        elif order_type == "limit":
            if entry_price <= 0:
                await _publish_error(
                    redis,
                    account_id,
                    order_id,
                    request_id,
                    action,
                    "invalid_request",
                    "entry_price required for limit order",
                )
                return
            result = dict(
                await bridge.place_limit_order(
                    symbol_id=symbol_id,
                    side=side,  # type: ignore[arg-type]
                    volume_lots=volume_lots,
                    lot_size=lot_size,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    client_msg_id=request_id,
                )
            )
        elif order_type == "stop":
            if entry_price <= 0:
                await _publish_error(
                    redis,
                    account_id,
                    order_id,
                    request_id,
                    action,
                    "invalid_request",
                    "entry_price required for stop order",
                )
                return
            result = dict(
                await bridge.place_stop_order(
                    symbol_id=symbol_id,
                    side=side,  # type: ignore[arg-type]
                    volume_lots=volume_lots,
                    lot_size=lot_size,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    client_msg_id=request_id,
                )
            )
        else:
            await _publish_error(
                redis,
                account_id,
                order_id,
                request_id,
                action,
                "invalid_request",
                f"unknown order_type: {order_type}",
            )
            return
    except TimeoutError as exc:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "timeout",
            f"cTrader no response: {exc}",
        )
        return
    except Exception as exc:
        logger.exception("open handler bridge call failed")
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "broker_error",
            str(exc),
        )
        return

    await _publish_response(redis, account_id, order_id, request_id, action, result)


async def handle_close(
    redis: redis_asyncio.Redis,
    bridge: CtraderBridge,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Close a position by broker_order_id."""
    request_id = fields.get("request_id", "")
    order_id = fields.get("order_id", "")
    action = "close"

    broker_order_id_str = fields.get("broker_order_id", "")
    if not broker_order_id_str:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            "broker_order_id missing",
        )
        return
    try:
        position_id = int(broker_order_id_str)
    except ValueError:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            f"broker_order_id not an int: {broker_order_id_str!r}",
        )
        return

    # cTrader's ClosePositionReq requires an explicit volume — there is no
    # "close everything" magic value. Server-side order_service (step 3.6+)
    # is responsible for tracking position size and passing the right
    # volume_lots; we surface an invalid_request here if the caller forgot.
    try:
        volume_lots = float(fields.get("volume_lots", "0"))
    except ValueError:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            "volume_lots not a float",
        )
        return
    if volume_lots <= 0:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            "volume_lots missing or zero",
        )
        return

    # We also need lot_size for the volume conversion. The symbol field is
    # not strictly required by the protocol close action (docs/05 §4.3),
    # but we need it here for the conversion. Server's order_service can
    # be relied on to include it when issuing close commands.
    symbol = fields.get("symbol", "")
    if not symbol:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            "symbol missing (needed for volume conversion)",
        )
        return
    symbol_config = await redis.hgetall(f"symbol_config:{symbol}")  # type: ignore[misc]
    if not symbol_config or "lot_size" not in symbol_config:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "symbol_not_synced",
            f"symbol_config:{symbol} missing lot_size",
        )
        return
    lot_size = int(symbol_config["lot_size"])

    try:
        result: dict[str, Any] = dict(
            await bridge.close_position(
                position_id=position_id,
                volume_lots=volume_lots,
                lot_size=lot_size,
                client_msg_id=request_id,
            )
        )
    except TimeoutError as exc:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "timeout",
            f"cTrader no response: {exc}",
        )
        return
    except Exception as exc:
        logger.exception("close handler bridge call failed")
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "broker_error",
            str(exc),
        )
        return

    await _publish_response(redis, account_id, order_id, request_id, action, result)


async def handle_modify_sl_tp(
    redis: redis_asyncio.Redis,
    bridge: CtraderBridge,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Amend stopLoss / takeProfit on an open position."""
    request_id = fields.get("request_id", "")
    order_id = fields.get("order_id", "")
    action = "modify_sl_tp"

    broker_order_id_str = fields.get("broker_order_id", "")
    if not broker_order_id_str:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            "broker_order_id missing",
        )
        return
    try:
        position_id = int(broker_order_id_str)
    except ValueError:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "invalid_request",
            f"broker_order_id not an int: {broker_order_id_str!r}",
        )
        return

    new_sl = _safe_float(fields.get("sl", "0"))
    new_tp = _safe_float(fields.get("tp", "0"))

    try:
        result: dict[str, Any] = dict(
            await bridge.modify_sl_tp(
                position_id=position_id,
                sl_price=new_sl,
                tp_price=new_tp,
                client_msg_id=request_id,
            )
        )
    except TimeoutError as exc:
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "timeout",
            f"cTrader no response: {exc}",
        )
        return
    except Exception as exc:
        logger.exception("modify_sl_tp handler bridge call failed")
        await _publish_error(
            redis,
            account_id,
            order_id,
            request_id,
            action,
            "broker_error",
            str(exc),
        )
        return

    await _publish_response(redis, account_id, order_id, request_id, action, result)


# Action-name → handler dispatch table. ``command_loop`` looks up by
# ``action`` field on each command; unknown actions are warned + XACKed
# without re-delivery (per docs/05-redis-protocol.md the action vocab is
# closed, so an unknown action is a server-side bug, not a transient fault).
ACTION_HANDLERS: dict[str, ActionHandler] = {
    "open": handle_open,
    "close": handle_close,
    "modify_sl_tp": handle_modify_sl_tp,
}


# ---------- helpers ----------


async def _publish_response(
    redis: redis_asyncio.Redis,
    account_id: str,
    order_id: str,
    request_id: str,
    action: str,
    result: dict[str, Any],
) -> None:
    """Publish a successful or error response to ``resp_stream:ftmo:{acc}``.

    Every field is stringified — Redis Streams accept only string field
    values; the bridge's Result TypedDicts already enforce string fields
    for the broker-supplied values, but caller code paths that synthesize
    a result (validation errors) may pass non-strings. We coerce here so
    the call site doesn't have to.
    """
    success = bool(result.get("success"))
    base: dict[str, str] = {
        "order_id": order_id,
        "request_id": request_id,
        "action": action,
        "status": "success" if success else "error",
    }
    for k, v in result.items():
        if k == "success":
            continue
        base[k] = "" if v is None else str(v)

    await redis.xadd(
        f"resp_stream:ftmo:{account_id}",
        base,  # type: ignore[arg-type]  # redis-py xadd dict-value variance
        maxlen=_RESP_STREAM_MAXLEN,
        approximate=True,
    )
    logger.info(
        "published response: action=%s order_id=%s status=%s",
        action,
        order_id,
        base["status"],
    )


async def _publish_error(
    redis: redis_asyncio.Redis,
    account_id: str,
    order_id: str,
    request_id: str,
    action: str,
    error_code: str,
    error_msg: str,
) -> None:
    """Publish a client-side validation error. No bridge involvement.

    Used when the handler can't even reach the broker — missing required
    fields, unknown order_type, symbol not synced. The server response
    handler treats these as non-retryable (caller bug).
    """
    fields: dict[str, str] = {
        "order_id": order_id,
        "request_id": request_id,
        "action": action,
        "status": "error",
        "error_code": error_code,
        "error_msg": error_msg,
    }
    await redis.xadd(
        f"resp_stream:ftmo:{account_id}",
        fields,  # type: ignore[arg-type]
        maxlen=_RESP_STREAM_MAXLEN,
        approximate=True,
    )
    logger.warning(
        "published error: action=%s order_id=%s code=%s msg=%s",
        action,
        order_id,
        error_code,
        error_msg,
    )


def _safe_float(s: str, default: float = 0.0) -> float:
    """Best-effort float parse. Returns ``default`` on empty / unparseable.

    Used for optional price fields (SL/TP/entry) where an empty string
    from the cmd_stream means "not set" rather than zero.
    """
    if not s:
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default
