"""Symbol sync publisher (Phase 4.2).

On bridge connect (and on operator-triggered ``resync_symbols`` actions)
we enumerate every MT5 symbol the broker exposes via ``mt5.symbols_get``
+ ``mt5.symbol_info`` per symbol, filter to the tradeable set, and
publish the snapshot to ``exness_raw_symbols:{account_id}`` (a JSON
STRING in Redis, no TTL — the server's wizard layer cleans it up after
the operator saves a mapping; D-SM-06).

Per ``docs/SYMBOL_MAPPING_DECISIONS.md`` D-SM-02 step 2 the published
shape is one ``RawSymbolEntry`` (server schema, D-SM-11) per tradeable
symbol — the wizard then runs auto-match against the FTMO whitelist and
prompts the operator to confirm.

Every MT5 call is wrapped in ``asyncio.to_thread`` because the
``MetaTrader5`` package is fully synchronous (D-4.1.A).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _derive_pip_size(point: float, digits: int) -> float:
    """Convert MT5 ``point`` (smallest price increment) into a pip.

    CTO Phase 4 lock: ``digits == 5`` (5-digit forex, e.g. EURUSD) and
    ``digits == 3`` (3-digit JPY-quote, e.g. USDJPY) → ``point * 10``;
    everything else (2-digit metals, indices, crypto) → ``point`` as-is.
    Documented in ``docs/mt5-execution-events.md`` §6.
    """
    if digits in (3, 5):
        return point * 10
    return point


class SymbolSyncPublisher:
    """Publishes the broker's tradeable symbols to Redis.

    Stateless other than the bound ``account_id`` + ``mt5_module``; safe
    to instantiate per-bridge or per-action-handler. ``publish_snapshot``
    is the only public method.
    """

    def __init__(
        self,
        redis_client: Any,
        account_id: str,
        mt5_module: Any,
    ) -> None:
        self._redis = redis_client
        self._account_id = account_id
        self._mt5 = mt5_module
        self._key = f"exness_raw_symbols:{account_id}"

    async def publish_snapshot(self) -> int:
        """Enumerate broker symbols → publish JSON to Redis.

        Returns the number of symbols published. Returns ``0`` (without
        raising) on any partial-state failure: empty broker list, all
        symbols filtered out, even an exception during enumeration.
        Bridge connect treats this as non-fatal so the client stays up
        and the operator can re-trigger via ``resync_symbols``.
        """
        try:
            symbols = await asyncio.to_thread(self._mt5.symbols_get)
        except Exception:
            logger.exception("symbol_sync.symbols_get_failed")
            return 0

        if not symbols:
            logger.warning("symbol_sync.empty_symbols_get")
            return 0

        snapshot: list[dict[str, Any]] = []
        for sym in symbols:
            try:
                # Ensure the symbol is in MarketWatch — quote feed needs
                # the ``symbol_select`` call before tick data is available
                # (real MT5 quirk; the stub is a no-op that just records
                # the call for assertions).
                await asyncio.to_thread(self._mt5.symbol_select, sym.name, True)
                detail = await asyncio.to_thread(self._mt5.symbol_info, sym.name)
                if detail is None:
                    logger.debug("symbol_sync.no_info_for symbol=%s", sym.name)
                    continue
                if detail.trade_mode != self._mt5.SYMBOL_TRADE_MODE_FULL:
                    logger.debug(
                        "symbol_sync.not_tradeable symbol=%s trade_mode=%s",
                        sym.name,
                        detail.trade_mode,
                    )
                    continue
                snapshot.append(
                    {
                        "name": detail.name,
                        "contract_size": detail.trade_contract_size,
                        "digits": detail.digits,
                        "pip_size": _derive_pip_size(
                            detail.point, detail.digits
                        ),
                        "volume_min": detail.volume_min,
                        "volume_step": detail.volume_step,
                        "volume_max": detail.volume_max,
                        "currency_profit": detail.currency_profit,
                    }
                )
            except Exception:
                # One bad symbol doesn't poison the whole snapshot.
                logger.exception(
                    "symbol_sync.symbol_failed symbol=%s",
                    getattr(sym, "name", "<unknown>"),
                )
                continue

        if not snapshot:
            logger.warning("symbol_sync.empty_filtered_snapshot")
            return 0

        try:
            await self._redis.set(self._key, json.dumps(snapshot))
        except Exception:
            logger.exception(
                "symbol_sync.redis_set_failed key=%s", self._key
            )
            return 0

        logger.info(
            "symbol_sync.published key=%s count=%d",
            self._key,
            len(snapshot),
        )
        return len(snapshot)
