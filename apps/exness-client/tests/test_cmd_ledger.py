"""Tests for ``CmdLedger`` (Phase 4.3a).

The ledger is intentionally tolerant of Redis failures: ``mark`` /
``clear`` log + swallow, ``is_server_initiated`` returns ``False`` on
failure (conservative). These tests pin both the happy-path Redis SET
behaviour and the failure-mode contract.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest

from exness_client.cmd_ledger import CmdLedger


@pytest.fixture
def ledger(fake_redis: fakeredis.aioredis.FakeRedis) -> CmdLedger:
    return CmdLedger(fake_redis, "exness_001")


@pytest.mark.asyncio
async def test_mark_server_initiated_adds_to_set(
    ledger: CmdLedger, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await ledger.mark_server_initiated(12345)
    members = await fake_redis.smembers(ledger.key)
    assert "12345" in members


@pytest.mark.asyncio
async def test_mark_sets_24h_ttl(
    ledger: CmdLedger, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await ledger.mark_server_initiated(12345)
    ttl = await fake_redis.ttl(ledger.key)
    # TTL is in seconds; 24h = 86400. Allow 1s drift.
    assert 86399 <= ttl <= 86400


@pytest.mark.asyncio
async def test_is_server_initiated_returns_true_for_marked(
    ledger: CmdLedger,
) -> None:
    await ledger.mark_server_initiated(99)
    assert await ledger.is_server_initiated(99) is True


@pytest.mark.asyncio
async def test_is_server_initiated_returns_false_for_unmarked(
    ledger: CmdLedger,
) -> None:
    assert await ledger.is_server_initiated(99) is False


@pytest.mark.asyncio
async def test_clear_removes_from_set(
    ledger: CmdLedger, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    await ledger.mark_server_initiated(77)
    await ledger.clear(77)
    assert await ledger.is_server_initiated(77) is False
    members = await fake_redis.smembers(ledger.key)
    assert "77" not in members


@pytest.mark.asyncio
async def test_mark_failure_logged_no_crash(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = CmdLedger(fake_redis, "exness_001")

    async def _boom_sadd(*_args: Any, **_kw: Any) -> None:
        raise RuntimeError("redis_dropped")

    monkeypatch.setattr(fake_redis, "sadd", _boom_sadd)
    with caplog.at_level("WARNING"):
        await ledger.mark_server_initiated(42)
    assert any("cmd_ledger.mark_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_is_server_initiated_failure_returns_false(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = CmdLedger(fake_redis, "exness_001")

    async def _boom_sismember(*_args: Any, **_kw: Any) -> None:
        raise RuntimeError("redis_dropped")

    monkeypatch.setattr(fake_redis, "sismember", _boom_sismember)
    with caplog.at_level("WARNING"):
        result = await ledger.is_server_initiated(42)
    assert result is False
    assert any("cmd_ledger.check_failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_clear_failure_logged_no_crash(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = CmdLedger(fake_redis, "exness_001")
    await ledger.mark_server_initiated(42)

    async def _boom_srem(*_args: Any, **_kw: Any) -> None:
        raise RuntimeError("redis_dropped")

    monkeypatch.setattr(fake_redis, "srem", _boom_srem)
    with caplog.at_level("WARNING"):
        await ledger.clear(42)
    assert any("cmd_ledger.clear_failed" in r.message for r in caplog.records)
