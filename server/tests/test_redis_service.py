"""Direct-method tests for ``RedisService`` (step 3.12).

Covers the new ``get_all_accounts_with_status`` aggregation helper.
The composer ties together three existing methods
(``get_all_account_ids`` / ``get_account_meta`` / ``get_client_status``
/ ``get_account_info``) plus the enabled-flag override, so the unit
tests here focus on the merge logic + sort order + default-fallback
behaviour rather than re-testing each underlying method.

Other ``RedisService`` methods are exercised indirectly by the
endpoint-level test suites (test_orders_api, test_positions_api,
test_pairs, …). This file exists specifically for helpers that
aggregate across multiple Redis keys.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


async def _seed_account_info(
    redis_client: fakeredis.aioredis.FakeRedis,
    broker: str,
    account_id: str,
    *,
    balance: str = "1000000",
    equity: str = "1005000",
    margin: str = "0",
    free_margin: str = "1005000",
    currency: str = "USD",
    money_digits: str = "2",
) -> None:
    await redis_client.hset(  # type: ignore[misc]
        f"account:{broker}:{account_id}",
        mapping={
            "balance": balance,
            "equity": equity,
            "margin": margin,
            "free_margin": free_margin,
            "currency": currency,
            "money_digits": money_digits,
        },
    )


# ---------- get_all_accounts_with_status ----------


@pytest.mark.asyncio
async def test_get_all_accounts_with_status_empty(redis_svc: RedisService) -> None:
    """No accounts registered → empty list (no exception, no defaults)."""
    rows = await redis_svc.get_all_accounts_with_status()
    assert rows == []


@pytest.mark.asyncio
async def test_get_all_accounts_with_status_single_online(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """One enabled FTMO account + heartbeat present + account info
    populated → status=online and every money field reflects the seeded
    raw values (string, money_digits-scaled int convention)."""
    await redis_svc.add_account("ftmo", "ftmo_001", name="primary", enabled=True)
    # Heartbeat key — only existence is checked (string vs HASH irrelevant).
    await redis_client.set("client:ftmo:ftmo_001", "1", ex=30)
    await _seed_account_info(
        redis_client,
        "ftmo",
        "ftmo_001",
        balance="1000000",
        equity="1005000",
        free_margin="1005000",
    )

    rows = await redis_svc.get_all_accounts_with_status()

    assert len(rows) == 1
    row = rows[0]
    assert row["broker"] == "ftmo"
    assert row["account_id"] == "ftmo_001"
    assert row["name"] == "primary"
    assert row["enabled"] == "true"
    assert row["status"] == "online"
    assert row["balance_raw"] == "1000000"
    assert row["equity_raw"] == "1005000"
    assert row["margin_raw"] == "0"
    assert row["free_margin_raw"] == "1005000"
    assert row["currency"] == "USD"
    assert row["money_digits"] == "2"


@pytest.mark.asyncio
async def test_get_all_accounts_with_status_offline_when_no_heartbeat(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """Registered + enabled but no heartbeat key → status=offline. Money
    fields still emit their defaults (``"0"`` / ``"USD"`` / ``"2"``)
    when ``account:{broker}:{id}`` HASH is absent — covers the
    "FTMO client started but never ran account_info_loop yet" case."""
    await redis_svc.add_account("ftmo", "ftmo_001", name="offline_acc", enabled=True)
    # Do NOT seed heartbeat. Do NOT seed account info.

    rows = await redis_svc.get_all_accounts_with_status()

    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "offline"
    assert row["balance_raw"] == "0"
    assert row["equity_raw"] == "0"
    assert row["margin_raw"] == "0"
    assert row["free_margin_raw"] == "0"
    assert row["currency"] == "USD"
    assert row["money_digits"] == "2"


@pytest.mark.asyncio
async def test_get_all_accounts_with_status_disabled_overrides_heartbeat(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """enabled=false in account_meta → status=disabled even when the
    heartbeat key is present. Operator-side override takes precedence
    so a paused account can't accidentally take new orders."""
    await redis_svc.add_account("ftmo", "ftmo_001", name="paused", enabled=False)
    await redis_client.set("client:ftmo:ftmo_001", "1", ex=30)
    await _seed_account_info(redis_client, "ftmo", "ftmo_001")

    rows = await redis_svc.get_all_accounts_with_status()

    assert len(rows) == 1
    assert rows[0]["status"] == "disabled"
    assert rows[0]["enabled"] == "false"
    # Money fields still surface (so the UI can show last-known balance
    # even while disabled).
    assert rows[0]["balance_raw"] == "1000000"


@pytest.mark.asyncio
async def test_get_all_accounts_with_status_sorted_ftmo_first_then_by_account_id(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """Mixed broker + multiple accounts → output is ftmo block first
    (sorted by account_id asc), then exness block (sorted by
    account_id asc). Deterministic order for both UI and tests."""
    # Insert in jumbled order to verify sorting really fires.
    await redis_svc.add_account("ftmo", "ftmo_002", name="ftmo2")
    await redis_svc.add_account("exness", "exness_001", name="ex1")
    await redis_svc.add_account("ftmo", "ftmo_001", name="ftmo1")
    await redis_svc.add_account("exness", "exness_002", name="ex2")
    # Heartbeat for half of them — irrelevant to sort order.
    await redis_client.set("client:ftmo:ftmo_001", "1", ex=30)
    await redis_client.set("client:exness:exness_001", "1", ex=30)

    rows = await redis_svc.get_all_accounts_with_status()

    assert [(r["broker"], r["account_id"]) for r in rows] == [
        ("ftmo", "ftmo_001"),
        ("ftmo", "ftmo_002"),
        ("exness", "exness_001"),
        ("exness", "exness_002"),
    ]
