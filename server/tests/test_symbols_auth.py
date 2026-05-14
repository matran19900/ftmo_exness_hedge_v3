"""Auth-enforcement tests for the /api/symbols endpoints."""

from __future__ import annotations

import pytest
from app.services import symbol_whitelist
from httpx import AsyncClient


def _existing_symbol() -> str:
    syms = symbol_whitelist.get_all_symbols()
    return "EURUSD" if "EURUSD" in syms else syms[0]


@pytest.mark.asyncio
async def test_list_symbols_no_token(client: AsyncClient) -> None:
    resp = await client.get("/api/symbols/")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Not authenticated"


@pytest.mark.asyncio
async def test_list_symbols_invalid_token(client: AsyncClient) -> None:
    resp = await client.get("/api/symbols/", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid token"


@pytest.mark.asyncio
async def test_list_symbols_valid_token(authed_client: AsyncClient) -> None:
    resp = await authed_client.get("/api/symbols/")
    assert resp.status_code == 200
    assert "symbols" in resp.json()


@pytest.mark.asyncio
async def test_get_symbol_no_token(client: AsyncClient) -> None:
    resp = await client.get(f"/api/symbols/{_existing_symbol()}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_symbol_valid_token(authed_client: AsyncClient) -> None:
    target = _existing_symbol()
    resp = await authed_client.get(f"/api/symbols/{target}")
    assert resp.status_code == 200
    # Phase 4.A.1: response now uses FTMOSymbol.name (was SymbolMapping.ftmo).
    assert resp.json()["name"] == target
