"""Pair CRUD endpoints.

A pair links one FTMO account, one Exness account, and a hedge ratio. The
order form's PairPicker (step 2.8) reads this list. Phase 2 simplification:
account_id existence is NOT validated here — Phase 4 will validate against
the ``accounts:ftmo`` / ``accounts:exness`` Redis sets once accounts CRUD
ships.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.dependencies.auth import get_current_user_rest
from app.dependencies.mapping_service import get_mapping_service
from app.services.mapping_service import MappingService
from app.services.redis_service import RedisService, get_redis_service

router = APIRouter(prefix="/api/pairs", tags=["pairs"])


class PairCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    ftmo_account_id: str = Field(..., min_length=1, max_length=50)
    exness_account_id: str = Field(..., min_length=1, max_length=50)
    ratio: float = Field(..., gt=0, le=10)


class PairUpdateRequest(BaseModel):
    """All fields optional; only provided fields are patched."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    ftmo_account_id: str | None = Field(default=None, min_length=1, max_length=50)
    exness_account_id: str | None = Field(default=None, min_length=1, max_length=50)
    ratio: float | None = Field(default=None, gt=0, le=10)


class PairResponse(BaseModel):
    pair_id: str
    name: str
    ftmo_account_id: str
    exness_account_id: str
    ratio: float
    created_at: int
    updated_at: int


def _redis_to_response(data: dict[str, str]) -> PairResponse:
    """Lift the all-strings Redis hash into a typed PairResponse."""
    return PairResponse(
        pair_id=data["pair_id"],
        name=data["name"],
        ftmo_account_id=data["ftmo_account_id"],
        exness_account_id=data["exness_account_id"],
        ratio=float(data["ratio"]),
        created_at=int(data["created_at"]),
        updated_at=int(data["updated_at"]),
    )


@router.get("/", response_model=list[PairResponse])
async def list_pairs(
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> list[PairResponse]:
    """Return all pairs, newest first."""
    pairs = await redis_svc.list_pairs()
    return [_redis_to_response(p) for p in pairs]


@router.post("/", response_model=PairResponse, status_code=status.HTTP_201_CREATED)
async def create_pair(
    req: PairCreateRequest,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> PairResponse:
    """Create a new pair. ``pair_id`` is generated server-side (uuid4)."""
    pair_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    fields: dict[str, Any] = {
        "pair_id": pair_id,
        "name": req.name,
        "ftmo_account_id": req.ftmo_account_id,
        "exness_account_id": req.exness_account_id,
        "ratio": req.ratio,
        "created_at": now_ms,
        "updated_at": now_ms,
    }
    await redis_svc.create_pair(pair_id, fields)
    return PairResponse(
        pair_id=pair_id,
        name=req.name,
        ftmo_account_id=req.ftmo_account_id,
        exness_account_id=req.exness_account_id,
        ratio=req.ratio,
        created_at=now_ms,
        updated_at=now_ms,
    )


@router.get("/{pair_id}", response_model=PairResponse)
async def get_pair(
    pair_id: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> PairResponse:
    data = await redis_svc.get_pair(pair_id)
    if not data:
        raise HTTPException(status_code=404, detail="Pair not found")
    return _redis_to_response(data)


@router.patch("/{pair_id}", response_model=PairResponse)
async def update_pair(
    pair_id: str,
    req: PairUpdateRequest,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> PairResponse:
    """Patch any subset of fields. ``updated_at`` is always refreshed."""
    fields: dict[str, Any] = {
        k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None
    }
    if not fields:
        raise HTTPException(status_code=400, detail="At least one field must be provided")
    fields["updated_at"] = int(time.time() * 1000)

    success = await redis_svc.update_pair(pair_id, fields)
    if not success:
        raise HTTPException(status_code=404, detail="Pair not found")

    data = await redis_svc.get_pair(pair_id)
    if not data:
        # Should not happen given the success check; defensive only.
        raise HTTPException(status_code=500, detail="Pair vanished after update")
    return _redis_to_response(data)


@router.delete("/{pair_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_pair(
    pair_id: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> Response:
    # Step 3.13: refuse delete while active orders reference this
    # pair. ``count_orders_by_pair`` scans pending + filled SETs only
    # (closed/rejected/cancelled orders are frozen references, not
    # active dependencies). The 409 detail uses the ``error_code`` +
    # ``message`` shape consumed by the frontend's ``formatOrderError``
    # helper.
    referencing = await redis_svc.count_orders_by_pair(pair_id)
    if referencing > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "pair_in_use",
                "message": (
                    f"Cannot delete pair: {referencing} order(s) reference it. Close them first."
                ),
            },
        )

    success = await redis_svc.delete_pair(pair_id)
    if not success:
        raise HTTPException(status_code=404, detail="Pair not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Phase 4.A.5 — pre-flight check for HedgeOrderForm submission
# ---------------------------------------------------------------------------


class CheckSymbolResponse(BaseModel):
    """Result of ``GET /api/pairs/{pair_id}/check-symbol/{symbol}``."""

    tradeable: bool
    reason: str | None = None


@router.get(
    "/{pair_id}/check-symbol/{symbol}",
    response_model=CheckSymbolResponse,
    summary="Pre-flight tradeability check for a (pair, FTMO symbol) combo",
)
async def check_pair_symbol(
    pair_id: str,
    symbol: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    mapping_service: Annotated[MappingService, Depends(get_mapping_service)],
) -> CheckSymbolResponse:
    """Return whether ``symbol`` can be hedged on ``pair_id``.

    Phase 3 single-leg pairs (no ``exness_account_id``) get an FTMO-only
    check. Phase 4 hedge pairs additionally require an active mapping
    cache entry — see ``MappingService.is_pair_symbol_tradeable`` for
    the full rule set."""
    tradeable, reason = await mapping_service.is_pair_symbol_tradeable(
        pair_id, symbol
    )
    return CheckSymbolResponse(tradeable=tradeable, reason=reason)
