"""Symbol whitelist endpoints. Auth required (added in step 1.4).

Phase 2.1 update: when the server has synced symbols from cTrader, GET /symbols
returns the intersection of the static whitelist and the broker-side active
set. Without a sync (Phase 1 default), it returns the whitelist as-is.

Phase 2.4 adds POST ``/symbols/{ftmo_symbol}/calculate-volume`` — the volume
preview the order form will use before placing an actual order.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from hedger_shared.symbol_mapping import SymbolMapping
from pydantic import BaseModel, Field

from app.dependencies.auth import get_current_user_rest
from app.services import symbol_whitelist
from app.services.conversion_rate import get_quote_to_usd_rate
from app.services.market_data import MarketDataService
from app.services.redis_service import RedisService, get_redis_service
from app.services.volume_calc import calculate_volume

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


class CalculateVolumeRequest(BaseModel):
    """Request body for ``POST /api/symbols/{sym}/calculate-volume``."""

    entry: float = Field(..., gt=0, description="Entry price in quote currency")
    sl: float = Field(..., gt=0, description="Stop-loss price in quote currency")
    risk_amount: float = Field(..., gt=0, description="USD willing to lose on the primary leg")
    ratio: float = Field(default=1.0, gt=0, le=10, description="secondary/primary volume ratio")


class CalculateVolumeResponse(BaseModel):
    symbol: str
    volume_primary: float
    volume_secondary: float
    sl_pips: float
    pip_value_usd_per_lot: float
    sl_usd_per_lot: float
    quote_ccy: str
    quote_to_usd_rate: float


def _get_market_data() -> MarketDataService | None:
    """Return the running MarketDataService (or None if not authenticated)."""
    from app.main import app  # noqa: PLC0415  — lazy to avoid import cycle

    md: MarketDataService | None = getattr(app.state, "market_data", None)
    if md is None or not md.is_authenticated:
        return None
    return md


@router.get("/")
async def list_symbols(
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> dict[str, list[str]]:
    whitelist_names = symbol_whitelist.get_all_symbols()
    active = await redis_svc.get_active_symbols()
    if not active:
        return {"symbols": whitelist_names}
    whitelist_set = set(whitelist_names)
    return {"symbols": sorted(s for s in active if s in whitelist_set)}


@router.get("/{ftmo_symbol}")
async def get_symbol(
    ftmo_symbol: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
) -> SymbolMapping:
    mapping = symbol_whitelist.get_symbol_mapping(ftmo_symbol)
    if mapping is None:
        raise HTTPException(status_code=404, detail="Symbol not in whitelist")
    return mapping


@router.post("/{ftmo_symbol}/calculate-volume", response_model=CalculateVolumeResponse)
async def calculate_volume_endpoint(
    ftmo_symbol: str,
    req: CalculateVolumeRequest,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> CalculateVolumeResponse:
    """Preview the primary + secondary volumes for a hedge order.

    Does not place anything. Returns 404 if the symbol isn't in the whitelist
    or hasn't been synced from the broker, 503 if the USD conversion rate
    isn't available yet (the call subscribes the missing pair so a retry in
    a few seconds usually succeeds), 400 for SL too tight, 422 for malformed
    inputs.
    """
    mapping = symbol_whitelist.get_symbol_mapping(ftmo_symbol)
    if mapping is None:
        raise HTTPException(status_code=404, detail="Symbol not in whitelist")

    config = await redis_svc.get_symbol_config(ftmo_symbol)
    if not config or "ctrader_symbol_id" not in config:
        raise HTTPException(status_code=404, detail="Symbol not in active set")

    md = _get_market_data()
    rate = await get_quote_to_usd_rate(mapping.quote_ccy, redis_svc, md)
    if rate <= 0:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Conversion rate {mapping.quote_ccy}->USD not available yet. "
                "Try again in a few seconds."
            ),
        )

    try:
        result = calculate_volume(
            risk_amount=req.risk_amount,
            entry=req.entry,
            sl=req.sl,
            symbol_config=config,
            whitelist_row=mapping,
            ratio=req.ratio,
            quote_to_usd_rate=rate,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return CalculateVolumeResponse(
        symbol=ftmo_symbol,
        volume_primary=result["volume_primary"],
        volume_secondary=result["volume_secondary"],
        sl_pips=result["sl_pips"],
        pip_value_usd_per_lot=result["pip_value_usd_per_lot"],
        sl_usd_per_lot=result["sl_usd_per_lot"],
        quote_ccy=mapping.quote_ccy,
        quote_to_usd_rate=rate,
    )
