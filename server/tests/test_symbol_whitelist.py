"""Tests for the shared symbol mapping loader and the whitelist service cache."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services import symbol_whitelist
from hedger_shared.symbol_mapping import load_symbol_mapping
from pydantic import ValidationError

EXPECTED_SYMBOL_COUNT = 117


def _existing_symbol() -> str:
    syms = symbol_whitelist.get_all_symbols()
    return "EURUSD" if "EURUSD" in syms else syms[0]


def test_load_real_file(real_mapping_path: Path) -> None:
    parsed = load_symbol_mapping(real_mapping_path)
    assert parsed.version == 1
    assert len(parsed.mappings) == EXPECTED_SYMBOL_COUNT


def test_strict_schema_rejects_unknown_field(tmp_path: Path, real_mapping_path: Path) -> None:
    raw = json.loads(real_mapping_path.read_text(encoding="utf-8"))
    raw["mappings"][0]["unexpected_field"] = "boom"
    bad_path = tmp_path / "bad_mapping.json"
    bad_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_symbol_mapping(bad_path)


def test_get_symbol_mapping_returns_correct_entry() -> None:
    target = _existing_symbol()
    mapping = symbol_whitelist.get_symbol_mapping(target)
    assert mapping is not None
    assert mapping.ftmo == target
    assert len(mapping.quote_ccy) == 3


def test_get_symbol_mapping_returns_none_for_unknown() -> None:
    assert symbol_whitelist.get_symbol_mapping("NOTREAL") is None
