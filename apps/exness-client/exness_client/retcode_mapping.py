"""MT5 retcode → server vocab mapping (Phase 4.2).

Per ``docs/mt5-execution-events.md`` §3 the server-side vocab is

  - ``status``: one of ``filled`` | ``rejected`` | ``error`` | ``requote``
  - ``reason``: short snake_case slug for log + UI mapping
  - ``retry_strategy``: ``no_retry`` | ``retry_alternate_filling`` |
    ``retry_fresh_tick`` — read by the action handler before it decides
    whether to re-issue the order with different parameters

Centralising the mapping here keeps the server free to change its
externally-facing vocab without touching every retcode site in the
client. Unknown retcodes fall through to ``error/unknown_retcode_{n}``
so an unexpected broker response surfaces loudly in logs without
crashing the client loop.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

# Retcode integer constants. Duplicated from ``mt5_stub`` so production
# code can ``from .retcode_mapping import TRADE_RETCODE_DONE`` without
# pulling in the stub-or-real selection logic.
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


RetryStrategy = Literal[
    "no_retry", "retry_alternate_filling", "retry_fresh_tick"
]
OutcomeStatus = Literal["filled", "rejected", "error", "requote"]


class RetcodeOutcome(NamedTuple):
    status: OutcomeStatus
    reason: str
    retry_strategy: RetryStrategy


RETCODE_MAP: dict[int, RetcodeOutcome] = {
    TRADE_RETCODE_DONE: RetcodeOutcome("filled", "ok", "no_retry"),
    TRADE_RETCODE_REJECT: RetcodeOutcome(
        "rejected", "generic_reject", "no_retry"
    ),
    TRADE_RETCODE_INVALID_VOLUME: RetcodeOutcome(
        "rejected", "invalid_volume", "no_retry"
    ),
    TRADE_RETCODE_INVALID_PRICE: RetcodeOutcome(
        "requote", "invalid_price_stale", "retry_fresh_tick"
    ),
    TRADE_RETCODE_INVALID_STOPS: RetcodeOutcome(
        "rejected", "invalid_stops", "no_retry"
    ),
    TRADE_RETCODE_MARKET_CLOSED: RetcodeOutcome(
        "rejected", "market_closed", "no_retry"
    ),
    TRADE_RETCODE_NO_MONEY: RetcodeOutcome(
        "rejected", "insufficient_margin", "no_retry"
    ),
    TRADE_RETCODE_POSITION_NOT_FOUND: RetcodeOutcome(
        "error", "position_not_found", "no_retry"
    ),
    TRADE_RETCODE_UNSUPPORTED_FILLING: RetcodeOutcome(
        "requote", "unsupported_filling", "retry_alternate_filling"
    ),
    TRADE_RETCODE_REQUOTE: RetcodeOutcome(
        "requote", "price_moved", "retry_fresh_tick"
    ),
}


def map_retcode(retcode: int) -> RetcodeOutcome:
    """Map MT5 ``retcode`` integer to the server-side outcome triple.

    Unknown retcodes return ``error / unknown_retcode_{retcode} / no_retry``
    so the operator sees the raw integer in logs while we never silently
    swallow an unexpected broker response.
    """
    return RETCODE_MAP.get(
        retcode,
        RetcodeOutcome(
            "error", f"unknown_retcode_{retcode}", "no_retry"
        ),
    )
