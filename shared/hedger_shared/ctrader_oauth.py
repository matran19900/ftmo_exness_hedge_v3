"""cTrader Open API OAuth helpers.

Extracted from ``server/app/api/auth_ctrader.py`` in step 3.3 so the
server (market-data account) and the FTMO trading client (per-account
trading tokens) can share the URL/exchange/refresh logic. The server
keeps the FastAPI routing + Redis storage glue; the FTMO client uses
these helpers from a standalone CLI (``run_oauth_flow.py``) and from
the in-process token-refresh path.

cTrader OAuth quirk: the consent callback does NOT echo the ``state``
parameter back, so we don't send one. CSRF risk is accepted for this
single-admin tool — see D-031 in ``docs/DECISIONS.md``.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, TypedDict

import httpx

# Endpoint constants. Sourced from ``ctrader_open_api.endpoints.EndPoints``;
# duplicated here so this module has no runtime dependency on the cTrader
# Twisted client.
CTRADER_AUTHORIZE_URL = "https://openapi.ctrader.com/apps/auth"
CTRADER_TOKEN_URL = "https://openapi.ctrader.com/apps/token"
# Trading accounts associated with an access_token. cTrader serves this
# over GET with the token as a query parameter.
CTRADER_TRADING_ACCOUNTS_URL = "https://api.spotware.com/connect/tradingaccounts"

# httpx default timeout for OAuth + accounts calls. 30s is generous —
# token endpoint typically replies in <500ms, but we'd rather wait than
# silently retry on a transient slow path.
_DEFAULT_TIMEOUT_SECONDS = 30.0


class TokenResponse(TypedDict):
    """Shape returned by ``exchange_code_for_token`` / ``refresh_token``.

    Field names mirror cTrader's snake_cased internal names rather than
    the camelCase keys in the raw JSON payload — callers should treat
    the wire format as private to this module.
    """

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str


class TradingAccount(TypedDict, total=False):
    """One entry from ``/connect/tradingaccounts``.

    Marked ``total=False`` because the cTrader payload has shifted field
    names over the years (``accountId`` vs ``ctidTraderAccountId``,
    ``live`` vs ``isLive``); callers normalize per-account before use.
    """

    accountId: int
    ctidTraderAccountId: int
    live: bool
    isLive: bool
    accountNumber: str
    brokerName: str


def build_authorization_url(client_id: str, redirect_uri: str, scope: str = "trading") -> str:
    """Compose the cTrader consent-page URL for a given client + redirect_uri.

    No ``state`` parameter — see the module docstring (D-031). ``scope``
    defaults to ``trading`` which covers all of submit/close/modify and
    market-data subscriptions; pass a narrower scope explicitly if a
    future caller wants read-only.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
    }
    return f"{CTRADER_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code_for_token(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> TokenResponse:
    """Exchange an authorization code for an access/refresh token pair.

    Raises ``RuntimeError`` on a non-200 response or a payload missing
    ``accessToken``. The error message includes the raw body text so a
    CLI / FastAPI handler can surface it to the operator.
    """
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as http:
        resp = await http.get(
            CTRADER_TOKEN_URL,
            params={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"cTrader token exchange failed: {resp.text}")
    payload = resp.json()
    if "accessToken" not in payload:
        raise RuntimeError(f"cTrader token response missing accessToken: {payload}")
    return _normalize_token_payload(payload)


async def refresh_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> TokenResponse:
    """Refresh an access token using the stored refresh_token.

    cTrader's token endpoint reuses the same URL with
    ``grant_type=refresh_token``. The refresh_token returned in the
    response replaces the previous one — callers should overwrite their
    stored copy.
    """
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as http:
        resp = await http.get(
            CTRADER_TOKEN_URL,
            params={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    if resp.status_code != 200:
        raise RuntimeError(f"cTrader token refresh failed: {resp.text}")
    payload = resp.json()
    if "accessToken" not in payload:
        raise RuntimeError(f"cTrader refresh response missing accessToken: {payload}")
    return _normalize_token_payload(payload)


async def fetch_trading_accounts(access_token: str) -> list[TradingAccount]:
    """List trading accounts associated with an access token.

    Returned list preserves cTrader's ordering. Caller decides whether
    to filter for demo (live=False) or pick the first ``live=True``
    account — server's market-data flow prefers demo, FTMO client picks
    the live trading account that the operator points at via env.
    """
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as http:
        resp = await http.get(CTRADER_TRADING_ACCOUNTS_URL, params={"oauth_token": access_token})
    if resp.status_code != 200:
        raise RuntimeError(f"cTrader trading-accounts fetch failed: {resp.text}")
    payload = resp.json()
    accounts_raw: list[dict[str, Any]] = payload.get("data") or payload or []
    # cast to TradingAccount via dict copy — mypy treats TypedDict as
    # structural so this is a no-op at runtime.
    return [dict(a) for a in accounts_raw]  # type: ignore[misc]


def _normalize_token_payload(payload: dict[str, Any]) -> TokenResponse:
    """Convert cTrader's camelCase JSON keys into our snake_case TypedDict."""
    return TokenResponse(
        access_token=str(payload["accessToken"]),
        refresh_token=str(payload.get("refreshToken", "")),
        expires_in=int(payload.get("expiresIn", 0)),
        token_type=str(payload.get("tokenType", "Bearer")),
    )
