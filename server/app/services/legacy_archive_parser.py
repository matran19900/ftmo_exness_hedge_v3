"""Legacy archive mapping parser (Phase 1-3 monolithic format).

Originally ``shared/hedger_shared/symbol_mapping.py``. Moved to
``server/app/services/`` in step 4.4b as part of the CPR finalization
that deleted the entire ``hedger_shared`` package.

This module exists ONLY to parse archived Phase 1-3 monolithic symbol
mapping JSON files (``archive/symbol_mapping_ftmo_exness_v1.json``).
NOT used by runtime production code post-step 4.A.5 — the
``MappingService`` orchestrator handles all runtime mapping operations.

Phase 4.A migration completed as of step 4.A.5: the Phase 1-3 single-file
``symbol_mapping_ftmo_exness.json`` shape modeled here was split into:

  - ``server/data/ftmo_whitelist.json`` (FTMO half), loaded by
    ``app.services.ftmo_whitelist_service.FTMOWhitelistService``.
  - ``server/data/symbol_mapping_cache/{account}_{signature}.json``
    (per-Exness-account half), managed by
    ``app.services.mapping_cache_service.MappingCacheService``.

Per-pair lookups now go through
``app.services.mapping_service.MappingService``.

Retained for forensic / data archaeology — the only consumer is the
archive regression test
``server/tests/test_legacy_archive_parser.py::test_load_legacy_mapping_file``
which guards against schema regressions in the parser itself. Do NOT
import in new runtime code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SymbolMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ftmo: str
    exness: str
    match_type: Literal["exact", "manual", "suffix_strip"]
    ftmo_units_per_lot: float
    exness_trade_contract_size: float
    ftmo_pip_size: float
    exness_pip_size: float
    ftmo_pip_value: float
    exness_pip_value: float
    quote_ccy: str = Field(min_length=3, max_length=3)


class SymbolMappingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    generated_from: list[str] = []
    pip_formula: str = ""
    notes: list[str] = []
    mappings: list[SymbolMapping]
    ftmo_unmapped: list[str] = []


def load_symbol_mapping(path: str | Path) -> SymbolMappingFile:
    """Load and strictly validate the symbol mapping JSON file.

    Raises FileNotFoundError if the path does not exist, and pydantic ValidationError
    on schema violations (extra fields at top level or per-mapping are rejected).
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Symbol mapping file not found: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    return SymbolMappingFile.model_validate(raw)


def build_ftmo_index(mapping: SymbolMappingFile) -> dict[str, SymbolMapping]:
    """Build an O(1) lookup index keyed by FTMO symbol name."""
    return {m.ftmo: m for m in mapping.mappings}
