"""WebSocket broadcast service.

Channel-based fan-out for the /ws endpoint. Manages WebSocket connections,
their subscriptions, and pushes ``{"channel": ..., "data": ...}`` envelopes
to all subscribers when a publish lands.

Channels used in step 2.3: ``ticks:{symbol}``, ``candles:{symbol}:{timeframe}``.
``positions`` and ``agents`` are reserved for Phase 3 / Phase 4.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)

_DEFAULT_TICK_TTL_SECONDS = 60


class BroadcastService:
    """Manages WebSocket connections and per-channel fan-out."""

    def __init__(self, redis_svc: RedisService | None = None) -> None:
        self._redis_svc = redis_svc
        self._connections: dict[WebSocket, set[str]] = {}
        self._channels: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    # ---------- connection lifecycle ----------

    async def connect(self, ws: WebSocket, user: str) -> None:
        """Register a new connection. The caller must have already accepted the WS."""
        async with self._lock:
            self._connections[ws] = set()
        logger.info("WS connected: user=%s, total_connections=%d", user, len(self._connections))

    async def disconnect(self, ws: WebSocket) -> None:
        """Drop a connection and all of its subscription bindings."""
        async with self._lock:
            channels = self._connections.pop(ws, set())
            for ch in channels:
                self._channels[ch].discard(ws)
                if not self._channels[ch]:
                    del self._channels[ch]
        logger.info("WS disconnected: total_connections=%d", len(self._connections))

    # ---------- subscription management ----------

    async def subscribe(self, ws: WebSocket, channels: list[str]) -> None:
        async with self._lock:
            if ws not in self._connections:
                return
            for ch in channels:
                self._connections[ws].add(ch)
                self._channels[ch].add(ws)

    async def unsubscribe(self, ws: WebSocket, channels: list[str]) -> None:
        async with self._lock:
            if ws not in self._connections:
                return
            for ch in channels:
                self._connections[ws].discard(ch)
                self._channels[ch].discard(ws)
                if not self._channels[ch]:
                    del self._channels[ch]

    # ---------- publishing ----------

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        """Send ``{channel, data}`` to all subscribers. Failures are logged, not raised."""
        async with self._lock:
            subscribers = list(self._channels.get(channel, set()))
        if not subscribers:
            return
        envelope = json.dumps({"channel": channel, "data": data})
        await asyncio.gather(
            *(self._safe_send(ws, envelope) for ws in subscribers),
            return_exceptions=True,
        )

    async def _safe_send(self, ws: WebSocket, envelope: str) -> None:
        try:
            await ws.send_text(envelope)
        except Exception:  # noqa: BLE001
            logger.warning(
                "BroadcastService: send failed; connection will be cleaned up on next disconnect"
            )

    # ---------- convenience ----------

    async def publish_tick(self, ftmo_symbol: str, tick: dict[str, Any]) -> None:
        """Cache the tick (best-effort) and broadcast on ``ticks:{symbol}``."""
        if self._redis_svc is not None:
            try:
                await self._redis_svc.set_tick_cache(
                    ftmo_symbol, json.dumps(tick), ttl_seconds=_DEFAULT_TICK_TTL_SECONDS
                )
            except Exception:  # noqa: BLE001
                logger.exception("BroadcastService: tick cache write failed")
        await self.publish(f"ticks:{ftmo_symbol}", tick)

    async def publish_candle(
        self, ftmo_symbol: str, timeframe: str, candle: dict[str, Any]
    ) -> None:
        """Broadcast a live candle update on ``candles:{symbol}:{timeframe}``.

        Live candle updates are NOT cached in Redis — frontends fetch the
        initial 200 bars via /charts and patch the trailing bar from the WS.
        """
        await self.publish(f"candles:{ftmo_symbol}:{timeframe}", candle)

    # ---------- introspection (used by tests) ----------

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def channel_subscriber_count(self, channel: str) -> int:
        return len(self._channels.get(channel, set()))
