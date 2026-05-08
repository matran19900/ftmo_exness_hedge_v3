"""Chart OHLC endpoint.

GET /api/charts/{ftmo_symbol}/ohlc returns historical candles with a
60-second Redis cache (key ``ohlc:{symbol}:{timeframe}:{count}``).
Live trendbar / spot subscriptions land in step 2.3.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies.auth import get_current_user_rest
from app.services.market_data import MarketDataService
from app.services.redis_service import RedisService, get_redis_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/charts", tags=["charts"])

VALID_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1")
_OHLC_CACHE_TTL_SECONDS = 60


def get_market_data() -> MarketDataService:
    """FastAPI dependency: return the running MarketDataService or 503."""
    from app.main import app  # noqa: PLC0415  — lazy to avoid import cycle

    md: MarketDataService | None = getattr(app.state, "market_data", None)
    if md is None or not md.is_authenticated:
        raise HTTPException(
            status_code=503,
            detail="Market data service not ready. Complete cTrader OAuth at /api/auth/ctrader.",
        )
    return md


@router.get("/{ftmo_symbol}/ohlc")
async def get_ohlc(
    ftmo_symbol: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
    md: Annotated[MarketDataService, Depends(get_market_data)],
    timeframe: Annotated[str, Query()] = "M15",
    count: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    """Return the last ``count`` candles for ``ftmo_symbol`` at ``timeframe``."""
    if timeframe not in VALID_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid timeframe. Allowed: {list(VALID_TIMEFRAMES)}",
        )

    cache_key = f"{ftmo_symbol}:{timeframe}:{count}"
    cached = await redis_svc.get_ohlc_cache(cache_key)
    if cached:
        return dict(json.loads(cached))

    try:
        candles = await md.get_trendbars(ftmo_symbol, timeframe, count, redis_svc)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        msg = str(e)
        if "not in active set" in msg:
            raise HTTPException(
                status_code=404, detail=f"Symbol not available: {ftmo_symbol}"
            ) from e
        if "Not authenticated" in msg:
            raise HTTPException(status_code=503, detail="Market data not authenticated") from e
        logger.exception("get_trendbars failed for %s/%s", ftmo_symbol, timeframe)
        raise HTTPException(status_code=502, detail=f"cTrader error: {msg}") from e

    payload: dict[str, Any] = {
        "symbol": ftmo_symbol,
        "timeframe": timeframe,
        "count": len(candles),
        "candles": candles,
    }
    await redis_svc.set_ohlc_cache(
        cache_key, json.dumps(payload), ttl_seconds=_OHLC_CACHE_TTL_SECONDS
    )
    return payload
