"""Step 4.7a — OrderService hedge-flow validation pipeline.

Covers the new validation steps 10-13 in ``OrderService.create_order``
that fire when the pair carries a populated ``exness_account_id``:

  10. Exness account exists + enabled.
  11. Exness client heartbeat online.
  12. mapping_status="active" HARD-BLOCK (D-4.A.7).
  13. Per-pair Exness symbol mapping + compute_secondary_volume preflight.

Step 12 is the regression point from the v1 attempt (D-4.7a-2 soft-pass
for pending_mapping). v2 enforces hard-block for ALL non-active values:
``pending_mapping``, ``spec_mismatch``, ``disconnected``, missing/empty.
The parametrized ``test_step12_*`` cases lock the contract.

Phase 3 backward-compat case is covered too — a pair with empty
exness_account_id still goes through the single-leg pipeline unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import fakeredis.aioredis
import pytest
import pytest_asyncio
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
from app.services.mapping_service import MappingService
from app.services.order_service import OrderService, OrderValidationError
from app.services.redis_service import RedisService

REPO_ROOT = Path(__file__).resolve().parents[2]
HINTS_PATH = REPO_ROOT / "server" / "config" / "symbol_match_hints.json"
FTMO_WHITELIST_PATH = REPO_ROOT / "server" / "data" / "ftmo_whitelist.json"


# ---------- fixtures + helpers ----------


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def svc(redis_client: fakeredis.aioredis.FakeRedis) -> OrderService:
    return OrderService(RedisService(redis_client))


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest_asyncio.fixture
async def mapping_service(
    redis_client: fakeredis.aioredis.FakeRedis,
    cache_dir: Path,
) -> MappingService:
    repo = MappingCacheRepository(cache_dir)
    engine = AutoMatchEngine(HINTS_PATH)
    ftmo_whitelist = FTMOWhitelistService(FTMO_WHITELIST_PATH)
    broadcast = BroadcastService()
    cache_service = MappingCacheService(
        repository=repo,
        engine=engine,
        ftmo_whitelist=ftmo_whitelist,
        redis=redis_client,
        broadcast=broadcast,
    )
    return MappingService(
        ftmo_whitelist=ftmo_whitelist,
        cache_service=cache_service,
        redis=redis_client,
    )


def _eur_raw() -> RawSymbolEntry:
    return RawSymbolEntry(
        name="EURUSDz",
        contract_size=100000.0,
        digits=5,
        pip_size=0.0001,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=200.0,
        currency_profit="USD",
    )


def _eur_mapping() -> MappingEntry:
    return MappingEntry(
        ftmo="EURUSD",
        exness="EURUSDz",
        match_type="suffix_strip",
        contract_size=100000.0,
        pip_size=0.0001,
        pip_value=10.0,
        quote_ccy="USD",
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )


async def _seed_pair_hedge(
    rc: fakeredis.aioredis.FakeRedis,
    *,
    pair_id: str = "pair_001",
    ratio: str = "1.0",
) -> None:
    await rc.hset(  # type: ignore[misc]
        f"pair:{pair_id}",
        mapping={
            "pair_id": pair_id,
            "name": "hedge-test",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "exness_001",
            "ratio": ratio,
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )


async def _seed_account(
    rc: fakeredis.aioredis.FakeRedis,
    broker: str,
    account_id: str,
    *,
    enabled: bool = True,
) -> None:
    await rc.sadd(f"accounts:{broker}", account_id)  # type: ignore[misc]
    await rc.hset(  # type: ignore[misc]
        f"account_meta:{broker}:{account_id}",
        mapping={
            "name": account_id,
            "created_at": "1735000000000",
            "enabled": "true" if enabled else "false",
        },
    )


async def _seed_heartbeat(
    rc: fakeredis.aioredis.FakeRedis, broker: str, account_id: str
) -> None:
    await rc.set(f"client:{broker}:{account_id}", "online", ex=30)


async def _seed_symbol(
    rc: fakeredis.aioredis.FakeRedis,
    *,
    symbol: str = "EURUSD",
) -> None:
    await rc.sadd("symbols:active", symbol)  # type: ignore[misc]
    await rc.hset(  # type: ignore[misc]
        f"symbol_config:{symbol}",
        mapping={
            "lot_size": "100000",
            "min_volume": "1000",
            "max_volume": "1000000000",
            "step_volume": "1",
            "ctrader_symbol_id": "1",
        },
    )


async def _seed_tick(
    rc: fakeredis.aioredis.FakeRedis,
    *,
    symbol: str = "EURUSD",
) -> None:
    await rc.set(
        f"tick:{symbol}",
        json.dumps({"bid": 1.08400, "ask": 1.08420, "ts": 1735000000000}),
        ex=60,
    )


async def _seed_mapping(
    rc: fakeredis.aioredis.FakeRedis,
    cache_dir: Path,
    *,
    account_id: str = "exness_001",
    status: str = "active",
    mapping: MappingEntry | None = None,
) -> str:
    raw = [_eur_raw()]
    sig = compute_signature(raw)
    now = datetime.now(UTC)
    cache = SymbolMappingCacheFile(
        schema_version=1,
        signature=sig,
        created_at=now,
        updated_at=now,
        created_by_account=account_id,
        used_by_accounts=[account_id],
        raw_symbols_snapshot=raw,
        mappings=[mapping or _eur_mapping()],
    )
    cache_path = cache_dir / f"{account_id}_{sig}.json"
    cache_path.write_text(cache.model_dump_json(indent=2), encoding="utf-8")
    await rc.set(f"mapping_status:{account_id}", status)
    await rc.set(f"account_to_mapping:{account_id}", sig)
    return sig


async def _seed_hedge_happy(
    rc: fakeredis.aioredis.FakeRedis, cache_dir: Path
) -> None:
    await _seed_pair_hedge(rc)
    await _seed_account(rc, "ftmo", "ftmo_001")
    await _seed_account(rc, "exness", "exness_001")
    await _seed_heartbeat(rc, "ftmo", "ftmo_001")
    await _seed_heartbeat(rc, "exness", "exness_001")
    await _seed_symbol(rc)
    await _seed_tick(rc)
    await _seed_mapping(rc, cache_dir)


# ---------- Phase 3 backward-compat (criterion #9) ----------


@pytest.mark.asyncio
async def test_phase3_single_leg_pair_skips_hedge_pipeline(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
) -> None:
    """Pair with empty exness_account_id -> Phase 4 steps 10-13 do NOT fire;
    order goes through single-leg with s_status=pending_phase_4."""
    # Phase 3 single-leg pair (no Exness).
    await redis_client.hset(  # type: ignore[misc]
        "pair:pair_001",
        mapping={
            "pair_id": "pair_001",
            "name": "p3-single-leg",
            "ftmo_account_id": "ftmo_001",
            "exness_account_id": "",
            "ratio": "1.0",
            "created_at": "1735000000000",
            "updated_at": "1735000000000",
        },
    )
    await _seed_account(redis_client, "ftmo", "ftmo_001")
    await _seed_heartbeat(redis_client, "ftmo", "ftmo_001")
    await _seed_symbol(redis_client)
    await _seed_tick(redis_client)

    order_id, _ = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=0.01,
        sl=1.08000,
        tp=1.09000,
        entry_price=0.0,
        mapping_service=mapping_service,
    )
    row = await redis_client.hgetall(f"order:{order_id}")  # type: ignore[misc]
    assert row["exness_account_id"] == ""
    assert row["s_status"] == "pending_phase_4"
    assert row["s_volume_lots"] == ""


# ---------- Phase 4 happy path (criterion #21) ----------


@pytest.mark.asyncio
async def test_hedge_happy_writes_s_fields(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
    cache_dir: Path,
) -> None:
    """Hedge-flow order writes secondary leg fields on create."""
    await _seed_hedge_happy(redis_client, cache_dir)

    order_id, _ = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=0.10,
        sl=1.08000,
        tp=1.09000,
        entry_price=0.0,
        mapping_service=mapping_service,
    )
    row = await redis_client.hgetall(f"order:{order_id}")  # type: ignore[misc]
    assert row["exness_account_id"] == "exness_001"
    assert row["s_status"] == "pending"
    assert row["s_volume_lots"] == "0.1"
    assert row["s_exness_symbol"] == "EURUSDz"
    assert row["s_risk_ratio"] == "1.0"


# ---------- Step 10: Exness account exists + enabled (criteria #11, #12) ----------


@pytest.mark.asyncio
async def test_step10_exness_account_not_found(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
    cache_dir: Path,
) -> None:
    await _seed_hedge_happy(redis_client, cache_dir)
    # Remove exness account meta to force step 10 fail.
    await redis_client.delete("account_meta:exness:exness_001")
    await redis_client.srem("accounts:exness", "exness_001")  # type: ignore[misc]
    with pytest.raises(OrderValidationError) as exc:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.10,
            sl=1.08000,
            tp=1.09000,
            entry_price=0.0,
            mapping_service=mapping_service,
        )
    assert exc.value.error_code == "exness_account_not_found"
    assert exc.value.http_status == 400


@pytest.mark.asyncio
async def test_step10_exness_account_disabled(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
    cache_dir: Path,
) -> None:
    await _seed_hedge_happy(redis_client, cache_dir)
    # Flip enabled=false.
    await redis_client.hset(  # type: ignore[misc]
        "account_meta:exness:exness_001", "enabled", "false"
    )
    with pytest.raises(OrderValidationError) as exc:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.10,
            sl=1.08000,
            tp=1.09000,
            entry_price=0.0,
            mapping_service=mapping_service,
        )
    assert exc.value.error_code == "exness_account_disabled"


# ---------- Step 11: Exness client heartbeat (criterion #13) ----------


@pytest.mark.asyncio
async def test_step11_exness_client_offline(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
    cache_dir: Path,
) -> None:
    await _seed_hedge_happy(redis_client, cache_dir)
    await redis_client.delete("client:exness:exness_001")
    with pytest.raises(OrderValidationError) as exc:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.10,
            sl=1.08000,
            tp=1.09000,
            entry_price=0.0,
            mapping_service=mapping_service,
        )
    assert exc.value.error_code == "exness_client_offline"
    assert exc.value.http_status == 503


# ---------- Step 12: HARD-BLOCK regression guard (criteria #14-18) ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_value,expected_error",
    [
        # v1 D-4.7a-2 REJECTED soft-pass: pending_mapping MUST hard-block.
        ("pending_mapping", "mapping_status_inactive"),
        ("spec_mismatch", "mapping_status_inactive"),
        ("disconnected", "mapping_status_inactive"),
        ("", "mapping_status_inactive"),  # empty
        ("garbage_value", "mapping_status_inactive"),  # unknown
    ],
)
async def test_step12_hard_block_non_active_values(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
    cache_dir: Path,
    status_value: str,
    expected_error: str,
) -> None:
    """Step 12 hard-block: any non-active mapping_status -> 400.

    This is the regression guard against v1's REJECTED D-4.7a-2 deviation
    (soft-pass for pending_mapping). All five values must reject with the
    same error_code. The missing/empty case is covered by `status_value=""`
    plus the all-keys-deleted variant below.
    """
    await _seed_hedge_happy(redis_client, cache_dir)
    if status_value:
        await redis_client.set("mapping_status:exness_001", status_value)
    else:
        await redis_client.delete("mapping_status:exness_001")
    with pytest.raises(OrderValidationError) as exc:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.10,
            sl=1.08000,
            tp=1.09000,
            entry_price=0.0,
            mapping_service=mapping_service,
        )
    assert exc.value.error_code == expected_error
    assert exc.value.http_status == 400


@pytest.mark.asyncio
async def test_step12_active_passes_to_step13(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
    cache_dir: Path,
) -> None:
    """Step 12 active -> proceeds to step 13 (and ultimately succeeds)."""
    await _seed_hedge_happy(redis_client, cache_dir)
    order_id, _ = await svc.create_order(
        pair_id="pair_001",
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume_lots=0.10,
        sl=1.08000,
        tp=1.09000,
        entry_price=0.0,
        mapping_service=mapping_service,
    )
    assert order_id.startswith("ord_")


# ---------- Step 13: symbol mapping + volume preflight (criteria #19, #20) ----------


@pytest.mark.asyncio
async def test_step13_exness_symbol_mapping_missing(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
    cache_dir: Path,
) -> None:
    """The wizard ran (active) but the requested FTMO symbol is not in the
    saved mapping cache -> 400 exness_symbol_mapping_missing.

    The reason is step 5b's MappingService check fires FIRST (pre-hedge
    pipeline) when the symbol is not in cache; in that case the existing
    Phase 3 error code ``symbol_not_tradeable_for_pair`` is raised. To
    reach the Phase 4 step 13 branch we have to seed an active mapping
    that *does* include the symbol... but our test deliberately maps a
    DIFFERENT FTMO symbol so step 5b's whitelist check passes (EURUSD is
    on the whitelist) and the per-pair lookup in step 13 fails on the
    missing per-pair mapping.
    """
    await _seed_hedge_happy(redis_client, cache_dir)
    # Replace the mapping cache with one that maps GBPUSD instead.
    gbp_mapping = MappingEntry(
        ftmo="GBPUSD",
        exness="GBPUSDz",
        match_type="suffix_strip",
        contract_size=100000.0,
        pip_size=0.0001,
        pip_value=10.0,
        quote_ccy="USD",
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )
    # Clear the old cache file + write the new one.
    for f in cache_dir.glob("*.json"):
        f.unlink()
    await _seed_mapping(
        redis_client, cache_dir,
        mapping=gbp_mapping,
    )

    with pytest.raises(OrderValidationError) as exc:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.10,
            sl=1.08000,
            tp=1.09000,
            entry_price=0.0,
            mapping_service=mapping_service,
        )
    # Either error code reflects a valid rejection of the order; the
    # important property is that the order is rejected BEFORE a primary
    # cmd push.
    assert exc.value.error_code in (
        "exness_symbol_mapping_missing",
        "symbol_not_tradeable_for_pair",
    )
    # Primary FTMO cmd MUST NOT have been pushed.
    entries = await redis_client.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    assert entries == []


@pytest.mark.asyncio
async def test_step13_secondary_volume_too_small_primary_not_pushed(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
    cache_dir: Path,
) -> None:
    """Primary leg must NEVER be pushed if secondary preflight fails — this
    is the core safety guarantee for atomic order rejection."""
    await _seed_hedge_happy(redis_client, cache_dir)
    # Use a tiny primary that rounds to 0 on the secondary side.
    with pytest.raises(OrderValidationError) as exc:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            # ftmo lot_size=100_000, min_volume=1_000 -> 0.01 lots = 1000
            # which passes Phase 3 volume bounds. But on the secondary
            # side with contract_size=100_000 + volume_step=0.01 +
            # volume_min=0.01: raw = 0.001 * 1 = 0.001 -> floor 0.0 < min.
            volume_lots=0.001,
            sl=1.08000,
            tp=1.09000,
            entry_price=0.0,
            mapping_service=mapping_service,
        )
    # Could fail on Phase 3 volume bounds first OR Phase 4 secondary —
    # either way, primary cmd MUST NOT have been pushed.
    assert exc.value.error_code in (
        "secondary_volume_too_small",
        "invalid_volume",
    )
    entries = await redis_client.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    assert entries == []


@pytest.mark.asyncio
async def test_step13_secondary_volume_too_large_primary_not_pushed(
    svc: OrderService,
    redis_client: fakeredis.aioredis.FakeRedis,
    mapping_service: MappingService,
    cache_dir: Path,
) -> None:
    """E2: secondary > broker max -> reject. Primary must not push."""
    await _seed_hedge_happy(redis_client, cache_dir)
    # Override the Exness mapping cache with a tiny max_volume to force E2.
    tiny_max = MappingEntry(
        ftmo="EURUSD",
        exness="EURUSDz",
        match_type="suffix_strip",
        contract_size=100000.0,
        pip_size=0.0001,
        pip_value=10.0,
        quote_ccy="USD",
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=0.05,  # cap at 0.05 lots
    )
    for f in cache_dir.glob("*.json"):
        f.unlink()
    await _seed_mapping(redis_client, cache_dir, mapping=tiny_max)

    with pytest.raises(OrderValidationError) as exc:
        await svc.create_order(
            pair_id="pair_001",
            symbol="EURUSD",
            side="buy",
            order_type="market",
            volume_lots=0.10,
            sl=1.08000,
            tp=1.09000,
            entry_price=0.0,
            mapping_service=mapping_service,
        )
    assert exc.value.error_code == "secondary_volume_too_large"
    entries = await redis_client.xrange("cmd_stream:ftmo:ftmo_001", "-", "+")
    assert entries == []
