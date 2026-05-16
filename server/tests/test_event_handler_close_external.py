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
    """Criteria #9-13: all 5 external-bucket reasons emit one WARNING.

    Step 4.8e amendment — the WARNING emit now follows a HASH stamp
    that flips ``s_status`` and composed ``status`` to ``closed``.
    Asserting the WARNING + the new stamp side-by-side locks the
    contract that BOTH happen for every external close_reason value.
    Dedicated 4.8e tests (``test_external_close_stamps_order_hash_*``
    + ``test_external_close_stamp_records_verbatim_close_reason``)
    cover the stamp shape in detail."""
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
    # Step 4.8e — order HASH stamped to composed status=closed for
    # every external close_reason value.
    order_row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert order_row["s_status"] == "closed"
    assert order_row["status"] == "closed"
    assert order_row["s_close_reason"] == close_reason


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
    cooldown window must NOT emit a duplicate alert.

    Step 4.8e amendment — the second event ALSO finds
    ``s_status == "closed"`` from the first event's stamp and
    short-circuits the HASH update. Two layers of idempotency working
    together: HASH-level (4.8e) + alert-cooldown-level (4.7b). The
    explicit 4.8e idempotency assertion lives in
    ``test_external_close_idempotent_skip_on_existing_closed`` below."""
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


# ---------------------------------------------------------------------------
# Step 4.8e — order HASH state-sync on external close (Option A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_close_stamps_order_hash_then_emits_warning(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.8e core fix — external Exness close now stamps the order
    HASH (``s_status=closed`` + composed ``status=closed`` + ``s_close_*``)
    BEFORE emitting the 4.7b WARNING. Pre-4.8e the handler emitted the
    alert and returned without HASH update, leaving the order in a
    phantom-open state that allowed double-close hazard."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual", close_price="1.08425"),
        broker="exness", alert_service=alert_svc,
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]

    # HASH stamp landed.
    assert row["s_status"] == "closed"
    assert row["status"] == "closed"
    assert row["s_close_price"] == "1.08425"
    assert row["s_closed_at"] == "1735000100000"
    assert row["s_close_reason"] == "manual"
    assert row["s_realized_pnl"] == "12.5"
    assert row["s_commission"] == "0"

    # WARNING still fires.
    alert_keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(alert_keys) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_reason",
    ["external", "sl_hit", "tp_hit", "stop_out", "manual"],
)
async def test_external_close_stamp_records_verbatim_close_reason(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
    close_reason: str,
) -> None:
    """``s_close_reason`` lands as the verbatim value from the event
    (not hardcoded) so future operator audit can distinguish the actual
    close cause across the 5 enumerated external bucket values."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason=close_reason),
        broker="exness", alert_service=alert_svc,
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_close_reason"] == close_reason


@pytest.mark.asyncio
async def test_external_close_idempotent_skip_on_existing_closed(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Idempotency: a duplicate external-close event (e.g. reconcile_state
    replay) finds ``s_status="closed"`` already and skips the re-stamp.
    The 4.7b alert cooldown_key=order_id separately suppresses the
    duplicate WARNING."""
    await _seed_hedge_order(redis_svc)
    # Pre-stamp the order to "closed" state via the first event.
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual", close_price="1.08425"),
        broker="exness", alert_service=alert_svc,
    )
    # Capture state after first event.
    row_after_first = await redis_client.hgetall(  # type: ignore[misc]
        "order:ord_hedge_1"
    )
    first_updated_at = row_after_first["updated_at"]

    # Duplicate event with a different close_price (test that idempotency
    # SHORT-CIRCUITS, not just re-stamps with same values).
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="sl_hit", close_price="9.99999"),
        broker="exness", alert_service=alert_svc,
    )
    row_after_second = await redis_client.hgetall(  # type: ignore[misc]
        "order:ord_hedge_1"
    )
    # Original stamp preserved; the bogus 9.99999 / sl_hit from the
    # duplicate did NOT overwrite. updated_at also unchanged.
    assert row_after_second["s_close_price"] == "1.08425"
    assert row_after_second["s_close_reason"] == "manual"
    assert row_after_second["updated_at"] == first_updated_at


@pytest.mark.asyncio
async def test_external_close_then_close_endpoint_rejects_double_close(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Double-close hazard regression: after an external close stamp,
    the OrderService.close_order pre-check (``_CLOSEABLE_STATUSES =
    ("filled",)``) MUST reject a follow-up close attempt with
    ``order_not_closeable`` (400). Pre-4.8e the composed status stayed
    "filled" so a re-close would have advanced into the cascade
    orchestrator and burned the 4-attempt retry budget for nothing."""
    from app.services.order_service import OrderService, OrderValidationError
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual"),
        broker="exness", alert_service=alert_svc,
    )
    # Composed status is now "closed".
    order_service = OrderService(redis_svc)
    with pytest.raises(OrderValidationError) as exc_info:
        await order_service.close_order("ord_hedge_1")
    assert exc_info.value.error_code == "order_not_closeable"
    assert exc_info.value.http_status == 400


