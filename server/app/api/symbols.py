"""Symbol whitelist endpoints. Auth required (added in step 1.4).

Phase 2.1 update: when the server has synced symbols from cTrader, GET /symbols
returns the intersection of the static whitelist and the broker-side active
set. Without a sync (Phase 1 default), it returns the whitelist as-is.

Phase 2.4 adds POST ``/symbols/{ftmo_symbol}/calculate-volume`` — the volume
preview the order form will use before placing an actual order.

Phase 4.A.1 update: ``GET /api/symbols/{ftmo_symbol}`` now returns the new
``FTMOSymbol`` shape (ftmo-only fields per D-SM-09). The volume calculator
still consumes the legacy ``SymbolMapping`` shape; this endpoint constructs
an adapter from ``FTMOSymbol`` with ``exness_*`` fields populated from the
FTMO equivalents (1:1 hedge assumption). Step 4.A.5 replaces the adapter
with a per-pair lookup via ``MappingService``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from hedger_shared.symbol_mapping import SymbolMapping
from pydantic import BaseModel, Field

from app.dependencies.auth import get_current_user_rest
from app.services import symbol_whitelist
from app.services.conversion_rate import get_quote_to_usd_rate
from app.services.ftmo_whitelist_service import FTMOSymbol
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
) -> FTMOSymbol:
    """Return the FTMOSymbol entry (ftmo-only fields per D-SM-09).

    The Phase 1-3 ``SymbolMapping`` return type carried Exness-side fields;
    those moved to ``MappingService`` (per Exness account) in Phase 4.A.
    Frontend pieces that still want an Exness-side preview are wired by
    step 4.A.6+ through the per-account mapping wizard endpoints.
    """
    entry = symbol_whitelist.get_symbol_mapping(ftmo_symbol)
    if entry is None:
        raise HTTPException(status_code=404, detail="Symbol not in whitelist")
    return entry


def _ftmo_symbol_to_legacy_mapping(entry: FTMOSymbol) -> SymbolMapping:
    """Build a transitional ``SymbolMapping`` from an ``FTMOSymbol``.

    Phase 4.A.1 leaves ``volume_calc.calculate_volume`` consuming the legacy
    type; this adapter populates the Exness fields with the FTMO equivalents
    (1:1 hedge ratio). Behaviour matches a Standard Exness account, which is
    the Phase 1-3 default and what the existing tests assert.

    Step 4.A.5 replaces this adapter with a per-pair ``MappingService``
    lookup that honours Cent / Pro / Raw account divergences.
    """
    return SymbolMapping(
        ftmo=entry.name,
        exness=entry.name,
        match_type="exact",
        ftmo_units_per_lot=entry.ftmo_units_per_lot,
        exness_trade_contract_size=entry.ftmo_units_per_lot,
        ftmo_pip_size=entry.ftmo_pip_size,
        exness_pip_size=entry.ftmo_pip_size,
        ftmo_pip_value=entry.ftmo_pip_value,
        exness_pip_value=entry.ftmo_pip_value,
        quote_ccy=entry.quote_ccy,
    )


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
    entry = symbol_whitelist.get_symbol_mapping(ftmo_symbol)
    if entry is None:
        raise HTTPException(status_code=404, detail="Symbol not in whitelist")

    config = await redis_svc.get_symbol_config(ftmo_symbol)
    if not config or "ctrader_symbol_id" not in config:
        raise HTTPException(status_code=404, detail="Symbol not in active set")

    md = _get_market_data()
    rate = await get_quote_to_usd_rate(entry.quote_ccy, redis_svc, md)
    if rate <= 0:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Conversion rate {entry.quote_ccy}->USD not available yet. "
                "Try again in a few seconds."
            ),
        )

    legacy_mapping = _ftmo_symbol_to_legacy_mapping(entry)
    try:
        result = calculate_volume(
            risk_amount=req.risk_amount,
            entry=req.entry,
            sl=req.sl,
            symbol_config=config,
            whitelist_row=legacy_mapping,
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
        quote_ccy=entry.quote_ccy,
        quote_to_usd_rate=rate,
    )
