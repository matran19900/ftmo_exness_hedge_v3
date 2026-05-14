"""Integration tests for ``GET /api/pairs/{pair_id}/check-symbol/{symbol}`` (Phase 4.A.5).

The endpoint is a thin adapter over ``MappingService.is_pair_symbol_tradeable``;
the unit-level cases are in ``test_mapping_service.py``. Here we cover the
HTTP shape: auth + 200 ``tradeable=true|false`` + 404 doesn't apply (the
endpoint always returns 200 with the ``reason`` field carrying the failure).
"""

from __future__ import annotations

from datetime import UTC, datetime

import fakeredis.aioredis
import pytest
from app.main import app
from app.services.mapping_cache_schemas import (
    MappingEntry,
    RawSymbolEntry,
    SymbolMappingCacheFile,
)
from httpx import AsyncClient


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


async def _seed_pair(
    fake_redis: fakeredis.aioredis.FakeRedis,
    *,
    pair_id: str = "pair_001",
    exness_account_id: str = "exness_001",
) -> None:
    await fake_redis.hset(  # type: ignore[misc]
        f"pair:{pair_id}",
        mapping={
            "pair_id": pair_id,
            "name": "test-pair",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": exness_account_id,
            "ratio": "1.0",
        },
    )


async def _seed_active_mapping(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    repo = app.state.mapping_cache_repository
    cache_service = app.state.mapping_cache_service
    cache = SymbolMappingCacheFile(
        schema_version=1,
        signature="sig-test",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        created_by_account="exness_001",
        used_by_accounts=["exness_001"],
        raw_symbols_snapshot=[_raw("EURUSD")],
        mappings=[_mapping("EURUSD", "EURUSD")],
    )
    await repo.write(cache)
    await fake_redis.set("account_to_mapping:exness_001", "sig-test")
    await fake_redis.set("mapping_status:exness_001", "active")
    await cache_service._populate_cache_into_redis(cache)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_401(client: AsyncClient) -> None:
    resp = await client.get("/api/pairs/pair_001/check-symbol/EURUSD")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Failure reasons
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_not_found(authed_client: AsyncClient) -> None:
    resp = await authed_client.get("/api/pairs/ghost_pair/check-symbol/EURUSD")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"tradeable": False, "reason": "pair_not_found"}


@pytest.mark.asyncio
async def test_ftmo_symbol_not_whitelisted(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_pair(fake_redis)
    resp = await authed_client.get("/api/pairs/pair_001/check-symbol/NOTREAL")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"tradeable": False, "reason": "ftmo_symbol_not_whitelisted"}


@pytest.mark.asyncio
async def test_phase_3_compat_no_exness_passes(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Phase 3 single-leg pair: no exness_account_id → tradeable=True."""
    await _seed_pair(fake_redis, exness_account_id="")
    resp = await authed_client.get("/api/pairs/pair_001/check-symbol/EURUSD")
    body = resp.json()
    assert body == {"tradeable": True, "reason": None}


@pytest.mark.asyncio
async def test_phase_3_compat_wizard_not_run_passes(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Pair has exness_account_id but mapping_status != active."""
    await _seed_pair(fake_redis)
    resp = await authed_client.get("/api/pairs/pair_001/check-symbol/EURUSD")
    body = resp.json()
    assert body == {"tradeable": True, "reason": None}


@pytest.mark.asyncio
async def test_active_status_no_cache_blocks(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_pair(fake_redis)
    await fake_redis.set("mapping_status:exness_001", "active")
    resp = await authed_client.get("/api/pairs/pair_001/check-symbol/EURUSD")
    body = resp.json()
    assert body == {
        "tradeable": False,
        "reason": "exness_account_has_no_mapping",
    }


@pytest.mark.asyncio
async def test_symbol_not_mapped_for_account(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_pair(fake_redis)
    # Active mapping exists but for GBPUSD only.
    repo = app.state.mapping_cache_repository
    cache_service = app.state.mapping_cache_service
    cache = SymbolMappingCacheFile(
        schema_version=1,
        signature="sig-gbpusd",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        created_by_account="exness_001",
        used_by_accounts=["exness_001"],
        raw_symbols_snapshot=[_raw("GBPUSD")],
        mappings=[_mapping("GBPUSD", "GBPUSD")],
    )
    await repo.write(cache)
    await fake_redis.set("account_to_mapping:exness_001", "sig-gbpusd")
    await fake_redis.set("mapping_status:exness_001", "active")
    await cache_service._populate_cache_into_redis(cache)  # noqa: SLF001
    resp = await authed_client.get("/api/pairs/pair_001/check-symbol/EURUSD")
    body = resp.json()
    assert body == {
        "tradeable": False,
        "reason": "ftmo_symbol_not_mapped_for_exness_account",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_tradeable(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_pair(fake_redis)
    await _seed_active_mapping(fake_redis)
    resp = await authed_client.get("/api/pairs/pair_001/check-symbol/EURUSD")
    body = resp.json()
    assert body == {"tradeable": True, "reason": None}
