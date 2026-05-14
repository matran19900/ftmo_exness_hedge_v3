"""Pydantic v2 strict schemas for manual symbol-match hints config.

Schemas per ``docs/phase-4-symbol-mapping-design.md`` §2.3 — used by the
``AutoMatchEngine`` tier-3 (``manual_hint``) lookup when neither tier-1
``exact`` nor tier-2 ``suffix_strip`` finds a match.

Config file location: ``server/config/symbol_match_hints.json`` (D-SM-12).
The file is bootstrapped at step 4.A.3 from the 14 ``match_type=="manual"``
entries in ``archive/symbol_mapping_ftmo_exness_v1.json`` (D-4.A.0-10) and
is hand-editable by the operator afterwards.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MatchHint(BaseModel):
    """One hint: maps a single FTMO symbol to one or more candidate Exness
    names. The engine tries the candidates in list order and accepts the
    first one that exists in the broker's raw symbol snapshot.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    ftmo: str
    exness_candidates: list[str]
    note: str = ""


class MatchHintsFile(BaseModel):
    """Top-level shape of ``server/config/symbol_match_hints.json``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(default=1, ge=1)
    version: int = Field(ge=1)
    hints: list[MatchHint]
