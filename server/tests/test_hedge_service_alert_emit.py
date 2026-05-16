"""Step 4.11 — HedgeService fire-site tests for hedge_closed +
leg_orphaned alerts.

Mirrors the existing ``test_hedge_service_cascade_open.py`` and
``test_hedge_service_cascade_close.py`` fixtures but builds the
``HedgeService`` with an injected ``AlertService`` so the new step 4.11
emit calls actually fire. The 4.7b/4.8 cascade behaviour itself is
already locked by those existing tests with a no-alert HedgeService,
and that backward-compat path is preserved here by the default ``None``
on the new ``alert_service`` kwarg.

We cover:

  - leg_orphaned alert fires once on cascade secondary_failed
    (all-rejected exhaustion path).
  - hedge_closed alert fires on the Path C complete_cascade_close
    terminal (server-initiated close landed via
    position_closed_external).
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from app.services import hedge_service as hedge_module
from app.services.alert_service import AlertService
from app.services.broadcast import BroadcastService
from app.services.hedge_service import HedgeService
from app.services.redis_service import RedisService


class _CapturingBroadcast(BroadcastService):
    def __init__(self) -> None:
        super().__init__(redis_svc=None)
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        self.published.append((channel, message))


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
    # No http_client + telegram_enabled=False so the Telegram path
    # short-circuits — we only want to assert the Redis HASH + WS push
    # contract from the fire site.
    return AlertService(redis_svc, broadcast)


@pytest.fixture
def hedge_svc_with_alerts(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    alert_svc: AlertService,
) -> HedgeService:
    return HedgeService(redis_svc, broadcast, alert_service=alert_svc)


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match the existing hedge-service test timing so cascade
    exhaustion completes in subsecond wall-clock."""
    monkeypatch.setattr(hedge_module, "SECONDARY_OUTCOME_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(hedge_module, "SECONDARY_OUTCOME_TIMEOUT", 0.2)
    monkeypatch.setattr(
        hedge_module, "RETRY_DELAYS_SECONDS", (0.001, 0.002, 0.004)
    )


async def _seed_hedge_order(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    *,
    order_id: str = "ord_alert_1",
) -> dict[str, str]:
    fields = {
        "order_id": order_id,
        "pair_id": "pair_001",
        "ftmo_account_id": "ftmo_001",
        "exness_account_id": "exness_001",
        "symbol": "EURUSD",
        "side": "buy",
        "order_type": "market",
        "status": "primary_filled",
        "p_status": "filled",
        "p_volume_lots": "0.10",
        "s_status": "pending",
        "s_volume_lots": "0.10",
        "s_exness_symbol": "EURUSDz",
        "s_risk_ratio": "1.0",
        "created_at": "1735000000000",
        "updated_at": "1735000000000",
    }
    await redis_svc.create_order(order_id, fields)
    await redis_client.hset(  # type: ignore[misc]
        "pair:pair_001",
        mapping={
            "pair_id": "pair_001",
            "name": "EURUSD-pair",
            "ftmo_account_id": "ftmo_001",
        },
    )
    return fields


# ---------- leg_orphaned ----------


@pytest.mark.asyncio
async def test_leg_orphaned_alert_fires_on_secondary_failed(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc_with_alerts: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Cascade open exhausts all 4 attempts → ``_finalize_failure`` →
    WS secondary_failed + step 4.11 leg_orphaned CRITICAL alert."""
    order = await _seed_hedge_order(redis_svc, redis_client)

    stop = asyncio.Event()

    async def _reject_loop() -> None:
        while not stop.is_set():
            current = await redis_svc.get_order("ord_alert_1")
            if (current or {}).get("s_status") == "pending_open":
                await redis_svc.update_order(
                    "ord_alert_1",
                    patch={
                        "s_status": "rejected",
                        "s_error_msg": "broker_rejected",
                    },
                )
            await asyncio.sleep(0.001)

    rej_task = asyncio.create_task(_reject_loop())
    try:
        await hedge_svc_with_alerts.cascade_secondary_open(
            "ord_alert_1", order
        )
    finally:
        stop.set()
        await rej_task

    # Exactly one alert HASH written, of type leg_orphaned.
    alert_keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(alert_keys) == 1
    row = await redis_client.hgetall(alert_keys[0])  # type: ignore[misc]
    assert row["alert_type"] == "leg_orphaned"
    assert row["severity"] == "CRITICAL"
    assert row["emoji"] == "🚨"
    # Body carries the failure_reason from _finalize_failure (which is
    # the s_error_msg the reject loop wrote).
    assert "broker_rejected" in row["body_vi"]
    assert "EURUSD-pair" in row["body_vi"]
    # WS broadcasts include both secondary_failed (existing 4.7a) AND
    # the alerts channel push (step 4.11).
    channels = [ch for ch, _ in broadcast.published]
    assert "alerts" in channels


# ---------- hedge_closed via complete_cascade_close (Path C) ----------


@pytest.mark.asyncio
async def test_hedge_closed_alert_fires_on_complete_cascade_close(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc_with_alerts: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Path C terminal: ``position_closed_external`` with
    ``close_reason="server_initiated"`` invokes
    ``HedgeService.complete_cascade_close`` which stamps the order
    terminal + broadcasts hedge_closed + emits the step 4.11 INFO
    alert."""
    await _seed_hedge_order(redis_svc, redis_client)
    # Bring the order to ``status="close_pending"`` so
    # complete_cascade_close treats it as a non-terminal in-flight
    # cascade and stamps it terminal. The pre-acquire mirrors the
    # existing test_complete_cascade_close_stamps_terminal fixture.
    await redis_svc.update_order(
        "ord_alert_1",
        patch={"status": "close_pending", "p_status": "close_pending"},
    )
    await redis_svc.acquire_cascade_lock("ord_alert_1", "test")

    await hedge_svc_with_alerts.complete_cascade_close(
        "ord_alert_1", closed_leg="s", close_reason="server_initiated",
    )

    # Order terminal.
    row = await redis_client.hgetall("order:ord_alert_1")  # type: ignore[misc]
    assert row["status"] == "closed"
    # Alert emitted.
    alert_keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(alert_keys) == 1
    alert_row = await redis_client.hgetall(alert_keys[0])  # type: ignore[misc]
    assert alert_row["alert_type"] == "hedge_closed"
    assert alert_row["severity"] == "INFO"
    assert alert_row["emoji"] == "✅"
    # Body enriched from the order row (pair name + side + volume).
    assert "EURUSD-pair" in alert_row["body_vi"]
    assert "0.10" in alert_row["body_vi"]
    # WS broadcast list includes hedge_closed on orders + alert on alerts.
    types = [
        (ch, msg.get("type"))
        for ch, msg in broadcast.published
    ]
    assert ("orders", "hedge_closed") in types
    assert ("alerts", "alert") in types


# ---------- HedgeService without alert wire keeps 4.7b behaviour ----------


@pytest.mark.asyncio
async def test_hedge_service_without_alert_service_skips_emit(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Backward-compat guard: an old call site that builds HedgeService
    without ``alert_service=`` (the default ``None``) must still drive
    cascade success / failure without trying to dereference None and
    crash."""
    svc = HedgeService(redis_svc, broadcast)  # no alert wire
    await _seed_hedge_order(redis_svc, redis_client)
    await redis_svc.update_order(
        "ord_alert_1",
        patch={"status": "close_pending", "p_status": "close_pending"},
    )
    await redis_svc.acquire_cascade_lock("ord_alert_1", "test")
    # Does not raise.
    await svc.complete_cascade_close(
        "ord_alert_1", closed_leg="s", close_reason="server_initiated",
    )
    # And no alert HASH was created either.
    alert_keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert alert_keys == []
