"""Tests for ``account_info.account_info_loop`` + bridge.get_account_info.

Combines:
- Bridge-side: ``get_account_info`` builds correct protobufs and
  reads ProtoOATraderRes + ProtoOAReconcileRes correctly.
- Loop-side: ``publish_once`` HSETs the right shape;
  ``account_info_loop`` polls + exits cleanly on shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAReconcileReq,
    ProtoOAReconcileRes,
    ProtoOATraderReq,
    ProtoOATraderRes,
)

from ftmo_client.account_info import account_info_loop, publish_once
from ftmo_client.ctrader_bridge import AccountInfo, CtraderBridge
from ftmo_client.shutdown import ShutdownController


def _bridge() -> CtraderBridge:
    return CtraderBridge(
        account_id="ftmo_001",
        access_token="acc",
        ctid_trader_account_id=42,
        client_id="cid",
        client_secret="sec",
    )


def _make_trader_res(balance: int = 100_000, money_digits: int = 2) -> ProtoOATraderRes:
    res = ProtoOATraderRes()
    res.ctidTraderAccountId = 42
    res.trader.ctidTraderAccountId = 42
    res.trader.balance = balance
    res.trader.depositAssetId = 1  # USD asset id in cTrader; unused (default "USD")
    res.trader.moneyDigits = money_digits
    return res


def _make_reconcile_res(used_margins: list[int]) -> ProtoOAReconcileRes:
    res = ProtoOAReconcileRes()
    res.ctidTraderAccountId = 42
    for i, um in enumerate(used_margins, start=1):
        pos = res.position.add()
        pos.positionId = i
        pos.tradeData.symbolId = 1
        pos.tradeData.volume = 100_000
        pos.tradeData.tradeSide = 1  # BUY
        pos.positionStatus = 1  # POSITION_STATUS_OPEN
        pos.swap = 0
        pos.usedMargin = um
    return res


# ---------- Bridge get_account_info ----------


@pytest.mark.asyncio
async def test_get_account_info_builds_trader_and_reconcile_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge()
    captured: list[Any] = []

    async def stub_send(msg: Any, **_kw: Any) -> Any:
        captured.append(msg)
        if isinstance(msg, ProtoOATraderReq):
            return _make_trader_res(balance=500_000, money_digits=2)
        if isinstance(msg, ProtoOAReconcileReq):
            return _make_reconcile_res(used_margins=[10_000, 25_000])
        raise AssertionError(f"unexpected message {msg!r}")

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    info = await bridge.get_account_info()

    assert len(captured) == 2
    assert isinstance(captured[0], ProtoOATraderReq)
    assert isinstance(captured[1], ProtoOAReconcileReq)
    assert captured[0].ctidTraderAccountId == 42
    assert captured[1].ctidTraderAccountId == 42

    assert info == AccountInfo(
        balance=500_000,
        equity=500_000,  # step 3.5 limitation: equity == balance
        margin=35_000,  # 10_000 + 25_000
        free_margin=465_000,  # 500_000 - 35_000
        currency="USD",
        money_digits=2,
    )


@pytest.mark.asyncio
async def test_get_account_info_no_open_positions_zero_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge()

    async def stub_send(msg: Any, **_kw: Any) -> Any:
        if isinstance(msg, ProtoOATraderReq):
            return _make_trader_res(balance=1_000_000)
        return _make_reconcile_res(used_margins=[])

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    info = await bridge.get_account_info()
    assert info["margin"] == 0
    assert info["free_margin"] == 1_000_000


@pytest.mark.asyncio
async def test_get_account_info_default_money_digits_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ProtoOATrader.moneyDigits is OPTIONAL. When the broker omits it,
    we default to 2 (USD cents)."""
    bridge = _bridge()

    async def stub_send(msg: Any, **_kw: Any) -> Any:
        if isinstance(msg, ProtoOATraderReq):
            res = ProtoOATraderRes()
            res.ctidTraderAccountId = 42
            res.trader.ctidTraderAccountId = 42
            res.trader.balance = 100_000
            res.trader.depositAssetId = 1
            # moneyDigits intentionally unset
            return res
        return _make_reconcile_res(used_margins=[])

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    info = await bridge.get_account_info()
    assert info["money_digits"] == 2


@pytest.mark.asyncio
async def test_get_account_info_raises_on_unexpected_trader_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge()

    async def stub_send(_msg: Any, **_kw: Any) -> Any:
        return "garbage"

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    with pytest.raises(RuntimeError, match="TraderReq"):
        await bridge.get_account_info()


@pytest.mark.asyncio
async def test_get_account_info_raises_on_unexpected_reconcile_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge()

    async def stub_send(msg: Any, **_kw: Any) -> Any:
        if isinstance(msg, ProtoOATraderReq):
            return _make_trader_res()
        return "garbage"

    monkeypatch.setattr(bridge, "_send_and_wait", stub_send)
    with pytest.raises(RuntimeError, match="ReconcileReq"):
        await bridge.get_account_info()


