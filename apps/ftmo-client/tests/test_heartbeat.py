"""Heartbeat publisher tests."""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from redis.exceptions import RedisError

from ftmo_client import __version__
from ftmo_client.heartbeat import (
    HEARTBEAT_TTL_SECONDS,
    heartbeat_loop,
    publish_once,
)
from ftmo_client.shutdown import ShutdownController


@pytest.mark.asyncio
async def test_publish_once_writes_expected_fields(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await publish_once(fake_redis, "ftmo_001")

    raw = await fake_redis.hgetall("client:ftmo:ftmo_001")  # type: ignore[misc]
    assert raw["status"] == "online"
    assert int(raw["last_seen"]) > 0
    assert raw["version"] == __version__

    ttl = await fake_redis.ttl("client:ftmo:ftmo_001")
    # docs/06-data-models.md §11: TTL 30s.
    assert 0 < ttl <= HEARTBEAT_TTL_SECONDS


@pytest.mark.asyncio
async def test_heartbeat_loop_writes_at_least_one_beat_before_shutdown(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Loop run, write first beat, then shutdown is signaled mid-wait → exits."""
    shutdown = ShutdownController()
    task = asyncio.create_task(heartbeat_loop(fake_redis, "ftmo_001", shutdown))
    # Wait for the first beat to land before requesting shutdown.
    for _ in range(40):
        if await fake_redis.exists("client:ftmo:ftmo_001"):
            break
        await asyncio.sleep(0.05)
    shutdown.request_shutdown()
    await asyncio.wait_for(task, timeout=2.0)
    raw = await fake_redis.hgetall("client:ftmo:ftmo_001")  # type: ignore[misc]
    assert raw["status"] == "online"


@pytest.mark.asyncio
async def test_heartbeat_loop_shutdown_before_start_exits_without_writing(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Shutdown set before loop entry → ``while`` gate skips even the first beat.

    main.amain handles this race by calling ``publish_once`` itself before
    starting the loop; this test pins the loop's contract: it does NOT
    promise a first beat regardless of shutdown state.
    """
    shutdown = ShutdownController()
    shutdown.request_shutdown()
    await heartbeat_loop(fake_redis, "ftmo_001", shutdown)
    assert not await fake_redis.exists("client:ftmo:ftmo_001")


@pytest.mark.asyncio
async def test_heartbeat_loop_swallows_redis_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis flap → loop logs the warning but does not crash."""

    class ExplodingRedis:
        async def hset(self, *_args: Any, **_kwargs: Any) -> None:
            raise RedisError("simulated flap")

        async def expire(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

    shutdown = ShutdownController()
    shutdown.request_shutdown()

    # The loop should run one iteration (which raises RedisError, gets
    # swallowed) and then exit because shutdown is requested.
    await heartbeat_loop(ExplodingRedis(), "ftmo_001", shutdown)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_heartbeat_loop_responds_to_shutdown_mid_wait(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Shutdown set while ``wait_for(shutdown.wait(), timeout=10)`` is
    awaiting → the wait returns and the loop exits within the same tick.
    """
    shutdown = ShutdownController()
    task = asyncio.create_task(heartbeat_loop(fake_redis, "ftmo_001", shutdown))
    # Give the loop a moment to write the first beat and enter wait_for.
    await asyncio.sleep(0.05)
    shutdown.request_shutdown()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
