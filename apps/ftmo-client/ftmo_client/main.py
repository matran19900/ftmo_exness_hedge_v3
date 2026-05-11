"""FTMO client entry point.

Wires Settings → Redis → OAuth token load → cTrader bridge connect →
heartbeat + command loop tasks. Exits 0 on graceful shutdown, 1 on
missing OAuth token (operator must run ``run_oauth_flow.py`` first), 2
on connect failure.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import redis.asyncio as redis_asyncio

from ftmo_client.command_loop import command_loop
from ftmo_client.config import FtmoClientSettings
from ftmo_client.ctrader_bridge import CtraderBridge
from ftmo_client.heartbeat import heartbeat_loop, publish_once
from ftmo_client.oauth_storage import is_token_expired, load_token
from ftmo_client.shutdown import ShutdownController, install_signal_handlers

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_NO_TOKEN = 1
EXIT_CONNECT_FAILED = 2


async def _connect_redis(redis_url: str) -> redis_asyncio.Redis:
    """Open a Redis connection and verify with PING."""
    client: redis_asyncio.Redis = redis_asyncio.from_url(  # type: ignore[no-untyped-call]
        redis_url, decode_responses=True, max_connections=8
    )
    await client.ping()
    return client


async def amain(settings: FtmoClientSettings | None = None) -> int:
    """Main coroutine. Tests call directly with a custom Settings + monkeypatched bridge."""
    if settings is None:
        settings = FtmoClientSettings()  # type: ignore[call-arg]

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("ftmo-client starting: account=%s", settings.ftmo_account_id)

    redis = await _connect_redis(settings.redis_url)
    logger.info("redis connected")

    token = await load_token(redis, settings.ftmo_account_id)
    if token is None:
        logger.error(
            "no OAuth token in Redis at ctrader:ftmo:%s:creds — "
            "run `python -m ftmo_client.scripts.run_oauth_flow "
            "--account-id %s` first",
            settings.ftmo_account_id,
            settings.ftmo_account_id,
        )
        await redis.aclose()
        return EXIT_NO_TOKEN

    if is_token_expired(token):
        logger.warning(
            "OAuth token at ctrader:ftmo:%s:creds is expired (or within skew). "
            "Step 3.5 will auto-refresh; for now please re-run run_oauth_flow.",
            settings.ftmo_account_id,
        )
    logger.info("oauth token loaded (ctid_trader_account_id=%s)", token["ctid_trader_account_id"])

    bridge = CtraderBridge(
        account_id=settings.ftmo_account_id,
        access_token=token["access_token"],
        ctid_trader_account_id=int(token["ctid_trader_account_id"]),
        client_id=settings.ctrader_client_id,
        client_secret=settings.ctrader_client_secret,
        host=settings.ctrader_host,
        port=settings.ctrader_port,
    )
    try:
        await bridge.connect_with_retry(max_attempts=10)
    except RuntimeError:
        logger.exception("cTrader connect_with_retry exhausted")
        await redis.aclose()
        return EXIT_CONNECT_FAILED

    # First heartbeat publish before entering the loop so the server
    # sees the client as ``online`` before the next interval elapses —
    # otherwise there's a 10s window where the server reports offline.
    try:
        await publish_once(redis, settings.ftmo_account_id)
    except Exception:
        logger.exception("initial heartbeat write failed; loop will retry")

    shutdown = ShutdownController()
    install_signal_handlers(shutdown)

    tasks = [
        asyncio.create_task(
            heartbeat_loop(redis, settings.ftmo_account_id, shutdown),
            name="heartbeat",
        ),
        asyncio.create_task(
            command_loop(redis, settings.ftmo_account_id, shutdown),
            name="command_loop",
        ),
    ]

    try:
        await shutdown.wait()
    finally:
        logger.info("shutdown initiated; cancelling tasks")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bridge.disconnect()
        await redis.aclose()
        logger.info("ftmo-client shutdown complete")

    return EXIT_OK


def main() -> None:
    """Sync entry point used by the ``ftmo-client`` console script."""
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
