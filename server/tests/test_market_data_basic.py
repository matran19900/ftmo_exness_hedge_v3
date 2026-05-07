"""Tests for the cTrader bridge surface that don't hit a real broker.

The Twisted/asyncio bridge is verified end-to-end by the manual smoke test
documented in the step report — too brittle to mock in a unit test.
"""

from __future__ import annotations

import time

import fakeredis.aioredis
import pytest
from app.services.market_data import MarketDataService
from app.services.redis_service import RedisService
from httpx import AsyncClient


def _make_service() -> MarketDataService:
    return MarketDataService(
        host="live.ctraderapi.com",
        port=5035,
        client_id="test-client-id",
        client_secret="test-client-secret",
    )


def test_market_data_initial_state_is_idle() -> None:
    md = _make_service()
    assert md.is_connected is False
    assert md.is_authenticated is False


@pytest.mark.asyncio
async def test_redis_service_oauth_state_consume_once(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(fake_redis)
    await svc.set_oauth_state("abc", ttl_seconds=600)
    assert await svc.consume_oauth_state("abc") is True
    assert await svc.consume_oauth_state("abc") is False


@pytest.mark.asyncio
async def test_redis_service_oauth_state_unknown_returns_false(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(fake_redis)
    assert await svc.consume_oauth_state("never-stored") is False


@pytest.mark.asyncio
async def test_redis_service_creds_roundtrip(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(fake_redis)
    assert await svc.get_ctrader_market_data_creds() is None
    await svc.set_ctrader_market_data_creds(
        access_token="acc-token",
        refresh_token="ref-token",
        account_id=12345,
        expires_at=1_700_000_000,
    )
    creds = await svc.get_ctrader_market_data_creds()
    assert creds is not None
    assert creds["access_token"] == "acc-token"
    assert creds["refresh_token"] == "ref-token"
    assert creds["account_id"] == 12345
    assert creds["expires_at"] == 1_700_000_000


@pytest.mark.asyncio
async def test_redis_service_active_symbols_add_and_clear(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(fake_redis)
    assert await svc.get_active_symbols() == []
    await svc.add_active_symbol("EURUSD")
    await svc.add_active_symbol("USDJPY")
    await svc.add_active_symbol("EURUSD")  # idempotent
    assert await svc.get_active_symbols() == ["EURUSD", "USDJPY"]
    await svc.clear_active_symbols()
    assert await svc.get_active_symbols() == []


@pytest.mark.asyncio
async def test_redis_service_symbol_config(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(fake_redis)
    await svc.set_symbol_config("EURUSD", {"ctrader_symbol_id": 1, "digits": 5})
    cfg = await svc.get_symbol_config("EURUSD")
    assert cfg is not None
    assert cfg["ctrader_symbol_id"] == "1"
    assert cfg["digits"] == "5"
    assert await svc.get_symbol_config("UNKNOWN") is None


@pytest.mark.asyncio
async def test_status_endpoint_no_creds(client: AsyncClient) -> None:
    resp = await client.get("/api/auth/ctrader/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"has_credentials": False, "expires_at": None, "expires_in_seconds": None}


@pytest.mark.asyncio
async def test_status_endpoint_with_creds(
    client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(fake_redis)
    expires_at = int(time.time()) + 3600
    await svc.set_ctrader_market_data_creds(
        access_token="x",
        refresh_token="y",
        account_id=42,
        expires_at=expires_at,
    )
    resp = await client.get("/api/auth/ctrader/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_credentials"] is True
    assert body["expires_at"] == expires_at * 1000
    assert 0 <= body["expires_in_seconds"] <= 3600


@pytest.mark.asyncio
async def test_login_endpoint_503_when_client_id_missing(client: AsyncClient) -> None:
    """Without CTRADER_CLIENT_ID set, the login redirect must refuse cleanly."""
    resp = await client.get("/api/auth/ctrader", follow_redirects=False)
    assert resp.status_code == 503
    assert "CTRADER_CLIENT_ID" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_callback_rejects_unknown_state(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/auth/ctrader/callback",
        params={"code": "deadbeef", "state": "never-issued"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "state" in resp.json()["detail"].lower()
