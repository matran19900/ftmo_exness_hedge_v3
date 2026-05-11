"""command_loop XREADGROUP tests."""

from __future__ import annotations

import asyncio
import logging

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ftmo_client import action_handlers
from ftmo_client import command_loop as cmdmod
from ftmo_client.command_loop import command_loop
from ftmo_client.shutdown import ShutdownController


@pytest_asyncio.fixture
async def consumer_ready(fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    """Create the consumer group the loop expects (matches the server's
    ``RedisService.setup_consumer_groups()`` shape: ``ftmo-{acc}``).
    """
    await fake_redis.xgroup_create(
        "cmd_stream:ftmo:ftmo_001", "ftmo-ftmo_001", id="0", mkstream=True
    )


def test_command_loop_consumer_group_matches_server() -> None:
    """The loop's group name format MUST match what
    ``server/app/services/redis_service.py`` `setup_consumer_groups`
    creates. Drift between the two would silently break delivery.

    Reference: ``redis_service.py:315`` —
    ``await self._create_group(f"cmd_stream:ftmo:{acc}", f"ftmo-{acc}")``
    """
    assert cmdmod._stream_name("ftmo_001") == "cmd_stream:ftmo:ftmo_001"
    assert cmdmod._group_name("ftmo_001") == "ftmo-ftmo_001"


@pytest.mark.asyncio
async def test_command_loop_dispatches_open_to_handler(
    fake_redis: fakeredis.aioredis.FakeRedis,
    consumer_ready: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, str]] = []

    async def fake_open(_redis: object, account_id: str, fields: dict[str, str]) -> None:
        seen.append({"account_id": account_id, **fields})

    monkeypatch.setitem(action_handlers.ACTION_HANDLERS, "open", fake_open)

    await fake_redis.xadd(
        "cmd_stream:ftmo:ftmo_001",
        {
            "request_id": "req_001",
            "order_id": "ord_001",
            "action": "open",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "order_type": "market",
        },
    )

    shutdown = ShutdownController()
    task = asyncio.create_task(command_loop(fake_redis, "ftmo_001", shutdown))
    # Poll until the dispatch fires; bound the test runtime so a hang surfaces.
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.05)
    shutdown.request_shutdown()
    await asyncio.wait_for(task, timeout=6.0)

    assert len(seen) == 1
    assert seen[0]["order_id"] == "ord_001"
    assert seen[0]["account_id"] == "ftmo_001"


@pytest.mark.asyncio
async def test_command_loop_xacks_after_handler_returns(
    fake_redis: fakeredis.aioredis.FakeRedis,
    consumer_ready: None,
) -> None:
    await fake_redis.xadd(
        "cmd_stream:ftmo:ftmo_001",
        {
            "request_id": "req_002",
            "order_id": "ord_002",
            "action": "open",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "order_type": "market",
        },
    )

    shutdown = ShutdownController()
    task = asyncio.create_task(command_loop(fake_redis, "ftmo_001", shutdown))
    # Drain interval is 5s; poll the pending count to detect the ack.
    for _ in range(50):
        info = await fake_redis.xpending("cmd_stream:ftmo:ftmo_001", "ftmo-ftmo_001")
        if info["pending"] == 0:
            break
        await asyncio.sleep(0.05)
    shutdown.request_shutdown()
    await asyncio.wait_for(task, timeout=6.0)

    info = await fake_redis.xpending("cmd_stream:ftmo:ftmo_001", "ftmo-ftmo_001")
    assert info["pending"] == 0


@pytest.mark.asyncio
async def test_command_loop_handles_unknown_action_with_ack(
    fake_redis: fakeredis.aioredis.FakeRedis,
    consumer_ready: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await fake_redis.xadd(
        "cmd_stream:ftmo:ftmo_001",
        {
            "request_id": "req_003",
            "action": "wat_is_this",
        },
    )

    shutdown = ShutdownController()
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(command_loop(fake_redis, "ftmo_001", shutdown))
        for _ in range(50):
            info = await fake_redis.xpending("cmd_stream:ftmo:ftmo_001", "ftmo-ftmo_001")
            if info["pending"] == 0:
                break
            await asyncio.sleep(0.05)
        shutdown.request_shutdown()
        await asyncio.wait_for(task, timeout=6.0)

    # Pending zero + warning logged.
    info = await fake_redis.xpending("cmd_stream:ftmo:ftmo_001", "ftmo-ftmo_001")
    assert info["pending"] == 0
    assert any("unknown action" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_command_loop_xacks_even_when_handler_raises(
    fake_redis: fakeredis.aioredis.FakeRedis,
    consumer_ready: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler exception is logged + the message is still XACKed (step
    3.3 contract — step 3.4 will introduce a no-ack retry path)."""

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated bug")

    monkeypatch.setitem(action_handlers.ACTION_HANDLERS, "open", boom)

    await fake_redis.xadd(
        "cmd_stream:ftmo:ftmo_001",
        {
            "request_id": "req_004",
            "order_id": "ord_004",
            "action": "open",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "order_type": "market",
        },
    )

    shutdown = ShutdownController()
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(command_loop(fake_redis, "ftmo_001", shutdown))
        for _ in range(50):
            info = await fake_redis.xpending("cmd_stream:ftmo:ftmo_001", "ftmo-ftmo_001")
            if info["pending"] == 0:
                break
            await asyncio.sleep(0.05)
        shutdown.request_shutdown()
        await asyncio.wait_for(task, timeout=6.0)

    info = await fake_redis.xpending("cmd_stream:ftmo:ftmo_001", "ftmo-ftmo_001")
    assert info["pending"] == 0
    assert any("handler 'open' raised" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_command_loop_respects_shutdown_immediately(
    fake_redis: fakeredis.aioredis.FakeRedis,
    consumer_ready: None,
) -> None:
    """If shutdown is set before the loop starts iterating, exit cleanly."""
    shutdown = ShutdownController()
    shutdown.request_shutdown()
    await asyncio.wait_for(command_loop(fake_redis, "ftmo_001", shutdown), timeout=2.0)


@pytest.mark.asyncio
async def test_command_loop_swallows_redis_error_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """xreadgroup raising → log warning, sleep, keep going (no crash)."""
    from redis.exceptions import RedisError as RedisErrorImport  # noqa: PLC0415

    call_count = {"n": 0}

    class ErroringRedis:
        async def xreadgroup(self, *_args: object, **_kwargs: object) -> None:
            call_count["n"] += 1
            if call_count["n"] >= 3:
                # Stop after a few attempts so the test bounds.
                raise asyncio.CancelledError
            raise RedisErrorImport("simulated unavailable")

        async def xack(self, *_args: object, **_kwargs: object) -> int:
            return 0

    # Patch the backoff so the test doesn't sleep a real second.
    monkeypatch.setattr(cmdmod, "ERROR_BACKOFF_SECONDS", 0.01)

    shutdown = ShutdownController()
    with pytest.raises(asyncio.CancelledError):
        await command_loop(ErroringRedis(), "ftmo_001", shutdown)  # type: ignore[arg-type]
    assert call_count["n"] >= 2  # retried at least once after the first error
