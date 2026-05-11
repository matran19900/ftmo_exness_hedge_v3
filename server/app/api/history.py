"""History API (step 3.9).

``GET /api/history`` lists closed orders within a time range. Default
window is the trailing 7 days when ``from_ts`` / ``to_ts`` aren't
provided — matches the most common operator workflow (this week's
trades).
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.dependencies.auth import get_current_user_rest
from app.services.order_service import OrderService
from app.services.redis_service import RedisService, get_redis_service

router = APIRouter(prefix="/api/history", tags=["history"])

# Default time window when the caller doesn't supply from_ts: 7 days
# back from now. Matches the default range a trader expects when
# opening the History tab without picking a custom range.
_DEFAULT_HISTORY_WINDOW_MS = 7 * 24 * 60 * 60 * 1000


class HistoryListResponse(BaseModel):
    """Paginated closed-order history. ``total`` is the unpaged count
    so the frontend renders pagination correctly."""

    history: list[dict[str, str]]
    total: int


@router.get(
    "",
    response_model=HistoryListResponse,
    summary="List closed orders in time range",
)
async def list_history(
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
    from_ts: Annotated[
        int | None,
        Query(
            description=(
                "Start of window (epoch ms, inclusive). Defaults to now − 7 days when omitted."
            ),
        ),
    ] = None,
    to_ts: Annotated[
        int | None,
        Query(
            description=("End of window (epoch ms, inclusive). Defaults to now when omitted."),
        ),
    ] = None,
    symbol: Annotated[str | None, Query()] = None,
    account_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HistoryListResponse:
    now_ms = int(time.time() * 1000)
    resolved_from = from_ts if from_ts is not None else now_ms - _DEFAULT_HISTORY_WINDOW_MS
    resolved_to = to_ts if to_ts is not None else now_ms

    if resolved_from > resolved_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_time_range",
                "message": "from_ts must be <= to_ts",
            },
        )

    service = OrderService(redis_svc)
    history, total = await service.list_history(
        from_ts=resolved_from,
        to_ts=resolved_to,
        symbol=symbol,
        account_id=account_id,
        limit=limit,
        offset=offset,
    )
    return HistoryListResponse(history=history, total=total)
