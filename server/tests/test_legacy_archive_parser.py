"""Archive regression tests — Phase 1-3 monolithic mapping JSON parser.

Step 4.4b (CPR finalization): extracted from ``test_symbol_whitelist.py``
when ``hedger_shared.symbol_mapping`` moved to
``app.services.legacy_archive_parser`` and the ``shared/`` package was
deleted. The 8 production FTMOWhitelistService tests stayed in
``test_symbol_whitelist.py`` (their target IS a symbol whitelist); this
file holds only the 2 tests that exercise the legacy archive parser.

The parser exists solely to keep the Phase 1-3
``archive/symbol_mapping_ftmo_exness_v1.json`` data file forensically
loadable. These tests guard the parser's strict-schema validation, NOT
production whitelist behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services.legacy_archive_parser import load_symbol_mapping
from pydantic import ValidationError

EXPECTED_SYMBOL_COUNT = 117


def test_load_legacy_mapping_file(legacy_mapping_path: Path) -> None:
    """The archived Phase 1-3 mapping file still loads via the unchanged
    ``app.services.legacy_archive_parser`` module. This guards the
    archive pathway used by tests + reference scripts."""
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
