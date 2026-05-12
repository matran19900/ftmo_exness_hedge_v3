"""FastAPI entrypoint: lifespan-managed Redis pool + symbol whitelist + routers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.accounts import router as accounts_router
from app.api.auth import router as auth_router
from app.api.auth_ctrader import router as auth_ctrader_router
from app.api.charts import router as charts_router
from app.api.health import router as health_router
from app.api.history import router as history_router
from app.api.orders import router as orders_router
from app.api.pairs import router as pairs_router
from app.api.positions import router as positions_router
from app.api.symbols import router as symbols_router
from app.api.ws import router as ws_router
from app.config import get_settings
from app.redis_client import close_redis, get_redis, init_redis
from app.services import symbol_whitelist
from app.services.account_status import account_status_loop
from app.services.broadcast import BroadcastService
from app.services.event_handler import event_handler_loop
from app.services.market_data import MarketDataService
from app.services.position_tracker import position_tracker_loop
from app.services.redis_service import RedisService
from app.services.response_handler import response_handler_loop

logger = logging.getLogger(__name__)


def _mask_redis_url(url: str) -> str:
    """Strip the password from a Redis URL for safe logging."""
    parsed = urlparse(url)
    if parsed.password:
        netloc = parsed.hostname or ""
        if parsed.username:
            netloc = f"{parsed.username}:***@{netloc}"
        else:
            netloc = f":***@{netloc}"
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    return urlunparse(parsed)


def _bootstrap_cors_origins() -> list[str]:
    """Read CORS origins directly from env at import time.

    Settings cannot be constructed at module load (REDIS_URL has no default),
    so the CORS allow-list is resolved here independently and re-resolved at
    runtime by `get_settings()` for any code path that needs it.
    """
    raw = os.environ.get("CORS_ORIGINS", "http://localhost:5173")
    return [item.strip() for item in raw.split(",") if item.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    await init_redis(settings.redis_url)
    redis_svc = RedisService(get_redis())
    # Create consumer groups before any client / broker code runs so a client
    # XADDing into a stream we haven't subscribed to yet can't strand a
    # message. Idempotent (BUSYGROUP swallowed inside _create_group), so
    # re-runs across restarts are safe. Failures other than BUSYGROUP
    # propagate — fail loud during startup rather than ship a half-wired
    # server.
    n_ftmo, n_exness = await redis_svc.setup_consumer_groups()
    logger.info(
        "setup_consumer_groups: created groups for %d ftmo + %d exness accounts",
        n_ftmo,
        n_exness,
    )
    symbol_whitelist.load_whitelist(settings.symbol_mapping_path)
    logger.info(
        "Server ready (redis=%s, symbols=%d)",
        _mask_redis_url(settings.redis_url),
        len(symbol_whitelist.get_all_symbols()),
    )

    app.state.market_data = None
    broadcast = BroadcastService(redis_svc=redis_svc)
    app.state.broadcast = broadcast
    creds = await redis_svc.get_ctrader_market_data_creds()
    if creds and settings.ctrader_client_id and settings.ctrader_client_secret:
        md = MarketDataService(
            host=settings.ctrader_host,
            port=settings.ctrader_port,
            client_id=settings.ctrader_client_id,
            client_secret=settings.ctrader_client_secret,
        )
        md.inject_broadcast(broadcast)
        try:
            await md.start()
            if creds["expires_at"] > int(time.time()):
                await md.authenticate(creds["access_token"], creds["account_id"])
                cached = await md.sync_symbols(redis_svc)
                logger.info("MarketDataService ready, authenticated, %d symbols cached", cached)
            else:
                logger.warning(
                    "Stored cTrader token expired; OAuth re-flow required at /api/auth/ctrader"
                )
            app.state.market_data = md
        except Exception:
            logger.exception("Failed to start MarketDataService at startup")
    else:
        logger.info(
            "No cTrader credentials in Redis; market-data idle. Visit /api/auth/ctrader to setup."
        )

    # Step 3.7: per-account response + event handler tasks. We start
    # one of each per registered FTMO account so each consumer-group
    # `server` reader has a dedicated coroutine. Empty account list
    # is fine — startup still completes; add_account at runtime
    # (Phase 5) would need to spawn additional tasks dynamically.
    ftmo_accounts = await redis_svc.get_all_account_ids("ftmo")
    response_tasks: list[asyncio.Task[None]] = []
    event_tasks: list[asyncio.Task[None]] = []
    position_tracker_tasks: list[asyncio.Task[None]] = []
    for acc in ftmo_accounts:
        response_tasks.append(
            asyncio.create_task(
                response_handler_loop(redis_svc, broadcast, acc),
                name=f"response_handler_{acc}",
            )
        )
        event_tasks.append(
            asyncio.create_task(
                event_handler_loop(redis_svc, broadcast, acc),
                name=f"event_handler_{acc}",
            )
        )
        # Step 3.8: per-account unrealized P&L loop.
        position_tracker_tasks.append(
            asyncio.create_task(
                position_tracker_loop(redis_svc, broadcast, acc),
                name=f"position_tracker_{acc}",
            )
        )
    app.state.response_tasks = response_tasks
    app.state.event_tasks = event_tasks
    app.state.position_tracker_tasks = position_tracker_tasks
    logger.info(
        "Started %d response_handler + %d event_handler + %d position_tracker "
        "tasks for FTMO accounts",
        len(response_tasks),
        len(event_tasks),
        len(position_tracker_tasks),
    )

    # Step 3.12: single global account-status broadcaster — one task
    # for all accounts (not per-account like the handler loops), since
    # each cycle re-reads the full account set from Redis and publishes
    # a snapshot. Cancellation goes through the same finally block.
    account_status_task: asyncio.Task[None] = asyncio.create_task(
        account_status_loop(redis_svc, broadcast),
        name="account_status_loop",
    )
    app.state.account_status_task = account_status_task

    try:
        yield
    finally:
        # Cancel response/event/position_tracker handlers + account
        # status loop first so they don't try to talk to a closing
        # Redis connection mid-flight.
        account_status_task.cancel()
        for task in response_tasks + event_tasks + position_tracker_tasks:
            task.cancel()
        if response_tasks or event_tasks or position_tracker_tasks:
            await asyncio.gather(
                *response_tasks,
                *event_tasks,
                *position_tracker_tasks,
                return_exceptions=True,
            )
        try:
            await account_status_task
        except asyncio.CancelledError:
            pass
        md = app.state.market_data
        if md is not None:
            try:
                await md.stop()
            except Exception:
                logger.exception("Error during MarketDataService shutdown")
        await close_redis()
        logger.info("Server shutdown complete")


app = FastAPI(
    title="FTMO Hedge Tool v3 — Server",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_bootstrap_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(auth_ctrader_router)
app.include_router(symbols_router)
app.include_router(charts_router)
app.include_router(pairs_router)
app.include_router(orders_router)
app.include_router(positions_router)
app.include_router(history_router)
app.include_router(accounts_router)
app.include_router(ws_router)
