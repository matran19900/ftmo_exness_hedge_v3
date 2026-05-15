"""Action handlers for the Exness command stream (Phase 4.2).

The command processor's ``_dispatch_one`` delegates here. We handle three
``action`` values:

  - ``open``           — market order via ``mt5.order_send`` with IOC
                         filling; on ``UNSUPPORTED_FILLING`` retcode we
                         retry once with FOK before giving up.
  - ``close``          — close existing position by ticket. Looks up the
                         live ``Position`` to determine direction +
                         volume so the operator/server doesn't have to.
  - ``resync_symbols`` — re-publish the raw-symbols snapshot via
                         ``SymbolSyncPublisher.publish_snapshot``.

Response payload schema (per ``docs/05-redis-protocol.md`` §3.2 + the
FTMO client's pattern in ``apps/ftmo-client/ftmo_client/action_handlers.py``):

  request_id  — echo of the cmd ``request_id`` so the server's response
                 handler can correlate with the pending zset.
  action      — echo of the cmd ``action`` (``open`` / ``close`` /
                 ``resync_symbols``).
  status      — high-level outcome: ``filled`` | ``closed`` |
                 ``rejected`` | ``error`` | ``requote`` | ``completed``.
  reason      — short snake_case slug from ``retcode_mapping`` (or a
                 handler-specific slug like ``symbol_not_found``).
  retcode     — raw MT5 retcode int (string), present on every
                 broker-touching response so the server can re-map if
                 the vocab evolves.
  ts_ms       — milliseconds-epoch timestamp of when we shipped the
                 response.
  cascade_trigger — propagated verbatim from the cmd (Phase 4 design
                 §1.F) so the FTMO-side cascade knows whether this leg
                 was the trigger.

Plus action-specific extras: ``broker_order_id``, ``broker_position_id``,
``fill_price``, ``filled_volume``, ``close_price``, ``closed_volume``,
``symbol_count`` (resync), ``comment``.

Every MT5 call is wrapped via ``asyncio.to_thread`` because the
``MetaTrader5`` package is fully synchronous (D-4.1.A).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .cmd_ledger import CmdLedger
from .retcode_mapping import map_retcode
from .symbol_sync import SymbolSyncPublisher

logger = logging.getLogger(__name__)

# Phase 4.2 IOC→FOK retry deviation isn't a permanent retry budget — we
# only retry the filling-mode branch once per alternate. Cap explicitly
# so a future refactor can't accidentally turn this into an unbounded
# loop on a misbehaving broker. Step 4.8a query the symbol's filling
# bitmask first, so the list of modes the loop walks is at most 2 entries
# (preferred + alternate) — the cap stays at 2.
_MAX_FILLING_RETRIES = 2

# Step 4.8a — symbol-side bitmask values for ``info.filling_mode``.
# Duplicated from ``mt5_stub`` so production code can compare without
# pulling in the stub-or-real selection logic.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2


class ActionHandler:
    """Routes ``cmd_stream`` actions to the right MT5 call + publishes
    the resulting response back to ``resp_stream:exness:{account_id}``."""

    def __init__(
        self,
        redis_client: Any,
        account_id: str,
        mt5_module: Any,
        symbol_sync: SymbolSyncPublisher,
        cmd_ledger: CmdLedger,
    ) -> None:
        self._redis = redis_client
        self._account_id = account_id
        self._mt5 = mt5_module
        self._symbol_sync = symbol_sync
        self._cmd_ledger = cmd_ledger
        self._resp_key = f"resp_stream:exness:{account_id}"

    # ----- Dispatch -----

    async def dispatch(self, fields: dict[str, str]) -> None:
        """Route ``fields`` (already string-keyed by the command processor)
        to the action-specific handler. Unknown actions ship a ``rejected``
        response with reason ``unknown_action_{action}``."""
        action = fields.get("action", "")
        if action == "open":
            await self._handle_open(fields)
        elif action == "close":
            await self._handle_close(fields)
        elif action == "resync_symbols":
            await self._handle_resync(fields)
        else:
            await self._publish_response(
                request_id=fields.get("request_id", ""),
                action=action or "<missing>",
                status="rejected",
                reason=f"unknown_action_{action}",
                cascade_trigger=fields.get("cascade_trigger", "false"),
            )

    # ----- open -----

    async def _handle_open(self, fields: dict[str, str]) -> None:
        request_id = fields.get("request_id", "")
        cascade_trigger = fields.get("cascade_trigger", "false")
        try:
            symbol = fields["symbol"]
            side = fields["side"]
            volume = float(fields["volume"])
        except (KeyError, ValueError) as exc:
            await self._publish_response(
                request_id=request_id,
                action="open",
                status="rejected",
                reason=f"bad_request_{type(exc).__name__}",
                cascade_trigger=cascade_trigger,
            )
            return

        magic = int(fields.get("magic", "0"))
        order_type = (
            self._mt5.ORDER_TYPE_BUY
            if side == "buy"
            else self._mt5.ORDER_TYPE_SELL
        )

        info = await _to_thread(self._mt5.symbol_info, symbol)
        if info is None:
            await self._publish_response(
                request_id=request_id,
                action="open",
                status="rejected",
                reason="symbol_not_found",
                cascade_trigger=cascade_trigger,
                symbol=symbol,
            )
            return
        price = info.ask if side == "buy" else info.bid

        # Step 4.8a — belt-and-suspenders ``symbol_select`` per order.
        # The initial sync (``main.py:113``) calls this once, but a
        # silent skip during per-symbol iteration (or a terminal-side
        # deselect after sync) can leave the symbol missing from Market
        # Watch — in which case ``order_send`` returns ``None`` with no
        # retcode. Re-asserting selection here is idempotent on the real
        # MT5 lib for already-selected symbols (D-SMOKE-2).
        selected = await _to_thread(self._mt5.symbol_select, symbol, True)
        if not selected:
            await self._publish_response(
                request_id=request_id,
                action="open",
                status="error",
                reason="symbol_select_failed",
                cascade_trigger=cascade_trigger,
                symbol=symbol,
            )
            return

        # Step 4.8a — pick the filling mode the broker actually supports.
        # Exness Cent reports ``info.filling_mode=1`` (FOK only) on many
        # symbols including EURUSDm; an IOC submission is silently
        # rejected with ``None`` (not retcode 10030
        # UNSUPPORTED_FILLING) by the terminal-side validator. The
        # historical hardcoded ``[IOC, FOK]`` list never advanced past
        # the silent-None first attempt — see ``verify-exness-order-send-
        # none-diagnosis.md`` for the field evidence + D-SMOKE-2.
        supported = info.filling_mode
        if (
            supported & SYMBOL_FILLING_FOK
            and supported & SYMBOL_FILLING_IOC
        ):
            # Both supported — preserve historical IOC-first preference.
            filling_modes = [
                self._mt5.ORDER_FILLING_IOC,
                self._mt5.ORDER_FILLING_FOK,
            ]
        elif supported & SYMBOL_FILLING_FOK:
            # FOK-only broker (Exness Cent path). Try FOK first, IOC
            # as a defensive fallback in case the bitmask is stale.
            filling_modes = [
                self._mt5.ORDER_FILLING_FOK,
                self._mt5.ORDER_FILLING_IOC,
            ]
        elif supported & SYMBOL_FILLING_IOC:
            filling_modes = [
                self._mt5.ORDER_FILLING_IOC,
                self._mt5.ORDER_FILLING_FOK,
            ]
        else:
            # Bitmask reports neither FOK nor IOC (rare; e.g. BOC-only
            # passive symbol). Fall back to RETURN for stack accounts;
            # the broker will reject with a retcode we then publish.
            filling_modes = [self._mt5.ORDER_FILLING_RETURN]
        last_result = None
        for attempt, filling_mode in enumerate(filling_modes):
            request = {
                "action": self._mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "deviation": 20,
                "magic": magic,
                "type_filling": filling_mode,
                "comment": f"hedge:{request_id}"[:31],
            }
            try:
                result = await _to_thread(self._mt5.order_send, request)
            except Exception as exc:
                logger.exception("open.order_send_exception")
                await self._publish_response(
                    request_id=request_id,
                    action="open",
                    status="error",
                    reason=f"order_send_exception_{type(exc).__name__}",
                    cascade_trigger=cascade_trigger,
                )
                return

            if result is None:
                # Step 4.8a — Exness Cent (and similar quirky brokers)
                # silently reject unsupported filling with ``None``
                # instead of retcode ``TRADE_RETCODE_UNSUPPORTED_FILLING``
                # (10030). Treat ``None`` as a filling miss and advance
                # to the next mode in ``filling_modes`` before publishing
                # the terminal error.
                if attempt + 1 < len(filling_modes):
                    logger.info(
                        "open.silent_none_retry_alternate_filling "
                        "request_id=%s next_mode=%s",
                        request_id, filling_modes[attempt + 1],
                    )
                    continue
                await self._publish_response(
                    request_id=request_id,
                    action="open",
                    status="error",
                    reason="order_send_returned_none",
                    cascade_trigger=cascade_trigger,
                )
                return
            last_result = result
            outcome = map_retcode(result.retcode)
            if (
                outcome.retry_strategy == "retry_alternate_filling"
                and attempt + 1 < _MAX_FILLING_RETRIES
            ):
                logger.info(
                    "open.retry_filling_fok request_id=%s", request_id
                )
                continue
            await self._publish_response(
                request_id=request_id,
                action="open",
                status=outcome.status,
                reason=outcome.reason,
                cascade_trigger=cascade_trigger,
                broker_order_id=str(result.order)
                if outcome.status == "filled"
                else "",
                broker_position_id=str(result.order)
                if outcome.status == "filled"
                else "",
                fill_price=str(result.price)
                if outcome.status == "filled"
                else "",
                filled_volume=str(result.volume)
                if outcome.status == "filled"
                else "",
                retcode=str(result.retcode),
                comment=result.comment,
            )
            return

        # Reached only if the loop exhausted retries without publishing.
        # Defensive — shouldn't happen given the IOC/FOK contract.
        if last_result is not None:
            outcome = map_retcode(last_result.retcode)
            await self._publish_response(
                request_id=request_id,
                action="open",
                status=outcome.status,
                reason=outcome.reason,
                cascade_trigger=cascade_trigger,
                retcode=str(last_result.retcode),
                comment=last_result.comment,
            )

    # ----- close -----

    async def _handle_close(self, fields: dict[str, str]) -> None:
        request_id = fields.get("request_id", "")
        cascade_trigger = fields.get("cascade_trigger", "false")
        try:
            position_ticket = int(fields["broker_position_id"])
        except (KeyError, ValueError) as exc:
            await self._publish_response(
                request_id=request_id,
                action="close",
                status="rejected",
                reason=f"bad_request_{type(exc).__name__}",
                cascade_trigger=cascade_trigger,
            )
            return

        # Step 4.3a: mark the ticket in the ledger BEFORE issuing the
        # close so the position monitor's enrichment correctly stamps
        # ``close_reason="server_initiated"`` even if MT5 fills + the
        # monitor polls before our resp_stream publish lands. Failure
        # is logged inside the ledger and never raised — worst case
        # the close gets misclassified as ``external`` (server emits a
        # benign WARNING alert in step 4.7) but the close itself goes
        # through normally.
        await self._cmd_ledger.mark_server_initiated(position_ticket)

        positions = await _to_thread(
            self._mt5.positions_get, ticket=position_ticket
        )
        if not positions:
            # Real MT5 race: the position vanished between dispatch and
            # close (manual close on the terminal, or another instance of
            # the client hit it first). Surface as ``error`` with the
            # well-known ``position_not_found`` reason — the server's
            # cascade-aware reconciler (step 4.7) reads this slug.
            await self._publish_response(
                request_id=request_id,
                action="close",
                status="error",
                reason="position_not_found",
                cascade_trigger=cascade_trigger,
                broker_position_id=str(position_ticket),
            )
            return

        position = positions[0]
        close_type = (
            self._mt5.ORDER_TYPE_SELL
            if position.type == self._mt5.POSITION_TYPE_BUY
            else self._mt5.ORDER_TYPE_BUY
        )
        info = await _to_thread(self._mt5.symbol_info, position.symbol)
        if info is None:
            await self._publish_response(
                request_id=request_id,
                action="close",
                status="error",
                reason="symbol_not_found",
                cascade_trigger=cascade_trigger,
                broker_position_id=str(position_ticket),
                symbol=position.symbol,
            )
            return
        close_price = (
            info.bid if close_type == self._mt5.ORDER_TYPE_SELL else info.ask
        )

        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": close_type,
            "position": position_ticket,
            "price": close_price,
            "deviation": 20,
            "magic": position.magic,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
            "comment": f"close:{request_id}"[:31],
        }
        try:
            result = await _to_thread(self._mt5.order_send, request)
        except Exception as exc:
            logger.exception("close.order_send_exception")
            await self._publish_response(
                request_id=request_id,
                action="close",
                status="error",
                reason=f"order_send_exception_{type(exc).__name__}",
                cascade_trigger=cascade_trigger,
                broker_position_id=str(position_ticket),
            )
            return
        if result is None:
            await self._publish_response(
                request_id=request_id,
                action="close",
                status="error",
                reason="order_send_returned_none",
                cascade_trigger=cascade_trigger,
                broker_position_id=str(position_ticket),
            )
            return

        outcome = map_retcode(result.retcode)
        # ``filled`` on a close action is reported as ``closed`` — the
        # server's cascade orchestrator (step 4.7) reads ``closed`` to
        # know when to fire the FTMO-side cascade close.
        publish_status = "closed" if outcome.status == "filled" else outcome.status
        await self._publish_response(
            request_id=request_id,
            action="close",
            status=publish_status,
            reason=outcome.reason,
            cascade_trigger=cascade_trigger,
            broker_position_id=str(position_ticket),
            close_price=str(result.price)
            if outcome.status == "filled"
            else "",
            closed_volume=str(result.volume)
            if outcome.status == "filled"
            else "",
            retcode=str(result.retcode),
            comment=result.comment,
        )

    # ----- resync_symbols -----

    async def _handle_resync(self, fields: dict[str, str]) -> None:
        request_id = fields.get("request_id", "")
        cascade_trigger = fields.get("cascade_trigger", "false")
        try:
            count = await self._symbol_sync.publish_snapshot()
        except Exception as exc:
            logger.exception("resync.failed")
            await self._publish_response(
                request_id=request_id,
                action="resync_symbols",
                status="error",
                reason=f"resync_exception_{type(exc).__name__}",
                cascade_trigger=cascade_trigger,
            )
            return
        await self._publish_response(
            request_id=request_id,
            action="resync_symbols",
            status="completed",
            reason="resync_ok",
            cascade_trigger=cascade_trigger,
            symbol_count=str(count),
        )

    # ----- shared response publish -----

    async def _publish_response(
        self,
        *,
        request_id: str,
        action: str,
        status: str,
        reason: str,
        cascade_trigger: str,
        **extras: str,
    ) -> None:
        """XADD a flat string payload to ``resp_stream:exness:{account_id}``.

        Mirrors the FTMO client's ``_publish_response`` shape so the
        server-side response handler (step 4.7) can consume both brokers
        through the same code path.
        """
        payload: dict[str, str] = {
            "request_id": request_id,
            "action": action,
            "status": status,
            "reason": reason,
            "ts_ms": str(int(time.time() * 1000)),
            "cascade_trigger": cascade_trigger,
        }
        for k, v in extras.items():
            payload[k] = v
        try:
            await self._redis.xadd(self._resp_key, payload)
            logger.info(
                "response.published request_id=%s action=%s status=%s",
                request_id,
                action,
                status,
            )
        except Exception:
            logger.exception(
                "response.xadd_failed request_id=%s action=%s",
                request_id,
                action,
            )


# ----- internal helper -----


async def _to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Tiny indirection over ``asyncio.to_thread`` so the test surface
    can monkeypatch the wrapper if it ever needs to without touching the
    stdlib import. Ergonomic + future-proof; not load-bearing today."""
    return await asyncio.to_thread(func, *args, **kwargs)
