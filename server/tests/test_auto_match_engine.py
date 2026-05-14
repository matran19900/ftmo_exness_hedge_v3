"""Tests for ``AutoMatchEngine`` + ``MatchHint`` schemas (Phase 4.A.3).

Coverage matrix per step plan §2.7:
  - §2.7.1 Schema validation
  - §2.7.2 Engine load (happy / missing / malformed / hot-reload)
  - §2.7.3 Tier 1 — exact
  - §2.7.4 Tier 2 — suffix_strip (7 patterns + edge cases)
  - §2.7.5 Tier 3 — manual_hint
  - §2.7.6 Tier precedence (exact > suffix > hint)
  - §2.7.7 Uniqueness (one Exness name per call)
  - §2.7.8 Integration — real bootstrap config + real ftmo_whitelist
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services.auto_match_engine import (
    AutoMatchEngine,
    AutoMatchProposal,
    MatchProposal,
)
from app.services.ftmo_whitelist_service import FTMOSymbol, FTMOWhitelistService
from app.services.mapping_cache_schemas import RawSymbolEntry
from app.services.match_hints_schemas import MatchHint, MatchHintsFile
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_HINTS_PATH = REPO_ROOT / "server" / "config" / "symbol_match_hints.json"
REAL_FTMO_WHITELIST_PATH = REPO_ROOT / "server" / "data" / "ftmo_whitelist.json"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _ftmo(name: str) -> FTMOSymbol:
    """Build a minimal FTMOSymbol — most fields are not consulted by the engine."""
    return FTMOSymbol(
        name=name,
        asset_class="forex",
        quote_ccy="USD",
        ftmo_units_per_lot=100000.0,
        ftmo_pip_size=0.0001,
        ftmo_pip_value=10.0,
    )


def _raw(name: str) -> RawSymbolEntry:
    return RawSymbolEntry(
        name=name,
        contract_size=100000.0,
        digits=5,
        pip_size=0.0001,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=200.0,
        currency_profit="USD",
    )


def _write_hints_file(
    path: Path, hints: list[dict[str, object]], schema_version: int = 1, version: int = 1
) -> None:
    payload = {
        "schema_version": schema_version,
        "version": version,
        "hints": hints,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@pytest.fixture
def empty_hints_path(tmp_path: Path) -> Path:
    p = tmp_path / "hints.json"
    _write_hints_file(p, [])
    return p


@pytest.fixture
def engine_no_hints(empty_hints_path: Path) -> AutoMatchEngine:
    return AutoMatchEngine(empty_hints_path)


# ---------------------------------------------------------------------------
# §2.7.1 — Schema tests
# ---------------------------------------------------------------------------


class TestMatchHintSchema:
    def test_happy_path(self) -> None:
        h = MatchHint(ftmo="EURUSD", exness_candidates=["EURUSDz"], note="x")
        assert h.ftmo == "EURUSD"
        assert h.exness_candidates == ["EURUSDz"]

    def test_default_note_is_empty(self) -> None:
        h = MatchHint(ftmo="EURUSD", exness_candidates=["EURUSDz"])
        assert h.note == ""

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            MatchHint(
                ftmo="EURUSD",
                exness_candidates=["EURUSDz"],
                note="x",
                surprise=True,  # type: ignore[call-arg]
            )

    def test_empty_candidates_allowed(self) -> None:
        # A degenerate but legal config — degrades to "no hint match".
        h = MatchHint(ftmo="EURUSD", exness_candidates=[])
        assert h.exness_candidates == []

    def test_strict_int_for_str_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MatchHint(ftmo=123, exness_candidates=["EURUSDz"])  # type: ignore[arg-type]


class TestMatchHintsFileSchema:
    def test_happy_path(self) -> None:
        f = MatchHintsFile(
            schema_version=1,
            version=1,
            hints=[MatchHint(ftmo="EURUSD", exness_candidates=["EURUSDz"])],
        )
        assert f.schema_version == 1
        assert len(f.hints) == 1

    def test_top_level_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            MatchHintsFile.model_validate(
                {"schema_version": 1, "version": 1, "hints": [], "extra_top": "x"}
            )

    def test_missing_required_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MatchHintsFile.model_validate({"schema_version": 1, "hints": []})

    def test_schema_version_default(self) -> None:
        f = MatchHintsFile(version=1, hints=[])
        assert f.schema_version == 1

    def test_schema_version_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            MatchHintsFile.model_validate(
                {"schema_version": 0, "version": 1, "hints": []}
            )


# ---------------------------------------------------------------------------
# §2.7.2 — Engine load tests
# ---------------------------------------------------------------------------


class TestEngineLoad:
    def test_happy_path_loads_hints(self, tmp_path: Path) -> None:
        path = tmp_path / "h.json"
        _write_hints_file(
            path,
            [{"ftmo": "EURUSD", "exness_candidates": ["EURUSDz"], "note": ""}],
        )
        engine = AutoMatchEngine(path)
        assert engine.hint_count == 1
        assert engine.hints_path == path

    def test_missing_file_logs_warning_zero_hints(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "does-not-exist.json"
        with caplog.at_level("WARNING"):
            engine = AutoMatchEngine(path)
        assert engine.hint_count == 0
        assert any(
            "auto_match_engine.hints_file_missing" in r.message for r in caplog.records
        )

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ValidationError):
            AutoMatchEngine(path)

    def test_schema_drift_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "drift.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": 1,
                    "hints": [],
                    "rogue_field": True,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            AutoMatchEngine(path)

    def test_load_hints_reload_picks_up_changes(self, tmp_path: Path) -> None:
        path = tmp_path / "h.json"
        _write_hints_file(path, [])
        engine = AutoMatchEngine(path)
        assert engine.hint_count == 0
        _write_hints_file(
            path,
            [
                {"ftmo": "EURUSD", "exness_candidates": ["EURUSDz"], "note": ""},
                {"ftmo": "GBPUSD", "exness_candidates": ["GBPUSDz"], "note": ""},
            ],
        )
        engine.load_hints()
        assert engine.hint_count == 2

    def test_load_hints_zero_entries_ok(self, empty_hints_path: Path) -> None:
        engine = AutoMatchEngine(empty_hints_path)
        assert engine.hint_count == 0


# ---------------------------------------------------------------------------
# §2.7.3 — Tier 1 exact
# ---------------------------------------------------------------------------


class TestTierExact:
    def test_single_exact_match(self, engine_no_hints: AutoMatchEngine) -> None:
        result = engine_no_hints.match([_ftmo("EURUSD")], [_raw("EURUSD")])
        assert result.proposals == [
            MatchProposal("EURUSD", "EURUSD", "exact", "high")
        ]
        assert result.unmapped_ftmo == []
        assert result.unmapped_exness == []

    def test_no_exact_when_only_suffix_form_present(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        # Engine should NOT pick exact — but tier 2 will still match.
        result = engine_no_hints.match([_ftmo("EURUSD")], [_raw("EURUSDm")])
        assert len(result.proposals) == 1
        assert result.proposals[0].match_type == "suffix_strip"

    def test_multiple_exact_matches(self, engine_no_hints: AutoMatchEngine) -> None:
        result = engine_no_hints.match(
            [_ftmo("EURUSD"), _ftmo("GBPUSD")],
            [_raw("EURUSD"), _raw("GBPUSD")],
        )
        assert {p.ftmo for p in result.proposals} == {"EURUSD", "GBPUSD"}
        assert all(p.match_type == "exact" for p in result.proposals)

    def test_case_sensitive_exact_match(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        result = engine_no_hints.match([_ftmo("EURUSD")], [_raw("eurusd")])
        assert result.proposals == []
        assert result.unmapped_ftmo == ["EURUSD"]
        assert result.unmapped_exness == ["eurusd"]

    def test_empty_exness_list_all_unmapped(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        result = engine_no_hints.match([_ftmo("EURUSD"), _ftmo("GBPUSD")], [])
        assert result.proposals == []
        assert result.unmapped_ftmo == ["EURUSD", "GBPUSD"]
        assert result.unmapped_exness == []


# ---------------------------------------------------------------------------
# §2.7.4 — Tier 2 suffix_strip
# ---------------------------------------------------------------------------


class TestTierSuffixStrip:
    @pytest.mark.parametrize(
        ("ftmo", "exness", "expected_suffix"),
        [
            ("EURUSD", "EURUSDm", "m"),
            ("EURUSD", "EURUSDc", "c"),
            ("EURUSD", "EURUSDz", "z"),
            ("EURUSD", "EURUSD_i", "_i"),
            ("EURUSD", "EURUSD_premium", "_premium"),
            ("EURUSD", "EURUSD_raw", "_raw"),
            ("US30", "US30.cash", ".cash"),
        ],
    )
    def test_each_suffix_pattern_matches(
        self,
        engine_no_hints: AutoMatchEngine,
        ftmo: str,
        exness: str,
        expected_suffix: str,
    ) -> None:
        result = engine_no_hints.match([_ftmo(ftmo)], [_raw(exness)])
        assert len(result.proposals) == 1, (
            f"{ftmo} → {exness} (suffix={expected_suffix}) failed: {result}"
        )
        p = result.proposals[0]
        assert p.match_type == "suffix_strip"
        assert p.confidence == "medium"
        assert p.exness == exness

    def test_longest_suffix_wins_premium_over_m(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        # `_premium` is listed first in the suffix tuple; it must be tried
        # before `m`. So `EURUSDz_premium` strips `_premium` → `EURUSDz`,
        # which does not equal `EURUSD`, so no match. The interesting case
        # is `EURUSD_premium` which strips to `EURUSD` — never to
        # `EURUSD_premiu` (no `m` suffix taken first).
        result = engine_no_hints.match(
            [_ftmo("EURUSD")], [_raw("EURUSD_premium")]
        )
        assert len(result.proposals) == 1
        assert result.proposals[0].exness == "EURUSD_premium"

    def test_longest_suffix_priority_over_short(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        # Both candidates exist. Engine iterates suffix patterns in order
        # (longest first), so the `_premium` candidate is found before `m`.
        result = engine_no_hints.match(
            [_ftmo("EURUSD")],
            [_raw("EURUSDm"), _raw("EURUSD_premium")],
        )
        assert len(result.proposals) == 1
        assert result.proposals[0].exness == "EURUSD_premium"
        # The `m` form is left unclaimed.
        assert result.unmapped_exness == ["EURUSDm"]

    def test_symbol_shorter_than_suffix_skipped(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        # raw `m` would strip to "" — must not match an empty FTMO name.
        result = engine_no_hints.match([_ftmo("")], [_raw("m")])
        assert result.proposals == []
        assert result.unmapped_ftmo == [""]

    def test_no_partial_substring_match(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        # `EURUSDmid` does NOT end with any suffix → no tier-2 match.
        result = engine_no_hints.match([_ftmo("EURUSD")], [_raw("EURUSDmid")])
        assert result.proposals == []
        assert result.unmapped_ftmo == ["EURUSD"]
        assert result.unmapped_exness == ["EURUSDmid"]

    def test_suffix_match_ignored_when_exact_available(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        # If both an exact and a suffix candidate exist, exact must win
        # and the suffix candidate must show up unclaimed.
        result = engine_no_hints.match(
            [_ftmo("EURUSD")],
            [_raw("EURUSD"), _raw("EURUSDm")],
        )
        assert len(result.proposals) == 1
        assert result.proposals[0].match_type == "exact"
        assert result.unmapped_exness == ["EURUSDm"]


# ---------------------------------------------------------------------------
# §2.7.5 — Tier 3 manual_hint
# ---------------------------------------------------------------------------


class TestTierManualHint:
    def test_hint_match_low_confidence(self, tmp_path: Path) -> None:
        path = tmp_path / "h.json"
        _write_hints_file(
            path,
            [{"ftmo": "NATGAS.cash", "exness_candidates": ["XNGUSD"], "note": ""}],
        )
        engine = AutoMatchEngine(path)
        result = engine.match([_ftmo("NATGAS.cash")], [_raw("XNGUSD")])
        assert result.proposals == [
            MatchProposal("NATGAS.cash", "XNGUSD", "manual_hint", "low")
        ]

    def test_hint_candidate_absent_unmapped(self, tmp_path: Path) -> None:
        path = tmp_path / "h.json"
        _write_hints_file(
            path,
            [{"ftmo": "NATGAS.cash", "exness_candidates": ["XNGUSD"], "note": ""}],
        )
        engine = AutoMatchEngine(path)
        result = engine.match([_ftmo("NATGAS.cash")], [_raw("XYZ")])
        assert result.proposals == []
        assert result.unmapped_ftmo == ["NATGAS.cash"]
        assert result.unmapped_exness == ["XYZ"]

    def test_first_matching_candidate_wins(self, tmp_path: Path) -> None:
        path = tmp_path / "h.json"
        _write_hints_file(
            path,
            [
                {
                    "ftmo": "NATGAS.cash",
                    "exness_candidates": ["XNGUSD", "NG"],
                    "note": "",
                }
            ],
        )
        engine = AutoMatchEngine(path)
        # Both candidates available; the first listed (XNGUSD) must win.
        result = engine.match(
            [_ftmo("NATGAS.cash")],
            [_raw("XNGUSD"), _raw("NG")],
        )
        assert result.proposals[0].exness == "XNGUSD"
        assert result.unmapped_exness == ["NG"]

    def test_hint_not_in_config_unmapped(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        result = engine_no_hints.match([_ftmo("NATGAS.cash")], [_raw("XNGUSD")])
        assert result.proposals == []
        assert result.unmapped_ftmo == ["NATGAS.cash"]

    def test_hint_lookup_case_sensitive(self, tmp_path: Path) -> None:
        path = tmp_path / "h.json"
        _write_hints_file(
            path,
            [{"ftmo": "NATGAS.cash", "exness_candidates": ["XNGUSD"], "note": ""}],
        )
        engine = AutoMatchEngine(path)
        # FTMO name case differs → hint does not apply.
        result = engine.match([_ftmo("natgas.cash")], [_raw("XNGUSD")])
        assert result.proposals == []
        assert result.unmapped_ftmo == ["natgas.cash"]

    def test_hint_skipped_if_candidate_already_used(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "h.json"
        _write_hints_file(
            path,
            [
                {
                    "ftmo": "ALIAS",
                    "exness_candidates": ["EURUSD"],
                    "note": "",
                }
            ],
        )
        engine = AutoMatchEngine(path)
        # EURUSD is exact-matched by the EURUSD ftmo first; the hint for
        # ALIAS → EURUSD then has no candidate left.
        result = engine.match(
            [_ftmo("EURUSD"), _ftmo("ALIAS")],
            [_raw("EURUSD")],
        )
        assert len(result.proposals) == 1
        assert result.proposals[0].ftmo == "EURUSD"
        assert "ALIAS" in result.unmapped_ftmo


# ---------------------------------------------------------------------------
# §2.7.6 — Tier precedence
# ---------------------------------------------------------------------------


class TestTierPrecedence:
    def test_exact_beats_suffix_strip(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        result = engine_no_hints.match(
            [_ftmo("EURUSD")],
            [_raw("EURUSD"), _raw("EURUSDm")],
        )
        assert result.proposals[0].match_type == "exact"

    def test_suffix_strip_beats_manual_hint(self, tmp_path: Path) -> None:
        path = tmp_path / "h.json"
        _write_hints_file(
            path,
            [{"ftmo": "EURUSD", "exness_candidates": ["XYZ_HINT"], "note": ""}],
        )
        engine = AutoMatchEngine(path)
        result = engine.match(
            [_ftmo("EURUSD")],
            [_raw("EURUSDm"), _raw("XYZ_HINT")],
        )
        # suffix_strip (medium) wins over manual_hint (low).
        assert result.proposals[0].match_type == "suffix_strip"
        assert result.proposals[0].exness == "EURUSDm"

    def test_manual_hint_used_only_when_others_fail(self, tmp_path: Path) -> None:
        path = tmp_path / "h.json"
        _write_hints_file(
            path,
            [{"ftmo": "GER40.cash", "exness_candidates": ["DE30"], "note": ""}],
        )
        engine = AutoMatchEngine(path)
        # No exact, no suffix → hint kicks in.
        result = engine.match([_ftmo("GER40.cash")], [_raw("DE30")])
        assert result.proposals[0].match_type == "manual_hint"


# ---------------------------------------------------------------------------
# §2.7.7 — Uniqueness
# ---------------------------------------------------------------------------


class TestUniqueness:
    def test_one_exness_claimed_only_once(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        # Two FTMO names both want exact-match `EURUSD`; only the first
        # wins, the second goes unmapped.
        result = engine_no_hints.match(
            [_ftmo("EURUSD"), _ftmo("EURUSD")],
            [_raw("EURUSD")],
        )
        assert len(result.proposals) == 1
        assert result.unmapped_ftmo == ["EURUSD"]

    def test_unmapped_exness_sorted(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        result = engine_no_hints.match(
            [],
            [_raw("ZZZ"), _raw("AAA"), _raw("MMM")],
        )
        assert result.unmapped_exness == ["AAA", "MMM", "ZZZ"]

    def test_used_set_tracks_across_tiers(self, tmp_path: Path) -> None:
        # FTMO #1 exact-matches `EURUSD`. FTMO #2 is `ALT` whose hint
        # candidate is also `EURUSD`. The used-set must prevent re-claim.
        path = tmp_path / "h.json"
        _write_hints_file(
            path,
            [{"ftmo": "ALT", "exness_candidates": ["EURUSD"], "note": ""}],
        )
        engine = AutoMatchEngine(path)
        result = engine.match(
            [_ftmo("EURUSD"), _ftmo("ALT")],
            [_raw("EURUSD")],
        )
        assert len(result.proposals) == 1
        assert result.proposals[0].ftmo == "EURUSD"
        assert "ALT" in result.unmapped_ftmo

    def test_proposal_is_immutable(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        from dataclasses import FrozenInstanceError

        result = engine_no_hints.match([_ftmo("EURUSD")], [_raw("EURUSD")])
        with pytest.raises(FrozenInstanceError):
            result.proposals[0].exness = "GBPUSD"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# §2.7.8 — Integration with real bootstrap config + real whitelist
# ---------------------------------------------------------------------------


class TestIntegrationRealConfig:
    @pytest.fixture
    def real_engine(self) -> AutoMatchEngine:
        if not REAL_HINTS_PATH.is_file():
            pytest.skip(f"missing {REAL_HINTS_PATH}")
        return AutoMatchEngine(REAL_HINTS_PATH)

    @pytest.fixture
    def real_whitelist(self) -> FTMOWhitelistService:
        if not REAL_FTMO_WHITELIST_PATH.is_file():
            pytest.skip(f"missing {REAL_FTMO_WHITELIST_PATH}")
        return FTMOWhitelistService(REAL_FTMO_WHITELIST_PATH)

    def test_real_hints_count_is_14(self, real_engine: AutoMatchEngine) -> None:
        # Bootstrap derived from 14 archived `match_type=manual` entries.
        assert real_engine.hint_count == 14

    def test_real_whitelist_count_is_117(
        self, real_whitelist: FTMOWhitelistService
    ) -> None:
        # Sanity check — Phase 4.A.1 baseline.
        assert real_whitelist.count == 117

    def test_full_synthetic_run_resolves_hint_targets(
        self,
        real_engine: AutoMatchEngine,
        real_whitelist: FTMOWhitelistService,
    ) -> None:
        # Build a synthetic raw Exness list that includes every hint
        # candidate. Tier 1/2/3 should leave zero of the hinted FTMO
        # names in unmapped_ftmo.
        hint_ftmo_names = {
            h.ftmo
            for h in MatchHintsFile.model_validate_json(
                REAL_HINTS_PATH.read_text(encoding="utf-8")
            ).hints
        }
        hint_candidates = [
            c
            for h in MatchHintsFile.model_validate_json(
                REAL_HINTS_PATH.read_text(encoding="utf-8")
            ).hints
            for c in h.exness_candidates
        ]
        raw = [_raw(name) for name in hint_candidates]
        result = real_engine.match(real_whitelist.all_entries(), raw)

        # Every hinted FTMO should be in proposals (not unmapped_ftmo).
        proposal_ftmo = {p.ftmo for p in result.proposals}
        assert hint_ftmo_names.issubset(proposal_ftmo)

    def test_empty_ftmo_list_returns_all_raw_unmapped(
        self, real_engine: AutoMatchEngine
    ) -> None:
        result = real_engine.match([], [_raw("AAA"), _raw("BBB")])
        assert result.proposals == []
        assert result.unmapped_ftmo == []
        assert result.unmapped_exness == ["AAA", "BBB"]

    def test_empty_raw_list_returns_all_ftmo_unmapped(
        self,
        real_engine: AutoMatchEngine,
        real_whitelist: FTMOWhitelistService,
    ) -> None:
        result = real_engine.match(real_whitelist.all_entries(), [])
        assert result.proposals == []
        assert len(result.unmapped_ftmo) == real_whitelist.count
        assert result.unmapped_exness == []

    def test_proposals_returned_as_proposal_dataclass(
        self, engine_no_hints: AutoMatchEngine
    ) -> None:
        result = engine_no_hints.match([_ftmo("EURUSD")], [_raw("EURUSD")])
        assert isinstance(result, AutoMatchProposal)
        assert all(isinstance(p, MatchProposal) for p in result.proposals)
