"""Heartbeat publisher for the Exness client.

Writes ``client:exness:{account_id}`` HASH every 5 seconds with a 30s
TTL. The faster cadence (vs FTMO's 10s) gives operators a tighter
detection window for MT5 terminal disconnects — the synchronous MT5
lib has no push notifications, so heartbeats are the only signal the
server has that the client process is up + the MT5 lib is responsive.

RedisError during a single beat is logged + swallowed; the loop keeps
running so a flapping Redis doesn't take down the trading client.

Mirrors the FTMO ``heartbeat.py`` module-level loop in spirit, but
wraps it in a class so ``ShutdownCoordinator.shutdown()`` can call
``await heartbeat.stop()`` for symmetry with ``cmd_processor.stop()``.
"""

from __future__ import annotations

import asyncio
import logging
import time

import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError

from exness_client import __version__
from exness_client.bridge_service import MT5BridgeService

logger = logging.getLogger(__name__)


HEARTBEAT_TTL_SECONDS = 30


def _key(account_id: str) -> str:
    return f"client:exness:{account_id}"


class HeartbeatLoop:
    """5-second heartbeat HSET into ``client:exness:{account_id}``."""

    def __init__(
        self,
        redis: redis_asyncio.Redis,
        bridge: MT5BridgeService,
        account_id: str,
        interval_s: float = 5.0,
    ) -> None:
        self._redis = redis
        self._bridge = bridge
        self._account_id = account_id
        self._interval_s = interval_s
        self._running = False
        self._stopped = asyncio.Event()

    async def publish_once(self) -> None:
        """Single heartbeat write. Exposed for first-beat-on-startup.

        Uses ``bridge.health_check`` so the status reflects current
        MT5 lib state, not just process liveness. A process that's up
        but disconnected from MT5 publishes ``status=offline``.
        """
        health = await self._bridge.health_check()
        status = "online" if health["connected"] and health["terminal_ok"] else "offline"
        now_ms = int(time.time() * 1000)
        await self._redis.hset(  # type: ignore[misc]
            _key(self._account_id),
            mapping={
                "status": status,
                "last_heartbeat": str(now_ms),
                "broker": "exness",
                "account_id": self._account_id,
                "terminal_ok": str(health["terminal_ok"]).lower(),
                "trade_allowed": str(health["trade_allowed"]).lower(),
                "version": __version__,
            },
        )
        await self._redis.expire(_key(self._account_id), HEARTBEAT_TTL_SECONDS)

    async def run(self) -> None:
        """Run heartbeat writes until ``stop()`` is called."""
        logger.info(
            "heartbeat_loop starting for account=%s (interval=%ss, ttl=%ds)",
            self._account_id,
            self._interval_s,
            HEARTBEAT_TTL_SECONDS,
        )
        self._running = True
        self._stopped.clear()
        try:
            while self._running:
                try:
                    await self.publish_once()
                except RedisError as exc:
                    logger.warning("heartbeat write failed: %s", exc)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("heartbeat unexpected error")

                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_s)
                except TimeoutError:
                    continue
                else:
                    break
        finally:
            logger.info("heartbeat_loop exiting (account=%s)", self._account_id)

    async def stop(self) -> None:
        """Flip the running flag so the loop exits on its next wake."""
        self._running = False
        self._stopped.set()
