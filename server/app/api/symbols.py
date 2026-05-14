"""Symbol whitelist endpoints. Auth required (added in step 1.4).

Phase 2.1 update: when the server has synced symbols from cTrader, GET /symbols
returns the intersection of the static whitelist and the broker-side active
set. Without a sync (Phase 1 default), it returns the whitelist as-is.

Phase 2.4 adds POST ``/symbols/{ftmo_symbol}/calculate-volume`` — the volume
preview the order form will use before placing an actual order.

Phase 4.A.1 update: ``GET /api/symbols/{ftmo_symbol}`` returns the new
``FTMOSymbol`` shape (ftmo-only fields per D-SM-09).

Phase 4.A.5 update: ``POST .../calculate-volume`` now requires ``pair_id``
in the body so the volume calculator can resolve the per-Exness-account
mapping via ``MappingService``. The Phase 4.A.1 ``_ftmo_symbol_to_legacy_mapping``
adapter is gone — Phase 3 single-leg pairs reuse the FTMO contract size for
the Exness leg automatically because the wizard isn't run for them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.dependencies.auth import get_current_user_rest
from app.dependencies.mapping_service import get_mapping_service
from app.services.conversion_rate import get_quote_to_usd_rate
from app.services.ftmo_whitelist_service import FTMOSymbol
from app.services.mapping_cache_schemas import MappingEntry
from app.services.mapping_service import MappingService
from app.services.market_data import MarketDataService
from app.services.redis_service import RedisService, get_redis_service
from app.services.volume_calc import calculate_volume

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


class CalculateVolumeRequest(BaseModel):
    """Request body for ``POST /api/symbols/{sym}/calculate-volume``.

    Phase 4.A.5: ``pair_id`` is required so the calculator can resolve the
    per-Exness-account mapping. Frontend (step 4.A.7) updates the API
    client to thread the pair through from the OrderForm.
    """

    model_config = ConfigDict(extra="forbid")

    pair_id: str = Field(..., min_length=1, description="Pair from /api/pairs")
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


def _phase3_compat_exness_mapping(ftmo_entry: FTMOSymbol) -> MappingEntry:
    """Synthesize a 1:1 ``MappingEntry`` for Phase 3 single-leg pairs.

    A Phase 3 pair has no Exness mapping cache (the wizard hasn't been
    run for that account, or the pair simply has no ``exness_account_id``).
    The calculator still needs an Exness contract size to compute the
    secondary leg — for the single-leg case we mirror the FTMO spec, which
    is exactly what the Phase 1-3 ``_ftmo_symbol_to_legacy_mapping`` adapter
    used to do (see git history). Once the wizard is run for an account,
    the real ``MappingEntry`` from ``MappingService.get_pair_mapping`` is
    used instead. See deviation D-4.A.5-3 in the step self-check.
    """
    return MappingEntry(
        ftmo=ftmo_entry.name,
        exness=ftmo_entry.name,
        match_type="exact",
        contract_size=ftmo_entry.ftmo_units_per_lot,
        pip_size=ftmo_entry.ftmo_pip_size,
        pip_value=ftmo_entry.ftmo_pip_value,
        quote_ccy=ftmo_entry.quote_ccy,
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )


@router.get("/")
async def list_symbols(
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
    mapping_service: Annotated[MappingService, Depends(get_mapping_service)],
) -> dict[str, list[str]]:
    whitelist_names = mapping_service.all_ftmo_symbol_names()
    active = await redis_svc.get_active_symbols()
    if not active:
        return {"symbols": whitelist_names}
    whitelist_set = set(whitelist_names)
    return {"symbols": sorted(s for s in active if s in whitelist_set)}


@router.get("/{ftmo_symbol}")
async def get_symbol(
    ftmo_symbol: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    mapping_service: Annotated[MappingService, Depends(get_mapping_service)],
) -> FTMOSymbol:
    """Return the FTMOSymbol entry (ftmo-only fields per D-SM-09)."""
    entry = mapping_service.get_ftmo_symbol(ftmo_symbol)
    if entry is None:
        raise HTTPException(status_code=404, detail="Symbol not in whitelist")
    return entry


@router.post("/{ftmo_symbol}/calculate-volume", response_model=CalculateVolumeResponse)
async def calculate_volume_endpoint(
    ftmo_symbol: str,
    req: CalculateVolumeRequest,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
    mapping_service: Annotated[MappingService, Depends(get_mapping_service)],
) -> CalculateVolumeResponse:
    """Preview the primary + secondary volumes for a hedge order.

    Does not place anything. Returns 404 if the symbol isn't in the whitelist
    or hasn't been synced from the broker, 503 if the USD conversion rate
    isn't available yet (the call subscribes the missing pair so a retry in
    a few seconds usually succeeds), 400 for SL too tight, 422 for malformed
    inputs.

    Per-Exness-account mapping resolution (Phase 4.A.5): when the pair has
    a wizard-confirmed mapping for ``ftmo_symbol``, the real
    ``MappingEntry`` (with the broker's actual contract size) is used. For
    Phase 3 pairs (no wizard run yet), a 1:1 synthetic mapping is used —
    same behavior as the deleted ``_ftmo_symbol_to_legacy_mapping`` adapter.
    """
    ftmo_entry = mapping_service.get_ftmo_symbol(ftmo_symbol)
    if ftmo_entry is None:
        raise HTTPException(status_code=404, detail="Symbol not in whitelist")

    config = await redis_svc.get_symbol_config(ftmo_symbol)
    if not config or "ctrader_symbol_id" not in config:
        raise HTTPException(status_code=404, detail="Symbol not in active set")

    md = _get_market_data()
    rate = await get_quote_to_usd_rate(ftmo_entry.quote_ccy, redis_svc, md)
    if rate <= 0:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Conversion rate {ftmo_entry.quote_ccy}->USD not available yet. "
                "Try again in a few seconds."
            ),
        )

    # Per-pair Exness mapping lookup — falls back to a 1:1 synthetic for
    # Phase 3 pairs (D-4.A.5-3).
    pair_lookup = await mapping_service.get_pair_mapping(req.pair_id, ftmo_symbol)
    if pair_lookup is not None:
        _, exness_mapping = pair_lookup
    else:
        exness_mapping = _phase3_compat_exness_mapping(ftmo_entry)

    try:
        result = calculate_volume(
            risk_amount=req.risk_amount,
            entry=req.entry,
            sl=req.sl,
            symbol_config=config,
            ftmo_symbol=ftmo_entry,
            exness_mapping=exness_mapping,
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
        quote_ccy=ftmo_entry.quote_ccy,
        quote_to_usd_rate=rate,
    )
