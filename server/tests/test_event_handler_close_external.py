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
    # Step 4.11 split: ``stop_out`` now emits ``secondary_liquidation``
    # CRITICAL instead of the generic WARNING (asserted in the
    # dedicated test_stop_out_emits_secondary_liquidation_critical test
    # below). The remaining 4 external close_reasons keep the 4.7b
    # WARNING contract verbatim.
    ["external", "sl_hit", "tp_hit", "manual"],
)
async def test_external_close_reason_emits_warning(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
    close_reason: str,
) -> None:
    """Criteria #9-13: the 4 non-liquidation external bucket reasons
    each emit one WARNING.

    Step 4.8e + 4.8f Option C — the WARNING emit follows a HASH stamp
    that flips ``s_status`` to ``closed`` but leaves composed
    ``status="filled"`` so the frontend keeps the row in the Open tab
    and the operator's Close button stays available. Asserting the
    WARNING + the new stamp side-by-side locks the contract that BOTH
    happen for every external close_reason value. Dedicated 4.8e tests
    (``test_external_close_stamps_order_hash_*`` +
    ``test_external_close_stamp_records_verbatim_close_reason``) cover
    the stamp shape in detail."""
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
    # Step 4.8e + 4.8f — order HASH stamped: s_status flips to "closed"
    # for every external close_reason value, but composed ``status``
    # stays ``"filled"`` (4.8f Option C) so the operator can still
    # click Close via the API.
    order_row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert order_row["s_status"] == "closed"
    assert order_row["status"] == "filled"
    assert order_row["s_close_reason"] == close_reason


# ---------- step 4.11: stop_out -> secondary_liquidation CRITICAL ----------


@pytest.mark.asyncio
async def test_stop_out_emits_secondary_liquidation_critical(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.11 split: ``close_reason="stop_out"`` no longer falls
    under the generic WARN bucket — it surfaces a CRITICAL
    ``secondary_liquidation`` alert that names the liquidation
    explicitly. The existing 4.7b ``hedge_leg_external_close_warning``
    alert must NOT also fire for this event (one alert per event)."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="stop_out", close_price="1.07000"),
        broker="exness", alert_service=alert_svc,
    )
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1
    row = await redis_client.hgetall(keys[0])  # type: ignore[misc]
    assert row["alert_type"] == "secondary_liquidation"
    assert row["severity"] == "CRITICAL"
    assert row["emoji"] == "🚨"
    # Body names the liquidation + the close price + the realized P&L
    # from the event payload (12.5 in the canned fixture).
    assert "stop-out" in row["body_vi"]
    assert "12.5" in row["body_vi"]
    # State-only stamp (4.8f Option C) still applies: composed status
    # stays at "filled" so the operator can click Close.
    order_row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert order_row["s_status"] == "closed"
    assert order_row["status"] == "filled"
    # The 4.7b warning type did NOT fire alongside.
    cooldown_keys = [
        k async for k in redis_client.scan_iter(match="alert_cooldown:*")
    ]
    assert not any(
        "hedge_leg_external_close_warning" in k for k in cooldown_keys
    )


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
# Step 4.8e + 4.8f — order HASH state-sync on external close (Option C)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_close_stamps_order_hash_then_emits_warning(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.8e + 4.8f Option C — external Exness close stamps the
    order HASH (``s_status=closed`` + ``s_close_*``) BEFORE emitting
    the 4.7b WARNING. Composed ``status`` STAYS ``"filled"`` (4.8f
    Option C) so the row remains in the Open tab and the operator's
    Close button stays available. Pre-4.8e the handler emitted the
    alert and returned without HASH update — both legs looked open
    even though Exness was gone (phantom-open + double-close
    hazard)."""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual", close_price="1.08425"),
        broker="exness", alert_service=alert_svc,
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]

    # HASH stamp landed (s_status + s_close_*); composed UNCHANGED.
    assert row["s_status"] == "closed"
    assert row["status"] == "filled"
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
async def test_external_close_then_close_endpoint_accepts_orphan_close(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.8f Option C regression — orphan-close path. After an
    external Exness close stamp, the operator clicks Close on the
    still-visible row to close the FTMO orphan. The API endpoint MUST
    accept the close (composed stays ``"filled"`` ∈
    ``_CLOSEABLE_STATUSES``) and push the FTMO close cmd so the
    orphan-leg cleanup proceeds via the cascade chain.

    Inverted from the original 4.8e
    ``test_external_close_then_close_endpoint_rejects_double_close``
    semantics: 4.8e Option A flipped composed to ``"closed"`` and
    rejected with ``order_not_closeable``, leaving the operator with
    no UI close path. 4.8f Option C corrects that — the close
    endpoint accepts; the cascade short-circuit (see
    ``test_option_c_cascade_short_circuit_broadcasts_hedge_closed``)
    finalizes composed=closed once the FTMO close completes."""
    from app.services.order_service import OrderService
    await _seed_hedge_order(redis_svc)
    # close_order needs p_broker_order_id + an online FTMO client.
    await redis_svc.update_order(
        "ord_hedge_1",
        patch={"p_broker_order_id": "p_ticket_1", "p_volume_lots": "0.10"},
    )
    await redis_client.set("client:ftmo:ftmo_001", "1", ex=30)

    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual"),
        broker="exness", alert_service=alert_svc,
    )
    # Composed status is still "filled"; s_status="closed".
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "filled"
    assert row["s_status"] == "closed"

    order_service = OrderService(redis_svc)
    order_id_out, request_id = await order_service.close_order("ord_hedge_1")
    assert order_id_out == "ord_hedge_1"
    assert request_id  # non-empty request_id minted

    # FTMO close cmd was pushed.
    ftmo_entries = await redis_client.xrange(
        "cmd_stream:ftmo:ftmo_001", "-", "+"
    )
    assert len(ftmo_entries) == 1
    _entry_id, cmd_fields = ftmo_entries[0]
    assert cmd_fields["action"] == "close"
    assert cmd_fields["broker_order_id"] == "p_ticket_1"
    assert cmd_fields["order_id"] == "ord_hedge_1"


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
    # 4.8f Option C — composed stays "filled"; s_status flips to "closed".
    assert row["s_status"] == "closed"
    assert row["status"] == "filled"
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


