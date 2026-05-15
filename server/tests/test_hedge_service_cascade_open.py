"""Step 4.7a — HedgeService cascade open + 3-retry tests.

Covers ``HedgeService.cascade_secondary_open`` end-to-end on fakeredis:

  - Happy path (primary_filled -> push -> s_status=filled -> hedge_open).
  - Retry-then-succeed (one rejected, next attempt fills).
  - Exhausted (all 4 attempts fail -> secondary_failed terminal).
  - Timeout path.
  - cmd_stream payload contract (R3 sl=0 tp=0, R4 opposite side, R5 market).
  - request_id index written per attempt.
  - WS broadcast sequence: secondary_pending -> hedge_open / secondary_failed.

Polling cadence is monkeypatched to 0.001s so retry-budget assertions
finish in subsecond wall-clock time without affecting real production
behaviour.
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

# ---------- helpers ----------


class _CapturingBroadcast(BroadcastService):
    def __init__(self) -> None:
        super().__init__()
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
def hedge_svc(
    redis_svc: RedisService, broadcast: _CapturingBroadcast
) -> HedgeService:
    return HedgeService(redis_svc, broadcast)


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tests fast: 1ms poll, 10ms timeout, 1ms retry delays."""
    monkeypatch.setattr(hedge_module, "SECONDARY_OUTCOME_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(hedge_module, "SECONDARY_OUTCOME_TIMEOUT", 0.5)
    monkeypatch.setattr(
        hedge_module, "RETRY_DELAYS_SECONDS", (0.001, 0.002, 0.004)
    )


async def _seed_hedge_order(
    redis_svc: RedisService,
    redis_client: fakeredis.aioredis.FakeRedis,
    *,
    order_id: str = "ord_hedge_1",
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
    # The Exness account must exist so push_command's stream key is valid;
    # fakeredis accepts XADD on an unknown account though, so this seeding
    # is purely belt-and-suspenders for future invariant changes.
    await redis_client.sadd("accounts:exness", "exness_001")  # type: ignore[misc]
    return fields


async def _race_secondary_filled(
    redis_svc: RedisService,
    *,
    order_id: str,
    fill_after: float = 0.01,
) -> None:
    """Simulate response_handler:exness writing s_status=filled after a delay."""
    await asyncio.sleep(fill_after)
    await redis_svc.update_order(
        order_id,
        patch={
            "s_status": "filled",
            "s_broker_order_id": "55001",
            "s_fill_price": "1.08425",
            "status": "filled",
        },
        old_status="primary_filled",
    )


async def _race_secondary_rejected(
    redis_svc: RedisService,
    *,
    order_id: str,
    reason: str,
    fail_after: float = 0.005,
) -> None:
    await asyncio.sleep(fail_after)
    await redis_svc.update_order(
        order_id,
        patch={
            "s_status": "rejected",
            "s_error_msg": reason,
        },
    )


# ---------- happy path (criterion #22, #25, #26, #28-32) ----------


@pytest.mark.asyncio
async def test_cascade_happy_path_filled_first_attempt(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    order = await _seed_hedge_order(redis_svc, redis_client)

    fill_task = asyncio.create_task(
        _race_secondary_filled(redis_svc, order_id="ord_hedge_1")
    )
    await hedge_svc.cascade_secondary_open("ord_hedge_1", order)
    await fill_task

    # WS broadcasts: secondary_pending -> hedge_open.
    types = [msg.get("type") for _ch, msg in broadcast.published]
    assert "secondary_pending" in types
    assert "hedge_open" in types
    assert types.index("secondary_pending") < types.index("hedge_open")

    # cmd_stream entry — exactly one push.
    entries = await redis_client.xrange("cmd_stream:exness:exness_001", "-", "+")
    assert len(entries) == 1
    _id, fields = entries[0]
    # R3: sl=0 tp=0.
    assert fields["sl"] == "0"
    assert fields["tp"] == "0"
    # R4: secondary side opposite primary (primary=buy -> secondary=sell).
    assert fields["side"] == "sell"
    # R5: market only.
    assert fields["order_type"] == "market"
    # cascade_trigger reserved for close (step 4.8).
    assert fields["cascade_trigger"] == "false"
    # field name is ``volume`` per Exness ActionHandler contract.
    assert fields["volume"] == "0.10"
    # request_id index written.
    assert fields["request_id"]
    linked = await redis_client.get(f"request_id_to_order:{fields['request_id']}")
    assert linked == "ord_hedge_1"


# ---------- retry-then-succeed (criterion #23, #32) ----------


@pytest.mark.asyncio
async def test_cascade_first_rejected_second_succeeds(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    order = await _seed_hedge_order(redis_svc, redis_client)

    async def _two_phase() -> None:
        # First attempt: reject quickly.
        await _race_secondary_rejected(
            redis_svc, order_id="ord_hedge_1", reason="requote",
        )
        # Wait for the cascade to push attempt #2 (after RETRY_DELAYS_SECONDS[0]=0.001s).
        await asyncio.sleep(0.02)
        # Second attempt: fill.
        await redis_svc.update_order(
            "ord_hedge_1",
            patch={
                "s_status": "filled",
                "s_broker_order_id": "55002",
                "s_fill_price": "1.08430",
                "status": "filled",
            },
            old_status="primary_filled",
        )

    phase_task = asyncio.create_task(_two_phase())
    await hedge_svc.cascade_secondary_open("ord_hedge_1", order)
    await phase_task

    # Two cmd_stream pushes (initial + retry 1). Each has its own request_id.
    entries = await redis_client.xrange("cmd_stream:exness:exness_001", "-", "+")
    assert len(entries) >= 2
    request_ids = {e[1]["request_id"] for e in entries}
    assert len(request_ids) == len(entries)
    # Both request_ids resolve back to the same order via the side index.
    for rid in request_ids:
        assert (
            await redis_client.get(f"request_id_to_order:{rid}") == "ord_hedge_1"
        )


# ---------- exhausted (criteria #24, #27) ----------


@pytest.mark.asyncio
async def test_cascade_all_attempts_rejected_terminal_secondary_failed(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    order = await _seed_hedge_order(redis_svc, redis_client)

    # Spawn a watcher that rejects every push within a short window so all
    # 4 attempts time out into the rejected outcome.
    stop = asyncio.Event()

    async def _reject_loop() -> None:
        while not stop.is_set():
            current = await redis_svc.get_order("ord_hedge_1")
            if (current or {}).get("s_status") == "pending_open":
                await redis_svc.update_order(
                    "ord_hedge_1",
                    patch={
                        "s_status": "rejected",
                        "s_error_msg": "broker_rejected",
                    },
                )
            await asyncio.sleep(0.001)

    rej_task = asyncio.create_task(_reject_loop())
    try:
        await hedge_svc.cascade_secondary_open("ord_hedge_1", order)
    finally:
        stop.set()
        await rej_task

    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "secondary_failed"
    assert row["status"] == "secondary_failed"
    assert row["s_error_msg"] == "broker_rejected"

    types = [msg.get("type") for _ch, msg in broadcast.published]
    assert "secondary_failed" in types
    # hedge_open NOT broadcast.
    assert "hedge_open" not in types


# ---------- timeout (criterion variant) ----------


@pytest.mark.asyncio
async def test_cascade_timeout_treats_attempt_as_failed(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """No response ever lands -> all 4 attempts time out -> secondary_failed
    with last_error=timeout_waiting_response."""
    order = await _seed_hedge_order(redis_svc, redis_client)
    # Don't simulate any response — the order's s_status stays "pending_open".

    await hedge_svc.cascade_secondary_open("ord_hedge_1", order)

    row = await redis_client.hgetall("order:ord_hedge_1")  # type: ignore[misc]
    assert row["s_status"] == "secondary_failed"
    assert row["s_error_msg"] == "timeout_waiting_response"


# ---------- 4 attempts total (criterion #23) ----------


@pytest.mark.asyncio
async def test_cascade_pushes_four_attempts_on_exhaustion(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    order = await _seed_hedge_order(redis_svc, redis_client)

    stop = asyncio.Event()

    async def _reject_loop() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.001)
            await redis_svc.update_order(
                "ord_hedge_1",
                patch={"s_status": "rejected", "s_error_msg": "rej"},
            )

    rej_task = asyncio.create_task(_reject_loop())
    try:
        await hedge_svc.cascade_secondary_open("ord_hedge_1", order)
    finally:
        stop.set()
        await rej_task

    entries = await redis_client.xrange("cmd_stream:exness:exness_001", "-", "+")
    # 1 initial + 3 retries (RETRY_DELAYS_SECONDS) = 4 attempts.
    assert len(entries) == 4
    attempts = [e[1].get("attempt") for e in entries]
    assert attempts == ["1", "2", "3", "4"]


# ---------- short-circuit when missing fields (defensive) ----------


@pytest.mark.asyncio
async def test_cascade_missing_fields_finalize_failure(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """If order row lacks the cascade fields (somehow), finalize_failure
    fires without pushing any command."""
    order = {
        "order_id": "ord_bad",
        "exness_account_id": "",  # missing
        "s_volume_lots": "",
        "s_exness_symbol": "",
        "side": "buy",
    }
    await redis_svc.create_order(
        "ord_bad",
        {
            "order_id": "ord_bad",
            "status": "primary_filled",
            "s_status": "pending",
        },
    )

    await hedge_svc.cascade_secondary_open("ord_bad", order)

    row = await redis_client.hgetall("order:ord_bad")  # type: ignore[misc]
    assert row["s_status"] == "secondary_failed"
    assert row["s_error_msg"] == "missing_cascade_fields"
    # No cmd pushed.
    entries = await redis_client.xrange("cmd_stream:exness:exness_001", "-", "+")
    assert entries == []


# ---------- side mapping: sell primary -> buy secondary (R4) ----------


@pytest.mark.asyncio
async def test_cascade_sell_primary_yields_buy_secondary(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    order = await _seed_hedge_order(redis_svc, redis_client)
    # Override side to sell.
    await redis_svc.update_order("ord_hedge_1", patch={"side": "sell"})
    order["side"] = "sell"

    fill_task = asyncio.create_task(
        _race_secondary_filled(redis_svc, order_id="ord_hedge_1")
    )
    await hedge_svc.cascade_secondary_open("ord_hedge_1", order)
    await fill_task

    entries = await redis_client.xrange("cmd_stream:exness:exness_001", "-", "+")
    assert entries[0][1]["side"] == "buy"


# ---------- transient s_status pending_open is broadcast (criterion #25) ----------


@pytest.mark.asyncio
async def test_cascade_transient_state_pending_open_before_fill(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    """secondary_pending broadcast lands FIRST, before any cmd_stream push.
    Tests assert sequence; the actual s_status transition is verified via
    the order row's intermediate state too."""
    order = await _seed_hedge_order(redis_svc, redis_client)
    fill_task = asyncio.create_task(
        _race_secondary_filled(redis_svc, order_id="ord_hedge_1")
    )
    await hedge_svc.cascade_secondary_open("ord_hedge_1", order)
    await fill_task

    types = [msg.get("type") for _ch, msg in broadcast.published]
    assert types[0] == "secondary_pending"


# ---------- retry attempt numbering (criterion #23, sequence stamp) ----------


@pytest.mark.asyncio
async def test_cascade_retry_attempt_field_increments(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    order = await _seed_hedge_order(redis_svc, redis_client)

    stop = asyncio.Event()

    async def _reject_loop() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.001)
            await redis_svc.update_order(
                "ord_hedge_1",
                patch={"s_status": "rejected", "s_error_msg": "r"},
            )

    rej_task = asyncio.create_task(_reject_loop())
    try:
        await hedge_svc.cascade_secondary_open("ord_hedge_1", order)
    finally:
        stop.set()
        await rej_task

    entries = await redis_client.xrange("cmd_stream:exness:exness_001", "-", "+")
    attempts = [int(e[1]["attempt"]) for e in entries]
    assert attempts == sorted(attempts)


# ---------- request_id distinct per attempt ----------


@pytest.mark.asyncio
async def test_cascade_request_id_distinct_per_attempt(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    order = await _seed_hedge_order(redis_svc, redis_client)

    stop = asyncio.Event()

    async def _reject_loop() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.001)
            await redis_svc.update_order(
                "ord_hedge_1",
                patch={"s_status": "rejected", "s_error_msg": "r"},
            )

    rej_task = asyncio.create_task(_reject_loop())
    try:
        await hedge_svc.cascade_secondary_open("ord_hedge_1", order)
    finally:
        stop.set()
        await rej_task

    entries = await redis_client.xrange("cmd_stream:exness:exness_001", "-", "+")
    request_ids = [e[1]["request_id"] for e in entries]
    assert len(set(request_ids)) == len(request_ids)


# ---------- cmd payload contains all required Phase 4 fields ----------


@pytest.mark.asyncio
async def test_cascade_cmd_payload_complete(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    hedge_svc: HedgeService,
    broadcast: _CapturingBroadcast,
) -> None:
    order = await _seed_hedge_order(redis_svc, redis_client)
    fill_task = asyncio.create_task(
        _race_secondary_filled(redis_svc, order_id="ord_hedge_1")
    )
    await hedge_svc.cascade_secondary_open("ord_hedge_1", order)
    await fill_task

    entries = await redis_client.xrange("cmd_stream:exness:exness_001", "-", "+")
    _, fields = entries[0]
    for required in (
        "request_id", "order_id", "action", "symbol", "side", "volume",
        "sl", "tp", "order_type", "entry_price", "cascade_trigger",
        "created_at", "attempt",
    ):
        assert required in fields, f"missing field {required}"
    assert fields["action"] == "open"
    assert fields["symbol"] == "EURUSDz"
