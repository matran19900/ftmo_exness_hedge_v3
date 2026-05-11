"""cTrader trading connection bridge for FTMO client.

NOTE: This file duplicates Twisted-asyncio bridge mechanics from
server/app/services/market_data.py (Phase 2.1). Duplication is
intentional in Phase 3 — extraction to shared/ will happen in Phase 4
or 5 once the MT5 adapter (Exness) shows the proper abstraction shape.
Do NOT extract prematurely.

Per-account: each FTMO client process drives ONE FTMO account. The
bridge is constructed with that account's access_token and
ctid_trader_account_id and immediately runs application + account auth
on connect.

Step 3.4 fills in the trading methods (``place_market_order``,
``place_limit_order``, ``place_stop_order``, ``close_position``,
``modify_sl_tp``) with real cTrader protobuf calls. Each method:

  - Maps lots → cTrader volume units using ``lot_size`` from the
    caller-supplied symbol_config (cTrader convention: volume field is
    in 0.01 base-currency units, equal to lots * lot_size).
  - Sends prices as raw doubles (cTrader uses double-precision floats
    for stopLoss / takeProfit / limitPrice / stopPrice — only spot
    tick/trendbar prices use the int*10^5 wire format from D-032).
  - Passes the caller's ``client_msg_id`` (the request_id from the
    cmd_stream entry) to cTrader's ``clientMsgId`` so a retried command
    receives the same response back from the broker.
  - Returns a TypedDict (``OrderPlacementResult`` / ``ClosePositionResult``
    / ``ModifySltpResult``) with all fields as strings so the caller
    can XADD them straight into the resp_stream.

cTrader response handling: a successful ProtoOANewOrderReq triggers a
ProtoOAExecutionEvent with ``executionType=ORDER_FILLED`` (market) or
``ORDER_ACCEPTED`` (limit/stop). Failures come back as
``ORDER_REJECTED`` execution events OR ProtoOAErrorRes (auth/transport)
OR ProtoOAOrderErrorEvent (business-rule rejection). All three are
mapped to error fields via ``retcode_mapping.map_ctrader_error``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Literal, TypedDict

from ctrader_open_api import Client, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAApplicationAuthReq,
    ProtoOAClosePositionReq,
    ProtoOAErrorRes,
    ProtoOAExecutionEvent,
    ProtoOANewOrderReq,
    ProtoOAOrderErrorEvent,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAExecutionType,
    ProtoOAOrderType,
    ProtoOATradeSide,
)
from twisted.internet import reactor as _reactor

from ftmo_client.retcode_mapping import map_ctrader_error

# Twisted's reactor is a process-global singleton typed as the bare base
# module by its partial stubs. Alias as Any so attribute access doesn't
# trip mypy strict.
reactor: Any = _reactor

logger = logging.getLogger(__name__)

# Default timeout for trading-call round-trips. cTrader typically replies
# within ~200ms but slow market hours + retry paths can push it to ~5s; 30s
# is a generous ceiling that still lets a stuck request fail before the
# command_loop's next XREADGROUP block window.
_TRADING_TIMEOUT_SECONDS = 30.0


class OrderPlacementResult(TypedDict):
    """Shape returned by ``place_market_order`` / ``place_limit_order`` /
    ``place_stop_order``. All values are stringified so the caller can
    XADD them straight into the resp_stream without additional formatting.
    """

    success: bool
    broker_order_id: str  # cTrader positionId (filled) or orderId (pending)
    fill_price: str  # filled price as string; "" if pending
    fill_time: str  # epoch ms as string; "" if pending
    commission: str  # raw cTrader value (moneyDigits-scaled); "" if unknown
    error_code: str  # "" on success; mapped retcode on error
    error_msg: str  # "" on success; human-readable cTrader reason on error


class ClosePositionResult(TypedDict):
    success: bool
    close_price: str
    close_time: str
    realized_pnl: str  # raw cTrader moneyDigits-scaled value; "" if unknown
    error_code: str
    error_msg: str


class ModifySltpResult(TypedDict):
    success: bool
    new_sl: str
    new_tp: str
    error_code: str
    error_msg: str


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

    # ---------- trading methods ----------

    async def place_market_order(
        self,
        *,
        symbol_id: int,
        side: Literal["buy", "sell"],
        volume_lots: float,
        lot_size: int,
        sl_price: float,
        tp_price: float,
        client_msg_id: str,
    ) -> OrderPlacementResult:
        """Place a market order. Returns immediately after fill / rejection."""
        req = self._build_new_order_req(
            symbol_id=symbol_id,
            order_type=ProtoOAOrderType.MARKET,
            side=side,
            volume_lots=volume_lots,
            lot_size=lot_size,
            sl_price=sl_price,
            tp_price=tp_price,
            entry_price=0.0,  # ignored for market orders
        )
        response = await self._send_and_wait(
            req, timeout=_TRADING_TIMEOUT_SECONDS, client_msg_id=client_msg_id
        )
        return self._parse_order_placement(response)

    async def place_limit_order(
        self,
        *,
        symbol_id: int,
        side: Literal["buy", "sell"],
        volume_lots: float,
        lot_size: int,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        client_msg_id: str,
    ) -> OrderPlacementResult:
        """Place a limit order. Returns after broker accepts / rejects it."""
        req = self._build_new_order_req(
            symbol_id=symbol_id,
            order_type=ProtoOAOrderType.LIMIT,
            side=side,
            volume_lots=volume_lots,
            lot_size=lot_size,
            sl_price=sl_price,
            tp_price=tp_price,
            entry_price=entry_price,
        )
        response = await self._send_and_wait(
            req, timeout=_TRADING_TIMEOUT_SECONDS, client_msg_id=client_msg_id
        )
        return self._parse_order_placement(response)

    async def place_stop_order(
        self,
        *,
        symbol_id: int,
        side: Literal["buy", "sell"],
        volume_lots: float,
        lot_size: int,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        client_msg_id: str,
    ) -> OrderPlacementResult:
        """Place a stop order. Returns after broker accepts / rejects it."""
        req = self._build_new_order_req(
            symbol_id=symbol_id,
            order_type=ProtoOAOrderType.STOP,
            side=side,
            volume_lots=volume_lots,
            lot_size=lot_size,
            sl_price=sl_price,
            tp_price=tp_price,
            entry_price=entry_price,
        )
        response = await self._send_and_wait(
            req, timeout=_TRADING_TIMEOUT_SECONDS, client_msg_id=client_msg_id
        )
        return self._parse_order_placement(response)

    async def close_position(
        self,
        *,
        position_id: int,
        volume_lots: float,
        lot_size: int,
        client_msg_id: str,
    ) -> ClosePositionResult:
        """Close (fully or partially) an existing position by cTrader positionId."""
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self._ctid_trader_account_id
        req.positionId = position_id
        req.volume = int(volume_lots * lot_size)
        response = await self._send_and_wait(
            req, timeout=_TRADING_TIMEOUT_SECONDS, client_msg_id=client_msg_id
        )
        return self._parse_close_position(response)

    async def modify_sl_tp(
        self,
        *,
        position_id: int,
        sl_price: float,
        tp_price: float,
        client_msg_id: str,
    ) -> ModifySltpResult:
        """Amend a position's stopLoss / takeProfit prices.

        Pass ``sl_price=0`` or ``tp_price=0`` to clear that side — the
        cTrader proto skips unset double fields, so the protobuf builder
        below only assigns the field when the price is positive.
        """
        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self._ctid_trader_account_id
        req.positionId = position_id
        if sl_price > 0:
            req.stopLoss = sl_price
        if tp_price > 0:
            req.takeProfit = tp_price
        response = await self._send_and_wait(
            req, timeout=_TRADING_TIMEOUT_SECONDS, client_msg_id=client_msg_id
        )
        return self._parse_modify_sl_tp(response, sl_price, tp_price)

    async def subscribe_execution_events(self) -> None:
        raise NotImplementedError("step 3.5 will subscribe ProtoOAExecutionEvent")

    # ---------- protobuf builders + response parsers ----------

    def _build_new_order_req(
        self,
        *,
        symbol_id: int,
        order_type: int,  # ProtoOAOrderType enum value
        side: Literal["buy", "sell"],
        volume_lots: float,
        lot_size: int,
        sl_price: float,
        tp_price: float,
        entry_price: float,
    ) -> ProtoOANewOrderReq:
        """Compose ProtoOANewOrderReq with the right field set for the order_type.

        Volume conversion: cTrader's ``volume`` field is in 0.01 base-currency
        units, equal to ``lots * lot_size`` where ``lot_size`` is the broker's
        cached cTrader value (e.g. EURUSD 1 lot → lot_size = 10_000_000;
        0.01 lot → volume = 100_000).

        Prices (limitPrice, stopPrice, stopLoss, takeProfit) are doubles on
        ProtoOANewOrderReq — NOT the int*10^5 wire format used for tick/
        trendbar messages (D-032). We send raw price floats here.
        """
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self._ctid_trader_account_id
        req.symbolId = symbol_id
        req.orderType = order_type
        req.tradeSide = ProtoOATradeSide.BUY if side == "buy" else ProtoOATradeSide.SELL
        req.volume = int(volume_lots * lot_size)
        if sl_price > 0:
            req.stopLoss = sl_price
        if tp_price > 0:
            req.takeProfit = tp_price
        if order_type == ProtoOAOrderType.LIMIT:
            req.limitPrice = entry_price
        elif order_type == ProtoOAOrderType.STOP:
            req.stopPrice = entry_price
        return req

    @staticmethod
    def _parse_order_placement(response: Any) -> OrderPlacementResult:
        """Translate the cTrader response into an ``OrderPlacementResult``.

        Three response shapes are handled:
          - ProtoOAExecutionEvent → inspect ``executionType``.
              * ORDER_FILLED → success with fill price / time from
                response.deal.
              * ORDER_ACCEPTED → success with broker_order_id = response.order.orderId;
                no fill data (pending order).
              * ORDER_REJECTED / ORDER_CANCEL_REJECTED → error path.
          - ProtoOAErrorRes → auth / transport error.
          - ProtoOAOrderErrorEvent → business-rule rejection.

        Anything else (defensive) → generic broker_error.
        """
        empty: OrderPlacementResult = {
            "success": False,
            "broker_order_id": "",
            "fill_price": "",
            "fill_time": "",
            "commission": "",
            "error_code": "",
            "error_msg": "",
        }

        if isinstance(response, ProtoOAExecutionEvent):
            exec_type = int(response.executionType)
            if exec_type == ProtoOAExecutionType.ORDER_FILLED:
                deal = response.deal
                position_id = int(deal.positionId) if deal.HasField("positionId") else 0
                return {
                    "success": True,
                    "broker_order_id": str(position_id),
                    "fill_price": str(float(deal.executionPrice)),
                    "fill_time": str(int(deal.executionTimestamp)),
                    "commission": str(int(deal.commission)),
                    "error_code": "",
                    "error_msg": "",
                }
            if exec_type == ProtoOAExecutionType.ORDER_ACCEPTED:
                order = response.order
                return {
                    "success": True,
                    "broker_order_id": str(int(order.orderId)),
                    "fill_price": "",
                    "fill_time": "",
                    "commission": "",
                    "error_code": "",
                    "error_msg": "",
                }
            # ORDER_REJECTED / ORDER_CANCEL_REJECTED / unexpected types.
            raw_code = str(response.errorCode) if response.HasField("errorCode") else ""
            return {
                **empty,
                "error_code": map_ctrader_error(raw_code),
                "error_msg": raw_code or f"executionType={exec_type}",
            }

        if isinstance(response, ProtoOAOrderErrorEvent):
            return {
                **empty,
                "error_code": map_ctrader_error(str(response.errorCode)),
                "error_msg": str(response.description or response.errorCode),
            }

        if isinstance(response, ProtoOAErrorRes):
            return {
                **empty,
                "error_code": map_ctrader_error(str(response.errorCode)),
                "error_msg": str(response.description or response.errorCode),
            }

        return {
            **empty,
            "error_code": "broker_error",
            "error_msg": f"unexpected response: {type(response).__name__}",
        }

    @staticmethod
    def _parse_close_position(response: Any) -> ClosePositionResult:
        """Map a cTrader response to ``ClosePositionResult``.

        On success: pull close_price + close_time from the deal sub-message.
        On error: same three response shapes as ``_parse_order_placement``.
        """
        empty: ClosePositionResult = {
            "success": False,
            "close_price": "",
            "close_time": "",
            "realized_pnl": "",
            "error_code": "",
            "error_msg": "",
        }

        if isinstance(response, ProtoOAExecutionEvent):
            exec_type = int(response.executionType)
            if exec_type == ProtoOAExecutionType.ORDER_FILLED:
                deal = response.deal
                # closePositionDetail carries realized P&L on a closing deal.
                realized = ""
                if deal.HasField("closePositionDetail"):
                    realized = str(int(deal.closePositionDetail.grossProfit))
                return {
                    "success": True,
                    "close_price": str(float(deal.executionPrice)),
                    "close_time": str(int(deal.executionTimestamp)),
                    "realized_pnl": realized,
                    "error_code": "",
                    "error_msg": "",
                }
            raw_code = str(response.errorCode) if response.HasField("errorCode") else ""
            return {
                **empty,
                "error_code": map_ctrader_error(raw_code),
                "error_msg": raw_code or f"executionType={exec_type}",
            }

        if isinstance(response, ProtoOAOrderErrorEvent):
            return {
                **empty,
                "error_code": map_ctrader_error(str(response.errorCode)),
                "error_msg": str(response.description or response.errorCode),
            }

        if isinstance(response, ProtoOAErrorRes):
            return {
                **empty,
                "error_code": map_ctrader_error(str(response.errorCode)),
                "error_msg": str(response.description or response.errorCode),
            }

        return {
            **empty,
            "error_code": "broker_error",
            "error_msg": f"unexpected response: {type(response).__name__}",
        }

    @staticmethod
    def _parse_modify_sl_tp(
        response: Any, requested_sl: float, requested_tp: float
    ) -> ModifySltpResult:
        """Map the amend response. cTrader sends an execution event with
        ``executionType=ORDER_REPLACED`` (or similar) on success; we treat
        any non-error response as success and echo back the requested
        prices for the client (the broker confirms by NOT erroring)."""
        empty: ModifySltpResult = {
            "success": False,
            "new_sl": "",
            "new_tp": "",
            "error_code": "",
            "error_msg": "",
        }

        if isinstance(response, ProtoOAExecutionEvent):
            exec_type = int(response.executionType)
            if exec_type in (
                ProtoOAExecutionType.ORDER_REPLACED,
                ProtoOAExecutionType.ORDER_ACCEPTED,
                ProtoOAExecutionType.ORDER_FILLED,
            ):
                # cTrader echoes the new prices on response.position when amending.
                new_sl = ""
                new_tp = ""
                if response.HasField("position"):
                    pos = response.position
                    if pos.HasField("stopLoss"):
                        new_sl = str(float(pos.stopLoss))
                    if pos.HasField("takeProfit"):
                        new_tp = str(float(pos.takeProfit))
                return {
                    "success": True,
                    "new_sl": new_sl or str(requested_sl),
                    "new_tp": new_tp or str(requested_tp),
                    "error_code": "",
                    "error_msg": "",
                }
            raw_code = str(response.errorCode) if response.HasField("errorCode") else ""
            return {
                **empty,
                "error_code": map_ctrader_error(raw_code),
                "error_msg": raw_code or f"executionType={exec_type}",
            }

        if isinstance(response, ProtoOAOrderErrorEvent):
            return {
                **empty,
                "error_code": map_ctrader_error(str(response.errorCode)),
                "error_msg": str(response.description or response.errorCode),
            }

        if isinstance(response, ProtoOAErrorRes):
            return {
                **empty,
                "error_code": map_ctrader_error(str(response.errorCode)),
                "error_msg": str(response.description or response.errorCode),
            }

        return {
            **empty,
            "error_code": "broker_error",
            "error_msg": f"unexpected response: {type(response).__name__}",
        }

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

    async def _send_and_wait(
        self,
        message: Any,
        timeout: float = 30.0,
        client_msg_id: str | None = None,
    ) -> Any:
        """Send a request through Twisted and await the response in asyncio.

        Same shape as ``MarketDataService._send_and_wait`` (server, Phase 2.1):
        the cTrader library delivers responses as a ``ProtoMessage`` wrapper;
        we unwrap once via ``Protobuf.extract`` so callers always see the
        already-decoded inner protobuf.

        ``client_msg_id`` is forwarded as cTrader's ``clientMsgId`` so a
        retried command (same request_id from the cmd_stream) lands in the
        broker's deduplication table and the eventual execution event
        carries the same id back — the bridge can route the response to the
        right Deferred.
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
            # cTrader's Deferred has its own ``responseTimeoutInSeconds`` (5s
            # default) — pass our ceiling so the broker-side wait matches the
            # asyncio-side wait_for, otherwise short-lived deferred timeouts
            # would error before our wait elapses.
            send_kwargs: dict[str, Any] = {"responseTimeoutInSeconds": timeout}
            if client_msg_id is not None:
                send_kwargs["clientMsgId"] = client_msg_id
            d = client.send(message, **send_kwargs)
            d.addCallback(on_success)
            d.addErrback(on_error)

        reactor.callFromThread(send_in_reactor)
        wrapper = await asyncio.wait_for(future, timeout=timeout)
        return Protobuf.extract(wrapper)
