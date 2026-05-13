"""Linux-compatible stub for the MetaTrader5 package.

The real ``MetaTrader5`` package is Windows-only (``pip install`` fails
outright on Linux because no wheel exists). This stub provides:

  - Module-level functions (``initialize``, ``shutdown``, ``account_info``,
    ``terminal_info``, ``last_error``) that mirror the MetaTrader5 API
    surface step 4.1 needs.
  - Module-level constants for the margin-mode enum so production code
    can branch on ``ACCOUNT_MARGIN_MODE_RETAIL_HEDGING`` without
    conditioning on platform.
  - Test-controllable state via ``set_state_for_tests`` /
    ``reset_state_for_tests``.

Production code on Windows imports ``MetaTrader5`` directly; tests +
Linux CI import this stub. The selection happens in ``main.py`` per
``sys.platform``.

Step 4.1 covers ONLY the surface the skeleton needs (connect + health).
Step 4.2+ extends the stub with ``positions_get``, ``order_send``,
``history_deals_get``, ``symbol_info``, etc., when those handlers land.
DO NOT pre-emptively expand the stub here — keep it tracking what's
actually consumed.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# ----- MT5 enum constants (mirror real MT5 lib values) -----

# Order in MT5 docs: NETTING = 0, EXCHANGE = 1, RETAIL_HEDGING = 2.
ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2


# ----- API return shapes -----


class AccountInfo(NamedTuple):
    """Subset of the real ``mt5.account_info()`` NamedTuple — only the
    fields step 4.1 consumes. Step 4.2+ may extend."""

    login: int
    balance: float
    currency: str
    margin_mode: int
    leverage: int
    server: str


class TerminalInfo(NamedTuple):
    """Subset of the real ``mt5.terminal_info()`` NamedTuple."""

    connected: bool
    trade_allowed: bool
    name: str


# ----- Test-controllable module state -----

_DEFAULT_ACCOUNT_INFO = AccountInfo(
    login=12345678,
    balance=10000.0,
    currency="USD",
    margin_mode=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
    leverage=500,
    server="Exness-Stub",
)
_DEFAULT_TERMINAL_INFO = TerminalInfo(connected=True, trade_allowed=True, name="stub")

_state: dict[str, Any] = {
    "connected": False,
    "init_should_fail": False,
    "account_info": _DEFAULT_ACCOUNT_INFO,
    "terminal_info": _DEFAULT_TERMINAL_INFO,
    "last_error": (0, "no error"),
}


# ----- API surface used by step 4.1 (bridge_service.connect / health_check) -----


def initialize(*_args: object, **_kwargs: object) -> bool:
    """Mirror of ``mt5.initialize(login=..., password=..., server=..., path=...)``.

    Returns True on success, False on failure. Real MT5 lib populates
    ``last_error()`` on failure; the stub does the same when
    ``init_should_fail`` is set.
    """
    if _state["init_should_fail"]:
        _state["last_error"] = (-10003, "Stub: init_should_fail=True")
        return False
    _state["connected"] = True
    _state["last_error"] = (0, "no error")
    return True


def shutdown() -> None:
    """Mirror of ``mt5.shutdown()`` — idempotent; safe to call when not
    connected."""
    _state["connected"] = False


def account_info() -> AccountInfo | None:
    """Mirror of ``mt5.account_info()`` — returns None when MT5 is not
    connected or the lib can't read account state."""
    if not _state["connected"]:
        return None
    info: AccountInfo | None = _state["account_info"]
    return info


def terminal_info() -> TerminalInfo | None:
    """Mirror of ``mt5.terminal_info()``."""
    if not _state["connected"]:
        return None
    info: TerminalInfo | None = _state["terminal_info"]
    return info


def last_error() -> tuple[int, str]:
    """Mirror of ``mt5.last_error()`` — (code, message) tuple."""
    err: tuple[int, str] = _state["last_error"]
    return err


# ----- Test helpers (NOT part of the real MetaTrader5 API) -----


def set_state_for_tests(**kwargs: object) -> None:
    """Override stub internal state. Production code MUST NOT call this."""
    for k, v in kwargs.items():
        if k not in _state:
            raise KeyError(f"unknown stub state field: {k}")
        _state[k] = v


def reset_state_for_tests() -> None:
    """Restore stub defaults. Used as a per-test autouse fixture in conftest."""
    _state["connected"] = False
    _state["init_should_fail"] = False
    _state["account_info"] = _DEFAULT_ACCOUNT_INFO
    _state["terminal_info"] = _DEFAULT_TERMINAL_INFO
    _state["last_error"] = (0, "no error")
