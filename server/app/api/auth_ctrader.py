"""cTrader OAuth flow for the market-data account.

Endpoints are intentionally unauthenticated: the OAuth dance is a browser
redirect chain that cannot carry an Authorization header.

Step 3.3: the URL builder, code-to-token exchange, and trading-accounts
fetch were extracted to ``hedger_shared.ctrader_oauth`` so the FTMO
client can reuse them for per-account trading tokens. This module keeps
the FastAPI routing + Redis storage + MarketDataService trigger glue.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from hedger_shared.ctrader_oauth import (  # type: ignore[import-not-found]
    build_authorization_url,
    exchange_code_for_token,
    fetch_trading_accounts,
)

from app.config import Settings, get_settings
from app.services.market_data import MarketDataService
from app.services.redis_service import RedisService, get_redis_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/ctrader", tags=["ctrader-auth"])


@router.get("")
async def ctrader_login(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    """Redirect the user to the cTrader consent page.

    Note: cTrader's OAuth callback does not echo `state` back, so we don't
    send one. CSRF risk is accepted for this single-admin tool — see D-031.
    """
    if not settings.ctrader_client_id:
        raise HTTPException(
            status_code=503, detail="cTrader client_id is not configured (CTRADER_CLIENT_ID)"
        )
    url = build_authorization_url(
        client_id=settings.ctrader_client_id,
        redirect_uri=settings.ctrader_redirect_uri,
    )
    logger.info("Redirecting to cTrader consent page")
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def ctrader_callback(
    code: Annotated[str, Query()],
    settings: Annotated[Settings, Depends(get_settings)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> RedirectResponse:
    """Exchange the OAuth code, store credentials, and trigger MarketDataService."""
    try:
        token = await exchange_code_for_token(
            client_id=settings.ctrader_client_id,
            client_secret=settings.ctrader_client_secret,
            code=code,
            redirect_uri=settings.ctrader_redirect_uri,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    access_token: str = token["access_token"]
    refresh_token: str = token["refresh_token"]
    expires_in: int = token["expires_in"]
    expires_at: int = int(time.time()) + expires_in

    try:
        # TradingAccount is a TypedDict (structurally a dict[str, Any] at
        # runtime) — cast keeps the local variable typed for the dict-style
        # ``.get()`` lookups below without a stricter generic.
        accounts: list[dict[str, Any]] = [
            dict(a) for a in await fetch_trading_accounts(access_token)
        ]
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not accounts:
        raise HTTPException(
            status_code=400, detail="No cTrader trading accounts associated with this user"
        )

    # Prefer demo account (live=False) for market-data feed.
    demo_accounts = [a for a in accounts if not a.get("live", a.get("isLive", True))]
    chosen = demo_accounts[0] if demo_accounts else accounts[0]
    raw_account_id = chosen.get("accountId") or chosen.get("ctidTraderAccountId")
    if raw_account_id is None:
        raise HTTPException(
            status_code=502,
            detail=f"cTrader account payload missing accountId field: {chosen}",
        )
    account_id = int(raw_account_id)

    await redis_svc.set_ctrader_market_data_creds(
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=account_id,
        expires_at=expires_at,
    )
    logger.info(
        "Stored cTrader credentials for account_id=%s (demo=%s)", account_id, bool(demo_accounts)
    )

    # Trigger the MarketDataService — held on app.state. Lazy-import main to
    # avoid an import cycle (main imports this router).
    from app.main import app  # noqa: PLC0415

    md: MarketDataService | None = getattr(app.state, "market_data", None)
    if md is None:
        md = MarketDataService(
            host=settings.ctrader_host,
            port=settings.ctrader_port,
            client_id=settings.ctrader_client_id,
            client_secret=settings.ctrader_client_secret,
        )
        try:
            await md.start()
        except Exception:
            logger.exception("Failed to start MarketDataService after OAuth callback")
            raise HTTPException(status_code=502, detail="MarketDataService start failed") from None
        app.state.market_data = md

    try:
        await md.authenticate(access_token, account_id)
        cached = await md.sync_symbols(redis_svc)
        logger.info("OAuth callback completed: cached %d symbols", cached)
    except Exception:
        logger.exception("MarketDataService authenticate/sync failed after OAuth callback")
        # Credentials were stored — operator can retry sync via a future endpoint.
        raise HTTPException(
            status_code=502, detail="MarketDataService authenticate/sync failed"
        ) from None

    return RedirectResponse("/", status_code=302)


@router.get("/status")
async def ctrader_status(
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> dict[str, Any]:
    creds = await redis_svc.get_ctrader_market_data_creds()
    if creds is None:
        return {"has_credentials": False, "expires_at": None, "expires_in_seconds": None}
    now = int(time.time())
    return {
        "has_credentials": True,
        "expires_at": creds["expires_at"] * 1000,  # frontend expects ms
        "expires_in_seconds": max(0, creds["expires_at"] - now),
    }
