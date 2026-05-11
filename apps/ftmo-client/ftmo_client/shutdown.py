"""Signal-driven graceful shutdown controller.

``ShutdownController.wait()`` blocks until the process receives SIGINT
or SIGTERM. ``main.amain`` then cancels the heartbeat + command loops,
disconnects the cTrader bridge, and exits cleanly. Tests construct the
controller without installing handlers and trigger shutdown directly.
"""

from __future__ import annotations

import asyncio
import logging
import signal

logger = logging.getLogger(__name__)


class ShutdownController:
    """Single asyncio.Event with explicit ``request_shutdown`` for tests."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def is_requested(self) -> bool:
        return self._event.is_set()

    def request_shutdown(self) -> None:
        """Idempotent — multiple signals trigger a single shutdown."""
        if not self._event.is_set():
            logger.info("shutdown signal received")
            self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


def install_signal_handlers(controller: ShutdownController) -> None:
    """Wire SIGINT + SIGTERM to the controller via the running asyncio loop.

    Uses ``loop.add_signal_handler`` rather than ``signal.signal`` so the
    coroutine cancellation path is honored — the standard ``signal``
    module would interrupt the running task synchronously.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, controller.request_shutdown)
        except NotImplementedError:
            # Windows event loops don't support add_signal_handler. The
            # FTMO client runs on Linux in Phase 3, but Phase 4 will run
            # on Windows for the Exness side — keep the fallback so the
            # same module compiles cross-platform.
            signal.signal(sig, lambda *_args: controller.request_shutdown())
