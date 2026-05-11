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
            "sl_price": str(sl),
            "tp_price": str(tp),
            "entry_price": str(entry_price),
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
            "sl": str(sl),
            "tp": str(tp),
            "entry_price": str(entry_price),
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
