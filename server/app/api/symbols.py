"""Symbol whitelist endpoints. Auth required (added in step 1.4).

Phase 2.1 update: when the server has synced symbols from cTrader, GET /symbols
returns the intersection of the static whitelist and the broker-side active
set. Without a sync (Phase 1 default), it returns the whitelist as-is.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from hedger_shared.symbol_mapping import SymbolMapping

from app.dependencies.auth import get_current_user_rest
from app.services import symbol_whitelist
from app.services.redis_service import RedisService, get_redis_service

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


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
