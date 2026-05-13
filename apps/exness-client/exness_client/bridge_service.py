"""MT5 broker connection bridge.

Owns the lifecycle of the synchronous ``MetaTrader5`` Python library on
behalf of the Exness client process. Mirrors the role of
``ctrader_bridge.py`` in the FTMO client but trades Twisted-in-thread
for ``asyncio.to_thread`` because MT5 is a blocking C library, not an
async Twisted client.

Step 4.1 scope: connect, disconnect, is_connected, health_check.
Action handlers (open / close / modify) and the position monitor land
in step 4.2+.

The hedging-mode assertion at startup is non-negotiable: cascade close
logic in ``docs/phase-4-design.md §1.E`` assumes per-position handles,
which MT5 netting accounts collapse into a single net position per
symbol. A netting account would silently break cascade close — fail
fast at startup instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import ModuleType
from typing import Any

from exness_client.config import ExnessClientSettings

logger = logging.getLogger(__name__)


class MT5ConnectError(RuntimeError):
    """Raised when ``mt5.initialize`` fails or account info is unreadable.

    Carries the ``mt5.last_error()`` tuple so callers can log / publish
    the broker-side detail.
    """

    def __init__(self, message: str, last_error: tuple[int, str]) -> None:
        super().__init__(message)
        self.last_error = last_error


class MT5HedgingModeRequiredError(RuntimeError):
    """Raised when ``mt5.account_info().margin_mode`` is not
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING.

    Cascade close (path A↔E in phase-4-design §1.E) requires per-position
    handles; netting mode collapses positions into one net handle per
    symbol and breaks cascade silently. Fail-fast at startup keeps the
    operator from running a misconfigured account into production.
    """

    def __init__(self, observed_mode: int) -> None:
        super().__init__(
            f"MT5 account margin_mode={observed_mode!r} is not "
            "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING (2). Exness account must be "
            "hedging-mode for cascade close to work — see "
            "docs/phase-4-design.md §1.E."
        )
        self.observed_mode = observed_mode


class MT5BridgeService:
    """Thin async wrapper around the synchronous MetaTrader5 lib.

    All blocking calls go through ``asyncio.to_thread`` so the asyncio
    event loop driving heartbeat + command_processor stays responsive.
    Tests inject ``mt5_module=exness_client.mt5_stub``; production on
    Windows injects the real ``MetaTrader5`` package.
    """

    def __init__(self, settings: ExnessClientSettings, mt5_module: ModuleType) -> None:
        self._settings = settings
        self._mt5 = mt5_module
        self._connected = False
        self._connected_at_ms: int | None = None

    # ----- Public API -----

    async def connect(self) -> None:
        """Initialize MT5 and assert hedging mode.

        Raises ``MT5ConnectError`` if init fails or account_info is None.
        Raises ``MT5HedgingModeRequiredError`` if account is netting / exchange.
        """
        ok = await asyncio.to_thread(
            self._mt5.initialize,
            login=self._settings.mt5_login,
            password=self._settings.mt5_password.get_secret_value(),
            server=self._settings.mt5_server,
            path=self._settings.mt5_path,
        )
        if not ok:
            err = await asyncio.to_thread(self._mt5.last_error)
            logger.error("mt5.initialize failed: %s", err)
            raise MT5ConnectError("mt5.initialize returned False", err)

        info = await asyncio.to_thread(self._mt5.account_info)
        if info is None:
            err = await asyncio.to_thread(self._mt5.last_error)
            await asyncio.to_thread(self._mt5.shutdown)
            logger.error("mt5.account_info returned None: %s", err)
            raise MT5ConnectError("mt5.account_info returned None", err)

        hedging_mode = getattr(self._mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        if info.margin_mode != hedging_mode:
            await asyncio.to_thread(self._mt5.shutdown)
            raise MT5HedgingModeRequiredError(info.margin_mode)

        self._connected = True
        self._connected_at_ms = int(time.time() * 1000)
        logger.info(
            "mt5.connected: login=%s server=%s balance=%s currency=%s leverage=%s",
            info.login,
            info.server,
            info.balance,
            info.currency,
            info.leverage,
        )

    async def disconnect(self) -> None:
        """Shut down the MT5 connection. Idempotent."""
        if not self._connected:
            return
        await asyncio.to_thread(self._mt5.shutdown)
        self._connected = False
        self._connected_at_ms = None
        logger.info("mt5.disconnected")

    def is_connected(self) -> bool:
        """In-memory flag — does NOT call the MT5 lib.

        Cheap probe for ``HeartbeatLoop`` and ``CommandProcessor`` hot
        paths; the authoritative check is ``health_check``.
        """
        return self._connected

    async def health_check(self) -> dict[str, Any]:
        """Probe terminal + account info to populate the heartbeat payload.

        Returns a flat dict with five keys:
          - connected     : in-memory flag (matches ``is_connected``)
          - terminal_ok   : ``terminal_info().connected``
          - trade_allowed : ``terminal_info().trade_allowed``
          - account_login : ``account_info().login`` (0 if unavailable)
          - checked_at    : unix ms at probe time

        Failures inside the probe (lib returned None) flip the relevant
        boolean(s) to False rather than raising — the heartbeat loop
        keeps publishing so the server keeps an accurate offline read.
        """
        checked_at = int(time.time() * 1000)
        if not self._connected:
            return {
                "connected": False,
                "terminal_ok": False,
                "trade_allowed": False,
                "account_login": 0,
                "checked_at": checked_at,
            }

        terminal = await asyncio.to_thread(self._mt5.terminal_info)
        account = await asyncio.to_thread(self._mt5.account_info)
        return {
            "connected": True,
            "terminal_ok": bool(terminal and terminal.connected),
            "trade_allowed": bool(terminal and terminal.trade_allowed),
            "account_login": int(account.login) if account else 0,
            "checked_at": checked_at,
        }
