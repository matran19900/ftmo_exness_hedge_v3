"""Async Redis pool initialization and accessor used as a FastAPI dependency."""

from __future__ import annotations

import redis.asyncio as redis_asyncio

_redis_pool: redis_asyncio.Redis | None = None


async def init_redis(url: str) -> None:
    """Create the global Redis pool and verify connectivity with a single PING."""
    global _redis_pool
    pool = redis_asyncio.from_url(url, decode_responses=True, max_connections=20)
    await pool.ping()
    _redis_pool = pool


async def close_redis() -> None:
    """Close the Redis pool gracefully if it was initialized."""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None


def get_redis() -> redis_asyncio.Redis:
    """FastAPI dependency: return the active Redis pool or raise if uninitialized."""
    if _redis_pool is None:
        raise RuntimeError("Redis pool is not initialized; call init_redis() first")
    return _redis_pool
