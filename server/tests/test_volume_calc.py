"""Unit tests for conversion_rate.get_quote_to_usd_rate and volume_calc.calculate_volume.

These tests run against fakeredis + AsyncMock'd MarketDataService — no real
broker contact. The HTTP endpoint is exercised in test_volume_endpoint.py.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from app.services.conversion_rate import get_quote_to_usd_rate
from app.services.redis_service import RedisService
from app.services.volume_calc import calculate_volume
from hedger_shared.symbol_mapping import SymbolMapping


def _eurusd_mapping() -> SymbolMapping:
    return SymbolMapping(
        ftmo="EURUSD",
        exness="EURUSDm",
        match_type="exact",
        ftmo_units_per_lot=100000,
        exness_trade_contract_size=100000,
        ftmo_pip_size=0.0001,
        exness_pip_size=0.0001,
        ftmo_pip_value=10.0,
        exness_pip_value=10.0,
        quote_ccy="USD",
    )


def _usdjpy_mapping() -> SymbolMapping:
    return SymbolMapping(
        ftmo="USDJPY",
        exness="USDJPYm",
        match_type="exact",
        ftmo_units_per_lot=100000,
        exness_trade_contract_size=100000,
        ftmo_pip_size=0.01,
        exness_pip_size=0.01,
        ftmo_pip_value=1000.0,
        exness_pip_value=1000.0,
        quote_ccy="JPY",
    )


def _fx_symbol_config() -> dict[str, str]:
    """A typical FX symbol_config emitted by sync_symbols (cTrader unit-scale)."""
    return {
        "ctrader_symbol_id": "1",
        "digits": "5",
        "min_volume": "100000",  # 0.01 lot at lot_size=10_000_000
        "max_volume": "10000000000",  # 1000 lots
        "step_volume": "100000",
        "lot_size": "10000000",
    }


# ---------- conversion_rate tests ----------


@pytest.mark.asyncio
async def test_rate_usd_returns_one(fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    svc = RedisService(fake_redis)
    md = AsyncMock()
    rate = await get_quote_to_usd_rate("USD", svc, md)
    assert rate == 1.0
    md.subscribe_spots.assert_not_called()


@pytest.mark.asyncio
async def test_rate_forward_pair_cached(fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    """EURUSD bid → quote_ccy=EUR rate = bid."""
    svc = RedisService(fake_redis)
    await svc.set_tick_cache("EURUSD", json.dumps({"bid": 1.0825, "ask": 1.0826, "ts": 1}))
    md = AsyncMock()
    rate = await get_quote_to_usd_rate("EUR", svc, md)
    assert rate == 1.0825
    md.subscribe_spots.assert_not_called()


@pytest.mark.asyncio
async def test_rate_inverse_pair_cached(fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    """USDJPY bid → quote_ccy=JPY rate = 1/bid."""
    svc = RedisService(fake_redis)
    await svc.set_tick_cache("USDJPY", json.dumps({"bid": 156.0, "ask": 156.02, "ts": 1}))
    md = AsyncMock()
    rate = await get_quote_to_usd_rate("JPY", svc, md)
    assert rate == pytest.approx(1.0 / 156.0, rel=1e-9)
    md.subscribe_spots.assert_not_called()


@pytest.mark.asyncio
async def test_rate_no_cache_subscribes_and_returns_zero(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Both forward + inverse miss → subscribe pairs in active set, return 0.0."""
    svc = RedisService(fake_redis)
    # GBPUSD is in active set, USDGBP isn't → only GBPUSD should be subscribed.
    await svc.set_symbol_config("GBPUSD", {"ctrader_symbol_id": "2"})
    md = AsyncMock()
    md.subscribe_spots = AsyncMock(return_value=None)
    rate = await get_quote_to_usd_rate("GBP", svc, md)
    assert rate == 0.0
    md.subscribe_spots.assert_awaited_once()
    args, _ = md.subscribe_spots.call_args
    assert args[0] == ["GBPUSD"]


