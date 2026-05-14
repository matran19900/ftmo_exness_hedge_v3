"""FastAPI entrypoint: lifespan-managed Redis pool + symbol whitelist + routers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
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
from app.api.symbol_mapping import router as symbol_mapping_router
from app.api.symbols import router as symbols_router
from app.api.ws import router as ws_router
from app.config import get_settings
from app.redis_client import close_redis, get_redis, init_redis
from app.services.account_status import account_status_loop
from app.services.auto_match_engine import AutoMatchEngine
from app.services.broadcast import BroadcastService
from app.services.event_handler import event_handler_loop
from app.services.ftmo_whitelist_service import FTMOWhitelistService
from app.services.mapping_cache_repository import MappingCacheRepository
from app.services.mapping_cache_service import MappingCacheService
from app.services.mapping_service import MappingService
from app.services.market_data import MarketDataService
from app.services.position_tracker import position_tracker_loop
from app.services.redis_service import RedisService
from app.services.response_handler import response_handler_loop

logger = logging.getLogger(__name__)


async def _init_mapping_statuses(redis_client: Any) -> int:
    """Phase 4.2: ensure every known Exness account has a
    ``mapping_status:{account_id}`` Redis key.

    For each member of ``accounts:exness``:
      - If a key already exists, leave it alone (covers the wizard-active
        state that ``populate_redis_from_disk`` set up moments ago).
      - Else, set ``active`` if ``account_to_mapping:{acc}`` resolves to
        a known cache file, otherwise ``pending_mapping`` so the
        AccountsTab UI surfaces the "Map Symbols" CTA on first paint.

    Returns the number of newly-initialised keys (purely diagnostic).
    """
    members = await redis_client.smembers("accounts:exness")
    initialized = 0
    for member in members or set():
        acc = member.decode() if isinstance(member, bytes) else str(member)
        key = f"mapping_status:{acc}"
        if await redis_client.exists(key):
            continue
        sig = await redis_client.get(f"account_to_mapping:{acc}")
        status = "active" if sig else "pending_mapping"
        await redis_client.set(key, status)
        initialized += 1
    return initialized


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
    # Phase 4.A.5: the legacy ``app.services.symbol_whitelist`` shim is
    # gone. ``ftmo_whitelist_service`` is the single source of truth for
    # FTMO whitelist lookups and gets stashed on ``app.state`` so the
    # mapping orchestrators can pick it up.
    ftmo_whitelist_service = FTMOWhitelistService(settings.symbol_mapping_path)
    app.state.ftmo_whitelist = ftmo_whitelist_service
    logger.info(
        "Server ready (redis=%s, symbols=%d)",
        _mask_redis_url(settings.redis_url),
        ftmo_whitelist_service.count,
    )

    # Phase 4.A.2: per-Exness-account mapping cache files. The repository
    # is the file-layer only — Redis populate lands in step 4.A.4. Sweeping
    # crashed tempfiles + listing existing caches at startup is enough to
    # surface filesystem issues (D-4.A.0-8) loudly here, before any wizard
    # write path is exercised at runtime.
    mapping_cache_repository = MappingCacheRepository(settings.symbol_mapping_cache_dir)
    sweep_result = mapping_cache_repository.sweep_temp_artifacts()
    logger.info(
        "mapping_cache_repository.initialized cache_dir=%s tmp_swept=%d bak_swept=%d",
        settings.symbol_mapping_cache_dir,
        sweep_result["tmp_removed"],
        sweep_result["bak_removed"],
    )
    loaded_caches = await mapping_cache_repository.list_all()
    logger.info(
        "mapping_cache_repository.loaded cache_count=%d", len(loaded_caches)
    )
    app.state.mapping_cache_repository = mapping_cache_repository

    # Phase 4.A.3: AutoMatchEngine — pure logic layer, no Redis. Hints
    # config bootstrapped from the 14 archived manual entries (D-SM-12 +
    # D-4.A.0-10). Engine instance is dormant until step 4.A.4 wires the
    # POST /symbol-mapping/auto-match endpoint.
    auto_match_engine = AutoMatchEngine(settings.symbol_match_hints_path)
    logger.info(
        "auto_match_engine.initialized hints_path=%s hint_count=%d",
        settings.symbol_match_hints_path,
        auto_match_engine.hint_count,
    )
    app.state.auto_match_engine = auto_match_engine

    app.state.market_data = None
    broadcast = BroadcastService(redis_svc=redis_svc)
    app.state.broadcast = broadcast

    # Phase 4.A.4: MappingCacheService orchestrates repository + engine + Redis
    # for the symbol-mapping wizard. Must be wired AFTER BroadcastService
    # exists (status-change broadcasts publish on `mapping_status:{acc}`)
    # and AFTER the Redis pool is up (populate_redis_from_disk reads it).
    # See deviation D-4.A.4-3 in the step self-check for why this lives
    # here instead of next to the engine init as the plan §2.5 sketched.
    mapping_cache_service = MappingCacheService(
        repository=mapping_cache_repository,
        engine=auto_match_engine,
        ftmo_whitelist=ftmo_whitelist_service,
        redis=get_redis(),
        broadcast=broadcast,
    )
    populated = await mapping_cache_service.populate_redis_from_disk()
    logger.info(
        "mapping_cache_service.initialized redis_populated_count=%d", populated
    )
    app.state.mapping_cache_service = mapping_cache_service

    # Phase 4.A.5: MappingService is the read-only per-pair facade used by
    # OrderService pre-flight, volume_calc, and the /check-symbol endpoint.
    # No I/O of its own at construction — it just stashes references.
    mapping_service = MappingService(
        ftmo_whitelist=ftmo_whitelist_service,
        cache_service=mapping_cache_service,
        redis=get_redis(),
    )
    logger.info("mapping_service.initialized")
    app.state.mapping_service = mapping_service

    # Phase 4.2: ensure every known Exness account has a ``mapping_status``
    # key set so the AccountsTab "Map Symbols" / "Active" / "Edit Mapping"
    # buttons render correctly on first paint. The Exness client (step 4.2)
    # may publish raw symbols before the operator officially saves a
    # mapping; this sweep covers the gap. Accounts already pointing at a
    # cache via ``account_to_mapping:{acc}`` are marked ``active`` so we
    # don't trash a wizard-completed state on a server restart.
    initialized_status_count = await _init_mapping_statuses(get_redis())
    logger.info(
        "mapping_status.initialized_on_startup count=%d",
        initialized_status_count,
    )

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
                cached = await md.sync_symbols(
                    redis_svc, ftmo_whitelist_service.all_symbols()
                )
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
app.include_router(symbol_mapping_router)
app.include_router(ws_router)
