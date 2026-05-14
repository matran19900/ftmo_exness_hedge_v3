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

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.dependencies.auth import get_current_user_rest
from app.dependencies.mapping_service import get_mapping_service
from app.services.mapping_service import MappingService
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
    mapping_service: Annotated[MappingService, Depends(get_mapping_service)],
) -> OrderCreateResponse:
    """Create a hedge order.

    Returns 202 Accepted immediately after the cmd_stream push — the
    eventual cTrader fill / rejection flows to the client over WS.

    Error mapping (see ``OrderValidationError``):
      - 400: bad input (volume out of range, invalid SL/TP direction,
        pair/account disabled, symbol not whitelisted, symbol not
        tradeable for pair Phase 4.A.5).
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
            mapping_service=mapping_service,
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


# ---------- Step 3.9: list / detail / close / modify schemas ----------


class OrderListResponse(BaseModel):
    """Paginated order list. ``total`` is the unpaged count so the
    frontend renders pagination correctly without re-issuing
    queries."""

    orders: list[dict[str, str]]
    total: int
    limit: int
    offset: int


class OrderDetailResponse(BaseModel):
    """Single-order detail. ``order`` is the raw HASH; consumers
    cast fields they care about (most are stored as strings in Redis)."""

    order: dict[str, str]


class OrderCloseRequest(BaseModel):
    """``POST /api/orders/{order_id}/close`` body.

    Phase 3 supports full close only (D-057). ``volume_lots`` is
    optional — when omitted, the service closes the order's full open
    volume. When provided, it MUST equal the open volume; partial
    close raises 400 ``partial_close_unsupported``.
    """

    volume_lots: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional explicit volume. Must equal the order's open "
            "volume if set (Phase 3 supports full close only)."
        ),
    )


class OrderModifyRequest(BaseModel):
    """``POST /api/orders/{order_id}/modify`` body.

    Each side is independently:
      - ``None``     → keep the order's existing value (frontend
        omits the field).
      - ``0``        → remove that side (BUY SL=0 means no stop loss).
      - positive    → set to that price. Direction validated against
        the latest tick.

    ``model_validator`` rejects a body with both fields ``None`` so
    the operator can't accidentally issue a no-op modify.
    """

    sl: float | None = Field(
        default=None,
        ge=0,
        description="New SL price; 0 = remove SL; null/missing = unchanged.",
    )
    tp: float | None = Field(
        default=None,
        ge=0,
        description="New TP price; 0 = remove TP; null/missing = unchanged.",
    )

    @model_validator(mode="after")
    def _at_least_one_side(self) -> OrderModifyRequest:
        if self.sl is None and self.tp is None:
            raise ValueError("at least one of sl or tp must be provided")
        return self


class OrderActionResponse(BaseModel):
    """Response shape for close + modify (mirror of the open response
    so the frontend can use a single ``OrderActionResponse`` type for
    all three mutation paths)."""

    order_id: str
    request_id: str
    status: Literal["accepted"]
    message: str


# ---------- Step 3.9: endpoints ----------


@router.get(
    "",
    response_model=OrderListResponse,
    summary="List orders with status / symbol / account filter + pagination",
)
async def list_orders(
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
    status_filter: Annotated[
        str,
        Query(
            alias="status",
            description=(
                "Filter by status; one of pending/filled/closed/"
                "rejected/cancelled/unknown, or 'all' (default)."
            ),
        ),
    ] = "all",
    symbol: Annotated[str | None, Query()] = None,
    account_id: Annotated[
        str | None,
        Query(description="Filter by ftmo_account_id."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderListResponse:
    """List orders matching the filters, newest-first.

    ``status`` is aliased as a query param so the URL stays
    ``?status=...`` while the Python identifier doesn't clash with
    ``fastapi.status`` (imported above for the 202 constant).
    """
    service = OrderService(redis_svc)
    orders, total = await service.list_orders(
        status=status_filter,
        symbol=symbol,
        account_id=account_id,
        limit=limit,
        offset=offset,
    )
    return OrderListResponse(orders=orders, total=total, limit=limit, offset=offset)


@router.get(
    "/{order_id}",
    response_model=OrderDetailResponse,
    summary="Get single order detail",
)
async def get_order_detail(
    order_id: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> OrderDetailResponse:
    service = OrderService(redis_svc)
    try:
        order = await service.get_order_by_id(order_id)
    except OrderValidationError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc
    return OrderDetailResponse(order=order)


@router.post(
    "/{order_id}/close",
    response_model=OrderActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Close a filled order (dispatch close command)",
)
async def close_order(
    order_id: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
    req: Annotated[OrderCloseRequest | None, Body()] = None,
) -> OrderActionResponse:
    """Dispatch a ``close`` command for one filled order.

    The mutation is async like ``POST /api/orders``: 202 means the
    command was pushed to ``cmd_stream:ftmo:{acc}`` + the side index
    is linked. The actual broker close flows back on resp_stream
    (consumed by step 3.7's response_handler).
    """
    service = OrderService(redis_svc)
    requested_volume = req.volume_lots if req is not None else None
    try:
        oid, rid = await service.close_order(order_id, volume_lots=requested_volume)
    except OrderValidationError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc
    return OrderActionResponse(
        order_id=oid,
        request_id=rid,
        status="accepted",
        message="Close command dispatched",
    )


@router.post(
    "/{order_id}/modify",
    response_model=OrderActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Modify SL/TP on a filled order",
)
async def modify_order(
    order_id: str,
    req: OrderModifyRequest,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> OrderActionResponse:
    service = OrderService(redis_svc)
    try:
        oid, rid = await service.modify_order(order_id, sl=req.sl, tp=req.tp)
    except OrderValidationError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"error_code": exc.error_code, "message": str(exc)},
        ) from exc
    return OrderActionResponse(
        order_id=oid,
        request_id=rid,
        status="accepted",
        message="Modify command dispatched",
    )
