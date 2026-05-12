"""Tests for ``app.services.account_helpers.row_to_entry`` (step 3.13a).

The helper bridges the gap between Redis HASH-string convention
(``enabled: "true"|"false"``, all-string fields) and the typed
``AccountStatusEntry`` Pydantic model. Both the REST list/PATCH and
the WS broadcast loop route through it, so this is the single
choke-point worth pinning.
"""

from __future__ import annotations

import pytest
from app.api.accounts import AccountStatusEntry
from app.services.account_helpers import row_to_entry


def _base_row(**overrides: str) -> dict[str, str]:
    row = {
        "broker": "ftmo",
        "account_id": "ftmo_001",
        "name": "primary",
        "enabled": "true",
        "status": "online",
        "balance_raw": "1000000",
        "equity_raw": "1005000",
        "margin_raw": "0",
        "free_margin_raw": "1005000",
        "currency": "USD",
        "money_digits": "2",
    }
    row.update(overrides)
    return row


def test_row_to_entry_enabled_true() -> None:
    """The string ``"true"`` parses to a real Python ``True`` — pin
    against the pre-3.13a regression where the WS payload shipped the
    raw string and ``Boolean("false") === true`` in the browser."""
    entry = row_to_entry(_base_row(enabled="true"))
    assert isinstance(entry, AccountStatusEntry)
    assert entry.enabled is True


def test_row_to_entry_enabled_false() -> None:
    """The string ``"false"`` parses to ``False``."""
    entry = row_to_entry(_base_row(enabled="false"))
    assert entry.enabled is False


def test_row_to_entry_preserves_money_fields() -> None:
    """``balance_raw``, ``equity_raw``, ``margin_raw``, ``free_margin_raw``
    stay as strings — D-108 scaling happens at the frontend render
    boundary, never in this layer (the WS + REST payloads must be
    shape-identical and the wire stays integer-precise)."""
    entry = row_to_entry(
        _base_row(
            balance_raw="1000000",
            equity_raw="1005000",
            margin_raw="500",
            free_margin_raw="1004500",
            money_digits="2",
        )
    )
    assert entry.balance_raw == "1000000"
    assert entry.equity_raw == "1005000"
    assert entry.margin_raw == "500"
    assert entry.free_margin_raw == "1004500"
    assert entry.money_digits == "2"


def test_row_to_entry_status_literal_disabled() -> None:
    """``status: "disabled"`` round-trips through the Literal cast."""
    entry = row_to_entry(_base_row(status="disabled", enabled="false"))
    assert entry.status == "disabled"


def test_row_to_entry_model_dump_yields_json_native_types() -> None:
    """The WS broadcast loop calls ``row_to_entry(row).model_dump()``;
    the resulting dict must have ``enabled: bool`` (not str) so the
    JSON serialiser produces ``true`` / ``false`` literals on the
    wire. Pinning this is the whole reason 3.13a exists."""
    dumped = row_to_entry(_base_row(enabled="false", status="disabled")).model_dump()
    assert dumped["enabled"] is False
    assert dumped["status"] == "disabled"
    assert dumped["broker"] == "ftmo"
    # Money fields still strings.
    assert isinstance(dumped["balance_raw"], str)


def test_row_to_entry_unknown_enabled_value_falls_back_to_false() -> None:
    """Any value other than the literal ``"true"`` → ``False``. Defensive
    against a future writer that uses ``"True"`` (capitalised) or
    ``"1"`` — without explicit casing we'd silently flip behaviour."""
    pytest.importorskip("pydantic")
    entry = row_to_entry(_base_row(enabled="True"))  # capitalised, NOT the literal
    assert entry.enabled is False
