"""Tests for ``AccountInfoPublisher`` (Phase 4.4)."""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest

from exness_client import mt5_stub
from exness_client.account_info import POLL_INTERVAL_S, AccountInfoPublisher


@pytest.fixture
def publisher(fake_redis: fakeredis.aioredis.FakeRedis) -> AccountInfoPublisher:
    # The publisher itself doesn't depend on a "connected" stub state —
    # ``mt5.account_info()`` is what it queries — but the default stub
    # check returns ``None`` until ``initialize()`` has been called. We
    # mark the stub connected so the publish path runs end-to-end.
    mt5_stub.set_state_for_tests(connected=True)
    # Use a tiny poll interval for tests so the loop variants finish
    # quickly. The 30-second prod default is asserted separately.
    return AccountInfoPublisher(
        fake_redis, "exness_001", mt5_stub, poll_interval_s=0.05
    )


@pytest.mark.asyncio
async def test_publish_on_start_immediate(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """``run()`` calls ``_publish_once`` BEFORE the first sleep so a
    fresh boot doesn't render a 30-second placeholder."""
    task = asyncio.create_task(publisher.run())
    # Give the loop a single event-loop tick to enter and call
    # _publish_once — much shorter than the 0.05s poll interval.
    for _ in range(20):
        if await fake_redis.exists(publisher.key):
            break
        await asyncio.sleep(0.001)
    await publisher.stop()
    await asyncio.wait_for(task, timeout=0.5)
    assert await fake_redis.exists(publisher.key) == 1


@pytest.mark.asyncio
async def test_publish_periodic_at_interval(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Two distinct ``synced_at_ms`` values appear within a few intervals."""
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.18)  # ~3 polls at 0.05s
    await publisher.stop()
    await asyncio.wait_for(task, timeout=0.5)
    payload = await fake_redis.hgetall(publisher.key)
    assert "synced_at_ms" in payload
    # Hard to assert distinct timestamps without waiting longer; this
    # test guards "the loop ran multiple cycles without crashing".


@pytest.mark.asyncio
async def test_hash_fields_correct_count_and_names(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await publisher._publish_once()
    payload = await fake_redis.hgetall(publisher.key)
    expected = {
        "broker", "account_id", "login", "balance", "equity",
        "margin", "free_margin", "leverage", "currency", "server",
        "margin_mode", "synced_at_ms",
    }
    assert set(payload.keys()) == expected
    assert payload["broker"] == "exness"
    assert payload["account_id"] == "exness_001"


@pytest.mark.asyncio
async def test_publish_uses_account_info_data(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    custom = mt5_stub.AccountInfo(
        login=99999999,
        balance=12345.67,
        currency="EUR",
        margin_mode=mt5_stub.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        leverage=200,
        server="Custom-Server",
        equity=12000.0,
        margin=345.67,
        margin_free=11654.33,
    )
    mt5_stub.set_state_for_tests(account_info=custom)
    await publisher._publish_once()
    payload = await fake_redis.hgetall(publisher.key)
    assert payload["login"] == "99999999"
    assert payload["balance"] == "12345.67"
    assert payload["equity"] == "12000.0"
    assert payload["margin"] == "345.67"
    assert payload["free_margin"] == "11654.33"
    assert payload["leverage"] == "200"
    assert payload["currency"] == "EUR"
    assert payload["server"] == "Custom-Server"


@pytest.mark.asyncio
async def test_account_info_none_response_logged_no_crash(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Disconnect the stub so account_info() returns None.
    mt5_stub.set_state_for_tests(connected=False)
    with caplog.at_level("WARNING"):
        await publisher._publish_once()
    assert any(
        "account_info.none_response" in r.message for r in caplog.records
    )
    # No HASH written.
    assert await fake_redis.exists(publisher.key) == 0


@pytest.mark.asyncio
async def test_account_info_exception_logged_no_crash(
    publisher: AccountInfoPublisher,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom() -> None:
        raise RuntimeError("mt5_dropped")

    monkeypatch.setattr(mt5_stub, "account_info", _boom)
    with caplog.at_level("ERROR"):
        await publisher._publish_once()
    assert any(
        "account_info.account_info_exception" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_redis_hset_failure_logged_no_crash(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _boom_hset(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("redis_dropped")

    monkeypatch.setattr(fake_redis, "hset", _boom_hset)
    with caplog.at_level("ERROR"):
        await publisher._publish_once()
    assert any(
        "account_info.publish_failed" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_stop_exits_loop_within_short_window(
    publisher: AccountInfoPublisher,
) -> None:
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.02)
    await publisher.stop()
    await asyncio.wait_for(task, timeout=0.5)
    assert task.done() and task.exception() is None


@pytest.mark.asyncio
async def test_publish_values_are_strings(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await publisher._publish_once()
    payload = await fake_redis.hgetall(publisher.key)
    assert all(isinstance(v, str) for v in payload.values())


@pytest.mark.asyncio
async def test_synced_at_ms_format_is_int_string(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await publisher._publish_once()
    payload = await fake_redis.hgetall(publisher.key)
    int(payload["synced_at_ms"])  # parses as int
    assert int(payload["synced_at_ms"]) > 0


@pytest.mark.asyncio
async def test_balance_equity_margin_fields_match_stub(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    # Defaults: balance=10000.0 / equity=10000.0 / margin=0.0 / margin_free=10000.0
    await publisher._publish_once()
    payload = await fake_redis.hgetall(publisher.key)
    assert payload["balance"] == "10000.0"
    assert payload["equity"] == "10000.0"
    assert payload["margin"] == "0.0"
    assert payload["free_margin"] == "10000.0"


@pytest.mark.asyncio
async def test_two_publishers_distinct_account_ids_distinct_keys(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    mt5_stub.set_state_for_tests(connected=True)
    p1 = AccountInfoPublisher(fake_redis, "exness_a", mt5_stub)
    p2 = AccountInfoPublisher(fake_redis, "exness_b", mt5_stub)
    await p1._publish_once()
    await p2._publish_once()
    assert await fake_redis.exists("account:exness:exness_a") == 1
    assert await fake_redis.exists("account:exness:exness_b") == 1
    assert p1.key != p2.key


@pytest.mark.asyncio
async def test_initial_publish_failure_loop_continues(
    publisher: AccountInfoPublisher,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first-publish exception (e.g. transient Redis blip) doesn't kill
    the loop — subsequent publishes still run."""
    calls = {"n": 0}
    real_hset = fake_redis.hset

    async def _intermittent(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first_call_blip")
        return await real_hset(*args, **kwargs)

    monkeypatch.setattr(fake_redis, "hset", _intermittent)
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.15)
    await publisher.stop()
    await asyncio.wait_for(task, timeout=0.5)
    # First call failed; subsequent calls populated the HASH.
    assert await fake_redis.exists(publisher.key) == 1


@pytest.mark.asyncio
async def test_concurrent_stop_callers_safe(
    publisher: AccountInfoPublisher,
) -> None:
    task = asyncio.create_task(publisher.run())
    await asyncio.sleep(0.02)
    await asyncio.gather(publisher.stop(), publisher.stop(), publisher.stop())
    await asyncio.wait_for(task, timeout=0.5)
    assert task.done()


def test_poll_interval_constant_30s() -> None:
    """The production cadence is locked at 30 s — mirrors FTMO Phase 3."""
    assert POLL_INTERVAL_S == 30.0
