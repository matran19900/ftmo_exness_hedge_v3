"""Tests for GET /api/symbols and GET /api/symbols/{ftmo_symbol} (happy path, authed)."""

from __future__ import annotations

import pytest
from app.main import app
from httpx import AsyncClient

EXPECTED_SYMBOL_COUNT = 117


def _existing_symbol() -> str:
    """Return EURUSD if present, else the first symbol from the sorted whitelist."""
    all_syms = app.state.ftmo_whitelist.all_symbols()
    return "EURUSD" if "EURUSD" in all_syms else all_syms[0]


@pytest.mark.asyncio
async def test_list_symbols_returns_sorted_list(authed_client: AsyncClient) -> None:
    resp = await authed_client.get("/api/symbols/")
    assert resp.status_code == 200
    body = resp.json()
    syms = body["symbols"]
    assert isinstance(syms, list)
    assert len(syms) > 0
    assert syms == sorted(syms)


@pytest.mark.asyncio
async def test_list_symbols_count_matches_file(authed_client: AsyncClient) -> None:
    resp = await authed_client.get("/api/symbols/")
    assert resp.status_code == 200
    assert len(resp.json()["symbols"]) == EXPECTED_SYMBOL_COUNT


@pytest.mark.asyncio
async def test_get_symbol_existing(authed_client: AsyncClient) -> None:
    """Phase 4.A.1 (D-SM-09): response is now ``FTMOSymbol`` — Exness-side
    fields are gone; they live on per-account mapping caches now."""
    target = _existing_symbol()
    resp = await authed_client.get(f"/api/symbols/{target}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == target
    for field in (
        "name",
        "asset_class",
        "quote_ccy",
        "ftmo_units_per_lot",
        "ftmo_pip_size",
        "ftmo_pip_value",
    ):
        assert field in body, f"missing field {field} in {body}"
    # Exness-side fields are explicitly removed; assert absence.
    for forbidden in (
        "exness",
        "match_type",
        "exness_trade_contract_size",
        "exness_pip_size",
        "exness_pip_value",
    ):
        assert forbidden not in body, f"unexpected field {forbidden}"


@pytest.mark.asyncio
async def test_get_symbol_not_found(authed_client: AsyncClient) -> None:
    resp = await authed_client.get("/api/symbols/NOTREAL")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Symbol not in whitelist"