# ---------- publish_once ----------


@pytest.mark.asyncio
async def test_publish_once_hsets_account_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = _bridge()

    async def fake_get_info() -> AccountInfo:
        return AccountInfo(
            balance=500_000,
            equity=500_000,
            margin=10_000,
            free_margin=490_000,
            currency="USD",
            money_digits=2,
        )

    monkeypatch.setattr(bridge, "get_account_info", fake_get_info)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    await publish_once(bridge, redis, "ftmo_001")
    fields = await redis.hgetall("account:ftmo:ftmo_001")  # type: ignore[misc]
    assert fields["balance"] == "500000"
    assert fields["equity"] == "500000"
    assert fields["margin"] == "10000"
    assert fields["free_margin"] == "490000"
    assert fields["currency"] == "USD"
    assert fields["money_digits"] == "2"
    assert "updated_at" in fields


# ---------- account_info_loop ----------


@pytest.mark.asyncio
async def test_account_info_loop_publishes_then_exits_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge()
    poll_count = 0

    async def fake_get_info() -> AccountInfo:
        nonlocal poll_count
        poll_count += 1
        return AccountInfo(
            balance=100_000 + poll_count,
            equity=100_000 + poll_count,
            margin=0,
            free_margin=100_000 + poll_count,
            currency="USD",
            money_digits=2,
        )

    monkeypatch.setattr(bridge, "get_account_info", fake_get_info)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    shutdown = ShutdownController()

    async def run() -> None:
        # Tiny interval so the loop ticks fast in tests.
        await account_info_loop(bridge, redis, "ftmo_001", shutdown, interval_seconds=0.05)

    task = asyncio.create_task(run())
    # Allow at least 2 polls.
    for _ in range(20):
        if poll_count >= 2:
            break
        await asyncio.sleep(0.02)
    shutdown.request_shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    assert poll_count >= 2
    fields = await redis.hgetall("account:ftmo:ftmo_001")  # type: ignore[misc]
    # Latest poll value:
    assert int(fields["balance"]) >= 100_002


@pytest.mark.asyncio
async def test_account_info_loop_continues_on_bridge_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge call raising mid-poll must not exit the loop."""
    bridge = _bridge()
    call_count = 0

    async def flaky_get_info() -> AccountInfo:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated cTrader hiccup")
        return AccountInfo(
            balance=100_000,
            equity=100_000,
            margin=0,
            free_margin=100_000,
            currency="USD",
            money_digits=2,
        )

    monkeypatch.setattr(bridge, "get_account_info", flaky_get_info)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    shutdown = ShutdownController()

    task = asyncio.create_task(
        account_info_loop(bridge, redis, "ftmo_001", shutdown, interval_seconds=0.02)
    )
    for _ in range(50):
        if call_count >= 2 and await redis.exists("account:ftmo:ftmo_001"):
            break
        await asyncio.sleep(0.02)
    shutdown.request_shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    # Both calls happened — error didn't terminate the loop.
    assert call_count >= 2
    # Second call populated Redis.
    fields = await redis.hgetall("account:ftmo:ftmo_001")  # type: ignore[misc]
    assert fields["balance"] == "100000"


@pytest.mark.asyncio
async def test_account_info_loop_exits_promptly_when_shutdown_set_during_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``await asyncio.wait_for(shutdown.wait(), timeout=interval)`` —
    if shutdown is set during the wait, the loop wakes immediately
    rather than waiting out the full interval."""
    bridge = _bridge()

    async def fake_get_info() -> AccountInfo:
        return AccountInfo(
            balance=1, equity=1, margin=0, free_margin=1, currency="USD", money_digits=2
        )

    monkeypatch.setattr(bridge, "get_account_info", fake_get_info)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    shutdown = ShutdownController()

    # Use a deliberately long interval — if the loop honored it, the
    # test would hang for 5 seconds. With prompt shutdown, the loop
    # exits within ~50ms.
    task = asyncio.create_task(
        account_info_loop(bridge, redis, "ftmo_001", shutdown, interval_seconds=5.0)
    )
    # Let first poll complete.
    await asyncio.sleep(0.05)
    shutdown.request_shutdown()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_publish_once_uses_correct_key_pattern() -> None:
    """Key contract: ``account:ftmo:{account_id}``."""

    class _StubBridge:
        async def get_account_info(self) -> AccountInfo:
            return AccountInfo(
                balance=100,
                equity=100,
                margin=0,
                free_margin=100,
                currency="USD",
                money_digits=2,
            )

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await publish_once(_StubBridge(), redis, "acc_xyz")  # type: ignore[arg-type]
    keys = await redis.keys("account:ftmo:*")
    assert keys == ["account:ftmo:acc_xyz"]
