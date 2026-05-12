"""Accounts REST endpoints.

Step 3.12 added ``GET /api/accounts`` — single read endpoint that
surfaces the result of ``RedisService.get_all_accounts_with_status()``
over HTTP. The operator UI uses this for an initial REST load on
AccountStatusBar mount; the ``account_status_loop`` keeps the bar
fresh via WS broadcasts after that.

Step 3.13 adds ``PATCH /api/accounts/{broker}/{account_id}`` — toggle
the ``enabled`` flag. The frontend Settings modal uses this to pause
/ resume an account without removing it. Other meta fields (``name``,
``created_at``) stay read-only here — Phase 5 will add full create /
delete (FTMO needs an OAuth flow first).

Authentication mirrors the rest of Phase 3 (``Bearer`` JWT via
``get_current_user_rest``).
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies.auth import get_current_user_rest
from app.services.redis_service import RedisService, get_redis_service

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountStatusEntry(BaseModel):
    """One account row in the status snapshot.

    ``balance_raw`` and friends are ``money_digits``-scaled int strings
    straight from Redis (D-108). The frontend divides by
    ``10**money_digits`` at the render boundary; we deliberately do
    NOT pre-divide here so the WS-broadcast payload and the REST
    response share an identical shape.
    """

    broker: Literal["ftmo", "exness"]
    account_id: str
    name: str
    enabled: bool
    status: Literal["online", "offline", "disabled"]
    balance_raw: str
    equity_raw: str
    margin_raw: str
    free_margin_raw: str
    currency: str
    money_digits: str


class AccountListResponse(BaseModel):
    accounts: list[AccountStatusEntry]
    total: int


class AccountUpdateRequest(BaseModel):
    """Step 3.13 PATCH body. Only ``enabled`` is mutable from the UI;
    name / created_at are read-only Phase 5 territory."""

    enabled: bool


def _row_to_entry(row: dict[str, str]) -> AccountStatusEntry:
    """Map a ``get_all_accounts_with_status`` row dict → AccountStatusEntry.

    Pulled into a helper so both ``list_accounts`` and ``update_account``
    use the same status / broker narrowing — Literal-typed pydantic
    fields require ``cast`` rather than a string assertion.
    """
    broker = cast(Literal["ftmo", "exness"], row["broker"])
    raw_status = row["status"]
    status: Literal["online", "offline", "disabled"]
    if raw_status == "online":
        status = "online"
    elif raw_status == "offline":
        status = "offline"
    else:
        status = "disabled"
    return AccountStatusEntry(
        broker=broker,
        account_id=row["account_id"],
        name=row["name"],
        enabled=row["enabled"] == "true",
        status=status,
        balance_raw=row["balance_raw"],
        equity_raw=row["equity_raw"],
        margin_raw=row["margin_raw"],
        free_margin_raw=row["free_margin_raw"],
        currency=row["currency"],
        money_digits=row["money_digits"],
    )


@router.get(
    "",
    response_model=AccountListResponse,
    summary="List every configured account with heartbeat status + balance/equity",
)
async def list_accounts(
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> AccountListResponse:
    rows = await redis_svc.get_all_accounts_with_status()
    accounts = [_row_to_entry(row) for row in rows]
    return AccountListResponse(accounts=accounts, total=len(accounts))


@router.patch(
    "/{broker}/{account_id}",
    response_model=AccountStatusEntry,
    summary="Toggle the enabled flag for an account",
)
async def update_account(
    broker: Literal["ftmo", "exness"],
    account_id: str,
    req: AccountUpdateRequest,
    _user: Annotated[str, Depends(get_current_user_rest)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> AccountStatusEntry:
    """Patch the ``enabled`` flag on an existing account_meta HASH.

    Returns the same shape as one entry from ``GET /api/accounts``, so
    the frontend can splice it into its cached list without a follow-up
    list fetch (though the WS ``account_status_loop`` broadcast will
    overwrite within 5 s anyway).
    """
    meta = await redis_svc.get_account_meta(broker, account_id)
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "account_not_found",
                "message": f"{broker} account not found: {account_id}",
            },
        )

    await redis_svc.update_account_meta(
        broker, account_id, {"enabled": "true" if req.enabled else "false"}
    )

    rows = await redis_svc.get_all_accounts_with_status()
    matching = next(
        (r for r in rows if r["broker"] == broker and r["account_id"] == account_id),
        None,
    )
    if matching is None:
        # The account vanished between the update and the re-read.
        # Concurrent ``remove_account`` from another process is the only
        # plausible path; we surface 500 rather than synthesise a stale
        # response.
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "internal_error",
                "message": "Account update succeeded but entry not found on re-read",
            },
        )
    return _row_to_entry(matching)