# ---------------------------------------------------------------------------
# Step 4.8f Option C — orphan-close behaviour (new tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_option_c_external_close_does_not_drop_row(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.8f Option C — the external-close stamp MUST NOT flip
    composed ``status="closed"``. Frontend ``useWebSocket.ts:169`` drops
    the row from the Open tab when ``msg.data.status === "closed"`` OR
    ``msg.data.p_status === "closed"``; under Option C neither is true
    after the external-close stamp, so the row stays visible and the
    operator's Close button stays available.

    The handler also MUST NOT publish to ``ORDERS_CHANNEL`` from the
    external branch — only the alerts channel WARNING fires. (Any
    ``order_updated`` broadcast carrying ``status="closed"`` would
    trigger the same frontend drop.)"""
    await _seed_hedge_order(redis_svc)
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual"),
        broker="exness", alert_service=alert_svc,
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    # Composed stays "filled"; p_status untouched.
    assert row["status"] == "filled"
    assert row["p_status"] == "filled"
    # No order_updated broadcast carrying status="closed" was emitted
    # from the external-close handler.
    closed_broadcasts = [
        m for _ch, m in broadcast.published
        if m.get("status") == "closed" or m.get("p_status") == "closed"
    ]
    assert closed_broadcasts == []


@pytest.mark.asyncio
async def test_option_c_close_endpoint_accepts_orphan_order(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
) -> None:
    """Step 4.8f Option C — close_order endpoint accepts an order whose
    secondary leg already closed externally (s_status="closed",
    composed="filled"). The composed-status gate
    ``_CLOSEABLE_STATUSES=("filled",)`` accepts because composed is
    untouched by the external-close stamp. No external-close event is
    triggered here; we just exercise the endpoint against the orphan
    state directly."""
    from app.services.order_service import OrderService
    await _seed_hedge_order(redis_svc)
    # Manually arrange orphan state: s_status=closed, composed=filled,
    # p_broker_order_id + client online for close_order to proceed.
    await redis_svc.update_order(
        "ord_hedge_1",
        patch={
            "s_status": "closed",
            "p_broker_order_id": "p_ticket_1",
            "p_volume_lots": "0.10",
        },
    )
    await redis_client.set("client:ftmo:ftmo_001", "1", ex=30)

    order_service = OrderService(redis_svc)
    order_id_out, request_id = await order_service.close_order("ord_hedge_1")
    assert order_id_out == "ord_hedge_1"
    assert request_id
    # FTMO close cmd present on the stream.
    ftmo_entries = await redis_client.xrange(
        "cmd_stream:ftmo:ftmo_001", "-", "+"
    )
    assert len(ftmo_entries) == 1


@pytest.mark.asyncio
async def test_option_c_orphan_close_full_flow_via_api(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    alert_svc: AlertService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.8f Option C — end-to-end orphan close.

    1. Seed hedge order (both legs filled).
    2. External Exness close event lands -> 4.8e + 4.8f stamp:
       s_status=closed, composed stays "filled".
    3. Operator calls OrderService.close_order -> accepted, FTMO close
       cmd pushed.
    4. Simulate the FTMO position_closed event (event_handler stamps
       p_status=closed + invokes cascade_close_other_leg).
    5. cascade_close_other_leg fires the 4.8f orphan-close
       finalization: composed -> "closed", broadcast hedge_closed{
       outcome:"orphan_close_finalized"}, NO Exness cmd pushed.

    Final order HASH: status=closed, p_status=closed, s_status=closed,
    p_close_*, s_close_* populated.
    """
    from app.services.hedge_service import HedgeService
    from app.services.order_service import OrderService

    await _seed_hedge_order(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1",
        patch={"p_broker_order_id": "p_ticket_1", "p_volume_lots": "0.10"},
    )
    await redis_client.set("client:ftmo:ftmo_001", "1", ex=30)
    # FTMO side-index so event_handler can resolve the position_closed
    # event back to ord_hedge_1.
    await redis_svc.link_broker_order_id("p", "p_ticket_1", "ord_hedge_1")

    # 2. External Exness close lands.
    await _handle_event_entry(
        redis_svc, broadcast, "exness_001",
        _exness_close_event(close_reason="manual"),
        broker="exness", alert_service=alert_svc,
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "closed"
    assert row["status"] == "filled"

    # 3. Operator clicks Close -> close_order accepts.
    order_service = OrderService(redis_svc)
    await order_service.close_order("ord_hedge_1")

    # 4. Simulate FTMO position_closed event (skip the actual close
    # response — just deliver the event the orchestrator listens on).
    hedge_svc = HedgeService(redis_svc, broadcast)
    ftmo_close_event = {
        "event_type": "position_closed",
        "position_id": "p_ticket_1",
        "close_price": "1.08400",
        "close_time": "1735000200000",
        "realized_pnl": "10.0",
        "commission": "0.5",
        "swap": "0",
        "close_reason": "manual",
    }
    await _handle_event_entry(
        redis_svc, broadcast, "ftmo_001",
        ftmo_close_event,
        broker="ftmo",
        hedge_service=hedge_svc,
    )

    # 5. Final order state. Composed=closed via cascade short-circuit.
    final = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert final["status"] == "closed"
    assert final["p_status"] == "closed"
    assert final["s_status"] == "closed"
    assert final["p_close_price"] == "1.08400"
    assert final["s_close_price"] == "1.08425"

    # NO Exness cmd pushed in step (5) — short-circuit skipped it.
    exness_entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert exness_entries == []

    # hedge_closed broadcast with orphan_close_finalized fired.
    finalized = [
        m for _ch, m in broadcast.published
        if m.get("type") == "hedge_closed"
        and m.get("outcome") == "orphan_close_finalized"
    ]
    assert len(finalized) == 1
    assert finalized[0]["order_id"] == "ord_hedge_1"


@pytest.mark.asyncio
async def test_option_c_cascade_short_circuit_broadcasts_hedge_closed(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.8f Option C — direct unit test on the cascade short-
    circuit branch. Pre-condition: s_status="closed", composed="filled"
    (i.e. the post-external-close orphan state). Invoke
    ``cascade_close_other_leg`` directly. Assertions on the broadcast
    payload + composed-status finalization.

    Decoupled from the end-to-end full-flow test so a regression in
    the short-circuit's broadcast payload fails this test cleanly
    rather than hiding inside the orchestration chain."""
    from app.services.hedge_service import HedgeService

    await _seed_hedge_order(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1", patch={"s_status": "closed"}
    )
    hedge_svc = HedgeService(redis_svc, broadcast)

    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="A",
    )

    # Composed status finalized.
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "closed"

    # Broadcast payload exact-match assertion.
    hedge_closed = [
        m for _ch, m in broadcast.published if m.get("type") == "hedge_closed"
    ]
    assert hedge_closed == [
        {
            "type": "hedge_closed",
            "order_id": "ord_hedge_1",
            "outcome": "orphan_close_finalized",
        }
    ]

    # No Exness cmd pushed.
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []
