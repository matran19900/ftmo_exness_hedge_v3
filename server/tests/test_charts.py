"""Tests for /api/charts/{symbol}/ohlc.

MarketDataService is replaced by an AsyncMock via FastAPI dependency override —
no real cTrader calls. Redis is fakeredis (autouse fixture in conftest).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from app.api.charts import get_market_data
from app.main import app
from app.services.redis_service import RedisService
from httpx import AsyncClient


def _sample_candles() -> list[dict[str, Any]]:
    return [
        {
            "time": 1735000000 + i * 900,
            "open": 1.10000 + i * 0.0001,
            "high": 1.10050 + i * 0.0001,
            "low": 1.09950 + i * 0.0001,
            "close": 1.10025 + i * 0.0001,
            "volume": 1000 + i,
        }
        for i in range(3)
    ]


@pytest.fixture
def mock_md() -> AsyncMock:
    """An AsyncMock standing in for MarketDataService — is_authenticated=True by default."""
    md = AsyncMock()
    md.is_authenticated = True
    md.get_trendbars = AsyncMock(return_value=_sample_candles())
    return md


@pytest.fixture
def override_md(mock_md: AsyncMock) -> Iterator[AsyncMock]:
    """Override the get_market_data dependency with a working mock."""
    app.dependency_overrides[get_market_data] = lambda: mock_md
    yield mock_md
    app.dependency_overrides.pop(get_market_data, None)


@pytest.mark.asyncio
async def test_ohlc_requires_auth(client: AsyncClient, override_md: AsyncMock) -> None:
    resp = await client.get("/api/charts/EURUSD/ohlc")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ohlc_invalid_timeframe(authed_client: AsyncClient, override_md: AsyncMock) -> None:
    resp = await authed_client.get("/api/charts/EURUSD/ohlc?timeframe=XYZ")
    assert resp.status_code == 400
    assert "timeframe" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_ohlc_invalid_count_zero(authed_client: AsyncClient, override_md: AsyncMock) -> None:
    resp = await authed_client.get("/api/charts/EURUSD/ohlc?count=0")
    assert resp.status_code == 422  # FastAPI Query ge=1 validator


@pytest.mark.asyncio
async def test_ohlc_invalid_count_too_large(
    authed_client: AsyncClient, override_md: AsyncMock
) -> None:
    resp = await authed_client.get("/api/charts/EURUSD/ohlc?count=1001")
    assert resp.status_code == 422  # FastAPI Query le=1000 validator


@pytest.mark.asyncio
async def test_ohlc_market_data_not_ready(authed_client: AsyncClient) -> None:
    """No override + app.state.market_data = None → get_market_data returns 503."""
    app.dependency_overrides.pop(get_market_data, None)
    app.state.market_data = None
    resp = await authed_client.get("/api/charts/EURUSD/ohlc")
    assert resp.status_code == 503
    assert "Complete cTrader OAuth" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ohlc_symbol_not_in_active_set(
    authed_client: AsyncClient, override_md: AsyncMock
) -> None:
    override_md.get_trendbars.side_effect = RuntimeError("Symbol UNKNOWN not in active set")
    resp = await authed_client.get("/api/charts/UNKNOWN/ohlc?timeframe=M15&count=20")
    assert resp.status_code == 404
    assert "Symbol not available" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ohlc_runtime_error_other_returns_502(
    authed_client: AsyncClient, override_md: AsyncMock
) -> None:
    override_md.get_trendbars.side_effect = RuntimeError("cTrader request failed: boom")
    resp = await authed_client.get("/api/charts/EURUSD/ohlc?timeframe=M15&count=10")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_ohlc_returns_correct_payload_shape(
    authed_client: AsyncClient, override_md: AsyncMock
) -> None:
    resp = await authed_client.get("/api/charts/EURUSD/ohlc?timeframe=M15&count=3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "EURUSD"
    assert body["timeframe"] == "M15"
    assert body["count"] == 3
    # No symbol_config seeded → endpoint falls back to default 5-digit precision.
    assert body["digits"] == 5
    assert len(body["candles"]) == 3
    first = body["candles"][0]
    for field in ("time", "open", "high", "low", "close", "volume"):
        assert field in first
    override_md.get_trendbars.assert_awaited_once_with("EURUSD", "M15", 3, _AnyArg())


@pytest.mark.asyncio
async def test_ohlc_digits_from_symbol_config(
    authed_client: AsyncClient,
    override_md: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """When symbol_config has digits=3 (e.g. JPY pairs), response.digits == 3."""
    svc = RedisService(fake_redis)
    await svc.set_symbol_config("USDJPY", {"ctrader_symbol_id": "42", "digits": "3"})

    resp = await authed_client.get("/api/charts/USDJPY/ohlc?timeframe=M15&count=3")
    assert resp.status_code == 200
    assert resp.json()["digits"] == 3


@pytest.mark.asyncio
async def test_ohlc_digits_default_when_config_missing_field(
    authed_client: AsyncClient,
    override_md: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """symbol_config without `digits` field → fallback to default 5."""
    svc = RedisService(fake_redis)
    await svc.set_symbol_config("EURUSD", {"ctrader_symbol_id": "1"})

    resp = await authed_client.get("/api/charts/EURUSD/ohlc?timeframe=M15&count=3")
    assert resp.status_code == 200
    assert resp.json()["digits"] == 5


@pytest.mark.asyncio
async def test_ohlc_cache_miss_then_hit(
    authed_client: AsyncClient,
    override_md: AsyncMock,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    # First call: cache miss → fetches from md.
    resp1 = await authed_client.get("/api/charts/EURUSD/ohlc?timeframe=M15&count=3")
    assert resp1.status_code == 200
    assert override_md.get_trendbars.await_count == 1

    # Verify the cache key was written.
    svc = RedisService(fake_redis)
    cached_raw = await svc.get_ohlc_cache("EURUSD:M15:3")
    assert cached_raw is not None

    # Second call: cache hit → md NOT invoked again.
    resp2 = await authed_client.get("/api/charts/EURUSD/ohlc?timeframe=M15&count=3")
    assert resp2.status_code == 200
    assert resp2.json() == resp1.json()
    assert override_md.get_trendbars.await_count == 1  # still 1, not 2


class _AnyArg:
    """Match any argument (RedisService instance varies per request)."""

    def __eq__(self, other: object) -> bool:
        return True
