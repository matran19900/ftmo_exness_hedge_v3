"""Tests for ``retcode_mapping.map_ctrader_error``.

The mapping is a small lookup table — these tests just lock the
contract so a future expansion doesn't silently rename / drop a code.
"""

from __future__ import annotations

import pytest

from ftmo_client.retcode_mapping import (
    CTRADER_ERROR_CODES,
    CTRADER_ORDER_ERROR_CODES,
    map_ctrader_error,
)


@pytest.mark.parametrize(
    ("input_code", "expected"),
    [
        ("AUTH_FAILED", "auth_failed"),
        ("ACCOUNT_NOT_AUTHORIZED", "auth_failed"),
        ("TRADING_DISABLED", "trading_disabled"),
        ("POSITION_NOT_FOUND", "position_not_found"),
        ("INVALID_REQUEST", "invalid_request"),
    ],
)
def test_transport_error_codes_map(input_code: str, expected: str) -> None:
    assert map_ctrader_error(input_code) == expected


@pytest.mark.parametrize(
    ("input_code", "expected"),
    [
        ("MARKET_CLOSED", "market_closed"),
        ("NOT_ENOUGH_MONEY", "not_enough_money"),
        ("INVALID_VOLUME", "invalid_volume"),
        ("TRADING_BAD_VOLUME", "invalid_volume"),
        ("INVALID_STOPS_LEVEL", "invalid_sl_distance"),
        ("PRICE_OFF", "price_off"),
    ],
)
def test_order_error_codes_map(input_code: str, expected: str) -> None:
    assert map_ctrader_error(input_code) == expected


def test_unknown_code_falls_back_to_broker_error() -> None:
    assert map_ctrader_error("SOME_FUTURE_CTRADER_CODE") == "broker_error"


def test_empty_string_returns_fallback() -> None:
    """Empty input shouldn't false-hit any map; returns fallback directly."""
    assert map_ctrader_error("") == "broker_error"
    assert map_ctrader_error("", fallback="other") == "other"


def test_custom_fallback_used_only_on_unknown_input() -> None:
    """A known code wins over the custom fallback."""
    assert map_ctrader_error("AUTH_FAILED", fallback="something_else") == "auth_failed"


def test_mapping_tables_have_distinct_keys() -> None:
    """The two tables should not overlap on key names — overlap would make
    the lookup precedence (transport-first) load-bearing for no reason."""
    overlap = set(CTRADER_ERROR_CODES) & set(CTRADER_ORDER_ERROR_CODES)
    assert overlap == set(), f"unexpected key overlap: {overlap}"
