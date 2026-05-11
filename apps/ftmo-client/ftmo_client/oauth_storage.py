"""Per-account OAuth token storage in Redis.

Key namespace: ``ctrader:ftmo:{account_id}:creds`` (HASH, no TTL).
Schema mirrors the camelCase-stripped form returned by
``hedger_shared.ctrader_oauth.exchange_code_for_token`` plus a
``saved_at`` epoch-ms timestamp + the cTrader ``ctid_trader_account_id``
(numeric trading-account id used as ``ctidTraderAccountId`` in protobuf
requests).

All timestamps are epoch milliseconds stored as strings, per
``docs/06-data-models.md §10``.
"""

from __future__ import annotations

import time
from typing import TypedDict

import redis.asyncio as redis_asyncio
from hedger_shared.ctrader_oauth import TokenResponse  # type: ignore[import-not-found]


class TokenData(TypedDict):
    """Hash shape of ``ctrader:ftmo:{acc}:creds`` after HGETALL.

    Matches the bytes-decoded view used by ``redis-py`` with
    ``decode_responses=True`` (everything is a string until the caller
    parses it).
    """

    access_token: str
    refresh_token: str
    expires_at: str  # epoch ms when the access_token expires
    saved_at: str  # epoch ms when we last wrote this hash
    ctid_trader_account_id: str
    token_type: str


def _key(account_id: str) -> str:
    return f"ctrader:ftmo:{account_id}:creds"


async def load_token(redis: redis_asyncio.Redis, account_id: str) -> TokenData | None:
    """Read the per-account OAuth hash. Returns None when missing."""
    raw = await redis.hgetall(_key(account_id))  # type: ignore[misc]
    if not raw:
        return None
    # Trust the schema we wrote in via ``save_token``; cast keeps mypy happy.
    return TokenData(
        access_token=raw.get("access_token", ""),
        refresh_token=raw.get("refresh_token", ""),
        expires_at=raw.get("expires_at", "0"),
        saved_at=raw.get("saved_at", "0"),
        ctid_trader_account_id=raw.get("ctid_trader_account_id", "0"),
        token_type=raw.get("token_type", "Bearer"),
    )


async def save_token(
    redis: redis_asyncio.Redis,
    account_id: str,
    token: TokenResponse,
    ctid_trader_account_id: int,
) -> None:
    """Write a freshly-exchanged token to Redis.

    ``token["expires_in"]`` is added to *now* to compute ``expires_at``;
    callers can later treat that as the authoritative expiry without
    re-doing the math. ``ctid_trader_account_id`` is the cTrader-side
    integer id (from ``fetch_trading_accounts``), needed by the trading
    bridge to populate ``ProtoOAAccountAuthReq.ctidTraderAccountId``.
    """
    now_ms = int(time.time() * 1000)
    expires_at_ms = now_ms + token["expires_in"] * 1000
    await redis.hset(  # type: ignore[misc]
        _key(account_id),
        mapping={
            "access_token": token["access_token"],
            "refresh_token": token["refresh_token"],
            "expires_at": str(expires_at_ms),
            "saved_at": str(now_ms),
            "ctid_trader_account_id": str(ctid_trader_account_id),
            "token_type": token["token_type"],
        },
    )


def is_token_expired(token: TokenData, skew_seconds: int = 300) -> bool:
    """True when the access token is within ``skew_seconds`` of expiry.

    Default 5-minute skew gives the FTMO client time to refresh ahead of
    the actual deadline so an in-flight request isn't rejected mid-way.
    """
    try:
        expires_at_ms = int(token["expires_at"])
    except (KeyError, ValueError):
        # Malformed entry — treat as expired so the caller re-runs OAuth.
        return True
    now_ms = int(time.time() * 1000)
    return expires_at_ms - now_ms <= skew_seconds * 1000
