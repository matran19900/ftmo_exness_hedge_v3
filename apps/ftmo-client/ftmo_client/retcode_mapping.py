"""Map cTrader error codes to protocol ``error_code`` strings.

The values returned by ``map_ctrader_error`` are what the FTMO client
writes into ``resp_stream:ftmo:{account_id}`` so the server-side
response_handler can branch on a stable, broker-independent vocabulary.
The matching Exness mapping will live in `exness-client` (Phase 4).

Coverage is intentionally minimal here — Phase 5 hardening will expand
as real cTrader error codes surface in smoke / production. Unknown codes
fall back to ``broker_error`` (the catch-all that server treats as
non-retryable, per docs/05-redis-protocol.md R-IDM-5).
"""

from __future__ import annotations

# Codes returned by ProtoOAErrorRes (authentication / transport-level
# errors). These typically indicate the FTMO client process itself is in
# a bad state — auth expired, account not bound, account suspended.
CTRADER_ERROR_CODES = {
    "AUTH_FAILED": "auth_failed",
    "ACCOUNT_NOT_AUTHORIZED": "auth_failed",
    "CH_CLIENT_AUTH_FAILURE": "auth_failed",
    "TRADING_DISABLED": "trading_disabled",
    "ACCOUNT_DISABLED": "trading_disabled",
    "POSITION_NOT_FOUND": "position_not_found",
    "ORDER_NOT_FOUND": "order_not_found",
    "INVALID_REQUEST": "invalid_request",
    "BAD_REQUEST": "invalid_request",
}

# Codes returned by ProtoOAOrderErrorEvent or the ``errorCode`` field on
# ProtoOAExecutionEvent (business-rule rejections — the order itself was
# valid syntactically but the broker rejected it).
CTRADER_ORDER_ERROR_CODES = {
    "MARKET_CLOSED": "market_closed",
    "SYMBOL_HAS_HOLIDAY": "market_closed",
    "NOT_ENOUGH_MONEY": "not_enough_money",
    "INVALID_VOLUME": "invalid_volume",
    "TRADING_BAD_VOLUME": "invalid_volume",
    "INVALID_STOPS_LEVEL": "invalid_sl_distance",
    "INVALID_STOPS": "invalid_sl_distance",
    "PRICE_OFF": "price_off",
    "INVALID_PRICE": "price_off",
    "TRADING_FROZEN_ACCOUNT": "trading_disabled",
    "POSITION_LOCKED": "position_locked",
    "POSITION_FROZEN": "position_locked",
}


def map_ctrader_error(error_code: str, fallback: str = "broker_error") -> str:
    """Convert a cTrader error-code string to the protocol vocabulary.

    Looks up both maps (transport then order-business) and falls back to
    ``broker_error`` for anything unrecognized. An empty input returns
    ``fallback`` directly — callers that see an empty code typically had
    an unexpected response shape, not a broker reject.
    """
    if not error_code:
        return fallback
    return (
        CTRADER_ERROR_CODES.get(error_code) or CTRADER_ORDER_ERROR_CODES.get(error_code) or fallback
    )
