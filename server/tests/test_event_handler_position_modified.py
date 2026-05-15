"""Step 4.7b — event_handler position_modified WARNING tests.

Filter contract for the Exness ``position_modified`` event:

  - changed_fields == "volume" only → silent ignore (DEBUG log, no
    alert). Phase 4 has no partial-close support.
  - SL or TP changed (with or without volume) → WARNING alert with the
    delta in body_vi.
  - Unknown change_fields → log warning + drop.
  - Order not found → log warning + drop, no crash.
  - cooldown_key disambiguates SL-only vs TP-only vs both so
    independent edits don't suppress each other.

Phase 4 R3 guarantee: NO modify cmd published to ``cmd_stream:exness:*``
in any branch.
"""

from __future__ import annotations

import json
from typing import Any

import fakeredis.aioredis
import pytest
from app.services.alert_service import AlertService
from app.services.broadcast import BroadcastService
from app.services.event_handler import _handle_event_entry
from app.services.redis_service import RedisService


class _CapturingBroadcast(BroadcastService):
    def __init__(self) -> None:
        super().__init__(redis_svc=None)
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        self.published.append((channel, data))


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


@pytest.fixture
def broadcast() -> _CapturingBroadcast:
    return _CapturingBroadcast()


@pytest.fixture
def alert_svc(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> AlertService:
    return AlertService(redis_svc, broadcast)


async def _seed_hedge_order(
    redis_svc: RedisService,
    *,
    order_id: str = "ord_hedge_1",
    s_broker_order_id: str = "55001",
) -> None:
    await redis_svc.create_order(
        order_id,
        {
            "order_id": order_id,
            "pair_id": "pair_001",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "exness_001",
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "status": "filled",
            "p_status": "filled",
            "s_status": "filled",
            "s_volume_lots": "0.10",
            "s_broker_order_id": s_broker_order_id,
            "s_exness_symbol": "EURUSDz",
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )
    await redis_svc.link_broker_order_id("s", s_broker_order_id, order_id)
    await redis_svc._redis.hset(  # type: ignore[misc]
        "pair:pair_001",
        mapping={"pair_id": "pair_001", "name": "EURUSD-pair", "ftmo_account_id": "ftmo_001"},
    )


def _modified_event(
    *,
    ticket: str = "55001",
    changed_fields: str = "sl",
    old_sl: str = "1.08000",
    new_sl: str = "1.07500",
    old_tp: str = "1.09000",
    new_tp: str = "1.09000",
    old_volume: str = "0.10",
    new_volume: str = "0.10",
) -> dict[str, str]:
    """Mirror position_monitor._publish_event payload for the
    ``position_modified`` event."""
    return {
        "event_type": "position_modified",
        "broker_position_id": ticket,
        "symbol": "EURUSDz",
        "changed_fields": changed_fields,
        "old_sl": old_sl,
        "new_sl": new_sl,
        "old_tp": old_tp,
        "new_tp": new_tp,
        "old_volume": old_volume,
        "new_volume": new_volume,
        "ts_ms": "1735000060000",
    }


# ---------- volume-only silent ignore (criterion #19) ----------


@pytest.mark.asyncio
async def test_volume_only_changed_silent_ignore(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Phase 4 has no partial-close support; a volume-only modify must
    NOT emit a WARNING — operator-side bookkeeping outside our state
    machine."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(
            changed_fields="volume",
            old_volume="0.10", new_volume="0.05",
        ),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert keys == []


# ---------- SL change -> WARNING with SL delta (criterion #20) ----------


@pytest.mark.asyncio
async def test_sl_change_emits_warning_with_delta(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(changed_fields="sl", old_sl="1.0800", new_sl="1.0750"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1
    row = await redis_client.hgetall(keys[0])  # type: ignore[misc]
    assert row["alert_type"] == "hedge_leg_external_modify_warning"
    assert "SL 1.0800→1.0750" in row["body_vi"]
    # TP delta NOT in body when only SL changed.
    assert "TP" not in row["body_vi"]


# ---------- TP change -> WARNING with TP delta (criterion #21) ----------


@pytest.mark.asyncio
async def test_tp_change_emits_warning_with_delta(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(changed_fields="tp", old_tp="1.0900", new_tp="1.0950"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1
    row = await redis_client.hgetall(keys[0])  # type: ignore[misc]
    assert "TP 1.0900→1.0950" in row["body_vi"]
    assert "SL" not in row["body_vi"]


# ---------- SL+TP change -> single WARNING with both deltas (criterion #22) ----------


@pytest.mark.asyncio
async def test_sl_and_tp_change_emits_single_warning(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(
            changed_fields="sl,tp",
            old_sl="1.0800", new_sl="1.0750",
            old_tp="1.0900", new_tp="1.0950",
        ),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1
    row = await redis_client.hgetall(keys[0])  # type: ignore[misc]
    body = row["body_vi"]
    assert "SL 1.0800→1.0750" in body
    assert "TP 1.0900→1.0950" in body


# ---------- volume+SL -> WARNING (volume ignored from body) (criterion #23) ----------


@pytest.mark.asyncio
async def test_volume_and_sl_emits_warning_volume_ignored(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """volume + SL → still emit (SL present), volume change is not
    surfaced in body_vi (Phase 4 doesn't track partial closes)."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(
            changed_fields="volume,sl",
            old_sl="1.0800", new_sl="1.0750",
            old_volume="0.10", new_volume="0.05",
        ),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1
    row = await redis_client.hgetall(keys[0])  # type: ignore[misc]
    assert "SL 1.0800→1.0750" in row["body_vi"]
    assert "volume" not in row["body_vi"].lower()


# ---------- unknown change_fields -> log + drop (criterion #24) ----------


@pytest.mark.asyncio
async def test_unknown_change_fields_no_emit(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(changed_fields="unknown_field_xyz"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert keys == []


# ---------- order not found (criterion #25) ----------


@pytest.mark.asyncio
async def test_order_not_found_logs_no_emit(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Orphan ticket — no seeded order/link. Handler logs warning + returns.
    Crucially, the alert is NOT emitted."""
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(ticket="99999", changed_fields="sl"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert keys == []


# ---------- cooldown disambiguation (criterion #26) ----------


@pytest.mark.asyncio
async def test_cooldown_disambiguates_sl_vs_tp(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """An SL-only edit followed by a TP-only edit on the same order
    should emit TWO alerts — different cooldown keys per signal."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(changed_fields="sl", new_sl="1.0750"),
        broker="exness", alert_service=alert_svc,
    )
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(changed_fields="tp", new_tp="1.0950"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 2


# ---------- second identical change suppressed by cooldown ----------


@pytest.mark.asyncio
async def test_duplicate_sl_change_suppressed(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(changed_fields="sl", new_sl="1.0750"),
        broker="exness", alert_service=alert_svc,
    )
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(changed_fields="sl", new_sl="1.0700"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1


# ---------- Phase 4 R3: NO modify cmd published (criterion #28) ----------


@pytest.mark.asyncio
async def test_no_modify_cmd_pushed_on_external_sl_change(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Phase 4 R3 forbids server-issued modify on the Exness leg. An
    external SL change must NOT push a modify cmd."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(changed_fields="sl", new_sl="1.0750"),
        broker="exness", alert_service=alert_svc,
    )
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []
    # And NO modify cmd to FTMO either.
    ftmo_entries = await redis_client.xrange(
        "cmd_stream:ftmo:ftmo_001", "-", "+"
    )
    assert ftmo_entries == []


# ---------- Vietnamese title + context structure (criterion #27) ----------


@pytest.mark.asyncio
async def test_vietnamese_title_and_context(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _modified_event(
            changed_fields="sl,tp",
            old_sl="1.0800", new_sl="1.0750",
            old_tp="1.0900", new_tp="1.0950",
        ),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    row = await redis_client.hgetall(keys[0])  # type: ignore[misc]
    assert row["title_vi"] == "⚠️ Lệnh Exness bị sửa SL/TP ngoài hệ thống"
    ctx = json.loads(row["context"])
    assert ctx["order_id"] == "ord_hedge_1"
    assert ctx["exness_ticket"] == "55001"
    assert ctx["exness_account_id"] == "exness_001"
    # changed_fields preserved as JSON-encoded list in context.
    assert set(json.loads(ctx["changed_fields"])) == {"sl", "tp"}
    assert ctx["old_sl"] == "1.0800"
    assert ctx["new_sl"] == "1.0750"
    assert ctx["old_tp"] == "1.0900"
    assert ctx["new_tp"] == "1.0950"
