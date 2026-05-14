"""Tests for ``PositionMonitor`` (Phase 4.3).

The monitor runs against the ``mt5_stub`` so we can simulate a live MT5
broker by mutating its ``positions_get`` state mid-test (open / close /
SL-edit). Every test asserts behaviour through the resulting
``event_stream:exness:{account_id}`` payloads — the same contract the
server's cascade orchestrator will read in step 4.7/4.8.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import fakeredis.aioredis
import pytest

from exness_client import mt5_stub
from exness_client.cmd_ledger import CmdLedger
from exness_client.position_monitor import (
    POLL_INTERVAL_S,
    PositionMonitor,
    PositionSnapshot,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _pos(
    *,
    ticket: int,
    symbol: str = "EURUSDm",
    pos_type: int = mt5_stub.POSITION_TYPE_BUY,
    volume: float = 0.10,
    price_open: float = 1.0850,
    sl: float = 0.0,
    tp: float = 0.0,
    magic: int = 424242,
) -> mt5_stub.Position:
    return mt5_stub.Position(
        ticket=ticket,
        symbol=symbol,
        type=pos_type,
        volume=volume,
        price_open=price_open,
        magic=magic,
        sl=sl,
        tp=tp,
    )


@pytest.fixture
def cmd_ledger(fake_redis: fakeredis.aioredis.FakeRedis) -> CmdLedger:
    return CmdLedger(fake_redis, "exness_001")


@pytest.fixture
def monitor(
    fake_redis: fakeredis.aioredis.FakeRedis, cmd_ledger: CmdLedger
) -> PositionMonitor:
    return PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)


async def _read_events(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> list[dict[str, str]]:
    """Drain every entry from ``event_stream:exness:exness_001``."""
    entries = await fake_redis.xrange("event_stream:exness:exness_001")
    return [dict(fields) for _msg_id, fields in entries]


# ---------------------------------------------------------------------------
# §2.4.1 — Baseline behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_poll_baseline_emits_no_events(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=100), _pos(ticket=101)])
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert events == []
    assert monitor._baseline_done is True
    assert set(monitor._last_snapshot.keys()) == {100, 101}


@pytest.mark.asyncio
async def test_baseline_with_zero_positions(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert events == []
    assert monitor._baseline_done is True
    assert monitor._last_snapshot == {}


@pytest.mark.asyncio
async def test_baseline_stores_full_snapshot(
    monitor: PositionMonitor,
) -> None:
    mt5_stub._set_positions_for_tests([
        _pos(ticket=200, sl=1.08, tp=1.10),
        _pos(ticket=201, sl=0.0, tp=0.0),
    ])
    await monitor._poll_once()
    snap_200 = monitor._last_snapshot[200]
    assert snap_200.sl == 1.08
    assert snap_200.tp == 1.10
    assert snap_200.position_type == mt5_stub.POSITION_TYPE_BUY


# ---------------------------------------------------------------------------
# §2.4.2 — New position detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_position_after_baseline_emits_event(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await monitor._poll_once()  # baseline empty
    mt5_stub._set_positions_for_tests([_pos(ticket=300, sl=1.08, tp=1.10)])
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "position_new"
    assert e["broker_position_id"] == "300"
    assert e["symbol"] == "EURUSDm"
    assert e["side"] == "buy"
    assert e["volume"] == "0.1"
    assert e["sl"] == "1.08"
    assert e["tp"] == "1.1"
    assert e["entry_price"] == "1.085"


@pytest.mark.asyncio
async def test_multiple_new_positions_emit_one_event_each(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await monitor._poll_once()
    mt5_stub._set_positions_for_tests([
        _pos(ticket=301), _pos(ticket=302), _pos(ticket=303),
    ])
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert len(events) == 3
    assert {e["broker_position_id"] for e in events} == {"301", "302", "303"}
    assert all(e["event_type"] == "position_new" for e in events)


@pytest.mark.asyncio
async def test_new_position_event_has_timestamp(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await monitor._poll_once()
    mt5_stub._set_positions_for_tests([_pos(ticket=400)])
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert events[0]["ts_ms"]
    assert int(events[0]["ts_ms"]) > 0


@pytest.mark.asyncio
async def test_new_position_sell_side_label(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await monitor._poll_once()
    mt5_stub._set_positions_for_tests(
        [_pos(ticket=410, pos_type=mt5_stub.POSITION_TYPE_SELL)]
    )
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert events[0]["side"] == "sell"


# ---------------------------------------------------------------------------
# §2.4.3 — Closed external detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_position_removed_after_baseline_emits_closed_external(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=500, sl=1.08, tp=1.10, volume=0.20)])
    await monitor._poll_once()
    mt5_stub._remove_position_for_tests(500)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "position_closed_external"
    assert e["broker_position_id"] == "500"
    assert e["last_volume"] == "0.2"
    assert e["last_sl"] == "1.08"
    assert e["last_tp"] == "1.1"


@pytest.mark.asyncio
async def test_multiple_closed_in_single_poll(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests(
        [_pos(ticket=510), _pos(ticket=511), _pos(ticket=512)]
    )
    await monitor._poll_once()
    mt5_stub._remove_position_for_tests(510)
    mt5_stub._remove_position_for_tests(512)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert {e["broker_position_id"] for e in closed} == {"510", "512"}


@pytest.mark.asyncio
async def test_closed_event_uses_last_snapshot_data(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests(
        [_pos(ticket=520, symbol="GBPUSDm", pos_type=mt5_stub.POSITION_TYPE_SELL, volume=0.05)]
    )
    await monitor._poll_once()
    mt5_stub._remove_position_for_tests(520)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    e = events[0]
    assert e["symbol"] == "GBPUSDm"
    assert e["side"] == "sell"
    assert e["last_volume"] == "0.05"


@pytest.mark.asyncio
async def test_closed_event_no_cascade_trigger_marker(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    """The monitor publishes the *source* of a cascade — the server adds
    the ``cascade_trigger`` marker when it issues the cmd_stream close."""
    mt5_stub._set_positions_for_tests([_pos(ticket=530)])
    await monitor._poll_once()
    mt5_stub._remove_position_for_tests(530)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert "cascade_trigger" not in events[0]


# ---------------------------------------------------------------------------
# §2.4.4 — Modified position detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sl_change_emits_modified(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=600, sl=1.08)])
    await monitor._poll_once()
    mt5_stub._mutate_position_for_tests(600, sl=1.0750)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "position_modified"
    assert e["changed_fields"] == "sl"
    assert e["new_sl"] == "1.075"
    assert e["old_sl"] == "1.08"


@pytest.mark.asyncio
async def test_tp_change_emits_modified(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=601, tp=1.10)])
    await monitor._poll_once()
    mt5_stub._mutate_position_for_tests(601, tp=1.12)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    e = events[0]
    assert e["event_type"] == "position_modified"
    assert e["changed_fields"] == "tp"
    assert e["new_tp"] == "1.12"


@pytest.mark.asyncio
async def test_volume_change_emits_modified(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=602, volume=0.20)])
    await monitor._poll_once()
    mt5_stub._mutate_position_for_tests(602, volume=0.05)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    e = events[0]
    assert e["changed_fields"] == "volume"
    assert e["new_volume"] == "0.05"
    assert e["old_volume"] == "0.2"


@pytest.mark.asyncio
async def test_multiple_field_changes_comma_joined(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=603, sl=1.08, tp=1.10)])
    await monitor._poll_once()
    mt5_stub._mutate_position_for_tests(603, sl=1.07, tp=1.12)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    e = events[0]
    assert e["changed_fields"] == "sl,tp"


@pytest.mark.asyncio
async def test_no_change_emits_no_event(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=604, sl=1.08, tp=1.10)])
    await monitor._poll_once()
    # Same poll twice — nothing changed.
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert events == []


# ---------------------------------------------------------------------------
# §2.4.5 — Combined diff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_combined_diff_new_closed_modified_single_poll(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    # Baseline: 700 (will be modified), 701 (will be closed).
    mt5_stub._set_positions_for_tests([
        _pos(ticket=700, sl=1.08), _pos(ticket=701),
    ])
    await monitor._poll_once()
    # Now: 700 still here with new SL, 701 gone, 702 brand new.
    mt5_stub._mutate_position_for_tests(700, sl=1.07)
    mt5_stub._remove_position_for_tests(701)
    positions = [*mt5_stub._state["positions_get"], _pos(ticket=702)]
    mt5_stub._set_positions_for_tests(positions)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert len(events) == 3
    by_type = {e["event_type"]: e for e in events}
    assert by_type["position_new"]["broker_position_id"] == "702"
    assert by_type["position_closed_external"]["broker_position_id"] == "701"
    assert by_type["position_modified"]["broker_position_id"] == "700"


@pytest.mark.asyncio
async def test_combined_diff_event_order_deterministic(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=t) for t in (810, 820, 830)])
    await monitor._poll_once()
    # Drop 810 and 830, add 815 and 825.
    mt5_stub._set_positions_for_tests([
        _pos(ticket=820), _pos(ticket=815), _pos(ticket=825),
    ])
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    # Order: news first (815, 825), then closed (810, 830), each sorted.
    types_and_tickets = [(e["event_type"], int(e["broker_position_id"])) for e in events]
    assert types_and_tickets == [
        ("position_new", 815),
        ("position_new", 825),
        ("position_closed_external", 810),
        ("position_closed_external", 830),
    ]


# ---------------------------------------------------------------------------
# §2.4.6 — Error resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positions_get_exception_logged_no_crash(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mt5_stub.set_state_for_tests(positions_get_raises=RuntimeError("disconnect"))
    with caplog.at_level("ERROR"):
        await monitor._poll_once()
    assert any(
        "position_monitor.positions_get_exception" in r.message for r in caplog.records
    )
    # Baseline NOT marked done so a recovered MT5 still runs the silent
    # baseline pass on the next poll.
    assert monitor._baseline_done is False
    events = await _read_events(fake_redis)
    assert events == []


@pytest.mark.asyncio
async def test_positions_get_returns_none_treated_as_empty(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Baseline with one position.
    mt5_stub._set_positions_for_tests([_pos(ticket=900)])
    await monitor._poll_once()
    # Now the broker returns None → monitor treats as empty → ticket
    # 900 disappears → emits position_closed_external.
    monkeypatch.setattr(mt5_stub, "positions_get", lambda **_kw: None)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    assert len(events) == 1
    assert events[0]["event_type"] == "position_closed_external"


@pytest.mark.asyncio
async def test_redis_xadd_failure_logged_no_crash(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mt5_stub._set_positions_for_tests([])
    await monitor._poll_once()
    mt5_stub._set_positions_for_tests([_pos(ticket=910)])

    async def _boom_xadd(*_args: Any, **_kw: Any) -> None:
        raise RuntimeError("redis_dropped")

    monkeypatch.setattr(fake_redis, "xadd", _boom_xadd)
    with caplog.at_level("ERROR"):
        await monitor._poll_once()
    assert any("position_event.xadd_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_loop_continues_after_exception(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """First poll raises (no baseline). Second poll succeeds → baseline.
    Third poll detects a new position. Verifies the run() loop survives
    a transient error without losing the next-cycle behavior."""
    monitor._poll_interval_s = 0.01  # speed up
    # Arrange: exception first, then good data.
    call_count = {"n": 0}

    async def _scripted_poll() -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first_poll_blip")
        if call_count["n"] == 2:
            mt5_stub._set_positions_for_tests([])
            await PositionMonitor._poll_once(monitor)
        elif call_count["n"] == 3:
            mt5_stub._set_positions_for_tests([_pos(ticket=920)])
            await PositionMonitor._poll_once(monitor)
        else:
            await monitor.stop()

    # Bind the scripted poll into the monitor instance.
    monitor._poll_once = _scripted_poll  # type: ignore[method-assign]
    await asyncio.wait_for(monitor.run(), timeout=2)
    events = await _read_events(fake_redis)
    assert any(e["event_type"] == "position_new" for e in events)


# ---------------------------------------------------------------------------
# §2.4.7 — Lifecycle integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_exits_loop_within_interval_plus_overhead(
    monitor: PositionMonitor,
) -> None:
    monitor._poll_interval_s = 0.10
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.05)
    await monitor.stop()
    await asyncio.wait_for(task, timeout=0.5)
    assert task.done() and task.exception() is None


@pytest.mark.asyncio
async def test_run_then_three_polls_then_stop(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    monitor._poll_interval_s = 0.05
    mt5_stub._set_positions_for_tests([])
    task = asyncio.create_task(monitor.run())
    # Let three poll cycles run.
    await asyncio.sleep(0.20)
    await monitor.stop()
    await asyncio.wait_for(task, timeout=0.5)
    # The loop ran without crashing — that's the contract here.
    assert task.done() and task.exception() is None


@pytest.mark.asyncio
async def test_concurrent_stop_calls_safe(
    monitor: PositionMonitor,
) -> None:
    monitor._poll_interval_s = 0.05
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.02)
    await asyncio.gather(monitor.stop(), monitor.stop(), monitor.stop())
    await asyncio.wait_for(task, timeout=0.3)
    assert task.done()


# ---------------------------------------------------------------------------
# PositionSnapshot dataclass
# ---------------------------------------------------------------------------


def test_position_snapshot_equality_and_field_diff() -> None:
    a = PositionSnapshot(
        ticket=1, symbol="EURUSDm", volume=0.1, sl=1.08, tp=1.10, position_type=0
    )
    b = PositionSnapshot(
        ticket=1, symbol="EURUSDm", volume=0.1, sl=1.08, tp=1.10, position_type=0
    )
    c = PositionSnapshot(
        ticket=1, symbol="EURUSDm", volume=0.1, sl=1.07, tp=1.10, position_type=0
    )
    assert a == b
    assert a != c


def test_poll_interval_constant() -> None:
    assert POLL_INTERVAL_S == 2.0


# ---------------------------------------------------------------------------
# Phase 4.3a — persistent snapshot
# ---------------------------------------------------------------------------


def _deal(
    *,
    position_id: int,
    entry: int = mt5_stub.DEAL_ENTRY_OUT,
    price: float = 1.083,
    profit: float = -3.20,
    commission: float = 0.0,
    swap: float = 0.0,
    time_seconds: int = 1747225180,
    ticket: int = 0,
    symbol: str = "EURUSDm",
) -> mt5_stub.Deal:
    return mt5_stub.Deal(
        ticket=ticket or position_id + 1,
        order=position_id + 100,
        time=time_seconds,
        time_msc=time_seconds * 1000,
        type=mt5_stub.ORDER_TYPE_SELL,
        entry=entry,
        magic=424242,
        position_id=position_id,
        reason=0,
        volume=0.1,
        price=price,
        commission=commission,
        swap=swap,
        profit=profit,
        fee=0.0,
        symbol=symbol,
        comment="",
    )


@pytest.mark.asyncio
async def test_snapshot_persisted_after_baseline_init(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=1000, sl=1.08)])
    await monitor._poll_once()
    raw = await fake_redis.get(monitor._snapshot_key)
    assert raw is not None
    payload = json.loads(raw)
    assert payload["schema_version"] == 1
    assert len(payload["positions"]) == 1
    assert payload["positions"][0]["ticket"] == 1000


@pytest.mark.asyncio
async def test_snapshot_persisted_after_diff_poll(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([])
    await monitor._poll_once()
    mt5_stub._set_positions_for_tests([_pos(ticket=1001)])
    await monitor._poll_once()
    payload = json.loads(await fake_redis.get(monitor._snapshot_key))
    assert payload["positions"][0]["ticket"] == 1001


@pytest.mark.asyncio
async def test_snapshot_persist_failure_logged_no_crash(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _boom_set(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("redis_dropped")

    monkeypatch.setattr(fake_redis, "set", _boom_set)
    mt5_stub._set_positions_for_tests([_pos(ticket=1002)])
    with caplog.at_level("ERROR"):
        await monitor._poll_once()
    assert any(
        "position_monitor.snapshot_persist_failed" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_snapshot_payload_has_schema_version_field(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await monitor._poll_once()
    payload = json.loads(await fake_redis.get(monitor._snapshot_key))
    assert payload["schema_version"] == monitor.SNAPSHOT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_snapshot_ttl_30_days(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await monitor._poll_once()
    ttl = await fake_redis.ttl(monitor._snapshot_key)
    expected = monitor.SNAPSHOT_TTL_SECONDS
    assert expected - 1 <= ttl <= expected


@pytest.mark.asyncio
async def test_load_snapshot_on_connect_uses_as_baseline(
    fake_redis: fakeredis.aioredis.FakeRedis, cmd_ledger: CmdLedger
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=1100, sl=1.08)])
    m1 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m1._poll_once()
    m2 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    loaded = await m2._load_persisted_snapshot()
    assert loaded is not None
    assert 1100 in loaded
    assert loaded[1100].sl == 1.08


@pytest.mark.asyncio
async def test_load_snapshot_missing_returns_none(
    monitor: PositionMonitor, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        loaded = await monitor._load_persisted_snapshot()
    assert loaded is None
    assert any(
        "position_monitor.no_persisted_snapshot" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_load_snapshot_malformed_json_logs_warning(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await fake_redis.set(monitor._snapshot_key, "{not valid json")
    with caplog.at_level("WARNING"):
        loaded = await monitor._load_persisted_snapshot()
    assert loaded is None
    assert any(
        "position_monitor.snapshot_malformed_json" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_load_snapshot_schema_mismatch_logs_warning(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await fake_redis.set(
        monitor._snapshot_key,
        json.dumps({"schema_version": 99, "positions": []}),
    )
    with caplog.at_level("WARNING"):
        loaded = await monitor._load_persisted_snapshot()
    assert loaded is None
    assert any(
        "position_monitor.snapshot_schema_mismatch" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_load_snapshot_invalid_entry_skipped(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await fake_redis.set(
        monitor._snapshot_key,
        json.dumps(
            {
                "schema_version": 1,
                "last_poll_ts_ms": 0,
                "positions": [
                    {
                        "ticket": 1200, "symbol": "EURUSDm",
                        "volume": 0.1, "sl": 1.08, "tp": 1.10,
                        "position_type": 0,
                    },
                    {"ticket": "junk"},
                ],
            }
        ),
    )
    loaded = await monitor._load_persisted_snapshot()
    assert loaded is not None
    assert 1200 in loaded
    assert len(loaded) == 1


# ---------------------------------------------------------------------------
# Phase 4.3a — Offline diff detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_close_detected_after_restart(
    fake_redis: fakeredis.aioredis.FakeRedis, cmd_ledger: CmdLedger
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=2000)])
    m1 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m1._poll_once()
    mt5_stub._remove_position_for_tests(2000)
    m2 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m2._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert len(closed) == 1
    assert closed[0]["broker_position_id"] == "2000"


@pytest.mark.asyncio
async def test_offline_modify_detected_after_restart(
    fake_redis: fakeredis.aioredis.FakeRedis, cmd_ledger: CmdLedger
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=2010, sl=1.08, tp=1.10)])
    m1 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m1._poll_once()
    mt5_stub._mutate_position_for_tests(2010, sl=1.07)
    m2 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m2._poll_once()
    events = await _read_events(fake_redis)
    modified = [e for e in events if e["event_type"] == "position_modified"]
    assert len(modified) == 1
    assert modified[0]["broker_position_id"] == "2010"
    assert modified[0]["changed_fields"] == "sl"


@pytest.mark.asyncio
async def test_offline_new_position_detected_after_restart(
    fake_redis: fakeredis.aioredis.FakeRedis, cmd_ledger: CmdLedger
) -> None:
    mt5_stub._set_positions_for_tests([])
    m1 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m1._poll_once()
    mt5_stub._set_positions_for_tests([_pos(ticket=2020)])
    m2 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m2._poll_once()
    events = await _read_events(fake_redis)
    news = [e for e in events if e["event_type"] == "position_new"]
    assert len(news) == 1
    assert news[0]["broker_position_id"] == "2020"


@pytest.mark.asyncio
async def test_offline_multiple_changes_detected(
    fake_redis: fakeredis.aioredis.FakeRedis, cmd_ledger: CmdLedger
) -> None:
    mt5_stub._set_positions_for_tests([
        _pos(ticket=2030), _pos(ticket=2031, sl=1.08),
    ])
    m1 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m1._poll_once()
    mt5_stub._mutate_position_for_tests(2031, sl=1.07)
    mt5_stub._remove_position_for_tests(2030)
    mt5_stub._set_positions_for_tests([
        *mt5_stub._state["positions_get"], _pos(ticket=2032),
    ])
    m2 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m2._poll_once()
    events = await _read_events(fake_redis)
    types = {e["event_type"] for e in events}
    assert types == {"position_new", "position_closed_external", "position_modified"}


@pytest.mark.asyncio
async def test_offline_no_change_no_events(
    fake_redis: fakeredis.aioredis.FakeRedis, cmd_ledger: CmdLedger
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=2040, sl=1.08)])
    m1 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m1._poll_once()
    m2 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m2._poll_once()
    events = await _read_events(fake_redis)
    assert events == []


# ---------------------------------------------------------------------------
# Phase 4.3a — close_reason classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_reason_external_when_ledger_miss(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=3000)])
    await monitor._poll_once()
    mt5_stub._remove_position_for_tests(3000)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert closed[0]["close_reason"] == "external"


@pytest.mark.asyncio
async def test_close_reason_server_initiated_when_ledger_hit(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
    cmd_ledger: CmdLedger,
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=3010)])
    await monitor._poll_once()
    await cmd_ledger.mark_server_initiated(3010)
    mt5_stub._remove_position_for_tests(3010)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert closed[0]["close_reason"] == "server_initiated"


@pytest.mark.asyncio
async def test_close_reason_server_initiated_clears_ledger(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
    cmd_ledger: CmdLedger,
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=3020)])
    await monitor._poll_once()
    await cmd_ledger.mark_server_initiated(3020)
    mt5_stub._remove_position_for_tests(3020)
    await monitor._poll_once()
    assert await cmd_ledger.is_server_initiated(3020) is False


@pytest.mark.asyncio
async def test_close_reason_external_persists_when_history_fails(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=3030)])
    await monitor._poll_once()
    mt5_stub.set_state_for_tests(
        history_deals_get_raises=RuntimeError("query_fail")
    )
    mt5_stub._remove_position_for_tests(3030)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert closed[0]["close_reason"] == "external"
    assert closed[0]["enrichment_source"] == "snapshot_fallback"


@pytest.mark.asyncio
async def test_close_reason_server_initiated_persists_when_history_fails(
    monitor: PositionMonitor,
    fake_redis: fakeredis.aioredis.FakeRedis,
    cmd_ledger: CmdLedger,
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=3040)])
    await monitor._poll_once()
    await cmd_ledger.mark_server_initiated(3040)
    mt5_stub.set_state_for_tests(
        history_deals_get_raises=RuntimeError("query_fail")
    )
    mt5_stub._remove_position_for_tests(3040)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert closed[0]["close_reason"] == "server_initiated"
    assert closed[0]["enrichment_source"] == "snapshot_fallback"


# ---------------------------------------------------------------------------
# Phase 4.3a — history_deals_get enrichment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_event_enriched_with_close_price_profit(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=4000)])
    await monitor._poll_once()
    mt5_stub._set_history_deals_for_tests([
        _deal(position_id=4000, price=1.0830, profit=-3.20, commission=0.5, swap=0.1),
    ])
    mt5_stub._remove_position_for_tests(4000)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert closed[0]["close_price"] == "1.083"
    assert closed[0]["realized_profit"] == "-3.2"
    assert closed[0]["commission"] == "0.5"
    assert closed[0]["swap"] == "0.1"
    assert closed[0]["enrichment_source"] == "history_deals"


@pytest.mark.asyncio
async def test_close_event_fallback_when_history_empty(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=4010)])
    await monitor._poll_once()
    mt5_stub._remove_position_for_tests(4010)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert closed[0]["enrichment_source"] == "snapshot_fallback"
    assert "close_price" not in closed[0]


@pytest.mark.asyncio
async def test_close_event_fallback_when_no_deal_entry_out(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=4020)])
    await monitor._poll_once()
    mt5_stub._set_history_deals_for_tests([
        _deal(position_id=4020, entry=mt5_stub.DEAL_ENTRY_IN),
    ])
    mt5_stub._remove_position_for_tests(4020)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert closed[0]["enrichment_source"] == "snapshot_fallback"


@pytest.mark.asyncio
async def test_close_event_picks_deal_entry_out_among_multiple(
    monitor: PositionMonitor, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=4030)])
    await monitor._poll_once()
    mt5_stub._set_history_deals_for_tests([
        _deal(position_id=4030, entry=mt5_stub.DEAL_ENTRY_IN, price=1.085),
        _deal(position_id=4030, entry=mt5_stub.DEAL_ENTRY_OUT, price=1.083, profit=-2.0),
    ])
    mt5_stub._remove_position_for_tests(4030)
    await monitor._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert closed[0]["close_price"] == "1.083"


# ---------------------------------------------------------------------------
# Phase 4.3a — CEO scenario simulation (the Windows-smoke gap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ceo_scenario_modify_stop_close_restart_emits_external(
    fake_redis: fakeredis.aioredis.FakeRedis, cmd_ledger: CmdLedger
) -> None:
    """CEO Windows-smoke gap simulation:
      1. Boot 1 baseline empty.
      2. Open ticket 100 → poll → position_new + snapshot saved.
      3. Modify SL → poll → position_modified + snapshot saved.
      4. STOP monitor (downtime).
      5. Position 100 closed manually + history_deals records the close.
      6. RESTART monitor (fresh instance, same Redis + account_id).
      7. First poll loads snapshot → diffs against empty → emits
         position_closed_external with close_reason=external + history enrich.
    """
    mt5_stub._set_positions_for_tests([])
    m1 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m1._poll_once()
    mt5_stub._set_positions_for_tests([_pos(ticket=100, sl=1.08)])
    await m1._poll_once()
    mt5_stub._mutate_position_for_tests(100, sl=1.07)
    await m1._poll_once()
    pre_events = await _read_events(fake_redis)
    assert any(e["event_type"] == "position_new" for e in pre_events)
    assert any(e["event_type"] == "position_modified" for e in pre_events)
    await fake_redis.delete("event_stream:exness:exness_001")

    mt5_stub._remove_position_for_tests(100)
    mt5_stub._set_history_deals_for_tests([
        _deal(position_id=100, price=1.0825, profit=-2.50),
    ])

    m2 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m2._poll_once()
    post_events = await _read_events(fake_redis)
    closed = [e for e in post_events if e["event_type"] == "position_closed_external"]
    assert len(closed) == 1, f"expected 1 close event, got {post_events}"
    e = closed[0]
    assert e["broker_position_id"] == "100"
    assert e["close_reason"] == "external"
    assert e["enrichment_source"] == "history_deals"
    assert e["close_price"] == "1.0825"


@pytest.mark.asyncio
async def test_server_initiated_close_after_restart_uses_ledger_hit(
    fake_redis: fakeredis.aioredis.FakeRedis, cmd_ledger: CmdLedger
) -> None:
    mt5_stub._set_positions_for_tests([_pos(ticket=200)])
    m1 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m1._poll_once()
    await cmd_ledger.mark_server_initiated(200)
    await fake_redis.delete("event_stream:exness:exness_001")
    mt5_stub._remove_position_for_tests(200)
    m2 = PositionMonitor(fake_redis, "exness_001", mt5_stub, cmd_ledger)
    await m2._poll_once()
    events = await _read_events(fake_redis)
    closed = [e for e in events if e["event_type"] == "position_closed_external"]
    assert closed[0]["close_reason"] == "server_initiated"
    assert await cmd_ledger.is_server_initiated(200) is False
