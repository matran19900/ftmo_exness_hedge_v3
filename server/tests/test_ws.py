"""Tests for /ws WebSocket endpoint and BroadcastService.

WebSocket round-trips use FastAPI's synchronous TestClient (the async httpx
client doesn't support WS frames). MarketDataService is replaced with an
AsyncMock and BroadcastService is injected directly onto ``app.state`` so we
don't need the production lifespan.

End-to-end "publish reaches the WS client" is covered indirectly: the
BroadcastService's logic is tested in pure-async unit tests below, and the
WS route's subscribe/unsubscribe message handling is asserted by inspecting
BroadcastService internal counters.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from app.auth import create_access_token
from app.main import app
from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService
from fastapi.testclient import TestClient


@pytest.fixture
def jwt_token() -> str:
    """Mint a JWT signed with the same secret conftest seeded into the env."""
    secret = os.environ["JWT_SECRET"]
    return create_access_token(subject="admin", secret=secret, expires_minutes=60)


@pytest.fixture
def broadcast_svc(fake_redis: fakeredis.aioredis.FakeRedis) -> Iterator[BroadcastService]:
    """Inject a fresh BroadcastService onto app.state for the test."""
    bs = BroadcastService(redis_svc=RedisService(fake_redis))
    app.state.broadcast = bs
    yield bs
    app.state.broadcast = None


@pytest.fixture
def mock_md_on_state() -> Iterator[AsyncMock]:
    """Install an AsyncMock as app.state.market_data; reset on teardown."""
    md = AsyncMock()
    md.is_authenticated = True
    md.subscribe_spots = AsyncMock(return_value=None)
    md.subscribe_live_trendbar = AsyncMock(return_value=None)
    md.unsubscribe_spots = AsyncMock(return_value=None)
    md.unsubscribe_live_trendbar = AsyncMock(return_value=None)
    previous = getattr(app.state, "market_data", None)
    app.state.market_data = md
    yield md
    app.state.market_data = previous


@pytest.fixture
def ws_test_client(broadcast_svc: BroadcastService) -> Iterator[TestClient]:
    """TestClient that bypasses the app's production lifespan.

    Using ``TestClient(app)`` without a context manager skips startup/shutdown,
    so our injected ``app.state.broadcast`` survives — the lifespan would
    otherwise overwrite it with a fresh BroadcastService.
    """
    yield TestClient(app)


# ---------- auth ----------


def test_ws_no_token_rejected(ws_test_client: TestClient) -> None:
    """Connect without ?token= → close 4401 before accept."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_test_client.websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 4401


def test_ws_invalid_token_rejected(ws_test_client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with ws_test_client.websocket_connect("/ws?token=not-a-jwt"):
            pass
    assert exc_info.value.code == 4401


def test_ws_valid_token_accepted(ws_test_client: TestClient, jwt_token: str) -> None:
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "pong"})


# ---------- subscribe / unsubscribe ----------


def test_ws_subscribe_invalid_channel(ws_test_client: TestClient, jwt_token: str) -> None:
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["garbage:foo"]})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "Invalid channels" in msg["detail"]


def test_ws_subscribe_registers_in_broadcast(
    ws_test_client: TestClient, jwt_token: str, broadcast_svc: BroadcastService
) -> None:
    """subscribe must register the WS in the channel index inside BroadcastService."""
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["ticks:EURUSD"]})
        # Force a server response so we know the subscribe message was processed
        # (TestClient's send_json is fire-and-forget; without a round-trip the
        # handler may still be queued).
        ws.send_json({"type": "subscribe", "channels": ["bogus:foo"]})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert broadcast_svc.channel_subscriber_count("ticks:EURUSD") == 1
    # After context exit the disconnect handler runs cleanup.
    assert broadcast_svc.channel_subscriber_count("ticks:EURUSD") == 0


def test_ws_unsubscribe_removes_from_broadcast(
    ws_test_client: TestClient, jwt_token: str, broadcast_svc: BroadcastService
) -> None:
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["ticks:EURUSD"]})
        ws.send_json({"type": "unsubscribe", "channels": ["ticks:EURUSD"]})
        ws.send_json({"type": "pong"})
        assert broadcast_svc.channel_subscriber_count("ticks:EURUSD") == 0


# ---------- step 3.10a: "orders" channel whitelist ----------


def test_ws_subscribe_orders_channel_accepted(
    ws_test_client: TestClient, jwt_token: str, broadcast_svc: BroadcastService
) -> None:
    """step 3.10a: ``orders`` is the channel response_handler +
    event_handler publish to. Must be in the whitelist so the
    frontend can subscribe and receive ``order_updated`` broadcasts."""
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["orders"]})
        # Trigger a round-trip with an INVALID subscribe so we can
        # confirm the prior valid subscribe was processed (TestClient
        # send_json is fire-and-forget).
        ws.send_json({"type": "subscribe", "channels": ["garbage:foo"]})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "Invalid channels" in msg["detail"]
        assert broadcast_svc.channel_subscriber_count("orders") == 1
    assert broadcast_svc.channel_subscriber_count("orders") == 0


