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

Step 4.1 covered ONLY the surface the skeleton needed (connect + health).
Step 4.2 extends the stub with the bits the action handlers + symbol-sync
publisher consume: ``symbols_get``, ``symbol_info``, ``symbol_select``,
``order_send``, ``positions_get`` plus the trade-action / order-type /
position-type / order-filling / retcode constants. Stay disciplined —
extend only when a handler imports the symbol; do not pre-emptively grow
the surface.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# ----- MT5 enum constants (mirror real MT5 lib values) -----

# Order in MT5 docs: NETTING = 0, EXCHANGE = 1, RETAIL_HEDGING = 2.
ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

# Trade action types (mt5.TRADE_ACTION_*). Step 4.2 only needs DEAL.
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6  # for completeness — not exercised in 4.2

# Order direction.
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

# Filling mode (IOC tried first, FOK as fallback per IOC→FOK retry).
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

# Position direction (mirrors order side semantics for opened positions).
POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

# Symbol trade mode — only FULL is tradeable; everything else is filtered.
SYMBOL_TRADE_MODE_FULL = 4

# Retcodes consumed by retcode_mapping.RETCODE_MAP. Duplicated as
# module-level ints so production code can compare ``result.retcode ==
# mt5.TRADE_RETCODE_DONE`` without pulling in the mapping module.
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_POSITION_NOT_FOUND = 10027
TRADE_RETCODE_UNSUPPORTED_FILLING = 10030

# Step 4.3a — deal entry direction. Used by the position-monitor close
# enrichment to pick the deal that *closed* the position out of a list
# that may also contain the original IN deal + IN-OUT partials.
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2


# ----- API return shapes -----


class AccountInfo(NamedTuple):
    """Subset of the real ``mt5.account_info()`` NamedTuple.

    Step 4.1 added: ``login`` / ``balance`` / ``currency`` / ``margin_mode``
    / ``leverage`` / ``server`` (connect-time fields).

    Step 4.4 added: ``equity`` / ``margin`` / ``margin_free`` so the
    ``AccountInfoPublisher`` can write balance/equity/margin/free_margin
    to ``account:exness:{account_id}`` for the server's position tracker
    + the frontend's account-status bar. Defaults preserve every
    existing call site (``AccountInfo(login=…, balance=…, …)`` without
    the new kwargs still works)."""

    login: int
    balance: float
    currency: str
    margin_mode: int
    leverage: int
    server: str
    equity: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0


class TerminalInfo(NamedTuple):
    """Subset of the real ``mt5.terminal_info()`` NamedTuple."""

    connected: bool
    trade_allowed: bool
    name: str


class SymbolInfo(NamedTuple):
    """Subset of ``mt5.symbol_info(name)`` consumed by step 4.2.

    Real MT5 ``SymbolInfo`` exposes ~40 fields; we keep only the ones the
    symbol-sync publisher + action handlers consume so a future stub-vs-
    real type drift is easy to spot. ``bid``/``ask`` carry the latest
    quote so the action handlers can derive a market price without an
    extra ``mt5.symbol_info_tick`` call.
    """

    name: str
    trade_contract_size: float
    digits: int
    point: float
    volume_min: float
    volume_step: float
    volume_max: float
    currency_profit: str
    trade_mode: int
    bid: float
    ask: float


class OrderSendResult(NamedTuple):
    """Subset of ``mt5.order_send(request)`` result. Real MT5 result has
    more fields (request_id, external_id, etc.); we keep only the ones
    the handler reads so the response schema stays narrow."""

    retcode: int
    deal: int
    order: int
    volume: float
    price: float
    bid: float
    ask: float
    comment: str


class Position(NamedTuple):
    """Subset of ``mt5.positions_get()`` entries used by the close handler
    + position-monitor poll loop. Step 4.3 added ``sl`` / ``tp`` so the
    monitor can detect terminal-side SL/TP modifications."""

    ticket: int
    symbol: str
    type: int  # POSITION_TYPE_BUY / POSITION_TYPE_SELL
    volume: float
    price_open: float
    magic: int
    sl: float = 0.0
    tp: float = 0.0


class Deal(NamedTuple):
    """Subset of ``mt5.history_deals_get()`` entries.

    Step 4.3a uses these to enrich ``position_closed_external`` events
    with the actual broker fill details (close_price, realized profit,
    commission, swap). ``reason`` is included in the shape because
    Phase 5+ may parse it; today the close-reason classification runs
    purely off the client-side ``CmdLedger``.
    """

    ticket: int
    order: int
    time: int           # epoch seconds
    time_msc: int       # epoch milliseconds
    type: int           # ORDER_TYPE_BUY / ORDER_TYPE_SELL
    entry: int          # DEAL_ENTRY_IN / DEAL_ENTRY_OUT / DEAL_ENTRY_INOUT
    magic: int
    position_id: int
    reason: int
    volume: float
    price: float
    commission: float
    swap: float
    profit: float
    fee: float
    symbol: str
    comment: str


