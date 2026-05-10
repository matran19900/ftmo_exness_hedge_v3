"""FastAPI entrypoint: lifespan-managed Redis pool + symbol whitelist + routers."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.auth_ctrader import router as auth_ctrader_router
from app.api.charts import router as charts_router
from app.api.health import router as health_router
from app.api.pairs import router as pairs_router
from app.api.symbols import router as symbols_router
from app.api.ws import router as ws_router
from app.config import get_settings
from app.redis_client import close_redis, get_redis, init_redis
from app.services import symbol_whitelist
from app.services.broadcast import BroadcastService
from app.services.market_data import MarketDataService
from app.services.redis_service import RedisService

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

    try:
        yield
    finally:
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
app.include_router(ws_router)
