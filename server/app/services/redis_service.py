"""Redis access layer.

Phase 2.1 introduces this module with the minimum surface required to support
cTrader OAuth credential storage, OAuth CSRF state, and symbol-config caching.
Future phases will extend it (orders, accounts, pairs, etc.) per docs/07-server-services.md.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as redis_asyncio

from app.redis_client import get_redis


class RedisService:
    """Thin async wrapper that owns a single Redis pool reference."""

    def __init__(self, redis: redis_asyncio.Redis) -> None:
        self._redis = redis

    # ----- cTrader market-data credentials -----

    async def set_ctrader_market_data_creds(
        self,
        access_token: str,
        refresh_token: str,
        account_id: int,
        expires_at: int,
    ) -> None:
        """Persist OAuth tokens for the cTrader market-data account.

        `expires_at` is a unix timestamp in seconds.
        """
        await self._redis.hset(  # type: ignore[misc]
            "ctrader:market_data_creds",
            mapping={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": str(account_id),
                "expires_at": str(expires_at),
            },
        )

    async def get_ctrader_market_data_creds(self) -> dict[str, Any] | None:
        """Return stored OAuth credentials or None if not present."""
        data = await self._redis.hgetall("ctrader:market_data_creds")  # type: ignore[misc]
        if not data:
            return None
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "account_id": int(data["account_id"]),
            "expires_at": int(data["expires_at"]),
        }

    # ----- Symbol config / active symbols -----

    async def set_symbol_config(self, ftmo_symbol: str, config: dict[str, Any]) -> None:
        """Persist a synced symbol's broker-side details under its FTMO key."""
        await self._redis.hset(  # type: ignore[misc]
            f"symbol_config:{ftmo_symbol}",
            mapping={k: str(v) for k, v in config.items()},
        )

    async def get_symbol_config(self, ftmo_symbol: str) -> dict[str, str] | None:
        data = await self._redis.hgetall(f"symbol_config:{ftmo_symbol}")  # type: ignore[misc]
        if not data:
            return None
        return dict(data)

    async def add_active_symbol(self, ftmo_symbol: str) -> None:
        await self._redis.sadd("symbols:active", ftmo_symbol)  # type: ignore[misc]

    async def get_active_symbols(self) -> list[str]:
        members = await self._redis.smembers("symbols:active")  # type: ignore[misc]
        return sorted(members)

    async def clear_active_symbols(self) -> None:
        """Drop the active-symbols set so a re-sync rebuilds it cleanly."""
        await self._redis.delete("symbols:active")


def get_redis_service() -> RedisService:
    """FastAPI dependency: build a service over the shared Redis pool."""
    return RedisService(get_redis())
