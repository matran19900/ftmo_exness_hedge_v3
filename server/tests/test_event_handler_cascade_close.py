"""Step 4.8 — event_handler Path B/C/D/E + cascade trigger derivation.

FTMO ``position_closed`` events on hedge orders trigger
``HedgeService.cascade_close_other_leg`` with a trigger_path classified
from the (Path A marker, close_reason) tuple. Exness
``position_closed_external`` events with ``close_reason="server_initiated"``
route through ``complete_cascade_close``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from app.services import hedge_service as hedge_module
from app.services.alert_service import AlertService
from app.services.broadcast import BroadcastService
from app.services.event_handler import _derive_cascade_trigger_path, _handle_event_entry
from app.services.hedge_service import HedgeService
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


@pytest.fixture
def hedge_svc(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> HedgeService:
    return HedgeService(redis_svc, broadcast)


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hedge_module, "SECONDARY_OUTCOME_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(hedge_module, "SECONDARY_OUTCOME_TIMEOUT", 0.2)
    monkeypatch.setattr(
        hedge_module, "RETRY_DELAYS_SECONDS", (0.001, 0.002, 0.004)
    )
    monkeypatch.setattr(HedgeService, "_LATE_FILL_WAIT_SECONDS", 0.02)


async def _seed_hedge_filled(
    redis_svc: RedisService,
    *,
    order_id: str = "ord_hedge_1",
    p_broker: str = "9001",
    s_broker: str = "55001",
    extra: dict[str, str] | None = None,
) -> None:
    fields = {
        "order_id": order_id,
        "pair_id": "pair_001",
        "ftmo_account_id": "ftmo_001",
        "exness_account_id": "exness_001",
        "symbol": "EURUSD",
        "side": "buy",
        "order_type": "market",
        "status": "filled",
        "p_status": "filled",
        "p_broker_order_id": p_broker,
        "s_status": "filled",
        "s_broker_order_id": s_broker,
        "s_volume_lots": "0.10",
        "s_exness_symbol": "EURUSDz",
        "created_at": "1735000000000",
        "updated_at": "1735000000000",
    }
    if extra:
        fields.update(extra)
    await redis_svc.create_order(order_id, fields)
    await redis_svc.link_broker_order_id("p", p_broker, order_id)
    await redis_svc.link_broker_order_id("s", s_broker, order_id)


def _ftmo_close_event(
    *, position_id: str = "9001", close_reason: str = "manual"
) -> dict[str, str]:
    return {
        "event_type": "position_closed",
        "position_id": position_id,
        "close_price": "1.08600",
        "close_time": "1735000050000",
        "realized_pnl": "120",
        "close_reason": close_reason,
    }


def _exness_close_event(
    *, ticket: str = "55001", close_reason: str = "external"
) -> dict[str, str]:
    return {
        "event_type": "position_closed_external",
        "broker_position_id": ticket,
        "symbol": "EURUSDz",
        "side": "buy",
        "last_volume": "0.10",
        "last_sl": "0",
        "last_tp": "0",
        "close_reason": close_reason,
        "close_price": "1.08200",
        "close_time_ms": "1735000100000",
        "ts_ms": "1735000100100",
    }


# ---------- trigger_path derivation unit tests ----------


@pytest.mark.parametrize(
    "flag,reason,expected",
    [
        ("A", "manual", "A"),
        ("A", "sl", "A"),       # Path A flag wins even on SL race
        ("", "manual", "B"),
        ("", "unknown", "B"),
        ("", "sl", "D"),
        ("", "tp", "D"),
        ("", "stopout", "E"),
    ],
)
def test_derive_cascade_trigger_path(flag: str, reason: str, expected: str) -> None:
    order = {"close_trigger_initiated": flag} if flag else {}
    assert _derive_cascade_trigger_path(order, reason) == expected


# ---------- FTMO close_reason buckets (criteria #38-42) ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_reason,expected_path",
    [
        ("manual", "B"),
        ("sl", "D"),
        ("tp", "D"),
        ("unknown", "B"),
    ],
)
async def test_ftmo_close_reason_dispatches_cascade(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
    close_reason: str,
    expected_path: str,
) -> None:
    """Each FTMO close_reason value triggers cascade close with the
    correct trigger_path classification."""
    await _seed_hedge_filled(redis_svc)

    fill_task = asyncio.create_task(_late_fill_secondary(redis_svc, "ord_hedge_1"))
    await _handle_event_entry(
        redis_svc, broadcast, "ftmo_001",
        _ftmo_close_event(close_reason=close_reason),
        broker="ftmo", hedge_service=hedge_svc,
    )
    await fill_task

    # cmd pushed to Exness — cascade fired.
    exness_entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert len(exness_entries) == 1
    # close_initiated broadcast carries the trigger_path.
    initiated = [m for _ch, m in broadcast.published if m.get("type") == "close_initiated"]
    assert initiated[0]["trigger_path"] == expected_path


async def _late_fill_secondary(
    redis_svc: RedisService, order_id: str
) -> None:
    """Simulate Exness close fill landing during cascade poll."""
    await asyncio.sleep(0.01)
    await redis_svc.update_order(
        order_id, patch={"s_status": "closed", "s_close_price": "1.08"}
    )


@pytest.mark.asyncio
async def test_ftmo_path_a_marker_overrides_close_reason(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """An order with ``close_trigger_initiated="A"`` (from the API
    endpoint) classifies trigger_path="A" even if the event reports
    close_reason="manual" (operator clicked Close UI; FTMO close
    arrives as MARKET = manual)."""
    await _seed_hedge_filled(
        redis_svc, extra={"close_trigger_initiated": "A"}
    )
    fill_task = asyncio.create_task(
        _late_fill_secondary(redis_svc, "ord_hedge_1")
    )
    await _handle_event_entry(
        redis_svc, broadcast, "ftmo_001",
        _ftmo_close_event(close_reason="manual"),
        broker="ftmo", hedge_service=hedge_svc,
    )
    await fill_task
    initiated = [m for _ch, m in broadcast.published if m.get("type") == "close_initiated"]
    assert initiated[0]["trigger_path"] == "A"


# ---------- single-leg backward compat ----------


@pytest.mark.asyncio
async def test_ftmo_position_closed_single_leg_no_cascade(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Phase 3 single-leg orders (no exness_account_id) close normally
    without invoking cascade; composed status goes to closed."""
    await redis_svc.create_order(
        "ord_single",
        {
            "order_id": "ord_single",
            "pair_id": "pair_x",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "",  # single-leg
            "symbol": "EURUSD",
            "side": "buy",
            "order_type": "market",
            "status": "filled",
            "p_status": "filled",
            "p_broker_order_id": "9099",
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )
    await redis_svc.link_broker_order_id("p", "9099", "ord_single")
    await _handle_event_entry(
        redis_svc, broadcast, "ftmo_001",
        _ftmo_close_event(position_id="9099", close_reason="manual"),
        broker="ftmo", hedge_service=hedge_svc,
    )
    row = await redis_client.hgetall("order:ord_single")  # type: ignore[misc]
    assert row["status"] == "closed"
    # No Exness cmd pushed.
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []


# ---------- Path C: Exness server_initiated completes cascade ----------


@pytest.mark.asyncio
async def test_exness_server_initiated_completes_cascade(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Exness position_closed_external with close_reason=server_initiated
    triggers ``complete_cascade_close`` (Path C)."""
    await _seed_hedge_filled(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1", patch={"status": "close_pending"}
    )
    # Acquire lock as if cascade_close_other_leg holds it.
    await redis_svc.acquire_cascade_lock("ord_hedge_1", "A")

    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="server_initiated"),
        broker="exness",
        alert_service=alert_svc,
        hedge_service=hedge_svc,
    )

    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "closed"
    assert row["s_status"] == "closed"
    assert row["s_close_price"] == "1.08200"
    assert row["s_close_reason"] == "server_initiated"
    # Lock released.
    assert await redis_svc.read_cascade_lock("ord_hedge_1") is None
    # hedge_closed broadcast.
    types = [m.get("type") for _ch, m in broadcast.published]
    assert "hedge_closed" in types


# ---------- Path: Exness external -> WARNING only, no cascade FTMO ----------


@pytest.mark.asyncio
async def test_exness_external_emits_warning_no_cascade(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Exness external close (manual/sl_hit/etc.) emits the 4.7b WARNING
    + NO cascade FTMO (passive policy R3)."""
    await _seed_hedge_filled(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual"),
        broker="exness",
        alert_service=alert_svc,
        hedge_service=hedge_svc,
    )
    # NO FTMO cmd pushed.
    entries = await redis_client.xrange(
        "cmd_stream:ftmo:ftmo_001", "-", "+"
    )
    assert entries == []
    # Alert published.
    alert_keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(alert_keys) == 1


# ---------- idempotent no-op on terminal ----------


@pytest.mark.asyncio
async def test_late_event_on_already_closed_order_no_op(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """A stale position_closed event arriving after status=closed
    no-ops idempotently."""
    await _seed_hedge_filled(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1", patch={"status": "closed"}
    )
    await _handle_event_entry(
        redis_svc, broadcast, "ftmo_001",
        _ftmo_close_event(close_reason="manual"),
        broker="ftmo", hedge_service=hedge_svc,
    )
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []


@pytest.mark.asyncio
async def test_late_event_on_close_failed_order_no_op(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_filled(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1", patch={"status": "close_failed"}
    )
    await _handle_event_entry(
        redis_svc, broadcast, "ftmo_001",
        _ftmo_close_event(close_reason="manual"),
        broker="ftmo", hedge_service=hedge_svc,
    )
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []


# ---------- order not found (orphan event) ----------


@pytest.mark.asyncio
async def test_ftmo_close_event_unknown_position_no_crash(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _handle_event_entry(
        redis_svc, broadcast, "ftmo_001",
        _ftmo_close_event(position_id="ghost"),
        broker="ftmo", hedge_service=hedge_svc,
    )
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []


@pytest.mark.asyncio
async def test_exness_server_initiated_unknown_ticket_no_crash(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(ticket="ghost", close_reason="server_initiated"),
        broker="exness",
        alert_service=alert_svc,
        hedge_service=hedge_svc,
    )
    # No mutation, no crash.
    keys = [k async for k in redis_client.scan_iter(match="order:*")]
    assert keys == []


# ---------- hedge order without hedge_service: no-op + log ----------


@pytest.mark.asyncio
async def test_ftmo_close_no_hedge_service_logs_error(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If event_handler is wired without hedge_service for a hedge
    order, log ERROR + don't crash."""
    await _seed_hedge_filled(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "ftmo_001",
        _ftmo_close_event(close_reason="manual"),
        broker="ftmo", hedge_service=None,
    )
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []


# ---------- Exness close_reason missing defaults to external bucket ----------


@pytest.mark.asyncio
async def test_exness_missing_close_reason_defaults_external(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_hedge_filled(redis_svc)
    event = _exness_close_event()
    del event["close_reason"]
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001", event,
        broker="exness",
        alert_service=alert_svc,
        hedge_service=hedge_svc,
    )
    # External bucket -> WARNING fired.
    alert_keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(alert_keys) == 1


# ---------- ftmo close does NOT emit WARNING ----------


@pytest.mark.asyncio
async def test_ftmo_close_no_warning_emitted(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Per CEO policy, WARNING is hedge-leg-side only. FTMO close is
    expected operator action and cascades silently."""
    await _seed_hedge_filled(redis_svc)
    fill_task = asyncio.create_task(_late_fill_secondary(redis_svc, "ord_hedge_1"))
    await _handle_event_entry(
        redis_svc, broadcast, "ftmo_001",
        _ftmo_close_event(close_reason="manual"),
        broker="ftmo", hedge_service=hedge_svc,
    )
    await fill_task
    alert_keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert alert_keys == []
