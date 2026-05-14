"""Tests for ``SymbolSyncPublisher`` (Phase 4.2)."""

from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from exness_client import mt5_stub
from exness_client.symbol_sync import SymbolSyncPublisher, _derive_pip_size


def _stub_symbol(
    name: str,
    *,
    digits: int = 5,
    point: float = 0.00001,
    contract_size: float = 100000.0,
    trade_mode: int | None = None,
    currency_profit: str = "USD",
    volume_min: float = 0.01,
    volume_step: float = 0.01,
    volume_max: float = 200.0,
) -> mt5_stub.SymbolInfo:
    return mt5_stub.SymbolInfo(
        name=name,
        trade_contract_size=contract_size,
        digits=digits,
        point=point,
        volume_min=volume_min,
        volume_step=volume_step,
        volume_max=volume_max,
        currency_profit=currency_profit,
        trade_mode=mt5_stub.SYMBOL_TRADE_MODE_FULL if trade_mode is None else trade_mode,
        bid=1.0,
        ask=1.0001,
    )


# ---------------------------------------------------------------------------
# _derive_pip_size
# ---------------------------------------------------------------------------


def test_pip_size_5_digit_forex() -> None:
    # EURUSD: digits=5, point=0.00001 → pip=0.0001
    assert _derive_pip_size(0.00001, 5) == pytest.approx(0.0001)


def test_pip_size_3_digit_jpy() -> None:
    # USDJPY: digits=3, point=0.001 → pip=0.01
    assert _derive_pip_size(0.001, 3) == pytest.approx(0.01)


def test_pip_size_2_digit_metals() -> None:
    # XAUUSD: digits=2, point=0.01 → pip=0.01 (no multiply)
    assert _derive_pip_size(0.01, 2) == pytest.approx(0.01)


def test_pip_size_2_digit_btc() -> None:
    # BTC/USD often digits=2 → pip=point
    assert _derive_pip_size(0.01, 2) == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# publish_snapshot happy / edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_snapshot_writes_full_list_to_redis(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    eurusd = _stub_symbol("EURUSDm")
    usdjpy = _stub_symbol("USDJPYm", digits=3, point=0.001, currency_profit="JPY")
    xauusd = _stub_symbol(
        "XAUUSDm", digits=2, point=0.01, contract_size=100.0
    )
    mt5_stub.set_state_for_tests(
        symbols_get=(eurusd, usdjpy, xauusd),
        symbol_info={s.name: s for s in (eurusd, usdjpy, xauusd)},
    )

    pub = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    count = await pub.publish_snapshot()

    assert count == 3
    raw = await fake_redis.get("exness_raw_symbols:exness_001")
    payload = json.loads(raw)
    assert {s["name"] for s in payload} == {"EURUSDm", "USDJPYm", "XAUUSDm"}
    eurusd_entry = next(s for s in payload if s["name"] == "EURUSDm")
    assert eurusd_entry["pip_size"] == pytest.approx(0.0001)


@pytest.mark.asyncio
async def test_empty_symbols_get_logs_and_returns_zero(
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mt5_stub.set_state_for_tests(symbols_get=())
    pub = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    with caplog.at_level("WARNING"):
        count = await pub.publish_snapshot()
    assert count == 0
    assert any("symbol_sync.empty_symbols_get" in r.message for r in caplog.records)
    assert await fake_redis.get("exness_raw_symbols:exness_001") is None


@pytest.mark.asyncio
async def test_symbol_with_no_info_is_skipped_others_published(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    eurusd = _stub_symbol("EURUSDm")
    ghost = _stub_symbol("GHOSTm")
    mt5_stub.set_state_for_tests(
        symbols_get=(eurusd, ghost),
        symbol_info={"EURUSDm": eurusd},  # GHOSTm absent
    )
    pub = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    count = await pub.publish_snapshot()
    assert count == 1
    payload = json.loads(await fake_redis.get("exness_raw_symbols:exness_001"))
    assert [s["name"] for s in payload] == ["EURUSDm"]


@pytest.mark.asyncio
async def test_non_full_trade_mode_skipped(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    closed = _stub_symbol("DISABLED", trade_mode=0)
    open_sym = _stub_symbol("EURUSDm")
    mt5_stub.set_state_for_tests(
        symbols_get=(closed, open_sym),
        symbol_info={"DISABLED": closed, "EURUSDm": open_sym},
    )
    pub = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    count = await pub.publish_snapshot()
    assert count == 1
    payload = json.loads(await fake_redis.get("exness_raw_symbols:exness_001"))
    assert payload[0]["name"] == "EURUSDm"


@pytest.mark.asyncio
async def test_all_filtered_out_returns_zero_no_redis_write(
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    closed = _stub_symbol("DISABLED", trade_mode=0)
    mt5_stub.set_state_for_tests(
        symbols_get=(closed,), symbol_info={"DISABLED": closed}
    )
    pub = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    with caplog.at_level("WARNING"):
        count = await pub.publish_snapshot()
    assert count == 0
    assert await fake_redis.get("exness_raw_symbols:exness_001") is None
    assert any(
        "symbol_sync.empty_filtered_snapshot" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_symbol_select_called_per_symbol(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    s1 = _stub_symbol("EURUSDm")
    s2 = _stub_symbol("GBPUSDm")
    mt5_stub.set_state_for_tests(
        symbols_get=(s1, s2), symbol_info={"EURUSDm": s1, "GBPUSDm": s2}
    )
    pub = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    await pub.publish_snapshot()
    calls = mt5_stub._state["symbol_select_calls"]
    assert ("EURUSDm", True) in calls
    assert ("GBPUSDm", True) in calls


@pytest.mark.asyncio
async def test_symbols_get_raises_returns_zero(
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> tuple[mt5_stub.SymbolInfo, ...]:
        raise RuntimeError("broker_offline")

    monkeypatch.setattr(mt5_stub, "symbols_get", _boom)
    pub = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    with caplog.at_level("ERROR"):
        count = await pub.publish_snapshot()
    assert count == 0
    assert any(
        "symbol_sync.symbols_get_failed" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_per_symbol_exception_does_not_poison_snapshot(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eurusd = _stub_symbol("EURUSDm")
    flaky = _stub_symbol("FLAKYm")
    mt5_stub.set_state_for_tests(
        symbols_get=(eurusd, flaky),
        symbol_info={"EURUSDm": eurusd, "FLAKYm": flaky},
    )

    real_select = mt5_stub.symbol_select

    def _boom_for_flaky(name: str, enable: bool = True) -> bool:
        if name == "FLAKYm":
            raise RuntimeError("simulated_select_failure")
        return real_select(name, enable)

    monkeypatch.setattr(mt5_stub, "symbol_select", _boom_for_flaky)
    pub = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    count = await pub.publish_snapshot()
    assert count == 1
    payload = json.loads(await fake_redis.get("exness_raw_symbols:exness_001"))
    assert [s["name"] for s in payload] == ["EURUSDm"]


@pytest.mark.asyncio
async def test_redis_set_failure_returns_zero(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    eurusd = _stub_symbol("EURUSDm")
    mt5_stub.set_state_for_tests(
        symbols_get=(eurusd,), symbol_info={"EURUSDm": eurusd}
    )

    async def _boom_set(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("redis_unreachable")

    monkeypatch.setattr(fake_redis, "set", _boom_set)
    pub = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    with caplog.at_level("ERROR"):
        count = await pub.publish_snapshot()
    assert count == 0
    assert any(
        "symbol_sync.redis_set_failed" in r.message for r in caplog.records
    )
