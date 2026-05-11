"""Tests for the cTrader bridge surface that don't hit a real broker.

The Twisted/asyncio bridge is verified end-to-end by the manual smoke test
documented in the step report — too brittle to mock in a unit test.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from app.services.market_data import MarketDataService
from app.services.redis_service import RedisService
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
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
async def test_get_trendbars_uses_fixed_scale_not_digits(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Regression for step 2.2a: cTrader sends raw trendbar prices uniformly
    scaled by 10^5, regardless of the symbol's display digits.

    Before the fix, USDJPY (digits=3 in symbol_config) was divided by 10^3,
    yielding 100x-too-large prices. The fix ignores `digits` and always
    divides by 100000.0. This test proves the bug shape: with the buggy
    `digits`-driven scale, the assertion at the bottom would fail for any
    non-5-digit symbol.
    """
    from ctrader_open_api import Protobuf
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTrendbarsRes

    # Seed Redis with a USDJPY symbol_config — digits=3 (the trap).
    svc = RedisService(fake_redis)
    await svc.set_symbol_config(
        "USDJPY",
        {"ctrader_symbol_id": 4, "digits": 3},
    )

    # Build a fake cTrader response with one trendbar whose `low` is the raw
    # integer 15690000 — i.e. price 156.90 at the correct 10^5 scale.
    resp = ProtoOAGetTrendbarsRes()
    resp.ctidTraderAccountId = 42
    resp.symbolId = 4
    resp.period = 7  # M15
    resp.timestamp = 0  # required field
    tb = resp.trendbar.add()
    tb.utcTimestampInMinutes = 28_500_000  # arbitrary, ~2024-ish
    tb.low = 15_690_000
    tb.deltaOpen = 0
    tb.deltaHigh = 10
    tb.deltaClose = 5
    tb.volume = 1234

    wrapper = ProtoMessage()
    wrapper.payloadType = resp.payloadType
    wrapper.payload = resp.SerializeToString()
    extracted = Protobuf.extract(wrapper)

    md = _make_service()
    md._authenticated.set()  # bypass auth gate
    md._account_id = 42

    async def fake_send_and_wait(message: Any, timeout: float = 30.0) -> Any:
        return extracted

    md._send_and_wait = fake_send_and_wait  # type: ignore[method-assign]

    candles = await md.get_trendbars("USDJPY", "M15", 1, svc)
    assert len(candles) == 1
    candle = candles[0]
    # Correct: 15_690_000 / 10^5 = 156.90. Buggy 10**digits=10^3 would have
    # produced 15690.0 — assert against the correct value.
    assert candle["low"] == pytest.approx(156.9, rel=1e-6)
    assert candle["close"] == pytest.approx(156.90005, rel=1e-6)
    assert candle["volume"] == 1234


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
async def test_sync_symbols_batches_detail_request(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Step 2.7b: detail fetch is a SINGLE batch request, not per-symbol.

    Before this fix, sync_symbols issued one ProtoOASymbolByIdReq per matched
    symbol (~91 round-trips, ~60-90s). Now it issues one batch request
    carrying all matched ids (~1-3s). This test pins that contract:

      - exactly 2 _send_and_wait calls (list + batch detail),
      - the second call's request has all three matched ids in `symbolId`.
    """
    svc = RedisService(fake_redis)

    # 1) List response: three FX symbols, all enabled.
    list_resp = ProtoOASymbolsListRes()
    list_resp.ctidTraderAccountId = 42
    for sym_id, name in [(1, "EURUSD"), (4, "USDJPY"), (2, "GBPUSD")]:
        s = list_resp.symbol.add()
        s.symbolId = sym_id
        s.symbolName = name
        s.enabled = True

    # 2) Batch detail response: details for all three, returned in a different
    # order than the list to prove the endpoint indexes by symbolId.
    detail_resp = ProtoOASymbolByIdRes()
    detail_resp.ctidTraderAccountId = 42
    for sym_id, digits, pip_pos in [(4, 3, 1), (1, 5, 4), (2, 5, 4)]:
        d = detail_resp.symbol.add()
        d.symbolId = sym_id
        d.digits = digits
        d.pipPosition = pip_pos
        d.minVolume = 1000
        d.maxVolume = 100_000_000
        d.stepVolume = 1000
        d.lotSize = 100_000

    md = _make_service()
    md._authenticated.set()
    md._account_id = 42
    md._send_and_wait = AsyncMock(side_effect=[list_resp, detail_resp])  # type: ignore[method-assign]

    cached = await md.sync_symbols(svc)

    assert cached == 3
    assert md._send_and_wait.await_count == 2

    first_req = md._send_and_wait.await_args_list[0].args[0]
    second_req = md._send_and_wait.await_args_list[1].args[0]
    assert isinstance(first_req, ProtoOASymbolsListReq)
    assert isinstance(second_req, ProtoOASymbolByIdReq)
    # Repeated symbolId field carries every matched broker id; order tracks
    # the whitelist iteration order, which we don't pin in this test — just
    # assert membership so the test stays robust to whitelist reshuffles.
    assert sorted(second_req.symbolId) == [1, 2, 4]

    # Configs were written and active set populated for all three.
    assert sorted(await svc.get_active_symbols()) == ["EURUSD", "GBPUSD", "USDJPY"]
    eurusd_cfg = await svc.get_symbol_config("EURUSD")
    assert eurusd_cfg is not None
    assert eurusd_cfg["digits"] == "5"
    usdjpy_cfg = await svc.get_symbol_config("USDJPY")
    assert usdjpy_cfg is not None
    # Detail-by-id indexing: USDJPY (id=4) gets digits=3 even though the broker
    # returned it first in the response — proves we don't rely on response order.
    assert usdjpy_cfg["digits"] == "3"


@pytest.mark.asyncio
async def test_sync_symbols_skips_symbols_missing_from_batch_response(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Fail-soft: when the broker omits a symbol from the batch detail
    response, sync_symbols logs a warning and caches the rest."""
    svc = RedisService(fake_redis)

    list_resp = ProtoOASymbolsListRes()
    list_resp.ctidTraderAccountId = 42
    for sym_id, name in [(1, "EURUSD"), (4, "USDJPY")]:
        s = list_resp.symbol.add()
        s.symbolId = sym_id
        s.symbolName = name
        s.enabled = True

    # Broker only returns details for EURUSD (id=1); USDJPY (id=4) is missing.
    detail_resp = ProtoOASymbolByIdRes()
    detail_resp.ctidTraderAccountId = 42
    d = detail_resp.symbol.add()
    d.symbolId = 1
    d.digits = 5
    d.pipPosition = 4
    d.minVolume = 1000
    d.maxVolume = 100_000_000
    d.stepVolume = 1000
    d.lotSize = 100_000

    md = _make_service()
    md._authenticated.set()
    md._account_id = 42
    md._send_and_wait = AsyncMock(side_effect=[list_resp, detail_resp])  # type: ignore[method-assign]

    cached = await md.sync_symbols(svc)

    assert cached == 1
    assert await svc.get_active_symbols() == ["EURUSD"]
    assert await svc.get_symbol_config("USDJPY") is None


@pytest.mark.asyncio
async def test_sync_symbols_returns_zero_when_batch_fails(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """If the batch detail RPC raises, sync_symbols returns 0 with no caches."""
    svc = RedisService(fake_redis)

    list_resp = ProtoOASymbolsListRes()
    list_resp.ctidTraderAccountId = 42
    s = list_resp.symbol.add()
    s.symbolId = 1
    s.symbolName = "EURUSD"
    s.enabled = True

    md = _make_service()
    md._authenticated.set()
    md._account_id = 42
    md._send_and_wait = AsyncMock(side_effect=[list_resp, RuntimeError("timeout")])  # type: ignore[method-assign]

    cached = await md.sync_symbols(svc)

    assert cached == 0
    assert await svc.get_active_symbols() == []


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

    # Step 3.3: token exchange logic moved to hedger_shared.ctrader_oauth, so
    # the httpx.AsyncClient patch must target that module instead.
    monkeypatch.setattr("hedger_shared.ctrader_oauth.httpx.AsyncClient", patched_async_client)

    resp = await client.get(
        "/api/auth/ctrader/callback",
        params={"code": "deadbeef"},
        follow_redirects=False,
    )
    assert resp.status_code == 502
    assert "token exchange failed" in resp.json()["detail"].lower()
