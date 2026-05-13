"""Graceful shutdown coordinator for the Exness client.

Mirrors ``apps/ftmo-client/ftmo_client/shutdown.py`` but uses an explicit
``ShutdownCoordinator`` class that owns the cancellation ORDER specified
in D-088: stop cmd_processor → stop heartbeat → disconnect bridge →
close Redis.

Tests construct the coordinator with stubs for each component and assert
that ``shutdown`` invokes them in the right order.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Protocol

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
    ) -> None:
        self._cmd = cmd_processor
        self._heartbeat = heartbeat
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

        Order:
          1. ``cmd_processor.stop()`` — flip the running flag so the
             XREADGROUP loop exits on its next BLOCK timeout.
          2. ``heartbeat.stop()`` — flip the loop flag.
          3. Cancel + gather the asyncio.Tasks (gives loops time to
             observe the flag and exit cleanly).
          4. ``bridge.disconnect()`` — release MT5 lib handle.
          5. ``redis_client.aclose()`` — close the pool.

        Each stage is wrapped in try/except so a flaky shutdown step
        doesn't prevent the next stage from running.
        """
        logger.info("shutdown.begin")

        for step_name, awaitable in (
            ("cmd_processor.stop", self._cmd.stop()),
            ("heartbeat.stop", self._heartbeat.stop()),
        ):
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
