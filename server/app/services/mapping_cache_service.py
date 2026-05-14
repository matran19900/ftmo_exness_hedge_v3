"""Mapping cache service — orchestrates repository + engine + Redis (Phase 4.A.4).

Owns the symbol-mapping wizard's full lifecycle:

  - Compute signature → look up cache (hit / fuzzy_match / miss).
  - Run auto-match against an Exness raw snapshot.
  - Validate spec divergence between raw snapshot and an existing cache.
  - Save / edit a confirmed mapping (atomic file write + Redis populate +
    ``account_to_mapping`` pointer + ``exness_raw_symbols`` cleanup +
    ``mapping_status`` broadcast).
  - Re-populate Redis from disk at server startup (file is source of truth
    per D-SM-07).

Public API per ``docs/phase-4-symbol-mapping-design.md`` §2.4 + §4 + §5 +
§6 + §7. The class is async because every Redis call is async; the
repository / engine / whitelist dependencies are sync.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from redis import asyncio as redis_asyncio

from .auto_match_engine import AutoMatchEngine, AutoMatchProposal
from .broadcast import BroadcastService
from .ftmo_whitelist_service import FTMOWhitelistService
from .mapping_cache_repository import MappingCacheRepository, compute_signature
from .mapping_cache_schemas import (
    MappingEntry,
    RawSymbolEntry,
    SymbolMappingCacheFile,
)

logger = logging.getLogger(__name__)


MappingStatusValue = Literal[
    "pending_mapping", "active", "spec_mismatch", "disconnected"
]
LookupOutcome = Literal["hit", "fuzzy_match", "miss"]
DecisionAction = Literal["accept", "override", "skip"]
DivergenceSeverity = Literal["BLOCK", "WARN"]


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MappingDecision:
    """One CEO decision for a single FTMO symbol during save / edit.

    ``action == "accept"`` keeps whatever the engine proposed (or whatever
    the existing cache said in diff-aware mode). ``"override"`` requires
    ``exness_override`` and replaces the proposal. ``"skip"`` excludes
    the FTMO symbol from the new cache entirely.
    """

    ftmo: str
    action: DecisionAction
    exness_override: str | None = None


@dataclass
class FuzzyMatchResult:
    cache: SymbolMappingCacheFile
    score: float


@dataclass
class SignatureLookupResult:
    """Result of ``lookup_signature`` per design §2.4."""

    signature: str
    outcome: LookupOutcome
    matched_cache: SymbolMappingCacheFile | None = None
    fuzzy_candidate: SymbolMappingCacheFile | None = None
    fuzzy_score: float = 0.0


@dataclass
class SpecDivergence:
    """A single field-level mismatch between raw snapshot and cached mapping."""

    symbol: str
    field: str
    cached_value: float | int | str
    raw_value: float | int | str
    severity: DivergenceSeverity
    delta_percent: float | None = None


@dataclass
class SaveResult:
    signature: str
    cache_filename: str
    created_new_cache: bool
    mapping_count: int


@dataclass
class MappingStatusSnapshot:
    """Cheap read-only view of an account's mapping state."""

    account_id: str
    status: MappingStatusValue
    signature: str | None = None
    cache_filename: str | None = None


# ---------------------------------------------------------------------------
# Constants — locked design thresholds
# ---------------------------------------------------------------------------

JACCARD_FUZZY_THRESHOLD: float = 0.95   # design §4 — 95% set overlap
PIP_SIZE_TOLERANCE_PCT: float = 5.0     # design §5
PIP_VALUE_TOLERANCE_PCT: float = 10.0   # design §5

MAPPING_STATUS_CHANNEL_PREFIX = "mapping_status:"


# ---------------------------------------------------------------------------
# Redis key builders — kept module-private to centralise the layout
# ---------------------------------------------------------------------------


def _key_mapping_cache(signature: str) -> str:
    return f"mapping_cache:{signature}"


def _key_account_to_mapping(account_id: str) -> str:
    return f"account_to_mapping:{account_id}"


def _key_exness_raw_symbols(account_id: str) -> str:
    return f"exness_raw_symbols:{account_id}"


def _key_mapping_status(account_id: str) -> str:
    return f"mapping_status:{account_id}"


def _channel_mapping_status(account_id: str) -> str:
    return f"{MAPPING_STATUS_CHANNEL_PREFIX}{account_id}"


# ---------------------------------------------------------------------------
# MappingCacheService
# ---------------------------------------------------------------------------


