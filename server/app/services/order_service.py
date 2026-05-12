"""Order creation service (step 3.6).

Coordinates the full validation pipeline + Redis state mutations for a
new hedge order:

  1. Resolve + validate pair (exists, enabled).
  2. Resolve + validate FTMO account (registered, enabled, client online).
  3. Validate symbol against the active whitelist.
  4. Resolve symbol_config (lot_size, min/max/step volume) — fail if
     missing because the FTMO client can't translate lots → cTrader
     wire units without it.
  5. Validate volume bounds (>= min, <= max, multiple of step).
  6. Validate entry_price for limit/stop orders.
  7. Pull the latest tick and validate SL/TP direction against bid/ask
     (market) or entry_price (limit/stop). Per D-045: BUY SL < ref,
     BUY TP > ref; SELL SL > ref, SELL TP < ref.

On success: create the ``order:{order_id}`` HASH (FTMO leg only —
Phase 3), push the ``open`` command to ``cmd_stream:ftmo:{acc}`` via
``RedisService.push_command`` (which generates request_id + zadds the
pending-cmds index), and link ``request_id_to_order:{request_id}`` →
order_id so the response_handler (step 3.7) can route the eventual
cTrader response back to this order row.

Phase 3 scope: Exness leg is stored on the order row
(``exness_account_id``, ``s_status="pending_phase_4"``) but NO
command is dispatched to ``cmd_stream:exness:{acc}``. Phase 4 will
cascade the Exness leg from the FTMO fill event.

Validation errors raise ``OrderValidationError`` carrying an HTTP
status hint + an ``error_code`` string (matches the protocol enum in
``docs/05-redis-protocol.md §6``). The router maps that into a
``fastapi.HTTPException`` with a structured detail body. We never
let a partial-state escape: every check runs BEFORE any Redis write.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Literal

from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop"]


class OrderValidationError(Exception):
    """Validation failure with HTTP + protocol-level error mapping.

    ``http_status`` is the status code the router should return:
      - 400 — caller-provided bad data (volume out of range, invalid
        SL direction, etc.).
      - 404 — referenced resource doesn't exist (pair, account,
        symbol_config).
      - 409 — server-side state blocks the request (client offline,
        no recent tick).

    ``error_code`` mirrors the protocol enum used on resp_stream
    failure entries — keeps the REST + cmd_stream surfaces aligned so
    a frontend toast doesn't have to translate between two
    vocabularies.
    """

    def __init__(
        self,
        message: str,
        http_status: int = 400,
        error_code: str = "validation_error",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code


class OrderService:
    """Stateless service over ``RedisService``.

    Built fresh per request inside the FastAPI router (cheap — no
    network ops in ``__init__``). Holds a reference to the per-app
    ``RedisService`` injected via the FastAPI dependency.
    """

    def __init__(self, redis_service: RedisService) -> None:
        self.redis = redis_service

    async def create_order(
        self,
        *,
        pair_id: str,
        symbol: str,
        side: Side,
        order_type: OrderType,
        volume_lots: float,
        sl: float,
        tp: float,
        entry_price: float,
    ) -> tuple[str, str]:
        """Validate + create order row + push FTMO leg command.

        Returns ``(order_id, request_id)`` on success.

        Validation order is deterministic so the operator gets the
        most actionable error first: structural fields (pair,
        account) before runtime state (client status, tick freshness).
        """
        # 1. Pair must exist + be enabled. ``enabled`` is OPTIONAL on
        #    the pair hash in Phase 2; absence is treated as enabled
        #    so we don't break Phase 2 tests that pre-date the field.
        pair = await self.redis.get_pair(pair_id)
        if pair is None:
            raise OrderValidationError(
                f"pair not found: {pair_id}",
                http_status=404,
                error_code="pair_not_found",
            )
        if pair.get("enabled", "true").lower() == "false":
            raise OrderValidationError(
                f"pair disabled: {pair_id}",
                http_status=400,
                error_code="pair_disabled",
            )

        ftmo_account_id = pair["ftmo_account_id"]
        exness_account_id = pair.get("exness_account_id", "")

        # 2. FTMO account must be registered + enabled.
        account_meta = await self.redis.get_account_meta("ftmo", ftmo_account_id)
        if account_meta is None:
            raise OrderValidationError(
                f"ftmo account not found: {ftmo_account_id}",
                http_status=404,
                error_code="account_not_found",
            )
        if account_meta.get("enabled", "true").lower() == "false":
            raise OrderValidationError(
                f"ftmo account disabled: {ftmo_account_id}",
                http_status=400,
                error_code="account_disabled",
            )

        # 3. FTMO client must be online — heartbeat key present.
        #    Offline → 409 (server-state conflict, not bad input).
        client_status = await self.redis.get_client_status("ftmo", ftmo_account_id)
        if client_status != "online":
            raise OrderValidationError(
                f"ftmo client offline for account {ftmo_account_id}",
                http_status=409,
                error_code="client_offline",
            )

        # 4. Symbol must be in the active whitelist
        #    (the SADD'd set from ``server.sync_symbols``).
        active_symbols = await self.redis.get_active_symbols()
        if symbol not in active_symbols:
            raise OrderValidationError(
                f"symbol not in active whitelist: {symbol}",
                http_status=400,
                error_code="symbol_inactive",
            )

        # 5. symbol_config must exist — without lot_size we can't
        #    translate lots → cTrader wire volume.
        symbol_config = await self.redis.get_symbol_config(symbol)
        if symbol_config is None:
            raise OrderValidationError(
                f"symbol_config missing for {symbol}; run sync_symbols on the server first",
                http_status=404,
                error_code="symbol_not_synced",
            )

        # 6. Volume bounds. cTrader wire volume = lots * lot_size.
        lot_size = int(symbol_config["lot_size"])
        min_volume = int(symbol_config.get("min_volume", "0"))
        max_volume_raw = symbol_config.get("max_volume", "")
        # cTrader's maxVolume is unbounded for many symbols; treat
        # blank as "no upper limit" rather than a magic int.
        max_volume = int(max_volume_raw) if max_volume_raw else None
        step_volume = int(symbol_config.get("step_volume", "1"))

        ctrader_volume = int(volume_lots * lot_size)
        if ctrader_volume < min_volume:
            raise OrderValidationError(
                f"volume too small: {volume_lots} lots = {ctrader_volume} units, min {min_volume}",
                http_status=400,
                error_code="invalid_volume",
            )
        if max_volume is not None and ctrader_volume > max_volume:
            raise OrderValidationError(
                f"volume too large: {volume_lots} lots = {ctrader_volume} units, max {max_volume}",
                http_status=400,
                error_code="invalid_volume",
            )
        if step_volume > 1 and ctrader_volume % step_volume != 0:
            raise OrderValidationError(
                f"volume not a multiple of step {step_volume}: got {ctrader_volume}",
                http_status=400,
                error_code="invalid_volume",
            )

        # 7. entry_price required for limit/stop, ignored for market.
        if order_type in ("limit", "stop") and entry_price <= 0:
            raise OrderValidationError(
                f"entry_price required for {order_type} orders",
                http_status=400,
                error_code="missing_entry_price",
            )

        # 8. Pull the latest tick. SL/TP direction validation needs
        #    bid/ask for market orders. If the bridge hasn't published
        #    a tick recently (broker disconnect, market closed), we
        #    can't validate direction safely — fail closed with 409.
        tick_json = await self.redis.get_tick_cache(symbol)
        if tick_json is None:
            raise OrderValidationError(
                f"no recent tick for {symbol}; cannot validate SL/TP",
                http_status=409,
                error_code="no_tick_data",
            )
        try:
            tick = json.loads(tick_json)
            bid = float(tick["bid"])
            ask = float(tick["ask"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            raise OrderValidationError(
                f"malformed tick cache for {symbol}: {exc}",
                http_status=409,
                error_code="no_tick_data",
            ) from exc

        # 9. SL/TP direction (D-045). The reference price differs by
        #    order type:
        #      - market  → BUY SL < bid, BUY TP > ask;
        #                  SELL SL > ask, SELL TP < bid.
        #      - limit/stop → both sides reference the requested
        #                     entry_price (we don't know what price the
        #                     pending order will eventually fill at, so
        #                     entry_price is the conservative anchor).
        if side == "buy":
            buy_sl_ref = bid if order_type == "market" else entry_price
            buy_tp_ref = ask if order_type == "market" else entry_price
            if sl > 0 and sl >= buy_sl_ref:
                raise OrderValidationError(
                    f"SL {sl} must be < {buy_sl_ref} for BUY",
                    http_status=400,
                    error_code="invalid_sl_direction",
                )
            if tp > 0 and tp <= buy_tp_ref:
                raise OrderValidationError(
                    f"TP {tp} must be > {buy_tp_ref} for BUY",
                    http_status=400,
                    error_code="invalid_tp_direction",
                )
        else:  # sell
            sell_sl_ref = ask if order_type == "market" else entry_price
            sell_tp_ref = bid if order_type == "market" else entry_price
            if sl > 0 and sl <= sell_sl_ref:
                raise OrderValidationError(
                    f"SL {sl} must be > {sell_sl_ref} for SELL",
                    http_status=400,
                    error_code="invalid_sl_direction",
                )
            if tp > 0 and tp >= sell_tp_ref:
                raise OrderValidationError(
                    f"TP {tp} must be < {sell_tp_ref} for SELL",
                    http_status=400,
                    error_code="invalid_tp_direction",
                )

        # All validation passed. Mint identifiers + persist.
        order_id = f"ord_{uuid.uuid4().hex[:8]}"
        now_ms = int(time.time() * 1000)

        # Step 3.11a: normalize SL/TP/entry_price to the symbol's
        # display digits before persistence + cmd_stream dispatch.
        # cTrader rejects orders whose price strings carry more
        # decimals than the symbol allows (e.g. ``"Order price =
        # 1.170440454222853 has more digits than allowed"``). The
        # frontend computes SL from float arithmetic and can emit
        # 15+ digits; trimming here is silent so the operator
        # doesn't see a spurious validation error.
        # NOTE: this happens AFTER direction validation runs against
        # the raw values — those compares are float-precise enough
        # that rounding wouldn't change the outcome, and we want to
        # keep the error messages reporting the user's exact input.
        digits_raw = symbol_config.get("digits", "5") or "5"
        try:
            symbol_digits = int(digits_raw)
        except (TypeError, ValueError):
            symbol_digits = 5
        normalized_sl = round(sl, symbol_digits) if sl > 0 else 0.0
        normalized_tp = round(tp, symbol_digits) if tp > 0 else 0.0
        normalized_entry_price = round(entry_price, symbol_digits) if entry_price > 0 else 0.0

        # Order hash. Field names + leg-prefix convention follow
        # ``RedisService.OrderHash`` (docs/06-data-models.md §7).
        # ``p_volume_lots`` mirrors the per-leg pattern even though
        # Phase 3 only fills the primary (FTMO) side; ``s_status``
        # is ``pending_phase_4`` so the response_handler in step 3.7
        # leaves the secondary leg alone.
        order_fields: dict[str, str] = {
            "order_id": order_id,
            "pair_id": pair_id,
            "ftmo_account_id": ftmo_account_id,
            "exness_account_id": exness_account_id,
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "sl_price": str(normalized_sl),
            "tp_price": str(normalized_tp),
            "entry_price": str(normalized_entry_price),
            "status": "pending",
            "p_status": "pending",
            "p_volume_lots": str(volume_lots),
            "s_status": "pending_phase_4",
            "s_volume_lots": "",
            "created_at": str(now_ms),
            "updated_at": str(now_ms),
        }
        await self.redis.create_order(order_id, order_fields)

        # Push the open command to the FTMO leg. ``push_command``
        # generates request_id + created_at and zadds the pending
        # entry; we capture the returned request_id for the side index.
        cmd_fields: dict[str, str] = {
            "order_id": order_id,
            "action": "open",
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "volume_lots": str(volume_lots),
            "sl": str(normalized_sl),
            "tp": str(normalized_tp),
            "entry_price": str(normalized_entry_price),
        }
        request_id = await self.redis.push_command("ftmo", ftmo_account_id, cmd_fields)

        # Side index: response_handler resolves request_id → order_id
        # when the cTrader response arrives on resp_stream.
        await self.redis.link_request_to_order(request_id, order_id)

        logger.info(
            "create_order: order_id=%s pair_id=%s symbol=%s side=%s "
            "volume_lots=%s order_type=%s request_id=%s",
            order_id,
            pair_id,
            symbol,
            side,
            volume_lots,
            order_type,
            request_id,
        )
        return order_id, request_id

    # ---------- Step 3.9 read endpoints ----------

    # Status sets maintained by ``RedisService.create_order`` +
    # ``update_order`` Lua. ``"all"`` is a synthetic alias the API
    # exposes for convenience; we union the underlying sets.
    _ALL_STATUSES: tuple[str, ...] = (
        "pending",
        "filled",
        "closed",
        "rejected",
        "cancelled",
        "unknown",
    )

    async def list_orders(
        self,
        *,
        status: str = "all",
        symbol: str | None = None,
        account_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, str]], int]:
        """List orders with status / symbol / account_id filters.

        Returns ``(page, total)`` where ``page`` is the slice
        ``[offset:offset+limit]`` and ``total`` is the count BEFORE
        slicing (so the frontend can render pagination correctly).

        Status branches:
          - ``"all"`` → union of every status set.
          - any status NOT in ``_ALL_STATUSES`` → empty list (no
            error; the API surface allows clients to query
            speculative filters).
          - otherwise → just the matching set.

        Sort: ``created_at`` DESC so the newest orders surface first.
        """
        if status == "all":
            collected: dict[str, dict[str, str]] = {}
            for st in self._ALL_STATUSES:
                for order in await self.redis.list_orders_by_status(st):
                    oid = order.get("order_id", "")
                    if oid and oid not in collected:
                        collected[oid] = dict(order)  # type: ignore[arg-type]
            orders: list[dict[str, str]] = list(collected.values())
        elif status in self._ALL_STATUSES:
            orders = [
                dict(o)  # type: ignore[arg-type]
                for o in await self.redis.list_orders_by_status(status)
            ]
        else:
            return [], 0

        sym_upper = symbol.upper() if symbol else None
        filtered: list[dict[str, str]] = []
        for o in orders:
            if sym_upper and o.get("symbol", "") != sym_upper:
                continue
            if account_id and o.get("ftmo_account_id", "") != account_id:
                continue
            filtered.append(o)

        filtered.sort(
            key=lambda o: int(o.get("created_at", "0") or "0"),
            reverse=True,
        )
        total = len(filtered)
        return filtered[offset : offset + limit], total

    async def get_order_by_id(self, order_id: str) -> dict[str, str]:
        """Return one order's HASH. Raises 404 ``order_not_found``."""
        order = await self.redis.get_order(order_id)
        if order is None:
            raise OrderValidationError(
                f"order not found: {order_id}",
                http_status=404,
                error_code="order_not_found",
            )
        return dict(order)  # type: ignore[arg-type]

    async def list_positions(
        self,
        *,
        account_id: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, str]]:
        """Filled orders enriched with ``position_cache:{id}``.

        Each entry merges the live-P&L snapshot from step 3.8 with the
        static order fields (sl_price, tp_price, fill_time) so the
        frontend can render the open-positions table from a single
        payload.

        Race tolerance: ``position_cache`` is written 1 Hz by
        ``position_tracker``. Orders that flipped to ``filled`` within
        the last second may not have a cache entry yet — we include
        them with empty live fields + ``is_stale="true"`` so the row
        still appears (the next list_positions call will pick up the
        real values).
        """
        sym_upper = symbol.upper() if symbol else None
        filled = await self.redis.list_orders_by_status("filled")
        positions: list[dict[str, str]] = []

        for order in filled:
            if sym_upper and order.get("symbol", "") != sym_upper:
                continue
            if account_id and order.get("ftmo_account_id", "") != account_id:
                continue
            oid = order.get("order_id", "")
            if not oid:
                continue

            cache = await self.redis.get_position_cache(oid)
            static_overlay = {
                "sl_price": order.get("sl_price", ""),
                "tp_price": order.get("tp_price", ""),
                "p_executed_at": order.get("p_executed_at", ""),
            }
            if cache is None:
                # Just-filled race: no live snapshot yet. Surface the
                # order's static state with a stale flag so the row
                # renders, just without live P&L.
                position: dict[str, str] = {
                    "order_id": oid,
                    "symbol": order.get("symbol", ""),
                    "side": order.get("side", ""),
                    "volume_lots": order.get("p_volume_lots", ""),
                    "entry_price": order.get("p_fill_price", ""),
                    "current_price": "",
                    "unrealized_pnl": "",
                    "money_digits": order.get("p_money_digits", "2"),
                    "is_stale": "true",
                    "tick_age_ms": "",
                    "computed_at": "",
                    **static_overlay,
                }
            else:
                position = {**cache, **static_overlay}
            positions.append(position)

        positions.sort(
            key=lambda p: int(p.get("p_executed_at", "0") or "0"),
            reverse=True,
        )
        return positions

    async def list_history(
        self,
        *,
        from_ts: int,
        to_ts: int,
        symbol: str | None = None,
        account_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, str]], int]:
        """Closed orders in ``[from_ts, to_ts]`` by ``p_closed_at``.

        Filter is INCLUSIVE on both ends. Orders without a
        ``p_closed_at`` field (shouldn't happen post step-3.7's
        response_handler) are skipped silently.
        """
        sym_upper = symbol.upper() if symbol else None
        closed = await self.redis.list_orders_by_status("closed")
        history: list[dict[str, str]] = []

        for order in closed:
            close_time_str = order.get("p_closed_at", "")
            if not close_time_str:
                continue
            try:
                close_time = int(close_time_str)
            except (TypeError, ValueError):
                continue
            if close_time < from_ts or close_time > to_ts:
                continue
            if sym_upper and order.get("symbol", "") != sym_upper:
                continue
            if account_id and order.get("ftmo_account_id", "") != account_id:
                continue
            history.append(dict(order))  # type: ignore[arg-type]

        history.sort(
            key=lambda o: int(o.get("p_closed_at", "0") or "0"),
            reverse=True,
        )
        total = len(history)
        return history[offset : offset + limit], total

    # ---------- Step 3.9 mutation endpoints ----------

    async def close_order(
        self,
        order_id: str,
        *,
        volume_lots: float | None = None,
    ) -> tuple[str, str]:
        """Dispatch a ``close`` command for one filled order.

        Phase 3 supports full close only (D-057). If ``volume_lots`` is
        provided, it MUST equal the order's open volume; partial close
        raises ``partial_close_unsupported``.

        Returns ``(order_id, request_id)`` on success — same shape as
        ``create_order`` so the frontend can correlate the eventual
        resp_stream entry.
        """
        order = await self.redis.get_order(order_id)
        if order is None:
            raise OrderValidationError(
                f"order not found: {order_id}",
                http_status=404,
                error_code="order_not_found",
            )

        p_status = order.get("p_status", "")
        if p_status != "filled":
            raise OrderValidationError(
                f"cannot close order with p_status={p_status}: must be filled",
                http_status=400,
                error_code="invalid_state",
            )

        current_volume = float(order.get("p_volume_lots", "0") or "0")
        if volume_lots is not None and abs(volume_lots - current_volume) > 1e-9:
            raise OrderValidationError(
                f"partial close not supported in Phase 3; volume must be "
                f"{current_volume}, got {volume_lots}",
                http_status=400,
                error_code="partial_close_unsupported",
            )

        ftmo_account_id = order.get("ftmo_account_id", "")
        if not ftmo_account_id:
            raise OrderValidationError(
                "order missing ftmo_account_id",
                http_status=500,
                error_code="order_corrupt",
            )

        broker_order_id = order.get("p_broker_order_id", "")
        if not broker_order_id:
            raise OrderValidationError(
                "order has no p_broker_order_id; cannot dispatch close",
                http_status=500,
                error_code="order_corrupt",
            )

        client_status = await self.redis.get_client_status("ftmo", ftmo_account_id)
        if client_status != "online":
            raise OrderValidationError(
                f"ftmo client offline for account {ftmo_account_id}",
                http_status=409,
                error_code="client_offline",
            )

        cmd_fields: dict[str, str] = {
            "order_id": order_id,
            "action": "close",
            "broker_order_id": broker_order_id,
            "symbol": order.get("symbol", ""),
            "volume_lots": str(current_volume),
        }
        request_id = await self.redis.push_command("ftmo", ftmo_account_id, cmd_fields)
        await self.redis.link_request_to_order(request_id, order_id)

        logger.info(
            "close_order: order_id=%s broker_order_id=%s request_id=%s",
            order_id,
            broker_order_id,
            request_id,
        )
        return order_id, request_id

    async def modify_order(
        self,
        order_id: str,
        *,
        sl: float | None,
        tp: float | None,
    ) -> tuple[str, str]:
        """Dispatch a ``modify_sl_tp`` command for one filled order.

        Field semantics (matching the REST schema):
          - ``None``  → keep the order's existing value.
          - ``0``     → remove that side (BUY SL set to 0 means
            "no stop loss"; same for TP).
          - positive → set to that price; direction validated against
            the latest tick.

        Direction validation runs ONLY when a side is being set to a
        positive price. Removing (``0``) or keeping (``None``) skips
        the tick check.
        """
        if sl is None and tp is None:
            raise OrderValidationError(
                "at least one of sl or tp must be provided",
                http_status=400,
                error_code="missing_field",
            )

        order = await self.redis.get_order(order_id)
        if order is None:
            raise OrderValidationError(
                f"order not found: {order_id}",
                http_status=404,
                error_code="order_not_found",
            )

        p_status = order.get("p_status", "")
        if p_status != "filled":
            raise OrderValidationError(
                f"cannot modify order with p_status={p_status}: must be filled",
                http_status=400,
                error_code="invalid_state",
            )

        symbol = order.get("symbol", "")
        side = order.get("side", "")

        needs_tick_check = (sl is not None and sl > 0) or (tp is not None and tp > 0)
        if needs_tick_check:
            tick_raw = await self.redis.get_tick_cache(symbol)
            if tick_raw is None:
                raise OrderValidationError(
                    f"no recent tick for {symbol}; cannot validate SL/TP",
                    http_status=409,
                    error_code="no_tick_data",
                )
            try:
                tick = json.loads(tick_raw)
                bid = float(tick["bid"])
                ask = float(tick["ask"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                raise OrderValidationError(
                    f"malformed tick cache for {symbol}: {exc}",
                    http_status=409,
                    error_code="no_tick_data",
                ) from exc
            if side == "buy":
                if sl is not None and sl > 0 and sl >= bid:
                    raise OrderValidationError(
                        f"SL {sl} must be < {bid} for BUY",
                        http_status=400,
                        error_code="invalid_sl_direction",
                    )
                if tp is not None and tp > 0 and tp <= ask:
                    raise OrderValidationError(
                        f"TP {tp} must be > {ask} for BUY",
                        http_status=400,
                        error_code="invalid_tp_direction",
                    )
            elif side == "sell":
                if sl is not None and sl > 0 and sl <= ask:
                    raise OrderValidationError(
                        f"SL {sl} must be > {ask} for SELL",
                        http_status=400,
                        error_code="invalid_sl_direction",
                    )
                if tp is not None and tp > 0 and tp >= bid:
                    raise OrderValidationError(
                        f"TP {tp} must be < {bid} for SELL",
                        http_status=400,
                        error_code="invalid_tp_direction",
                    )

        ftmo_account_id = order.get("ftmo_account_id", "")
        if not ftmo_account_id:
            raise OrderValidationError(
                "order missing ftmo_account_id",
                http_status=500,
                error_code="order_corrupt",
            )
        broker_order_id = order.get("p_broker_order_id", "")
        if not broker_order_id:
            raise OrderValidationError(
                "order has no p_broker_order_id",
                http_status=500,
                error_code="order_corrupt",
            )

        client_status = await self.redis.get_client_status("ftmo", ftmo_account_id)
        if client_status != "online":
            raise OrderValidationError(
                f"ftmo client offline for account {ftmo_account_id}",
                http_status=409,
                error_code="client_offline",
            )

        new_sl = sl if sl is not None else float(order.get("sl_price", "0") or "0")
        new_tp = tp if tp is not None else float(order.get("tp_price", "0") or "0")

        cmd_fields: dict[str, str] = {
            "order_id": order_id,
            "action": "modify_sl_tp",
            "broker_order_id": broker_order_id,
            "sl": str(new_sl),
            "tp": str(new_tp),
        }
        request_id = await self.redis.push_command("ftmo", ftmo_account_id, cmd_fields)
        await self.redis.link_request_to_order(request_id, order_id)

        logger.info(
            "modify_order: order_id=%s sl=%s tp=%s request_id=%s",
            order_id,
            new_sl,
            new_tp,
            request_id,
        )
        return order_id, request_id
