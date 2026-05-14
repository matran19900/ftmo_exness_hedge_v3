"""FTMO whitelist service.

Loads ``server/data/ftmo_whitelist.json`` at lifespan startup, exposes a
lookup API for the rest of the server. Read-only at runtime (R34 immutable);
restart the server to reload.

Per ``docs/phase-4-symbol-mapping-design.md`` §2.1 (D-SM-09) — this is the
FTMO half of the Phase 1-3 ``symbol_mapping_ftmo_exness.json`` split. The
Exness half lives in ``server/data/symbol_mapping_cache/*.json`` and is
managed per-Exness-account (D-SM-01).

The legacy module-level functions in ``app.services.symbol_whitelist`` are
preserved as a thin shim during the staged refactor — see that module for
the transitional API surface that consumers still call. Step 4.A.5 will
remove the shim once all call sites migrate to ``MappingService``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# AssetClass enum is loose at this layer (str) so the migration script's
# heuristics can extend over time without forcing a Pydantic update on every
# new bucket. The full enum is enforced at the wizard layer (step 4.A.4).
AssetClass = Literal[
    "forex", "metals", "indices", "commodities", "energies", "crypto", "stocks"
]


class FTMOSymbol(BaseModel):
    """A single entry in ``server/data/ftmo_whitelist.json``.

    Mirror of ``docs/phase-4-symbol-mapping-design.md §2.1.2``. ``frozen=True``
    means callers can stash references without worrying about accidental
    mutation; ``extra="forbid"`` (strict mode) catches schema drift early
    per D-016.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str
    asset_class: AssetClass
    quote_ccy: str = Field(min_length=3, max_length=3)
    ftmo_units_per_lot: float = Field(gt=0)
    ftmo_pip_size: float = Field(gt=0)
    ftmo_pip_value: float = Field(gt=0)


class FTMOWhitelistFile(BaseModel):
    """Top-level shape of ``server/data/ftmo_whitelist.json``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(ge=1)
    version: int = Field(ge=1)
    symbols: list[FTMOSymbol]


class FTMOWhitelistService:
    """Singleton wrapper around the loaded whitelist file.

    Public API:
      - ``is_allowed(name)``        — membership check.
      - ``get(name)``               — full ``FTMOSymbol`` or ``None``.
      - ``all_symbols()``           — sorted list of names.
      - ``all_entries()``           — list of ``FTMOSymbol``.
      - ``asset_class_of(name)``    — convenience.
      - ``count``                   — total entries (property).

    The class also exposes ``map_to_exness`` and ``volume_conversion_ratio``
    that intentionally raise ``NotImplementedError``. These methods were
    moved to ``MappingService`` per D-SM-01 — keeping the names as raising
    stubs makes ``grep`` for migration progress straightforward (step 4.A.5
    will delete them once all call sites are reachable through the new
    service).
    """

    SUPPORTED_SCHEMA_VERSION = 1

    def __init__(self, file_path: str | Path) -> None:
        self._file_path = Path(file_path)
        self._entries: dict[str, FTMOSymbol] = {}
        self.load()

    def load(self) -> None:
        """(Re)load the whitelist from disk. Raises ``FileNotFoundError`` if
        the file is missing and ``ValidationError`` on schema drift."""
        if not self._file_path.is_file():
            raise FileNotFoundError(
                f"FTMO whitelist file not found: {self._file_path}"
            )
        raw = json.loads(self._file_path.read_text(encoding="utf-8"))
        parsed = FTMOWhitelistFile.model_validate(raw)
        if parsed.schema_version != self.SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {parsed.schema_version} in "
                f"{self._file_path}; expected {self.SUPPORTED_SCHEMA_VERSION}"
            )
        self._entries = {s.name: s for s in parsed.symbols}
        logger.info(
            "FTMOWhitelistService loaded %d symbols from %s",
            len(self._entries),
            self._file_path,
        )

    # ----- Lookup -----

    def is_allowed(self, name: str) -> bool:
        return name in self._entries

    def get(self, name: str) -> FTMOSymbol | None:
        return self._entries.get(name)

    def all_symbols(self) -> list[str]:
        return sorted(self._entries.keys())

    def all_entries(self) -> list[FTMOSymbol]:
        return list(self._entries.values())

    def asset_class_of(self, name: str) -> str | None:
        entry = self._entries.get(name)
        return entry.asset_class if entry else None

    @property
    def count(self) -> int:
        return len(self._entries)

    # ----- Stubs for methods moved to MappingService (step 4.A.5) -----

    def map_to_exness(self, ftmo_symbol: str) -> str:
        raise NotImplementedError(
            "map_to_exness moved to MappingService at step 4.A.5 "
            "(per-Exness-account mapping; cannot be resolved without "
            "an account context)"
        )

    def volume_conversion_ratio(self, ftmo_symbol: str) -> float:
        raise NotImplementedError(
            "volume_conversion_ratio moved to MappingService at step 4.A.5 "
            "(depends on per-account exness_contract_size)"
        )
