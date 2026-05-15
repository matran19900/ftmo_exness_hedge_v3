"""Step 4.5a — ``remove_account`` Exness mapping cleanup.

Covers the new behaviour introduced by step 4.5a:

  - Exness ``remove_account`` DELs ``mapping_status:{id}``,
    ``account_to_mapping:{id}`` and ``exness_raw_symbols:{id}``.
  - ``mapping_cache:{sig}`` HASH:
      * sole-user case → HASH deleted + on-disk cache file deleted.
      * multi-user case → ``used_by_accounts`` updated, HASH preserved,
        file preserved with new ``used_by_accounts``.
  - Idempotent against pre-existing legacy state (no pointer, no HASH).
  - FTMO ``remove_account`` is unchanged — no mapping keys touched.

Bug context: verify-mapping-status-leak.md confirmed that a re-added
Exness ``account_id`` against a Cent broker would silently reuse a
Standard-broker cache (contract_size 100000 vs 1000) → 100× under-hedge.
The fix is to scrub mapping artefacts on removal so the re-added account
re-enters the wizard from a clean slate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import fakeredis.aioredis
import pytest
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw(name: str = "EURUSDz") -> RawSymbolEntry:
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


def _mapping_entry(ftmo: str = "EURUSD", exness: str = "EURUSDz") -> MappingEntry:
    return MappingEntry(
        ftmo=ftmo,
        exness=exness,
        match_type="suffix_strip",
        contract_size=100000.0,
        pip_size=0.0001,
        pip_value=10.0,
        quote_ccy="USD",
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )


def _cache(
    *,
    raw: list[RawSymbolEntry] | None = None,
    created_by: str = "exness_001",
    used_by: list[str] | None = None,
) -> SymbolMappingCacheFile:
    raw_list = raw if raw is not None else [_raw()]
    sig = compute_signature(raw_list)
    now = datetime.now(UTC)
    return SymbolMappingCacheFile(
        schema_version=1,
        signature=sig,
        created_at=now,
        updated_at=now,
        created_by_account=created_by,
        used_by_accounts=list(used_by) if used_by is not None else [created_by],
        raw_symbols_snapshot=raw_list,
        mappings=[_mapping_entry()],
    )


async def _seed_mapping_cache_hash(
    redis_client: fakeredis.aioredis.FakeRedis,
    cache: SymbolMappingCacheFile,
) -> None:
    """Write the cache's ``used_by_accounts`` into the Redis HASH the way
    ``MappingCacheService._populate_cache_into_redis`` would."""
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_exness_deletes_mapping_status(
    svc: RedisService, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    """Criterion #1: ``mapping_status:{account_id}`` is deleted by
    ``remove_account`` on the Exness path. Pre-4.5a this key leaked,
    making D-4.A.7's ``status == 'active'`` guard bypassable on re-add."""
    await svc.add_account("exness", "exness_001", "Cent demo")
    await fake_redis.set("mapping_status:exness_001", "active")

    await svc.remove_account("exness", "exness_001")

    assert await fake_redis.get("mapping_status:exness_001") is None


