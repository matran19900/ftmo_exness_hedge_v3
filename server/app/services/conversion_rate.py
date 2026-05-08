"""USD conversion rate computation from cached ticks.

Given a quote currency, return how much 1 unit of that currency is worth in
USD. Reads bid prices from the ``tick:{symbol}`` Redis cache populated by the
WebSocket spot-event handler (step 2.3).

Strategy:
    1. ``USD`` → ``1.0``
    2. Try forward pair ``<quote>USD`` (e.g. ``EURUSD``): rate = ``bid``
    3. Else try inverse pair ``USD<quote>`` (e.g. ``USDJPY``): rate = ``1.0 / bid``
    4. Else subscribe whichever candidate is in the active set and return ``0.0``
       to signal "not yet available — try again in a few seconds".

Step 2.4 deviation from D-005: no separate ``rate:{ccy}`` 24h cache. The
60-second tick cache is fresh enough and avoids a second cache layer.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.market_data import MarketDataService
    from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)


async def get_quote_to_usd_rate(
    quote_ccy: str,
    redis_svc: RedisService,
    market_data: MarketDataService | None,
) -> float:
    """Return ``quote_ccy → USD`` rate or ``0.0`` if not yet available.

    The ``0.0`` sentinel means: the relevant tick is not cached. The function
    has subscribed the missing pair(s) on cTrader so the caller can retry.
    """
    quote_ccy = quote_ccy.upper()
    if quote_ccy == "USD":
        return 1.0

    forward_pair = f"{quote_ccy}USD"
    forward_bid = await _read_bid(redis_svc, forward_pair)
    if forward_bid is not None and forward_bid > 0:
        return forward_bid

    inverse_pair = f"USD{quote_ccy}"
    inverse_bid = await _read_bid(redis_svc, inverse_pair)
    if inverse_bid is not None and inverse_bid > 0:
        return 1.0 / inverse_bid

    candidates: list[str] = []
    for pair in (forward_pair, inverse_pair):
        config = await redis_svc.get_symbol_config(pair)
        if config and "ctrader_symbol_id" in config:
            candidates.append(pair)

    if candidates and market_data is not None:
        try:
            await market_data.subscribe_spots(candidates, redis_svc)
            logger.info(
                "Subscribed to conversion pair(s) %s for quote_ccy=%s",
                candidates,
                quote_ccy,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to subscribe conversion pairs for %s", quote_ccy)

    return 0.0


async def _read_bid(redis_svc: RedisService, pair: str) -> float | None:
    """Return the bid from the cached tick for ``pair``, or ``None`` if missing/invalid."""
    raw = await redis_svc.get_tick_cache(pair)
    if raw is None:
        return None
    try:
        tick = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid tick JSON for %s", pair)
        return None
    bid = tick.get("bid")
    if bid is None:
        return None
    try:
        return float(bid)
    except (TypeError, ValueError):
        logger.warning("Non-numeric bid for %s: %r", pair, bid)
        return None
