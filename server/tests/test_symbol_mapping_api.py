"""Integration tests for the /api/symbol-mapping wizard endpoints (Phase 4.A.4).

For each of the 7 endpoints we cover:
  - Auth (401 without JWT, OK with JWT)
  - Happy path against fakeredis-backed MappingCacheService
  - 404 / 400 error paths
  - Side effects (Redis state change, broadcast call, command stream XADD)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio
from app.dependencies.mapping_cache_service import get_mapping_cache_service
from app.main import app
from app.services.auto_match_engine import AutoMatchEngine
from app.services.broadcast import BroadcastService
from app.services.ftmo_whitelist_service import FTMOWhitelistService
from app.services.mapping_cache_repository import (
    MappingCacheRepository,
    compute_signature,
)
from app.services.mapping_cache_schemas import (
    MappingEntry,
    RawSymbolEntry,
    SymbolMappingCacheFile,
)
from app.services.mapping_cache_service import MappingCacheService
from httpx import AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_HINTS_PATH = REPO_ROOT / "server" / "config" / "symbol_match_hints.json"
REAL_FTMO_WHITELIST_PATH = REPO_ROOT / "server" / "data" / "ftmo_whitelist.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_dict(name: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": name,
        "contract_size": 100000.0,
        "digits": 5,
        "pip_size": 0.0001,
        "volume_min": 0.01,
        "volume_step": 0.01,
        "volume_max": 200.0,
        "currency_profit": "USD",
    }
    base.update(overrides)
    return base


def _raw(name: str, **overrides: Any) -> RawSymbolEntry:
    return RawSymbolEntry(**_raw_dict(name, **overrides))


def _mapping(ftmo: str, exness: str, **overrides: Any) -> MappingEntry:
    base: dict[str, Any] = {
        "ftmo": ftmo,
        "exness": exness,
        "match_type": "suffix_strip",
        "contract_size": 100000.0,
        "pip_size": 0.0001,
        "pip_value": 10.0,
        "quote_ccy": "USD",
        "exness_volume_step": 0.01,
        "exness_volume_min": 0.01,
        "exness_volume_max": 200.0,
    }
    base.update(overrides)
    return MappingEntry(**base)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def wizard_service(
    tmp_path: Path,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> AsyncIterator[MappingCacheService]:
    """Build a service backed by the same fakeredis the autouse fixture
    installs into the FastAPI Depends layer, plus a per-test repository
    under ``tmp_path``. Override ``get_mapping_cache_service`` so the
    routes resolve to *this* service."""
    repo = MappingCacheRepository(tmp_path)
    engine = AutoMatchEngine(REAL_HINTS_PATH)
    whitelist = FTMOWhitelistService(REAL_FTMO_WHITELIST_PATH)
    bs = BroadcastService()
    bs.publish = AsyncMock()  # type: ignore[method-assign]
    svc = MappingCacheService(
        repository=repo,
        engine=engine,
        ftmo_whitelist=whitelist,
        redis=fake_redis,
        broadcast=bs,
    )
    app.dependency_overrides[get_mapping_cache_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(get_mapping_cache_service, None)


# ---------------------------------------------------------------------------
# Endpoint 1 — GET raw-symbols
# ---------------------------------------------------------------------------


class TestGetRawSymbols:
    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, client: AsyncClient) -> None:
        resp = await client.get("/api/accounts/exness/exn_001/raw-symbols")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_404_when_no_snapshot(
        self, authed_client: AsyncClient, wizard_service: MappingCacheService
    ) -> None:
        resp = await authed_client.get(
            "/api/accounts/exness/exn_unknown/raw-symbols"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_happy_path(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await fake_redis.set(
            "exness_raw_symbols:exn_001",
            json.dumps([_raw_dict("EURUSDz")]),
        )
        resp = await authed_client.get(
            "/api/accounts/exness/exn_001/raw-symbols"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["account_id"] == "exn_001"
        assert len(body["symbols"]) == 1
        assert body["symbols"][0]["name"] == "EURUSDz"


# ---------------------------------------------------------------------------
# Endpoint 2 — GET mapping-status
# ---------------------------------------------------------------------------


class TestGetMappingStatus:
    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, client: AsyncClient) -> None:
        resp = await client.get("/api/accounts/exness/exn_001/mapping-status")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_default_pending_mapping(
        self, authed_client: AsyncClient, wizard_service: MappingCacheService
    ) -> None:
        resp = await authed_client.get(
            "/api/accounts/exness/exn_unknown/mapping-status"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending_mapping"
        assert body["signature"] is None

    @pytest.mark.asyncio
    async def test_active_status_with_signature(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await fake_redis.set("mapping_status:exn_001", "active")
        await fake_redis.set("account_to_mapping:exn_001", "sig-xyz")
        resp = await authed_client.get(
            "/api/accounts/exness/exn_001/mapping-status"
        )
        body = resp.json()
        assert body["status"] == "active"
        assert body["signature"] == "sig-xyz"


# ---------------------------------------------------------------------------
# Endpoint 3 — POST auto-match
# ---------------------------------------------------------------------------


class TestPostAutoMatch:
    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/auto-match"
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_404_without_snapshot(
        self, authed_client: AsyncClient, wizard_service: MappingCacheService
    ) -> None:
        resp = await authed_client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/auto-match"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_miss_returns_engine_proposals(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # EURUSD is in FTMO whitelist + raw → tier-1 exact match expected.
        await fake_redis.set(
            "exness_raw_symbols:exn_001",
            json.dumps([_raw_dict("EURUSD")]),
        )
        resp = await authed_client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/auto-match"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["fuzzy_match_source"] is None
        eurusd = [p for p in body["proposals"] if p["ftmo"] == "EURUSD"]
        assert len(eurusd) == 1
        assert eurusd[0]["match_type"] == "exact"

    @pytest.mark.asyncio
    async def test_hit_returns_cached_mappings(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
        tmp_path: Path,
    ) -> None:
        raw_entry = _raw("EURUSDz")
        sig = compute_signature([raw_entry])
        cache = SymbolMappingCacheFile(
            schema_version=1,
            signature=sig,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            created_by_account="exn_seed",
            used_by_accounts=["exn_seed"],
            raw_symbols_snapshot=[raw_entry],
            mappings=[_mapping("EURUSD", "EURUSDz")],
        )
        await wizard_service._repository.write(cache)  # noqa: SLF001
        # mark redis hit:
        await fake_redis.hset(  # type: ignore[misc]
            f"mapping_cache:{sig}", mapping={"signature": sig}
        )
        await fake_redis.set(
            "exness_raw_symbols:exn_001",
            json.dumps([_raw_dict("EURUSDz")]),
        )
        resp = await authed_client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/auto-match"
        )
        body = resp.json()
        assert body["fuzzy_match_score"] == 1.0
        assert body["fuzzy_match_source"].endswith(f"{sig}.json")
        assert body["proposals"][0]["ftmo"] == "EURUSD"


# ---------------------------------------------------------------------------
# Endpoint 4 — POST save
# ---------------------------------------------------------------------------


class TestPostSave:
    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/save",
            json={"decisions": []},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_404_without_snapshot(
        self, authed_client: AsyncClient, wizard_service: MappingCacheService
    ) -> None:
        resp = await authed_client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/save",
            json={"decisions": []},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_save_returns_201_for_new_cache(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await fake_redis.set(
            "exness_raw_symbols:exn_001",
            json.dumps([_raw_dict("EURUSD")]),
        )
        resp = await authed_client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/save",
            json={"decisions": [{"ftmo": "EURUSD", "action": "accept"}]},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["created_new_cache"] is True
        assert body["mapping_count"] == 1

    @pytest.mark.asyncio
    async def test_save_returns_200_when_linking_existing(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # First account creates the cache.
        await fake_redis.set(
            "exness_raw_symbols:exn_001",
            json.dumps([_raw_dict("EURUSD")]),
        )
        await authed_client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/save",
            json={"decisions": [{"ftmo": "EURUSD", "action": "accept"}]},
        )
        # Second account with same raw → link.
        await fake_redis.set(
            "exness_raw_symbols:exn_002",
            json.dumps([_raw_dict("EURUSD")]),
        )
        resp = await authed_client.post(
            "/api/accounts/exness/exn_002/symbol-mapping/save",
            json={"decisions": [{"ftmo": "EURUSD", "action": "accept"}]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["created_new_cache"] is False

    @pytest.mark.asyncio
    async def test_save_400_on_spec_block(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        # Pre-seed cache with EURUSD mapping using contract_size=100000.
        # Then publish raw with contract_size=10000 (10x divergence).
        raw_initial = _raw("EURUSD")
        sig = compute_signature([raw_initial])
        cache = SymbolMappingCacheFile(
            schema_version=1,
            signature=sig,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            created_by_account="exn_pre",
            used_by_accounts=["exn_pre"],
            raw_symbols_snapshot=[raw_initial],
            mappings=[_mapping("EURUSD", "EURUSD", contract_size=100000.0)],
        )
        await wizard_service._repository.write(cache)  # noqa: SLF001
        await fake_redis.hset(  # type: ignore[misc]
            f"mapping_cache:{sig}", mapping={"signature": sig}
        )
        # Raw published with the SAME signature (same name) but divergent contract_size.
        await fake_redis.set(
            "exness_raw_symbols:exn_002",
            json.dumps([_raw_dict("EURUSD", contract_size=10000.0)]),
        )
        resp = await authed_client.post(
            "/api/accounts/exness/exn_002/symbol-mapping/save",
            json={"decisions": [{"ftmo": "EURUSD", "action": "accept"}]},
        )
        assert resp.status_code == 400
        body = resp.json()
        # FastAPI nests under detail
        assert body["detail"]["detail"] == "spec_divergence_block"
        assert any(
            d["field"] == "contract_size"
            for d in body["detail"]["divergences"]
        )

    @pytest.mark.asyncio
    async def test_save_validation_error_extra_field(
        self, authed_client: AsyncClient, wizard_service: MappingCacheService
    ) -> None:
        resp = await authed_client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/save",
            json={
                "decisions": [],
                "rogue": "x",
            },
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Endpoint 5 — PATCH edit
# ---------------------------------------------------------------------------


class TestPatchEdit:
    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, client: AsyncClient) -> None:
        resp = await client.patch(
            "/api/accounts/exness/exn_001/symbol-mapping/edit",
            json={"decisions": []},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_404_without_prior_mapping(
        self, authed_client: AsyncClient, wizard_service: MappingCacheService
    ) -> None:
        resp = await authed_client.patch(
            "/api/accounts/exness/exn_unknown/symbol-mapping/edit",
            json={"decisions": []},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_edit_after_save(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await fake_redis.set(
            "exness_raw_symbols:exn_001",
            json.dumps([_raw_dict("EURUSD"), _raw_dict("ALT")]),
        )
        await authed_client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/save",
            json={
                "decisions": [
                    {"ftmo": "EURUSD", "action": "accept"},
                    {"ftmo": "ALT", "action": "override", "exness_override": "ALT"},
                ]
            },
        )
        resp = await authed_client.patch(
            "/api/accounts/exness/exn_001/symbol-mapping/edit",
            json={
                "decisions": [
                    {"ftmo": "EURUSD", "action": "accept"},
                    {"ftmo": "ALT", "action": "skip"},
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mapping_count"] == 1


# ---------------------------------------------------------------------------
# Endpoint 6 — GET cache list
# ---------------------------------------------------------------------------


class TestGetCacheList:
    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, client: AsyncClient) -> None:
        resp = await client.get("/api/symbol-mapping-cache")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_cache_list(
        self, authed_client: AsyncClient, wizard_service: MappingCacheService
    ) -> None:
        resp = await authed_client.get("/api/symbol-mapping-cache")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"caches": []}

    @pytest.mark.asyncio
    async def test_cache_list_after_save(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        await fake_redis.set(
            "exness_raw_symbols:exn_001",
            json.dumps([_raw_dict("EURUSD")]),
        )
        await authed_client.post(
            "/api/accounts/exness/exn_001/symbol-mapping/save",
            json={"decisions": [{"ftmo": "EURUSD", "action": "accept"}]},
        )
        resp = await authed_client.get("/api/symbol-mapping-cache")
        body = resp.json()
        assert len(body["caches"]) == 1
        entry = body["caches"][0]
        assert entry["used_by_accounts"] == ["exn_001"]
        assert entry["mapping_count"] == 1


# ---------------------------------------------------------------------------
# Endpoint 7 — POST resync
# ---------------------------------------------------------------------------


class TestPostResync:
    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/accounts/exness/exn_001/symbols/resync"
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_resync_xadds_command(
        self,
        authed_client: AsyncClient,
        wizard_service: MappingCacheService,
        fake_redis: fakeredis.aioredis.FakeRedis,
    ) -> None:
        resp = await authed_client.post(
            "/api/accounts/exness/exn_001/symbols/resync"
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "resync_requested"
        assert body["account_id"] == "exn_001"
        # Stream now has one entry.
        entries = await fake_redis.xrange("cmd_stream:exness:exn_001")
        assert len(entries) == 1
        _msg_id, fields = entries[0]
        assert fields["action"] == "resync_symbols"
        assert "request_id" in fields
        assert fields["request_id"] == body["request_id"]
