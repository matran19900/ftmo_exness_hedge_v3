"""Tests for ``app.services.position_tracker`` (step 3.8).

Covers:
  - ``_compute_pnl`` formula correctness across BUY/SELL, profit/loss,
    USD quote / JPY quote / cross quote (USDGBP), and the fallback
    when no conversion tick is cached.
  - ``_run_one_cycle`` skip criteria (non-filled p_status, missing
    symbol_config, missing tick) + position_cache HSET + WS batch
    publish.
  - Stale-tick threshold (5 s).
  - ``position_tracker_loop`` Task.cancel exit + cycle-exception
    resilience.
  - Lifespan wiring: one task per FTMO account.

The capturing ``BroadcastService`` from step 3.7 tests is duplicated
here (small helper; cross-importing test fixtures via conftest would
risk surprising test parallelism behaviour).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import fakeredis.aioredis
import pytest
import pytest_asyncio
from app.services.broadcast import BroadcastService
from app.services.position_tracker import (
    POSITIONS_CHANNEL,
    _compute_pnl,
    _convert_to_usd,
    _derive_quote_currency,
    _run_one_cycle,
    position_tracker_loop,
)
from app.services.redis_service import RedisService

# ---------- fixtures ----------


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


class _CapturingBroadcast(BroadcastService):
    def __init__(self) -> None:
        super().__init__(redis_svc=None)
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        self.published.append((channel, data))


@pytest.fixture
def broadcast() -> _CapturingBroadcast:
    return _CapturingBroadcast()


# ---------- helpers ----------


async def _seed_tick(
    redis_svc: RedisService,
    symbol: str,
    bid: float,
    ask: float,
    *,
    age_ms: int = 0,
) -> None:
    """Set tick:{symbol} JSON cache. ``age_ms`` lets tests simulate
    stale ticks by back-dating the ts."""
    ts = int(time.time() * 1000) - age_ms
    await redis_svc.set_tick_cache(
        symbol,
        json.dumps({"type": "tick", "symbol": symbol, "bid": bid, "ask": ask, "ts": ts}),
    )


async def _seed_symbol_config(
    redis_svc: RedisService,
    symbol: str = "EURUSD",
    *,
    lot_size: int = 10_000_000,
) -> None:
    """Symbol config with a cTrader-style lot_size (=contract_size * 100)."""
    await redis_svc.set_symbol_config(
        symbol,
        {
            "ftmo_symbol": symbol,
            "ctrader_symbol_id": 1,
            "lot_size": lot_size,
            "min_volume": 1000,
            "max_volume": 1_000_000_000,
            "step_volume": 1,
            "digits": 5,
        },
    )


async def _seed_filled_order(
    redis_svc: RedisService,
    *,
    order_id: str = "ord_a",
    symbol: str = "EURUSD",
    side: str = "buy",
    p_status: str = "filled",
    p_fill_price: str = "1.17500",
    p_volume_lots: str = "0.01",
    p_money_digits: str = "2",
    ftmo_account_id: str = "ftmo_001",
) -> None:
    fields: dict[str, str] = {
        "order_id": order_id,
        "pair_id": "pair_001",
        "ftmo_account_id": ftmo_account_id,
        "exness_account_id": "exness_001",
        "symbol": symbol,
        "side": side,
        "order_type": "market",
        "status": p_status,
        "p_status": p_status,
        "p_volume_lots": p_volume_lots,
        "p_fill_price": p_fill_price,
        "p_money_digits": p_money_digits,
        "created_at": "1735000000000",
        "updated_at": "1735000000000",
    }
    await redis_svc.create_order(order_id, fields)


# ---------- _derive_quote_currency ----------


def test_derive_quote_currency_fx_eurusd() -> None:
    assert _derive_quote_currency("EURUSD") == "USD"


def test_derive_quote_currency_fx_usdjpy() -> None:
    assert _derive_quote_currency("USDJPY") == "JPY"


def test_derive_quote_currency_fx_eurgbp_cross() -> None:
    assert _derive_quote_currency("EURGBP") == "GBP"


def test_derive_quote_currency_non_fx_defaults_usd() -> None:
    """Indices, metals, crypto — not 6-char FX → default USD."""
    assert _derive_quote_currency("XAUUSD.x") == "USD"
    assert _derive_quote_currency("US500") == "USD"
    assert _derive_quote_currency("BTC-USDT") == "USD"


# ---------- _compute_pnl: USD quote ----------


@pytest.mark.asyncio
async def test_compute_pnl_buy_eurusd_profit(
    redis_svc: RedisService,
) -> None:
    """BUY 0.01 lot EURUSD from 1.17500 to bid 1.17600 = +10 pips.
    contract_size = 10_000_000 / 100 = 100_000.
    volume_base = 0.01 * 100_000 = 1_000 base.
    pnl_quote = 0.001 * 1_000 = 1.0 USD = 100 raw (money_digits=2).
    """
    await _seed_symbol_config(redis_svc)
    order = {
        "symbol": "EURUSD",
        "side": "buy",
        "p_fill_price": "1.17500",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    sym = (await redis_svc.get_symbol_config("EURUSD")) or {}
    tick = {"bid": 1.17600, "ask": 1.17602}
    pnl_raw, stale, cur = await _compute_pnl(redis_svc, order, sym, tick)
    assert pnl_raw == 100
    assert stale is False
    assert cur == pytest.approx(1.17600)


@pytest.mark.asyncio
async def test_compute_pnl_buy_eurusd_loss(
    redis_svc: RedisService,
) -> None:
    """BUY 0.01 EURUSD from 1.17500 to bid 1.17400 = -10 pips = -100 raw."""
    await _seed_symbol_config(redis_svc)
    order = {
        "symbol": "EURUSD",
        "side": "buy",
        "p_fill_price": "1.17500",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    sym = (await redis_svc.get_symbol_config("EURUSD")) or {}
    tick = {"bid": 1.17400, "ask": 1.17402}
    pnl_raw, _, _ = await _compute_pnl(redis_svc, order, sym, tick)
    assert pnl_raw == -100


@pytest.mark.asyncio
async def test_compute_pnl_sell_eurusd_profit(
    redis_svc: RedisService,
) -> None:
    """SELL 0.01 EURUSD from 1.17500: ASK 1.17400 = +10 pips profit (close at ask)."""
    await _seed_symbol_config(redis_svc)
    order = {
        "symbol": "EURUSD",
        "side": "sell",
        "p_fill_price": "1.17500",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    sym = (await redis_svc.get_symbol_config("EURUSD")) or {}
    tick = {"bid": 1.17400, "ask": 1.17400}
    pnl_raw, _, cur = await _compute_pnl(redis_svc, order, sym, tick)
    assert pnl_raw == 100
    assert cur == pytest.approx(1.17400)  # close side = ask for SELL


@pytest.mark.asyncio
async def test_compute_pnl_buy_uses_bid_sell_uses_ask(
    redis_svc: RedisService,
) -> None:
    """Pin the close-side price selection: BUY → bid, SELL → ask."""
    await _seed_symbol_config(redis_svc)
    sym = (await redis_svc.get_symbol_config("EURUSD")) or {}
    tick = {"bid": 1.10, "ask": 1.20}
    order_buy = {
        "symbol": "EURUSD",
        "side": "buy",
        "p_fill_price": "1.0",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    _, _, cur_buy = await _compute_pnl(redis_svc, order_buy, sym, tick)
    order_sell = {
        "symbol": "EURUSD",
        "side": "sell",
        "p_fill_price": "1.0",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    _, _, cur_sell = await _compute_pnl(redis_svc, order_sell, sym, tick)
    assert cur_buy == pytest.approx(1.10)
    assert cur_sell == pytest.approx(1.20)


# ---------- _compute_pnl: JPY quote ----------


@pytest.mark.asyncio
async def test_compute_pnl_usdjpy_via_usdjpy_bid(
    redis_svc: RedisService,
) -> None:
    """USDJPY is itself the conversion rate. BUY 0.01 lot from 150.000
    to 150.500 = +0.500 JPY per base unit. volume_base = 0.01 * 100_000
    = 1_000 USD (since USDJPY base is USD). pnl_quote = 500 JPY.
    Convert to USD via USDJPY bid 150.500 → 500/150.5 ≈ 3.32 USD.
    Raw money_digits=2 → 332."""
    await _seed_symbol_config(redis_svc, "USDJPY")
    # Conversion needs USDJPY tick to be present in cache (we use the
    # same symbol since USDJPY's quote IS JPY and its rate IS USDJPY).
    await _seed_tick(redis_svc, "USDJPY", bid=150.500, ask=150.502)
    order = {
        "symbol": "USDJPY",
        "side": "buy",
        "p_fill_price": "150.000",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    sym = (await redis_svc.get_symbol_config("USDJPY")) or {}
    tick = {"bid": 150.500, "ask": 150.502}
    pnl_raw, stale, _ = await _compute_pnl(redis_svc, order, sym, tick)
    # 500 / 150.5 = 3.32225... × 100 = 332.225 → 332.
    assert pnl_raw == 332
    assert stale is False


@pytest.mark.asyncio
async def test_compute_pnl_jpy_quote_no_usdjpy_tick_flags_stale(
    redis_svc: RedisService,
) -> None:
    """A JPY pair with USDJPY tick missing → fall back to raw quote
    value and flag is_stale=True."""
    await _seed_symbol_config(redis_svc, "USDJPY")
    # Do NOT seed USDJPY tick cache.
    order = {
        "symbol": "USDJPY",
        "side": "buy",
        "p_fill_price": "150.000",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    sym = (await redis_svc.get_symbol_config("USDJPY")) or {}
    tick = {"bid": 150.500, "ask": 150.502}
    pnl_raw, stale, _ = await _compute_pnl(redis_svc, order, sym, tick)
    # Raw pnl_quote = 500 (no division by USDJPY). 500 * 100 = 50000.
    assert pnl_raw == 50000
    assert stale is True


# ---------- _compute_pnl: cross quote ----------


@pytest.mark.asyncio
async def test_compute_pnl_eurgbp_via_usdgbp_direct_cross(
    redis_svc: RedisService,
) -> None:
    """EURGBP quote=GBP. Pnl in GBP → convert via USDGBP bid (divide).

    BUY 0.01 EURGBP from 0.85000 to bid 0.85100 = +0.001 GBP per EUR.
    volume_base = 0.01 * 100_000 = 1000 EUR. pnl_gbp = 1.0 GBP.
    USDGBP bid 0.79000 → pnl_usd = 1.0 / 0.79 = 1.2658... → 127 raw.
    """
    await _seed_symbol_config(redis_svc, "EURGBP")
    await _seed_tick(redis_svc, "USDGBP", bid=0.79000, ask=0.79002)
    order = {
        "symbol": "EURGBP",
        "side": "buy",
        "p_fill_price": "0.85000",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    sym = (await redis_svc.get_symbol_config("EURGBP")) or {}
    tick = {"bid": 0.85100, "ask": 0.85102}
    pnl_raw, stale, _ = await _compute_pnl(redis_svc, order, sym, tick)
    assert pnl_raw == 127
    assert stale is False


@pytest.mark.asyncio
async def test_compute_pnl_cross_inverse_falls_back_to_quoteusd(
    redis_svc: RedisService,
) -> None:
    """When USDxxx isn't cached but xxxUSD is, use the inverse: multiply
    by the xxxUSD bid. Verifies the fallback path in `_convert_to_usd`."""
    # Symbol with quote=EUR. Imagine a hypothetical xxxEUR pair.
    # USDEUR not cached but EURUSD is.
    await _seed_symbol_config(redis_svc, "AUDEUR")
    await _seed_tick(redis_svc, "EURUSD", bid=1.10000, ask=1.10002)
    order = {
        "symbol": "AUDEUR",
        "side": "buy",
        "p_fill_price": "0.60000",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    sym = (await redis_svc.get_symbol_config("AUDEUR")) or {}
    tick = {"bid": 0.60100, "ask": 0.60102}
    pnl_raw, stale, _ = await _compute_pnl(redis_svc, order, sym, tick)
    # pnl_quote = 0.001 * 1000 = 1.0 EUR.
    # EURUSD bid = 1.1 → 1.0 * 1.1 = 1.10 USD → 110 raw.
    assert pnl_raw == 110
    assert stale is False


@pytest.mark.asyncio
async def test_compute_pnl_unknown_quote_no_conversion_flags_stale(
    redis_svc: RedisService,
) -> None:
    """No cross tick AND no inverse tick → return raw + stale=True."""
    await _seed_symbol_config(redis_svc, "AAABBB")
    order = {
        "symbol": "AAABBB",
        "side": "buy",
        "p_fill_price": "1.0",
        "p_volume_lots": "0.01",
        "p_money_digits": "2",
    }
    sym = (await redis_svc.get_symbol_config("AAABBB")) or {}
    tick = {"bid": 1.1, "ask": 1.1}
    _, stale, _ = await _compute_pnl(redis_svc, order, sym, tick)
    assert stale is True


# ---------- _convert_to_usd direct tests ----------


@pytest.mark.asyncio
async def test_convert_usd_quote_passthrough(redis_svc: RedisService) -> None:
    """USD quote → no conversion call, no stale."""
    val, stale = await _convert_to_usd(redis_svc, pnl_quote=42.5, quote_currency="USD")
    assert val == 42.5
    assert stale is False


@pytest.mark.asyncio
async def test_convert_jpy_quote_uses_usdjpy_bid(
    redis_svc: RedisService,
) -> None:
    await _seed_tick(redis_svc, "USDJPY", bid=150.0, ask=150.1)
    val, stale = await _convert_to_usd(redis_svc, pnl_quote=300.0, quote_currency="JPY")
    assert val == pytest.approx(2.0)
    assert stale is False


@pytest.mark.asyncio
async def test_convert_jpy_zero_rate_flags_stale(
    redis_svc: RedisService,
) -> None:
    """Defensive: a degenerate 0 bid (cTrader spec rejection) → stale."""
    await _seed_tick(redis_svc, "USDJPY", bid=0.0, ask=0.0)
    val, stale = await _convert_to_usd(redis_svc, pnl_quote=300.0, quote_currency="JPY")
    assert val == 300.0  # raw
    assert stale is True


# ---------- _run_one_cycle: skip criteria ----------


@pytest.mark.asyncio
async def test_cycle_skips_pending_order(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_filled_order(redis_svc, p_status="pending")
    await _seed_symbol_config(redis_svc)
    await _seed_tick(redis_svc, "EURUSD", 1.176, 1.176)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    assert broadcast.published == []
    # No position_cache entry.
    assert await redis_client.exists("position_cache:ord_a") == 0


@pytest.mark.asyncio
async def test_cycle_skips_closed_order(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_filled_order(redis_svc, p_status="closed")
    await _seed_symbol_config(redis_svc)
    await _seed_tick(redis_svc, "EURUSD", 1.176, 1.176)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    # Closed orders don't appear in list_open_orders_by_account → no
    # publish even if there were a tick.
    assert broadcast.published == []


@pytest.mark.asyncio
async def test_cycle_skips_when_symbol_config_missing(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _seed_filled_order(redis_svc)
    # No symbol_config.
    await _seed_tick(redis_svc, "EURUSD", 1.176, 1.176)
    with caplog.at_level("WARNING"):
        await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    assert "symbol_config missing" in caplog.text
    assert broadcast.published == []


@pytest.mark.asyncio
async def test_cycle_skips_when_tick_missing(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_filled_order(redis_svc)
    await _seed_symbol_config(redis_svc)
    # No tick.
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    assert broadcast.published == []


# ---------- _run_one_cycle: happy path + position_cache ----------


@pytest.mark.asyncio
async def test_cycle_writes_position_cache_with_ttl(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    await _seed_filled_order(redis_svc)
    await _seed_symbol_config(redis_svc)
    await _seed_tick(redis_svc, "EURUSD", 1.17600, 1.17602)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    cache = await redis_client.hgetall("position_cache:ord_a")  # type: ignore[misc]
    assert cache["order_id"] == "ord_a"
    assert cache["symbol"] == "EURUSD"
    assert cache["side"] == "buy"
    assert cache["volume_lots"] == "0.01"
    assert cache["entry_price"] == "1.17500"
    assert cache["current_price"] == "1.176"
    assert cache["unrealized_pnl"] == "100"
    assert cache["is_stale"] == "false"
    # TTL applied (positive int < 600 + small jitter).
    ttl = await redis_client.ttl("position_cache:ord_a")
    assert 0 < ttl <= 600


@pytest.mark.asyncio
async def test_cycle_broadcasts_positions_tick(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    await _seed_filled_order(redis_svc)
    await _seed_symbol_config(redis_svc)
    await _seed_tick(redis_svc, "EURUSD", 1.17600, 1.17602)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    assert len(broadcast.published) == 1
    channel, data = broadcast.published[0]
    assert channel == POSITIONS_CHANNEL
    assert data["type"] == "positions_tick"
    assert data["account_id"] == "ftmo_001"
    assert "ts" in data
    assert len(data["positions"]) == 1
    pos = data["positions"][0]
    assert pos["order_id"] == "ord_a"
    assert pos["symbol"] == "EURUSD"
    assert pos["unrealized_pnl"] == "100"
    assert pos["is_stale"] is False
    assert "tick_age_ms" in pos


@pytest.mark.asyncio
async def test_cycle_empty_batch_no_broadcast(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """No open orders → no broadcast. Frontend should keep its last
    snapshot rather than redrawing an empty table every tick."""
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    assert broadcast.published == []


# ---------- mixed scenarios ----------


@pytest.mark.asyncio
async def test_cycle_mixed_orders_only_filled_in_batch(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """1 filled + 1 pending + 1 closed → batch has only the filled one."""
    await _seed_filled_order(redis_svc, order_id="ord_filled", p_status="filled")
    await _seed_filled_order(redis_svc, order_id="ord_pending", p_status="pending")
    await _seed_filled_order(redis_svc, order_id="ord_closed", p_status="closed")
    await _seed_symbol_config(redis_svc)
    await _seed_tick(redis_svc, "EURUSD", 1.17600, 1.17602)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    assert len(broadcast.published) == 1
    data = broadcast.published[0][1]
    assert len(data["positions"]) == 1
    assert data["positions"][0]["order_id"] == "ord_filled"


# ---------- stale tick ----------


@pytest.mark.asyncio
async def test_cycle_stale_tick_marks_is_stale_true(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """Tick older than 5 s → is_stale=true on cache + broadcast."""
    await _seed_filled_order(redis_svc)
    await _seed_symbol_config(redis_svc)
    # 8 seconds old → over the 5s threshold.
    await _seed_tick(redis_svc, "EURUSD", 1.17600, 1.17602, age_ms=8_000)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    cache = await redis_client.hgetall("position_cache:ord_a")  # type: ignore[misc]
    assert cache["is_stale"] == "true"
    assert int(cache["tick_age_ms"]) >= 8_000
    pos = broadcast.published[0][1]["positions"][0]
    assert pos["is_stale"] is True


@pytest.mark.asyncio
async def test_cycle_fresh_tick_marks_is_stale_false(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """Fresh tick (~0 ms old) → is_stale=false."""
    await _seed_filled_order(redis_svc)
    await _seed_symbol_config(redis_svc)
    await _seed_tick(redis_svc, "EURUSD", 1.17600, 1.17602, age_ms=0)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    cache = await redis_client.hgetall("position_cache:ord_a")  # type: ignore[misc]
    assert cache["is_stale"] == "false"


# ---------- repeated cycles overwrite position_cache ----------


@pytest.mark.asyncio
async def test_repeated_cycles_overwrite_same_position_cache_key(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """Running the cycle twice doesn't create two cache rows — the
    HSET overwrites the same hash."""
    await _seed_filled_order(redis_svc)
    await _seed_symbol_config(redis_svc)
    await _seed_tick(redis_svc, "EURUSD", 1.17600, 1.17602)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    # Move the price.
    await _seed_tick(redis_svc, "EURUSD", 1.17800, 1.17802)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    cache = await redis_client.hgetall("position_cache:ord_a")  # type: ignore[misc]
    assert cache["unrealized_pnl"] == "300"  # +30 pips × $0.10/pip × 100 = 300
    keys = await redis_client.keys("position_cache:*")
    assert keys == ["position_cache:ord_a"]


# ---------- cross-account isolation ----------


@pytest.mark.asyncio
async def test_cycle_ignores_orders_for_other_accounts(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Order belongs to ftmo_002 → not in ftmo_001's cycle batch."""
    await _seed_filled_order(redis_svc, order_id="ord_other", ftmo_account_id="ftmo_002")
    await _seed_symbol_config(redis_svc)
    await _seed_tick(redis_svc, "EURUSD", 1.176, 1.176)
    await _run_one_cycle(redis_svc, broadcast, "ftmo_001")
    assert broadcast.published == []


