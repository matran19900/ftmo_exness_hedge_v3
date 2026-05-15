"""Step 4.7b — AlertService publish-only contract tests.

Covers the 4.7b registry + emit() lifecycle:

  - Successful emit writes the Redis HASH with TTL + publishes to the
    ``alerts`` WS channel + returns the freshly-generated alert_id.
  - SET NX EX cooldown is atomic against concurrent emits.
  - Cooldown suppresses duplicate emits within the window; expires
    cleanly after TTL.
  - Unknown alert_type returns None + logs ERROR, with no Redis writes.
  - The AlertPayload helpers (to_redis_hash / to_ws_message) preserve
    the structured ``context`` differently per surface (JSON-encoded
    string for Redis HASH, dict for WS).
  - Concurrent emits with the same cooldown_key — only one wins.

Step 4.11 will extend with Telegram delivery + 6 more alert types;
those tests live in a future test_alert_service_telegram.py.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import fakeredis.aioredis
import pytest
from app.services.alert_service import (
    ALERT_TYPES,
    ALERTS_CHANNEL,
    AlertPayload,
    AlertService,
)
from app.services.broadcast import BroadcastService
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


# ---------- registry sanity (criterion #5) ----------


def test_alert_types_registry_has_4_7b_types() -> None:
    """The two 4.7b types must be registered with WARN severity, 300s
    cooldown, and a 7-day TTL — the values the design freezes."""
    assert "hedge_leg_external_close_warning" in ALERT_TYPES
    assert "hedge_leg_external_modify_warning" in ALERT_TYPES
    for name in (
        "hedge_leg_external_close_warning",
        "hedge_leg_external_modify_warning",
    ):
        spec = ALERT_TYPES[name]
        assert spec["severity"] == "WARN"
        assert spec["emoji"] == "⚠️"
        assert spec["cooldown_seconds"] == 300
        assert spec["ttl_seconds"] == 7 * 86400


# ---------- emit happy path (criterion #1, #6, #7) ----------


@pytest.mark.asyncio
async def test_emit_happy_path_writes_redis_and_publishes_ws(
    alert_svc: AlertService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    alert_id = await alert_svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_xyz",
        title_vi="⚠️ Lệnh Exness đóng ngoài hệ thống",
        body_vi="Order ord_xyz: Exness leg đã đóng.",
        context={"order_id": "ord_xyz", "exness_ticket": "55001"},
    )
    assert alert_id is not None
    assert len(alert_id) == 32  # uuid4 hex

    # Redis HASH written.
    row = await redis_client.hgetall(f"alert:{alert_id}")  # type: ignore[misc]
    assert row["alert_id"] == alert_id
    assert row["alert_type"] == "hedge_leg_external_close_warning"
    assert row["severity"] == "WARN"
    assert row["emoji"] == "⚠️"
    assert row["title_vi"] == "⚠️ Lệnh Exness đóng ngoài hệ thống"
    # Context is JSON-encoded on the HASH.
    assert json.loads(row["context"]) == {
        "order_id": "ord_xyz", "exness_ticket": "55001"
    }
    # TTL set (~ 7 days, allow drift).
    ttl = await redis_client.ttl(f"alert:{alert_id}")
    assert 7 * 86400 - 5 <= ttl <= 7 * 86400

    # WS broadcast sent on the alerts channel with structured context.
    assert len(broadcast.published) == 1
    chan, data = broadcast.published[0]
    assert chan == ALERTS_CHANNEL
    assert data["type"] == "alert"
    assert data["alert_id"] == alert_id
    # Context preserved as a dict on the WS surface.
    assert data["context"] == {
        "order_id": "ord_xyz", "exness_ticket": "55001"
    }


# ---------- cooldown active (criterion #2) ----------


@pytest.mark.asyncio
async def test_emit_cooldown_suppresses_duplicate(
    alert_svc: AlertService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    first = await alert_svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_xyz",
        title_vi="t", body_vi="b", context={},
    )
    assert first is not None

    second = await alert_svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_xyz",
        title_vi="t", body_vi="b", context={},
    )
    assert second is None

    # Only one WS broadcast.
    assert len(broadcast.published) == 1
    # Only one alert: HASH exists.
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1


# ---------- cooldown expires (criterion #3) ----------


@pytest.mark.asyncio
async def test_emit_after_cooldown_expiry(
    alert_svc: AlertService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    first = await alert_svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_xyz",
        title_vi="t", body_vi="b", context={},
    )
    assert first is not None
    # Manually expire the cooldown by deleting the key.
    await redis_client.delete(
        "alert_cooldown:hedge_leg_external_close_warning:ord_xyz"
    )

    second = await alert_svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_xyz",
        title_vi="t", body_vi="b", context={},
    )
    assert second is not None
    assert second != first


# ---------- unknown alert_type (criterion #4) ----------


@pytest.mark.asyncio
async def test_emit_unknown_alert_type_returns_none_no_writes(
    alert_svc: AlertService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    result = await alert_svc.emit(
        alert_type="unknown_type_xyz",
        cooldown_key="anything",
        title_vi="t", body_vi="b", context={},
    )
    assert result is None
    # No Redis writes (alert:* nor alert_cooldown:*).
    keys = [k async for k in redis_client.scan_iter(match="alert*")]
    assert keys == []
    # No WS broadcasts.
    assert broadcast.published == []


# ---------- AlertPayload helpers (criterion #6, #7) ----------


def test_alert_payload_to_redis_hash_json_encodes_context() -> None:
    p = AlertPayload(
        alert_id="abc",
        alert_type="hedge_leg_external_close_warning",
        severity="WARN",
        emoji="⚠️",
        title_vi="t",
        body_vi="b",
        context={"k1": "v1", "k2": "v2"},
        created_at_ms=1700000000000,
    )
    row = p.to_redis_hash()
    assert row["alert_id"] == "abc"
    assert row["alert_type"] == "hedge_leg_external_close_warning"
    assert row["severity"] == "WARN"
    assert row["emoji"] == "⚠️"
    assert row["title_vi"] == "t"
    assert row["body_vi"] == "b"
    assert json.loads(row["context"]) == {"k1": "v1", "k2": "v2"}
    assert row["created_at_ms"] == "1700000000000"


def test_alert_payload_to_ws_message_keeps_context_dict() -> None:
    p = AlertPayload(
        alert_id="abc",
        alert_type="hedge_leg_external_close_warning",
        severity="WARN",
        emoji="⚠️",
        title_vi="t",
        body_vi="b",
        context={"k1": "v1"},
        created_at_ms=1700000000000,
    )
    msg = p.to_ws_message()
    assert msg["type"] == "alert"
    assert msg["context"] == {"k1": "v1"}
    assert msg["created_at_ms"] == 1700000000000


# ---------- concurrent emit single-winner (criterion #8) ----------


@pytest.mark.asyncio
async def test_emit_concurrent_same_cooldown_key_single_winner(
    alert_svc: AlertService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Two coroutines emit the same (alert_type, cooldown_key) at the
    same time. SET NX EX must let exactly one through."""
    results = await asyncio.gather(
        alert_svc.emit(
            alert_type="hedge_leg_external_close_warning",
            cooldown_key="race_key",
            title_vi="t", body_vi="b", context={"k": "1"},
        ),
        alert_svc.emit(
            alert_type="hedge_leg_external_close_warning",
            cooldown_key="race_key",
            title_vi="t", body_vi="b", context={"k": "2"},
        ),
    )
    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1
    assert len(losers) == 1
    # Exactly one alert HASH written.
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1


# ---------- different cooldown_keys do not collide ----------


@pytest.mark.asyncio
async def test_emit_distinct_cooldown_keys_both_succeed(
    alert_svc: AlertService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    a = await alert_svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_A", title_vi="t", body_vi="b", context={},
    )
    b = await alert_svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_B", title_vi="t", body_vi="b", context={},
    )
    assert a is not None
    assert b is not None
    assert a != b


# ---------- different alert_types do not collide ----------


@pytest.mark.asyncio
async def test_emit_different_alert_types_same_key_both_succeed(
    alert_svc: AlertService,
    redis_client: fakeredis.aioredis.FakeRedis,
    broadcast: _CapturingBroadcast,
) -> None:
    """Cooldown namespace is per alert_type — a close + modify on the
    same order can both fire."""
    a = await alert_svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_X", title_vi="t", body_vi="b", context={},
    )
    b = await alert_svc.emit(
        alert_type="hedge_leg_external_modify_warning",
        cooldown_key="ord_X", title_vi="t", body_vi="b", context={},
    )
    assert a is not None
    assert b is not None
