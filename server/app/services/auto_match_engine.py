"""Auto-match engine for the symbol-mapping wizard.

Three-tier matching algorithm per ``docs/phase-4-symbol-mapping-design.md`` §6:

  Tier 1 — ``exact``        : FTMO name == Exness name (case-sensitive)        → high confidence
  Tier 2 — ``suffix_strip`` : strip a known broker suffix from the Exness name → medium confidence
  Tier 3 — ``manual_hint``  : look up ``server/config/symbol_match_hints.json`` → low  confidence

Pure function design:

  - No Redis interaction.
  - No file writes (only reads ``hints_file_path`` at construction / on
    explicit ``load_hints()``).
  - No API surface, no FastAPI imports.
  - ``match()`` produces an ``AutoMatchProposal`` and mutates nothing on
    the engine instance — safe for concurrent invocation.

Step 4.A.4 will wire the engine into
``POST /api/accounts/exness/{}/symbol-mapping/auto-match``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .ftmo_whitelist_service import FTMOSymbol
from .mapping_cache_schemas import RawSymbolEntry
from .match_hints_schemas import MatchHint, MatchHintsFile

logger = logging.getLogger(__name__)


MatchType = Literal["exact", "suffix_strip", "manual_hint"]
Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class MatchProposal:
    """One proposed mapping for the wizard."""

    ftmo: str
    exness: str
    match_type: MatchType
    confidence: Confidence


@dataclass
class AutoMatchProposal:
    """Result of ``AutoMatchEngine.match()``.

    ``proposals`` lists every FTMO symbol that the engine could map.
    ``unmapped_ftmo`` lists FTMO names with no proposal — the wizard will
    surface these for manual confirmation. ``unmapped_exness`` lists
    Exness names that no proposal claimed (sorted, de-duplicated) — useful
    diagnostic for the operator and for the spec-snapshot view.
    """

    proposals: list[MatchProposal] = field(default_factory=list)
    unmapped_ftmo: list[str] = field(default_factory=list)
    unmapped_exness: list[str] = field(default_factory=list)


class AutoMatchEngine:
    """3-tier symbol matching engine.

    Public API:
      - ``match(ftmo_symbols, raw_exness_symbols)`` → ``AutoMatchProposal``
      - ``load_hints()``                            → reload hints from disk
      - ``hint_count``                              → property, hint count

    Tier-2 suffix patterns are listed *longest first* so a name like
    ``EURUSDz_premium`` strips ``_premium`` rather than the trailing ``m``.
    Suffix semantics mirror the archived Phase 1-3 ``MANUAL_EXNESS`` set
    (CTO Phase 4 Q1 fallback D-4.A.0-10).
    """

    EXNESS_SUFFIX_PATTERNS: tuple[str, ...] = (
        "_premium",
        "_raw",
        ".cash",
        "_i",
        "m",
        "c",
        "z",
    )

    def __init__(self, hints_file_path: str | Path) -> None:
        self._hints_path = Path(hints_file_path)
        self._hints: list[MatchHint] = []
        self.load_hints()

    def load_hints(self) -> None:
        """(Re)load hints from disk. Missing file → empty list + WARNING log
        (not an error: the hints file is optional, and tier-1 + tier-2 still
        work). Schema-drifted file → ``ValidationError`` propagates."""
        if not self._hints_path.is_file():
            logger.warning(
                "auto_match_engine.hints_file_missing path=%s", self._hints_path
            )
            self._hints = []
            return
        raw = self._hints_path.read_text(encoding="utf-8")
        parsed = MatchHintsFile.model_validate_json(raw)
        self._hints = parsed.hints
        logger.info(
            "auto_match_engine.hints_loaded path=%s count=%d",
            self._hints_path,
            len(self._hints),
        )

    @property
    def hint_count(self) -> int:
        return len(self._hints)

    @property
    def hints_path(self) -> Path:
        return self._hints_path

    def match(
        self,
        ftmo_symbols: list[FTMOSymbol],
        raw_exness_symbols: list[RawSymbolEntry],
    ) -> AutoMatchProposal:
        """Run the 3-tier algorithm.

        Each Exness name can be claimed by **at most one** FTMO symbol per
        call — once tier-1 matches ``EURUSD`` to itself, no later FTMO
        symbol can take ``EURUSD`` via a hint. FTMO symbols are processed
        in input order so tie-breaking is deterministic.

        Suffix candidates shorter than the suffix length are skipped to
        avoid empty-prefix degenerate matches (so a 1-char raw name
        ``X`` does not silently match an FTMO symbol ``""`` after
        ``"X".endswith("X")[:-1]``).
        """
        proposals: list[MatchProposal] = []
        unmapped_ftmo: list[str] = []
        exness_name_set = {s.name for s in raw_exness_symbols}
        used_exness: set[str] = set()
        hints_by_ftmo: dict[str, MatchHint] = {h.ftmo: h for h in self._hints}

        for ftmo_sym in ftmo_symbols:
            ftmo_name = ftmo_sym.name
            matched: MatchProposal | None = None

            # Tier 1: exact (case-sensitive)
            if ftmo_name in exness_name_set and ftmo_name not in used_exness:
                matched = MatchProposal(
                    ftmo=ftmo_name,
                    exness=ftmo_name,
                    match_type="exact",
                    confidence="high",
                )

            # Tier 2: suffix_strip — iterate suffixes (longest first) then
            # candidate names. This ordering means the suffix list dominates
            # over alphabetical broker-name iteration and is reproducible.
            if matched is None:
                for suffix in self.EXNESS_SUFFIX_PATTERNS:
                    suffix_len = len(suffix)
                    candidate_exness: str | None = None
                    for exness_name in sorted(exness_name_set):
                        if exness_name in used_exness:
                            continue
                        if len(exness_name) <= suffix_len:
                            continue
                        if not exness_name.endswith(suffix):
                            continue
                        if exness_name[:-suffix_len] == ftmo_name:
                            candidate_exness = exness_name
                            break
                    if candidate_exness is not None:
                        matched = MatchProposal(
                            ftmo=ftmo_name,
                            exness=candidate_exness,
                            match_type="suffix_strip",
                            confidence="medium",
                        )
                        break

            # Tier 3: manual_hint
            if matched is None and ftmo_name in hints_by_ftmo:
                hint = hints_by_ftmo[ftmo_name]
                for candidate in hint.exness_candidates:
                    if candidate in exness_name_set and candidate not in used_exness:
                        matched = MatchProposal(
                            ftmo=ftmo_name,
                            exness=candidate,
                            match_type="manual_hint",
                            confidence="low",
                        )
                        break

            if matched is not None:
                proposals.append(matched)
                used_exness.add(matched.exness)
            else:
                unmapped_ftmo.append(ftmo_name)

        unmapped_exness = sorted(exness_name_set - used_exness)

        return AutoMatchProposal(
            proposals=proposals,
            unmapped_ftmo=unmapped_ftmo,
            unmapped_exness=unmapped_exness,
        )
