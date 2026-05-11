"""Lifespan-wiring tests for step 3.7 handlers.

Verifies that ``app/main.py`` starts one response_handler + one
event_handler task per registered FTMO account, handles an empty
account set gracefully, and cancels both task groups on shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from app.main import app, lifespan
from app.services.redis_service import RedisService


@pytest.mark.asyncio
async def test_lifespan_starts_handler_tasks_per_account(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With two FTMO accounts registered, the lifespan startup must
    schedule two response_handler tasks + two event_handler tasks."""
    # Wire fakeredis through the app's init_redis stub.
    from app import redis_client as redis_client_module  # noqa: PLC0415

    async def _stub_init_redis(_url: str) -> None:
        # No-op — the redis_client global stays whatever we mock below.
        pass

    monkeypatch.setattr(redis_client_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(redis_client_module, "get_redis", lambda: fake_redis)
    # close_redis would call .close() on the real client; bypass.
    monkeypatch.setattr(redis_client_module, "close_redis", lambda: _noop_async())

    # Mirror to the module-level imports inside app.main.
    from app import main as main_module  # noqa: PLC0415

    monkeypatch.setattr(main_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(main_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(main_module, "close_redis", _noop_async)

    # Seed two accounts.
    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", "ftmo_001", name="t1")
    await svc.add_account("ftmo", "ftmo_002", name="t2")

    captured: list[str] = []

    async def fake_response_loop(_svc: Any, _bcast: Any, account_id: str) -> None:
        captured.append(f"resp:{account_id}")
        await asyncio.Event().wait()  # block until cancelled

    async def fake_event_loop(_svc: Any, _bcast: Any, account_id: str) -> None:
        captured.append(f"event:{account_id}")
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "response_handler_loop", fake_response_loop)
    monkeypatch.setattr(main_module, "event_handler_loop", fake_event_loop)

    async with lifespan(app):
        # Give the spawned tasks one tick to enter their coroutines.
        for _ in range(20):
            if len(captured) >= 4:
                break
            await asyncio.sleep(0.01)

    # Two of each, one per account.
    assert sorted(captured) == [
        "event:ftmo_001",
        "event:ftmo_002",
        "resp:ftmo_001",
        "resp:ftmo_002",
    ]


@pytest.mark.asyncio
async def test_lifespan_handles_empty_account_list_gracefully(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero registered accounts → zero handler tasks; lifespan still
    starts and shuts down cleanly."""
    from app import main as main_module  # noqa: PLC0415
    from app import redis_client as redis_client_module  # noqa: PLC0415

    async def _stub_init_redis(_url: str) -> None:
        pass

    monkeypatch.setattr(redis_client_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(redis_client_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(redis_client_module, "close_redis", _noop_async)
    monkeypatch.setattr(main_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(main_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(main_module, "close_redis", _noop_async)

    started = 0

    async def fake_loop(*_args: Any) -> None:
        nonlocal started
        started += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "response_handler_loop", fake_loop)
    monkeypatch.setattr(main_module, "event_handler_loop", fake_loop)

    async with lifespan(app):
        pass

    assert started == 0
    # State attributes still set (empty lists).
    assert app.state.response_tasks == []
    assert app.state.event_tasks == []


@pytest.mark.asyncio
async def test_lifespan_cancels_tasks_on_shutdown(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tasks created in startup must be cancelled + awaited during
    shutdown so the asyncio runtime exits cleanly."""
    from app import main as main_module  # noqa: PLC0415
    from app import redis_client as redis_client_module  # noqa: PLC0415

    async def _stub_init_redis(_url: str) -> None:
        pass

    monkeypatch.setattr(redis_client_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(redis_client_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(redis_client_module, "close_redis", _noop_async)
    monkeypatch.setattr(main_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(main_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(main_module, "close_redis", _noop_async)

    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", "ftmo_001", name="t")

    cancelled = 0
    started = 0

    async def fake_loop_track(*_args: Any) -> None:
        nonlocal started
        started += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            nonlocal cancelled
            cancelled += 1
            raise

    monkeypatch.setattr(main_module, "response_handler_loop", fake_loop_track)
    monkeypatch.setattr(main_module, "event_handler_loop", fake_loop_track)

    async with lifespan(app):
        # Give the spawned tasks a moment to enter the coroutine
        # body so cancellation actually interrupts a real await.
        for _ in range(20):
            if started >= 2:
                break
            await asyncio.sleep(0.01)

    # Both tasks (response + event for ftmo_001) saw CancelledError.
    assert started == 2
    assert cancelled == 2


@pytest.mark.asyncio
async def test_lifespan_task_names_include_account_id(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task names matter for log readability + asyncio debug output.
    Pin the naming convention."""
    from app import main as main_module  # noqa: PLC0415
    from app import redis_client as redis_client_module  # noqa: PLC0415

    async def _stub_init_redis(_url: str) -> None:
        pass

    monkeypatch.setattr(redis_client_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(redis_client_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(redis_client_module, "close_redis", _noop_async)
    monkeypatch.setattr(main_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(main_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(main_module, "close_redis", _noop_async)

    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", "ftmo_001", name="t")

    async def fake_loop(*_args: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "response_handler_loop", fake_loop)
    monkeypatch.setattr(main_module, "event_handler_loop", fake_loop)

    async with lifespan(app):
        names = {t.get_name() for t in app.state.response_tasks}
        names |= {t.get_name() for t in app.state.event_tasks}
        assert "response_handler_ftmo_001" in names
        assert "event_handler_ftmo_001" in names


async def _noop_async() -> None:
    return None
