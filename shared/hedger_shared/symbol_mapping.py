"""Strict loader and Pydantic models for the FTMO/Exness symbol mapping JSON file."""

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
