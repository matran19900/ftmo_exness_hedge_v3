"""Position tracker — unrealized P&L compute loop (step 3.8).

One background task per FTMO account, started in the lifespan
alongside ``response_handler`` and ``event_handler`` (step 3.7).

Cadence: every ``_POLL_INTERVAL_SECONDS`` (1 s). For each filled
position on this account:

  1. Read the latest ``tick:{symbol}`` cache.
  2. Compute unrealized P&L as raw money-digits-scaled int.
  3. HSET ``position_cache:{order_id}`` with the snapshot (TTL 600s).
  4. Append to a per-account batch.

At the end of the cycle, publish ONE WS message over the
``positions`` channel carrying the full batch. The frontend
re-renders all live P&L cells from a single envelope rather than
N per-order broadcasts.

Phase 3 scope: FTMO leg only. The order's ``p_*`` fields (entry
price, side, volume_lots) drive the math; the Exness leg is
ignored. Phase 4 will compose ``total_pnl`` from both legs.

P&L formula derivation
----------------------
  side_mult     = +1 (BUY) or -1 (SELL)
  current_price = tick.bid   (BUY closes at bid)
                  tick.ask   (SELL closes at ask)
  price_diff    = (current_price - entry_price) * side_mult
  contract_size = lot_size / 100     # cTrader volume is 0.01 base units
  volume_base   = volume_lots * contract_size
  pnl_quote     = price_diff * volume_base
  pnl_usd       = convert(pnl_quote, quote_currency → USD)
  pnl_raw       = round(pnl_usd * 10^money_digits)

The contract_size derivation comes from cTrader's wire convention:
``ProtoOANewOrderReq.volume`` is in 0.01 base-currency units, so
``lot_size`` (which the bridge multiplies by ``volume_lots`` to
build the wire value) is ``contract_size × 100``. For standard FX:
``lotSize=10_000_000`` → ``contract_size=100_000``.

USD conversion
--------------
Step 3.8 supports three paths, chosen by ``quote_currency`` derived
from the symbol name (last 3 chars):

  - ``quote=USD``  → no conversion.
  - ``quote=JPY``  → divide by USDJPY bid.
  - other (e.g. ``GBP``) → divide by ``USDxxx`` bid if available,
    else multiply by ``xxxUSD`` bid (inverse). If neither cross is
    cached, log + flag ``is_stale=true`` + emit raw quote-currency
    P&L unconverted (consumer can show "?USD" until ticks arrive).

Stale-tick handling
-------------------
A tick whose ``ts`` field is older than ``_STALE_TICK_THRESHOLD_MS``
(5 s) is flagged ``is_stale=true`` on the position cache + WS
broadcast. The compute still runs against the stale price — the
frontend can decide whether to grey-out the row or render a banner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)

# Public for testability — every constant has a behavioural impact and
# tests may want to assert against the exact value.
POSITIONS_CHANNEL = "positions"

_POLL_INTERVAL_SECONDS = 1.0
_STALE_TICK_THRESHOLD_MS = 5_000
_POSITION_CACHE_TTL_SECONDS = 600


async def position_tracker_loop(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
    *,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> None:
    """Run the unrealized-P&L compute loop for one FTMO account.

    The loop runs until cancelled via ``asyncio.Task.cancel()`` from
    the lifespan shutdown handler. Single-cycle errors are logged and
    the loop continues — a transient Redis failure or a single
    malformed order shouldn't take down P&L tracking for the others.

    ``poll_interval_seconds`` is configurable for tests (small values
    let the loop tick fast); production uses 1.0 s.
    """
    logger.info(
        "position_tracker_loop starting: account_id=%s interval=%.2fs",
        account_id,
        poll_interval_seconds,
    )
    try:
        while True:
            cycle_start = time.monotonic()
            try:
                await _run_one_cycle(redis_svc, broadcast, account_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "position_tracker_loop cycle failed for account=%s; continuing",
                    account_id,
                )

            elapsed = time.monotonic() - cycle_start
            remaining = max(0.0, poll_interval_seconds - elapsed)
            if remaining > 0:
                await asyncio.sleep(remaining)
    except asyncio.CancelledError:
        logger.info("position_tracker_loop cancelled: account_id=%s", account_id)
        raise


async def _run_one_cycle(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    account_id: str,
) -> None:
    """One poll cycle: compute P&L for every filled position on this
    account, write to position_cache, broadcast batch.

    Iterates orders fetched via the step-3.7
    ``list_open_orders_by_account`` (no SCAN/KEYS). Pending /
    rejected / cancelled / unknown orders are skipped — they have no
    open broker-side position to mark to market.
    """
    open_orders = await redis_svc.list_open_orders_by_account("ftmo", account_id)
    batch: list[dict[str, Any]] = []
    now_ms = int(time.time() * 1000)

    for order in open_orders:
        if order.get("p_status") != "filled":
            continue
        order_id = order.get("order_id", "")
        symbol = order.get("symbol", "")
        if not order_id or not symbol:
            continue

        symbol_config = await redis_svc.get_symbol_config(symbol)
        if symbol_config is None:
            logger.warning(
                "position_tracker: symbol_config missing for %s order=%s",
                symbol,
                order_id,
            )
            continue

        tick = await _read_tick(redis_svc, symbol)
        if tick is None:
            # Market closed, or first sync hasn't happened yet. Skip
            # silently rather than spam logs; the frontend will keep
            # the last broadcast value or render "no data".
            continue

        try:
            tick_ts = int(tick.get("ts", 0))
        except (TypeError, ValueError):
            tick_ts = 0
        tick_age_ms = max(0, now_ms - tick_ts) if tick_ts else 0
        is_stale = tick_age_ms > _STALE_TICK_THRESHOLD_MS

        try:
            pnl_raw, conv_stale, current_price = await _compute_pnl(
                redis_svc, dict(order), symbol_config, tick
            )
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            logger.warning(
                "position_tracker: P&L compute failed for order=%s: %s",
                order_id,
                exc,
            )
            continue

        is_stale = is_stale or conv_stale

        position_fields = {
            "order_id": order_id,
            "symbol": symbol,
            "side": order.get("side", ""),
            "volume_lots": order.get("p_volume_lots", ""),
            "entry_price": order.get("p_fill_price", ""),
            "current_price": str(current_price),
            "unrealized_pnl": str(pnl_raw),
            "money_digits": order.get("p_money_digits", "2") or "2",
            "is_stale": "true" if is_stale else "false",
            "tick_age_ms": str(tick_age_ms),
            "computed_at": str(now_ms),
        }
        await redis_svc.set_position_cache(
            order_id, position_fields, ttl_seconds=_POSITION_CACHE_TTL_SECONDS
        )

        batch.append(
            {
                "order_id": order_id,
                "symbol": symbol,
                "current_price": current_price,
                "unrealized_pnl": str(pnl_raw),
                "is_stale": is_stale,
                "tick_age_ms": tick_age_ms,
            }
        )

    if batch:
        await broadcast.publish(
            POSITIONS_CHANNEL,
            {
                "type": "positions_tick",
                "account_id": account_id,
                "ts": now_ms,
                "positions": batch,
            },
        )


async def _read_tick(redis_svc: RedisService, symbol: str) -> dict[str, Any] | None:
    """Read + parse the JSON-encoded tick cache.

    Phase 2's ``set_tick_cache`` stores ``{type, symbol, bid, ask,
    ts}`` as a JSON string. Returns ``None`` on miss OR on parse
    failure (defensive — a corrupted entry shouldn't crash the
    loop). Logs at debug since this is in the hot path.
    """
    raw = await redis_svc.get_tick_cache(symbol)
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("position_tracker: malformed tick cache for %s", symbol)
        return None
    return decoded if isinstance(decoded, dict) else None


def _derive_quote_currency(symbol: str) -> str:
    """Last 3 chars of a 6-char FX-style symbol = quote currency.

    Used because Phase 2's ``sync_symbols`` doesn't persist
    ``quote_currency`` to ``symbol_config`` (cTrader doesn't expose
    it directly via ``ProtoOASymbol``). For non-FX symbols (indices,
    metals) the heuristic falls back to ``"USD"`` since those
    typically settle in the account currency.
    """
    if len(symbol) == 6 and symbol.isalpha():
        return symbol[3:].upper()
    return "USD"


async def _compute_pnl(
    redis_svc: RedisService,
    order: dict[str, Any],
    symbol_config: dict[str, Any],
    tick: dict[str, Any],
) -> tuple[int, bool, float]:
    """Compute unrealized P&L for one position.

    Returns ``(pnl_raw_int, conversion_stale_flag, current_price)``.

    Raises ``KeyError`` / ``ValueError`` / ``ZeroDivisionError`` on
    bad input — caller catches and logs.
    """
    side = order["side"]
    side_mult = 1 if side == "buy" else -1

    entry_price = float(order["p_fill_price"])
    volume_lots = float(order["p_volume_lots"])
    lot_size = int(symbol_config["lot_size"])
    # cTrader convention: volume wire = volume_lots * lot_size, and
    # the wire value is in 0.01 base-currency units → contract_size
    # in base = lot_size / 100. For standard FX (lotSize=10_000_000)
    # this yields 100_000 per lot, matching the broker's "100k = 1
    # standard lot" convention.
    contract_size = lot_size / 100.0

    # Close-side price: BUY exits at bid, SELL exits at ask.
    if side == "buy":
        current_price = float(tick["bid"])
    else:
        current_price = float(tick["ask"])

    price_diff = (current_price - entry_price) * side_mult
    volume_base = volume_lots * contract_size
    pnl_quote = price_diff * volume_base

    quote_currency = _derive_quote_currency(order.get("symbol", ""))
    pnl_usd, conv_stale = await _convert_to_usd(redis_svc, pnl_quote, quote_currency)

    money_digits_str = order.get("p_money_digits", "2") or "2"
    try:
        money_digits = int(money_digits_str)
    except ValueError:
        money_digits = 2
    pnl_raw = int(round(pnl_usd * (10**money_digits)))
    return pnl_raw, conv_stale, current_price


async def _convert_to_usd(
    redis_svc: RedisService,
    pnl_quote: float,
    quote_currency: str,
) -> tuple[float, bool]:
    """Convert a quote-currency P&L value to USD.

    Returns ``(pnl_usd, is_conversion_stale)``. ``is_conversion_stale``
    is True when the conversion cross-rate wasn't found and the raw
    quote-currency value is returned unconverted — the frontend can
    grey-out the cell and warn the operator.

    Routing:
      - ``quote == "USD"`` → no conversion (pnl_quote == pnl_usd).
      - ``quote == "JPY"`` → divide by USDJPY bid.
      - other → try USD{quote} first (divide by its bid); fall back
        to {quote}USD (multiply by its bid). If neither cached,
        return pnl_quote with stale=True.
    """
    if quote_currency == "USD":
        return pnl_quote, False

    if quote_currency == "JPY":
        usd_jpy = await _read_tick(redis_svc, "USDJPY")
        if usd_jpy is None:
            logger.warning(
                "position_tracker: no USDJPY tick for JPY-quote conversion; "
                "emitting raw quote value with is_stale=true"
            )
            return pnl_quote, True
        rate = float(usd_jpy["bid"])
        if rate == 0:
            return pnl_quote, True
        return pnl_quote / rate, False

    cross_symbol = f"USD{quote_currency}"
    cross_tick = await _read_tick(redis_svc, cross_symbol)
    if cross_tick is not None:
        rate = float(cross_tick["bid"])
        if rate == 0:
            return pnl_quote, True
        return pnl_quote / rate, False

    inv_symbol = f"{quote_currency}USD"
    inv_tick = await _read_tick(redis_svc, inv_symbol)
    if inv_tick is not None:
        rate = float(inv_tick["bid"])
        return pnl_quote * rate, False

    logger.warning(
        "position_tracker: no conversion tick for quote=%s "
        "(tried %s, %s); emitting raw with is_stale=true",
        quote_currency,
        cross_symbol,
        inv_symbol,
    )
    return pnl_quote, True