def test_ws_subscribe_positions_channel_still_accepted(
    ws_test_client: TestClient, jwt_token: str, broadcast_svc: BroadcastService
) -> None:
    """Regression: ``positions`` was already in the whitelist; the
    step-3.10a addition of ``orders`` must not have shifted the
    exact-match logic."""
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["positions"]})
        ws.send_json({"type": "subscribe", "channels": ["bogus:foo"]})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert broadcast_svc.channel_subscriber_count("positions") == 1
    assert broadcast_svc.channel_subscriber_count("positions") == 0


def test_ws_subscribe_orders_and_positions_mixed_both_accepted(
    ws_test_client: TestClient, jwt_token: str, broadcast_svc: BroadcastService
) -> None:
    """step-3.10 frontend sends a single ``{channels: ["positions",
    "orders"]}`` subscribe on each (re)connect. Both must land."""
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["positions", "orders"]})
        ws.send_json({"type": "subscribe", "channels": ["bogus:foo"]})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert broadcast_svc.channel_subscriber_count("orders") == 1
        assert broadcast_svc.channel_subscriber_count("positions") == 1
    assert broadcast_svc.channel_subscriber_count("orders") == 0
    assert broadcast_svc.channel_subscriber_count("positions") == 0


def test_ws_subscribe_ticks_prefix_still_accepted(
    ws_test_client: TestClient, jwt_token: str, broadcast_svc: BroadcastService
) -> None:
    """Regression: the prefix-match branch (``ticks:EURUSD``
    matches ``ticks:``) must still work after adding the exact-match
    ``orders`` entry to the tuple."""
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "subscribe", "channels": ["ticks:EURUSD"]})
        ws.send_json({"type": "subscribe", "channels": ["bogus:foo"]})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert broadcast_svc.channel_subscriber_count("ticks:EURUSD") == 1


# ---------- set_symbol ----------


def test_ws_set_symbol_no_md_returns_error(ws_test_client: TestClient, jwt_token: str) -> None:
    """When MD is missing/unauthenticated, set_symbol replies with an error."""
    previous = getattr(app.state, "market_data", None)
    app.state.market_data = None
    try:
        with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
            ws.send_json({"type": "set_symbol", "symbol": "EURUSD", "timeframe": "M15"})
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert "Market data service not ready" in msg["detail"]
    finally:
        app.state.market_data = previous


def test_ws_set_symbol_calls_md(
    ws_test_client: TestClient,
    jwt_token: str,
    mock_md_on_state: AsyncMock,
    broadcast_svc: BroadcastService,
) -> None:
    """When MD is ready, set_symbol invokes subscribe_spots + subscribe_live_trendbar."""
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "set_symbol", "symbol": "EURUSD", "timeframe": "M15"})
        # round-trip to flush the handler
        ws.send_json({"type": "pong"})

    assert mock_md_on_state.subscribe_spots.await_count >= 1
    sub_args, _ = mock_md_on_state.subscribe_spots.call_args
    assert sub_args[0] == ["EURUSD"]
    assert mock_md_on_state.subscribe_live_trendbar.await_count >= 1
    ltb_args, _ = mock_md_on_state.subscribe_live_trendbar.call_args
    assert ltb_args[0] == "EURUSD"
    assert ltb_args[1] == "M15"


def test_ws_set_symbol_then_switch(
    ws_test_client: TestClient,
    jwt_token: str,
    mock_md_on_state: AsyncMock,
) -> None:
    """Switching symbol must unsubscribe the previous before subscribing the new."""
    with ws_test_client.websocket_connect(f"/ws?token={jwt_token}") as ws:
        ws.send_json({"type": "set_symbol", "symbol": "EURUSD", "timeframe": "M15"})
        ws.send_json({"type": "set_symbol", "symbol": "USDJPY", "timeframe": "M15"})
        ws.send_json({"type": "pong"})

    assert mock_md_on_state.unsubscribe_spots.await_count >= 1
    assert mock_md_on_state.unsubscribe_live_trendbar.await_count >= 1
    last_sub, _ = mock_md_on_state.subscribe_spots.call_args
    assert last_sub[0] == ["USDJPY"]


# ---------- BroadcastService unit tests ----------


@pytest.mark.asyncio
async def test_broadcast_publish_tick_caches_to_redis(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """publish_tick must SETEX tick:{sym} with the JSON payload."""
    svc = RedisService(fake_redis)
    bs = BroadcastService(redis_svc=svc)
    tick: dict[str, Any] = {"type": "tick", "bid": 1.05, "ask": 1.0501, "ts": 1}
    await bs.publish_tick("EURUSD", tick)
    cached = await svc.get_tick_cache("EURUSD")
    assert cached is not None
    assert "1.05" in cached and "1.0501" in cached


@pytest.mark.asyncio
async def test_broadcast_publish_no_subscribers_is_noop() -> None:
    """publish must not raise when no one is listening on the channel."""
    bs = BroadcastService()
    await bs.publish("ticks:EURUSD", {"foo": "bar"})  # should silently no-op


@pytest.mark.asyncio
async def test_broadcast_publish_candle_does_not_cache(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Live candles are ephemeral — must NOT touch Redis."""
    svc = RedisService(fake_redis)
    bs = BroadcastService(redis_svc=svc)
    candle: dict[str, Any] = {"type": "candle_update", "open": 1.0, "close": 1.05}
    await bs.publish_candle("EURUSD", "M15", candle)
    # The redis tick cache key must not have been populated by publish_candle.
    assert await svc.get_tick_cache("EURUSD") is None
