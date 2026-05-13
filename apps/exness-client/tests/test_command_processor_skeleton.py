"""CommandProcessor skeleton tests — log + XACK, no real handlers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import fakeredis.aioredis
import pytest
import pytest_asyncio
from redis.exceptions import RedisError

from exness_client import command_processor as cp_module
from exness_client import mt5_stub
from exness_client.bridge_service import MT5BridgeService
from exness_client.command_processor import CommandProcessor
from exness_client.config import ExnessClientSettings


def test_consumer_group_naming_matches_server_convention() -> None:
    """Pin the stream + group + consumer name format. The server's
    ``setup_consumer_groups`` (step 4.4) MUST create the same name —
    drift between the two would silently break delivery, same as the
    FTMO contract test."""
    assert cp_module._stream_name("exness_acc_001") == "cmd_stream:exness:exness_acc_001"
    assert cp_module._group_name("exness_acc_001") == "exness-exness_acc_001"
    assert cp_module._consumer_name("exness_acc_001") == "exness-exness_acc_001"


@pytest.mark.asyncio
async def test_ensure_consumer_group_creates_and_is_idempotent(
    fake_redis: fakeredis.aioredis.FakeRedis, settings: ExnessClientSettings
) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    proc = CommandProcessor(fake_redis, bridge, "exness_acc_001")
    await proc.ensure_consumer_group()
    # Second call must not raise (BUSYGROUP swallowed).
    await proc.ensure_consumer_group()
    info = await fake_redis.xinfo_groups("cmd_stream:exness:exness_acc_001")  # type: ignore[misc]
    assert any(g["name"] == "exness-exness_acc_001" for g in info)


@pytest_asyncio.fixture
async def consumer_ready(
    fake_redis: fakeredis.aioredis.FakeRedis, settings: ExnessClientSettings
) -> None:
    await fake_redis.xgroup_create(
        "cmd_stream:exness:exness_acc_001",
        "exness-exness_acc_001",
        id="0",
        mkstream=True,
    )


@pytest.mark.asyncio
async def test_run_reads_entry_logs_warn_xacks(
    fake_redis: fakeredis.aioredis.FakeRedis,
    consumer_ready: None,
    settings: ExnessClientSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    proc = CommandProcessor(fake_redis, bridge, "exness_acc_001", block_ms=50)

    await fake_redis.xadd(
        "cmd_stream:exness:exness_acc_001",
        {
            "request_id": "req_001",
            "order_id": "ord_001",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume_lots": "0.01",
            "order_type": "market",
        },
    )

    caplog.set_level(logging.WARNING, logger="exness_client.command_processor")
    task = asyncio.create_task(proc.run())
    # Give the loop enough cycles to drain + ACK.
    for _ in range(40):
        pending = await fake_redis.xpending(  # type: ignore[misc]
            "cmd_stream:exness:exness_acc_001",
            "exness-exness_acc_001",
        )
        if pending["pending"] == 0 and any(
            "action_not_implemented_phase_4_1" in r.message for r in caplog.records
        ):
            break
        await asyncio.sleep(0.05)
    await proc.stop()
    await asyncio.wait_for(task, timeout=2.0)

    # Action was logged as not-implemented.
    assert any(
        "action_not_implemented_phase_4_1" in r.message and "action='open'" in r.message
        for r in caplog.records
    )
    # And the message was XACKed (no pending).
    pending = await fake_redis.xpending(  # type: ignore[misc]
        "cmd_stream:exness:exness_acc_001", "exness-exness_acc_001"
    )
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_run_exits_cleanly_on_stop(
    fake_redis: fakeredis.aioredis.FakeRedis,
    consumer_ready: None,
    settings: ExnessClientSettings,
) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    proc = CommandProcessor(fake_redis, bridge, "exness_acc_001", block_ms=50)
    task = asyncio.create_task(proc.run())
    await asyncio.sleep(0.1)
    await proc.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


@pytest.mark.asyncio
async def test_run_recovers_from_redis_error(
    settings: ExnessClientSettings,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient RedisError on XREADGROUP gets logged + backed off, not crash."""
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)

    call_count = {"n": 0}

    class FakeRedis:
        async def xreadgroup(self, **_kwargs: Any) -> list[Any]:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RedisError("simulated flap")
            # Yield to let proc.stop() flip the running flag.
            await asyncio.sleep(0.005)
            return []

        async def xack(self, *_args: Any, **_kwargs: Any) -> int:
            return 1

    # Tighter backoff so the test runs fast.
    monkeypatch.setattr(cp_module, "ERROR_BACKOFF_SECONDS", 0.01)
    caplog.set_level(logging.WARNING, logger="exness_client.command_processor")

    proc = CommandProcessor(FakeRedis(), bridge, "exness_acc_001", block_ms=10)  # type: ignore[arg-type]
    task = asyncio.create_task(proc.run())
    await asyncio.sleep(0.1)
    await proc.stop()
    await asyncio.wait_for(task, timeout=1.0)

    assert any("xreadgroup failed" in r.message for r in caplog.records)
    assert call_count["n"] >= 2
