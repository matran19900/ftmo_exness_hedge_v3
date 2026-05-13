"""HeartbeatLoop tests."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import fakeredis.aioredis
import pytest
from redis.exceptions import RedisError

from exness_client import __version__, mt5_stub
from exness_client.bridge_service import MT5BridgeService
from exness_client.config import ExnessClientSettings
from exness_client.heartbeat import HEARTBEAT_TTL_SECONDS, HeartbeatLoop


@pytest.mark.asyncio
async def test_publish_once_writes_expected_fields_when_connected(
    fake_redis: fakeredis.aioredis.FakeRedis, settings: ExnessClientSettings
) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    await bridge.connect()
    hb = HeartbeatLoop(fake_redis, bridge, "exness_acc_001", interval_s=0.05)
    await hb.publish_once()

    raw = await fake_redis.hgetall("client:exness:exness_acc_001")  # type: ignore[misc]
    assert raw["status"] == "online"
    assert int(raw["last_heartbeat"]) > 0
    assert raw["broker"] == "exness"
    assert raw["account_id"] == "exness_acc_001"
    assert raw["terminal_ok"] == "true"
    assert raw["trade_allowed"] == "true"
    assert raw["version"] == __version__

    ttl = await fake_redis.ttl("client:exness:exness_acc_001")
    assert 0 < ttl <= HEARTBEAT_TTL_SECONDS


@pytest.mark.asyncio
async def test_publish_once_status_offline_when_bridge_disconnected(
    fake_redis: fakeredis.aioredis.FakeRedis, settings: ExnessClientSettings
) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    # Never connect — bridge.is_connected() False.
    hb = HeartbeatLoop(fake_redis, bridge, "exness_acc_001", interval_s=0.05)
    await hb.publish_once()
    raw = await fake_redis.hgetall("client:exness:exness_acc_001")  # type: ignore[misc]
    assert raw["status"] == "offline"
    assert raw["terminal_ok"] == "false"
    assert raw["trade_allowed"] == "false"


@pytest.mark.asyncio
async def test_publish_once_status_offline_when_terminal_disconnected(
    fake_redis: fakeredis.aioredis.FakeRedis, settings: ExnessClientSettings
) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    await bridge.connect()
    # MT5 lib still connected but terminal reports offline.
    mt5_stub.set_state_for_tests(
        terminal_info=mt5_stub.TerminalInfo(
            connected=False, trade_allowed=False, name="stub"
        ),
    )
    hb = HeartbeatLoop(fake_redis, bridge, "exness_acc_001", interval_s=0.05)
    await hb.publish_once()
    raw = await fake_redis.hgetall("client:exness:exness_acc_001")  # type: ignore[misc]
    assert raw["status"] == "offline"


@pytest.mark.asyncio
async def test_loop_writes_at_least_one_beat_then_exits_on_stop(
    fake_redis: fakeredis.aioredis.FakeRedis, settings: ExnessClientSettings
) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    await bridge.connect()
    hb = HeartbeatLoop(fake_redis, bridge, "exness_acc_001", interval_s=0.05)
    task = asyncio.create_task(hb.run())
    for _ in range(40):
        if await fake_redis.exists("client:exness:exness_acc_001"):
            break
        await asyncio.sleep(0.02)
    assert await fake_redis.exists("client:exness:exness_acc_001")
    await hb.stop()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_loop_swallows_redis_error_and_continues(
    settings: ExnessClientSettings, caplog: pytest.LogCaptureFixture
) -> None:
    """A transient Redis flap during HSET must NOT crash the loop."""

    class ExplodingRedis:
        calls = 0

        async def hset(self, *_args: Any, **_kwargs: Any) -> int:
            type(self).calls += 1
            raise RedisError("simulated flap")

        async def expire(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    await bridge.connect()
    caplog.set_level(logging.WARNING, logger="exness_client.heartbeat")
    hb = HeartbeatLoop(ExplodingRedis(), bridge, "exness_acc_001", interval_s=0.01)  # type: ignore[arg-type]
    task = asyncio.create_task(hb.run())
    await asyncio.sleep(0.1)
    await hb.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert any("heartbeat write failed" in r.message for r in caplog.records)
    assert ExplodingRedis.calls >= 1
