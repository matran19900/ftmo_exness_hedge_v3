"""Auto-match engine for the symbol-mapping wizard.

Three-tier matching algorithm per ``docs/phase-4-symbol-mapping-design.md`` §6,
with the Phase 4.2a bilateral-normalize refinement layered on top:

  Tier 1 — ``exact``        : FTMO name == Exness name (case-sensitive)            → high confidence
  Tier 2 — ``suffix_strip`` : strip a known broker suffix from BOTH sides           → medium confidence
                              (FTMO ``.cash`` + Exness ``m`` / ``c`` /
                              ``_premium`` / …)
  Tier 3 — ``manual_hint``  : ``server/config/symbol_match_hints.json``            → low  confidence
                              (hint ``ftmo`` + ``exness_candidates`` are also
                              normalized so a single hint covers every broker
                              variant — Standard / Cent / Pro / Raw)

Pure function design:

  - No Redis interaction.
  - No file writes (only reads ``hints_file_path`` at construction / on
    explicit ``load_hints()``).
  - No API surface, no FastAPI imports.
  - ``match()`` produces an ``AutoMatchProposal`` and mutates nothing on
    the engine instance — safe for concurrent invocation.

Wired into the wizard at step 4.A.4
(``POST /api/accounts/exness/{}/symbol-mapping/auto-match``).
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

    # Phase 4.2a — FTMO branding suffixes. ``.cash`` shows up on FTMO's
    # CFD product family (``GER40.cash``, ``NATGAS.cash``, …). Stripped
    # from the FTMO side during Tier 2 + Tier 3 so a single mapping rule
    # works regardless of whether the broker uses ``DE30m`` or ``DE30``.
    FTMO_SUFFIX_PATTERNS: tuple[str, ...] = (".cash",)

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

    @staticmethod
    def _normalize(name: str, suffix_patterns: tuple[str, ...]) -> str:
        """Strip a single matching suffix from ``name``, longest-first.

        Returns the plain name when no suffix matches (Tier 2's existing
        ``EURUSD == EURUSD`` short-circuit still works because both sides
        normalise to the same plain string).

        Single-suffix only — the helper is non-recursive on purpose.
        ``EURUSDm.cash`` (hypothetical) would strip ``.cash`` once and
        stop, leaving ``EURUSDm``; the operator would have to add a hint
        for the residual mismatch.
        """
        for sfx in suffix_patterns:
            if name.endswith(sfx) and len(name) > len(sfx):
                return name[: -len(sfx)]
        return name

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

            # Tier 2: bilateral suffix_strip (Phase 4.2a) — strip both the
            # FTMO ``.cash`` and any Exness account-flavour suffix, then
            # compare the normalised names. Iteration is sorted so the
            # picked Exness candidate is deterministic when multiple raw
            # names share a normalised stem.
            if matched is None:
                ftmo_normalized = self._normalize(
                    ftmo_name, self.FTMO_SUFFIX_PATTERNS
                )
                for exness_name in sorted(exness_name_set):
                    if exness_name in used_exness:
                        continue
                    if exness_name == ftmo_name:
                        # Tier 1 owns identical names — defensive skip so
                        # we don't double-count when Tier 1 has already
                        # rejected the pair (e.g. used_exness is set).
                        continue
                    exness_normalized = self._normalize(
                        exness_name, self.EXNESS_SUFFIX_PATTERNS
                    )
                    if ftmo_normalized == exness_normalized:
                        matched = MatchProposal(
                            ftmo=ftmo_name,
                            exness=exness_name,
                            match_type="suffix_strip",
                            confidence="medium",
                        )
                        break

            # Tier 3: manual_hint with normalized lookup (Phase 4.2a). A
            # single hint like ``{ftmo: "GER40", candidates: ["DE30"]}``
            # now matches every broker variant: ``GER40.cash`` against
            # ``DE30m`` / ``DE30c`` / ``DE30_premium`` / plain ``DE30``.
            # Hints in the config are stored in their broker-agnostic
            # plain form (no ``.cash`` / no Exness suffix).
            if matched is None:
                ftmo_normalized = self._normalize(
                    ftmo_name, self.FTMO_SUFFIX_PATTERNS
                )
                for hint in self._hints:
                    if (
                        self._normalize(hint.ftmo, self.FTMO_SUFFIX_PATTERNS)
                        != ftmo_normalized
                    ):
                        continue
                    for candidate in hint.exness_candidates:
                        candidate_normalized = self._normalize(
                            candidate, self.EXNESS_SUFFIX_PATTERNS
                        )
                        for exness_name in sorted(exness_name_set):
                            if exness_name in used_exness:
                                continue
                            exness_normalized = self._normalize(
                                exness_name, self.EXNESS_SUFFIX_PATTERNS
                            )
                            if exness_normalized == candidate_normalized:
                                matched = MatchProposal(
                                    ftmo=ftmo_name,
                                    exness=exness_name,
                                    match_type="manual_hint",
                                    confidence="low",
                                )
                                break
                        if matched is not None:
                            break
                    if matched is not None:
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
