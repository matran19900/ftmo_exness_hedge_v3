"""Account info polling loop.

Step 3.5. Polls cTrader for balance + used margin every 30s and HSETs
the result to ``account:ftmo:{account_id}`` so the server (Phase 4 UI
layer) can read account snapshots without round-tripping to cTrader on
every page load. Cadence matches the FTMO compliance dashboard refresh
rate; if the operator needs faster updates, the cmd-stream path
(executions → resp_stream + event_stream) carries every realized
balance change as it happens.

Failure mode: a single poll error is logged and the next poll retries.
Cancellation propagates so ``main.amain``'s shutdown sequence can
``await asyncio.gather(*tasks, return_exceptions=True)`` cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import time

import redis.asyncio as redis_asyncio

from ftmo_client.ctrader_bridge import CtraderBridge
from ftmo_client.shutdown import ShutdownController

logger = logging.getLogger(__name__)

ACCOUNT_INFO_INTERVAL_SECONDS = 30.0


def _key(account_id: str) -> str:
    return f"account:ftmo:{account_id}"


async def publish_once(
    bridge: CtraderBridge,
    redis: redis_asyncio.Redis,
    account_id: str,
) -> None:
    """Single poll + HSET. Exposed for tests + for the first publish on
    startup (so the server sees a fresh snapshot before the first
    interval elapses).
    """
    info = await bridge.get_account_info()
    payload = {
        "balance": str(info["balance"]),
        "equity": str(info["equity"]),
        "margin": str(info["margin"]),
        "free_margin": str(info["free_margin"]),
        "currency": info["currency"],
        "money_digits": str(info["money_digits"]),
        "updated_at": str(int(time.time() * 1000)),
    }
    await redis.hset(_key(account_id), mapping=payload)  # type: ignore[misc]
    logger.debug(
        "published account info: balance=%s margin=%s",
        payload["balance"],
        payload["margin"],
    )


async def account_info_loop(
    bridge: CtraderBridge,
    redis: redis_asyncio.Redis,
    account_id: str,
    shutdown: ShutdownController,
    interval_seconds: float = ACCOUNT_INFO_INTERVAL_SECONDS,
) -> None:
    """Run account-info publishes until shutdown is requested.

    Sleeps via ``asyncio.wait_for(shutdown.wait(), timeout=interval)``
    so the loop wakes immediately on shutdown rather than waiting out
    the full 30s interval.
    """
    logger.info(
        "account_info_loop starting for account=%s (interval=%.1fs)",
        account_id,
        interval_seconds,
    )
    while not shutdown.is_requested:
        try:
            await publish_once(bridge, redis, account_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Bridge timeouts, transient cTrader errors, Redis flaps —
            # log + keep looping. Next poll will retry.
            logger.exception("account_info_loop error; continuing")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)
            # wait() returned → shutdown was requested; exit loop.
        except TimeoutError:
            # Normal interval tick.
            continue
    logger.info("account_info_loop exiting (account=%s)", account_id)
