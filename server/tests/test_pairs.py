"""Tests for /api/pairs CRUD endpoints.

Uses the autouse fakeredis dependency override + real whitelist load from
conftest. ``authed_client`` provides a JWT-bearing httpx client.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from httpx import AsyncClient

PAIR_PATH = "/api/pairs/"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "FTMO Challenge $100k <-> Exness Hedge",
        "ftmo_account_id": "ftmo_001",
        "exness_account_id": "exness_001",
        "ratio": 1.0,
    }
    body.update(overrides)
    return body


# ---------- auth ----------


@pytest.mark.asyncio
async def test_unauth_list_returns_401(client: AsyncClient) -> None:
    resp = await client.get(PAIR_PATH)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unauth_create_returns_401(client: AsyncClient) -> None:
    resp = await client.post(PAIR_PATH, json=_create_body())
    assert resp.status_code == 401


# ---------- list (empty) ----------


@pytest.mark.asyncio
async def test_list_empty_returns_empty_array(authed_client: AsyncClient) -> None:
    resp = await authed_client.get(PAIR_PATH)
    assert resp.status_code == 200
    assert resp.json() == []


# ---------- create ----------


@pytest.mark.asyncio
async def test_create_pair_success(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(PAIR_PATH, json=_create_body())
    assert resp.status_code == 201
    body = resp.json()
    assert UUID_RE.match(body["pair_id"])
    assert body["name"] == "FTMO Challenge $100k <-> Exness Hedge"
    assert body["ftmo_account_id"] == "ftmo_001"
    assert body["exness_account_id"] == "exness_001"
    assert body["ratio"] == 1.0
    assert body["created_at"] == body["updated_at"]
    assert body["created_at"] > 0


@pytest.mark.asyncio
async def test_create_invalid_name_too_long(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(PAIR_PATH, json=_create_body(name="x" * 101))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_invalid_name_empty(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(PAIR_PATH, json=_create_body(name=""))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_invalid_ratio_zero(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(PAIR_PATH, json=_create_body(ratio=0))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_invalid_ratio_too_high(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(PAIR_PATH, json=_create_body(ratio=11))
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_missing_field(authed_client: AsyncClient) -> None:
    body = _create_body()
    body.pop("ftmo_account_id")
    resp = await authed_client.post(PAIR_PATH, json=body)
    assert resp.status_code == 422


# ---------- get ----------


@pytest.mark.asyncio
async def test_get_existing(authed_client: AsyncClient) -> None:
    create = await authed_client.post(PAIR_PATH, json=_create_body())
    pair_id = create.json()["pair_id"]
    resp = await authed_client.get(f"{PAIR_PATH}{pair_id}")
    assert resp.status_code == 200
    assert resp.json()["pair_id"] == pair_id


@pytest.mark.asyncio
async def test_get_not_found(authed_client: AsyncClient) -> None:
    resp = await authed_client.get(f"{PAIR_PATH}00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Pair not found"


# ---------- list (populated) ----------


@pytest.mark.asyncio
async def test_list_after_create_returns_one(authed_client: AsyncClient) -> None:
    await authed_client.post(PAIR_PATH, json=_create_body())
    resp = await authed_client.get(PAIR_PATH)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_list_sorted_by_created_at_desc(authed_client: AsyncClient) -> None:
    """Create three pairs (with tiny delays) and verify newest-first order."""
    import asyncio

    ids: list[str] = []
    for i in range(3):
        resp = await authed_client.post(PAIR_PATH, json=_create_body(name=f"Pair {i}"))
        ids.append(resp.json()["pair_id"])
        await asyncio.sleep(0.005)  # ensure created_at differs

    listing = await authed_client.get(PAIR_PATH)
    assert listing.status_code == 200
    listed_ids = [p["pair_id"] for p in listing.json()]
    # Newest first → reverse of insertion order.
    assert listed_ids == list(reversed(ids))


# ---------- update ----------


@pytest.mark.asyncio
async def test_update_partial_only_changes_provided_fields(
    authed_client: AsyncClient,
) -> None:
    create = await authed_client.post(PAIR_PATH, json=_create_body())
    original = create.json()
    pair_id = original["pair_id"]

    # Tiny sleep so the new updated_at is strictly greater.
    import asyncio

    await asyncio.sleep(0.005)

    resp = await authed_client.patch(f"{PAIR_PATH}{pair_id}", json={"name": "Renamed"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["ftmo_account_id"] == original["ftmo_account_id"]
    assert body["exness_account_id"] == original["exness_account_id"]
    assert body["ratio"] == original["ratio"]
    assert body["created_at"] == original["created_at"]
    assert body["updated_at"] > original["updated_at"]


@pytest.mark.asyncio
async def test_update_no_fields_returns_400(authed_client: AsyncClient) -> None:
    create = await authed_client.post(PAIR_PATH, json=_create_body())
    pair_id = create.json()["pair_id"]
    resp = await authed_client.patch(f"{PAIR_PATH}{pair_id}", json={})
    assert resp.status_code == 400
    assert "field" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_not_found(authed_client: AsyncClient) -> None:
    resp = await authed_client.patch(
        f"{PAIR_PATH}00000000-0000-0000-0000-000000000000", json={"ratio": 1.5}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_validates_field_value(authed_client: AsyncClient) -> None:
    create = await authed_client.post(PAIR_PATH, json=_create_body())
    pair_id = create.json()["pair_id"]
    resp = await authed_client.patch(f"{PAIR_PATH}{pair_id}", json={"ratio": 15})
    assert resp.status_code == 422


# ---------- delete ----------


@pytest.mark.asyncio
async def test_delete_success_then_get_404(authed_client: AsyncClient) -> None:
    create = await authed_client.post(PAIR_PATH, json=_create_body())
    pair_id = create.json()["pair_id"]
    delete_resp = await authed_client.delete(f"{PAIR_PATH}{pair_id}")
    assert delete_resp.status_code == 204
    assert delete_resp.content == b""
    follow = await authed_client.get(f"{PAIR_PATH}{pair_id}")
    assert follow.status_code == 404


@pytest.mark.asyncio
async def test_delete_not_found(authed_client: AsyncClient) -> None:
    resp = await authed_client.delete(f"{PAIR_PATH}00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# ---------- step 3.13: delete guard against in-use pair ----------


async def _seed_pending_order_referencing(fake_redis: Any, *, order_id: str, pair_id: str) -> None:
    """Minimal order HASH + ``orders:by_status:pending`` SET entry — the
    exact shape ``count_orders_by_pair`` looks at."""
    await fake_redis.hset(
        f"order:{order_id}",
        mapping={
            "order_id": order_id,
            "pair_id": pair_id,
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "exness_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "status": "pending",
            "p_status": "pending",
            "p_volume_lots": "0.01",
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )
    await fake_redis.sadd("orders:by_status:pending", order_id)


@pytest.mark.asyncio
async def test_delete_pair_with_open_order_blocked(
    authed_client: AsyncClient, fake_redis: Any
) -> None:
    """Step 3.13: deleting a pair while an order references it must
    return 409 ``pair_in_use`` so the operator gets a clear "close
    those orders first" message instead of an orphaned reference."""
    create = await authed_client.post(PAIR_PATH, json=_create_body())
    pair_id = create.json()["pair_id"]
    await _seed_pending_order_referencing(fake_redis, order_id="ord_a", pair_id=pair_id)

    resp = await authed_client.delete(f"{PAIR_PATH}{pair_id}")

    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["error_code"] == "pair_in_use"
    assert "1 order(s)" in body["detail"]["message"]
    # And the pair itself is still there.
    follow = await authed_client.get(f"{PAIR_PATH}{pair_id}")
    assert follow.status_code == 200


@pytest.mark.asyncio
async def test_delete_pair_no_references_succeeds(
    authed_client: AsyncClient, fake_redis: Any
) -> None:
    """Negative-space test: an order referencing a DIFFERENT pair must
    not prevent deletion of the target. Pins that the guard filters by
    ``pair_id`` rather than counting all open orders."""
    create_target = await authed_client.post(PAIR_PATH, json=_create_body(name="target"))
    target_id = create_target.json()["pair_id"]
    create_other = await authed_client.post(PAIR_PATH, json=_create_body(name="other"))
    other_id = create_other.json()["pair_id"]
    # The order references "other", not "target".
    await _seed_pending_order_referencing(fake_redis, order_id="ord_b", pair_id=other_id)

    resp = await authed_client.delete(f"{PAIR_PATH}{target_id}")

    assert resp.status_code == 204
    follow = await authed_client.get(f"{PAIR_PATH}{target_id}")
    assert follow.status_code == 404
