"""Tests for POST /api/symbols/{ftmo_symbol}/calculate-volume.

Reuses the autouse fakeredis dependency override and real whitelist load from
conftest. The MarketDataService is replaced via app.state for the few tests
that exercise the conversion-rate subscribe-on-miss path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio
from app.main import app
from app.services.redis_service import RedisService
from httpx import AsyncClient


def _fx_symbol_config_payload() -> dict[str, Any]:
    return {
        "ctrader_symbol_id": 1,
        "digits": 5,
        "min_volume": 100000,
        "max_volume": 10000000000,
        "step_volume": 100000,
        "lot_size": 10000000,
    }


@pytest_asyncio.fixture
async def seeded_eurusd(fake_redis: fakeredis.aioredis.FakeRedis) -> RedisService:
    """Seed Redis with EURUSD symbol_config so the endpoint passes the active-set check."""
    svc = RedisService(fake_redis)
    await svc.set_symbol_config("EURUSD", _fx_symbol_config_payload())
    return svc


@pytest_asyncio.fixture
async def seeded_usdjpy(fake_redis: fakeredis.aioredis.FakeRedis) -> RedisService:
    """Seed Redis with USDJPY symbol_config + a USDJPY tick (enables JPY rate)."""
    svc = RedisService(fake_redis)
    await svc.set_symbol_config("USDJPY", _fx_symbol_config_payload())
    await svc.set_tick_cache("USDJPY", json.dumps({"bid": 156.0, "ask": 156.02, "ts": 1}))
    return svc


@pytest.fixture
def md_unauth() -> Iterator[None]:
    """No MarketDataService on app.state — exercises the rate-not-available path."""
    previous = getattr(app.state, "market_data", None)
    app.state.market_data = None
    yield
    app.state.market_data = previous


@pytest.fixture
def md_authed_with_subscribe_spy() -> Iterator[AsyncMock]:
    """An AsyncMock standing in for an authenticated MarketDataService."""
    md = AsyncMock()
    md.is_authenticated = True
    md.subscribe_spots = AsyncMock(return_value=None)
    previous = getattr(app.state, "market_data", None)
    app.state.market_data = md
    yield md
    app.state.market_data = previous


# ---------- auth + validation ----------


@pytest.mark.asyncio
async def test_endpoint_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/symbols/EURUSD/calculate-volume",
        json={"pair_id": "pair_001", "entry": 1.085, "sl": 1.080, "risk_amount": 100.0, "ratio": 1.0},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_unknown_symbol(authed_client: AsyncClient) -> None:
    resp = await authed_client.post(
        "/api/symbols/NOTREAL/calculate-volume",
        json={"pair_id": "pair_001", "entry": 1.085, "sl": 1.080, "risk_amount": 100.0, "ratio": 1.0},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Symbol not in whitelist"


@pytest.mark.asyncio
async def test_endpoint_symbol_not_in_active_set(authed_client: AsyncClient) -> None:
    """Whitelist OK but no symbol_config in Redis → 404 'Symbol not in active set'."""
    resp = await authed_client.post(
        "/api/symbols/EURUSD/calculate-volume",
        json={"pair_id": "pair_001", "entry": 1.085, "sl": 1.080, "risk_amount": 100.0, "ratio": 1.0},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Symbol not in active set"


@pytest.mark.asyncio
async def test_endpoint_invalid_request_returns_422(
    authed_client: AsyncClient, seeded_eurusd: RedisService
) -> None:
    """Pydantic catches negative entry/sl/risk_amount before the route runs."""
    resp = await authed_client.post(
        "/api/symbols/EURUSD/calculate-volume",
        json={"pair_id": "pair_001", "entry": -1, "sl": 1.080, "risk_amount": 100.0, "ratio": 1.0},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_sl_too_tight_returns_400(
    authed_client: AsyncClient, seeded_eurusd: RedisService
) -> None:
    resp = await authed_client.post(
        "/api/symbols/EURUSD/calculate-volume",
        json={"pair_id": "pair_001", "entry": 1.0850, "sl": 1.0849, "risk_amount": 100.0, "ratio": 1.0},
    )
    assert resp.status_code == 400
    assert "SL too tight" in resp.json()["detail"]


# ---------- happy path / rate handling ----------


@pytest.mark.asyncio
async def test_endpoint_eurusd_happy_path(
    authed_client: AsyncClient, seeded_eurusd: RedisService
) -> None:
    """EURUSD has quote_ccy=USD → rate=1.0, no tick lookup needed."""
    resp = await authed_client.post(
        "/api/symbols/EURUSD/calculate-volume",
        json={"pair_id": "pair_001", "entry": 1.0850, "sl": 1.0800, "risk_amount": 100.0, "ratio": 1.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "EURUSD"
    assert body["quote_ccy"] == "USD"
    assert body["quote_to_usd_rate"] == 1.0
    assert body["sl_pips"] == 50.0
    assert body["volume_primary"] == pytest.approx(0.20, abs=0.001)


@pytest.mark.asyncio
async def test_endpoint_usdjpy_uses_inverse_rate(
    authed_client: AsyncClient, seeded_usdjpy: RedisService
) -> None:
    """USDJPY quote_ccy=JPY → rate from USDJPY tick inverse."""
    resp = await authed_client.post(
        "/api/symbols/USDJPY/calculate-volume",
        json={"pair_id": "pair_001", "entry": 156.50, "sl": 156.00, "risk_amount": 100.0, "ratio": 1.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["quote_ccy"] == "JPY"
    assert body["quote_to_usd_rate"] == pytest.approx(1.0 / 156.0, rel=1e-6)
    assert body["sl_pips"] == 50.0
    assert body["volume_primary"] > 0


@pytest.mark.asyncio
async def test_endpoint_rate_unavailable_returns_503(
    authed_client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
    md_authed_with_subscribe_spy: AsyncMock,
) -> None:
    """USDJPY symbol_config set but no USDJPY tick → 503, subscribe triggered."""
    svc = RedisService(fake_redis)
    await svc.set_symbol_config("USDJPY", _fx_symbol_config_payload())
    # No tick set anywhere → conversion rate unavailable.

    resp = await authed_client.post(
        "/api/symbols/USDJPY/calculate-volume",
        json={"pair_id": "pair_001", "entry": 156.50, "sl": 156.00, "risk_amount": 100.0, "ratio": 1.0},
    )
    assert resp.status_code == 503
    assert "JPY" in resp.json()["detail"] and "USD" in resp.json()["detail"]
    md_authed_with_subscribe_spy.subscribe_spots.assert_awaited()


@pytest.mark.asyncio
async def test_endpoint_returns_full_metadata(
    authed_client: AsyncClient, seeded_eurusd: RedisService
) -> None:
    resp = await authed_client.post(
        "/api/symbols/EURUSD/calculate-volume",
        json={"pair_id": "pair_001", "entry": 1.0850, "sl": 1.0800, "risk_amount": 100.0, "ratio": 1.0},
    )
    assert resp.status_code == 200
    body = resp.json()
    for field in (
        "symbol",
        "volume_primary",
        "volume_secondary",
        "sl_pips",
        "pip_value_usd_per_lot",
        "sl_usd_per_lot",
        "quote_ccy",
        "quote_to_usd_rate",
    ):
        assert field in body