@pytest.mark.asyncio
async def test_external_close_p_status_unchanged_ftmo_orphan(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """R3 + design §1.B: secondary passive policy means the server does
    NOT auto-cascade close the FTMO leg when Exness closes externally.
    ``p_status`` MUST stay "filled" (FTMO leg orphan); the 4.7b WARNING
    surfaces the orphan condition to the operator for manual cleanup."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual"),
        broker="exness", alert_service=alert_svc,
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    # Secondary closed, primary still open.
    assert row["s_status"] == "closed"
    assert row["p_status"] == "filled"
    # No FTMO cmd pushed.
    ftmo_entries = await redis_client.xrange(
        "cmd_stream:ftmo:ftmo_001", "-", "+"
    )
    assert ftmo_entries == []


@pytest.mark.asyncio
async def test_external_close_handles_missing_optional_fields(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Defensive: if the Exness ``position_monitor`` enrichment falls back
    to ``snapshot_fallback`` (history_deals empty), the event payload
    omits ``realized_profit`` / ``commission``. Stamp stores empty
    strings instead of crashing."""
    await _seed_hedge_order(redis_svc)
    minimal_event = {
        "event_type": "position_closed_external",
        "broker_position_id": "55001",
        "symbol": "EURUSDz",
        "side": "buy",
        "close_reason": "external",
        "enrichment_source": "snapshot_fallback",
        "ts_ms": "1735000100100",
        # No close_price / close_time_ms / realized_profit / commission.
    }
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        minimal_event,
        broker="exness", alert_service=alert_svc,
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "closed"
    assert row["status"] == "closed"
    assert row["s_close_price"] == ""
    assert row["s_realized_pnl"] == ""
    assert row["s_commission"] == ""


@pytest.mark.asyncio
async def test_external_close_server_initiated_branch_unchanged(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Regression: 4.8e adds the external-bucket stamp; the
    ``server_initiated`` branch (Path C — step 4.8) still does its own
    stamp + cascade-completion call. Independent code paths; no
    cross-contamination."""
    from app.services.hedge_service import HedgeService
    await _seed_hedge_order(redis_svc)
    # Set up a hedge_service so Path C completion has somewhere to fire.
    hedge_svc = HedgeService(redis_svc, broadcast)
    # Acquire the cascade lock as if a prior cascade_close_other_leg
    # acquired it; complete_cascade_close will release.
    await redis_svc.acquire_cascade_lock("ord_hedge_1", "A")
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="server_initiated"),
        broker="exness",
        alert_service=alert_svc,
        hedge_service=hedge_svc,
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    # Path C populates the same fields with close_reason="server_initiated".
    assert row["s_close_reason"] == "server_initiated"
    assert row["s_close_price"] == "1.08425"
    assert row["status"] == "closed"
    # No 4.7b WARNING for the server-initiated path.
    alert_keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert alert_keys == []
