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

    # ----- OHLC cache -----

    async def get_ohlc_cache(self, key: str) -> str | None:
        """Return the cached OHLC JSON string for ``key`` or None if missing/expired."""
        value = await self._redis.get(f"ohlc:{key}")
        if value is None:
            return None
        return str(value)

    async def set_ohlc_cache(self, key: str, json_str: str, ttl_seconds: int = 60) -> None:
        """Cache an OHLC JSON payload under ``ohlc:{key}`` with a TTL."""
        await self._redis.setex(f"ohlc:{key}", ttl_seconds, json_str)

    # ----- Tick cache (latest bid/ask per symbol) -----

    async def set_tick_cache(self, ftmo_symbol: str, json_str: str, ttl_seconds: int = 60) -> None:
        """Cache the latest tick under ``tick:{ftmo_symbol}`` with a TTL.

        Used by Phase 2.4 conversion-rate calc and Phase 3 P&L snapshots.
        """
        await self._redis.setex(f"tick:{ftmo_symbol}", ttl_seconds, json_str)

    async def get_tick_cache(self, ftmo_symbol: str) -> str | None:
        """Return the cached tick JSON string, or None if missing/expired."""
        value = await self._redis.get(f"tick:{ftmo_symbol}")
        if value is None:
            return None
        return str(value)

    # ----- Pairs CRUD -----

    async def create_pair(self, pair_id: str, fields: dict[str, Any]) -> None:
        """Atomically create a pair: HSET pair:{id} + SADD pairs:all."""
        pipe = self._redis.pipeline()
        pipe.hset(f"pair:{pair_id}", mapping={k: str(v) for k, v in fields.items()})
        pipe.sadd("pairs:all", pair_id)
        await pipe.execute()

    async def get_pair(self, pair_id: str) -> dict[str, str] | None:
        """Return the pair hash by id, or None if not present."""
        data = await self._redis.hgetall(f"pair:{pair_id}")  # type: ignore[misc]
        if not data:
            return None
        return dict(data)

    async def list_pairs(self) -> list[dict[str, str]]:
        """Return all pairs sorted by ``created_at`` desc (newest first)."""
        ids = await self._redis.smembers("pairs:all")  # type: ignore[misc]
        if not ids:
            return []
        out: list[dict[str, str]] = []
        for pid in ids:
            data = await self._redis.hgetall(f"pair:{pid}")  # type: ignore[misc]
            if data:
                out.append(dict(data))
        out.sort(key=lambda p: int(p.get("created_at", "0")), reverse=True)
        return out

    async def update_pair(self, pair_id: str, fields: dict[str, Any]) -> bool:
        """Patch a pair's fields in place. Return False if the pair doesn't exist."""
        exists = await self._redis.sismember("pairs:all", pair_id)  # type: ignore[misc]
        if not exists:
            return False
        await self._redis.hset(  # type: ignore[misc]
            f"pair:{pair_id}", mapping={k: str(v) for k, v in fields.items()}
        )
        return True

    async def delete_pair(self, pair_id: str) -> bool:
        """Atomically delete a pair. Return False if it didn't exist."""
        exists = await self._redis.sismember("pairs:all", pair_id)  # type: ignore[misc]
        if not exists:
            return False
        pipe = self._redis.pipeline()
        pipe.delete(f"pair:{pair_id}")
        pipe.srem("pairs:all", pair_id)
        await pipe.execute()
        return True


def get_redis_service() -> RedisService:
    """FastAPI dependency: build a service over the shared Redis pool."""
    return RedisService(get_redis())
