"""Symbol-mapping wizard API (Phase 4.A.4).

Seven JWT-protected endpoints that drive the per-Exness-account symbol-
mapping wizard. The wizard's lifecycle (raw snapshot → auto-match → save
→ activate) is owned by ``MappingCacheService``; the routes are thin
adapters that translate HTTP shape ↔ service dataclasses.

Schemas live inline per the Phase 2 convention (see
``app/api/orders.py`` docstring).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.dependencies.auth import get_current_user_rest
from app.dependencies.mapping_cache_service import get_mapping_cache_service
from app.services.mapping_cache_service import (
    MappingCacheService,
    MappingDecision,
)
from app.services.redis_service import RedisService, get_redis_service

router = APIRouter(tags=["symbol-mapping"])


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class MappingDecisionRequest(BaseModel):
    """One CEO decision per FTMO symbol — see ``MappingCacheService.MappingDecision``."""

    model_config = ConfigDict(extra="forbid")

    ftmo: str = Field(..., min_length=1)
    action: Literal["accept", "override", "skip"]
    exness_override: str | None = None


class SaveMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[MappingDecisionRequest]


class EditMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[MappingDecisionRequest]


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class RawSymbolResponse(BaseModel):
    name: str
    contract_size: float
    digits: int
    pip_size: float
    volume_min: float
    volume_step: float
    volume_max: float
    currency_profit: str


class RawSymbolsResponse(BaseModel):
    account_id: str
    symbols: list[RawSymbolResponse]


class MappingStatusResponse(BaseModel):
    account_id: str
    status: Literal["pending_mapping", "active", "spec_mismatch", "disconnected"]
    signature: str | None = None
    cache_filename: str | None = None


class MatchProposalResponse(BaseModel):
    ftmo: str
    exness: str
    match_type: Literal["exact", "suffix_strip", "manual_hint"]
    confidence: Literal["high", "medium", "low"]


class AutoMatchResponse(BaseModel):
    account_id: str
    signature: str
    proposals: list[MatchProposalResponse]
    unmapped_ftmo: list[str]
    unmapped_exness: list[str]
    fuzzy_match_source: str | None = None
    fuzzy_match_score: float | None = None


class SaveMappingResponse(BaseModel):
    signature: str
    cache_filename: str
    created_new_cache: bool
    mapping_count: int


class SpecDivergenceResponse(BaseModel):
    symbol: str
    field: str
    cached_value: float | int | str
    raw_value: float | int | str
    severity: Literal["BLOCK", "WARN"]
    delta_percent: float | None = None


class SpecDivergenceErrorResponse(BaseModel):
    detail: str
    divergences: list[SpecDivergenceResponse]


class CacheListEntryResponse(BaseModel):
    signature: str
    filename: str
    created_at: str
    used_by_accounts: list[str]
    mapping_count: int


class CacheListResponse(BaseModel):
    caches: list[CacheListEntryResponse]


class ResyncResponse(BaseModel):
    status: Literal["resync_requested"]
    account_id: str
    request_id: str


# ---------------------------------------------------------------------------
# Endpoint 1 — GET raw-symbols
# ---------------------------------------------------------------------------


@router.get(
    "/api/accounts/exness/{account_id}/raw-symbols",
    response_model=RawSymbolsResponse,
    summary="Read the latest broker raw-symbols snapshot for an Exness account",
)
async def get_raw_symbols(
    account_id: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    svc: Annotated[MappingCacheService, Depends(get_mapping_cache_service)],
) -> RawSymbolsResponse:
    snapshot = await svc.get_raw_snapshot(account_id)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no raw-symbols snapshot for account {account_id} "
                "(account never connected, or already saved)"
            ),
        )
    return RawSymbolsResponse(
        account_id=account_id,
        symbols=[
            RawSymbolResponse(
                name=s.name,
                contract_size=s.contract_size,
                digits=s.digits,
                pip_size=s.pip_size,
                volume_min=s.volume_min,
                volume_step=s.volume_step,
                volume_max=s.volume_max,
                currency_profit=s.currency_profit,
            )
            for s in snapshot
        ],
    )


# ---------------------------------------------------------------------------
# Endpoint 2 — GET mapping-status
# ---------------------------------------------------------------------------


@router.get(
    "/api/accounts/exness/{account_id}/mapping-status",
    response_model=MappingStatusResponse,
    summary="Read current mapping wizard status for an Exness account",
)
async def get_mapping_status(
    account_id: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    svc: Annotated[MappingCacheService, Depends(get_mapping_cache_service)],
) -> MappingStatusResponse:
    snap = await svc.get_mapping_status(account_id)
    return MappingStatusResponse(
        account_id=snap.account_id,
        status=snap.status,
        signature=snap.signature,
        cache_filename=snap.cache_filename,
    )


# ---------------------------------------------------------------------------
# Endpoint 3 — POST auto-match
# ---------------------------------------------------------------------------


@router.post(
    "/api/accounts/exness/{account_id}/symbol-mapping/auto-match",
    response_model=AutoMatchResponse,
    summary="Run auto-match (or surface fuzzy / hit) for an Exness account",
)
async def post_auto_match(
    account_id: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    svc: Annotated[MappingCacheService, Depends(get_mapping_cache_service)],
) -> AutoMatchResponse:
    raw = await svc.get_raw_snapshot(account_id)
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no raw-symbols snapshot for account {account_id}; "
                "run /symbols/resync first or wait for client publish"
            ),
        )

    lookup = await svc.lookup_signature(raw)

    if lookup.outcome == "hit" and lookup.matched_cache is not None:
        cache = lookup.matched_cache
        proposals = [
            MatchProposalResponse(
                ftmo=m.ftmo,
                exness=m.exness,
                match_type=_match_type_for_response(m.match_type),
                confidence="high",
            )
            for m in cache.mappings
        ]
        return AutoMatchResponse(
            account_id=account_id,
            signature=lookup.signature,
            proposals=proposals,
            unmapped_ftmo=[],
            unmapped_exness=_unmapped_exness(raw, [m.exness for m in cache.mappings]),
            fuzzy_match_source=f"{cache.created_by_account}_{cache.signature}.json",
            fuzzy_match_score=1.0,
        )

    if lookup.outcome == "fuzzy_match" and lookup.fuzzy_candidate is not None:
        cache = lookup.fuzzy_candidate
        cached_proposals = [
            MatchProposalResponse(
                ftmo=m.ftmo,
                exness=m.exness,
                match_type=_match_type_for_response(m.match_type),
                confidence="medium",
            )
            for m in cache.mappings
        ]
        # For symbols not present in the cached mapping, run engine for those.
        cached_ftmo = {m.ftmo for m in cache.mappings}
        engine_result = svc.run_auto_match(raw)
        extra_proposals = [
            MatchProposalResponse(
                ftmo=p.ftmo,
                exness=p.exness,
                match_type=p.match_type,
                confidence=p.confidence,
            )
            for p in engine_result.proposals
            if p.ftmo not in cached_ftmo
        ]
        unmapped_ftmo = [
            f for f in engine_result.unmapped_ftmo if f not in cached_ftmo
        ]
        used_exness = {p.exness for p in cached_proposals + extra_proposals}
        return AutoMatchResponse(
            account_id=account_id,
            signature=lookup.signature,
            proposals=cached_proposals + extra_proposals,
            unmapped_ftmo=unmapped_ftmo,
            unmapped_exness=sorted({s.name for s in raw} - used_exness),
            fuzzy_match_source=f"{cache.created_by_account}_{cache.signature}.json",
            fuzzy_match_score=lookup.fuzzy_score,
        )

    # Miss — pure engine output.
    engine_result = svc.run_auto_match(raw)
    return AutoMatchResponse(
        account_id=account_id,
        signature=lookup.signature,
        proposals=[
            MatchProposalResponse(
                ftmo=p.ftmo,
                exness=p.exness,
                match_type=p.match_type,
                confidence=p.confidence,
            )
            for p in engine_result.proposals
        ],
        unmapped_ftmo=engine_result.unmapped_ftmo,
        unmapped_exness=engine_result.unmapped_exness,
        fuzzy_match_source=None,
        fuzzy_match_score=None,
    )


def _unmapped_exness(
    raw: list[Any], used: list[str]
) -> list[str]:
    used_set = set(used)
    return sorted({s.name for s in raw} - used_set)


def _match_type_for_response(value: str) -> Literal[
    "exact", "suffix_strip", "manual_hint"
]:
    """``MappingEntry.match_type`` may be ``override`` from a save, but the
    response schema only enumerates engine-produced labels. Coerce
    ``override`` (and any other non-engine value) to ``manual_hint`` so the
    UI still has a reasonable confidence-coloured chip to show."""
    if value in ("exact", "suffix_strip", "manual_hint"):
        return value  # type: ignore[return-value]
    return "manual_hint"


# ---------------------------------------------------------------------------
# Endpoint 4 — POST save
# ---------------------------------------------------------------------------


@router.post(
    "/api/accounts/exness/{account_id}/symbol-mapping/save",
    summary="Save (or link) the confirmed mapping for an Exness account",
    responses={
        201: {"model": SaveMappingResponse, "description": "New cache created"},
        200: {"model": SaveMappingResponse, "description": "Linked to existing cache"},
        400: {
            "model": SpecDivergenceErrorResponse,
            "description": "Spec divergence BLOCK — re-create mapping required",
        },
    },
)
async def post_save_mapping(
    account_id: str,
    body: SaveMappingRequest,
    response: Response,
    _user: Annotated[str, Depends(get_current_user_rest)],
    svc: Annotated[MappingCacheService, Depends(get_mapping_cache_service)],
) -> SaveMappingResponse:
    raw = await svc.get_raw_snapshot(account_id)
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail=f"no raw-symbols snapshot for account {account_id}",
        )

    # Pre-flight: signature hit + spec drift → BLOCK.
    lookup = await svc.lookup_signature(raw)
    if lookup.outcome == "hit" and lookup.matched_cache is not None:
        divergences = svc.validate_spec_divergence(raw, lookup.matched_cache)
        blocking = [d for d in divergences if d.severity == "BLOCK"]
        if blocking:
            raise HTTPException(
                status_code=400,
                detail={
                    "detail": "spec_divergence_block",
                    "divergences": [
                        SpecDivergenceResponse(
                            symbol=d.symbol,
                            field=d.field,
                            cached_value=d.cached_value,
                            raw_value=d.raw_value,
                            severity=d.severity,
                            delta_percent=d.delta_percent,
                        ).model_dump()
                        for d in divergences
                    ],
                },
            )

    decisions = [_decision_from_request(d) for d in body.decisions]
    result = await svc.save_mapping(account_id, raw, decisions)
    response.status_code = (
        status.HTTP_201_CREATED if result.created_new_cache else status.HTTP_200_OK
    )
    return SaveMappingResponse(
        signature=result.signature,
        cache_filename=result.cache_filename,
        created_new_cache=result.created_new_cache,
        mapping_count=result.mapping_count,
    )


# ---------------------------------------------------------------------------
# Endpoint 5 — PATCH edit
# ---------------------------------------------------------------------------


@router.patch(
    "/api/accounts/exness/{account_id}/symbol-mapping/edit",
    response_model=SaveMappingResponse,
    summary="Edit an Exness account's mapping (creates a new cache file)",
)
async def patch_edit_mapping(
    account_id: str,
    body: EditMappingRequest,
    _user: Annotated[str, Depends(get_current_user_rest)],
    svc: Annotated[MappingCacheService, Depends(get_mapping_cache_service)],
) -> SaveMappingResponse:
    decisions = [_decision_from_request(d) for d in body.decisions]
    try:
        result = await svc.edit_mapping(account_id, decisions)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SaveMappingResponse(
        signature=result.signature,
        cache_filename=result.cache_filename,
        created_new_cache=result.created_new_cache,
        mapping_count=result.mapping_count,
    )


# ---------------------------------------------------------------------------
# Endpoint 6 — GET cache list
# ---------------------------------------------------------------------------


@router.get(
    "/api/symbol-mapping-cache",
    response_model=CacheListResponse,
    summary="Admin/debug — list every on-disk mapping cache file with metadata",
)
async def get_cache_list(
    _user: Annotated[str, Depends(get_current_user_rest)],
    svc: Annotated[MappingCacheService, Depends(get_mapping_cache_service)],
) -> CacheListResponse:
    caches = await svc._repository.list_all()  # noqa: SLF001 — admin path
    return CacheListResponse(
        caches=[
            CacheListEntryResponse(
                signature=c.signature,
                filename=f"{c.created_by_account}_{c.signature}.json",
                created_at=c.created_at.isoformat(),
                used_by_accounts=list(c.used_by_accounts),
                mapping_count=len(c.mappings),
            )
            for c in caches
        ]
    )


# ---------------------------------------------------------------------------
# Endpoint 7 — POST resync
# ---------------------------------------------------------------------------


@router.post(
    "/api/accounts/exness/{account_id}/symbols/resync",
    response_model=ResyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Push a resync_symbols command to an Exness client",
)
async def post_resync(
    account_id: str,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> ResyncResponse:
    request_id = await redis_svc.push_command(
        broker="exness",
        account_id=account_id,
        fields={"action": "resync_symbols"},
    )
    return ResyncResponse(
        status="resync_requested",
        account_id=account_id,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _decision_from_request(d: MappingDecisionRequest) -> MappingDecision:
    return MappingDecision(
        ftmo=d.ftmo,
        action=d.action,
        exness_override=d.exness_override,
    )
