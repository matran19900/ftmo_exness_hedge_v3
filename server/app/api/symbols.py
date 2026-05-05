"""Symbol whitelist endpoints. Auth will be added in step 1.3."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from hedger_shared.symbol_mapping import SymbolMapping

from app.services import symbol_whitelist

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


@router.get("/")
async def list_symbols() -> dict[str, list[str]]:
    return {"symbols": symbol_whitelist.get_all_symbols()}


@router.get("/{ftmo_symbol}")
async def get_symbol(ftmo_symbol: str) -> SymbolMapping:
    mapping = symbol_whitelist.get_symbol_mapping(ftmo_symbol)
    if mapping is None:
        raise HTTPException(status_code=404, detail="Symbol not in whitelist")
    return mapping
