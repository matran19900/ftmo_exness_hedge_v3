"""Order creation endpoint (step 3.6).

POST /api/orders accepts a hedge-order create request, validates it
against Redis state (pair, account, client status, symbol, volume,
SL/TP direction), persists the ``order:{order_id}`` HASH, and pushes
the ``open`` command to ``cmd_stream:ftmo:{ftmo_account_id}``. The
response is 202 Accepted with the generated ``order_id`` +
``request_id`` — the actual fill outcome flows back asynchronously
via ``resp_stream`` (consumed by step 3.7's response_handler).

Phase 3 scope: FTMO leg only. The pair's ``exness_account_id`` is
recorded on the order row but no command is pushed to the Exness
cmd_stream; Phase 4 cascades the Exness leg from the FTMO fill
event.

Schema lives inline per the Phase 2 convention (``app/api/pairs.py``,
``app/api/symbols.py``) — no separate ``app/schemas/`` directory.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.dependencies.auth import get_current_user_rest
from app.services.order_service import OrderService, OrderValidationError
from app.services.redis_service import RedisService, get_redis_service

router = APIRouter(prefix="/api/orders", tags=["orders"])


class OrderCreateRequest(BaseModel):
    """Payload shape for ``POST /api/orders``.

    ``volume_lots`` is the user's input in lots (e.g. 0.01); the
    service converts to cTrader wire units via the symbol's
    ``lot_size``. ``sl`` / ``tp`` / ``entry_price`` default to 0 with
    the convention "0 = unset" (mirrors the cmd_stream schema in
    ``docs/05-redis-protocol.md §4.2``).
    """

    pair_id: str = Field(..., min_length=1, description="Existing pair_id from /api/pairs")
    symbol: str = Field(..., min_length=1, max_length=32)
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit", "stop"]
    volume_lots: float = Field(..., gt=0, description="Volume in lots, e.g. 0.01")
    sl: float = Field(default=0.0, ge=0, description="Stop loss price; 0 = unset")
    tp: float = Field(default=0.0, ge=0, description="Take profit price; 0 = unset")
    entry_price: float = Field(
        default=0.0,
        ge=0,
        description="Required for limit/stop; ignored for market",
    )

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        """Upper-case + strip the symbol so 'eurusd' / ' EURUSD ' both
        resolve to ``EURUSD`` against the active whitelist."""
        return v.upper().strip()


class OrderCreateResponse(BaseModel):
    """Server response on successful order creation.

    The actual fill / rejection arrives later via WS broadcast (step
    3.7's response_handler picks it up off resp_stream and pushes to
    subscribed clients keyed by ``order_id``)."""

    order_id: str
    request_id: str
    status: Literal["accepted"]
    message: str = "Order accepted, processing async via cmd_stream"


@router.post(
    "",
    response_model=OrderCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create new hedge order (FTMO leg only in Phase 3)",
)
async def create_order(
    req: OrderCreateRequest,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> OrderCreateResponse:
    """Create a hedge order.

    Returns 202 Accepted immediately after the cmd_stream push — the
    eventual cTrader fill / rejection flows to the client over WS.

    Error mapping (see ``OrderValidationError``):
      - 400: bad input (volume out of range, invalid SL/TP direction,
        pair/account disabled, symbol not whitelisted).
      - 404: missing referenced resource (pair, FTMO account,
        symbol_config).
      - 409: server-state conflict (FTMO client offline, no recent
        tick).
    """
    service = OrderService(redis_svc)
    try:
        order_id, request_id = await service.create_order(
            pair_id=req.pair_id,
            symbol=req.symbol,
            side=req.side,
            order_type=req.order_type,
            volume_lots=req.volume_lots,
            sl=req.sl,
            tp=req.tp,
            entry_price=req.entry_price,
        )
    except OrderValidationError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc
    return OrderCreateResponse(
        order_id=order_id,
        request_id=request_id,
        status="accepted",
    )