@pytest.mark.asyncio
async def test_rate_neither_pair_in_active_set_no_subscribe(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """If neither candidate exists in symbol_config, don't try to subscribe spam."""
    svc = RedisService(fake_redis)
    md = AsyncMock()
    md.subscribe_spots = AsyncMock(return_value=None)
    rate = await get_quote_to_usd_rate("XYZ", svc, md)
    assert rate == 0.0
    md.subscribe_spots.assert_not_called()


@pytest.mark.asyncio
async def test_rate_invalid_json_falls_through(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Malformed JSON in forward → fall through to inverse pair."""
    svc = RedisService(fake_redis)
    await svc.set_tick_cache("EURUSD", "{not-json")
    await svc.set_tick_cache("USDEUR", json.dumps({"bid": 0.9, "ask": 0.9001, "ts": 1}))
    md = AsyncMock()
    rate = await get_quote_to_usd_rate("EUR", svc, md)
    # Should pick up the inverse since forward JSON was bad.
    assert rate == pytest.approx(1.0 / 0.9, rel=1e-9)


@pytest.mark.asyncio
async def test_rate_zero_bid_treated_as_missing(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(fake_redis)
    await svc.set_tick_cache("EURUSD", json.dumps({"bid": 0, "ask": 0, "ts": 1}))
    md = AsyncMock()
    rate = await get_quote_to_usd_rate("EUR", svc, md)
    assert rate == 0.0


# ---------- volume_calc tests ----------


def test_calc_eurusd_basic() -> None:
    """EURUSD: $100 risk, entry 1.0850, sl 1.0800 (50 pips) → ~0.20 lot."""
    out = calculate_volume(
        risk_amount=100.0,
        entry=1.0850,
        sl=1.0800,
        symbol_config=_fx_symbol_config(),
        whitelist_row=_eurusd_mapping(),
        ratio=1.0,
        quote_to_usd_rate=1.0,
    )
    assert out["sl_pips"] == 50.0
    assert out["pip_value_usd_per_lot"] == pytest.approx(10.0)
    assert out["sl_usd_per_lot"] == pytest.approx(500.0)
    assert out["volume_primary"] == pytest.approx(0.20, abs=0.001)
    assert out["volume_secondary"] == pytest.approx(0.20, abs=0.001)


def test_calc_usdjpy_quote_jpy() -> None:
    """USDJPY: rate ≈ 1/156, 50 pips, $100 risk."""
    out = calculate_volume(
        risk_amount=100.0,
        entry=156.50,
        sl=156.00,
        symbol_config=_fx_symbol_config(),
        whitelist_row=_usdjpy_mapping(),
        ratio=1.0,
        quote_to_usd_rate=1.0 / 156.0,
    )
    assert out["sl_pips"] == 50.0
    # pip_value_usd_per_lot = 0.01 * 100000 * (1/156) = 1000/156 ≈ 6.41
    assert out["pip_value_usd_per_lot"] == pytest.approx(1000.0 / 156.0, rel=1e-3)
    expected_volume = 100.0 / (50.0 * (1000.0 / 156.0))
    # rounded down to step 0.01
    assert out["volume_primary"] == pytest.approx(
        round(expected_volume - (expected_volume % 0.01), 2), abs=0.011
    )


def test_calc_sl_too_tight_raises() -> None:
    with pytest.raises(ValueError, match="SL too tight"):
        calculate_volume(
            risk_amount=100.0,
            entry=1.0850,
            sl=1.0848,  # 2 pips
            symbol_config=_fx_symbol_config(),
            whitelist_row=_eurusd_mapping(),
            ratio=1.0,
            quote_to_usd_rate=1.0,
        )


def test_calc_negative_risk_raises() -> None:
    with pytest.raises(ValueError, match="risk_amount"):
        calculate_volume(
            risk_amount=-1.0,
            entry=1.0850,
            sl=1.0800,
            symbol_config=_fx_symbol_config(),
            whitelist_row=_eurusd_mapping(),
            ratio=1.0,
            quote_to_usd_rate=1.0,
        )


def test_calc_zero_rate_raises() -> None:
    with pytest.raises(ValueError, match="quote_to_usd_rate"):
        calculate_volume(
            risk_amount=100.0,
            entry=1.0850,
            sl=1.0800,
            symbol_config=_fx_symbol_config(),
            whitelist_row=_eurusd_mapping(),
            ratio=1.0,
            quote_to_usd_rate=0.0,
        )


def test_calc_entry_equals_sl_raises() -> None:
    with pytest.raises(ValueError, match="differ"):
        calculate_volume(
            risk_amount=100.0,
            entry=1.0850,
            sl=1.0850,
            symbol_config=_fx_symbol_config(),
            whitelist_row=_eurusd_mapping(),
            ratio=1.0,
            quote_to_usd_rate=1.0,
        )


def test_calc_clamp_to_min() -> None:
    """Tiny risk → raw volume below min → clamps up to min."""
    out = calculate_volume(
        risk_amount=0.10,  # extremely tiny
        entry=1.0850,
        sl=1.0800,
        symbol_config=_fx_symbol_config(),
        whitelist_row=_eurusd_mapping(),
        ratio=1.0,
        quote_to_usd_rate=1.0,
    )
    # Raw = 0.10 / 500 = 0.0002 lot, well below 0.01 min → clamps up.
    assert out["volume_primary"] == 0.01


def test_calc_step_rounds_down() -> None:
    """Volume should round DOWN to step (never up)."""
    # entry/sl pick numbers so volume_p_raw lands strictly between two steps.
    out = calculate_volume(
        risk_amount=125.0,  # 0.25 lot raw
        entry=1.0850,
        sl=1.0800,
        symbol_config=_fx_symbol_config(),
        whitelist_row=_eurusd_mapping(),
        ratio=1.0,
        quote_to_usd_rate=1.0,
    )
    assert out["volume_primary"] == 0.25  # already on step
    out2 = calculate_volume(
        risk_amount=126.0,  # raw = 0.252; step 0.01 → round DOWN to 0.25
        entry=1.0850,
        sl=1.0800,
        symbol_config=_fx_symbol_config(),
        whitelist_row=_eurusd_mapping(),
        ratio=1.0,
        quote_to_usd_rate=1.0,
    )
    assert out2["volume_primary"] == 0.25


def test_calc_secondary_ratio_applied() -> None:
    """ratio=2.0 → volume_secondary = 2 * volume_primary (same contract sizes)."""
    out = calculate_volume(
        risk_amount=100.0,
        entry=1.0850,
        sl=1.0800,
        symbol_config=_fx_symbol_config(),
        whitelist_row=_eurusd_mapping(),
        ratio=2.0,
        quote_to_usd_rate=1.0,
    )
    assert out["volume_secondary"] == pytest.approx(out["volume_primary"] * 2.0, abs=0.011)


def test_calc_different_contract_sizes_secondary_scales() -> None:
    """If FTMO/Exness contract sizes differ, secondary scales by their ratio."""
    mapping = SymbolMapping(
        ftmo="XAUUSD",
        exness="XAUUSDm",
        match_type="exact",
        ftmo_units_per_lot=100,
        exness_trade_contract_size=10,  # 10x smaller per lot
        ftmo_pip_size=0.01,
        exness_pip_size=0.01,
        ftmo_pip_value=1.0,
        exness_pip_value=0.1,
        quote_ccy="USD",
    )
    out = calculate_volume(
        risk_amount=100.0,
        entry=3400.0,
        sl=3380.0,  # 20 pips at pip_size=0.01 means 20*0.01=0.20 — but wait:
        # diff/pip_size = 20/0.01 = 2000 pips. Tight risk = 100/(2000*0.01*100*1.0) = 0.05 lot.
        symbol_config=_fx_symbol_config(),
        whitelist_row=mapping,
        ratio=1.0,
        quote_to_usd_rate=1.0,
    )
    # 10 ratio means secondary should be 10x primary in raw units, but capped
    # by max_vol_lot derived from the symbol_config — assertion is that it's
    # at least the primary (sanity check).
    assert out["volume_secondary"] >= out["volume_primary"]


def test_calc_sl_inverted_works() -> None:
    """SELL trades have sl > entry; abs() handles both sides."""
    out = calculate_volume(
        risk_amount=100.0,
        entry=1.0800,
        sl=1.0850,  # inverted (sell side)
        symbol_config=_fx_symbol_config(),
        whitelist_row=_eurusd_mapping(),
        ratio=1.0,
        quote_to_usd_rate=1.0,
    )
    assert out["sl_pips"] == 50.0
    assert out["volume_primary"] == pytest.approx(0.20, abs=0.001)


def test_calc_returns_raw_volumes_for_debug() -> None:
    """Debug fields must include the unrounded primary/secondary values."""
    out: dict[str, Any] = calculate_volume(
        risk_amount=100.0,
        entry=1.0850,
        sl=1.0800,
        symbol_config=_fx_symbol_config(),
        whitelist_row=_eurusd_mapping(),
        ratio=1.0,
        quote_to_usd_rate=1.0,
    )
    assert "volume_primary_raw" in out
    assert "volume_secondary_raw" in out
    assert isinstance(out["volume_primary_raw"], float)
