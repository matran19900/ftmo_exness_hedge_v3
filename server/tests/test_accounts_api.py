"""End-to-end tests for GET /api/accounts (step 3.12).

Hits the FastAPI app over the ASGI transport with the ``authed_client``
fixture. Asserts:

  - 401 without Bearer.
  - 200 + AccountListResponse shape on happy path.
  - status=online / offline / disabled correctly derived from Redis state.
  - Every documented response field present per entry.

Underlying ``get_all_accounts_with_status`` logic is unit-tested in
``test_redis_service.py``; this file covers the HTTP boundary
(auth + response shape + JSON serialisation).
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService
from httpx import AsyncClient


async def _seed_ftmo_account(
    fake_redis: fakeredis.aioredis.FakeRedis,
    *,
    account_id: str = "ftmo_001",
    name: str = "primary",
    enabled: bool = True,
    heartbeat: bool = True,
    seed_info: bool = True,
) -> None:
    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", account_id, name=name, enabled=enabled)
    if heartbeat:
        await fake_redis.set(f"client:ftmo:{account_id}", "1", ex=30)
    if seed_info:
        await fake_redis.hset(  # type: ignore[misc]
            f"account:ftmo:{account_id}",
            mapping={
                "balance": "1000000",
                "equity": "1005000",
                "margin": "0",
                "free_margin": "1005000",
                "currency": "USD",
                "money_digits": "2",
            },
        )


# ---------- auth ----------


@pytest.mark.asyncio
async def test_list_accounts_unauthenticated(client: AsyncClient) -> None:
    """No Bearer → 401 (matches /api/orders, /api/positions auth posture)."""
    resp = await client.get("/api/accounts")
    assert resp.status_code == 401


# ---------- happy path ----------


@pytest.mark.asyncio
async def test_list_accounts_happy_path_online_ftmo(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """One enabled FTMO account, heartbeat present, account info seeded
    → 200 with 1 entry, status=online, full money fields."""
    await _seed_ftmo_account(fake_redis)

    resp = await authed_client.get("/api/accounts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert len(body["accounts"]) == 1
    acc = body["accounts"][0]
    assert acc["broker"] == "ftmo"
    assert acc["account_id"] == "ftmo_001"
    assert acc["name"] == "primary"
    assert acc["enabled"] is True
    assert acc["status"] == "online"
    assert acc["balance_raw"] == "1000000"
    assert acc["equity_raw"] == "1005000"
    assert acc["currency"] == "USD"
    assert acc["money_digits"] == "2"


# ---------- status derivation ----------


@pytest.mark.asyncio
async def test_list_accounts_disabled_status(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """enabled=false in meta + heartbeat present → status=disabled
    (operator override beats heartbeat)."""
    await _seed_ftmo_account(fake_redis, enabled=False, heartbeat=True)

    resp = await authed_client.get("/api/accounts")
    assert resp.status_code == 200
    acc = resp.json()["accounts"][0]
    assert acc["enabled"] is False
    assert acc["status"] == "disabled"


@pytest.mark.asyncio
async def test_list_accounts_offline_status(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Registered + enabled but no heartbeat → status=offline. The UI
    uses this to disable the OrderForm submit button."""
    await _seed_ftmo_account(fake_redis, heartbeat=False, seed_info=False)

    resp = await authed_client.get("/api/accounts")
    assert resp.status_code == 200
    acc = resp.json()["accounts"][0]
    assert acc["status"] == "offline"
    # Default money fields when account info HASH absent.
    assert acc["balance_raw"] == "0"
    assert acc["equity_raw"] == "0"


# ---------- response shape ----------


@pytest.mark.asyncio
async def test_list_accounts_entry_carries_all_documented_fields(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Pin the 11 fields per entry — frontend AccountStatusEntry type
    expects all of them, so a silent drop here breaks the UI."""
    await _seed_ftmo_account(fake_redis)

    resp = await authed_client.get("/api/accounts")
    assert resp.status_code == 200
    acc = resp.json()["accounts"][0]
    expected = {
        "broker",
        "account_id",
        "name",
        "enabled",
        "status",
        "balance_raw",
        "equity_raw",
        "margin_raw",
        "free_margin_raw",
        "currency",
        "money_digits",
    }
    assert set(acc.keys()) == expected


# ---------- step 3.13: PATCH /api/accounts/{broker}/{account_id} ----------


@pytest.mark.asyncio
async def test_patch_account_unauth(client: AsyncClient) -> None:
    """No Bearer → 401, same posture as the other write endpoints."""
    resp = await client.patch("/api/accounts/ftmo/ftmo_001", json={"enabled": False})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_account_disable_happy_path(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Toggle an enabled account off → 200 + ``enabled=false`` +
    ``status=disabled`` even though the heartbeat is still alive
    (operator-side override takes precedence per step 3.12)."""
    await _seed_ftmo_account(fake_redis, enabled=True, heartbeat=True)

    resp = await authed_client.patch("/api/accounts/ftmo/ftmo_001", json={"enabled": False})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    assert body["status"] == "disabled"
    # Money fields still surface unchanged.
    assert body["balance_raw"] == "1000000"


@pytest.mark.asyncio
async def test_patch_account_enable_happy_path(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Toggle a disabled account on → 200 + ``enabled=true`` +
    ``status`` reflects heartbeat (online here)."""
    await _seed_ftmo_account(fake_redis, enabled=False, heartbeat=True)

    resp = await authed_client.patch("/api/accounts/ftmo/ftmo_001", json={"enabled": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["status"] == "online"


@pytest.mark.asyncio
async def test_patch_account_not_found(
    authed_client: AsyncClient,
) -> None:
    """Account that's never been registered → 404 with
    ``account_not_found`` error code so the frontend can surface a
    targeted toast."""
    resp = await authed_client.patch("/api/accounts/ftmo/never_existed", json={"enabled": True})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "account_not_found"
