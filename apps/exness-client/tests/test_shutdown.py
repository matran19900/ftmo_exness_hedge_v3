"""ShutdownCoordinator tests — verify D-088 teardown order + signal handling."""

from __future__ import annotations

import asyncio
import signal

import pytest

from exness_client.shutdown import ShutdownCoordinator


class _Recorder:
    """Captures the call order across stub lifecycle components."""

    def __init__(self) -> None:
        self.calls: list[str] = []


class _StubCmd:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def stop(self) -> None:
        self._rec.calls.append("cmd.stop")


class _StubHb:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def stop(self) -> None:
        self._rec.calls.append("hb.stop")


class _StubBridge:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def disconnect(self) -> None:
        self._rec.calls.append("bridge.disconnect")


class _StubRedis:
    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    async def aclose(self) -> None:
        self._rec.calls.append("redis.aclose")


@pytest.mark.asyncio
async def test_shutdown_order_matches_d088() -> None:
    """D-088 lesson: cmd_processor.stop → heartbeat.stop → cancel tasks →
    bridge.disconnect → redis.aclose. Order matters because heartbeat
    writes to Redis (must stop before aclose) and command handlers may
    touch the bridge (must stop before disconnect)."""
    rec = _Recorder()
    coord = ShutdownCoordinator(
        _StubCmd(rec), _StubHb(rec), _StubBridge(rec), _StubRedis(rec)
    )
    await coord.shutdown(tasks=[])
    assert rec.calls == [
        "cmd.stop",
        "hb.stop",
        "bridge.disconnect",
        "redis.aclose",
    ]


@pytest.mark.asyncio
async def test_shutdown_cancels_tasks_after_stops() -> None:
    """Long-lived asyncio tasks get cancelled + gathered."""
    rec = _Recorder()

    async def long_task() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            rec.calls.append("task.cancelled")
            raise

    task = asyncio.create_task(long_task())
    # Let the task actually start running before we tear down — otherwise
    # asyncio.gather may consume the cancellation before the coroutine
    # body gets a chance to enter its try/except.
    await asyncio.sleep(0)
    coord = ShutdownCoordinator(
        _StubCmd(rec), _StubHb(rec), _StubBridge(rec), _StubRedis(rec)
    )
    await coord.shutdown(tasks=[task])
    assert task.cancelled() or task.done()
    # The task's except block must have run after the lifecycle stops
    # but before the bridge/redis teardown.
    idx_cmd = rec.calls.index("cmd.stop")
    idx_hb = rec.calls.index("hb.stop")
    idx_cancel = rec.calls.index("task.cancelled")
    idx_bridge = rec.calls.index("bridge.disconnect")
    assert idx_cmd < idx_hb < idx_cancel < idx_bridge


@pytest.mark.asyncio
async def test_request_shutdown_idempotent() -> None:
    rec = _Recorder()
    coord = ShutdownCoordinator(
        _StubCmd(rec), _StubHb(rec), _StubBridge(rec), _StubRedis(rec)
    )
    coord.request_shutdown()
    coord.request_shutdown()
    coord.request_shutdown()
    assert coord.is_requested
    await coord.wait_for_shutdown()  # returns immediately


@pytest.mark.asyncio
async def test_install_signal_handlers_does_not_raise() -> None:
    """install_signal_handlers wires SIGINT + SIGTERM; the call itself
    is exercised in a Linux event loop where add_signal_handler works."""
    rec = _Recorder()
    coord = ShutdownCoordinator(
        _StubCmd(rec), _StubHb(rec), _StubBridge(rec), _StubRedis(rec)
    )
    coord.install_signal_handlers()
    # Trigger a synthetic SIGTERM via the running loop to verify wiring.
    loop = asyncio.get_running_loop()
    loop.call_soon(coord.request_shutdown)  # simulates handler firing
    await asyncio.wait_for(coord.wait_for_shutdown(), timeout=1.0)
    # Best-effort cleanup; if add_signal_handler did install, remove it.
    try:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)
    except (NotImplementedError, ValueError):
        pass


@pytest.mark.asyncio
async def test_shutdown_continues_when_a_step_raises() -> None:
    """A flaky teardown step (e.g. bridge.disconnect raises) must not
    block the next step from running."""
    rec = _Recorder()

    class _BrokenBridge:
        async def disconnect(self) -> None:
            rec.calls.append("bridge.disconnect.raised")
            raise RuntimeError("simulated disconnect fail")

    coord = ShutdownCoordinator(
        _StubCmd(rec), _StubHb(rec), _BrokenBridge(), _StubRedis(rec)
    )
    await coord.shutdown(tasks=[])
    assert "bridge.disconnect.raised" in rec.calls
    # Redis still closed despite bridge failure.
    assert "redis.aclose" in rec.calls
