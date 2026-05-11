"""cTrader trading connection bridge for FTMO client.

NOTE: This file duplicates Twisted-asyncio bridge mechanics from
server/app/services/market_data.py (Phase 2.1). Duplication is
intentional in Phase 3 — extraction to shared/ will happen in Phase 4
or 5 once the MT5 adapter (Exness) shows the proper abstraction shape.
Do NOT extract prematurely.

Per-account: each FTMO client process drives ONE FTMO account. The
bridge is constructed with that account's access_token and
ctid_trader_account_id and immediately runs application + account auth
on connect. Trading methods (``place_market_order``, ``close_position``,
``modify_sl_tp``) are placeholders here — step 3.4 wires them to real
cTrader protobuf requests.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from ctrader_open_api import Client, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
)
from twisted.internet import reactor as _reactor

# Twisted's reactor is a process-global singleton typed as the bare base
# module by its partial stubs. Alias as Any so attribute access doesn't
# trip mypy strict.
reactor: Any = _reactor

logger = logging.getLogger(__name__)


class CtraderBridge:
    """Async-friendly facade over a Twisted-driven cTrader trading client.

    Lifecycle:
        1. ``connect_with_retry()`` — exponential-backoff connect + app auth.
        2. ``authenticate()``       — bind the connection to one account
           via OAuth access token + ctid_trader_account_id.
        3. ``disconnect()``         — graceful shutdown.

    Idempotent ``connect_with_retry()`` and ``disconnect()``: calling
    twice is safe.
    """

    def __init__(
        self,
        account_id: str,
        access_token: str,
        ctid_trader_account_id: int,
        client_id: str,
        client_secret: str,
        host: str = "live.ctraderapi.com",
        port: int = 5035,
    ) -> None:
        self._account_id = account_id
        self._access_token = access_token
        self._ctid_trader_account_id = ctid_trader_account_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._host = host
        self._port = port
        self._client: Any = None  # ctrader_open_api.Client; untyped lib
        self._reactor_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = asyncio.Event()  # TCP up + app auth ok
        self._authenticated = asyncio.Event()  # account auth ok

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated.is_set()

    # ---------- public API ----------

    async def connect_with_retry(
        self,
        max_attempts: int = 10,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ) -> None:
        """Connect + app-auth + account-auth, retrying with exponential backoff.

        Each attempt has a 30s timeout for the connect/app-auth phase.
        Sleep doubles between attempts up to ``max_backoff``. After
        ``max_attempts`` consecutive failures, the underlying RuntimeError
        propagates so ``main.amain`` can decide whether to crash or fall
        back to a degraded mode (today: it crashes — process restart by
        whatever supervisor is running it is acceptable).
        """
        backoff = initial_backoff
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                await self._start()
                await self._authenticate()
                logger.info(
                    "cTrader bridge ready for account=%s (attempt %d/%d)",
                    self._account_id,
                    attempt,
                    max_attempts,
                )
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "cTrader connect attempt %d/%d failed: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                # Tear down the partial state so the next attempt starts clean.
                await self.disconnect()
                if attempt == max_attempts:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
        raise RuntimeError(f"cTrader connect failed after {max_attempts} attempts: {last_exc}")

    async def disconnect(self) -> None:
        """Shut down client + reactor. Safe to call when not started."""
        if self._client is None:
            self._connected.clear()
            self._authenticated.clear()
            return
        client = self._client
        try:
            reactor.callFromThread(client.stopService)
        except Exception:
            logger.exception("Error stopping cTrader client")
        try:
            if reactor.running:
                reactor.callFromThread(reactor.stop)
        except Exception:
            logger.exception("Error stopping Twisted reactor")
        if self._reactor_thread and self._reactor_thread.is_alive():
            self._reactor_thread.join(timeout=5.0)
        self._client = None
        self._reactor_thread = None
        self._connected.clear()
        self._authenticated.clear()
        logger.info("cTrader bridge stopped for account=%s", self._account_id)

    # ---------- step-3.4 placeholders ----------

    async def place_market_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("step 3.4 will wire ProtoOANewOrderReq (market)")

    async def place_limit_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("step 3.4 will wire ProtoOANewOrderReq (limit)")

    async def place_stop_order(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("step 3.4 will wire ProtoOANewOrderReq (stop)")

    async def close_position(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("step 3.4 will wire ProtoOAClosePositionReq")

    async def modify_sl_tp(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError("step 3.4 will wire ProtoOAAmendPositionSLTPReq")

    async def subscribe_execution_events(self) -> None:
        raise NotImplementedError("step 3.5 will subscribe ProtoOAExecutionEvent")

    # ---------- private lifecycle ----------

    async def _start(self) -> None:
        """Open TCP + run app-auth. Idempotent."""
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
            # Twisted's reactor is a process singleton; start only if no
            # other code already started it.
            if not reactor.running:
                reactor.run(installSignalHandlers=False)

        self._reactor_thread = threading.Thread(
            target=run_reactor,
            daemon=True,
            name=f"ctrader-reactor-{self._account_id}",
        )
        self._reactor_thread.start()

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=30.0)
        except TimeoutError as e:
            raise RuntimeError(
                f"cTrader connect/app-auth timed out after 30s ({self._host}:{self._port})"
            ) from e

        logger.info(
            "CtraderBridge connected to %s:%s (account=%s)",
            self._host,
            self._port,
            self._account_id,
        )

    async def _authenticate(self) -> None:
        """Account-auth step: bind the connection to a cTrader trading account."""
        if self._client is None or not self._connected.is_set():
            raise RuntimeError("CtraderBridge not started")
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self._ctid_trader_account_id
        req.accessToken = self._access_token
        await self._send_and_wait(req, timeout=30.0)
        self._authenticated.set()
        logger.info(
            "CtraderBridge authenticated for account=%s (ctid=%d)",
            self._account_id,
            self._ctid_trader_account_id,
        )

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
        """Step 3.5 will route ProtoOAExecutionEvent here for cascade events."""
        # Step 3.3 only needs connect + heartbeat; unsolicited messages
        # are logged at debug level so they're visible during smoke testing
        # without flooding production logs.
        logger.debug(
            "cTrader unsolicited message: payloadType=%s", getattr(message, "payloadType", None)
        )

    # ---------- async <-> Twisted bridge ----------

    async def _send_and_wait(self, message: Any, timeout: float = 30.0) -> Any:
        """Send a request through Twisted and await the response in asyncio.

        Same shape as ``MarketDataService._send_and_wait`` (server, Phase 2.1):
        the cTrader library delivers responses as a ``ProtoMessage`` wrapper;
        we unwrap once via ``Protobuf.extract`` so callers always see the
        already-decoded inner protobuf.
        """
        if self._loop is None or self._client is None:
            raise RuntimeError("CtraderBridge not started")
        client = self._client
        loop = self._loop
        future: asyncio.Future[Any] = loop.create_future()

        def on_success(response: Any) -> None:
            if not future.done():
                loop.call_soon_threadsafe(future.set_result, response)

        def on_error(failure: Any) -> None:
            if not future.done():
                loop.call_soon_threadsafe(
                    future.set_exception,
                    RuntimeError(f"cTrader request failed: {failure}"),
                )

        def send_in_reactor() -> None:
            d = client.send(message)
            d.addCallback(on_success)
            d.addErrback(on_error)

        reactor.callFromThread(send_in_reactor)
        wrapper = await asyncio.wait_for(future, timeout=timeout)
        return Protobuf.extract(wrapper)