@pytest.mark.asyncio
async def test_remove_exness_deletes_account_to_mapping_pointer(
    svc: RedisService, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    """Criterion #2: ``account_to_mapping:{account_id}`` is deleted. The
    pointer alone is enough for the pre-4.5a ``_init_mapping_statuses``
    fallback to set ``mapping_status="active"`` on re-add — so dropping
    it is part of the under-hedge fix."""
    await svc.add_account("exness", "exness_001", "Cent demo")
    await fake_redis.set("account_to_mapping:exness_001", "sig_stale")

    await svc.remove_account("exness", "exness_001")

    assert await fake_redis.get("account_to_mapping:exness_001") is None


@pytest.mark.asyncio
async def test_remove_exness_deletes_raw_symbols_key(
    svc: RedisService, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    """Criterion #3: ``exness_raw_symbols:{account_id}`` is deleted.
    ``save_mapping`` already cleans it on commit, but a wizard abandoned
    mid-flow could leave a stale snapshot — scrub it on removal so it
    can't shadow a fresh one on re-add."""
    await svc.add_account("exness", "exness_001", "Cent demo")
    await fake_redis.set(
        "exness_raw_symbols:exness_001",
        json.dumps(
            [
                {
                    "name": "EURUSDz",
                    "contract_size": 100000.0,
                    "digits": 5,
                    "pip_size": 0.0001,
                    "volume_min": 0.01,
                    "volume_step": 0.01,
                    "volume_max": 200.0,
                    "currency_profit": "USD",
                }
            ]
        ),
    )

    await svc.remove_account("exness", "exness_001")

    assert await fake_redis.get("exness_raw_symbols:exness_001") is None


@pytest.mark.asyncio
async def test_remove_exness_sole_user_deletes_hash_and_file(
    svc: RedisService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    repo: MappingCacheRepository,
) -> None:
    """Criterion #4 (sample test in self-check §7): when the removed
    account is the sole user of a signature, both the ``mapping_cache:{sig}``
    HASH AND the on-disk ``{creator}_{sig}.json`` file are deleted. This
    closes the verify-mapping-status-leak.md exploit chain at its source:
    no stale cache can survive to mis-size a re-add against a different
    broker."""
    await svc.add_account("exness", "exness_001", "Cent demo")

    cache = _cache(created_by="exness_001", used_by=["exness_001"])
    await repo.write(cache)
    await _seed_mapping_cache_hash(fake_redis, cache)
    await fake_redis.set("account_to_mapping:exness_001", cache.signature)

    file_path = repo.cache_dir / f"exness_001_{cache.signature}.json"
    assert file_path.is_file()
    assert await fake_redis.exists(f"mapping_cache:{cache.signature}")

    await svc.remove_account("exness", "exness_001")

    assert not await fake_redis.exists(f"mapping_cache:{cache.signature}")
    assert not file_path.is_file()


@pytest.mark.asyncio
async def test_remove_exness_multi_user_preserves_hash_and_file(
    svc: RedisService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    repo: MappingCacheRepository,
) -> None:
    """Criterion #5 (sample test in self-check §7): when other accounts
    still reference the signature, the cache HASH and file are kept; only
    the removed account is dropped from ``used_by_accounts``. Both Redis
    HASH and on-disk file are updated in lock-step so a server restart
    doesn't resurrect the deleted account via ``populate_redis_from_disk``."""
    await svc.add_account("exness", "exness_001", "primary")
    await svc.add_account("exness", "exness_002", "shared")

    cache = _cache(
        created_by="exness_001", used_by=["exness_001", "exness_002"]
    )
    await repo.write(cache)
    await _seed_mapping_cache_hash(fake_redis, cache)
    await fake_redis.set("account_to_mapping:exness_001", cache.signature)
    await fake_redis.set("account_to_mapping:exness_002", cache.signature)

    file_path = repo.cache_dir / f"exness_001_{cache.signature}.json"

    await svc.remove_account("exness", "exness_001")

    assert await fake_redis.exists(f"mapping_cache:{cache.signature}")
    used_raw = await fake_redis.hget(  # type: ignore[misc]
        f"mapping_cache:{cache.signature}", "used_by_accounts"
    )
    assert json.loads(used_raw) == ["exness_002"]

    assert file_path.is_file()
    refreshed = await repo.read(cache.signature)
    assert refreshed is not None
    assert refreshed.used_by_accounts == ["exness_002"]


@pytest.mark.asyncio
async def test_remove_exness_handles_missing_pointer_gracefully(
    svc: RedisService, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    """Criterion #6: a clean account with no pointer must still run the
    base cleanup pipeline without raising — the wizard might never have
    completed, or it was already torn down on a prior removal."""
    await svc.add_account("exness", "exness_001", "fresh")

    await svc.remove_account("exness", "exness_001")

    # Base cleanup still ran.
    assert "exness_001" not in await fake_redis.smembers("accounts:exness")
    assert not await fake_redis.exists("account_meta:exness:exness_001")


@pytest.mark.asyncio
async def test_remove_exness_handles_orphan_pointer_without_hash(
    svc: RedisService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    repo: MappingCacheRepository,
) -> None:
    """Criterion #7: legacy state where ``account_to_mapping:{id}`` exists
    but the corresponding ``mapping_cache:{sig}`` HASH was already deleted
    (e.g. via ``populate_redis_from_disk`` orphan-sweep). Cleanup must be
    idempotent — no error, and any matching on-disk file is still deleted."""
    await svc.add_account("exness", "exness_001", "orphan")
    await fake_redis.set("account_to_mapping:exness_001", "sig_orphan")

    cache = _cache(created_by="exness_001", used_by=["exness_001"])
    legacy_path = repo.cache_dir / "exness_001_sig_orphan.json"
    # Hand-build a file with the orphan signature embedded in the name.
    cache_with_sig = SymbolMappingCacheFile(
        schema_version=cache.schema_version,
        signature="sig_orphan",
        created_at=cache.created_at,
        updated_at=cache.updated_at,
        created_by_account=cache.created_by_account,
        used_by_accounts=cache.used_by_accounts,
        raw_symbols_snapshot=cache.raw_symbols_snapshot,
        mappings=cache.mappings,
    )
    legacy_path.write_text(
        cache_with_sig.model_dump_json(indent=2), encoding="utf-8"
    )
    assert legacy_path.is_file()

    await svc.remove_account("exness", "exness_001")

    # No exception, file deleted via the missing-HASH legacy branch.
    assert not legacy_path.is_file()
    assert await fake_redis.get("account_to_mapping:exness_001") is None


@pytest.mark.asyncio
async def test_remove_ftmo_does_not_touch_mapping_keys(
    svc: RedisService,
    fake_redis: fakeredis.aioredis.FakeRedis,
    repo: MappingCacheRepository,
) -> None:
    """Criterion #8: FTMO removal path is unchanged. FTMO accounts have no
    mapping_status / account_to_mapping / mapping_cache lifecycle (the
    wizard is Exness-only). Seed those keys against an Exness account
    and confirm an FTMO remove leaves them untouched."""
    await svc.add_account("ftmo", "ftmo_001", "primary")
    await svc.add_account("exness", "exness_001", "Cent")

    cache = _cache(created_by="exness_001", used_by=["exness_001"])
    await repo.write(cache)
    await _seed_mapping_cache_hash(fake_redis, cache)
    await fake_redis.set("mapping_status:exness_001", "active")
    await fake_redis.set("account_to_mapping:exness_001", cache.signature)
    await fake_redis.set("exness_raw_symbols:exness_001", "[]")

    await svc.remove_account("ftmo", "ftmo_001")

    # FTMO side cleaned up...
    assert "ftmo_001" not in await fake_redis.smembers("accounts:ftmo")
    # ...but Exness-side mapping artefacts are intact.
    assert await fake_redis.get("mapping_status:exness_001") == "active"
    assert await fake_redis.get("account_to_mapping:exness_001") == cache.signature
    assert await fake_redis.get("exness_raw_symbols:exness_001") == "[]"
    assert await fake_redis.exists(f"mapping_cache:{cache.signature}")
    file_path = repo.cache_dir / f"exness_001_{cache.signature}.json"
    assert file_path.is_file()
