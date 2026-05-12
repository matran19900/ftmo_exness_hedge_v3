"""Account-status broadcast loop (step 3.12).

A single global background task — not one per account — that builds an
``account_status`` snapshot from
``RedisService.get_all_accounts_with_status()`` every 5 s and publishes
it on the WS ``accounts`` channel. The frontend's
``AccountStatusBar`` consumes the latest snapshot.

Shutdown contract: cancellation-based, matching the
``response_handler_loop`` / ``event_handler_loop`` /
``position_tracker_loop`` pattern in this codebase. The lifespan
``finally`` block calls ``Task.cancel()`` and ``await``s the task —
``asyncio.CancelledError`` propagates out and the loop terminates.
A single-cycle exception is logged and swallowed so a transient
Redis failure doesn't kill the indicator.

The 5 s cadence is intentional: the heartbeat key TTL is 30 s, so a
status flip needs at most one cycle to be picked up at the UI. Cutting
the interval lower buys nothing visible to the operator and adds
network chatter.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.services.account_helpers import row_to_entry
from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)

# Public for testability — tests assert against the exact channel name.
ACCOUNTS_CHANNEL = "accounts"

_BROADCAST_INTERVAL_SECONDS = 5.0


async def account_status_loop(
    redis_svc: RedisService,
    broadcast: BroadcastService,
    *,
    interval_seconds: float = _BROADCAST_INTERVAL_SECONDS,
) -> None:
    """Broadcast an ``account_status`` snapshot to the ``accounts`` WS
    channel on a fixed cadence.

    Runs until cancelled via ``Task.cancel()`` from the lifespan
    shutdown handler. ``interval_seconds`` is overridable for tests
    so the cycle can fire in milliseconds.
    """
    logger.info(
        "account_status_loop starting: interval=%.2fs",
        interval_seconds,
    )
    try:
        while True:
            cycle_start = time.monotonic()
            try:
                rows = await redis_svc.get_all_accounts_with_status()
                # Step 3.13a: route rows through ``row_to_entry`` so the
                # WS payload's ``enabled`` arrives as a real JSON bool
                # and ``status`` as the documented Literal. Pre-3.13a
                # the raw HASH-string rows shipped verbatim and the
                # frontend's ``Boolean("false") === true`` evaluation
                # made disabled accounts render as enabled.
                payload = {
                    "type": "account_status",
                    "ts": int(time.time() * 1000),
                    "accounts": [row_to_entry(row).model_dump() for row in rows],
                }
                await broadcast.publish(ACCOUNTS_CHANNEL, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("account_status_loop cycle failed; continuing")

            elapsed = time.monotonic() - cycle_start
            remaining = max(0.0, interval_seconds - elapsed)
            if remaining > 0:
                await asyncio.sleep(remaining)
    except asyncio.CancelledError:
        logger.info("account_status_loop cancelled")
        raise
