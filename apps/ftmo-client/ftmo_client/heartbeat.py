"""Heartbeat publisher for the FTMO client.

Writes ``client:ftmo:{account_id}`` HASH every 10 seconds with a 30s
TTL. Server checks key existence to decide online/offline (allow 3
missed beats — see ``docs/06-data-models.md §4``).

RedisError during a single beat is logged + swallowed; the loop keeps
running so a flapping Redis doesn't take down the trading client.
"""

from __future__ import annotations

import asyncio
import logging
import time

import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError

from ftmo_client import __version__
from ftmo_client.shutdown import ShutdownController

logger = logging.getLogger(__name__)

# Heartbeat write cadence. Server's TTL of 30s allows 3 missed beats
# (per docs/06-data-models.md §4 and §11) before the client is marked
# offline.
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_TTL_SECONDS = 30


def _key(account_id: str) -> str:
    return f"client:ftmo:{account_id}"


async def publish_once(redis: redis_asyncio.Redis, account_id: str) -> None:
    """Single heartbeat write — exposed for tests and for the first beat
    on startup so the server sees ``online`` before the first interval
    elapses.
    """
    now_ms = int(time.time() * 1000)
    await redis.hset(  # type: ignore[misc]
        _key(account_id),
        mapping={
            "status": "online",
            "last_seen": str(now_ms),
            "version": __version__,
        },
    )
    await redis.expire(_key(account_id), HEARTBEAT_TTL_SECONDS)


async def heartbeat_loop(
    redis: redis_asyncio.Redis,
    account_id: str,
    shutdown: ShutdownController,
) -> None:
    """Run heartbeat writes until shutdown is requested.

    Sleeps via ``asyncio.wait_for(shutdown.wait(), timeout=10)`` so the
    loop wakes immediately on shutdown rather than waiting out the full
    interval.
    """
    logger.info(
        "heartbeat_loop starting for account=%s (interval=%ds, ttl=%ds)",
        account_id,
        HEARTBEAT_INTERVAL_SECONDS,
        HEARTBEAT_TTL_SECONDS,
    )
    while not shutdown.is_requested:
        try:
            await publish_once(redis, account_id)
        except RedisError as exc:
            # Don't crash on Redis flap; the next beat will retry.
            logger.warning("heartbeat write failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Any other unexpected error — log once, keep looping.
            logger.exception("heartbeat unexpected error")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            # If wait() returned, shutdown was requested — break next iter.
        except TimeoutError:
            # Normal interval tick.
            continue
    logger.info("heartbeat_loop exiting (account=%s)", account_id)
