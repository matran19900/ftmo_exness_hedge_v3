"""Step 4.5a — end-to-end remove + readd scenarios.

Reproduces the full bug from verify-mapping-status-leak.md and confirms
the 4.5a fix closes it. Scenario summary:

  T0: account exness_001 mapped against Standard broker (contract_size 100000).
  T1: remove_account("exness", "exness_001").
  T2: add_account("exness", "exness_001") against Cent broker
      (contract_size 1000).
  T3: read mapping_status:exness_001 -> must be "pending_mapping" (not "active").

Before 4.5a, T3 read "active" (leaked) → D-4.A.7 hard-block was bypassed
and downstream ``compute_secondary_volume`` undersized the hedge by 100×.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import fakeredis.aioredis
import pytest
from app.main import _init_mapping_statuses
from app.services.mapping_cache_repository import (
    MappingCacheRepository,
    compute_signature,
)
from app.services.mapping_cache_schemas import (
    MappingEntry,
    RawSymbolEntry,
    SymbolMappingCacheFile,
)
from app.services.redis_service import RedisService


def _raw(name: str = "EURUSDz", contract_size: float = 100000.0) -> RawSymbolEntry:
    return RawSymbolEntry(
        name=name,
        contract_size=contract_size,
        digits=5,
        pip_size=0.0001,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=200.0,
        currency_profit="USD",
    )


def _mapping_entry(contract_size: float = 100000.0) -> MappingEntry:
    return MappingEntry(
        ftmo="EURUSD",
        exness="EURUSDz",
        match_type="suffix_strip",
        contract_size=contract_size,
        pip_size=0.0001,
        pip_value=contract_size * 0.0001,
        quote_ccy="USD",
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )


def _build_cache(
    *,
    created_by: str,
    used_by: list[str],
    contract_size: float = 100000.0,
) -> SymbolMappingCacheFile:
    raw_list = [_raw(contract_size=contract_size)]
    sig = compute_signature(raw_list)
    now = datetime.now(UTC)
    return SymbolMappingCacheFile(
        schema_version=1,
        signature=sig,
        created_at=now,
        updated_at=now,
        created_by_account=created_by,
        used_by_accounts=list(used_by),
        raw_symbols_snapshot=raw_list,
        mappings=[_mapping_entry(contract_size=contract_size)],
    )


async def _seed_mapping_cache_hash(
    redis_client: fakeredis.aioredis.FakeRedis,
    cache: SymbolMappingCacheFile,
) -> None:
    await redis_client.hset(  # type: ignore[misc]
        f"mapping_cache:{cache.signature}",
        mapping={
            "signature": cache.signature,
            "used_by_accounts": json.dumps(cache.used_by_accounts),
        },
    )


@pytest.fixture
def repo(tmp_path: Path) -> MappingCacheRepository:
    return MappingCacheRepository(tmp_path)


@pytest.fixture
def svc(
    fake_redis: fakeredis.aioredis.FakeRedis,
    repo: MappingCacheRepository,
) -> RedisService:
    s = RedisService(fake_redis)
    s.attach_mapping_cache_repository(repo)
    return s


@pytest.mark.asyncio
async def test_remove_then_readd_yields_pending_mapping(
    svc: RedisService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    repo: MappingCacheRepository,
) -> None:
    """Criterion #14 (sample test in self-check §7): end-to-end exploit
    chain from verify-mapping-status-leak.md. Set up the full leaked
    state, remove the account, re-add it, and confirm the
    ``mapping_status`` reads as ``pending_mapping`` rather than the
    pre-4.5a leaked ``"active"``."""
    # T0: account_001 mapped against Standard broker (contract_size 100k).
    await svc.add_account("exness", "exness_001", "Standard demo")
    cache = _build_cache(
        created_by="exness_001",
        used_by=["exness_001"],
        contract_size=100000.0,
    )
    await repo.write(cache)
    await _seed_mapping_cache_hash(fake_redis, cache)
    await fake_redis.set("mapping_status:exness_001", "active")
    await fake_redis.set("account_to_mapping:exness_001", cache.signature)

    file_path = repo.cache_dir / f"exness_001_{cache.signature}.json"
    assert file_path.is_file()

    # T1: remove the account.
    await svc.remove_account("exness", "exness_001")

    # Every leaked key should be gone.
    assert await fake_redis.get("mapping_status:exness_001") is None
    assert await fake_redis.get("account_to_mapping:exness_001") is None
    assert not await fake_redis.exists(f"mapping_cache:{cache.signature}")
    assert not file_path.is_file()

    # T2: re-add the same account_id (CEO points it at a Cent broker
    # this time). add_account does not write mapping_status.
    await svc.add_account("exness", "exness_001", "Cent demo")
    assert await fake_redis.get("mapping_status:exness_001") is None

    # T3: simulate the AccountsTab REST load — without a mapping_status
    # key the wizard surfaces "Map Symbols" CTA. We assert via the
    # raw key the same way ``MappingCacheService.get_mapping_status``
    # would (returning ``pending_mapping`` when the key is absent), and
    # via the lifespan sweep that also normalises this on startup.
    initialised = await _init_mapping_statuses(fake_redis)
    assert initialised == 1
    assert (
        await fake_redis.get("mapping_status:exness_001") == "pending_mapping"
    )


@pytest.mark.asyncio
async def test_lifespan_orphan_detection_after_pointer_survives(
    svc: RedisService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    repo: MappingCacheRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Criterion #15: defensive belt-and-suspenders — even if a future
    bug somehow left ``account_to_mapping`` intact past ``remove_account``,
    the next lifespan boot's ``_init_mapping_statuses`` orphan branch
    must still force the readded account back into pending_mapping."""
    # Simulate the legacy buggy state: account removed but pointer leaked.
    await svc.add_account("exness", "exness_001", "Cent")
    await fake_redis.set("account_to_mapping:exness_001", "sig_pre_4_5a")
    # NB: mapping_status key not set (matches pre-4.5a state where
    # _init_mapping_statuses fallback would have set "active").

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _init_mapping_statuses(fake_redis)

    assert await fake_redis.get("mapping_status:exness_001") == "pending_mapping"
    assert await fake_redis.get("account_to_mapping:exness_001") is None
    assert any(
        "orphan_pointer_detected" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_multi_account_cache_sharing_preserved(
    svc: RedisService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    repo: MappingCacheRepository,
) -> None:
    """Criterion #16: when two accounts share a cache signature (same
    broker, same symbol list), removing one must leave the other's
    mapping intact. Both Redis HASH and on-disk file should still be
    readable by the surviving account."""
    await svc.add_account("exness", "exness_a", "first")
    await svc.add_account("exness", "exness_b", "second")

    cache = _build_cache(
        created_by="exness_a", used_by=["exness_a", "exness_b"]
    )
    await repo.write(cache)
    await _seed_mapping_cache_hash(fake_redis, cache)
    await fake_redis.set("mapping_status:exness_a", "active")
    await fake_redis.set("mapping_status:exness_b", "active")
    await fake_redis.set("account_to_mapping:exness_a", cache.signature)
    await fake_redis.set("account_to_mapping:exness_b", cache.signature)

    file_path = repo.cache_dir / f"exness_a_{cache.signature}.json"

    await svc.remove_account("exness", "exness_a")

    # exness_a fully cleaned up.
    assert await fake_redis.get("mapping_status:exness_a") is None
    assert await fake_redis.get("account_to_mapping:exness_a") is None

    # exness_b mapping intact, both HASH + file.
    assert await fake_redis.get("mapping_status:exness_b") == "active"
    assert await fake_redis.get("account_to_mapping:exness_b") == cache.signature
    assert await fake_redis.exists(f"mapping_cache:{cache.signature}")
    used_raw = await fake_redis.hget(  # type: ignore[misc]
        f"mapping_cache:{cache.signature}", "used_by_accounts"
    )
    assert json.loads(used_raw) == ["exness_b"]
    assert file_path.is_file()
    refreshed = await repo.read(cache.signature)
    assert refreshed is not None
    assert refreshed.used_by_accounts == ["exness_b"]
