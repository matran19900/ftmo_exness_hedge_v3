"""Frontend-facing symbol mapping orchestrator (Phase 4.A.5).

Combines ``FTMOWhitelistService`` (FTMO half) + ``MappingCacheService``
(per-Exness-account half) into a per-pair lookup that the order service,
volume calculator, and check-symbol endpoint all consume.

Distinct from ``MappingCacheService``:

- ``MappingCacheService`` owns the wizard's *write* lifecycle (save/edit/
  populate Redis/broadcast).
- ``MappingService`` is the *read* layer used at order-execution time. It
  never mutates cache files or Redis state.

Per ``docs/phase-4-symbol-mapping-design.md`` §9 + §10.1.
"""

from __future__ import annotations

import logging
from typing import Literal

from redis import asyncio as redis_asyncio

from .ftmo_whitelist_service import FTMOSymbol, FTMOWhitelistService
from .mapping_cache_schemas import MappingEntry
from .mapping_cache_service import MappingCacheService

logger = logging.getLogger(__name__)


TradeableReason = Literal[
    "pair_not_found",
    "ftmo_symbol_not_whitelisted",
    "exness_account_has_no_mapping",
    "ftmo_symbol_not_mapped_for_exness_account",
]


class MappingService:
    """Read-only per-pair mapping facade.

    Public API:
      - ``get_ftmo_symbol(name)``
      - ``get_exness_mapping(exness_account_id, ftmo_symbol)``
      - ``get_pair_mapping(pair_id, ftmo_symbol)``
      - ``is_pair_symbol_tradeable(pair_id, ftmo_symbol)``
      - ``get_all_mappings_for_account(exness_account_id)``
    """

    def __init__(
        self,
        ftmo_whitelist: FTMOWhitelistService,
        cache_service: MappingCacheService,
        redis: redis_asyncio.Redis,
    ) -> None:
        self._ftmo_whitelist = ftmo_whitelist
        self._cache_service = cache_service
        self._redis = redis

    # ----- FTMO half -----

    def get_ftmo_symbol(self, ftmo_symbol: str) -> FTMOSymbol | None:
        """Return the FTMO whitelist entry, or ``None`` if not whitelisted."""
        return self._ftmo_whitelist.get(ftmo_symbol)

    def all_ftmo_symbol_names(self) -> list[str]:
        """Sorted list of every name in the FTMO whitelist."""
        return self._ftmo_whitelist.all_symbols()

    # ----- Exness half -----

    async def get_exness_mapping(
        self, exness_account_id: str, ftmo_symbol: str
    ) -> MappingEntry | None:
        """Return the per-account ``MappingEntry`` for ``ftmo_symbol``.

        Returns ``None`` when the account has no mapping cache or when the
        cache exists but does not include ``ftmo_symbol`` (the wizard saved
        a mapping with this symbol skipped).
        """
        cache = await self._cache_service.get_account_mapping(exness_account_id)
        if cache is None:
            return None
        for entry in cache.mappings:
            if entry.ftmo == ftmo_symbol:
                return entry
        return None

    async def get_all_mappings_for_account(
        self, exness_account_id: str
    ) -> list[MappingEntry]:
        """Return every ``MappingEntry`` for the account (empty list if no cache)."""
        cache = await self._cache_service.get_account_mapping(exness_account_id)
        if cache is None:
            return []
        return list(cache.mappings)

    # ----- Per-pair combined -----

    async def get_pair_mapping(
        self, pair_id: str, ftmo_symbol: str
    ) -> tuple[FTMOSymbol, MappingEntry] | None:
        """Per-pair convenience: return both the FTMO entry and the matching
        Exness mapping for ``pair_id`` + ``ftmo_symbol``.

        Returns ``None`` if any link is missing — pair, whitelist row, account
        cache, or per-symbol mapping. Callers that need to distinguish *which*
        link failed should use ``is_pair_symbol_tradeable`` instead.
        """
        pair = await self._redis.hgetall(f"pair:{pair_id}")  # type: ignore[misc]
        if not pair:
            return None
        exness_account_id = self._maybe_str(pair.get("exness_account_id"))
        if not exness_account_id:
            return None
        ftmo_entry = self.get_ftmo_symbol(ftmo_symbol)
        if ftmo_entry is None:
            return None
        exness_entry = await self.get_exness_mapping(exness_account_id, ftmo_symbol)
        if exness_entry is None:
            return None
        return ftmo_entry, exness_entry

    # ----- Pre-flight (D-4.A.0-5) -----

    async def is_pair_symbol_tradeable(
        self, pair_id: str, ftmo_symbol: str
    ) -> tuple[bool, TradeableReason | None]:
        """Pre-flight check used by ``OrderService.create_order`` and the
        ``GET /api/pairs/{pair_id}/check-symbol/{symbol}`` endpoint.

        Returns ``(True, None)`` when the order can proceed.

        Returns ``(False, reason)`` for one of the four documented failures.
        Phase 3 backward-compat (D-4.A.5-2):

          - When the pair has no ``exness_account_id`` (single-leg pair),
            only the FTMO whitelist is checked — Exness side is moot.
          - When the pair has ``exness_account_id`` but
            ``mapping_status:{acc} != "active"`` (wizard never run), the
            check passes for the FTMO side and the Exness side is treated
            as "not yet enforced" — preserves Phase 3 behavior. Once the
            operator runs the wizard for that account, the check upgrades
            to fully enforce per-symbol mapping.
        """
        pair = await self._redis.hgetall(f"pair:{pair_id}")  # type: ignore[misc]
        if not pair:
            return False, "pair_not_found"

        if not self._ftmo_whitelist.is_allowed(ftmo_symbol):
            return False, "ftmo_symbol_not_whitelisted"

        exness_account_id = self._maybe_str(pair.get("exness_account_id"))
        if not exness_account_id:
            # Phase 3 single-leg pair — FTMO check is sufficient.
            return True, None

        # Phase 3 compat: pair has exness_account_id, but the wizard hasn't
        # been run for that account yet. Treat as "Exness side not enforced".
        status_snap = await self._cache_service.get_mapping_status(exness_account_id)
        if status_snap.status != "active":
            logger.debug(
                "is_pair_symbol_tradeable: phase-3-compat pass "
                "pair=%s account=%s status=%s",
                pair_id, exness_account_id, status_snap.status,
            )
            return True, None

        cache = await self._cache_service.get_account_mapping(exness_account_id)
        if cache is None:
            return False, "exness_account_has_no_mapping"
        if not any(m.ftmo == ftmo_symbol for m in cache.mappings):
            return False, "ftmo_symbol_not_mapped_for_exness_account"
        return True, None

    # ----- helpers -----

    @staticmethod
    def _maybe_str(value: object) -> str | None:
        """Decode bytes / coerce str / treat empty as ``None``."""
        if value is None:
            return None
        if isinstance(value, bytes):
            text = value.decode()
        else:
            text = str(value)
        return text or None
