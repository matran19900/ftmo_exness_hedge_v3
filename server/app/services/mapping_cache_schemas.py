"""Pydantic v2 strict schemas for symbol mapping cache files.

Schemas per ``docs/phase-4-symbol-mapping-design.md`` §2.2 plus the
deviations applied at design freeze:

- D-4.A.0-1: ``SymbolMappingCacheFile.schema_version`` for forward-compat.
- D-4.A.0-2: ``MappingEntry`` adds ``exness_volume_step/min/max`` so the
  hot-path order service can clamp volumes without re-reading the raw
  snapshot.

All models are ``extra="forbid"`` + ``strict=True``: unknown fields cause a
ValidationError and integers are rejected where floats are expected (and
vice versa). Schema drift fails loud at load time rather than silently
corrupting Redis state at runtime.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RawSymbolEntry(BaseModel):
    """Single Exness symbol snapshot from ``mt5.symbol_info()``.

    Per design §2.2 RawSymbolEntry — 8 fields. ``contract_size`` mirrors
    ``SymbolInfo.trade_contract_size``; ``currency_profit`` is the 3-letter
    ISO quote currency the broker reports.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    contract_size: float
    digits: int
    pip_size: float
    volume_min: float
    volume_step: float
    volume_max: float
    currency_profit: str = Field(min_length=3, max_length=3)


class MappingEntry(BaseModel):
    """Confirmed FTMO ↔ Exness mapping with copied broker specs.

    Per design §2.2 MappingEntry plus D-4.A.0-2 additions. 10 fields total:
    7 from D-SM-11 (ftmo, exness, match_type, contract_size, pip_size,
    pip_value, quote_ccy) + 3 D-4.A.0-2 hot-path additions
    (exness_volume_step/min/max).

    ``contract_size`` is a *copy* of ``raw.contract_size`` taken at the
    time the mapping is confirmed — so a later snapshot refresh that
    changes the broker contract size is detected by signature mismatch
    rather than silently shifting in-flight orders.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    ftmo: str
    exness: str
    match_type: str
    contract_size: float
    pip_size: float
    pip_value: float
    quote_ccy: str = Field(min_length=3, max_length=3)
    exness_volume_step: float
    exness_volume_min: float
    exness_volume_max: float


class SymbolMappingCacheFile(BaseModel):
    """Full cache file content. One file per signature.

    Per design §2.2 SymbolMappingCacheFile + D-4.A.0-1 ``schema_version``.
    Filename pattern is ``{created_by_account}_{signature}.json`` (D-SM-10);
    ``used_by_accounts`` is the reverse index of every account that has
    later been bound to this signature.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(default=1, ge=1)
    signature: str
    created_at: datetime
    updated_at: datetime
    created_by_account: str
    used_by_accounts: list[str]
    raw_symbols_snapshot: list[RawSymbolEntry]
    mappings: list[MappingEntry]
