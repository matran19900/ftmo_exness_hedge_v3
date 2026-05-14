"""Unit tests for ``MappingService`` (Phase 4.A.5).

The service is read-only. We seed Redis (pair + mapping_status), point a
real ``FTMOWhitelistService`` at the bundled whitelist file, and use the
real ``MappingCacheService`` over a tmp ``MappingCacheRepository`` so the
test exercises the full lookup chain without mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import fakeredis.aioredis
import pytest
import pytest_asyncio
from app.services.auto_match_engine import AutoMatchEngine
from app.services.broadcast import BroadcastService
from app.services.ftmo_whitelist_service import FTMOSymbol, FTMOWhitelistService
from app.services.mapping_cache_repository import MappingCacheRepository
from app.services.mapping_cache_schemas import (
    MappingEntry,
    RawSymbolEntry,
    SymbolMappingCacheFile,
)
from app.services.mapping_cache_service import MappingCacheService
from app.services.mapping_service import MappingService

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_HINTS_PATH = REPO_ROOT / "server" / "config" / "symbol_match_hints.json"
REAL_FTMO_WHITELIST_PATH = REPO_ROOT / "server" / "data" / "ftmo_whitelist.json"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


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


def _mapping(ftmo: str, exness: str) -> MappingEntry:
    return MappingEntry(
        ftmo=ftmo,
        exness=exness,
        match_type="exact",
        contract_size=100000.0,
        pip_size=0.0001,
        pip_value=10.0,
        quote_ccy="USD",
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )


@pytest_asyncio.fixture
async def service(
    tmp_path: Path,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> MappingService:
    repo = MappingCacheRepository(tmp_path)
    engine = AutoMatchEngine(REAL_HINTS_PATH)
    whitelist = FTMOWhitelistService(REAL_FTMO_WHITELIST_PATH)
    bs = BroadcastService()
    cache_service = MappingCacheService(
        repository=repo,
        engine=engine,
        ftmo_whitelist=whitelist,
        redis=fake_redis,
        broadcast=bs,
    )
    return MappingService(
        ftmo_whitelist=whitelist,
        cache_service=cache_service,
        redis=fake_redis,
    )


async def _seed_pair(
    fake_redis: fakeredis.aioredis.FakeRedis,
    *,
    pair_id: str = "pair_001",
    ftmo_account_id: str = "ftmo_001",
    exness_account_id: str = "exness_001",
) -> None:
    await fake_redis.hset(  # type: ignore[misc]
        f"pair:{pair_id}",
        mapping={
            "pair_id": pair_id,
            "name": "test-pair",
            "ftmo_account_id": ftmo_account_id,
            "exness_account_id": exness_account_id,
            "ratio": "1.0",
        },
    )


async def _seed_mapping_cache(
    *,
    repo: MappingCacheRepository,
    fake_redis: fakeredis.aioredis.FakeRedis,
    cache_service: MappingCacheService,
    exness_account_id: str = "exness_001",
    raw: list[RawSymbolEntry] | None = None,
    mappings: list[MappingEntry] | None = None,
) -> None:
    raw_list = raw if raw is not None else [_raw("EURUSD")]
    map_list = mappings if mappings is not None else [_mapping("EURUSD", "EURUSD")]
    sig = "test-sig-" + exness_account_id
    cache = SymbolMappingCacheFile(
        schema_version=1,
        signature=sig,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        created_by_account=exness_account_id,
        used_by_accounts=[exness_account_id],
        raw_symbols_snapshot=raw_list,
        mappings=map_list,
    )
    await repo.write(cache)
    await fake_redis.set(f"account_to_mapping:{exness_account_id}", sig)
    await fake_redis.set(f"mapping_status:{exness_account_id}", "active")
    await cache_service._populate_cache_into_redis(cache)  # noqa: SLF001


# ---------------------------------------------------------------------------
# get_ftmo_symbol
# ---------------------------------------------------------------------------


class TestGetFtmoSymbol:
    def test_returns_typed_entry_when_whitelisted(
        self, service: MappingService
    ) -> None:
        entry = service.get_ftmo_symbol("EURUSD")
        assert entry is not None
        assert isinstance(entry, FTMOSymbol)
        assert entry.name == "EURUSD"

    def test_returns_none_when_not_whitelisted(
        self, service: MappingService
    ) -> None:
        assert service.get_ftmo_symbol("NOTREAL") is None

    def test_all_ftmo_symbol_names_returns_full_set(
        self, service: MappingService
    ) -> None:
        names = service.all_ftmo_symbol_names()
        assert "EURUSD" in names
        assert names == sorted(names)
        assert len(names) == 117


# ---------------------------------------------------------------------------
# get_exness_mapping + get_all_mappings_for_account
# ---------------------------------------------------------------------------


class TestGetExnessMapping:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_cache(
        self, service: MappingService
    ) -> None:
        assert await service.get_exness_mapping("exness_unknown", "EURUSD") is None

    @pytest.mark.asyncio
    async def test_returns_entry_when_mapped(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_mapping_cache(
            repo=service._cache_service._repository,  # noqa: SLF001
            fake_redis=fake_redis,
            cache_service=service._cache_service,  # noqa: SLF001
        )
        entry = await service.get_exness_mapping("exness_001", "EURUSD")
        assert entry is not None
        assert entry.ftmo == "EURUSD"
        assert entry.exness == "EURUSD"

    @pytest.mark.asyncio
    async def test_returns_none_when_symbol_not_in_cache(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_mapping_cache(
            repo=service._cache_service._repository,  # noqa: SLF001
            fake_redis=fake_redis,
            cache_service=service._cache_service,  # noqa: SLF001
        )
        assert await service.get_exness_mapping("exness_001", "GBPUSD") is None

    @pytest.mark.asyncio
    async def test_all_mappings_for_account_returns_full_list(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_mapping_cache(
            repo=service._cache_service._repository,  # noqa: SLF001
            fake_redis=fake_redis,
            cache_service=service._cache_service,  # noqa: SLF001
            raw=[_raw("EURUSD"), _raw("GBPUSD")],
            mappings=[
                _mapping("EURUSD", "EURUSD"),
                _mapping("GBPUSD", "GBPUSD"),
            ],
        )
        entries = await service.get_all_mappings_for_account("exness_001")
        assert {e.ftmo for e in entries} == {"EURUSD", "GBPUSD"}

    @pytest.mark.asyncio
    async def test_all_mappings_for_account_empty_when_no_cache(
        self, service: MappingService
    ) -> None:
        assert await service.get_all_mappings_for_account("exness_unknown") == []


# ---------------------------------------------------------------------------
# get_pair_mapping
# ---------------------------------------------------------------------------


class TestGetPairMapping:
    @pytest.mark.asyncio
    async def test_happy_path(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_pair(fake_redis)
        await _seed_mapping_cache(
            repo=service._cache_service._repository,  # noqa: SLF001
            fake_redis=fake_redis,
            cache_service=service._cache_service,  # noqa: SLF001
        )
        result = await service.get_pair_mapping("pair_001", "EURUSD")
        assert result is not None
        ftmo, exness = result
        assert ftmo.name == "EURUSD"
        assert exness.exness == "EURUSD"

    @pytest.mark.asyncio
    async def test_pair_not_found_returns_none(
        self, service: MappingService
    ) -> None:
        assert await service.get_pair_mapping("ghost_pair", "EURUSD") is None

    @pytest.mark.asyncio
    async def test_pair_no_exness_account_id_returns_none(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_pair(fake_redis, exness_account_id="")
        assert await service.get_pair_mapping("pair_001", "EURUSD") is None

    @pytest.mark.asyncio
    async def test_unknown_ftmo_symbol_returns_none(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_pair(fake_redis)
        await _seed_mapping_cache(
            repo=service._cache_service._repository,  # noqa: SLF001
            fake_redis=fake_redis,
            cache_service=service._cache_service,  # noqa: SLF001
        )
        assert await service.get_pair_mapping("pair_001", "NOTREAL") is None

    @pytest.mark.asyncio
    async def test_no_mapping_cache_returns_none(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_pair(fake_redis)
        # No mapping cache seeded.
        assert await service.get_pair_mapping("pair_001", "EURUSD") is None


# ---------------------------------------------------------------------------
# is_pair_symbol_tradeable
# ---------------------------------------------------------------------------


class TestIsPairSymbolTradeable:
    @pytest.mark.asyncio
    async def test_pair_not_found(self, service: MappingService) -> None:
        ok, reason = await service.is_pair_symbol_tradeable(
            "ghost_pair", "EURUSD"
        )
        assert (ok, reason) == (False, "pair_not_found")

    @pytest.mark.asyncio
    async def test_ftmo_symbol_not_whitelisted(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_pair(fake_redis)
        ok, reason = await service.is_pair_symbol_tradeable(
            "pair_001", "NOTREAL"
        )
        assert (ok, reason) == (False, "ftmo_symbol_not_whitelisted")

    @pytest.mark.asyncio
    async def test_phase_3_compat_no_exness_account_id(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # Phase 3 single-leg pair (no exness_account_id) → FTMO check only.
        await _seed_pair(fake_redis, exness_account_id="")
        ok, reason = await service.is_pair_symbol_tradeable(
            "pair_001", "EURUSD"
        )
        assert (ok, reason) == (True, None)

    @pytest.mark.asyncio
    async def test_phase_3_compat_no_active_mapping(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # Pair has exness_account_id but wizard hasn't been run.
        await _seed_pair(fake_redis)
        ok, reason = await service.is_pair_symbol_tradeable(
            "pair_001", "EURUSD"
        )
        assert (ok, reason) == (True, None)

    @pytest.mark.asyncio
    async def test_active_status_but_no_cache_blocks(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # mapping_status=active but cache file gone (corrupt state).
        await _seed_pair(fake_redis)
        await fake_redis.set("mapping_status:exness_001", "active")
        ok, reason = await service.is_pair_symbol_tradeable(
            "pair_001", "EURUSD"
        )
        assert (ok, reason) == (False, "exness_account_has_no_mapping")

    @pytest.mark.asyncio
    async def test_symbol_not_mapped_for_account(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_pair(fake_redis)
        await _seed_mapping_cache(
            repo=service._cache_service._repository,  # noqa: SLF001
            fake_redis=fake_redis,
            cache_service=service._cache_service,  # noqa: SLF001
            mappings=[_mapping("GBPUSD", "GBPUSD")],
            raw=[_raw("GBPUSD")],
        )
        ok, reason = await service.is_pair_symbol_tradeable(
            "pair_001", "EURUSD"
        )
        assert (ok, reason) == (
            False,
            "ftmo_symbol_not_mapped_for_exness_account",
        )

    @pytest.mark.asyncio
    async def test_full_happy_path_active_mapping_present(
        self,
        service: MappingService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await _seed_pair(fake_redis)
        await _seed_mapping_cache(
            repo=service._cache_service._repository,  # noqa: SLF001
            fake_redis=fake_redis,
            cache_service=service._cache_service,  # noqa: SLF001
        )
        ok, reason = await service.is_pair_symbol_tradeable(
            "pair_001", "EURUSD"
        )
        assert (ok, reason) == (True, None)
