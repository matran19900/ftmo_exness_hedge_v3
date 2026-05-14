"""Graceful shutdown coordinator for the Exness client.

Mirrors ``apps/ftmo-client/ftmo_client/shutdown.py`` but uses an explicit
``ShutdownCoordinator`` class that owns the cancellation ORDER specified
in D-088 (extended in step 4.3 to insert the position monitor between
the cmd processor and the heartbeat):

  1. ``cmd_processor.stop()`` — stop accepting new actions.
  2. ``position_monitor.stop()`` — stop polling MT5 (no more events
     after the heartbeat starts winding down).
  3. ``heartbeat.stop()`` — flip the running flag last so ``status=online``
     stays visible to the server until the rest of the lifecycle has
     wound down.
  4. Cancel + gather background tasks.
  5. ``bridge.disconnect()`` — release the MT5 lib handle.
  6. ``redis_client.aclose()``.

Tests construct the coordinator with stubs for each component and assert
that ``shutdown`` invokes them in the right order.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _StoppableLoop(Protocol):
    async def stop(self) -> None: ...


class _CloseableBridge(Protocol):
    async def disconnect(self) -> None: ...


class _CloseableRedis(Protocol):
    async def aclose(self) -> None: ...


class ShutdownCoordinator:
    """Signal-driven shutdown handle that orchestrates lifecycle teardown.

    Build with the four lifecycle components the main loop owns. Call
    ``install_signal_handlers`` once after the asyncio loop is running.
    Call ``wait_for_shutdown`` to block until SIGINT/SIGTERM, then call
    ``shutdown(tasks)`` to tear down in order.
    """

    def __init__(
        self,
        cmd_processor: _StoppableLoop,
        heartbeat: _StoppableLoop,
        bridge: _CloseableBridge,
        redis_client: _CloseableRedis,
        position_monitor: _StoppableLoop | None = None,
    ) -> None:
        self._cmd = cmd_processor
        self._heartbeat = heartbeat
        self._position_monitor = position_monitor
        self._bridge = bridge
        self._redis = redis_client
        self._event = asyncio.Event()

    # ----- Signal wiring -----

    @property
    def is_requested(self) -> bool:
        return self._event.is_set()

    def request_shutdown(self) -> None:
        """Idempotent — multiple signals trigger a single shutdown."""
        if not self._event.is_set():
            logger.info("shutdown signal received")
            self._event.set()

    async def wait_for_shutdown(self) -> None:
        await self._event.wait()

    def install_signal_handlers(self) -> None:
        """Wire SIGINT + SIGTERM to ``request_shutdown`` on the running loop.

        Falls back to the synchronous ``signal.signal`` API on platforms
        where ``loop.add_signal_handler`` is not supported (notably the
        default Windows ProactorEventLoop).
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
            except NotImplementedError:
                signal.signal(sig, lambda *_args: self.request_shutdown())

    # ----- Teardown -----

    async def shutdown(self, tasks: list[asyncio.Task[None]]) -> None:
        """Cancel background tasks in D-088 order then close resources.

        Order (Phase 4.3 inserted ``position_monitor`` between cmd and
        heartbeat per the class docstring):
          1. ``cmd_processor.stop()`` — flip the running flag so the
             XREADGROUP loop exits on its next BLOCK timeout.
          2. ``position_monitor.stop()`` — stop the 2-second poll loop.
          3. ``heartbeat.stop()`` — flip the loop flag.
          4. Cancel + gather the asyncio.Tasks (gives loops time to
             observe the flag and exit cleanly).
          5. ``bridge.disconnect()`` — release MT5 lib handle.
          6. ``redis_client.aclose()`` — close the pool.

        Each stage is wrapped in try/except so a flaky shutdown step
        doesn't prevent the next stage from running.
        """
        logger.info("shutdown.begin")

        steps: list[tuple[str, Any]] = [
            ("cmd_processor.stop", self._cmd.stop()),
        ]
        if self._position_monitor is not None:
            steps.append(("position_monitor.stop", self._position_monitor.stop()))
        steps.append(("heartbeat.stop", self._heartbeat.stop()))

        for step_name, awaitable in steps:
            try:
                await awaitable
            except Exception:
                logger.exception("%s raised during shutdown", step_name)

        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self._bridge.disconnect()
        except Exception:
            logger.exception("bridge.disconnect raised during shutdown")

        try:
            await self._redis.aclose()
        except Exception:
            logger.exception("redis.aclose raised during shutdown")

        logger.info("shutdown.complete")