# ----- Test-controllable module state -----

_DEFAULT_ACCOUNT_INFO = AccountInfo(
    login=12345678,
    balance=10000.0,
    currency="USD",
    margin_mode=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
    leverage=500,
    server="Exness-Stub",
    equity=10000.0,
    margin=0.0,
    margin_free=10000.0,
)
_DEFAULT_TERMINAL_INFO = TerminalInfo(connected=True, trade_allowed=True, name="stub")

_state: dict[str, Any] = {
    "connected": False,
    "init_should_fail": False,
    "account_info": _DEFAULT_ACCOUNT_INFO,
    "terminal_info": _DEFAULT_TERMINAL_INFO,
    "last_error": (0, "no error"),
    # Step 4.2 — set via ``set_state_for_tests``. Defaults are empty so an
    # unconfigured test surface never quietly succeeds.
    "symbols_get": (),               # tuple[SymbolInfo, ...]
    "symbol_info": {},               # dict[str, SymbolInfo]
    "symbol_select_calls": [],       # list[tuple[str, bool]] for assertions
    "order_send_response": None,     # OrderSendResult | Callable | list[OrderSendResult]
    "order_send_calls": [],          # list[dict] for assertions
    "order_send_raises": None,       # Exception to raise instead of returning
    "positions_get": (),             # tuple[Position, ...]
    "positions_get_raises": None,    # Exception | None — for monitor tests
    # Step 4.3a — history_deals_get state
    "history_deals_get": [],         # list[Deal]
    "history_deals_get_raises": None,
    # Step 4.4 — terminal_info raises slot for the position-monitor gate test.
    "terminal_info_raises": False,
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
    """Mirror of ``mt5.terminal_info()``.

    Step 4.4 added ``terminal_info_raises`` (test-only) so the position
    monitor's terminal-info gate can assert it survives a transient MT5
    exception without crashing the poll loop. ``connected=False`` is
    distinct: the gate skips the cycle and preserves the snapshot."""
    if _state.get("terminal_info_raises", False):
        raise RuntimeError("stub: terminal_info forced to raise")
    if not _state["connected"]:
        return None
    info: TerminalInfo | None = _state["terminal_info"]
    return info


def last_error() -> tuple[int, str]:
    """Mirror of ``mt5.last_error()`` — (code, message) tuple."""
    err: tuple[int, str] = _state["last_error"]
    return err


# ----- API surface used by step 4.2 (symbol_sync / action_handlers) -----


def symbols_get() -> tuple[SymbolInfo, ...]:
    """Mirror of ``mt5.symbols_get()`` — returns every symbol the broker
    exposes. Real MT5 returns a tuple."""
    syms: tuple[SymbolInfo, ...] = _state["symbols_get"]
    return syms


def symbol_info(name: str) -> SymbolInfo | None:
    """Mirror of ``mt5.symbol_info(name)`` — returns ``None`` when the
    name is not in MarketWatch / not exposed by the broker."""
    return _state["symbol_info"].get(name)  # type: ignore[no-any-return]


def symbol_select(name: str, enable: bool = True) -> bool:
    """Mirror of ``mt5.symbol_select(name, enable)`` — returns True on
    success. Test-side records every call for assertions."""
    _state["symbol_select_calls"].append((name, enable))
    return True


def order_send(request: dict[str, Any]) -> OrderSendResult | None:
    """Mirror of ``mt5.order_send(request)`` — runs the test-controllable
    response.

    State semantics:
      - ``order_send_raises`` — if set, raises that exception (used by
        the handler-exception tests).
      - ``order_send_response`` — if a list, pops the next response
        (used by IOC→FOK retry tests). If a single ``OrderSendResult``,
        returns it for every call. If ``None``, returns a synthetic
        ``DONE`` result so happy-path lifecycle tests don't have to
        configure anything.

    Records the request dict in ``order_send_calls`` for assertions.
    """
    _state["order_send_calls"].append(dict(request))
    if _state["order_send_raises"] is not None:
        raise _state["order_send_raises"]
    resp = _state["order_send_response"]
    if isinstance(resp, list):
        return resp.pop(0) if resp else None
    if resp is not None:
        return resp  # type: ignore[no-any-return]
    return OrderSendResult(
        retcode=TRADE_RETCODE_DONE,
        deal=12345,
        order=67890,
        volume=float(request.get("volume", 0.01)),
        price=float(request.get("price", 1.0)),
        bid=float(request.get("price", 1.0)) - 0.0001,
        ask=float(request.get("price", 1.0)) + 0.0001,
        comment="stub",
    )


def positions_get(
    *, ticket: int | None = None, symbol: str | None = None
) -> tuple[Position, ...]:
    """Mirror of ``mt5.positions_get(ticket=..., symbol=...)``.

    Real MT5 supports both keyword filters; we implement the same
    semantics so the close handler doesn't have to special-case the
    stub. ``ticket`` filter is used by ``_handle_close``.

    ``positions_get_raises`` (test-only) lets the monitor tests assert
    the loop survives a transient MT5 exception."""
    if _state["positions_get_raises"] is not None:
        raise _state["positions_get_raises"]
    positions = tuple(_state["positions_get"])
    if ticket is not None:
        return tuple(p for p in positions if p.ticket == ticket)
    if symbol is not None:
        return tuple(p for p in positions if p.symbol == symbol)
    return positions


def history_deals_get(
    *, position: int | None = None, **_kwargs: object
) -> tuple[Deal, ...]:
    """Mirror of ``mt5.history_deals_get(position=ticket, ...)``.

    Real MT5 supports several keyword filters (``date_from`` /
    ``date_to`` / ``group`` / ``ticket`` / ``position``); we implement
    only ``position`` because that's the single filter step 4.3a's
    enrichment uses. The ``**_kwargs`` swallow keeps a future caller
    that adds extra filters from blowing up unexpectedly.
    """
    if _state["history_deals_get_raises"] is not None:
        raise _state["history_deals_get_raises"]
    deals: list[Deal] = list(_state["history_deals_get"])
    if position is not None:
        return tuple(d for d in deals if d.position_id == position)
    return tuple(deals)


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
    _state["symbols_get"] = ()
    _state["symbol_info"] = {}
    _state["symbol_select_calls"] = []
    _state["order_send_response"] = None
    _state["order_send_calls"] = []
    _state["order_send_raises"] = None
    _state["positions_get"] = ()
    _state["positions_get_raises"] = None
    _state["history_deals_get"] = []
    _state["history_deals_get_raises"] = None
    _state["terminal_info_raises"] = False


# ----- Phase 4.4: terminal_info gate test helpers -----


def _set_terminal_connected_for_tests(connected: bool) -> None:
    """Toggle ``terminal_info().connected`` so the position monitor's
    gate test can simulate a transient broker disconnect without taking
    the rest of the stub state offline.

    Note: ``connected=True`` here also requires ``_state["connected"]``
    (set by a successful ``initialize()``) for ``terminal_info()`` to
    return non-None. Tests that exercise the gate should call
    ``initialize()`` first."""
    current: TerminalInfo = _state["terminal_info"]
    _state["terminal_info"] = current._replace(connected=connected)


def _set_terminal_info_raises(should_raise: bool) -> None:
    """Toggle the ``terminal_info()`` raise switch."""
    _state["terminal_info_raises"] = bool(should_raise)


# ----- Phase 4.3: position-monitor test helpers -----
#
# These mutate the ``positions_get`` state in place so a single test can
# simulate the full lifecycle (open → SL/TP modify → manual close)
# without rebuilding the whole list each time. The plain ``set_state``
# entry above still works for tests that only need a static snapshot.


def _set_positions_for_tests(positions: list[Position]) -> None:
    """Replace the positions_get response with a fresh list."""
    _state["positions_get"] = list(positions)


def _mutate_position_for_tests(ticket: int, **field_updates: object) -> None:
    """Update fields on the position with ``ticket`` (NamedTuple is
    immutable, so we ``_replace`` and write back). Used to simulate a
    terminal-side SL/TP edit between two monitor polls.

    Raises ``ValueError`` when ``ticket`` is not in the current
    positions list — surfaces a malformed test fixture loudly.
    """
    positions = list(_state["positions_get"])
    for i, pos in enumerate(positions):
        if pos.ticket == ticket:
            positions[i] = pos._replace(**field_updates)
            _state["positions_get"] = positions
            return
    raise ValueError(
        f"position with ticket {ticket} not found in stub state"
    )


def _remove_position_for_tests(ticket: int) -> None:
    """Drop ``ticket`` from positions_get (simulates a manual / SL-hit
    close happening between monitor polls)."""
    _state["positions_get"] = [
        p for p in _state["positions_get"] if p.ticket != ticket
    ]


def _set_history_deals_for_tests(deals: list[Deal]) -> None:
    """Inject a deals catalogue for ``history_deals_get`` filtering.
    Tests usually populate this in tandem with ``_remove_position_for_tests``
    so the close-enrichment path resolves the broker fill data."""
    _state["history_deals_get"] = list(deals)
