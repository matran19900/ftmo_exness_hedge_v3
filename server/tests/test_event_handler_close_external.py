"""Step 4.7b — event_handler position_closed_external WARNING tests.

Covers the Exness branch of ``_handle_event_entry``:

  - close_reason="server_initiated" → no WARNING, passthrough log
    (cascade close is step 4.8 scope).
  - close_reason in {external, sl_hit, tp_hit, stop_out, manual} →
    WARNING alert emitted, no FTMO cascade cmd.
  - Order not found (orphan ticket) → log + no crash.
  - Vietnamese template + structured context payload.
  - cooldown_key = order_id (one alert per order per cooldown window).

Phase 4 R3 guarantee: NO modify or close cmd published to
``cmd_stream:ftmo:*`` from this handler. External bucket is operator-
visible only.
"""

from __future__ import annotations

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
    # Seed the pair so pair_name lookup works.
    await redis_svc._redis.hset(  # type: ignore[misc]
        "pair:pair_001",
        mapping={"pair_id": "pair_001", "name": "EURUSD-pair", "ftmo_account_id": "ftmo_001"},
    )


def _exness_close_event(
    *,
    ticket: str = "55001",
    close_reason: str = "external",
    close_price: str = "1.08425",
) -> dict[str, str]:
    """Mirror the ``position_monitor._publish_event`` payload shape for
    an ``position_closed_external`` event."""
    return {
        "event_type": "position_closed_external",
        "broker_position_id": ticket,
        "symbol": "EURUSDz",
        "side": "buy",
        "last_volume": "0.10",
        "last_sl": "0",
        "last_tp": "0",
        "close_reason": close_reason,
        "close_price": close_price,
        "close_time_ms": "1735000100000",
        "realized_profit": "12.5",
        "commission": "0",
        "swap": "0",
        "enrichment_source": "history_deals",
        "ts_ms": "1735000100100",
    }


# ---------- bucket: external close reasons -> WARNING ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_reason",
    ["external", "sl_hit", "tp_hit", "stop_out", "manual"],
)
async def test_external_close_reason_emits_warning(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
    close_reason: str,
) -> None:
    """Criteria #9-13: all 5 external-bucket reasons emit one WARNING."""
    await _seed_hedge_order(redis_svc)

    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason=close_reason),
        broker="exness", alert_service=alert_svc,
    )

    # One alert HASH written.
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1
    alert_row = await redis_client.hgetall(keys[0])  # type: ignore[misc]
    assert alert_row["alert_type"] == "hedge_leg_external_close_warning"
    # Body contains the close_reason for operator triage.
    assert close_reason in alert_row["body_vi"]
    # No FTMO cascade cmd was pushed.
    ftmo_entries = await redis_client.xrange(
        "cmd_stream:ftmo:ftmo_001", "-", "+"
    )
    assert ftmo_entries == []


# ---------- server_initiated -> passthrough (criterion #14) ----------


@pytest.mark.asyncio
async def test_server_initiated_passthrough_no_alert(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """close_reason='server_initiated' → no WARNING. Step 4.8 wires the
    cascade FTMO path; for 4.7b we just log + return."""
    await _seed_hedge_order(redis_svc)

    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="server_initiated"),
        broker="exness", alert_service=alert_svc,
    )

    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert keys == []
    assert broadcast.published == []


# ---------- order not found (criterion #15) ----------


@pytest.mark.asyncio
async def test_order_not_found_logs_no_crash(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Orphan ticket (no seeded order/link) → handler does NOT raise +
    no alert emitted."""
    # Do NOT seed the order or link the broker_order_id.
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(ticket="99999"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert keys == []


# ---------- Vietnamese template content (criterion #16) ----------


@pytest.mark.asyncio
async def test_vietnamese_title_in_warning(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    alert_row = await redis_client.hgetall(keys[0])  # type: ignore[misc]
    assert alert_row["title_vi"] == "⚠️ Lệnh Exness đóng ngoài hệ thống"
    # Body has pair name + ticket + close_reason + close_price.
    assert "EURUSD-pair" in alert_row["body_vi"]
    assert "ord_hedge_1" in alert_row["body_vi"]
    assert "55001" in alert_row["body_vi"]
    assert "manual" in alert_row["body_vi"]
    assert "1.08425" in alert_row["body_vi"]


# ---------- context dict shape (criterion #17) ----------


@pytest.mark.asyncio
async def test_context_dict_has_all_required_keys(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    import json as _json
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    alert_row = await redis_client.hgetall(keys[0])  # type: ignore[misc]
    ctx = _json.loads(alert_row["context"])
    assert ctx["order_id"] == "ord_hedge_1"
    assert ctx["exness_ticket"] == "55001"
    assert ctx["close_reason"] == "external"
    assert ctx["close_price"] == "1.08425"
    assert ctx["close_time_ms"] == "1735000100000"
    assert ctx["pair_id"] == "pair_001"
    assert ctx["exness_account_id"] == "exness_001"


# ---------- cooldown_key = order_id (criterion #18) ----------


@pytest.mark.asyncio
async def test_cooldown_key_is_order_id(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """A second external-close event for the same order within the
    cooldown window must NOT emit a duplicate alert."""
    await _seed_hedge_order(redis_svc)

    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="sl_hit"),
        broker="exness", alert_service=alert_svc,
    )
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual"),
        broker="exness", alert_service=alert_svc,
    )

    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1
    # Cooldown key visible.
    cooldown_keys = [
        k async for k in redis_client.scan_iter(match="alert_cooldown:*")
    ]
    assert "alert_cooldown:hedge_leg_external_close_warning:ord_hedge_1" in cooldown_keys


# ---------- unknown close_reason still emits (defensive) ----------


@pytest.mark.asyncio
async def test_unknown_close_reason_defaults_to_external_bucket(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A future close_reason slug we don't recognize must still emit a
    WARNING (defensive — safer than silent drop)."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="future_slug"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1


# ---------- AlertService not wired (defensive) ----------


@pytest.mark.asyncio
async def test_missing_alert_service_logs_error_no_crash(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """If somehow the handler is wired without AlertService, log ERROR
    and return — never raise."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(),
        broker="exness", alert_service=None,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert keys == []
