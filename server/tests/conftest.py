"""Pytest fixtures for the FastAPI app and the symbol whitelist."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from app.main import app
from app.services import symbol_whitelist
from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MAPPING_PATH = REPO_ROOT / "symbol_mapping_ftmo_exness.json"


@pytest.fixture(scope="session")
def real_mapping_path() -> Path:
    """Path to the committed symbol mapping JSON used by integration-style tests."""
    if not REAL_MAPPING_PATH.is_file():
        pytest.skip(f"Real symbol mapping not found at {REAL_MAPPING_PATH}")
    return REAL_MAPPING_PATH


@pytest.fixture(autouse=True)
def _load_real_whitelist(real_mapping_path: Path) -> None:
    """Ensure the in-process whitelist cache is loaded before every test."""
    symbol_whitelist.load_whitelist(str(real_mapping_path))


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """ASGI httpx client bound to the FastAPI app (no real network)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