class MappingCacheService:
    """Orchestrates repository + engine + Redis for the wizard.

    Public API (11 entries):
      - ``lookup_signature(raw_symbols)``                 → SignatureLookupResult
      - ``run_auto_match(raw_symbols)``                   → AutoMatchProposal
      - ``save_mapping(account_id, raw, decisions)``      → SaveResult
      - ``edit_mapping(account_id, raw, decisions)``      → SaveResult
      - ``get_mapping_status(account_id)``                → MappingStatusSnapshot
      - ``set_mapping_status(account_id, status, ...)``   → None  (also broadcasts)
      - ``get_account_mapping(account_id)``               → SymbolMappingCacheFile | None
      - ``get_raw_snapshot(account_id)``                  → list[RawSymbolEntry] | None
      - ``validate_spec_divergence(raw, cache)``          → list[SpecDivergence]
      - ``populate_redis_from_disk()``                    → int
      - ``jaccard_fuzzy_match(raw, caches)``              → FuzzyMatchResult | None
    """

    def __init__(
        self,
        repository: MappingCacheRepository,
        engine: AutoMatchEngine,
        ftmo_whitelist: FTMOWhitelistService,
        redis: redis_asyncio.Redis,
        broadcast: BroadcastService,
    ) -> None:
        self._repository = repository
        self._engine = engine
        self._ftmo_whitelist = ftmo_whitelist
        self._redis = redis
        self._broadcast = broadcast

    # ----- Lookup -----

    async def lookup_signature(
        self, raw_symbols: list[RawSymbolEntry]
    ) -> SignatureLookupResult:
        """Compute signature + classify against existing caches.

        Order:
          1. Exact-signature hit (Redis ``mapping_cache:{sig}`` HASH check).
          2. Jaccard fuzzy match against all on-disk caches (≥0.95).
          3. Miss.

        The Redis check is cheap (HEXISTS); the fuzzy path reads from
        repository (file-system) — only walked when no exact hit.
        """
        signature = compute_signature(raw_symbols)
        if await self._redis.exists(_key_mapping_cache(signature)):
            cache = await self._repository.read(signature)
            return SignatureLookupResult(
                signature=signature,
                outcome="hit",
                matched_cache=cache,
            )
        existing = await self._repository.list_all()
        fuzzy = self.jaccard_fuzzy_match(raw_symbols, existing)
        if fuzzy is not None:
            return SignatureLookupResult(
                signature=signature,
                outcome="fuzzy_match",
                fuzzy_candidate=fuzzy.cache,
                fuzzy_score=fuzzy.score,
            )
        return SignatureLookupResult(signature=signature, outcome="miss")

    def jaccard_fuzzy_match(
        self,
        raw_symbols: list[RawSymbolEntry],
        existing_caches: list[SymbolMappingCacheFile],
    ) -> FuzzyMatchResult | None:
        """Return the cache with the highest Jaccard score over symbol names,
        but only if that score reaches ``JACCARD_FUZZY_THRESHOLD`` (0.95).

        Empty raw set → no fuzzy candidate (returns ``None``); we never want
        to suggest "you have nothing in common with this 100-symbol cache".
        Tied scores: deterministic by iteration order over ``existing_caches``.
        """
        raw_names = {s.name for s in raw_symbols}
        if not raw_names:
            return None
        best_score = 0.0
        best_cache: SymbolMappingCacheFile | None = None
        for cache in existing_caches:
            cached_names = {s.name for s in cache.raw_symbols_snapshot}
            union = raw_names | cached_names
            if not union:
                continue
            intersect = raw_names & cached_names
            score = len(intersect) / len(union)
            if score > best_score:
                best_score = score
                best_cache = cache
        if best_cache is not None and best_score >= JACCARD_FUZZY_THRESHOLD:
            return FuzzyMatchResult(cache=best_cache, score=best_score)
        return None

    def run_auto_match(
        self, raw_symbols: list[RawSymbolEntry]
    ) -> AutoMatchProposal:
        """Run the engine over the full FTMO whitelist + raw Exness snapshot."""
        return self._engine.match(self._ftmo_whitelist.all_entries(), raw_symbols)

    # ----- Spec divergence (design §5) -----

    def validate_spec_divergence(
        self,
        raw_symbols: list[RawSymbolEntry],
        cache: SymbolMappingCacheFile,
    ) -> list[SpecDivergence]:
        """Compare raw snapshot to a cache's mappings + raw_symbols_snapshot.

        Per §5 the rules are:
          - ``contract_size``     EXACT match → ``BLOCK``
          - ``digits``            EXACT match (vs raw_symbols_snapshot) → ``BLOCK``
          - ``currency_profit``   EXACT match (vs cache.mapping.quote_ccy) → ``BLOCK``
          - ``pip_size``          ±5%  → ``WARN`` (otherwise no event)
          - ``pip_value``         ±10% (raw value derived as ``contract_size × pip_size``
                                  per Phase 4.A simplification — see deviation note) → ``WARN``
          - ``volume_step/min/max`` always-latest from raw → ``WARN`` if delta

        A symbol that has been removed from the broker (mapping refers to an
        Exness name no longer in the raw snapshot) is reported as a single
        ``BLOCK`` divergence with ``field="symbol_missing"``.
        """
        raw_by_name: dict[str, RawSymbolEntry] = {s.name: s for s in raw_symbols}
        snap_by_name: dict[str, RawSymbolEntry] = {
            s.name: s for s in cache.raw_symbols_snapshot
        }
        divergences: list[SpecDivergence] = []

        for mapping in cache.mappings:
            raw = raw_by_name.get(mapping.exness)
            if raw is None:
                divergences.append(
                    SpecDivergence(
                        symbol=mapping.ftmo,
                        field="symbol_missing",
                        cached_value=mapping.exness,
                        raw_value="",
                        severity="BLOCK",
                    )
                )
                continue

            # contract_size BLOCK
            if raw.contract_size != mapping.contract_size:
                divergences.append(
                    SpecDivergence(
                        symbol=mapping.ftmo,
                        field="contract_size",
                        cached_value=mapping.contract_size,
                        raw_value=raw.contract_size,
                        severity="BLOCK",
                    )
                )

            # digits BLOCK — compared against the snapshot taken at confirm time.
            snap = snap_by_name.get(mapping.exness)
            if snap is not None and raw.digits != snap.digits:
                divergences.append(
                    SpecDivergence(
                        symbol=mapping.ftmo,
                        field="digits",
                        cached_value=snap.digits,
                        raw_value=raw.digits,
                        severity="BLOCK",
                    )
                )

            # currency_profit BLOCK
            if raw.currency_profit != mapping.quote_ccy:
                divergences.append(
                    SpecDivergence(
                        symbol=mapping.ftmo,
                        field="currency_profit",
                        cached_value=mapping.quote_ccy,
                        raw_value=raw.currency_profit,
                        severity="BLOCK",
                    )
                )

            # pip_size ±5% WARN
            if mapping.pip_size > 0:
                pct = abs(raw.pip_size - mapping.pip_size) / mapping.pip_size * 100.0
                if pct > PIP_SIZE_TOLERANCE_PCT:
                    divergences.append(
                        SpecDivergence(
                            symbol=mapping.ftmo,
                            field="pip_size",
                            cached_value=mapping.pip_size,
                            raw_value=raw.pip_size,
                            severity="WARN",
                            delta_percent=pct,
                        )
                    )

            # pip_value ±10% WARN — derived from raw spec (Phase 4.A
            # simplification: contract_size × pip_size; see D-4.A.4-2).
            if mapping.pip_value > 0:
                derived = raw.contract_size * raw.pip_size
                pct = abs(derived - mapping.pip_value) / mapping.pip_value * 100.0
                if pct > PIP_VALUE_TOLERANCE_PCT:
                    divergences.append(
                        SpecDivergence(
                            symbol=mapping.ftmo,
                            field="pip_value",
                            cached_value=mapping.pip_value,
                            raw_value=derived,
                            severity="WARN",
                            delta_percent=pct,
                        )
                    )

            # volume_* WARN if any delta (always-latest semantics)
            for field_name, cached_v, raw_v in (
                ("volume_step", mapping.exness_volume_step, raw.volume_step),
                ("volume_min", mapping.exness_volume_min, raw.volume_min),
                ("volume_max", mapping.exness_volume_max, raw.volume_max),
            ):
                if cached_v != raw_v:
                    divergences.append(
                        SpecDivergence(
                            symbol=mapping.ftmo,
                            field=field_name,
                            cached_value=cached_v,
                            raw_value=raw_v,
                            severity="WARN",
                        )
                    )

        return divergences

    # ----- Mapping construction -----

    def _build_mapping_entry(
        self,
        decision: MappingDecision,
        raw_by_name: dict[str, RawSymbolEntry],
        proposed_match_type: str,
    ) -> MappingEntry | None:
        """Translate a CEO decision into a ``MappingEntry`` ready for the cache.

        ``skip`` decisions return ``None`` (excluded from the cache). For
        ``accept`` / ``override`` we look up the chosen Exness name in the
        raw map, copy specs into the entry, and derive ``pip_value`` as
        ``contract_size × pip_size`` (Phase 4.A simplification per design
        §6.3 — full pip_value derivation lives in step 4.A.5).
        """
        if decision.action == "skip":
            return None
        # Both ``accept`` and ``override`` need ``exness_override``; for
        # ``accept`` the caller (``save_mapping``) populates it from the
        # engine proposal before we get here. ``override`` is filled by the
        # CEO via the API request body.
        exness_name = decision.exness_override
        if exness_name is None:
            raise ValueError(
                f"{decision.action} decision for {decision.ftmo} missing exness_override"
            )
        raw = raw_by_name.get(exness_name)
        if raw is None:
            raise ValueError(
                f"decision for {decision.ftmo} references unknown Exness "
                f"symbol '{exness_name}'"
            )
        match_type = (
            proposed_match_type if decision.action == "accept" else "override"
        )
        return MappingEntry(
            ftmo=decision.ftmo,
            exness=exness_name,
            match_type=match_type,
            contract_size=raw.contract_size,
            pip_size=raw.pip_size,
            pip_value=raw.contract_size * raw.pip_size,
            quote_ccy=raw.currency_profit,
            exness_volume_step=raw.volume_step,
            exness_volume_min=raw.volume_min,
            exness_volume_max=raw.volume_max,
        )

    # ----- Save / edit -----

    async def save_mapping(
        self,
        account_id: str,
        raw_symbols: list[RawSymbolEntry],
        decisions: list[MappingDecision],
    ) -> SaveResult:
        """Persist a confirmed mapping for ``account_id``.

        See class docstring for the full lifecycle. Returns a ``SaveResult``
        whose ``created_new_cache`` discriminates "wrote a new file" from
        "linked to an existing file" — used by the API layer to choose 201
        vs 200.
        """
        signature = compute_signature(raw_symbols)
        existing = await self._repository.read(signature)

        # The auto-match engine result is needed only for proposed match_type
        # labels on accept-decisions; we re-run rather than thread it through
        # the request so the API surface stays narrow.
        proposal = self.run_auto_match(raw_symbols)
        proposed_type_by_ftmo = {p.ftmo: p.match_type for p in proposal.proposals}
        raw_by_name = {s.name: s for s in raw_symbols}

        if existing is not None:
            # Signature hit: link the account to the existing cache, no new file.
            if account_id not in existing.used_by_accounts:
                existing.used_by_accounts.append(account_id)
                await self._repository.write(existing)
            cache = existing
            created_new = False
        else:
            mappings: list[MappingEntry] = []
            for decision in decisions:
                # On accept, default exness_override to whatever the engine
                # proposed for this FTMO symbol (the API layer also does
                # this, but defending the service against direct callers).
                if (
                    decision.action == "accept"
                    and decision.exness_override is None
                ):
                    proposal_match = next(
                        (
                            p
                            for p in proposal.proposals
                            if p.ftmo == decision.ftmo
                        ),
                        None,
                    )
                    if proposal_match is None:
                        raise ValueError(
                            f"accept decision for {decision.ftmo} has no "
                            "engine proposal to fall back on"
                        )
                    decision = MappingDecision(
                        ftmo=decision.ftmo,
                        action="accept",
                        exness_override=proposal_match.exness,
                    )
                entry = self._build_mapping_entry(
                    decision,
                    raw_by_name,
                    proposed_type_by_ftmo.get(decision.ftmo, "override"),
                )
                if entry is not None:
                    mappings.append(entry)

            now = datetime.now(UTC)
            cache = SymbolMappingCacheFile(
                schema_version=1,
                signature=signature,
                created_at=now,
                updated_at=now,
                created_by_account=account_id,
                used_by_accounts=[account_id],
                raw_symbols_snapshot=list(raw_symbols),
                mappings=mappings,
            )
            await self._repository.write(cache)
            created_new = True

        # Redis populate + pointer + cleanup + status broadcast.
        await self._populate_cache_into_redis(cache)
        await self._redis.set(
            _key_account_to_mapping(account_id), cache.signature
        )
        await self._redis.delete(_key_exness_raw_symbols(account_id))
        await self.set_mapping_status(
            account_id,
            "active",
            signature=cache.signature,
            cache_filename=self._cache_filename(cache),
        )

        return SaveResult(
            signature=cache.signature,
            cache_filename=self._cache_filename(cache),
            created_new_cache=created_new,
            mapping_count=len(cache.mappings),
        )

    async def edit_mapping(
        self,
        account_id: str,
        decisions: list[MappingDecision],
    ) -> SaveResult:
        """Edit a saved mapping in-place against the previous broker snapshot.

        Phase 4.A.4 semantics (D-4.A.4-4):
          - Raw symbols come from the previous cache's snapshot, never from
            Redis. Edit operates on the same broker view that produced the
            original mapping.
          - The new mappings overwrite the cache file's ``mappings`` list.
            Other accounts that share the cache effectively adopt the new
            mappings — Phase 4.A.4 explicit limitation; the orphan-sweep
            tooling that supports fork-on-shared-edit lives in Phase 5.
          - The old mappings are preserved on disk via the atomic-write
            ``.bak`` from ``MappingCacheRepository`` (step 4.A.2).
          - Status broadcasts ``active`` after the rewrite.

        Raises ``ValueError`` if no current mapping pointer exists, or
        the pointer references a missing cache file.
        """
        prev_signature = await self._redis.get(_key_account_to_mapping(account_id))
        if prev_signature is None:
            raise ValueError(
                f"account {account_id} has no current mapping to edit"
            )
        prev_cache = await self._repository.read(prev_signature)
        if prev_cache is None:
            raise ValueError(
                f"account {account_id} pointer references missing cache "
                f"{prev_signature}"
            )

        raw = list(prev_cache.raw_symbols_snapshot)
        proposal = self.run_auto_match(raw)
        proposed_type_by_ftmo = {p.ftmo: p.match_type for p in proposal.proposals}
        raw_by_name = {s.name: s for s in raw}

        new_mappings: list[MappingEntry] = []
        for decision in decisions:
            if decision.action == "accept" and decision.exness_override is None:
                proposal_match = next(
                    (p for p in proposal.proposals if p.ftmo == decision.ftmo),
                    None,
                )
                if proposal_match is None:
                    raise ValueError(
                        f"accept decision for {decision.ftmo} has no engine "
                        "proposal to fall back on"
                    )
                decision = MappingDecision(
                    ftmo=decision.ftmo,
                    action="accept",
                    exness_override=proposal_match.exness,
                )
            entry = self._build_mapping_entry(
                decision,
                raw_by_name,
                proposed_type_by_ftmo.get(decision.ftmo, "override"),
            )
            if entry is not None:
                new_mappings.append(entry)

        # Rewrite the cache file with the new mappings list. The repository's
        # atomic-write step copies the prior content to ``.bak`` so the
        # previous mapping decisions remain recoverable on disk.
        prev_cache.mappings = new_mappings
        await self._repository.write(prev_cache)
        await self._populate_cache_into_redis(prev_cache)

        await self.set_mapping_status(
            account_id,
            "active",
            signature=prev_cache.signature,
            cache_filename=self._cache_filename(prev_cache),
        )

        return SaveResult(
            signature=prev_cache.signature,
            cache_filename=self._cache_filename(prev_cache),
            created_new_cache=False,
            mapping_count=len(new_mappings),
        )

    # ----- Status -----

    async def get_mapping_status(
        self, account_id: str
    ) -> MappingStatusSnapshot:
        """Return the live status snapshot. Default to ``pending_mapping``
        when no key has ever been written — covers the fresh-account case."""
        status: MappingStatusValue
        raw_status = await self._redis.get(_key_mapping_status(account_id))
        if raw_status is None:
            status = "pending_mapping"
        else:
            status = self._parse_status(raw_status)
        signature = await self._redis.get(_key_account_to_mapping(account_id))
        cache_filename: str | None = None
        if signature is not None:
            cache = await self._repository.read(signature)
            if cache is not None:
                cache_filename = self._cache_filename(cache)
        return MappingStatusSnapshot(
            account_id=account_id,
            status=status,
            signature=signature,
            cache_filename=cache_filename,
        )

    async def set_mapping_status(
        self,
        account_id: str,
        status: MappingStatusValue,
        signature: str | None = None,
        cache_filename: str | None = None,
    ) -> None:
        """Persist the status to Redis and broadcast on the WS channel.

        Frontend (step 4.A.6) subscribes to ``mapping_status:{account_id}``
        for live wizard updates."""
        await self._redis.set(_key_mapping_status(account_id), status)
        await self._broadcast.publish(
            _channel_mapping_status(account_id),
            {
                "type": "status_changed",
                "account_id": account_id,
                "status": status,
                "signature": signature,
                "cache_filename": cache_filename,
            },
        )

    # ----- Convenience reads -----

    async def get_account_mapping(
        self, account_id: str
    ) -> SymbolMappingCacheFile | None:
        signature = await self._redis.get(_key_account_to_mapping(account_id))
        if signature is None:
            return None
        return await self._repository.read(signature)

    async def get_raw_snapshot(
        self, account_id: str
    ) -> list[RawSymbolEntry] | None:
        """Read the ephemeral ``exness_raw_symbols:{account_id}`` JSON STRING."""
        raw = await self._redis.get(_key_exness_raw_symbols(account_id))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.error(
                "mapping_cache.raw_snapshot_corrupt account_id=%s", account_id
            )
            return None
        if not isinstance(payload, list):
            logger.error(
                "mapping_cache.raw_snapshot_not_list account_id=%s", account_id
            )
            return None
        return [RawSymbolEntry.model_validate(item) for item in payload]

    # ----- Startup populate -----

    async def populate_redis_from_disk(self) -> int:
        """Server startup: write every cache file's content into Redis.

        Per D-SM-07 the file is the source of truth; Redis state is rebuilt
        from disk at every boot. This deletes stale ``mapping_cache:{sig}``
        Redis hashes that no longer correspond to a file (orphan detection)
        before writing the fresh batch.
        """
        # Orphan detection — read every existing mapping_cache:* key, drop
        # any whose signature has no on-disk file.
        on_disk = await self._repository.list_all()
        on_disk_sigs = {c.signature for c in on_disk}
        deleted = 0
        async for redis_key in self._redis.scan_iter("mapping_cache:*"):
            key = (
                redis_key.decode()
                if isinstance(redis_key, bytes)
                else str(redis_key)
            )
            sig = key.removeprefix("mapping_cache:")
            if sig not in on_disk_sigs:
                await self._redis.delete(key)
                deleted += 1
                logger.warning(
                    "mapping_cache.orphan_redis_key_deleted signature=%s", sig
                )

        # Fresh write of every on-disk cache + account pointer rebuild.
        for cache in on_disk:
            await self._populate_cache_into_redis(cache)
            for account_id in cache.used_by_accounts:
                await self._redis.set(
                    _key_account_to_mapping(account_id), cache.signature
                )
        if deleted:
            logger.info(
                "mapping_cache.populate_redis_from_disk loaded=%d orphans_dropped=%d",
                len(on_disk),
                deleted,
            )
        return len(on_disk)

    # ----- Internal helpers -----

    async def _populate_cache_into_redis(
        self, cache: SymbolMappingCacheFile
    ) -> None:
        """Write a single cache into ``mapping_cache:{sig}`` HASH."""
        payload: dict[str, Any] = {
            "signature": cache.signature,
            "schema_version": str(cache.schema_version),
            "created_at": cache.created_at.isoformat(),
            "updated_at": cache.updated_at.isoformat(),
            "created_by_account": cache.created_by_account,
            "used_by_accounts": json.dumps(cache.used_by_accounts),
            "raw_symbols_snapshot": json.dumps(
                [s.model_dump(mode="json") for s in cache.raw_symbols_snapshot]
            ),
            "mappings": json.dumps(
                [m.model_dump(mode="json") for m in cache.mappings]
            ),
        }
        await self._redis.hset(  # type: ignore[misc]
            _key_mapping_cache(cache.signature), mapping=payload
        )

    def _cache_filename(self, cache: SymbolMappingCacheFile) -> str:
        return f"{cache.created_by_account}_{cache.signature}.json"

    def _parse_status(self, raw: Any) -> MappingStatusValue:
        text = raw.decode() if isinstance(raw, bytes) else str(raw)
        if text in ("pending_mapping", "active", "spec_mismatch", "disconnected"):
            return text  # type: ignore[return-value]
        logger.warning("mapping_cache.unknown_status value=%s", text)
        return "pending_mapping"


# Mark the unused import as deliberately re-exported for typing convenience.
__all__ = [
    "MappingCacheService",
    "MappingDecision",
    "FuzzyMatchResult",
    "SignatureLookupResult",
    "SpecDivergence",
    "SaveResult",
    "MappingStatusSnapshot",
    "JACCARD_FUZZY_THRESHOLD",
    "PIP_SIZE_TOLERANCE_PCT",
    "PIP_VALUE_TOLERANCE_PCT",
    "MAPPING_STATUS_CHANNEL_PREFIX",
]
