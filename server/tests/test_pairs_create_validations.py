"""Step 4.5 — POST /api/pairs/ validation matrix.

Three new gates between auth and the Redis HSET, in priority order:

  1. ``ftmo_account_not_found`` (400) — ``ftmo_account_id`` not a member
     of ``accounts:ftmo``.
  2. ``exness_account_not_found`` (400) — ``exness_account_id`` not a
     member of ``accounts:exness``.
  3. ``mapping_status_inactive`` (400) — Exness account exists but its
     ``mapping_status:{exness_account_id}`` Redis key is not ``"active"``
     (or absent → defaults to ``"pending_mapping"``).

The pre-existing ``test_pairs.py`` autouse fixture seeds both accounts
+ flips mapping to ``active``; this file deliberately skips that
seeding (per-test) so each negative path runs against the relevant
empty / wrong state.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService
from httpx import AsyncClient

PAIR_PATH = "/api/pairs/"
DEFAULT_FTMO = "ftmo_acc_p4"
DEFAULT_EXNESS = "exness_acc_p4"


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "validation matrix",
        "ftmo_account_id": DEFAULT_FTMO,
        "exness_account_id": DEFAULT_EXNESS,
        "ratio": 1.0,
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _no_default_seed() -> None:
    """Override the test_pairs.py autouse seed by NOT seeding here.

    pytest collects fixtures by file scope; this empty fixture has no
    visible effect but documents intent."""
    return None


# ---------- ftmo_account_not_found (priority 1) ----------


@pytest.mark.asyncio
async def test_create_pair_rejects_unknown_ftmo_account(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """No accounts seeded → first gate fires on ftmo_account_id."""
    resp = await authed_client.post(PAIR_PATH, json=_body())
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "ftmo_account_not_found"
    assert DEFAULT_FTMO in detail["message"]


# ---------- exness_account_not_found (priority 2) ----------


@pytest.mark.asyncio
async def test_create_pair_rejects_unknown_exness_account(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """FTMO seeded, Exness missing → second gate fires."""
    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", DEFAULT_FTMO, name="primary")
    resp = await authed_client.post(PAIR_PATH, json=_body())
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "exness_account_not_found"
    assert DEFAULT_EXNESS in detail["message"]


# ---------- mapping_status_inactive (priority 3) — every non-active value ----------


async def _seed_both_accounts(fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", DEFAULT_FTMO, name="primary")
    await svc.add_account("exness", DEFAULT_EXNESS, name="hedge")


@pytest.mark.asyncio
async def test_create_pair_rejects_no_mapping_status_key(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """No mapping_status key set → defaults to ``pending_mapping`` and is
    rejected. Documents the fresh-Exness-account default behaviour."""
    await _seed_both_accounts(fake_redis)
    resp = await authed_client.post(PAIR_PATH, json=_body())
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "mapping_status_inactive"
    assert detail["mapping_status"] == "pending_mapping"
    assert detail["exness_account_id"] == DEFAULT_EXNESS
    assert "mapping wizard" in detail["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_value",
    ["pending_mapping", "spec_mismatch", "disconnected"],
)
async def test_create_pair_rejects_each_non_active_status(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
    status_value: str,
) -> None:
    """Hard-block matrix: every status that isn't ``active`` blocks
    creation. D-4.A.7 mirror at pair-creation time."""
    await _seed_both_accounts(fake_redis)
    await fake_redis.set(f"mapping_status:{DEFAULT_EXNESS}", status_value)
    resp = await authed_client.post(PAIR_PATH, json=_body())
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "mapping_status_inactive"
    assert detail["mapping_status"] == status_value


# ---------- happy path ----------


@pytest.mark.asyncio
async def test_create_pair_succeeds_when_mapping_active(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_both_accounts(fake_redis)
    await fake_redis.set(f"mapping_status:{DEFAULT_EXNESS}", "active")
    resp = await authed_client.post(PAIR_PATH, json=_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ftmo_account_id"] == DEFAULT_FTMO
    assert body["exness_account_id"] == DEFAULT_EXNESS


# ---------- gate ORDER (ftmo wins over exness wins over mapping) ----------


@pytest.mark.asyncio
async def test_create_pair_ftmo_check_runs_before_exness_check(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Both account_ids missing — error_code MUST be the ftmo one
    (priority order documented in the create_pair handler)."""
    resp = await authed_client.post(PAIR_PATH, json=_body())
    assert resp.json()["detail"]["error_code"] == "ftmo_account_not_found"


@pytest.mark.asyncio
async def test_create_pair_exness_check_runs_before_mapping_check(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """FTMO present, Exness missing, mapping_status=active for some other
    Exness account — error_code MUST be exness_account_not_found, NOT
    mapping_status_inactive (the mapping check is only relevant when the
    account exists)."""
    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", DEFAULT_FTMO, name="primary")
    await fake_redis.set(f"mapping_status:{DEFAULT_EXNESS}", "active")
    resp = await authed_client.post(PAIR_PATH, json=_body())
    assert resp.json()["detail"]["error_code"] == "exness_account_not_found"
