"""Pytest fixtures for the FastAPI app, the symbol whitelist, and JWT auth."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import bcrypt

# Test env must be in place BEFORE `app.main` is imported, because Settings has
# no defaults for REDIS_URL / JWT_SECRET / ADMIN_PASSWORD_HASH and would fail
# to construct otherwise. Bcrypt cost 4 keeps each hash under ~10ms.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-chars-long-xxxxxxxxxx")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("JWT_EXPIRES_MINUTES", "60")
os.environ.setdefault(
    "ADMIN_PASSWORD_HASH",
    bcrypt.hashpw(b"admin", bcrypt.gensalt(rounds=4)).decode("utf-8"),
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from app.main import app  # noqa: E402
from app.services import symbol_whitelist  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

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
    """ASGI httpx client bound to the FastAPI app (no auth headers)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def authed_client(client: AsyncClient) -> AsyncClient:
    """Like `client` but pre-loaded with a Bearer token from a real /login round-trip."""
    resp = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin"},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client
