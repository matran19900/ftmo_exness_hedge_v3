"""GET /api/accounts — list every configured account with status + balance.

Step 3.12 adds a single read endpoint that surfaces the result of
``RedisService.get_all_accounts_with_status()`` over HTTP. The operator
UI uses this for an initial REST load on AccountStatusBar mount; the
``account_status_loop`` keeps the bar fresh via WS broadcasts after
that.

No write/mutation endpoints here — account create / edit / delete are
Phase 5. Authentication mirrors the rest of Phase 3 (``Bearer`` JWT
via ``get_current_user_rest``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
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
    accounts = [
        AccountStatusEntry(
            broker="ftmo" if row["broker"] == "ftmo" else "exness",
            account_id=row["account_id"],
            name=row["name"],
            enabled=row["enabled"] == "true",
            status=(
                "online"
                if row["status"] == "online"
                else "offline"
                if row["status"] == "offline"
                else "disabled"
            ),
            balance_raw=row["balance_raw"],
            equity_raw=row["equity_raw"],
            margin_raw=row["margin_raw"],
            free_margin_raw=row["free_margin_raw"],
            currency=row["currency"],
            money_digits=row["money_digits"],
        )
        for row in rows
    ]
    return AccountListResponse(accounts=accounts, total=len(accounts))
