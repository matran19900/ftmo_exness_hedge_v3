"""Tests for the FTMO whitelist service + the archived legacy loader.

Phase 4.A.5 cleanup: the ``app.services.symbol_whitelist`` shim is gone;
all callers consume ``FTMOWhitelistService`` directly. The legacy file
format (with Exness fields) still loads through the unchanged
``hedger_shared.symbol_mapping`` module for the archived-data regression
test below.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services.ftmo_whitelist_service import (
    FTMOSymbol,
    FTMOWhitelistFile,
    FTMOWhitelistService,
)
from hedger_shared.symbol_mapping import load_symbol_mapping
from pydantic import ValidationError

EXPECTED_SYMBOL_COUNT = 117


# ---------- FTMOWhitelistService ----------


def test_load_real_ftmo_whitelist(real_ftmo_whitelist_path: Path) -> None:
    """The new whitelist JSON loads + Pydantic-validates."""
    svc = FTMOWhitelistService(real_ftmo_whitelist_path)
    assert svc.count == EXPECTED_SYMBOL_COUNT
    assert isinstance(svc.get("EURUSD"), FTMOSymbol)


def test_ftmo_whitelist_strict_schema_rejects_unknown_field(
    tmp_path: Path, real_ftmo_whitelist_path: Path
) -> None:
    raw = json.loads(real_ftmo_whitelist_path.read_text(encoding="utf-8"))
    raw["symbols"][0]["unexpected_field"] = "boom"
    bad_path = tmp_path / "bad_whitelist.json"
    bad_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        FTMOWhitelistFile.model_validate(json.loads(bad_path.read_text()))


def test_ftmo_whitelist_unsupported_schema_version_raises(
    tmp_path: Path, real_ftmo_whitelist_path: Path
) -> None:
    raw = json.loads(real_ftmo_whitelist_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 99
    bad_path = tmp_path / "future_whitelist.json"
    bad_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported schema_version"):
        FTMOWhitelistService(bad_path)


def test_ftmo_whitelist_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FTMOWhitelistService(tmp_path / "does_not_exist.json")


def test_ftmo_whitelist_asset_class_of(real_ftmo_whitelist_path: Path) -> None:
    svc = FTMOWhitelistService(real_ftmo_whitelist_path)
    assert svc.asset_class_of("EURUSD") == "forex"
    assert svc.asset_class_of("XAUUSD") == "metals"
    assert svc.asset_class_of("NOTREAL") is None


def test_ftmo_whitelist_get_returns_typed_entry(
    real_ftmo_whitelist_path: Path,
) -> None:
    svc = FTMOWhitelistService(real_ftmo_whitelist_path)
    entry = svc.get("EURUSD")
    assert entry is not None
    assert isinstance(entry, FTMOSymbol)
    assert entry.name == "EURUSD"
    assert len(entry.quote_ccy) == 3


def test_ftmo_whitelist_get_returns_none_for_unknown(
    real_ftmo_whitelist_path: Path,
) -> None:
    svc = FTMOWhitelistService(real_ftmo_whitelist_path)
    assert svc.get("NOTREAL") is None


def test_ftmo_whitelist_all_symbols_sorted(
    real_ftmo_whitelist_path: Path,
) -> None:
    svc = FTMOWhitelistService(real_ftmo_whitelist_path)
    names = svc.all_symbols()
    assert names == sorted(names)
    assert len(names) == EXPECTED_SYMBOL_COUNT


# ---------- Legacy loader (archived file regression) ----------


def test_load_legacy_mapping_file(legacy_mapping_path: Path) -> None:
    """The archived Phase 1-3 mapping file still loads via the unchanged
    ``hedger_shared.symbol_mapping`` module. This guards the archive
    pathway used by tests + reference scripts."""
    parsed = load_symbol_mapping(legacy_mapping_path)
    assert parsed.version == 1
    assert len(parsed.mappings) == EXPECTED_SYMBOL_COUNT


def test_legacy_loader_strict_schema_rejects_unknown_field(
    tmp_path: Path, legacy_mapping_path: Path
) -> None:
    raw = json.loads(legacy_mapping_path.read_text(encoding="utf-8"))
    raw["mappings"][0]["unexpected_field"] = "boom"
    bad_path = tmp_path / "bad_legacy.json"
    bad_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_symbol_mapping(bad_path)
