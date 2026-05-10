"""Tests for the Phase 3.1 RedisService extensions.

Covers stream/consumer-group helpers, pending tracking, order CRUD with
atomic CAS, position P&L cache + snapshots, heartbeat, account
management, and settings. Phase 1+2 surface (creds, symbol_config, ohlc,
tick, pairs) is exercised by the existing test suite and not duplicated
here.

All tests use the autouse ``fake_redis`` fixture from
``server/tests/conftest.py`` for an in-process FakeRedis with Lua + Streams
support. No network IO.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService


@pytest.fixture
def svc(fake_redis: fakeredis.aioredis.FakeRedis) -> RedisService:
    """RedisService bound to a fresh fakeredis per test."""
    return RedisService(fake_redis)


# ---------------------------------------------------------------------------
# Stream / consumer-group helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_group_creates_stream_and_group(svc: RedisService) -> None:
    await svc._create_group("cmd_stream:ftmo:acc_001", "ftmo-acc_001")
    # XINFO GROUPS confirms the group exists on the stream.
    groups = await svc._redis.xinfo_groups("cmd_stream:ftmo:acc_001")
    names = [g["name"] for g in groups]
    assert "ftmo-acc_001" in names


@pytest.mark.asyncio
async def test_create_group_idempotent_swallows_busygroup(svc: RedisService) -> None:
    await svc._create_group("cmd_stream:ftmo:acc_001", "ftmo-acc_001")
    # Second call must not raise — that would crash any lifespan setup.
    await svc._create_group("cmd_stream:ftmo:acc_001", "ftmo-acc_001")


@pytest.mark.asyncio
async def test_setup_consumer_groups_creates_three_streams_per_account(
    svc: RedisService,
) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    await svc.add_account("exness", "exn_001", "Exness 1")

    # Step 3.2: setup_consumer_groups now returns (ftmo_count, exness_count).
    counts = await svc.setup_consumer_groups()
    assert counts == (1, 1)

    for stream, group in [
        ("cmd_stream:ftmo:acc_001", "ftmo-acc_001"),
        ("resp_stream:ftmo:acc_001", "server"),
        ("event_stream:ftmo:acc_001", "server"),
        ("cmd_stream:exness:exn_001", "exness-exn_001"),
        ("resp_stream:exness:exn_001", "server"),
        ("event_stream:exness:exn_001", "server"),
    ]:
        groups = await svc._redis.xinfo_groups(stream)
        assert any(g["name"] == group for g in groups), f"missing {group} on {stream}"


@pytest.mark.asyncio
async def test_setup_consumer_groups_idempotent_second_call(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    counts1 = await svc.setup_consumer_groups()
    # Re-running on existing groups must succeed (every BUSYGROUP swallowed)
    # and return the same counts (idempotent: account membership unchanged).
    counts2 = await svc.setup_consumer_groups()
    assert counts1 == counts2 == (1, 0)


@pytest.mark.asyncio
async def test_setup_consumer_groups_zero_accounts_returns_zeros(
    svc: RedisService,
) -> None:
    """First-boot state: no accounts → no groups, returns (0, 0). Server must still start."""
    counts = await svc.setup_consumer_groups()
    assert counts == (0, 0)


@pytest.mark.asyncio
async def test_push_command_xadds_and_zadds_pending(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")

    request_id = await svc.push_command(
        "ftmo", "acc_001", {"action": "open", "order_id": "ord_xyz", "side": "buy"}
    )

    # request_id is a 32-char hex uuid.
    assert len(request_id) == 32 and all(c in "0123456789abcdef" for c in request_id)

    # XADD landed.
    msgs = await svc._redis.xrange("cmd_stream:ftmo:acc_001", "-", "+")
    assert len(msgs) == 1
    msg_id, fields = msgs[0]
    assert fields["request_id"] == request_id
    assert fields["action"] == "open"
    assert fields["order_id"] == "ord_xyz"
    assert int(fields["created_at"]) > 0

    # ZADD pending tracking.
    score = await svc._redis.zscore("pending_cmds:ftmo:acc_001", request_id)
    assert score is not None and int(score) == int(fields["created_at"])


@pytest.mark.asyncio
async def test_push_command_does_not_mutate_caller_dict(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    fields = {"action": "open", "order_id": "ord_xyz"}

    await svc.push_command("ftmo", "acc_001", fields)

    # Caller's dict must not gain request_id or created_at — push_command
    # works on a copy.
    assert "request_id" not in fields
    assert "created_at" not in fields


@pytest.mark.asyncio
async def test_push_command_rejects_bad_broker(svc: RedisService) -> None:
    with pytest.raises(ValueError, match="broker must be one of"):
        await svc.push_command("WRONG", "acc_001", {"action": "open"})


@pytest.mark.asyncio
async def test_push_command_rejects_bad_account_id(svc: RedisService) -> None:
    with pytest.raises(ValueError, match="account_id"):
        await svc.push_command("ftmo", "Bad-ID!", {"action": "open"})


@pytest.mark.asyncio
async def test_read_responses_returns_pending_messages(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    await svc.setup_consumer_groups()
    # Producer (the FTMO client) writes a response.
    await svc._redis.xadd("resp_stream:ftmo:acc_001", {"request_id": "abc", "status": "filled"})

    result = await svc.read_responses("ftmo", "acc_001", count=10, block_ms=10)

    # Shape: [(stream_name, [(msg_id, fields), ...])]
    assert len(result) == 1
    stream_name, msgs = result[0]
    assert stream_name == "resp_stream:ftmo:acc_001"
    assert len(msgs) == 1
    _, fields = msgs[0]
    assert fields["request_id"] == "abc"


@pytest.mark.asyncio
async def test_read_responses_returns_empty_when_no_messages(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    await svc.setup_consumer_groups()

    # Use block_ms=10 to avoid hanging the test.
    result = await svc.read_responses("ftmo", "acc_001", count=10, block_ms=10)
    assert result == []


@pytest.mark.asyncio
async def test_read_events_routes_to_event_stream(svc: RedisService) -> None:
    await svc.add_account("exness", "exn_001", "Exness 1")
    await svc.setup_consumer_groups()
    await svc._redis.xadd(
        "event_stream:exness:exn_001", {"type": "position_closed", "ticket": "12345"}
    )

    result = await svc.read_events("exness", "exn_001", count=10, block_ms=10)
    assert len(result) == 1
    stream_name, _ = result[0]
    assert stream_name == "event_stream:exness:exn_001"


@pytest.mark.asyncio
async def test_ack_marks_message_consumed(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    await svc.setup_consumer_groups()
    await svc._redis.xadd("resp_stream:ftmo:acc_001", {"r": "1"})

    [(_, msgs)] = await svc.read_responses("ftmo", "acc_001", count=10, block_ms=10)
    msg_id = msgs[0][0]

    await svc.ack("resp_stream:ftmo:acc_001", "server", msg_id)

    # Pending entries for the consumer should now be 0.
    pending = await svc._redis.xpending("resp_stream:ftmo:acc_001", "server")
    assert pending["pending"] == 0


# ---------------------------------------------------------------------------
# Pending command tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_pending_zrems_request_id(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    rid = await svc.push_command("ftmo", "acc_001", {"action": "open"})

    await svc.remove_pending("ftmo", "acc_001", rid)

    score = await svc._redis.zscore("pending_cmds:ftmo:acc_001", rid)
    assert score is None


@pytest.mark.asyncio
async def test_get_stuck_pending_excludes_boundary(svc: RedisService) -> None:
    """An entry exactly at the cutoff is NOT stuck (cutoff is exclusive)."""
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    now_ms = int(time.time() * 1000)
    # 30s old vs 5s old.
    await svc._redis.zadd(
        "pending_cmds:ftmo:acc_001",
        {"old_request": now_ms - 30_000, "fresh_request": now_ms - 5_000},
    )

    stuck = await svc.get_stuck_pending("ftmo", "acc_001", max_age_seconds=10)

    rids = [r for r, _ in stuck]
    assert "old_request" in rids
    assert "fresh_request" not in rids


@pytest.mark.asyncio
async def test_get_stuck_pending_returns_age_in_ms(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    now_ms = int(time.time() * 1000)
    await svc._redis.zadd("pending_cmds:ftmo:acc_001", {"r1": now_ms - 60_000})

    stuck = await svc.get_stuck_pending("ftmo", "acc_001", max_age_seconds=10)

    assert len(stuck) == 1
    rid, age_ms = stuck[0]
    assert rid == "r1"
    # Age is roughly 60s ± a few ms scheduling jitter.
    assert 59_000 <= age_ms <= 61_000


@pytest.mark.asyncio
async def test_get_all_account_pairs_lists_both_brokers(svc: RedisService) -> None:
    await svc.add_account("ftmo", "ftmo_001", "FTMO 1")
    await svc.add_account("ftmo", "ftmo_002", "FTMO 2")
    await svc.add_account("exness", "exn_001", "Exness 1")

    pairs = await svc.get_all_account_pairs()

    assert sorted(pairs) == [
        ("exness", "exn_001"),
        ("ftmo", "ftmo_001"),
        ("ftmo", "ftmo_002"),
    ]


# ---------------------------------------------------------------------------
# Order CRUD
# ---------------------------------------------------------------------------


def _basic_order_fields(status: str = "pending") -> dict[str, str]:
    return {
        "order_id": "ord_xyz",
        "pair_id": "pair_main",
        "ftmo_account_id": "ftmo_001",
        "exness_account_id": "exn_001",
        "symbol": "EURUSD",
        "side": "buy",
        "status": status,
        "risk_amount": "100",
        "secondary_ratio": "1.0",
        "created_at": "1735000000000",
        "updated_at": "1735000000000",
    }


@pytest.mark.asyncio
async def test_create_order_writes_hash_and_status_index(svc: RedisService) -> None:
    await svc.create_order("ord_xyz", _basic_order_fields("pending"))

    order = await svc.get_order("ord_xyz")
    assert order is not None
    assert order["order_id"] == "ord_xyz"
    assert order["status"] == "pending"

    members = await svc._redis.smembers("orders:by_status:pending")  # type: ignore[misc]
    assert "ord_xyz" in members


@pytest.mark.asyncio
async def test_create_order_requires_status_field(svc: RedisService) -> None:
    fields = _basic_order_fields()
    del fields["status"]
    with pytest.raises(ValueError, match="requires fields"):
        await svc.create_order("ord_xyz", fields)


@pytest.mark.asyncio
async def test_get_order_returns_none_when_missing(svc: RedisService) -> None:
    assert await svc.get_order("ord_does_not_exist") is None


@pytest.mark.asyncio
async def test_update_order_no_cas_simple_patch(svc: RedisService) -> None:
    await svc.create_order("ord_xyz", _basic_order_fields("pending"))

    ok = await svc.update_order("ord_xyz", {"sl_price": "1.08200"})
    assert ok is True

    order = await svc.get_order("ord_xyz")
    assert order is not None and order["sl_price"] == "1.08200"
    assert order["status"] == "pending"  # unchanged


@pytest.mark.asyncio
async def test_update_order_no_cas_with_status_change_swaps_index(
    svc: RedisService,
) -> None:
    await svc.create_order("ord_xyz", _basic_order_fields("pending"))

    ok = await svc.update_order("ord_xyz", {"status": "open", "p_status": "filled"})
    assert ok is True

    pending = await svc._redis.smembers("orders:by_status:pending")  # type: ignore[misc]
    open_set = await svc._redis.smembers("orders:by_status:open")  # type: ignore[misc]
    assert "ord_xyz" not in pending
    assert "ord_xyz" in open_set


@pytest.mark.asyncio
async def test_update_order_cas_hit_applies_patch(svc: RedisService) -> None:
    await svc.create_order("ord_xyz", _basic_order_fields("pending"))

    ok = await svc.update_order(
        "ord_xyz",
        {"status": "open", "p_status": "filled"},
        old_status="pending",
    )
    assert ok is True

    order = await svc.get_order("ord_xyz")
    assert order is not None and order["status"] == "open"


@pytest.mark.asyncio
async def test_update_order_cas_miss_returns_false_no_changes(svc: RedisService) -> None:
    await svc.create_order("ord_xyz", _basic_order_fields("open"))

    ok = await svc.update_order(
        "ord_xyz",
        {"status": "closed", "closed_at": "1735999999999"},
        old_status="pending",  # WRONG — current is "open"
    )
    assert ok is False

    # Hash + index unchanged.
    order = await svc.get_order("ord_xyz")
    assert order is not None and order["status"] == "open"
    assert "closed_at" not in order
    open_set = await svc._redis.smembers("orders:by_status:open")  # type: ignore[misc]
    assert "ord_xyz" in open_set


@pytest.mark.asyncio
async def test_update_order_returns_false_when_order_missing(svc: RedisService) -> None:
    ok = await svc.update_order("ord_nope", {"status": "open"})
    assert ok is False
    # Critically: must NOT create the hash from the patch.
    assert await svc.get_order("ord_nope") is None


@pytest.mark.asyncio
async def test_list_orders_by_status(svc: RedisService) -> None:
    await svc.create_order("ord_a", {**_basic_order_fields("open"), "order_id": "ord_a"})
    await svc.create_order("ord_b", {**_basic_order_fields("open"), "order_id": "ord_b"})
    await svc.create_order("ord_c", {**_basic_order_fields("pending"), "order_id": "ord_c"})

    open_orders = await svc.list_orders_by_status("open")
    assert {o["order_id"] for o in open_orders} == {"ord_a", "ord_b"}

    pending_orders = await svc.list_orders_by_status("pending")
    assert {o["order_id"] for o in pending_orders} == {"ord_c"}

    assert await svc.list_orders_by_status("nonexistent") == []


@pytest.mark.asyncio
async def test_add_to_closed_history_and_list_paginates(svc: RedisService) -> None:
    # Seed 5 closed orders with strictly increasing close timestamps.
    for i, oid in enumerate(["ord_1", "ord_2", "ord_3", "ord_4", "ord_5"]):
        await svc.create_order(oid, {**_basic_order_fields("closed"), "order_id": oid})
        await svc.add_to_closed_history(oid, 1735000000000 + i * 1000)

    # ZREVRANGE (newest first): ord_5, ord_4, ord_3, ord_2, ord_1.
    page1 = await svc.list_closed_orders(limit=2, offset=0)
    assert [o["order_id"] for o in page1] == ["ord_5", "ord_4"]

    page2 = await svc.list_closed_orders(limit=2, offset=2)
    assert [o["order_id"] for o in page2] == ["ord_3", "ord_2"]

    page3 = await svc.list_closed_orders(limit=2, offset=4)
    assert [o["order_id"] for o in page3] == ["ord_1"]


@pytest.mark.asyncio
async def test_list_closed_orders_validates_pagination(svc: RedisService) -> None:
    with pytest.raises(ValueError):
        await svc.list_closed_orders(limit=-1, offset=0)
    with pytest.raises(ValueError):
        await svc.list_closed_orders(limit=10, offset=-1)
    # limit=0 is a defined no-op (returns []).
    assert await svc.list_closed_orders(limit=0, offset=0) == []


@pytest.mark.asyncio
async def test_link_request_to_order_and_lookup(svc: RedisService) -> None:
    await svc.link_request_to_order("req_abc", "ord_xyz")

    assert await svc.find_order_by_request_id("req_abc") == "ord_xyz"
    assert await svc.find_order_by_request_id("req_unknown") is None


@pytest.mark.asyncio
async def test_link_request_to_order_sets_ttl_by_default(svc: RedisService) -> None:
    await svc.link_request_to_order("req_abc", "ord_xyz")
    ttl = await svc._redis.ttl("request_id_to_order:req_abc")
    # Default 86400s (24h); fakeredis returns positive int while live.
    assert 86000 < ttl <= 86400


@pytest.mark.asyncio
async def test_link_broker_order_id_for_both_legs(svc: RedisService) -> None:
    await svc.create_order("ord_xyz", {**_basic_order_fields("open"), "order_id": "ord_xyz"})
    await svc.link_broker_order_id("p", "ctrader_pos_111", "ord_xyz")
    await svc.link_broker_order_id("s", "mt5_ticket_222", "ord_xyz")

    p_order = await svc.find_order_by_p_broker_order_id("ctrader_pos_111")
    assert p_order is not None and p_order["order_id"] == "ord_xyz"

    s_order = await svc.find_order_by_s_broker_order_id("mt5_ticket_222")
    assert s_order is not None and s_order["order_id"] == "ord_xyz"

    # Unknown broker id returns None.
    assert await svc.find_order_by_p_broker_order_id("missing") is None
    assert await svc.find_order_by_s_broker_order_id("missing") is None


@pytest.mark.asyncio
async def test_link_broker_order_id_rejects_invalid_leg(svc: RedisService) -> None:
    with pytest.raises(ValueError, match="leg must be"):
        await svc.link_broker_order_id("x", "anything", "ord_xyz")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Position P&L cache + snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_position_pnl_setex_with_ttl(svc: RedisService) -> None:
    snap: dict[str, Any] = {
        "order_id": "ord_xyz",
        "symbol": "EURUSD",
        "p_pnl_usd": 5.20,
        "s_pnl_usd": -5.10,
        "total_pnl_usd": 0.10,
        "p_current_price": 1.08512,
        "s_current_price": 1.08510,
        "computed_at": 1735000060000,
    }
    await svc.set_position_pnl("ord_xyz", snap)

    raw = await svc._redis.get("position:ord_xyz")
    assert raw is not None
    assert json.loads(raw) == snap

    ttl = await svc._redis.ttl("position:ord_xyz")
    # docs §11: 600s.
    assert 590 < ttl <= 600


@pytest.mark.asyncio
async def test_get_position_pnl_hit_and_miss(svc: RedisService) -> None:
    snap = {"order_id": "ord_xyz", "total_pnl_usd": 1.5, "computed_at": 1}
    await svc.set_position_pnl("ord_xyz", snap)

    assert await svc.get_position_pnl("ord_xyz") == snap
    assert await svc.get_position_pnl("ord_missing") is None


@pytest.mark.asyncio
async def test_add_snapshot_and_get_returns_ordered_history(svc: RedisService) -> None:
    await svc.add_snapshot("ord_xyz", 1000, 0.5)
    await svc.add_snapshot("ord_xyz", 2000, 1.5)
    await svc.add_snapshot("ord_xyz", 3000, -0.25)

    snaps = await svc.get_snapshots("ord_xyz")
    assert snaps == [(1000, 0.5), (2000, 1.5), (3000, -0.25)]

    # TTL refreshed on each add (lazy refresh).
    ttl = await svc._redis.ttl("order:ord_xyz:snaps")
    assert 590 < ttl <= 600


@pytest.mark.asyncio
async def test_get_snapshots_empty_for_missing_order(svc: RedisService) -> None:
    assert await svc.get_snapshots("ord_no_history") == []


# ---------------------------------------------------------------------------
# Heartbeat & account info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_client_status_online_when_heartbeat_key_present(
    svc: RedisService,
) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    await svc._redis.setex("client:ftmo:acc_001", 30, "alive")

    assert await svc.get_client_status("ftmo", "acc_001") == "online"


@pytest.mark.asyncio
async def test_get_client_status_offline_when_missing(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    assert await svc.get_client_status("ftmo", "acc_001") == "offline"


@pytest.mark.asyncio
async def test_get_all_client_statuses(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    await svc.add_account("exness", "exn_001", "Exness 1")
    await svc._redis.setex("client:ftmo:acc_001", 30, "alive")

    statuses = await svc.get_all_client_statuses()
    assert statuses == {"ftmo:acc_001": "online", "exness:exn_001": "offline"}


@pytest.mark.asyncio
async def test_get_account_info_returns_balance_hash(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    await svc._redis.hset(  # type: ignore[misc]
        "account:ftmo:acc_001",
        mapping={"balance": "50012.34", "equity": "50050.12", "currency": "USD"},
    )

    info = await svc.get_account_info("ftmo", "acc_001")
    assert info == {"balance": "50012.34", "equity": "50050.12", "currency": "USD"}


@pytest.mark.asyncio
async def test_get_account_info_returns_none_when_missing(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    assert await svc.get_account_info("ftmo", "acc_001") is None


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_account_writes_set_and_meta_pipeline(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO Challenge $100k", enabled=True)

    assert await svc._redis.sismember("accounts:ftmo", "acc_001")  # type: ignore[misc]
    meta = await svc.get_account_meta("ftmo", "acc_001")
    assert meta is not None
    assert meta["name"] == "FTMO Challenge $100k"
    assert meta["enabled"] == "true"
    assert int(meta["created_at"]) > 0


@pytest.mark.asyncio
async def test_add_account_disabled_flag(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1", enabled=False)
    meta = await svc.get_account_meta("ftmo", "acc_001")
    assert meta is not None and meta["enabled"] == "false"


@pytest.mark.asyncio
async def test_add_account_rejects_bad_broker(svc: RedisService) -> None:
    with pytest.raises(ValueError, match="broker must be one of"):
        await svc.add_account("invalid", "acc_001", "X")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    ["", "ab", "a" * 65, "Invalid-Caps", "with spaces", "MIXEDCase", "kebab-case"],
)
async def test_add_account_rejects_bad_account_id(svc: RedisService, bad_id: str) -> None:
    with pytest.raises(ValueError, match="account_id"):
        await svc.add_account("ftmo", bad_id, "X")


@pytest.mark.asyncio
async def test_remove_account_drops_meta_and_heartbeat(svc: RedisService) -> None:
    await svc.add_account("ftmo", "acc_001", "FTMO 1")
    await svc._redis.setex("client:ftmo:acc_001", 30, "alive")
    await svc._redis.hset(  # type: ignore[misc]
        "account:ftmo:acc_001", mapping={"balance": "1000"}
    )

    await svc.remove_account("ftmo", "acc_001")

    assert not await svc._redis.sismember("accounts:ftmo", "acc_001")  # type: ignore[misc]
    assert await svc.get_account_meta("ftmo", "acc_001") is None
    assert await svc.get_account_info("ftmo", "acc_001") is None
    assert await svc.get_client_status("ftmo", "acc_001") == "offline"


@pytest.mark.asyncio
async def test_get_all_account_ids_sorted(svc: RedisService) -> None:
    await svc.add_account("ftmo", "z_acc", "Z")
    await svc.add_account("ftmo", "a_acc", "A")
    await svc.add_account("ftmo", "m_acc", "M")

    assert await svc.get_all_account_ids("ftmo") == ["a_acc", "m_acc", "z_acc"]


@pytest.mark.asyncio
async def test_get_all_account_ids_empty(svc: RedisService) -> None:
    assert await svc.get_all_account_ids("exness") == []


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_settings_empty_when_missing(svc: RedisService) -> None:
    assert await svc.get_settings() == {}


@pytest.mark.asyncio
async def test_patch_settings_returns_full_hash(svc: RedisService) -> None:
    out = await svc.patch_settings({"default_secondary_ratio": "1.5"})
    assert out == {"default_secondary_ratio": "1.5"}

    # A second patch merges, doesn't replace.
    out2 = await svc.patch_settings({"primary_fill_timeout_seconds": "30"})
    assert out2 == {
        "default_secondary_ratio": "1.5",
        "primary_fill_timeout_seconds": "30",
    }


@pytest.mark.asyncio
async def test_patch_settings_empty_dict_is_noop(svc: RedisService) -> None:
    # Seeding via patch first.
    await svc.patch_settings({"k": "v"})
    out = await svc.patch_settings({})
    assert out == {"k": "v"}


# ---------------------------------------------------------------------------
# Concurrency: CAS keeps two writers from clobbering each other
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_update_order_cas_only_one_winner(svc: RedisService) -> None:
    """Two coroutines try to transition pending→open at once. Exactly one wins."""
    await svc.create_order("ord_xyz", _basic_order_fields("pending"))

    async def transition() -> bool:
        return await svc.update_order(
            "ord_xyz",
            {"status": "open", "p_status": "filled"},
            old_status="pending",
        )

    results = await asyncio.gather(transition(), transition())
    # Lua is single-threaded; one True, one False.
    assert sorted(results) == [False, True]
    order = await svc.get_order("ord_xyz")
    assert order is not None and order["status"] == "open"
