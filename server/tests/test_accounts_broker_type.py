"""Step 4.5 — broker_type field on account_meta + ?broker_type query filter.

Two surfaces under test:

  1. ``RedisService.add_account`` now persists ``broker_type`` on the
     ``account_meta:{broker}:{account_id}`` HASH; ``get_account_meta``
     lazy-backfills it for legacy records that pre-date the field.

  2. ``GET /api/accounts?broker_type=ftmo|exness`` filters the
     ``get_all_accounts_with_status`` result client-side. Omitting the
     param returns both brokers (Phase 3 default behaviour preserved).
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService
from httpx import AsyncClient


async def _seed_account(
    fake_redis: fakeredis.aioredis.FakeRedis,
    broker: str,
    account_id: str,
    *,
    name: str = "test",
    enabled: bool = True,
    heartbeat: bool = True,
) -> None:
    svc = RedisService(fake_redis)
    await svc.add_account(broker, account_id, name=name, enabled=enabled)
    if heartbeat:
        await fake_redis.set(f"client:{broker}:{account_id}", "1", ex=30)
    await fake_redis.hset(  # type: ignore[misc]
        f"account:{broker}:{account_id}",
        mapping={
            "balance": "1000000",
            "equity": "1005000",
            "margin": "0",
            "free_margin": "1005000",
            "currency": "USD",
            "money_digits": "2",
        },
    )


# ---------- RedisService level ----------


@pytest.mark.asyncio
async def test_add_account_persists_broker_type(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Step 4.5 contract: fresh writes carry an explicit ``broker_type``
    field on the meta HASH (not just the URL key)."""
    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", "ftmo_001", name="primary")
    raw = await fake_redis.hgetall("account_meta:ftmo:ftmo_001")  # type: ignore[misc]
    assert raw["broker_type"] == "ftmo"


@pytest.mark.asyncio
async def test_add_account_persists_broker_type_for_exness(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(fake_redis)
    await svc.add_account("exness", "exness_001", name="hedge")
    raw = await fake_redis.hgetall("account_meta:exness:exness_001")  # type: ignore[misc]
    assert raw["broker_type"] == "exness"


@pytest.mark.asyncio
async def test_get_account_meta_lazy_backfills_legacy_record(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """A pre-step-4.5 record (no broker_type in HASH) must surface
    broker_type=URL-key on read without persisting (forward-compat with
    rollback to step 4.4b — the field stays absent on disk)."""
    # Hand-write a legacy meta HASH (no broker_type)
    await fake_redis.sadd("accounts:ftmo", "ftmo_legacy")  # type: ignore[misc]
    await fake_redis.hset(  # type: ignore[misc]
        "account_meta:ftmo:ftmo_legacy",
        mapping={"name": "legacy", "enabled": "true", "created_at": "1700000000000"},
    )
    svc = RedisService(fake_redis)
    meta = await svc.get_account_meta("ftmo", "ftmo_legacy")
    assert meta is not None
    assert meta["broker_type"] == "ftmo"
    # And the on-disk record stays unchanged (no opportunistic write).
    raw = await fake_redis.hgetall("account_meta:ftmo:ftmo_legacy")  # type: ignore[misc]
    assert "broker_type" not in raw


@pytest.mark.asyncio
async def test_get_account_meta_returns_none_for_missing(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(fake_redis)
    assert await svc.get_account_meta("ftmo", "nope") is None


# ---------- API filter — happy paths ----------


@pytest.mark.asyncio
async def test_list_accounts_no_filter_returns_both_brokers(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_account(fake_redis, "ftmo", "ftmo_001")
    await _seed_account(fake_redis, "exness", "exness_001")
    resp = await authed_client.get("/api/accounts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    brokers = sorted(a["broker"] for a in body["accounts"])
    assert brokers == ["exness", "ftmo"]


@pytest.mark.asyncio
async def test_list_accounts_filter_ftmo(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_account(fake_redis, "ftmo", "ftmo_001")
    await _seed_account(fake_redis, "exness", "exness_001")
    resp = await authed_client.get("/api/accounts?broker_type=ftmo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["accounts"][0]["broker"] == "ftmo"
    assert body["accounts"][0]["account_id"] == "ftmo_001"


@pytest.mark.asyncio
async def test_list_accounts_filter_exness(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_account(fake_redis, "ftmo", "ftmo_001")
    await _seed_account(fake_redis, "exness", "exness_001")
    resp = await authed_client.get("/api/accounts?broker_type=exness")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["accounts"][0]["broker"] == "exness"


@pytest.mark.asyncio
async def test_list_accounts_filter_no_match_returns_empty(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Filter applied, but no accounts of that broker exist → 200 + empty."""
    await _seed_account(fake_redis, "ftmo", "ftmo_001")
    resp = await authed_client.get("/api/accounts?broker_type=exness")
    assert resp.status_code == 200
    assert resp.json() == {"accounts": [], "total": 0}


# ---------- API filter — validation ----------


@pytest.mark.asyncio
async def test_list_accounts_filter_invalid_value_rejected(
    authed_client: AsyncClient,
) -> None:
    """Pydantic Literal["ftmo","exness"] rejects unknown brokers with 422."""
    resp = await authed_client.get("/api/accounts?broker_type=oanda")
    assert resp.status_code == 422
