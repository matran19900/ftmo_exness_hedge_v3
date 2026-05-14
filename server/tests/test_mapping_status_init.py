"""Tests for the Phase 4.2 ``_init_mapping_statuses`` lifespan sweep."""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from app.main import _init_mapping_statuses


@pytest.mark.asyncio
async def test_no_exness_accounts_returns_zero(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    count = await _init_mapping_statuses(fake_redis)
    assert count == 0


@pytest.mark.asyncio
async def test_fresh_account_gets_pending_mapping(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await fake_redis.sadd("accounts:exness", "exness_001")
    count = await _init_mapping_statuses(fake_redis)
    assert count == 1
    assert await fake_redis.get("mapping_status:exness_001") == "pending_mapping"


@pytest.mark.asyncio
async def test_existing_status_left_alone(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await fake_redis.sadd("accounts:exness", "exness_001")
    await fake_redis.set("mapping_status:exness_001", "spec_mismatch")
    count = await _init_mapping_statuses(fake_redis)
    assert count == 0
    assert await fake_redis.get("mapping_status:exness_001") == "spec_mismatch"


@pytest.mark.asyncio
async def test_account_pointing_at_cache_gets_active(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await fake_redis.sadd("accounts:exness", "exness_001")
    await fake_redis.set("account_to_mapping:exness_001", "sig-abc")
    count = await _init_mapping_statuses(fake_redis)
    assert count == 1
    assert await fake_redis.get("mapping_status:exness_001") == "active"


@pytest.mark.asyncio
async def test_multiple_accounts_mixed_state(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await fake_redis.sadd("accounts:exness", "exness_001", "exness_002", "exness_003")
    # exness_002 already has a status — must be preserved
    await fake_redis.set("mapping_status:exness_002", "active")
    # exness_003 has a cache pointer — should be initialised as active
    await fake_redis.set("account_to_mapping:exness_003", "sig-xyz")

    count = await _init_mapping_statuses(fake_redis)

    # exness_001 (fresh) + exness_003 (cache-pointer) → 2 inits
    assert count == 2
    assert await fake_redis.get("mapping_status:exness_001") == "pending_mapping"
    assert await fake_redis.get("mapping_status:exness_002") == "active"
    assert await fake_redis.get("mapping_status:exness_003") == "active"
