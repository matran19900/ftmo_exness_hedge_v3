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
import time
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
        """Coalesce a (possibly partial) delta tick against the last
        cached full tick, then cache + broadcast the merged result.

        cTrader's ``ProtoOASpotEvent`` only carries fields that
        changed in that update — Phase 2's ``_handle_spot_event``
        translates ``HasField("bid")=False`` to ``bid=None`` (same
        for ``ask``). Step 3.11b coalesces those partials here so
        downstream consumers (``OrderService.create_order``,
        ``position_tracker._compute_pnl``, frontend) always see a
        full ``{bid, ask, ts}`` tick.

        Initial-state edge: if a partial delta arrives BEFORE the
        cache has ever held a full tick (e.g. very first
        spot event after startup), the coalesced result is still
        incomplete. In that case we DROP the publish + cache write
        rather than poison the cache with a half-tick — the next
        cTrader update will fill the other side and we'll publish
        then. Defensive guards from step 3.11a in OrderService and
        position_tracker stay in place as belt-and-suspenders.

        Fast path: full deltas (both sides present) skip the cache
        read entirely — no extra Redis round-trip for the common
        case.
        """
        coalesced = await self._coalesce_tick(ftmo_symbol, tick)
        if coalesced is None:
            return

        if self._redis_svc is not None:
            try:
                await self._redis_svc.set_tick_cache(
                    ftmo_symbol,
                    json.dumps(coalesced),
                    ttl_seconds=_DEFAULT_TICK_TTL_SECONDS,
                )
            except Exception:  # noqa: BLE001
                logger.exception("BroadcastService: tick cache write failed")
        await self.publish(f"ticks:{ftmo_symbol}", coalesced)

    async def _coalesce_tick(
        self, ftmo_symbol: str, delta: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Merge a delta tick with the last cached full tick.

        Returns:
          - The original ``delta`` dict when it's already full (both
            ``bid`` and ``ask`` present and non-None) — fast path,
            zero cache reads.
          - A NEW dict combining the delta's present side(s) + the
            previous cache's missing side(s) + the delta's
            ``ts`` (or ``time.time()*1000`` if delta lacks ts).
          - ``None`` when the delta is partial AND no usable previous
            tick is available (missing cache, malformed JSON, cache
            read raised, or prev tick itself partial).

        Result is a fresh dict — does not mutate inputs.
        """
        delta_bid = delta.get("bid")
        delta_ask = delta.get("ask")

        # Fast path: full delta → no coalesce needed. Skip the cache
        # read entirely so the common case stays a single Redis
        # operation (the SETEX in publish_tick).
        if delta_bid is not None and delta_ask is not None:
            return delta

        # Partial delta — try to fill the missing side from the
        # cached previous tick. Defensive against every cache failure
        # mode: missing key, malformed JSON, redis errors. All fall
        # through to "no usable prev" so the publish gets dropped.
        prev_full: dict[str, Any] | None = None
        if self._redis_svc is not None:
            try:
                prev_raw = await self._redis_svc.get_tick_cache(ftmo_symbol)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "BroadcastService: tick cache read failed during coalesce for %s",
                    ftmo_symbol,
                )
                prev_raw = None
            if prev_raw:
                try:
                    decoded = json.loads(prev_raw)
                    if isinstance(decoded, dict):
                        prev_full = decoded
                except json.JSONDecodeError:
                    logger.debug(
                        "BroadcastService: malformed prev tick cache for %s; "
                        "treating as no previous state",
                        ftmo_symbol,
                    )

        prev_bid = prev_full.get("bid") if prev_full else None
        prev_ask = prev_full.get("ask") if prev_full else None

        merged_bid = delta_bid if delta_bid is not None else prev_bid
        merged_ask = delta_ask if delta_ask is not None else prev_ask

        # Initial-state guard: still incomplete after merge → drop.
        # ``publish_tick`` interprets ``None`` as "do not publish".
        if merged_bid is None or merged_ask is None:
            return None

        # Use delta's ``ts`` (newer) when present; fall back to wall
        # clock if the spot-event handler omitted it (shouldn't
        # happen in production but cheap belt-and-suspenders).
        ts = delta.get("ts")
        if ts is None:
            ts = int(time.time() * 1000)

        return {
            "type": "tick",
            "symbol": ftmo_symbol,
            "bid": merged_bid,
            "ask": merged_ask,
            "ts": ts,
        }

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
