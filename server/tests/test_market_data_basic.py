"""Tests for the cTrader bridge surface that don't hit a real broker.

The Twisted/asyncio bridge is verified end-to-end by the manual smoke test
documented in the step report — too brittle to mock in a unit test.
"""

from __future__ import annotations

import time
from typing import Any

import fakeredis.aioredis
import httpx
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


def test_protobuf_extract_unwraps_proto_message_wrapper() -> None:
    """Regression for step 2.1b: cTrader returns a ProtoMessage wrapper that
    has only payloadType / payload / clientMsgId. Callers want the inner
    message (e.g. ProtoOASymbolsListRes with its `symbol` field), so
    _send_and_wait now passes responses through Protobuf.extract.

    This test proves the library contract the fix depends on: a wrapper has
    no `.symbol` attribute; the extracted inner message does.
    """
    from ctrader_open_api import Protobuf
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASymbolsListRes

    inner = ProtoOASymbolsListRes()
    inner.ctidTraderAccountId = 42
    sym = inner.symbol.add()
    sym.symbolId = 1
    sym.symbolName = "EURUSD"
    sym.enabled = True

    wrapper = ProtoMessage()
    wrapper.payloadType = inner.payloadType
    wrapper.payload = inner.SerializeToString()
    assert not hasattr(wrapper, "symbol")  # the bug-reproducing assertion

    extracted = Protobuf.extract(wrapper)
    assert type(extracted).__name__ == "ProtoOASymbolsListRes"
    assert extracted.symbol[0].symbolName == "EURUSD"
    assert extracted.symbol[0].enabled is True


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
async def test_callback_rejects_invalid_code(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cTrader's token endpoint rejects the code, the callback returns 502."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid_grant")

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.api.auth_ctrader.httpx.AsyncClient", patched_async_client)

    resp = await client.get(
        "/api/auth/ctrader/callback",
        params={"code": "deadbeef"},
        follow_redirects=False,
    )
    assert resp.status_code == 502
    assert "token exchange failed" in resp.json()["detail"].lower()
