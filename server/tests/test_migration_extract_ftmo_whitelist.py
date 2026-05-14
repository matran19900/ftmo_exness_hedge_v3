"""Tests for ``scripts/migrate_extract_ftmo_whitelist.py``.

Verifies:
  - Asset-class derivation heuristics cover the 6 classes the migration
    produces (forex / metals / indices / crypto / energies / stocks).
  - End-to-end run against the archived legacy file reproduces the
    committed ``server/data/ftmo_whitelist.json`` byte-for-byte
    (idempotency contract).
  - Synthetic input → expected output for a small fixture.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_extract_ftmo_whitelist.py"


@pytest.fixture(scope="module")
def migration() -> object:
    """Import the migration script as a module via importlib so the tests
    don't depend on ``scripts/`` being on PYTHONPATH."""
    spec = importlib.util.spec_from_file_location(
        "migrate_extract_ftmo_whitelist", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migrate_extract_ftmo_whitelist"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_derive_asset_class_forex(migration: object) -> None:
    assert migration._derive_asset_class("EURUSD", 100000) == "forex"
    assert migration._derive_asset_class("USDJPY", 100000) == "forex"


def test_derive_asset_class_metals(migration: object) -> None:
    assert migration._derive_asset_class("XAUUSD", 100) == "metals"
    assert migration._derive_asset_class("XAGUSD", 5000) == "metals"


def test_derive_asset_class_indices(migration: object) -> None:
    assert migration._derive_asset_class("US100.cash", 1) == "indices"
    assert migration._derive_asset_class("GER40.cash", 1) == "indices"


def test_derive_asset_class_energies(migration: object) -> None:
    assert migration._derive_asset_class("UKOIL.cash", 1) == "energies"
    assert migration._derive_asset_class("NATGAS.cash", 1000) == "energies"


def test_derive_asset_class_crypto(migration: object) -> None:
    assert migration._derive_asset_class("BTCUSD", 1) == "crypto"
    assert migration._derive_asset_class("ETHUSD", 1) == "crypto"


def test_derive_asset_class_stocks(migration: object) -> None:
    assert migration._derive_asset_class("AAPL", 1) == "stocks"
    assert migration._derive_asset_class("TSLA", 1) == "stocks"


def test_extract_entry_strips_exness_fields(migration: object) -> None:
    legacy = {
        "ftmo": "EURUSD",
        "exness": "EURUSDm",
        "match_type": "exact",
        "ftmo_units_per_lot": 100000,
        "exness_trade_contract_size": 100000,
        "ftmo_pip_size": 0.0001,
        "exness_pip_size": 0.0001,
        "ftmo_pip_value": 10.0,
        "exness_pip_value": 10.0,
        "quote_ccy": "USD",
    }
    out = migration._extract_entry(legacy)
    assert set(out.keys()) == {
        "name",
        "asset_class",
        "quote_ccy",
        "ftmo_units_per_lot",
        "ftmo_pip_size",
        "ftmo_pip_value",
    }
    assert out["name"] == "EURUSD"
    assert out["asset_class"] == "forex"


def test_extract_entry_missing_quote_ccy_defaults_usd(
    migration: object, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = {
        "ftmo": "FOO",
        "ftmo_units_per_lot": 1,
        "ftmo_pip_size": 0.1,
        "ftmo_pip_value": 0.1,
    }
    out = migration._extract_entry(legacy)
    assert out["quote_ccy"] == "USD"


def test_migrate_end_to_end_idempotent(
    migration: object, tmp_path: Path
) -> None:
    """Run the migration against the archived legacy file and assert the
    output matches the committed ``server/data/ftmo_whitelist.json``
    byte-for-byte (idempotency claim in step 4.A.1)."""
    legacy_path = REPO_ROOT / "archive" / "symbol_mapping_ftmo_exness_v1.json"
    committed = REPO_ROOT / "server" / "data" / "ftmo_whitelist.json"
    out_path = tmp_path / "ftmo_whitelist.json"
    rc = migration.migrate(legacy_path, out_path, dry_run=False)
    assert rc == 0
    assert out_path.read_text() == committed.read_text()


def test_migrate_missing_input_returns_1(
    migration: object, tmp_path: Path
) -> None:
    rc = migration.migrate(
        tmp_path / "missing.json", tmp_path / "out.json", dry_run=False
    )
    assert rc == 1


def test_migrate_output_is_alpha_sorted(migration: object) -> None:
    out = json.loads(
        (REPO_ROOT / "server" / "data" / "ftmo_whitelist.json").read_text()
    )
    names = [s["name"] for s in out["symbols"]]
    assert names == sorted(names)


def test_migrate_output_has_no_exness_keys(migration: object) -> None:
    text = (REPO_ROOT / "server" / "data" / "ftmo_whitelist.json").read_text()
    assert "exness" not in text.lower()
