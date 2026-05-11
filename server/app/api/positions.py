"""Positions API (step 3.9).

``GET /api/positions`` returns filled orders enriched with the live
P&L snapshot from ``position_cache:{order_id}`` (written by step
3.8's position_tracker). Each row in the response carries the order's
static fields (sl_price, tp_price, executed_at, etc.) AND the
tracker's live fields (current_price, unrealized_pnl, is_stale).

Schemas inline per the Phase 2 / step 3.6 convention.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.dependencies.auth import get_current_user_rest
from app.services.order_service import OrderService
from app.services.redis_service import RedisService, get_redis_service

router = APIRouter(prefix="/api/positions", tags=["positions"])


class PositionListResponse(BaseModel):
    """Live-positions list. Order of entries: newest fill first.

    The ``positions`` array carries flat dicts with mixed static
    (order) + live (position_cache) fields — the frontend renders the
    open-positions table directly from this payload. ``is_stale=true``
    flags rows whose live P&L is older than the tracker's threshold
    OR whose cache entry hasn't been written yet (just-filled race).
    """

    positions: list[dict[str, str]]
    total: int


@router.get(
    "",
    response_model=PositionListResponse,
    summary="List filled positions with live P&L",
)
async def list_positions(
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
    account_id: Annotated[
        str | None,
        Query(description="Filter by ftmo_account_id."),
    ] = None,
    symbol: Annotated[str | None, Query()] = None,
) -> PositionListResponse:
    service = OrderService(redis_svc)
    positions = await service.list_positions(account_id=account_id, symbol=symbol)
    return PositionListResponse(positions=positions, total=len(positions))
