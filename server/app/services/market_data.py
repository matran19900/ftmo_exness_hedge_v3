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
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
)
from twisted.internet import reactor as _reactor

from app.services.redis_service import RedisService
from app.services.symbol_whitelist import get_all_symbols, get_symbol_mapping

# Twisted's installed `reactor` is a runtime singleton typed as the bare base
# module by its partial stubs. Alias as Any so attribute access (running / run /
# callFromThread / stop) doesn't trip mypy strict.
reactor: Any = _reactor

logger = logging.getLogger(__name__)

# cTrader allows ~5 outbound messages per second per app; we pace slightly under
# that for sequential calls (used in sync_symbols).
_INTER_REQUEST_DELAY_SECONDS = 0.25


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

    async def sync_symbols(self, redis_svc: RedisService) -> int:
        """Fetch broker symbols, intersect with whitelist, cache to Redis.

        Returns the number of symbols cached.

        Steps:
            1. ``ProtoOASymbolsListReq``                     → light symbols.
            2. Match (case-insensitive) against the whitelist.
            3. For each match, ``ProtoOASymbolByIdReq``      → digits / pip / volume metadata.
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

        whitelist_names = get_all_symbols()
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
        cached = 0
        for ftmo_name, broker_id in matched:
            try:
                detail_req = ProtoOASymbolByIdReq()
                detail_req.ctidTraderAccountId = self._account_id
                detail_req.symbolId.append(broker_id)
                detail_resp = await self._send_and_wait(detail_req, timeout=30.0)
                if not detail_resp.symbol:
                    logger.warning(
                        "sync_symbols: empty detail response for %s (id=%d)",
                        ftmo_name,
                        broker_id,
                    )
                    continue
                detail = detail_resp.symbol[0]
                mapping = get_symbol_mapping(ftmo_name)
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
                if mapping is not None:
                    config["exness_symbol"] = mapping.exness
                await redis_svc.set_symbol_config(ftmo_name, config)
                await redis_svc.add_active_symbol(ftmo_name)
                cached += 1
                # Pace requests so we stay under the 5/sec broker limit.
                await asyncio.sleep(_INTER_REQUEST_DELAY_SECONDS)
            except Exception:  # noqa: BLE001
                logger.exception("sync_symbols: failed for %s (id=%d)", ftmo_name, broker_id)

        logger.info(
            "sync_symbols: cached %d/%d symbols (broker=%d available)",
            cached,
            len(matched),
            len(broker_index),
        )
        return cached

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
        """Unsolicited and solicited messages both flow here.

        Solicited responses are routed to their per-clientMsgId Deferred by the
        underlying client; this callback only logs unexpected ones for now.
        Phase 2.3 will dispatch ProtoOASpotEvent / ProtoOATrendbarEvent here.
        """
        try:
            payload_type = getattr(message, "payloadType", None)
            logger.debug("cTrader message received: payloadType=%s", payload_type)
        except Exception:  # noqa: BLE001
            pass

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
