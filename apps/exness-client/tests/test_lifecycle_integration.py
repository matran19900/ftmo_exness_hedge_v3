"""Integration tests covering the Phase 4.2 wire-up:

  - ``main.amain`` triggers the initial ``SymbolSyncPublisher.publish_snapshot``
    after a successful bridge connect.
  - A symbol-sync failure does NOT block the bridge — main proceeds to
    spawn cmd_processor + heartbeat tasks anyway.
  - ``CommandProcessor`` with an ``ActionHandler`` round-trips a single
    cmd through to a resp_stream entry.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from pydantic import SecretStr

from exness_client import main as main_mod
from exness_client import mt5_stub
from exness_client.account_info import AccountInfoPublisher
from exness_client.action_handlers import ActionHandler
from exness_client.cmd_ledger import CmdLedger
from exness_client.command_processor import CommandProcessor
from exness_client.config import ExnessClientSettings
from exness_client.symbol_sync import SymbolSyncPublisher


def _stub_symbol(name: str = "EURUSDm") -> mt5_stub.SymbolInfo:
    return mt5_stub.SymbolInfo(
        name=name,
        trade_contract_size=100000.0,
        digits=5,
        point=0.00001,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=200.0,
        currency_profit="USD",
        trade_mode=mt5_stub.SYMBOL_TRADE_MODE_FULL,
        bid=1.0850,
        ask=1.0852,
    )


@pytest.fixture
def amain_settings() -> ExnessClientSettings:
    return ExnessClientSettings(
        account_id="exness_acc_001",
        redis_url="redis://localhost:6379/0",
        mt5_login=12345678,
        mt5_password=SecretStr("test"),
        mt5_server="Exness-Stub",
        mt5_path=None,
        heartbeat_interval_s=0.05,
        cmd_stream_block_ms=50,
        log_level="INFO",
    )


@pytest.mark.asyncio
async def test_amain_initial_symbol_sync_publishes_to_redis(
    amain_settings: ExnessClientSettings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean bridge connect publishes the snapshot before we exit."""
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbols_get=(sym,), symbol_info={"EURUSDm": sym})

    async def _fake_connect_redis(_url: str) -> Any:
        return fake_redis

    monkeypatch.setattr(main_mod, "_connect_redis", _fake_connect_redis)

    # Drive amain just long enough for the initial sync to run, then trip
    # the shutdown coordinator so we exit cleanly.
    async def _shutdown_after(coord: Any) -> None:
        await asyncio.sleep(0.05)
        coord._shutdown_event.set()

    real_install = main_mod.ShutdownCoordinator.install_signal_handlers

    def _no_signal(self: Any) -> None:
        # Don't try to wire OS signal handlers in the test event loop
        # (fails under pytest-asyncio); we trigger shutdown manually.
        return None

    monkeypatch.setattr(
        main_mod.ShutdownCoordinator,
        "install_signal_handlers",
        _no_signal,
    )

    async def _drive() -> int:
        task = asyncio.create_task(
            main_mod.amain(amain_settings, mt5_module=mt5_stub),
            name="amain",
        )
        # Give amain time to reach the wait_for_shutdown await.
        await asyncio.sleep(0.05)
        # Find the running coordinator via a tiny side channel:
        # main has already published the snapshot by now.
        published = await fake_redis.get(
            "exness_raw_symbols:exness_acc_001"
        )
        assert published is not None, "snapshot not published"
        # Trigger amain shutdown by cancelling the task — the
        # ShutdownCoordinator's wait_for_shutdown is a passive wait,
        # cancellation propagates through the finally block.
        task.cancel()
        try:
            return await task
        except asyncio.CancelledError:
            return -1

    rc = await _drive()
    # Either clean exit code or test-cancellation; both satisfy the
    # "snapshot was published" contract checked above.
    assert rc in (0, -1)
    # Avoid leaking the monkeypatch
    main_mod.ShutdownCoordinator.install_signal_handlers = real_install  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_amain_symbol_sync_failure_does_not_block_bridge(
    amain_settings: ExnessClientSettings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A snapshot exception logs but doesn't prevent ``amain`` from
    starting the heartbeat + cmd_processor tasks."""

    async def _fake_connect_redis(_url: str) -> Any:
        return fake_redis

    monkeypatch.setattr(main_mod, "_connect_redis", _fake_connect_redis)
    monkeypatch.setattr(
        main_mod.ShutdownCoordinator,
        "install_signal_handlers",
        lambda self: None,
    )

    # Make publish_snapshot raise.
    async def _boom(self: Any) -> int:
        raise RuntimeError("simulated_snapshot_failure")

    monkeypatch.setattr(
        SymbolSyncPublisher, "publish_snapshot", _boom
    )

    task = asyncio.create_task(
        main_mod.amain(amain_settings, mt5_module=mt5_stub),
        name="amain",
    )
    with caplog.at_level("ERROR"):
        await asyncio.sleep(0.10)

    # We should have seen the failure log AND the heartbeat key, proving
    # we passed the symbol-sync block and continued lifecycle setup.
    assert any(
        "initial_symbol_sync_failed_continuing" in r.message
        or "symbol_sync.symbols_get_failed" in r.message
        for r in caplog.records
    )
    hb = await fake_redis.hgetall("client:exness:exness_acc_001")
    assert hb, "heartbeat HASH not written"
    assert "last_heartbeat" in hb
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_command_processor_dispatches_to_action_handler(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """A cmd XADDed to the stream lands on ``ActionHandler.dispatch``
    and produces a resp_stream entry."""
    sync = SymbolSyncPublisher(fake_redis, "exness_acc_001", mt5_stub)
    ledger = CmdLedger(fake_redis, "exness_acc_001")
    handler = ActionHandler(
        fake_redis, "exness_acc_001", mt5_stub, sync, ledger
    )
    bridge = AsyncMock()
    proc = CommandProcessor(
        fake_redis,
        bridge,
        "exness_acc_001",
        action_handler=handler,
        block_ms=50,
    )
    await proc.ensure_consumer_group()

    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})

    await fake_redis.xadd(
        "cmd_stream:exness:exness_acc_001",
        {
            "request_id": "rINT",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        },
    )

    task = asyncio.create_task(proc.run(), name="cmd_proc")
    # Wait for the loop to round-trip.
    for _ in range(20):
        entries = await fake_redis.xrevrange(
            "resp_stream:exness:exness_acc_001", count=1
        )
        if entries:
            break
        await asyncio.sleep(0.025)
    await proc.stop()
    try:
        await asyncio.wait_for(task, timeout=1)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()

    entries = await fake_redis.xrevrange(
        "resp_stream:exness:exness_acc_001", count=1
    )
    assert entries
    _msg_id, fields = entries[0]
    payload = dict(fields)
    assert payload["request_id"] == "rINT"
    assert payload["status"] == "filled"


# ---------------------------------------------------------------------------
# Phase 4.4 — account_info_task lifecycle integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_amain_account_info_task_runs(
    amain_settings: ExnessClientSettings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main.amain`` spawns the account_info task and the HASH appears
    in Redis within a short window (publisher fires immediately on start)."""

    async def _fake_connect_redis(_url: str) -> Any:
        return fake_redis

    monkeypatch.setattr(main_mod, "_connect_redis", _fake_connect_redis)
    monkeypatch.setattr(
        main_mod.ShutdownCoordinator,
        "install_signal_handlers",
        lambda self: None,
    )

    task = asyncio.create_task(
        main_mod.amain(amain_settings, mt5_module=mt5_stub),
        name="amain",
    )
    for _ in range(50):
        if await fake_redis.exists(
            f"account:exness:{amain_settings.account_id}"
        ):
            break
        await asyncio.sleep(0.01)
    payload = await fake_redis.hgetall(
        f"account:exness:{amain_settings.account_id}"
    )
    assert payload
    assert payload["broker"] == "exness"
    assert payload["account_id"] == amain_settings.account_id
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_amain_shutdown_includes_account_info_publisher(
    amain_settings: ExnessClientSettings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graceful shutdown path stops the account_info publisher (no
    leaked task on exit)."""

    async def _fake_connect_redis(_url: str) -> Any:
        return fake_redis

    monkeypatch.setattr(main_mod, "_connect_redis", _fake_connect_redis)
    monkeypatch.setattr(
        main_mod.ShutdownCoordinator,
        "install_signal_handlers",
        lambda self: None,
    )

    task = asyncio.create_task(
        main_mod.amain(amain_settings, mt5_module=mt5_stub),
        name="amain",
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    pending = [
        t for t in asyncio.all_tasks() if t.get_name() == "account_info"
    ]
    assert not pending


@pytest.mark.asyncio
async def test_amain_account_info_failure_does_not_block_bridge(
    amain_settings: ExnessClientSettings,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside the account_info loop must not take down the
    bridge — heartbeat still publishes."""

    async def _fake_connect_redis(_url: str) -> Any:
        return fake_redis

    monkeypatch.setattr(main_mod, "_connect_redis", _fake_connect_redis)
    monkeypatch.setattr(
        main_mod.ShutdownCoordinator,
        "install_signal_handlers",
        lambda self: None,
    )

    async def _boom(self: Any) -> None:
        raise RuntimeError("simulated_account_info_publish_failure")

    monkeypatch.setattr(AccountInfoPublisher, "_publish_once", _boom)

    task = asyncio.create_task(
        main_mod.amain(amain_settings, mt5_module=mt5_stub),
        name="amain",
    )
    await asyncio.sleep(0.10)
    hb = await fake_redis.hgetall(
        f"client:exness:{amain_settings.account_id}"
    )
    assert hb and "last_heartbeat" in hb
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
