"""Stub action handler tests.

Each handler logs and returns. Step 3.4 will replace these with real
broker calls; the test surface is intentionally minimal so the rewrite
doesn't have to maintain backward compatibility with stub-era assertions.
"""

from __future__ import annotations

import logging

import fakeredis.aioredis
import pytest

from ftmo_client.action_handlers import (
    ACTION_HANDLERS,
    handle_close_stub,
    handle_modify_sl_tp_stub,
    handle_open_stub,
)


@pytest.mark.asyncio
async def test_handle_open_stub_logs_and_returns(
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fields = {
        "order_id": "ord_001",
        "symbol": "EURUSD",
        "side": "buy",
        "volume_lots": "0.01",
        "order_type": "market",
        "entry_price": "0",
        "sl": "1.08000",
        "tp": "0",
        "request_id": "req_001",
    }
    with caplog.at_level(logging.INFO):
        await handle_open_stub(fake_redis, "ftmo_001", fields)
    assert any(
        "[STUB step 3.4] open" in rec.message and "ord_001" in rec.message for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_handle_close_stub_logs_and_returns(
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fields = {
        "order_id": "ord_002",
        "broker_order_id": "987654321",
        "request_id": "req_002",
    }
    with caplog.at_level(logging.INFO):
        await handle_close_stub(fake_redis, "ftmo_001", fields)
    assert any(
        "[STUB step 3.4] close" in rec.message and "987654321" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_handle_modify_sl_tp_stub_logs_and_returns(
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fields = {
        "order_id": "ord_003",
        "broker_order_id": "987654321",
        "sl": "1.07900",
        "tp": "1.09100",
        "request_id": "req_003",
    }
    with caplog.at_level(logging.INFO):
        await handle_modify_sl_tp_stub(fake_redis, "ftmo_001", fields)
    assert any(
        "[STUB step 3.4] modify_sl_tp" in rec.message and "1.09100" in rec.message
        for rec in caplog.records
    )


def test_action_handlers_registry_covers_protocol_actions() -> None:
    """Dispatch table must list all 3 actions defined in
    ``docs/05-redis-protocol.md §4`` (open, close, modify_sl_tp)."""
    assert set(ACTION_HANDLERS) == {"open", "close", "modify_sl_tp"}
