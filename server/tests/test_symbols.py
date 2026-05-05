"""Tests for GET /api/symbols and GET /api/symbols/{ftmo_symbol}."""

from __future__ import annotations

import pytest
from app.services import symbol_whitelist
from httpx import AsyncClient

EXPECTED_SYMBOL_COUNT = 117


def _existing_symbol() -> str:
    """Return EURUSD if present, else the first symbol from the sorted whitelist."""
    all_syms = symbol_whitelist.get_all_symbols()
    return "EURUSD" if "EURUSD" in all_syms else all_syms[0]


@pytest.mark.asyncio
async def test_list_symbols_returns_sorted_list(client: AsyncClient) -> None:
    resp = await client.get("/api/symbols/")
    assert resp.status_code == 200
    body = resp.json()
    syms = body["symbols"]
    assert isinstance(syms, list)
    assert len(syms) > 0
    assert syms == sorted(syms)


@pytest.mark.asyncio
async def test_list_symbols_count_matches_file(client: AsyncClient) -> None:
    resp = await client.get("/api/symbols/")
    assert resp.status_code == 200
    assert len(resp.json()["symbols"]) == EXPECTED_SYMBOL_COUNT


@pytest.mark.asyncio
async def test_get_symbol_existing(client: AsyncClient) -> None:
    target = _existing_symbol()
    resp = await client.get(f"/api/symbols/{target}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ftmo"] == target
    for field in (
        "exness",
        "match_type",
        "ftmo_units_per_lot",
        "exness_trade_contract_size",
        "ftmo_pip_size",
        "exness_pip_size",
        "ftmo_pip_value",
        "exness_pip_value",
        "quote_ccy",
    ):
        assert field in body


@pytest.mark.asyncio
async def test_get_symbol_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/symbols/NOTREAL")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Symbol not in whitelist"
