"""Step 4.5 — GET /api/pairs/{pair_id}/metadata enriched-payload endpoint.

Single-roundtrip composition: pair record + both account meta summaries
+ live ``MappingStatusSnapshot`` (status/signature/cache_filename) for
the Exness leg. Frontend Settings modal uses this to render the row
without firing 4 separate REST calls.

Auth: same Bearer JWT posture as the rest of /api/pairs.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService
from httpx import AsyncClient

PAIR_PATH = "/api/pairs/"
FTMO_ID = "ftmo_meta_001"
EXNESS_ID = "exness_meta_001"


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "metadata round-trip",
        "ftmo_account_id": FTMO_ID,
        "exness_account_id": EXNESS_ID,
        "ratio": 1.0,
    }
    body.update(overrides)
    return body


async def _seed_full_state(fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    """Both accounts + active mapping (the prerequisites POST /api/pairs/
    needs to succeed)."""
    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", FTMO_ID, name="FTMO Main")
    await svc.add_account("exness", EXNESS_ID, name="Exness Hedge")
    await fake_redis.set(f"mapping_status:{EXNESS_ID}", "active")


# ---------- auth ----------


@pytest.mark.asyncio
async def test_metadata_unauth_returns_401(client: AsyncClient) -> None:
    resp = await client.get(f"{PAIR_PATH}some-id/metadata")
    assert resp.status_code == 401


# ---------- 404 ----------


@pytest.mark.asyncio
async def test_metadata_unknown_pair_returns_404(
    authed_client: AsyncClient,
) -> None:
    resp = await authed_client.get(
        f"{PAIR_PATH}00000000-0000-0000-0000-000000000000/metadata"
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Pair not found"


# ---------- happy path ----------


@pytest.mark.asyncio
async def test_metadata_returns_full_enriched_payload(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_full_state(fake_redis)
    create = await authed_client.post(PAIR_PATH, json=_body())
    assert create.status_code == 201, create.text
    pair_id = create.json()["pair_id"]

    resp = await authed_client.get(f"{PAIR_PATH}{pair_id}/metadata")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Pair sub-object.
    assert body["pair"]["pair_id"] == pair_id
    assert body["pair"]["ftmo_account_id"] == FTMO_ID
    assert body["pair"]["exness_account_id"] == EXNESS_ID
    assert body["pair"]["ratio"] == 1.0

    # FTMO account summary.
    assert body["ftmo_account"]["broker_type"] == "ftmo"
    assert body["ftmo_account"]["account_id"] == FTMO_ID
    assert body["ftmo_account"]["name"] == "FTMO Main"
    assert body["ftmo_account"]["enabled"] is True

    # Exness account summary.
    assert body["exness_account"]["broker_type"] == "exness"
    assert body["exness_account"]["account_id"] == EXNESS_ID
    assert body["exness_account"]["name"] == "Exness Hedge"
    assert body["exness_account"]["enabled"] is True

    # Mapping status (active because we seeded it).
    assert body["mapping_status"] == "active"
    # signature/cache_filename remain null because no mapping_cache file
    # was actually written for this Exness account in this test.
    assert body["mapping_signature"] is None
    assert body["mapping_cache_filename"] is None


# ---------- mapping_status reflects current Redis state ----------


@pytest.mark.asyncio
async def test_metadata_mapping_status_reflects_post_creation_change(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Pair created with mapping=active; flip Redis to spec_mismatch
    afterwards. /metadata must surface the LIVE value, not whatever
    was true at create-time."""
    await _seed_full_state(fake_redis)
    create = await authed_client.post(PAIR_PATH, json=_body())
    pair_id = create.json()["pair_id"]
    await fake_redis.set(f"mapping_status:{EXNESS_ID}", "spec_mismatch")

    resp = await authed_client.get(f"{PAIR_PATH}{pair_id}/metadata")
    assert resp.status_code == 200
    assert resp.json()["mapping_status"] == "spec_mismatch"


# ---------- referenced account deleted post-creation ----------


@pytest.mark.asyncio
async def test_metadata_with_ftmo_account_deleted_returns_null_summary(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Pair created → FTMO account meta deleted (e.g. operator removed
    the account). /metadata returns 200 with ``ftmo_account=null`` so
    the UI can render an explicit "FTMO account deleted" warning rather
    than a 404 for the whole pair."""
    await _seed_full_state(fake_redis)
    create = await authed_client.post(PAIR_PATH, json=_body())
    pair_id = create.json()["pair_id"]
    # Yank the FTMO meta + set membership.
    svc = RedisService(fake_redis)
    await svc.remove_account("ftmo", FTMO_ID)

    resp = await authed_client.get(f"{PAIR_PATH}{pair_id}/metadata")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ftmo_account"] is None
    # Pair record + Exness summary + mapping status still present.
    assert body["pair"]["ftmo_account_id"] == FTMO_ID  # frozen reference
    assert body["exness_account"] is not None
    assert body["mapping_status"] == "active"


@pytest.mark.asyncio
async def test_metadata_with_exness_account_deleted_returns_null_summary(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Same shape as the FTMO-deleted case for the Exness leg.

    NOTE: ``remove_account`` does NOT clear the ``mapping_status:`` key
    (that key is owned by ``MappingCacheService``, not by
    ``RedisService.remove_account``). So mapping_status remains
    surfaced as the last-known value — the UI uses ``exness_account ==
    null`` as the ground truth, mapping_status is informational only
    in this state.
    """
    await _seed_full_state(fake_redis)
    create = await authed_client.post(PAIR_PATH, json=_body())
    pair_id = create.json()["pair_id"]
    svc = RedisService(fake_redis)
    await svc.remove_account("exness", EXNESS_ID)

    resp = await authed_client.get(f"{PAIR_PATH}{pair_id}/metadata")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exness_account"] is None
    assert body["pair"]["exness_account_id"] == EXNESS_ID  # frozen reference
    # ftmo side unaffected.
    assert body["ftmo_account"] is not None
