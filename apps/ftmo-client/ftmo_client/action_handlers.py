"""Stub action handlers for the FTMO client (step 3.3).

Each handler logs ``[STUB step 3.4]`` and returns. ``command_loop`` is
responsible for XACKing the stream message after the handler returns —
that contract stays the same once step 3.4 replaces these stubs with
real cTrader calls + a response published to ``resp_stream:ftmo:{acc}``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import redis.asyncio as redis_asyncio

logger = logging.getLogger(__name__)

# A handler reads the command fields and produces no return value; any
# response publish, retry, or state mutation is the handler's own
# responsibility. ``command_loop`` only XACKs after the awaitable
# resolves so a handler that crashes leaves the message pending and
# eligible for re-delivery via XPENDING / XCLAIM.
ActionHandler = Callable[[redis_asyncio.Redis, str, dict[str, str]], Awaitable[None]]


async def handle_open_stub(
    _redis: redis_asyncio.Redis,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Log only. Step 3.4 will issue ProtoOANewOrderReq + push response."""
    logger.info(
        "[STUB step 3.4] open: account=%s order_id=%s symbol=%s side=%s "
        "volume_lots=%s order_type=%s entry_price=%s sl=%s tp=%s "
        "request_id=%s",
        account_id,
        fields.get("order_id"),
        fields.get("symbol"),
        fields.get("side"),
        fields.get("volume_lots"),
        fields.get("order_type"),
        fields.get("entry_price"),
        fields.get("sl"),
        fields.get("tp"),
        fields.get("request_id"),
    )


async def handle_close_stub(
    _redis: redis_asyncio.Redis,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Log only. Step 3.4 will issue ProtoOAClosePositionReq."""
    logger.info(
        "[STUB step 3.4] close: account=%s order_id=%s broker_order_id=%s request_id=%s",
        account_id,
        fields.get("order_id"),
        fields.get("broker_order_id"),
        fields.get("request_id"),
    )


async def handle_modify_sl_tp_stub(
    _redis: redis_asyncio.Redis,
    account_id: str,
    fields: dict[str, str],
) -> None:
    """Log only. Step 3.4 will issue ProtoOAAmendPositionSLTPReq."""
    logger.info(
        "[STUB step 3.4] modify_sl_tp: account=%s order_id=%s "
        "broker_order_id=%s sl=%s tp=%s request_id=%s",
        account_id,
        fields.get("order_id"),
        fields.get("broker_order_id"),
        fields.get("sl"),
        fields.get("tp"),
        fields.get("request_id"),
    )


# Action name → handler dispatch table. ``command_loop`` looks up by
# ``action`` field on each command message; unknown actions are logged
# at WARNING and XACKed (so a malformed message doesn't pile up in
# pending), per ``docs/05-redis-protocol.md``.
ACTION_HANDLERS: dict[str, ActionHandler] = {
    "open": handle_open_stub,
    "close": handle_close_stub,
    "modify_sl_tp": handle_modify_sl_tp_stub,
}
