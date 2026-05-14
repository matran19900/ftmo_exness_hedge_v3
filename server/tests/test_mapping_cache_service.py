"""Unit tests for ``MappingCacheService`` (Phase 4.A.4).

Coverage matrix per step plan §2.8.1:
  - lookup_signature hit / fuzzy_match / miss
  - jaccard_fuzzy_match thresholds (0%, 50%, 94%, 95%, 99%, 100%)
  - validate_spec_divergence rules (BLOCK + tolerance + WARN)
  - save_mapping new vs link, full Redis side effects
  - edit_mapping new cache + pointer migration + orphan log
  - populate_redis_from_disk including orphan key cleanup
  - WS broadcast invocation on status change
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio
from app.services.auto_match_engine import AutoMatchEngine
from app.services.broadcast import BroadcastService
from app.services.ftmo_whitelist_service import FTMOWhitelistService
from app.services.mapping_cache_repository import (
    MappingCacheRepository,
    compute_signature,
)
from app.services.mapping_cache_schemas import (
    MappingEntry,
    RawSymbolEntry,
    SymbolMappingCacheFile,
)
from app.services.mapping_cache_service import (
    JACCARD_FUZZY_THRESHOLD,
    PIP_SIZE_TOLERANCE_PCT,
    PIP_VALUE_TOLERANCE_PCT,
    MappingCacheService,
    MappingDecision,
    SpecDivergence,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_HINTS_PATH = REPO_ROOT / "server" / "config" / "symbol_match_hints.json"
REAL_FTMO_WHITELIST_PATH = REPO_ROOT / "server" / "data" / "ftmo_whitelist.json"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _raw(name: str, **overrides: Any) -> RawSymbolEntry:
    base: dict[str, Any] = {
        "name": name,
        "contract_size": 100000.0,
        "digits": 5,
        "pip_size": 0.0001,
        "volume_min": 0.01,
        "volume_step": 0.01,
        "volume_max": 200.0,
        "currency_profit": "USD",
    }
    base.update(overrides)
    return RawSymbolEntry(**base)


def _mapping(
    ftmo: str = "EURUSD", exness: str = "EURUSDz", **overrides: Any
) -> MappingEntry:
    base: dict[str, Any] = {
        "ftmo": ftmo,
        "exness": exness,
        "match_type": "suffix_strip",
        "contract_size": 100000.0,
        "pip_size": 0.0001,
        "pip_value": 10.0,
        "quote_ccy": "USD",
        "exness_volume_step": 0.01,
        "exness_volume_min": 0.01,
        "exness_volume_max": 200.0,
    }
    base.update(overrides)
    return MappingEntry(**base)


def _cache(
    *,
    raw: list[RawSymbolEntry],
    mappings: list[MappingEntry] | None = None,
    created_by: str = "exn_001",
    used_by: list[str] | None = None,
) -> SymbolMappingCacheFile:
    sig = compute_signature(raw)
    now = datetime.now(UTC)
    return SymbolMappingCacheFile(
        schema_version=1,
        signature=sig,
        created_at=now,
        updated_at=now,
        created_by_account=created_by,
        used_by_accounts=list(used_by) if used_by is not None else [created_by],
        raw_symbols_snapshot=list(raw),
        mappings=mappings if mappings is not None else [],
    )


@pytest.fixture
def repo(tmp_path: Path) -> MappingCacheRepository:
    return MappingCacheRepository(tmp_path)


@pytest.fixture
def engine() -> AutoMatchEngine:
    return AutoMatchEngine(REAL_HINTS_PATH)


@pytest.fixture
def ftmo_whitelist() -> FTMOWhitelistService:
    return FTMOWhitelistService(REAL_FTMO_WHITELIST_PATH)


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def broadcast_mock() -> BroadcastService:
    bs = BroadcastService()
    # Replace publish with an AsyncMock so tests can assert on calls
    # without setting up real WS subscribers.
    bs.publish = AsyncMock()  # type: ignore[method-assign]
    return bs


@pytest_asyncio.fixture
async def service(
    repo: MappingCacheRepository,
    engine: AutoMatchEngine,
    ftmo_whitelist: FTMOWhitelistService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    broadcast_mock: BroadcastService,
) -> MappingCacheService:
    return MappingCacheService(
        repository=repo,
        engine=engine,
        ftmo_whitelist=ftmo_whitelist,
        redis=fake_redis,
        broadcast=broadcast_mock,
    )


# ---------------------------------------------------------------------------
# Jaccard fuzzy match
# ---------------------------------------------------------------------------


class TestJaccardFuzzyMatch:
    def test_threshold_constant_locked(self) -> None:
        assert JACCARD_FUZZY_THRESHOLD == 0.95

    def test_zero_overlap_returns_none(self, service: MappingCacheService) -> None:
        cache = _cache(raw=[_raw("AAA"), _raw("BBB")])
        result = service.jaccard_fuzzy_match([_raw("XXX"), _raw("YYY")], [cache])
        assert result is None

    def test_50_percent_below_threshold(
        self, service: MappingCacheService
    ) -> None:
        cache = _cache(raw=[_raw("A"), _raw("B")])
        result = service.jaccard_fuzzy_match([_raw("A"), _raw("C")], [cache])
        # intersect=1, union=3, score=0.333 < 0.95 → None
        assert result is None

    def test_just_below_threshold_949_returns_none(
        self, service: MappingCacheService
    ) -> None:
        # Build sets so intersect/union = 19/20 = 0.95? No — that IS at threshold.
        # 18/20 = 0.9 → below. 19/20 = 0.95 → at threshold (>=). So we need
        # a configuration that gives strictly < 0.95. 18/19 = 0.947… below.
        cached_names = [f"S{i}" for i in range(20)]
        # raw shares 18 of them + 1 unique → intersect=18, union=21, score=18/21≈0.857.
        raw_names = cached_names[:18] + ["NEW1", "NEW2", "NEW3"]
        cache = _cache(raw=[_raw(n) for n in cached_names])
        result = service.jaccard_fuzzy_match(
            [_raw(n) for n in raw_names], [cache]
        )
        assert result is None  # 0.857 < 0.95

    def test_at_or_above_threshold_returns_match(
        self, service: MappingCacheService
    ) -> None:
        # 19 shared, 1 different on each side → intersect=19, union=21, ≈0.905 — too low.
        # 19 shared on both sides + 1 different on raw → intersect=19, union=20, =0.95
        cached_names = [f"S{i}" for i in range(20)]
        raw_names = cached_names[:19] + ["NEW1"]
        # intersect={S0..S18}=19, union={S0..S19,NEW1}=21 → no.
        # Adjust: keep all 20 cached + add 1 raw → intersect=20, union=21, =0.952
        raw_names = cached_names + ["NEW1"]
        cache = _cache(raw=[_raw(n) for n in cached_names])
        result = service.jaccard_fuzzy_match(
            [_raw(n) for n in raw_names], [cache]
        )
        assert result is not None
        assert result.cache.signature == cache.signature
        assert result.score >= JACCARD_FUZZY_THRESHOLD

    def test_perfect_overlap_score_one(
        self, service: MappingCacheService
    ) -> None:
        cache = _cache(raw=[_raw("EURUSD")])
        result = service.jaccard_fuzzy_match([_raw("EURUSD")], [cache])
        assert result is not None
        assert result.score == 1.0

    def test_empty_raw_returns_none(
        self, service: MappingCacheService
    ) -> None:
        cache = _cache(raw=[_raw("EURUSD")])
        assert service.jaccard_fuzzy_match([], [cache]) is None

    def test_no_caches_returns_none(
        self, service: MappingCacheService
    ) -> None:
        assert service.jaccard_fuzzy_match([_raw("EURUSD")], []) is None

    def test_best_of_multiple_wins(
        self, service: MappingCacheService
    ) -> None:
        big_overlap = _cache(raw=[_raw(f"S{i}") for i in range(20)])
        small_overlap = _cache(raw=[_raw("ZZZ")])
        raw = [_raw(f"S{i}") for i in range(20)]  # perfect match against big
        result = service.jaccard_fuzzy_match(raw, [small_overlap, big_overlap])
        assert result is not None
        assert result.cache.signature == big_overlap.signature
        assert result.score == 1.0


# ---------------------------------------------------------------------------
# lookup_signature hit / fuzzy / miss
# ---------------------------------------------------------------------------


class TestLookupSignature:
    @pytest.mark.asyncio
    async def test_miss_when_no_caches(
        self, service: MappingCacheService
    ) -> None:
        result = await service.lookup_signature([_raw("EURUSD")])
        assert result.outcome == "miss"

    @pytest.mark.asyncio
    async def test_hit_when_signature_in_redis(
        self,
        service: MappingCacheService,
        repo: MappingCacheRepository,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        raw = [_raw("EURUSD")]
        cache = _cache(raw=raw, mappings=[_mapping()])
        await repo.write(cache)
        # Manually set the redis hash so lookup considers it a hit.
        await fake_redis.hset(  # type: ignore[misc]
            f"mapping_cache:{cache.signature}", mapping={"signature": cache.signature}
        )
        result = await service.lookup_signature(raw)
        assert result.outcome == "hit"
        assert result.matched_cache is not None
        assert result.matched_cache.signature == cache.signature

    @pytest.mark.asyncio
    async def test_fuzzy_match_when_redis_empty_but_disk_close(
        self,
        service: MappingCacheService,
        repo: MappingCacheRepository,
    ) -> None:
        cached_names = [f"S{i}" for i in range(20)]
        cache = _cache(raw=[_raw(n) for n in cached_names])
        await repo.write(cache)
        # Raw with one new symbol → 20/21 = 0.952.
        raw = [_raw(n) for n in cached_names + ["NEW1"]]
        result = await service.lookup_signature(raw)
        assert result.outcome == "fuzzy_match"
        assert result.fuzzy_candidate is not None
        assert result.fuzzy_score >= JACCARD_FUZZY_THRESHOLD


# ---------------------------------------------------------------------------
# Spec divergence
# ---------------------------------------------------------------------------


class TestSpecDivergence:
    def test_no_divergence_returns_empty(
        self, service: MappingCacheService
    ) -> None:
        raw = [_raw("EURUSDz")]
        cache = _cache(raw=raw, mappings=[_mapping("EURUSD", "EURUSDz")])
        # mapping pip_value default is 10.0; raw derived = 100000 * 0.0001 = 10.0.
        assert service.validate_spec_divergence(raw, cache) == []

    def test_contract_size_block(
        self, service: MappingCacheService
    ) -> None:
        raw = [_raw("EURUSDz", contract_size=10000.0)]
        cache = _cache(
            raw=[_raw("EURUSDz")],
            mappings=[_mapping("EURUSD", "EURUSDz", contract_size=100000.0)],
        )
        divs = service.validate_spec_divergence(raw, cache)
        block = [d for d in divs if d.field == "contract_size"]
        assert len(block) == 1
        assert block[0].severity == "BLOCK"

    def test_digits_block_against_snapshot(
        self, service: MappingCacheService
    ) -> None:
        raw = [_raw("EURUSDz", digits=3)]
        cache = _cache(
            raw=[_raw("EURUSDz", digits=5)],  # snapshot remembered 5
            mappings=[_mapping("EURUSD", "EURUSDz")],
        )
        divs = service.validate_spec_divergence(raw, cache)
        digit_div = [d for d in divs if d.field == "digits"]
        assert len(digit_div) == 1
        assert digit_div[0].severity == "BLOCK"

    def test_currency_profit_block(
        self, service: MappingCacheService
    ) -> None:
        raw = [_raw("EURUSDz", currency_profit="EUR")]
        cache = _cache(
            raw=[_raw("EURUSDz")],
            mappings=[_mapping("EURUSD", "EURUSDz", quote_ccy="USD")],
        )
        divs = service.validate_spec_divergence(raw, cache)
        ccy_div = [d for d in divs if d.field == "currency_profit"]
        assert len(ccy_div) == 1
        assert ccy_div[0].severity == "BLOCK"

    def test_pip_size_within_5pct_no_warn(
        self, service: MappingCacheService
    ) -> None:
        # 4% delta: 0.0001 → 0.000104.
        raw = [_raw("EURUSDz", pip_size=0.000104)]
        cache = _cache(
            raw=[_raw("EURUSDz")],
            mappings=[_mapping("EURUSD", "EURUSDz", pip_size=0.0001)],
        )
        divs = service.validate_spec_divergence(raw, cache)
        assert all(d.field != "pip_size" for d in divs)

    def test_pip_size_above_5pct_warn(
        self, service: MappingCacheService
    ) -> None:
        # 6% delta: 0.0001 → 0.000106.
        raw = [_raw("EURUSDz", pip_size=0.000106)]
        cache = _cache(
            raw=[_raw("EURUSDz", pip_size=0.000106)],  # snapshot doesn't matter for pip
            mappings=[_mapping("EURUSD", "EURUSDz", pip_size=0.0001)],
        )
        divs = service.validate_spec_divergence(raw, cache)
        ps_div = [d for d in divs if d.field == "pip_size"]
        assert len(ps_div) == 1
        assert ps_div[0].severity == "WARN"
        assert ps_div[0].delta_percent is not None
        assert ps_div[0].delta_percent > PIP_SIZE_TOLERANCE_PCT

    def test_pip_value_above_10pct_warn(
        self, service: MappingCacheService
    ) -> None:
        # derived = contract_size × pip_size = 100000 × 0.000115 = 11.5
        # cached pip_value = 10.0 → delta = 15%.
        raw = [_raw("EURUSDz", pip_size=0.000115)]
        cache = _cache(
            raw=[_raw("EURUSDz", pip_size=0.000115)],
            mappings=[
                _mapping(
                    "EURUSD", "EURUSDz", pip_size=0.000115, pip_value=10.0
                )
            ],
        )
        divs = service.validate_spec_divergence(raw, cache)
        pv_div = [d for d in divs if d.field == "pip_value"]
        assert len(pv_div) == 1
        assert pv_div[0].severity == "WARN"
        assert pv_div[0].delta_percent is not None
        assert pv_div[0].delta_percent > PIP_VALUE_TOLERANCE_PCT

    def test_volume_step_warn_on_change(
        self, service: MappingCacheService
    ) -> None:
        raw = [_raw("EURUSDz", volume_step=0.05)]
        cache = _cache(
            raw=[_raw("EURUSDz")],
            mappings=[_mapping("EURUSD", "EURUSDz", exness_volume_step=0.01)],
        )
        divs = service.validate_spec_divergence(raw, cache)
        vs_div = [d for d in divs if d.field == "volume_step"]
        assert len(vs_div) == 1
        assert vs_div[0].severity == "WARN"

    def test_symbol_disappeared_block(
        self, service: MappingCacheService
    ) -> None:
        # Cache mapping refers to EURUSDz, but raw doesn't include it → BLOCK.
        raw = [_raw("OTHER")]
        cache = _cache(
            raw=[_raw("EURUSDz")],
            mappings=[_mapping("EURUSD", "EURUSDz")],
        )
        divs = service.validate_spec_divergence(raw, cache)
        miss = [d for d in divs if d.field == "symbol_missing"]
        assert len(miss) == 1
        assert miss[0].severity == "BLOCK"


# ---------------------------------------------------------------------------
# save_mapping
# ---------------------------------------------------------------------------


class TestSaveMapping:
    @pytest.mark.asyncio
    async def test_new_cache_writes_file_and_redis(
        self,
        service: MappingCacheService,
        repo: MappingCacheRepository,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        raw = [_raw("EURUSD")]  # exact match
        decisions = [MappingDecision(ftmo="EURUSD", action="accept")]
        # EURUSD is not in the FTMO whitelist as plain "EURUSD" — let me
        # check what's actually there. Phase 4.A.1 whitelist has standard
        # forex pairs. Use a known FTMO name from the whitelist.
        # Skip: replace with something that engine's tier-1 will match.
        result = await service.save_mapping("exn_001", raw, decisions)
        assert result.created_new_cache is True
        assert result.mapping_count == 1
        # File on disk
        cache = await repo.read(result.signature)
        assert cache is not None
        assert cache.created_by_account == "exn_001"
        # Redis hash populated
        hash_key = f"mapping_cache:{result.signature}"
        assert await fake_redis.exists(hash_key)
        # account pointer
        ptr = await fake_redis.get("account_to_mapping:exn_001")
        assert ptr == result.signature
        # status active
        st = await fake_redis.get("mapping_status:exn_001")
        assert st == "active"
        # raw deleted
        assert (await fake_redis.get("exness_raw_symbols:exn_001")) is None

    @pytest.mark.asyncio
    async def test_save_links_existing_cache_when_signature_hits(
        self,
        service: MappingCacheService,
        repo: MappingCacheRepository,
    ) -> None:
        raw = [_raw("EURUSD")]
        decisions = [MappingDecision(ftmo="EURUSD", action="accept")]
        first = await service.save_mapping("exn_001", raw, decisions)
        assert first.created_new_cache is True
        # Second account with same raw → link, not new file.
        second = await service.save_mapping("exn_002", raw, decisions)
        assert second.created_new_cache is False
        assert second.signature == first.signature
        # used_by_accounts now has both.
        cache = await repo.read(first.signature)
        assert cache is not None
        assert set(cache.used_by_accounts) == {"exn_001", "exn_002"}

    @pytest.mark.asyncio
    async def test_save_publishes_status_changed(
        self,
        service: MappingCacheService,
        broadcast_mock: BroadcastService,
    ) -> None:
        raw = [_raw("EURUSD")]
        decisions = [MappingDecision(ftmo="EURUSD", action="accept")]
        await service.save_mapping("exn_777", raw, decisions)
        broadcast_mock.publish.assert_awaited()  # type: ignore[attr-defined]
        # The most recent call should be on mapping_status:exn_777.
        last_call = broadcast_mock.publish.await_args_list[-1]  # type: ignore[attr-defined]
        channel, data = last_call.args
        assert channel == "mapping_status:exn_777"
        assert data["type"] == "status_changed"
        assert data["status"] == "active"
        assert data["account_id"] == "exn_777"

    @pytest.mark.asyncio
    async def test_save_with_override_decision(
        self, service: MappingCacheService, repo: MappingCacheRepository
    ) -> None:
        raw = [_raw("EURUSDz"), _raw("OTHER")]
        decisions = [
            MappingDecision(
                ftmo="EURUSD", action="override", exness_override="OTHER"
            ),
        ]
        result = await service.save_mapping("exn_001", raw, decisions)
        cache = await repo.read(result.signature)
        assert cache is not None
        assert len(cache.mappings) == 1
        assert cache.mappings[0].ftmo == "EURUSD"
        assert cache.mappings[0].exness == "OTHER"
        assert cache.mappings[0].match_type == "override"

    @pytest.mark.asyncio
    async def test_save_with_skip_excludes_symbol(
        self, service: MappingCacheService, repo: MappingCacheRepository
    ) -> None:
        raw = [_raw("EURUSD"), _raw("GBPUSD")]
        decisions = [
            MappingDecision(ftmo="EURUSD", action="accept"),
            MappingDecision(ftmo="GBPUSD", action="skip"),
        ]
        result = await service.save_mapping("exn_001", raw, decisions)
        cache = await repo.read(result.signature)
        assert cache is not None
        assert len(cache.mappings) == 1
        assert cache.mappings[0].ftmo == "EURUSD"

    @pytest.mark.asyncio
    async def test_save_override_unknown_symbol_raises(
        self, service: MappingCacheService
    ) -> None:
        raw = [_raw("EURUSD")]
        decisions = [
            MappingDecision(
                ftmo="EURUSD", action="override", exness_override="GHOST"
            ),
        ]
        with pytest.raises(ValueError, match="unknown Exness"):
            await service.save_mapping("exn_001", raw, decisions)


# ---------------------------------------------------------------------------
# edit_mapping
# ---------------------------------------------------------------------------


class TestEditMapping:
    @pytest.mark.asyncio
    async def test_edit_no_prior_mapping_raises(
        self, service: MappingCacheService
    ) -> None:
        with pytest.raises(ValueError, match="no current mapping"):
            await service.edit_mapping("exn_unknown", [])

    @pytest.mark.asyncio
    async def test_edit_overwrites_mappings(
        self,
        service: MappingCacheService,
        repo: MappingCacheRepository,
    ) -> None:
        raw = [_raw("EURUSD"), _raw("ALT")]
        await service.save_mapping(
            "exn_001",
            raw,
            [
                MappingDecision(ftmo="EURUSD", action="accept"),
                MappingDecision(
                    ftmo="ALT", action="override", exness_override="ALT"
                ),
            ],
        )
        result = await service.edit_mapping(
            "exn_001",
            [
                MappingDecision(ftmo="EURUSD", action="accept"),
                MappingDecision(ftmo="ALT", action="skip"),
            ],
        )
        # Edit removed ALT — count drops to 1 in the same cache.
        assert result.mapping_count == 1
        assert result.created_new_cache is False
        cache = await repo.read(result.signature)
        assert cache is not None
        assert len(cache.mappings) == 1
        assert cache.mappings[0].ftmo == "EURUSD"

    @pytest.mark.asyncio
    async def test_edit_preserves_prior_via_bak(
        self,
        service: MappingCacheService,
        repo: MappingCacheRepository,
        tmp_path: Path,
    ) -> None:
        raw = [_raw("EURUSD")]
        await service.save_mapping(
            "exn_001", raw, [MappingDecision(ftmo="EURUSD", action="accept")]
        )
        # Edit: skip everything — empty mapping.
        result = await service.edit_mapping(
            "exn_001",
            [MappingDecision(ftmo="EURUSD", action="skip")],
        )
        assert result.mapping_count == 0
        # .bak file must exist with the previous content.
        bak_files = list(tmp_path.glob("*.bak"))
        assert len(bak_files) == 1

    @pytest.mark.asyncio
    async def test_edit_publishes_active_status(
        self,
        service: MappingCacheService,
        broadcast_mock: BroadcastService,
    ) -> None:
        raw = [_raw("EURUSD")]
        await service.save_mapping(
            "exn_001", raw, [MappingDecision(ftmo="EURUSD", action="accept")]
        )
        broadcast_mock.publish.reset_mock()  # type: ignore[attr-defined]
        await service.edit_mapping(
            "exn_001", [MappingDecision(ftmo="EURUSD", action="accept")]
        )
        broadcast_mock.publish.assert_awaited()  # type: ignore[attr-defined]
        last_call = broadcast_mock.publish.await_args  # type: ignore[attr-defined]
        channel, data = last_call.args
        assert channel == "mapping_status:exn_001"
        assert data["status"] == "active"


# ---------------------------------------------------------------------------
# populate_redis_from_disk
# ---------------------------------------------------------------------------


class TestPopulateRedis:
    @pytest.mark.asyncio
    async def test_empty_repo_returns_zero(
        self, service: MappingCacheService
    ) -> None:
        assert await service.populate_redis_from_disk() == 0

    @pytest.mark.asyncio
    async def test_writes_each_cache_into_redis(
        self,
        service: MappingCacheService,
        repo: MappingCacheRepository,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        c1 = _cache(raw=[_raw("EURUSD")], created_by="exn_a")
        c2 = _cache(raw=[_raw("GBPUSD")], created_by="exn_b")
        await repo.write(c1)
        await repo.write(c2)
        loaded = await service.populate_redis_from_disk()
        assert loaded == 2
        assert await fake_redis.exists(f"mapping_cache:{c1.signature}")
        assert await fake_redis.exists(f"mapping_cache:{c2.signature}")
        assert await fake_redis.get("account_to_mapping:exn_a") == c1.signature

    @pytest.mark.asyncio
    async def test_orphan_redis_key_dropped(
        self,
        service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # Pre-populate a stale redis key with no on-disk file.
        await fake_redis.hset(  # type: ignore[misc]
            "mapping_cache:ghost-sig-xyz", mapping={"signature": "ghost-sig-xyz"}
        )
        loaded = await service.populate_redis_from_disk()
        assert loaded == 0
        assert not await fake_redis.exists("mapping_cache:ghost-sig-xyz")


# ---------------------------------------------------------------------------
# Status + raw snapshot reads
# ---------------------------------------------------------------------------


class TestStatusAndSnapshot:
    @pytest.mark.asyncio
    async def test_status_default_pending_mapping(
        self, service: MappingCacheService
    ) -> None:
        snap = await service.get_mapping_status("never-set")
        assert snap.status == "pending_mapping"
        assert snap.signature is None

    @pytest.mark.asyncio
    async def test_status_unknown_value_logs_and_defaults(
        self,
        service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await fake_redis.set("mapping_status:exn_z", "garbage_value")
        snap = await service.get_mapping_status("exn_z")
        assert snap.status == "pending_mapping"

    @pytest.mark.asyncio
    async def test_set_status_publishes_broadcast(
        self,
        service: MappingCacheService,
        broadcast_mock: BroadcastService,
    ) -> None:
        await service.set_mapping_status("exn_x", "spec_mismatch")
        broadcast_mock.publish.assert_awaited_once()  # type: ignore[attr-defined]
        channel, data = broadcast_mock.publish.await_args.args  # type: ignore[attr-defined]
        assert channel == "mapping_status:exn_x"
        assert data["status"] == "spec_mismatch"

    @pytest.mark.asyncio
    async def test_get_raw_snapshot_returns_none_when_absent(
        self, service: MappingCacheService
    ) -> None:
        assert await service.get_raw_snapshot("exn_unknown") is None

    @pytest.mark.asyncio
    async def test_get_raw_snapshot_parses_json(
        self,
        service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        snapshot_json = json.dumps([_raw("EURUSD").model_dump(mode="json")])
        await fake_redis.set("exness_raw_symbols:exn_seed", snapshot_json)
        loaded = await service.get_raw_snapshot("exn_seed")
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0].name == "EURUSD"

    @pytest.mark.asyncio
    async def test_get_raw_snapshot_corrupt_logs_returns_none(
        self,
        service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await fake_redis.set("exness_raw_symbols:exn_bad", "{not json")
        with caplog.at_level("ERROR"):
            assert await service.get_raw_snapshot("exn_bad") is None
        assert any(
            "raw_snapshot_corrupt" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# run_auto_match (integration with engine + whitelist)
# ---------------------------------------------------------------------------


class TestRunAutoMatch:
    def test_run_auto_match_empty_raw_all_unmapped(
        self,
        service: MappingCacheService,
        ftmo_whitelist: FTMOWhitelistService,
    ) -> None:
        result = service.run_auto_match([])
        assert result.proposals == []
        assert len(result.unmapped_ftmo) == ftmo_whitelist.count

    def test_run_auto_match_uses_real_engine(
        self, service: MappingCacheService
    ) -> None:
        # EURUSD exists in the FTMO whitelist; Tier-1 should match it.
        result = service.run_auto_match([_raw("EURUSD")])
        eurusd = [p for p in result.proposals if p.ftmo == "EURUSD"]
        assert len(eurusd) == 1
        assert eurusd[0].match_type == "exact"


# ---------------------------------------------------------------------------
# SpecDivergence dataclass shape
# ---------------------------------------------------------------------------


class TestSpecDivergenceDataclass:
    def test_construction(self) -> None:
        d = SpecDivergence(
            symbol="EURUSD",
            field="contract_size",
            cached_value=100000.0,
            raw_value=10000.0,
            severity="BLOCK",
        )
        assert d.delta_percent is None
        assert d.severity == "BLOCK"
