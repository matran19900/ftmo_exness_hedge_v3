"""Tests for ``retcode_mapping.RETCODE_MAP`` (Phase 4.2)."""

from __future__ import annotations

import pytest

from exness_client.retcode_mapping import (
    RETCODE_MAP,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_INVALID_PRICE,
    TRADE_RETCODE_INVALID_STOPS,
    TRADE_RETCODE_INVALID_VOLUME,
    TRADE_RETCODE_MARKET_CLOSED,
    TRADE_RETCODE_NO_MONEY,
    TRADE_RETCODE_POSITION_NOT_FOUND,
    TRADE_RETCODE_REJECT,
    TRADE_RETCODE_REQUOTE,
    TRADE_RETCODE_UNSUPPORTED_FILLING,
    map_retcode,
)


@pytest.mark.parametrize(
    ("retcode", "expected_status", "expected_reason", "expected_retry"),
    [
        (TRADE_RETCODE_DONE, "filled", "ok", "no_retry"),
        (TRADE_RETCODE_REJECT, "rejected", "generic_reject", "no_retry"),
        (TRADE_RETCODE_INVALID_VOLUME, "rejected", "invalid_volume", "no_retry"),
        (TRADE_RETCODE_INVALID_PRICE, "requote", "invalid_price_stale", "retry_fresh_tick"),
        (TRADE_RETCODE_INVALID_STOPS, "rejected", "invalid_stops", "no_retry"),
        (TRADE_RETCODE_MARKET_CLOSED, "rejected", "market_closed", "no_retry"),
        (TRADE_RETCODE_NO_MONEY, "rejected", "insufficient_margin", "no_retry"),
        (TRADE_RETCODE_POSITION_NOT_FOUND, "error", "position_not_found", "no_retry"),
        (
            TRADE_RETCODE_UNSUPPORTED_FILLING,
            "requote",
            "unsupported_filling",
            "retry_alternate_filling",
        ),
        (TRADE_RETCODE_REQUOTE, "requote", "price_moved", "retry_fresh_tick"),
    ],
)
def test_each_retcode_maps_to_expected_outcome(
    retcode: int,
    expected_status: str,
    expected_reason: str,
    expected_retry: str,
) -> None:
    outcome = map_retcode(retcode)
    assert outcome.status == expected_status
    assert outcome.reason == expected_reason
    assert outcome.retry_strategy == expected_retry


def test_retcode_map_has_exactly_10_entries() -> None:
    assert len(RETCODE_MAP) == 10


def test_unknown_retcode_returns_error_with_raw_int() -> None:
    outcome = map_retcode(99999)
    assert outcome.status == "error"
    assert outcome.reason == "unknown_retcode_99999"
    assert outcome.retry_strategy == "no_retry"
