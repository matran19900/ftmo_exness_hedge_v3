"""FastAPI entrypoint: lifespan-managed Redis pool + symbol whitelist + routers."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.symbols import router as symbols_router
from app.config import get_settings
from app.redis_client import close_redis, init_redis
from app.services import symbol_whitelist

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
    symbol_whitelist.load_whitelist(settings.symbol_mapping_path)
    logger.info(
        "Server ready (redis=%s, symbols=%d)",
        _mask_redis_url(settings.redis_url),
        len(symbol_whitelist.get_all_symbols()),
    )
    try:
        yield
    finally:
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
app.include_router(symbols_router)
