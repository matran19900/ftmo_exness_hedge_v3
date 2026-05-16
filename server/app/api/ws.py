"""WebSocket endpoint for real-time market data.

Auth: ``ws://host/ws?token=<JWT>``. Reject with close code 4401 if invalid.

Client → Server messages:
    {"type": "subscribe", "channels": ["ticks:EURUSD", ...]}
    {"type": "unsubscribe", "channels": [...]}
    {"type": "set_symbol", "symbol": "EURUSD", "timeframe": "M15"}
    {"type": "pong"}

Server → Client messages:
    {"channel": "ticks:EURUSD", "data": {...}}
    {"channel": "candles:EURUSD:M15", "data": {...}}
    {"type": "ping"}
    {"type": "error", "detail": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.auth import decode_access_token
from app.config import Settings, get_settings
from app.services.broadcast import BroadcastService
from app.services.market_data import MarketDataService
from app.services.redis_service import RedisService, get_redis_service

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_CHANNEL_PREFIXES = (
    "ticks:",
    "candles:",
    "positions",
    "orders",
    "accounts",
    "agents",
    "mapping_status:",  # Phase 4.A.4 — per-Exness-account wizard state (D-4.A.0-9)
    "alerts",  # Step 4.11 — AlertService.broadcast.publish target; the
               # frontend Settings UI tab (deferred 4.11c) will subscribe
               # here for live alert toasts. Whitelisted now so the WS
               # plumbing is ready when the UI lands.
)
HEARTBEAT_INTERVAL_SECONDS = 30
WS_CLOSE_INVALID_TOKEN = 4401


def _channel_is_valid(channel: str) -> bool:
    return any(channel.startswith(p) or channel == p for p in VALID_CHANNEL_PREFIXES)


def get_broadcast() -> BroadcastService:
    """Return the singleton BroadcastService held on app.state."""
    from app.main import app  # noqa: PLC0415  — lazy to avoid import cycle

    bs: BroadcastService | None = getattr(app.state, "broadcast", None)
    if bs is None:
        raise RuntimeError("Broadcast service not initialized")
    return bs


def get_market_data_optional() -> MarketDataService | None:
    """Return the MarketDataService if it's running and authenticated, else None."""
    from app.main import app  # noqa: PLC0415

    md: MarketDataService | None = getattr(app.state, "market_data", None)
    if md is None or not md.is_authenticated:
        return None
    return md


async def _validate_token(token: str | None, settings: Settings) -> str | None:
    """Decode the JWT once at the start of the WebSocket handshake."""
    if not token:
        return None
    try:
        payload = decode_access_token(token, settings.jwt_secret)
    except jwt.InvalidTokenError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


async def _safe_send_text(ws: WebSocket, payload: str) -> None:
    try:
        await ws.send_text(payload)
    except Exception:  # noqa: BLE001
        logger.debug("WS send failed (likely already closed)")


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    settings: Annotated[Settings, Depends(get_settings)],
    redis_svc: Annotated[RedisService, Depends(get_redis_service)],
    token: Annotated[str | None, Query()] = None,
) -> None:
    user = await _validate_token(token, settings)
    if user is None:
        await websocket.close(code=WS_CLOSE_INVALID_TOKEN, reason="Invalid token")
        return

    await websocket.accept()
    bs = get_broadcast()
    await bs.connect(websocket, user)

    async def heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                await _safe_send_text(websocket, json.dumps({"type": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("heartbeat task ended")

    hb_task = asyncio.create_task(heartbeat())

    current_symbol: str | None = None
    current_timeframe: str | None = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await _safe_send_text(
                    websocket, json.dumps({"type": "error", "detail": "Invalid JSON"})
                )
                continue

            msg_type = msg.get("type")

            if msg_type == "subscribe":
                channels_raw = msg.get("channels", [])
                if not isinstance(channels_raw, list):
                    await _safe_send_text(
                        websocket,
                        json.dumps(
                            {"type": "error", "detail": "subscribe requires 'channels' list"}
                        ),
                    )
                    continue
                channels = [c for c in channels_raw if isinstance(c, str)]
                valid = [c for c in channels if _channel_is_valid(c)]
                if valid:
                    await bs.subscribe(websocket, valid)
                invalid = sorted(set(channels) - set(valid))
                if invalid:
                    await _safe_send_text(
                        websocket,
                        json.dumps({"type": "error", "detail": f"Invalid channels: {invalid}"}),
                    )

            elif msg_type == "unsubscribe":
                channels_raw = msg.get("channels", [])
                if isinstance(channels_raw, list):
                    channels = [c for c in channels_raw if isinstance(c, str)]
                    await bs.unsubscribe(websocket, channels)

            elif msg_type == "set_symbol":
                symbol = msg.get("symbol")
                timeframe = msg.get("timeframe", "M15")
                if not isinstance(symbol, str) or not symbol:
                    await _safe_send_text(
                        websocket,
                        json.dumps(
                            {"type": "error", "detail": "set_symbol requires 'symbol' field"}
                        ),
                    )
                    continue
                if not isinstance(timeframe, str):
                    timeframe = "M15"

                md = get_market_data_optional()
                if md is None:
                    await _safe_send_text(
                        websocket,
                        json.dumps({"type": "error", "detail": "Market data service not ready"}),
                    )
                    continue

                if current_symbol is not None and (
                    current_symbol != symbol or current_timeframe != timeframe
                ):
                    try:
                        await md.unsubscribe_spots([current_symbol], redis_svc)
                        if current_timeframe:
                            await md.unsubscribe_live_trendbar(
                                current_symbol, current_timeframe, redis_svc
                            )
                    except Exception:  # noqa: BLE001
                        logger.exception("set_symbol: unsubscribe previous failed")

                try:
                    await md.subscribe_spots([symbol], redis_svc)
                    await md.subscribe_live_trendbar(symbol, timeframe, redis_svc)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("set_symbol failed")
                    await _safe_send_text(
                        websocket,
                        json.dumps({"type": "error", "detail": f"set_symbol failed: {exc}"}),
                    )
                    continue

                current_symbol = symbol
                current_timeframe = timeframe
                await bs.subscribe(
                    websocket,
                    [f"ticks:{symbol}", f"candles:{symbol}:{timeframe}"],
                )

            elif msg_type == "pong":
                # Client acknowledged our ping. Could track liveness here later.
                pass

            else:
                await _safe_send_text(
                    websocket,
                    json.dumps({"type": "error", "detail": f"Unknown message type: {msg_type}"}),
                )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected by client (user=%s)", user)
    except Exception:  # noqa: BLE001
        logger.exception("WebSocket error (user=%s)", user)
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass
        await bs.disconnect(websocket)
