"""Step 4.8 — HedgeService cascade close orchestrator tests.

Covers ``cascade_close_other_leg`` + ``complete_cascade_close`` +
``_handle_cascade_cancel_pending`` end-to-end on fakeredis:

  - Happy path: cascade close pushed, response filled -> closed + hedge_closed.
  - 3-retry exhausted -> close_failed terminal.
  - Lock contention: second trigger no-ops.
  - Idempotent abort on already-terminal status.
  - Missing position ticket -> close_failed.
  - closed_leg=p closes Exness; closed_leg=s closes FTMO.
  - cmd payload contract: action=close, broker_position_id or
    broker_order_id, cascade_trigger="true".
  - request_id_to_order side index per attempt.
  - WS broadcast sequence close_initiated -> hedge_closed / close_failed.
  - cascade_cancel_pending: primary closes mid-cascade-open -> never_filled
    terminal OR late-fill recursive close.
  - complete_cascade_close: Path C completion stamps terminal.

Retry timing is monkeypatched to subsecond so wall-clock tests stay fast.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from app.services import hedge_service as hedge_module
from app.services.broadcast import BroadcastService
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
def hedge_svc(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> HedgeService:
    return HedgeService(redis_svc, broadcast)


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hedge_module, "SECONDARY_OUTCOME_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(hedge_module, "SECONDARY_OUTCOME_TIMEOUT", 0.3)
    monkeypatch.setattr(
        hedge_module, "RETRY_DELAYS_SECONDS", (0.001, 0.002, 0.004)
    )
    # cascade_cancel_pending late-fill wait
    monkeypatch.setattr(HedgeService, "_LATE_FILL_WAIT_SECONDS", 0.05)


async def _seed_filled_hedge(
    redis_svc: RedisService,
    *,
    order_id: str = "ord_hedge_1",
    p_broker: str = "9001",
    s_broker: str = "55001",
) -> dict[str, str]:
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
    await redis_svc.create_order(order_id, fields)
    await redis_svc.link_broker_order_id("p", p_broker, order_id)
    await redis_svc.link_broker_order_id("s", s_broker, order_id)
    return fields


async def _race_leg_closed(
    redis_svc: RedisService,
    order_id: str,
    leg: str,
    *,
    fill_after: float = 0.01,
) -> None:
    await asyncio.sleep(fill_after)
    await redis_svc.update_order(
        order_id,
        patch={f"{leg}_status": "closed", f"{leg}_close_price": "1.08"},
    )


async def _race_leg_rejected(
    redis_svc: RedisService,
    order_id: str,
    leg: str,
    *,
    reason: str,
    fail_after: float = 0.005,
) -> None:
    await asyncio.sleep(fail_after)
    await redis_svc.update_order(
        order_id,
        patch={
            f"{leg}_status": "rejected",
            f"{leg}_close_error_msg": reason,
        },
    )


# ---------- happy path (criteria #10, #15, #18-24) ----------


@pytest.mark.asyncio
async def test_cascade_close_happy_closed_leg_p_closes_exness(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_filled_hedge(redis_svc)
    fill_task = asyncio.create_task(
        _race_leg_closed(redis_svc, "ord_hedge_1", "s")
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )
    await fill_task

    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "closed"
    assert row["p_status"] == "closed"
    assert row["s_status"] == "closed"
    assert row["p_close_reason"] == "manual"
    # cmd pushed to Exness, not FTMO.
    exness_entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    ftmo_entries = await redis_client.xrange(
        "cmd_stream:ftmo:ftmo_001", "-", "+"
    )
    assert len(exness_entries) == 1
    assert ftmo_entries == []
    _id, payload = exness_entries[0]
    # Criterion #18, #19: action=close, market, cascade_trigger=true.
    assert payload["action"] == "close"
    assert payload["order_type"] == "market"
    assert payload["cascade_trigger"] == "true"
    # Exness uses broker_position_id field name (D-4.7b-1 in 4.7b).
    assert payload["broker_position_id"] == "55001"
    # WS broadcast sequence.
    types = [m.get("type") for _ch, m in broadcast.published]
    assert types.index("close_initiated") < types.index("hedge_closed")
    # request_id index populated.
    rid = payload["request_id"]
    assert await redis_client.get(f"request_id_to_order:{rid}") == "ord_hedge_1"


@pytest.mark.asyncio
async def test_cascade_close_closed_leg_s_closes_ftmo(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """closed_leg=s → cmd dispatched to FTMO (uses broker_order_id field)."""
    await _seed_filled_hedge(redis_svc)
    fill_task = asyncio.create_task(
        _race_leg_closed(redis_svc, "ord_hedge_1", "p")
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="s", close_reason="server_initiated", trigger_path="C",
    )
    await fill_task
    entries = await redis_client.xrange(
        "cmd_stream:ftmo:ftmo_001", "-", "+"
    )
    assert len(entries) == 1
    _id, payload = entries[0]
    assert payload["broker_order_id"] == "9001"  # FTMO uses broker_order_id


# ---------- 3-retry exhausted (criterion #11) ----------


@pytest.mark.asyncio
async def test_cascade_close_three_retries_exhausted_terminal(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_filled_hedge(redis_svc)

    stop = asyncio.Event()

    async def _reject_loop() -> None:
        while not stop.is_set():
            current = await redis_svc.get_order("ord_hedge_1")
            if (current or {}).get("s_status") == "close_pending":
                await redis_svc.update_order(
                    "ord_hedge_1",
                    patch={
                        "s_status": "rejected",
                        "s_close_error_msg": "broker_rejected",
                    },
                )
            await asyncio.sleep(0.001)

    rej_task = asyncio.create_task(_reject_loop())
    try:
        await hedge_svc.cascade_close_other_leg(
            "ord_hedge_1",
            closed_leg="p", close_reason="manual", trigger_path="B",
        )
    finally:
        stop.set()
        await rej_task

    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "close_failed"
    assert row["close_error_msg"] == "broker_rejected"
    # Four pushes (1 initial + 3 retries).
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert len(entries) == 4
    types = [m.get("type") for _ch, m in broadcast.published]
    assert "close_failed" in types
    assert "hedge_closed" not in types


# ---------- lock contention (criterion #12) ----------


@pytest.mark.asyncio
async def test_cascade_close_lock_contention_second_aborts(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Two triggers fire near-simultaneously; only one cascade runs."""
    await _seed_filled_hedge(redis_svc)

    async def _runner(trigger_path: str) -> None:
        await hedge_svc.cascade_close_other_leg(
            "ord_hedge_1",
            closed_leg="p", close_reason="manual", trigger_path=trigger_path,
        )

    # Start path A; while it polls, fire path D simultaneously.
    fill_task = asyncio.create_task(
        _race_leg_closed(redis_svc, "ord_hedge_1", "s", fill_after=0.02)
    )
    await asyncio.gather(_runner("A"), _runner("D"))
    await fill_task

    # Exactly ONE cmd pushed despite two callers.
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert len(entries) == 1


