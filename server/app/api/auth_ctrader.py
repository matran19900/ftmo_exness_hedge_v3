"""cTrader OAuth flow for the market-data account.

Endpoints are intentionally unauthenticated: the OAuth dance is a browser
redirect chain that cannot carry an Authorization header.
"""

from __future__ import annotations

import logging
import secrets
import time
import urllib.parse
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.config import Settings, get_settings
from app.services.market_data import MarketDataService
from app.services.redis_service import RedisService, get_redis_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/ctrader", tags=["ctrader-auth"])

# cTrader OAuth endpoints (per ctrader_open_api.endpoints.EndPoints).
CTRADER_AUTHORIZE_URL = "https://openapi.ctrader.com/apps/auth"
CTRADER_TOKEN_URL = "https://openapi.ctrader.com/apps/token"
# Trading accounts associated with the access_token. cTrader serves this over
# GET with the access_token as a query parameter.
CTRADER_TRADING_ACCOUNTS_URL = "https://api.spotware.com/connect/tradingaccounts"


@router.get("")
async def ctrader_login(
    settings: Annotated[Settings, Depends(get_settings)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> RedirectResponse:
    """Redirect the user to the cTrader consent page."""
    if not settings.ctrader_client_id:
        raise HTTPException(
            status_code=503, detail="cTrader client_id is not configured (CTRADER_CLIENT_ID)"
        )
    state = secrets.token_urlsafe(32)
    await redis_svc.set_oauth_state(state, ttl_seconds=600)
    params = {
        "client_id": settings.ctrader_client_id,
        "redirect_uri": settings.ctrader_redirect_uri,
        "scope": "trading",
        "state": state,
    }
    url = f"{CTRADER_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    logger.info("Redirecting to cTrader consent page (state=%s)", state[:8])
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def ctrader_callback(
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    settings: Annotated[Settings, Depends(get_settings)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
) -> RedirectResponse:
    """Exchange the OAuth code, store credentials, and trigger MarketDataService."""
    if not await redis_svc.consume_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    async with httpx.AsyncClient(timeout=30.0) as http:
        token_resp = await http.get(
            CTRADER_TOKEN_URL,
            params={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.ctrader_redirect_uri,
                "client_id": settings.ctrader_client_id,
                "client_secret": settings.ctrader_client_secret,
            },
        )
        if token_resp.status_code != 200:
            raise HTTPException(
                status_code=502, detail=f"cTrader token exchange failed: {token_resp.text}"
            )
        token_data = token_resp.json()
    if "accessToken" not in token_data:
        raise HTTPException(
            status_code=502, detail=f"cTrader token response missing accessToken: {token_data}"
        )

    access_token: str = token_data["accessToken"]
    refresh_token: str = token_data.get("refreshToken", "")
    expires_in: int = int(token_data.get("expiresIn", 0))
    expires_at: int = int(time.time()) + expires_in

    async with httpx.AsyncClient(timeout=30.0) as http:
        accounts_resp = await http.get(
            CTRADER_TRADING_ACCOUNTS_URL, params={"oauth_token": access_token}
        )
        if accounts_resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"cTrader trading-accounts fetch failed: {accounts_resp.text}",
            )
        accounts_payload = accounts_resp.json()

    accounts: list[dict[str, Any]] = accounts_payload.get("data") or accounts_payload or []
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
