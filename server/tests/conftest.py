"""Pytest fixtures for the FastAPI app, the symbol whitelist, and JWT auth."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
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
# Force empty cTrader credentials in tests regardless of any .env shipped in
# the dev environment — the tests assert "no credentials" behaviour.
os.environ["CTRADER_CLIENT_ID"] = ""
os.environ["CTRADER_CLIENT_SECRET"] = ""
# Phase 4.A.1: pin SYMBOL_MAPPING_PATH to the new FTMO whitelist regardless
# of any stale value in the dev shell or local .env. Tests load + validate
# the new file format; an inherited old-format path would fail at lifespan
# startup.
_REPO_ROOT = Path(__file__).resolve().parents[2]
os.environ["SYMBOL_MAPPING_PATH"] = str(
    _REPO_ROOT / "server" / "data" / "ftmo_whitelist.json"
)

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auto_match_engine import AutoMatchEngine  # noqa: E402
from app.services.broadcast import BroadcastService  # noqa: E402
from app.services.ftmo_whitelist_service import FTMOWhitelistService  # noqa: E402
from app.services.mapping_cache_repository import MappingCacheRepository  # noqa: E402
from app.services.mapping_cache_service import MappingCacheService  # noqa: E402
from app.services.mapping_service import MappingService  # noqa: E402
from app.services.redis_service import RedisService, get_redis_service  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
# Phase 4.A.1 (D-SM-09): the FTMO whitelist now lives in a dedicated file
# under server/data/. The legacy Exness-bundled file is preserved under
# archive/ for the few tests that still want to exercise the old loader.
REAL_FTMO_WHITELIST_PATH = REPO_ROOT / "server" / "data" / "ftmo_whitelist.json"
LEGACY_MAPPING_PATH = REPO_ROOT / "archive" / "symbol_mapping_ftmo_exness_v1.json"


@pytest.fixture(scope="session")
def real_ftmo_whitelist_path() -> Path:
    """Path to the new FTMO whitelist JSON (Phase 4.A.1, D-SM-09)."""
    if not REAL_FTMO_WHITELIST_PATH.is_file():
        pytest.skip(
            f"Real FTMO whitelist not found at {REAL_FTMO_WHITELIST_PATH}"
        )
    return REAL_FTMO_WHITELIST_PATH


@pytest.fixture(scope="session")
def legacy_mapping_path() -> Path:
    """Path to the archived Phase 1-3 symbol mapping (legacy loader regressions)."""
    if not LEGACY_MAPPING_PATH.is_file():
        pytest.skip(f"Legacy mapping file not found at {LEGACY_MAPPING_PATH}")
    return LEGACY_MAPPING_PATH


# Back-compat alias so any not-yet-migrated test that references the old
# fixture name keeps working through step 4.A.1. Step 4.A.5 cleans this up.
@pytest.fixture(scope="session")
def real_mapping_path(real_ftmo_whitelist_path: Path) -> Path:
    return real_ftmo_whitelist_path


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    """A fresh fakeredis-async client per test, decoded as strings."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture(autouse=True)
def _override_redis_service(fake_redis: fakeredis.aioredis.FakeRedis) -> Iterator[None]:
    """Replace the FastAPI redis-service dependency with a fakeredis-backed one."""
    app.dependency_overrides[get_redis_service] = lambda: RedisService(fake_redis)
    yield
    app.dependency_overrides.pop(get_redis_service, None)


@pytest.fixture(autouse=True)
def _wire_app_state_for_tests(
    real_ftmo_whitelist_path: Path,
    fake_redis: fakeredis.aioredis.FakeRedis,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Phase 4.A.5: tests bypass the real lifespan, so the per-test FastAPI
    app needs the same ``app.state.*`` shape that lifespan would build —
    minus the cTrader feed and consumer-group tasks. We pin a fresh
    ``MappingCacheRepository`` per test (under a tmp dir), build a real
    ``AutoMatchEngine`` from the bootstrap hints, and stitch it all into
    a ``MappingService`` so route-level ``Depends(get_mapping_service)``
    resolves cleanly without each test reproducing the wiring."""
    cache_dir = tmp_path_factory.mktemp("symbol_mapping_cache")
    repo = MappingCacheRepository(cache_dir)
    hints_path = REPO_ROOT / "server" / "config" / "symbol_match_hints.json"
    engine = AutoMatchEngine(hints_path)
    whitelist = FTMOWhitelistService(real_ftmo_whitelist_path)
    broadcast = BroadcastService()
    cache_service = MappingCacheService(
        repository=repo,
        engine=engine,
        ftmo_whitelist=whitelist,
        redis=fake_redis,
        broadcast=broadcast,
    )
    mapping_service = MappingService(
        ftmo_whitelist=whitelist,
        cache_service=cache_service,
        redis=fake_redis,
    )
    app.state.ftmo_whitelist = whitelist
    app.state.mapping_cache_repository = repo
    app.state.auto_match_engine = engine
    app.state.broadcast = broadcast
    app.state.mapping_cache_service = cache_service
    app.state.mapping_service = mapping_service
    yield


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
