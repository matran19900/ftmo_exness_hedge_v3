"""Symbol whitelist endpoints. Auth required (added in step 1.4)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from hedger_shared.symbol_mapping import SymbolMapping

from app.dependencies.auth import get_current_user_rest
from app.services import symbol_whitelist

router = APIRouter(prefix="/api/symbols", tags=["symbols"])


@router.get("/")
async def list_symbols(
    _user: Annotated[str, Depends(get_current_user_rest)],
) -> dict[str, list[str]]:
    return {"symbols": symbol_whitelist.get_all_symbols()}


@router.get("/{ftmo_symbol}")
async def get_symbol(
    ftmo_symbol: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
) -> SymbolMapping:
    mapping = symbol_whitelist.get_symbol_mapping(ftmo_symbol)
    if mapping is None:
        raise HTTPException(status_code=404, detail="Symbol not in whitelist")
    return mapping