# ---------- loop semantics ----------


@pytest.mark.asyncio
async def test_loop_exits_on_cancel(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    task = asyncio.create_task(
        position_tracker_loop(redis_svc, broadcast, "ftmo_001", poll_interval_seconds=0.05)
    )
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loop_survives_cycle_exception(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a single cycle raises, the loop logs and continues on the
    next poll iteration."""
    call_count = 0
    real_run = _run_one_cycle

    async def flaky_run(*args: Any, **kw: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated cycle failure")
        await real_run(*args, **kw)

    monkeypatch.setattr("app.services.position_tracker._run_one_cycle", flaky_run)

    task = asyncio.create_task(
        position_tracker_loop(redis_svc, broadcast, "ftmo_001", poll_interval_seconds=0.05)
    )
    # Let both attempts run (first raises, second succeeds).
    await asyncio.sleep(0.20)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert call_count >= 2


@pytest.mark.asyncio
async def test_loop_polls_at_interval(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Cycle invocation count grows over ~3 ticks at a 50ms interval."""
    await _seed_filled_order(redis_svc)
    await _seed_symbol_config(redis_svc)
    await _seed_tick(redis_svc, "EURUSD", 1.17600, 1.17602)
    task = asyncio.create_task(
        position_tracker_loop(redis_svc, broadcast, "ftmo_001", poll_interval_seconds=0.05)
    )
    await asyncio.sleep(0.20)  # ~3-4 cycles
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # At least 2 broadcasts → confirms multiple cycles fired.
    assert len(broadcast.published) >= 2


# ---------- lifespan wiring ----------


@pytest.mark.asyncio
async def test_lifespan_starts_position_tracker_per_account(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two FTMO accounts → 2 position_tracker tasks named
    ``position_tracker_{acc}``."""
    from app import main as main_module  # noqa: PLC0415
    from app import redis_client as redis_client_module  # noqa: PLC0415
    from app.main import app, lifespan  # noqa: PLC0415

    async def _stub_init_redis(_url: str) -> None:
        return None

    async def _noop_async() -> None:
        return None

    monkeypatch.setattr(redis_client_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(redis_client_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(redis_client_module, "close_redis", _noop_async)
    monkeypatch.setattr(main_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(main_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(main_module, "close_redis", _noop_async)

    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", "ftmo_001", name="t1")
    await svc.add_account("ftmo", "ftmo_002", name="t2")

    started: list[str] = []

    async def fake_response(*_a: Any) -> None:
        await asyncio.Event().wait()

    async def fake_event(*_a: Any) -> None:
        await asyncio.Event().wait()

    async def fake_position(_svc: Any, _bcast: Any, account_id: str) -> None:
        started.append(account_id)
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "response_handler_loop", fake_response)
    monkeypatch.setattr(main_module, "event_handler_loop", fake_event)
    monkeypatch.setattr(main_module, "position_tracker_loop", fake_position)

    async with lifespan(app):
        for _ in range(20):
            if len(started) >= 2:
                break
            await asyncio.sleep(0.01)
        names = {t.get_name() for t in app.state.position_tracker_tasks}
        assert names == {"position_tracker_ftmo_001", "position_tracker_ftmo_002"}

    assert sorted(started) == ["ftmo_001", "ftmo_002"]


@pytest.mark.asyncio
async def test_lifespan_cancels_position_tracker_on_shutdown(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module  # noqa: PLC0415
    from app import redis_client as redis_client_module  # noqa: PLC0415
    from app.main import app, lifespan  # noqa: PLC0415

    async def _stub_init_redis(_url: str) -> None:
        return None

    async def _noop_async() -> None:
        return None

    monkeypatch.setattr(redis_client_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(redis_client_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(redis_client_module, "close_redis", _noop_async)
    monkeypatch.setattr(main_module, "init_redis", _stub_init_redis)
    monkeypatch.setattr(main_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(main_module, "close_redis", _noop_async)

    svc = RedisService(fake_redis)
    await svc.add_account("ftmo", "ftmo_001", name="t1")

    cancelled = 0
    started = 0

    async def fake_noop(*_a: Any) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    async def fake_position(*_a: Any) -> None:
        nonlocal started, cancelled
        started += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    monkeypatch.setattr(main_module, "response_handler_loop", fake_noop)
    monkeypatch.setattr(main_module, "event_handler_loop", fake_noop)
    monkeypatch.setattr(main_module, "position_tracker_loop", fake_position)

    async with lifespan(app):
        for _ in range(20):
            if started >= 1:
                break
            await asyncio.sleep(0.01)
    assert started == 1
    assert cancelled == 1


# ---------- redis cache HSET / get round trip ----------


@pytest_asyncio.fixture
async def cache_seeded(redis_svc: RedisService) -> None:
    await redis_svc.set_position_cache(
        "ord_x",
        {
            "order_id": "ord_x",
            "symbol": "EURUSD",
            "current_price": "1.18000",
            "unrealized_pnl": "1234",
            "is_stale": "false",
        },
    )


@pytest.mark.asyncio
async def test_set_get_position_cache_roundtrip(
    redis_svc: RedisService,
    cache_seeded: None,
) -> None:
    cached = await redis_svc.get_position_cache("ord_x")
    assert cached is not None
    assert cached["symbol"] == "EURUSD"
    assert cached["unrealized_pnl"] == "1234"


@pytest.mark.asyncio
async def test_get_position_cache_missing_returns_none(
    redis_svc: RedisService,
) -> None:
    assert await redis_svc.get_position_cache("nope") is None
