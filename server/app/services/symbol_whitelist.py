"""Legacy FTMO whitelist shim — module-level API preserved during Phase 4.A
staged refactor.

The shim delegates to ``FTMOWhitelistService`` (Phase 4.A.1) which owns
``server/data/ftmo_whitelist.json``. The legacy ``get_symbol_mapping``
returns the new ``FTMOSymbol`` shape (ftmo-only fields) — Exness fields
that used to live on the old ``SymbolMapping`` are deliberately gone.

Callers that depended on ``SymbolMapping.exness_*`` fields are slated for
refactor in step 4.A.5 (per ``docs/phase-4-symbol-mapping-design.md``
§9). Until then, this module continues to expose the function names
those callers reference; the *types* have changed.

The shim is removed entirely once step 4.A.5 lands and migrates every
call site to ``MappingService``.
"""

from __future__ import annotations

from app.services.ftmo_whitelist_service import (
    FTMOSymbol,
    FTMOWhitelistService,
)

_service: FTMOWhitelistService | None = None


def load_whitelist(path: str) -> None:
    """Construct the singleton service from ``path`` and cache it.

    Idempotent — calling twice replaces the cached state (used by tests
    that swap the fixture file mid-session).
    """
    global _service
    _service = FTMOWhitelistService(path)


def is_loaded() -> bool:
    """Return True once ``load_whitelist`` has populated the cache."""
    return _service is not None


def _require_service() -> FTMOWhitelistService:
    if _service is None:
        raise RuntimeError(
            "FTMO whitelist not loaded yet — call load_whitelist(path) first"
        )
    return _service


def get_all_symbols() -> list[str]:
    """Return the sorted list of FTMO symbol names from the cached whitelist."""
    if _service is None:
        return []
    return _service.all_symbols()


def get_symbol_mapping(ftmo_symbol: str) -> FTMOSymbol | None:
    """Return the FTMOSymbol entry, or None when not in the whitelist.

    Note: the return type is now ``FTMOSymbol`` (no Exness fields), down
    from the Phase 1-3 ``SymbolMapping``. Callers that touch
    ``.exness_*`` will get an AttributeError — they are refactored in
    step 4.A.5.
    """
    if _service is None:
        return None
    return _service.get(ftmo_symbol)