# ---------- already-terminal idempotency (criterion #14) ----------


@pytest.mark.asyncio
async def test_cascade_close_no_op_on_already_closed(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_filled_hedge(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1", patch={"status": "closed"}
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []


@pytest.mark.asyncio
async def test_cascade_close_no_op_on_close_failed(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
) -> None:
    await _seed_filled_hedge(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1", patch={"status": "close_failed"}
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []


# ---------- missing ticket (criterion #17) ----------


@pytest.mark.asyncio
async def test_cascade_close_missing_other_position_ticket(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Other-leg ticket missing → close_failed structured error."""
    await _seed_filled_hedge(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1", patch={"s_broker_order_id": ""}
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "close_failed"
    assert row["close_error_msg"] == "missing_position_ticket"


# ---------- order not found (criterion #13) ----------


@pytest.mark.asyncio
async def test_cascade_close_order_not_found_no_crash(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
) -> None:
    """Stale event for a deleted order: warns + releases lock + returns."""
    await hedge_svc.cascade_close_other_leg(
        "ord_ghost",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )
    # Lock released.
    assert await redis_svc.read_cascade_lock("ord_ghost") is None


# ---------- complete_cascade_close — Path C ----------


@pytest.mark.asyncio
async def test_complete_cascade_close_stamps_terminal(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Path C completion: secondary's server_initiated close event lands;
    stamp closed terminal + release lock."""
    await _seed_filled_hedge(redis_svc)
    # Pre-acquire the lock as if cascade_close_other_leg holds it.
    await redis_svc.acquire_cascade_lock("ord_hedge_1", "A")
    await redis_svc.update_order(
        "ord_hedge_1", patch={"status": "close_pending"}
    )
    await hedge_svc.complete_cascade_close(
        "ord_hedge_1", closed_leg="s", close_reason="server_initiated",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "closed"
    assert row["s_status"] == "closed"
    assert row["s_close_reason"] == "server_initiated"
    # Lock released.
    assert await redis_svc.read_cascade_lock("ord_hedge_1") is None


@pytest.mark.asyncio
async def test_complete_cascade_close_idempotent_on_terminal(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
) -> None:
    """Late duplicate event after status=closed: no-op."""
    await _seed_filled_hedge(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1", patch={"status": "closed"}
    )
    await hedge_svc.complete_cascade_close(
        "ord_hedge_1", closed_leg="s", close_reason="server_initiated",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "closed"


# ---------- cascade_cancel_pending (criteria #25-32) ----------


@pytest.mark.asyncio
async def test_cancel_pending_never_filled_terminal(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Primary closes externally while secondary cascade-open is still
    in flight; secondary never fills → ``s_status=never_filled``
    terminal + ``status=closed``."""
    await _seed_filled_hedge(redis_svc)
    # Reset to primary_filled (mid-cascade-open state).
    await redis_svc.update_order(
        "ord_hedge_1",
        patch={
            "status": "primary_filled",
            "s_status": "pending",
            "s_broker_order_id": "",
        },
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="sl", trigger_path="D",
    )
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "closed"
    assert row["s_status"] == "never_filled"
    assert row["p_status"] == "closed"
    assert row["p_close_reason"] == "sl"
    types = [m.get("type") for _ch, m in broadcast.published]
    assert "cascade_cancel_pending" in types
    assert "hedge_closed" in types


@pytest.mark.asyncio
async def test_cancel_pending_late_fill_triggers_recursive_close(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Primary closes mid-cascade; secondary fills late within the
    2s window; recursive cascade close runs."""
    await _seed_filled_hedge(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1",
        patch={"status": "primary_filled", "s_status": "pending_open"},
    )

    async def _late_fill() -> None:
        # Sleep just less than LATE_FILL_WAIT (50ms) so the late-fill
        # is observed.
        await asyncio.sleep(0.02)
        await redis_svc.update_order(
            "ord_hedge_1",
            patch={"s_status": "filled", "s_broker_order_id": "55001"},
        )

    async def _close_secondary_after_recursive() -> None:
        # After the recursive cascade pushes the close cmd, simulate
        # the broker filling the close.
        await asyncio.sleep(0.08)
        await redis_svc.update_order(
            "ord_hedge_1", patch={"s_status": "closed"}
        )

    fill_task = asyncio.create_task(_late_fill())
    close_task = asyncio.create_task(_close_secondary_after_recursive())
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="sl", trigger_path="D",
    )
    await fill_task
    await close_task

    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "closed"
    types = [m.get("type") for _ch, m in broadcast.published]
    assert "cascade_cancel_pending" in types


@pytest.mark.asyncio
async def test_cancel_pending_broadcasts_before_wait(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """cascade_cancel_pending WS message fires before the late-fill window."""
    await _seed_filled_hedge(redis_svc)
    await redis_svc.update_order(
        "ord_hedge_1",
        patch={"status": "primary_filled", "s_status": "pending"},
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )
    types = [m.get("type") for _ch, m in broadcast.published]
    assert types[0] == "cascade_cancel_pending"


# ---------- cmd_payload contract (criteria #18-21) ----------


@pytest.mark.asyncio
async def test_cascade_close_cmd_payload_required_fields(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
) -> None:
    await _seed_filled_hedge(redis_svc)
    fill_task = asyncio.create_task(
        _race_leg_closed(redis_svc, "ord_hedge_1", "s")
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )
    await fill_task

    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    payload = entries[0][1]
    for required in (
        "order_id", "action", "order_type", "cascade_trigger",
        "broker_position_id", "request_id", "attempt",
    ):
        assert required in payload, f"missing {required}"


@pytest.mark.asyncio
async def test_cascade_close_request_ids_distinct_per_attempt(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
) -> None:
    await _seed_filled_hedge(redis_svc)

    stop = asyncio.Event()

    async def _reject_loop() -> None:
        while not stop.is_set():
            current = await redis_svc.get_order("ord_hedge_1")
            if (current or {}).get("s_status") == "close_pending":
                await redis_svc.update_order(
                    "ord_hedge_1",
                    patch={"s_status": "rejected", "s_close_error_msg": "r"},
                )
            await asyncio.sleep(0.001)

    rej_task = asyncio.create_task(_reject_loop())
    try:
        await hedge_svc.cascade_close_other_leg(
            "ord_hedge_1",
            closed_leg="p", close_reason="manual", trigger_path="B",
        )
    finally:
        stop.set()
        await rej_task

    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    request_ids = [e[1]["request_id"] for e in entries]
    assert len(set(request_ids)) == len(request_ids)


# ---------- lock value records winning trigger_path (criterion #30) ----------


@pytest.mark.asyncio
async def test_cascade_close_lock_value_records_trigger_path(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
) -> None:
    """During the cascade, the lock's value is the trigger_path; can be
    inspected by an audit job. Released on terminal."""
    await _seed_filled_hedge(redis_svc)
    # Acquire-but-stall scenario: we'll inspect mid-flight via a parallel
    # coroutine that observes the lock before the fill races back.

    observed: list[str | None] = []

    async def _race_observe_then_fill() -> None:
        # Wait long enough that the cascade has pushed its first cmd
        # (after the initial 0 delay) and is polling for an outcome.
        await asyncio.sleep(0.005)
        observed.append(await redis_svc.read_cascade_lock("ord_hedge_1"))
        await redis_svc.update_order(
            "ord_hedge_1", patch={"s_status": "closed"}
        )

    obs_task = asyncio.create_task(_race_observe_then_fill())
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="tp", trigger_path="D",
    )
    await obs_task

    assert observed == ["D"]
    # Lock released after terminal.
    assert await redis_svc.read_cascade_lock("ord_hedge_1") is None


# ---------- close_pending transition assertion ----------


@pytest.mark.asyncio
async def test_cascade_close_transitions_through_close_pending(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
) -> None:
    await _seed_filled_hedge(redis_svc)

    seen: list[str] = []

    async def _observe() -> None:
        await asyncio.sleep(0.003)
        row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
        seen.append(row.get("status", ""))
        # Now fill the secondary close.
        await redis_svc.update_order(
            "ord_hedge_1", patch={"s_status": "closed"}
        )

    obs_task = asyncio.create_task(_observe())
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )
    await obs_task

    assert "close_pending" in seen
    final = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert final["status"] == "closed"


# ---------- close_initiated payload ----------


@pytest.mark.asyncio
async def test_close_initiated_broadcast_includes_trigger_path(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_filled_hedge(redis_svc)
    fill_task = asyncio.create_task(
        _race_leg_closed(redis_svc, "ord_hedge_1", "s")
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="sl", trigger_path="D",
    )
    await fill_task

    msgs = [m for _ch, m in broadcast.published if m.get("type") == "close_initiated"]
    assert len(msgs) == 1
    assert msgs[0]["trigger_path"] == "D"
    assert msgs[0]["closed_leg"] == "p"


# ---------------------------------------------------------------------------
# Step 4.8e + 4.8f Option C — orphan-close finalization short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cascade_close_orphan_finalization_when_s_status_closed(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Step 4.8f Option C — orphan-close finalization. The secondary
    leg closed externally and its ``s_status="closed"`` was stamped by
    event_handler's external-close branch (composed kept at
    ``"filled"`` so the operator's Close button stays available). The
    operator then clicks Close, the FTMO leg closes, and we land here.

    The cascade orchestrator MUST:
      1. Detect ``s_status="closed"`` (the Exness position is gone).
      2. SKIP the Exness cmd push (would 400 with
         ``position_not_found``).
      3. Stamp composed ``status="closed"``.
      4. Broadcast ``hedge_closed`` with
         ``outcome="orphan_close_finalized"`` so the frontend drops
         the row from the Open tab.
      5. Release the cascade lock cleanly.

    Inverted from the original 4.8e test which only asserted the bare
    no-op return — 4.8f extends the short-circuit to also finalize the
    composed-status transition + broadcast, since under Option C
    composed is the ONLY signal the frontend uses to drop the row.
    """
    await _seed_filled_hedge(redis_svc)
    # Simulate the 4.8e + 4.8f Option C external-close stamp:
    # s_status=closed lands, composed stays "filled".
    await redis_svc.update_order(
        "ord_hedge_1", patch={"s_status": "closed"}
    )

    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )

    # 1+2. No Exness cmd pushed — short-circuit fires.
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert entries == []
    # No close_initiated broadcast (the cascade never advanced into the
    # retry loop that emits close_initiated).
    types = [m.get("type") for _ch, m in broadcast.published]
    assert "close_initiated" not in types

    # 3. Composed status flipped to "closed" by the finalization branch.
    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["status"] == "closed"
    # 4. hedge_closed broadcast with outcome=orphan_close_finalized.
    hedge_closed_msgs = [
        m for _ch, m in broadcast.published if m.get("type") == "hedge_closed"
    ]
    assert len(hedge_closed_msgs) == 1
    assert hedge_closed_msgs[0]["order_id"] == "ord_hedge_1"
    assert hedge_closed_msgs[0]["outcome"] == "orphan_close_finalized"
    # 5. Lock released cleanly.
    assert await redis_svc.read_cascade_lock("ord_hedge_1") is None


@pytest.mark.asyncio
async def test_cascade_close_proceeds_when_s_status_filled(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Regression: the belt-and-suspenders short-circuit must NOT fire
    on the happy path (s_status="filled"). Cascade proceeds normally."""
    await _seed_filled_hedge(redis_svc)
    fill_task = asyncio.create_task(
        _race_leg_closed(redis_svc, "ord_hedge_1", "s")
    )
    await hedge_svc.cascade_close_other_leg(
        "ord_hedge_1",
        closed_leg="p", close_reason="manual", trigger_path="B",
    )
    await fill_task

    # Cascade fired — exactly one cmd pushed.
    entries = await redis_client.xrange(
        "cmd_stream:exness:exness_001", "-", "+"
    )
    assert len(entries) == 1
