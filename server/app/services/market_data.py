"""cTrader Open API market-data wrapper.

Bridges Twisted (which the cTrader client library uses) and asyncio (which
FastAPI runs on). One server has at most one market-data connection.

Phase 2.1 surface: start / stop / authenticate / sync_symbols.
Spots, trendbars, candle subscriptions are deferred to later Phase-2 steps
(see docs/07-server-services.md Section 8).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from ctrader_open_api import Client, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOAGetTrendbarsReq,
    ProtoOASubscribeLiveTrendbarReq,
    ProtoOASubscribeSpotsReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
    ProtoOAUnsubscribeLiveTrendbarReq,
    ProtoOAUnsubscribeSpotsReq,
)
from twisted.internet import reactor as _reactor

from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService

# Twisted's installed `reactor` is a runtime singleton typed as the bare base
# module by its partial stubs. Alias as Any so attribute access (running / run /
# callFromThread / stop) doesn't trip mypy strict.
reactor: Any = _reactor

logger = logging.getLogger(__name__)

# Map our timeframe string → cTrader ProtoOATrendbarPeriod enum value (and the
# bar-length in seconds, used to compute the fromTimestamp for a "last N bars
# from now" query).
_TIMEFRAME_TO_PERIOD: dict[str, tuple[int, int]] = {
    "M1": (1, 60),
    "M5": (5, 300),
    "M15": (7, 900),
    "M30": (8, 1800),
    "H1": (9, 3600),
    "H4": (10, 14400),
    "D1": (12, 86400),
    "W1": (13, 604800),
}

# cTrader Open API payloadType for ProtoOASpotEvent. Verified against the
# library's protobuf default value at module load time (the enum module
# isn't exposed by name in this library version).
_PROTO_OA_SPOT_EVENT = 2131

# All raw cTrader prices are integers uniformly scaled by 10^5 — see D-032.
_PRICE_SCALE = 100000.0


class MarketDataService:
    """Async-friendly facade over a Twisted-driven cTrader client.

    Lifecycle:
        1. ``start()``         — spawn reactor thread, open TCP, app-auth.
        2. ``authenticate()``  — bind a cTrader account via OAuth access token.
        3. ``sync_symbols()``  — fetch broker symbols, filter via whitelist, cache to Redis.
        4. ``stop()``          — disconnect and stop the reactor.

    Idempotent ``start()`` and ``stop()``: calling twice is safe.
    """

    def __init__(self, host: str, port: int, client_id: str, client_secret: str) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._client_secret = client_secret
        self._client: Any = None  # ctrader_open_api.Client; untyped lib
        self._reactor_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = asyncio.Event()  # TCP up + app auth ok
        self._authenticated = asyncio.Event()  # account auth ok
        self._account_id: int | None = None
        self._access_token: str | None = None
        # Spot/trendbar fan-out state (Phase 2.3).
        self._broadcast: BroadcastService | None = None
        self._symbol_id_to_name: dict[int, str] = {}
        # (ctrader_symbol_id, period_enum) -> (ftmo_symbol, timeframe)
        self._live_trendbar_map: dict[tuple[int, int], tuple[str, str]] = {}

    # ---------- public API ----------

    async def start(self) -> None:
        """Start reactor + connect TCP + app-auth. Idempotent."""
        if self._client is not None:
            return
        self._loop = asyncio.get_running_loop()

        def run_reactor() -> None:
            client = Client(self._host, self._port, TcpProtocol)
            client.setConnectedCallback(self._on_connected)
            client.setDisconnectedCallback(self._on_disconnected)
            client.setMessageReceivedCallback(self._on_message)
            self._client = client
            client.startService()
            # Once startService is called the reactor is already running for the
            # process (Twisted's installed reactor is a singleton). If reactor
            # is not yet running, start it; otherwise this thread idles until
            # stop().
            if not reactor.running:
                reactor.run(installSignalHandlers=False)

        self._reactor_thread = threading.Thread(
            target=run_reactor, daemon=True, name="ctrader-reactor"
        )
        self._reactor_thread.start()

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=30.0)
        except TimeoutError as e:
            raise RuntimeError(
                f"cTrader connection/app-auth timed out after 30s ({self._host}:{self._port})"
            ) from e

        logger.info("MarketDataService connected to %s:%s", self._host, self._port)

    async def stop(self) -> None:
        """Shut down the client and reactor. Safe to call when not started."""
        if self._client is None:
            return
        client = self._client
        try:
            reactor.callFromThread(client.stopService)
        except Exception:  # noqa: BLE001
            logger.exception("Error stopping cTrader client")
        # Stop the reactor only if we actually started it. Twisted's reactor
        # is process-global; in tests where a reactor is never started, calling
        # stop() would raise.
        try:
            if reactor.running:
                reactor.callFromThread(reactor.stop)
        except Exception:  # noqa: BLE001
            logger.exception("Error stopping Twisted reactor")
        if self._reactor_thread and self._reactor_thread.is_alive():
            self._reactor_thread.join(timeout=5.0)
        self._client = None
        self._reactor_thread = None
        self._connected.clear()
        self._authenticated.clear()
        logger.info("MarketDataService stopped")

    async def authenticate(self, access_token: str, account_id: int) -> None:
        """Account-auth step: bind the connection to a cTrader trading account."""
        if self._client is None or not self._connected.is_set():
            raise RuntimeError("MarketDataService not started")

        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = account_id
        req.accessToken = access_token

        await self._send_and_wait(req, timeout=30.0)
        self._account_id = account_id
        self._access_token = access_token
        self._authenticated.set()
        logger.info("MarketDataService authenticated for account_id=%s", account_id)

    async def sync_symbols(
        self, redis_svc: RedisService, whitelist_names: list[str]
    ) -> int:
        """Fetch broker symbols, intersect with ``whitelist_names``, cache to Redis.

        Returns the number of symbols cached.

        Phase 4.A.5: ``whitelist_names`` is now an explicit parameter (was a
        module-level lookup via the deleted ``symbol_whitelist`` shim). The
        caller threads it from ``FTMOWhitelistService.all_symbols()``.

        Steps:
            1. ``ProtoOASymbolsListReq``                     → light symbols.
            2. Match (case-insensitive) against ``whitelist_names``.
            3. ONE batched ``ProtoOASymbolByIdReq`` carrying every matched id
               → digits / pip / volume metadata for all symbols (step 2.7b).
            4. ``HSET symbol_config:{FTMO_NAME}`` per match.
            5. ``SADD symbols:active`` per match.
        """
        if not self._authenticated.is_set() or self._account_id is None:
            raise RuntimeError("Not authenticated")

        list_req = ProtoOASymbolsListReq()
        list_req.ctidTraderAccountId = self._account_id
        list_req.includeArchivedSymbols = False
        list_resp = await self._send_and_wait(list_req, timeout=30.0)

        # Build broker-side index: name (uppercase) → symbolId.
        broker_index: dict[str, int] = {}
        for light in list_resp.symbol:
            name = (light.symbolName or "").upper()
            if name and light.enabled:
                broker_index[name] = int(light.symbolId)
        matched: list[tuple[str, int]] = []
        for ftmo_name in whitelist_names:
            broker_id = broker_index.get(ftmo_name.upper())
            if broker_id is not None:
                matched.append((ftmo_name, broker_id))

        if not matched:
            logger.warning(
                "sync_symbols: no whitelist symbols matched broker availability "
                "(broker symbols=%d, whitelist=%d)",
                len(broker_index),
                len(whitelist_names),
            )
            await redis_svc.clear_active_symbols()
            return 0

        # Fresh rebuild of the active set.
        await redis_svc.clear_active_symbols()

        # Single batch ProtoOASymbolByIdReq containing every matched broker id.
        # `symbolId` is a `repeated` protobuf field — appending each id and
        # firing one request returns details for all of them in one round-trip.
        # This collapses ~91 sequential RTTs (~60–90s) into one (~1–3s).
        detail_req = ProtoOASymbolByIdReq()
        detail_req.ctidTraderAccountId = self._account_id
        for _, broker_id in matched:
            detail_req.symbolId.append(broker_id)

        logger.info("sync_symbols: requesting batch details for %d symbols", len(matched))
        batch_started = time.monotonic()
        try:
            detail_resp = await self._send_and_wait(detail_req, timeout=60.0)
        except Exception:  # noqa: BLE001
            logger.exception("sync_symbols: batch detail request failed")
            return 0

        logger.info(
            "sync_symbols: received batch details for %d symbols in %.2fs",
            len(detail_resp.symbol),
            time.monotonic() - batch_started,
        )

        # Index broker-side details by symbolId for O(1) match against `matched`.
        detail_by_id: dict[int, Any] = {int(d.symbolId): d for d in detail_resp.symbol}

        cached = 0
        for ftmo_name, broker_id in matched:
            detail = detail_by_id.get(broker_id)
            if detail is None:
                logger.warning(
                    "sync_symbols: broker did not return details for %s (id=%d)",
                    ftmo_name,
                    broker_id,
                )
                continue
            try:
                # Phase 4.A.1 (D-SM-09): symbol_config no longer carries any
                # Exness-side field. Exness resolution is per-account through
                # MappingService — step 4.A.5 wires the lookup.
                config: dict[str, Any] = {
                    "ftmo_symbol": ftmo_name,
                    "ctrader_symbol_id": broker_id,
                    "digits": int(detail.digits),
                    "pip_position": int(detail.pipPosition),
                    "min_volume": int(detail.minVolume),
                    "max_volume": int(detail.maxVolume),
                    "step_volume": int(detail.stepVolume),
                    "lot_size": int(detail.lotSize),
                    "synced_at": int(time.time() * 1000),
                }
                await redis_svc.set_symbol_config(ftmo_name, config)
                await redis_svc.add_active_symbol(ftmo_name)
                cached += 1
            except Exception:  # noqa: BLE001
                logger.exception("sync_symbols: failed to cache %s (id=%d)", ftmo_name, broker_id)

        logger.info(
            "sync_symbols: cached %d/%d symbols (broker=%d available)",
            cached,
            len(matched),
            len(broker_index),
        )
        return cached

    async def get_trendbars(
        self,
        ftmo_symbol: str,
        timeframe: str,
        count: int,
        redis_svc: RedisService,
    ) -> list[dict[str, Any]]:
        """Fetch the last ``count`` historical candles for ``ftmo_symbol``.

        The cTrader symbol id comes from the ``symbol_config:{ftmo_symbol}``
        hash that ``sync_symbols`` populated. Prices are returned as floats;
        timestamps are unix seconds (Lightweight Charts convention, per
        docs/08-server-api.md).
        """
        if not self._authenticated.is_set() or self._account_id is None:
            raise RuntimeError("Not authenticated")
        if timeframe not in _TIMEFRAME_TO_PERIOD:
            raise ValueError(
                f"Invalid timeframe: {timeframe}. Allowed: {sorted(_TIMEFRAME_TO_PERIOD)}"
            )

        config = await redis_svc.get_symbol_config(ftmo_symbol)
        if not config or "ctrader_symbol_id" not in config:
            raise RuntimeError(f"Symbol {ftmo_symbol} not in active set")
        symbol_id = int(config["ctrader_symbol_id"])
        # See D-032: cTrader sends raw prices uniformly scaled by 10^5; the
        # `digits` field in symbol_config is for display formatting only.
        scale = _PRICE_SCALE

        period_enum, period_seconds = _TIMEFRAME_TO_PERIOD[timeframe]
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - count * period_seconds * 1000

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = symbol_id
        req.period = period_enum
        req.fromTimestamp = from_ms
        req.toTimestamp = now_ms
        req.count = count

        response = await self._send_and_wait(req, timeout=30.0)

        candles: list[dict[str, Any]] = []
        for tb in response.trendbar:
            time_seconds = int(tb.utcTimestampInMinutes) * 60
            low = tb.low / scale
            open_ = (tb.low + tb.deltaOpen) / scale
            high = (tb.low + tb.deltaHigh) / scale
            close = (tb.low + tb.deltaClose) / scale
            candles.append(
                {
                    "time": time_seconds,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": int(tb.volume),
                }
            )

        if not candles:
            logger.warning(
                "get_trendbars: cTrader returned 0 candles for %s/%s (count=%d)",
                ftmo_symbol,
                timeframe,
                count,
            )

        candles.sort(key=lambda c: c["time"])
        return candles

    # ---------- spot / live trendbar subscriptions ----------

    def inject_broadcast(self, broadcast: BroadcastService) -> None:
        """Wire in the BroadcastService that fans out spot/candle events."""
        self._broadcast = broadcast

    async def subscribe_spots(self, ftmo_symbols: list[str], redis_svc: RedisService) -> None:
        """Subscribe to spot tick updates for ``ftmo_symbols``. Idempotent server-side."""
        if not self._authenticated.is_set() or self._account_id is None:
            raise RuntimeError("Not authenticated")
        symbol_ids: list[int] = []
        for ftmo_sym in ftmo_symbols:
            config = await redis_svc.get_symbol_config(ftmo_sym)
            if not config or "ctrader_symbol_id" not in config:
                logger.warning("subscribe_spots: skipping unknown symbol %s", ftmo_sym)
                continue
            sid = int(config["ctrader_symbol_id"])
            symbol_ids.append(sid)
            self._symbol_id_to_name[sid] = ftmo_sym
        if not symbol_ids:
            return
        req = ProtoOASubscribeSpotsReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId.extend(symbol_ids)
        await self._send_and_wait(req, timeout=30.0)
        logger.info("subscribe_spots: %s", ftmo_symbols)

    async def unsubscribe_spots(self, ftmo_symbols: list[str], redis_svc: RedisService) -> None:
        """Unsubscribe spots best-effort (silently no-op if not authenticated)."""
        if not self._authenticated.is_set() or self._account_id is None:
            return
        symbol_ids: list[int] = []
        for ftmo_sym in ftmo_symbols:
            config = await redis_svc.get_symbol_config(ftmo_sym)
            if config and "ctrader_symbol_id" in config:
                symbol_ids.append(int(config["ctrader_symbol_id"]))
        if not symbol_ids:
            return
        req = ProtoOAUnsubscribeSpotsReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId.extend(symbol_ids)
        await self._send_and_wait(req, timeout=10.0)
        logger.info("unsubscribe_spots: %s", ftmo_symbols)

    async def subscribe_live_trendbar(
        self, ftmo_symbol: str, timeframe: str, redis_svc: RedisService
    ) -> None:
        """Subscribe live trendbar updates for one (symbol, timeframe) pair."""
        if not self._authenticated.is_set() or self._account_id is None:
            raise RuntimeError("Not authenticated")
        if timeframe not in _TIMEFRAME_TO_PERIOD:
            raise ValueError(f"Invalid timeframe: {timeframe}")
        config = await redis_svc.get_symbol_config(ftmo_symbol)
        if not config or "ctrader_symbol_id" not in config:
            raise RuntimeError(f"Symbol {ftmo_symbol} not in active set")
        symbol_id = int(config["ctrader_symbol_id"])
        period_enum, _ = _TIMEFRAME_TO_PERIOD[timeframe]
        self._live_trendbar_map[(symbol_id, period_enum)] = (ftmo_symbol, timeframe)
        req = ProtoOASubscribeLiveTrendbarReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = symbol_id
        req.period = period_enum
        await self._send_and_wait(req, timeout=10.0)
        logger.info("subscribe_live_trendbar: %s %s", ftmo_symbol, timeframe)

    async def unsubscribe_live_trendbar(
        self, ftmo_symbol: str, timeframe: str, redis_svc: RedisService
    ) -> None:
        """Unsubscribe live trendbar best-effort."""
        if not self._authenticated.is_set() or self._account_id is None:
            return
        if timeframe not in _TIMEFRAME_TO_PERIOD:
            return
        config = await redis_svc.get_symbol_config(ftmo_symbol)
        if not config or "ctrader_symbol_id" not in config:
            return
        symbol_id = int(config["ctrader_symbol_id"])
        period_enum, _ = _TIMEFRAME_TO_PERIOD[timeframe]
        self._live_trendbar_map.pop((symbol_id, period_enum), None)
        req = ProtoOAUnsubscribeLiveTrendbarReq()
        req.ctidTraderAccountId = self._account_id
        req.symbolId = symbol_id
        req.period = period_enum
        await self._send_and_wait(req, timeout=10.0)
        logger.info("unsubscribe_live_trendbar: %s %s", ftmo_symbol, timeframe)

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated.is_set()

    # ---------- Twisted-thread callbacks ----------

    def _on_connected(self, client: Any) -> None:
        """TCP is up. Trigger application authentication."""
        logger.info("cTrader TCP connected; sending app auth")
        req = ProtoOAApplicationAuthReq()
        req.clientId = self._client_id
        req.clientSecret = self._client_secret
        d = client.send(req)
        d.addCallback(self._on_app_authed)
        d.addErrback(self._on_app_auth_error)

    def _on_app_authed(self, _response: Any) -> None:
        """App auth succeeded — flip the connected event from the asyncio loop."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._connected.set)

    def _on_app_auth_error(self, failure: Any) -> None:
        logger.error("cTrader app auth failed: %s", failure)

    def _on_disconnected(self, _client: Any, reason: Any) -> None:
        logger.warning("cTrader disconnected: %s", reason)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._connected.clear)
            self._loop.call_soon_threadsafe(self._authenticated.clear)

    def _on_message(self, _client: Any, message: Any) -> None:
        """Unsolicited and solicited messages flow here.

        Solicited responses are routed to their per-clientMsgId Deferred by the
        underlying client. We only handle unsolicited stream events (spot ticks
        and live trendbar updates) here — both arrive as ProtoOASpotEvent.
        """
        try:
            payload_type = getattr(message, "payloadType", None)
            if payload_type == _PROTO_OA_SPOT_EVENT:
                spot = Protobuf.extract(message)
                self._handle_spot_event(spot)
        except Exception:  # noqa: BLE001
            logger.exception("Error handling cTrader message")

    def _handle_spot_event(self, spot: Any) -> None:
        """Translate ProtoOASpotEvent into tick + (optional) candle broadcasts.

        Runs in the Twisted reactor thread; all asyncio work is scheduled on
        the captured event loop via ``asyncio.run_coroutine_threadsafe``.
        """
        if self._loop is None or self._broadcast is None:
            return
        symbol_id = int(spot.symbolId)
        ftmo_sym = self._symbol_id_to_name.get(symbol_id)
        if ftmo_sym is None:
            return

        bid = float(spot.bid) / _PRICE_SCALE if spot.HasField("bid") else None
        ask = float(spot.ask) / _PRICE_SCALE if spot.HasField("ask") else None
        if bid is not None or ask is not None:
            tick = {
                "type": "tick",
                "symbol": ftmo_sym,
                "bid": bid,
                "ask": ask,
                "ts": int(time.time() * 1000),
            }
            asyncio.run_coroutine_threadsafe(
                self._broadcast.publish_tick(ftmo_sym, tick), self._loop
            )

        for tb in spot.trendbar:
            period_enum = int(tb.period)
            entry = self._live_trendbar_map.get((symbol_id, period_enum))
            if entry is None:
                continue
            _, timeframe = entry
            time_seconds = int(tb.utcTimestampInMinutes) * 60
            low_raw = int(tb.low)
            candle = {
                "type": "candle_update",
                "time": time_seconds,
                "open": (low_raw + int(tb.deltaOpen)) / _PRICE_SCALE,
                "high": (low_raw + int(tb.deltaHigh)) / _PRICE_SCALE,
                "low": low_raw / _PRICE_SCALE,
                "close": (low_raw + int(tb.deltaClose)) / _PRICE_SCALE,
            }
            asyncio.run_coroutine_threadsafe(
                self._broadcast.publish_candle(ftmo_sym, timeframe, candle), self._loop
            )

    # ---------- async <-> Twisted bridge ----------

    async def _send_and_wait(self, message: Any, timeout: float = 30.0) -> Any:
        """Send a request through Twisted and await its response in asyncio.

        The cTrader library delivers responses as a ``ProtoMessage`` wrapper
        (payloadType / payload bytes / clientMsgId). Callers want the inner
        protobuf (e.g. ``ProtoOASymbolsListRes`` with its ``symbol`` field), so
        we unwrap once here via ``Protobuf.extract`` — every callsite gets the
        already-decoded message.
        """
        if self._loop is None or self._client is None:
            raise RuntimeError("MarketDataService not started")
        client = self._client
        loop = self._loop
        future: asyncio.Future[Any] = loop.create_future()

        def on_success(response: Any) -> None:
            if not future.done():
                loop.call_soon_threadsafe(future.set_result, response)

        def on_error(failure: Any) -> None:
            if not future.done():
                loop.call_soon_threadsafe(
                    future.set_exception, RuntimeError(f"cTrader request failed: {failure}")
                )

        def send_in_reactor() -> None:
            d = client.send(message)
            d.addCallback(on_success)
            d.addErrback(on_error)

        reactor.callFromThread(send_in_reactor)
        wrapper = await asyncio.wait_for(future, timeout=timeout)
        return Protobuf.extract(wrapper)
