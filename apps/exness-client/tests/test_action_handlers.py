"""Tests for ``ActionHandler`` (Phase 4.2).

The handler talks to MT5 via the stub + Redis via fakeredis. We assert
the resulting ``resp_stream:exness:{account_id}`` payload because that's
the contract the server's response handler (step 4.7) consumes.
"""

from __future__ import annotations

import logging
from typing import Any

import fakeredis.aioredis
import pytest

from exness_client import mt5_stub
from exness_client.action_handlers import ActionHandler
from exness_client.cmd_ledger import CmdLedger
from exness_client.symbol_sync import SymbolSyncPublisher

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def handler(fake_redis: fakeredis.aioredis.FakeRedis) -> ActionHandler:

    sync = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    ledger = CmdLedger(fake_redis, "exness_001")
    return ActionHandler(fake_redis, "exness_001", mt5_stub, sync, ledger)


def _stub_symbol(
    name: str = "EURUSDm",
    *,
    bid: float = 1.0850,
    ask: float = 1.0852,
    trade_mode: int | None = None,
    filling_mode: int = 3,
) -> mt5_stub.SymbolInfo:
    # Step 4.8a — ``filling_mode`` defaults to ``3`` (FOK+IOC bitmask).
    # Pre-4.8a tests implicitly assumed both modes; the default
    # preserves their behaviour. New 4.8a tests override per-scenario
    # (1 = FOK-only Exness Cent path, 2 = IOC-only, 0 = neither).
    return mt5_stub.SymbolInfo(
        name=name,
        trade_contract_size=100000.0,
        digits=5,
        point=0.00001,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=200.0,
        currency_profit="USD",
        trade_mode=mt5_stub.SYMBOL_TRADE_MODE_FULL if trade_mode is None else trade_mode,
        bid=bid,
        ask=ask,
        filling_mode=filling_mode,
    )


def _send_result(
    *,
    retcode: int = mt5_stub.TRADE_RETCODE_DONE,
    deal: int = 12345,
    order: int = 67890,
    volume: float = 0.10,
    price: float = 1.0852,
    comment: str = "ok",
) -> mt5_stub.OrderSendResult:
    return mt5_stub.OrderSendResult(
        retcode=retcode,
        deal=deal,
        order=order,
        volume=volume,
        price=price,
        bid=price - 0.0001,
        ask=price + 0.0001,
        comment=comment,
    )


async def _read_one_response(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> dict[str, str]:
    """Return the most recent fields dict from ``resp_stream:exness:exness_001``."""
    entries = await fake_redis.xrevrange("resp_stream:exness:exness_001", count=1)
    assert entries, "no response published"
    _msg_id, fields = entries[0]
    return dict(fields)


# ---------------------------------------------------------------------------
# dispatch routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_action_publishes_rejected(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await handler.dispatch(
        {"request_id": "r1", "action": "freeform_dance", "cascade_trigger": "false"}
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "rejected"
    assert payload["reason"] == "unknown_action_freeform_dance"
    assert payload["action"] == "freeform_dance"
    assert payload["request_id"] == "r1"
    assert payload["cascade_trigger"] == "false"


@pytest.mark.asyncio
async def test_response_payload_has_required_envelope(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await handler.dispatch({"request_id": "rX", "action": "unknown"})
    payload = await _read_one_response(fake_redis)
    for required in ("request_id", "action", "status", "reason", "ts_ms", "cascade_trigger"):
        assert required in payload, f"missing field {required}"
    int(payload["ts_ms"])  # parses as int


# ---------------------------------------------------------------------------
# _handle_open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_happy_path_publishes_filled(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
            "magic": "424242",
            "cascade_trigger": "true",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "filled"
    assert payload["reason"] == "ok"
    assert payload["broker_order_id"] == "67890"
    assert payload["broker_position_id"] == "67890"
    assert payload["fill_price"] == "1.0852"
    assert payload["filled_volume"] == "0.1"
    assert payload["retcode"] == str(mt5_stub.TRADE_RETCODE_DONE)
    assert payload["cascade_trigger"] == "true"


@pytest.mark.asyncio
async def test_open_buy_uses_ask_price(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol(bid=1.10, ask=1.11)
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"][0]
    assert sent["price"] == 1.11  # ask for buy


@pytest.mark.asyncio
async def test_open_sell_uses_bid_price(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol(bid=1.10, ask=1.11)
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "sell",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"][0]
    assert sent["price"] == 1.10  # bid for sell


@pytest.mark.asyncio
async def test_open_symbol_not_found_publishes_rejected(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    # symbol_info dict empty → returns None
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "GHOST",
            "side": "buy",
            "volume": "0.10",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "rejected"
    assert payload["reason"] == "symbol_not_found"
    assert payload["symbol"] == "GHOST"


@pytest.mark.asyncio
async def test_open_ioc_to_fok_retry_on_unsupported_filling(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[
            _send_result(retcode=mt5_stub.TRADE_RETCODE_UNSUPPORTED_FILLING),
            _send_result(retcode=mt5_stub.TRADE_RETCODE_DONE),
        ],
    )
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"]
    assert len(sent) == 2
    assert sent[0]["type_filling"] == mt5_stub.ORDER_FILLING_IOC
    assert sent[1]["type_filling"] == mt5_stub.ORDER_FILLING_FOK
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "filled"


@pytest.mark.asyncio
async def test_open_market_closed_publishes_rejected(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=_send_result(retcode=mt5_stub.TRADE_RETCODE_MARKET_CLOSED),
    )
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "rejected"
    assert payload["reason"] == "market_closed"


@pytest.mark.asyncio
async def test_open_invalid_volume_publishes_rejected(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=_send_result(retcode=mt5_stub.TRADE_RETCODE_INVALID_VOLUME),
    )
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "rejected"
    assert payload["reason"] == "invalid_volume"


@pytest.mark.asyncio
async def test_open_no_money_publishes_rejected(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=_send_result(retcode=mt5_stub.TRADE_RETCODE_NO_MONEY),
    )
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "rejected"
    assert payload["reason"] == "insufficient_margin"


@pytest.mark.asyncio
async def test_open_order_send_exception_publishes_error(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_raises=RuntimeError("MT5 disconnected"),
    )
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "error"
    assert payload["reason"] == "order_send_exception_RuntimeError"


@pytest.mark.asyncio
async def test_open_bad_request_missing_symbol(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await handler.dispatch(
        {"request_id": "r1", "action": "open", "side": "buy", "volume": "0.10"}
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "rejected"
    assert payload["reason"].startswith("bad_request_")


@pytest.mark.asyncio
async def test_open_cascade_trigger_default_false(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    await handler.dispatch(
        {
            "request_id": "r1",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["cascade_trigger"] == "false"


# ---------------------------------------------------------------------------
# _handle_close
# ---------------------------------------------------------------------------


def _open_position(
    *,
    ticket: int = 67890,
    symbol: str = "EURUSDm",
    pos_type: int = mt5_stub.POSITION_TYPE_BUY,
    volume: float = 0.10,
) -> mt5_stub.Position:
    return mt5_stub.Position(
        ticket=ticket,
        symbol=symbol,
        type=pos_type,
        volume=volume,
        price_open=1.0850,
        magic=424242,
    )


@pytest.mark.asyncio
async def test_close_happy_path_publishes_closed(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        positions_get=(_open_position(),),
    )
    await handler.dispatch(
        {
            "request_id": "r2",
            "action": "close",
            "broker_position_id": "67890",
            "cascade_trigger": "true",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "closed"
    assert payload["reason"] == "ok"
    assert payload["broker_position_id"] == "67890"
    # Closing a BUY position uses bid (1.0850) — the default stub
    # ``order_send`` echoes ``request['price']`` back into the result.
    assert payload["close_price"] == "1.085"
    assert payload["closed_volume"] == "0.1"
    assert payload["cascade_trigger"] == "true"


@pytest.mark.asyncio
async def test_close_buy_position_sends_sell(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol(bid=1.10, ask=1.11)
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        positions_get=(_open_position(pos_type=mt5_stub.POSITION_TYPE_BUY),),
    )
    await handler.dispatch(
        {
            "request_id": "r2",
            "action": "close",
            "broker_position_id": "67890",
        }
    )
    sent = mt5_stub._state["order_send_calls"][0]
    assert sent["type"] == mt5_stub.ORDER_TYPE_SELL
    assert sent["price"] == 1.10  # bid (closing BUY at bid)


@pytest.mark.asyncio
async def test_close_sell_position_sends_buy(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol(bid=1.10, ask=1.11)
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        positions_get=(_open_position(pos_type=mt5_stub.POSITION_TYPE_SELL),),
    )
    await handler.dispatch(
        {
            "request_id": "r2",
            "action": "close",
            "broker_position_id": "67890",
        }
    )
    sent = mt5_stub._state["order_send_calls"][0]
    assert sent["type"] == mt5_stub.ORDER_TYPE_BUY
    assert sent["price"] == 1.11  # ask (closing SELL at ask)


@pytest.mark.asyncio
async def test_close_position_not_found_publishes_error(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    mt5_stub.set_state_for_tests(positions_get=())
    await handler.dispatch(
        {
            "request_id": "r2",
            "action": "close",
            "broker_position_id": "999",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "error"
    assert payload["reason"] == "position_not_found"
    assert payload["broker_position_id"] == "999"


@pytest.mark.asyncio
async def test_close_order_send_exception_publishes_error(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        positions_get=(_open_position(),),
        order_send_raises=RuntimeError("disconnect"),
    )
    await handler.dispatch(
        {
            "request_id": "r2",
            "action": "close",
            "broker_position_id": "67890",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "error"
    assert payload["reason"] == "order_send_exception_RuntimeError"


@pytest.mark.asyncio
async def test_close_bad_request_missing_position_id(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await handler.dispatch({"request_id": "r2", "action": "close"})
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "rejected"
    assert payload["reason"].startswith("bad_request_")


@pytest.mark.asyncio
async def test_close_market_closed_passes_through_status(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        positions_get=(_open_position(),),
        order_send_response=_send_result(retcode=mt5_stub.TRADE_RETCODE_MARKET_CLOSED),
    )
    await handler.dispatch(
        {
            "request_id": "r2",
            "action": "close",
            "broker_position_id": "67890",
        }
    )
    payload = await _read_one_response(fake_redis)
    # Non-filled outcomes pass through their original status (not 'closed').
    assert payload["status"] == "rejected"
    assert payload["reason"] == "market_closed"


# ---------------------------------------------------------------------------
# _handle_resync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_happy_path_publishes_completed(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbols_get=(sym,), symbol_info={"EURUSDm": sym}
    )
    await handler.dispatch(
        {"request_id": "r3", "action": "resync_symbols"}
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "completed"
    assert payload["reason"] == "resync_ok"
    assert payload["symbol_count"] == "1"


@pytest.mark.asyncio
async def test_resync_publisher_exception_publishes_error(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)

    async def _boom() -> int:
        raise RuntimeError("snap_failed")

    monkeypatch.setattr(sync, "publish_snapshot", _boom)

    ledger = CmdLedger(fake_redis, "exness_001")
    handler = ActionHandler(fake_redis, "exness_001", mt5_stub, sync, ledger)
    await handler.dispatch(
        {"request_id": "r3", "action": "resync_symbols"}
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "error"
    assert payload["reason"] == "resync_exception_RuntimeError"


# ---------------------------------------------------------------------------
# _publish_response shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_response_extras_make_it_through(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    await handler._publish_response(
        request_id="rZ",
        action="probe",
        status="filled",
        reason="ok",
        cascade_trigger="false",
        custom_field_alpha="123",
    )
    payload = await _read_one_response(fake_redis)
    assert payload["custom_field_alpha"] == "123"


# ---------------------------------------------------------------------------
# Phase 4.3a — CmdLedger integration in _handle_close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_handler_close_marks_ledger_before_send(
    handler: ActionHandler, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:

    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        positions_get=(_open_position(),),
    )
    await handler.dispatch(
        {
            "request_id": "rL1",
            "action": "close",
            "broker_position_id": "67890",
        }
    )
    ledger = CmdLedger(fake_redis, "exness_001")
    assert await ledger.is_server_initiated(67890) is True


@pytest.mark.asyncio
async def test_action_handler_close_ledger_failure_does_not_block_send(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flaky Redis on the ledger SADD must NOT block the close cmd —
    the close still fires, the resp_stream still publishes."""

    async def _boom_sadd(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("redis_dropped")

    monkeypatch.setattr(fake_redis, "sadd", _boom_sadd)
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        positions_get=(_open_position(),),
    )
    await handler.dispatch(
        {
            "request_id": "rL2",
            "action": "close",
            "broker_position_id": "67890",
        }
    )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "closed"


@pytest.mark.asyncio
async def test_action_handler_open_does_not_mark_ledger(
    handler: ActionHandler, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:

    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    await handler.dispatch(
        {
            "request_id": "rL3",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    ledger = CmdLedger(fake_redis, "exness_001")
    members = await fake_redis.smembers(ledger.key)
    assert members == set()


# ---------------------------------------------------------------------------
# Step 4.8a — filling-mode bitmask + symbol_select + None-retry hardening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_silent_none_on_ioc_retries_fok_succeeds(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Core fix (D-SMOKE-2): Exness Cent silently returns ``None`` for an
    unsupported filling type instead of retcode 10030. The handler must
    advance to the next filling mode in ``filling_modes`` instead of
    bailing on the first ``None`` (the pre-4.8a behaviour that caused
    all 4 server-side cascade-open retries to fail identically)."""
    sym = _stub_symbol(filling_mode=3)  # both — IOC-first preserved
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[None, _send_result(retcode=mt5_stub.TRADE_RETCODE_DONE)],
    )
    await handler.dispatch(
        {
            "request_id": "r_none_retry",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "sell",
            "volume": "0.23",
        }
    )
    sent = mt5_stub._state["order_send_calls"]
    assert len(sent) == 2
    assert sent[0]["type_filling"] == mt5_stub.ORDER_FILLING_IOC
    assert sent[1]["type_filling"] == mt5_stub.ORDER_FILLING_FOK
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "filled"


@pytest.mark.asyncio
async def test_open_silent_none_on_both_filling_modes_exhausted(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Both modes return None → terminal ``order_send_returned_none`` is
    published only after the list is fully exhausted."""
    sym = _stub_symbol(filling_mode=3)
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[None, None],
    )
    await handler.dispatch(
        {
            "request_id": "r_exhausted",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"]
    assert len(sent) == 2
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "error"
    assert payload["reason"] == "order_send_returned_none"


@pytest.mark.asyncio
async def test_open_filling_mode_bitmask_fok_only_uses_fok_first(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Exness Cent path (filling_mode=1): first attempt uses
    ``ORDER_FILLING_FOK`` so the silent-None historical bug is bypassed
    entirely."""
    sym = _stub_symbol(filling_mode=mt5_stub.SYMBOL_FILLING_FOK)  # 1
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[_send_result(retcode=mt5_stub.TRADE_RETCODE_DONE)],
    )
    await handler.dispatch(
        {
            "request_id": "r_fok",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "sell",
            "volume": "0.23",
        }
    )
    sent = mt5_stub._state["order_send_calls"]
    assert len(sent) == 1
    assert sent[0]["type_filling"] == mt5_stub.ORDER_FILLING_FOK
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "filled"


@pytest.mark.asyncio
async def test_open_filling_mode_bitmask_ioc_only_uses_ioc_first(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """IOC-only broker (filling_mode=2): first attempt uses
    ``ORDER_FILLING_IOC``."""
    sym = _stub_symbol(filling_mode=mt5_stub.SYMBOL_FILLING_IOC)  # 2
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[_send_result(retcode=mt5_stub.TRADE_RETCODE_DONE)],
    )
    await handler.dispatch(
        {
            "request_id": "r_ioc",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"]
    assert len(sent) == 1
    assert sent[0]["type_filling"] == mt5_stub.ORDER_FILLING_IOC


@pytest.mark.asyncio
async def test_open_filling_mode_bitmask_both_preserves_ioc_first(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Standard broker (filling_mode=3 — FOK+IOC both): historical
    IOC-first preference preserved."""
    sym = _stub_symbol(filling_mode=3)
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[_send_result(retcode=mt5_stub.TRADE_RETCODE_DONE)],
    )
    await handler.dispatch(
        {
            "request_id": "r_both",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"]
    assert len(sent) == 1
    assert sent[0]["type_filling"] == mt5_stub.ORDER_FILLING_IOC


@pytest.mark.asyncio
async def test_open_filling_mode_bitmask_zero_fallback_to_return(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Bitmask=0 (no FOK no IOC, e.g. BOC-only) → fall back to
    ``ORDER_FILLING_RETURN`` for stack accounts."""
    sym = _stub_symbol(filling_mode=0)
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[_send_result(retcode=mt5_stub.TRADE_RETCODE_DONE)],
    )
    await handler.dispatch(
        {
            "request_id": "r_return",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"]
    assert len(sent) == 1
    assert sent[0]["type_filling"] == mt5_stub.ORDER_FILLING_RETURN


@pytest.mark.asyncio
async def test_open_symbol_select_failure_publishes_structured_error(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """``mt5.symbol_select`` returns False → publish
    ``status=error reason=symbol_select_failed`` and SKIP the order_send
    loop entirely (operator-actionable: investigate Market Watch)."""
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        symbol_select_response=False,
    )
    await handler.dispatch(
        {
            "request_id": "r_select_fail",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"]
    assert sent == []
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "error"
    assert payload["reason"] == "symbol_select_failed"
    assert payload["symbol"] == "EURUSDm"


@pytest.mark.asyncio
async def test_open_symbol_select_called_before_order_send(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """The belt-and-suspenders ``symbol_select`` must fire BEFORE the
    first ``order_send`` so an inactive symbol is reactivated before the
    terminal validator runs."""
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    await handler.dispatch(
        {
            "request_id": "r_ordering",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    select_calls = mt5_stub._state["symbol_select_calls"]
    send_calls = mt5_stub._state["order_send_calls"]
    assert select_calls == [("EURUSDm", True)]
    assert len(send_calls) == 1
    # Both lists are appended in their respective stubs in dispatch
    # order; the assertion above documents that select_calls is
    # non-empty BEFORE order_send_calls becomes non-empty (handler
    # control flow guarantees the sequence).


@pytest.mark.asyncio
async def test_open_unsupported_filling_retcode_still_advances_loop(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Regression: the retcode 10030 pathway (broker returns a struct
    instead of None) still triggers the next filling mode. This locks
    the historical IOC→FOK behaviour that 4.8a preserves alongside the
    new None-handling branch."""
    sym = _stub_symbol(filling_mode=3)
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[
            _send_result(retcode=mt5_stub.TRADE_RETCODE_UNSUPPORTED_FILLING),
            _send_result(retcode=mt5_stub.TRADE_RETCODE_DONE),
        ],
    )
    await handler.dispatch(
        {
            "request_id": "r_unsupported_retcode",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"]
    assert len(sent) == 2
    assert sent[0]["type_filling"] == mt5_stub.ORDER_FILLING_IOC
    assert sent[1]["type_filling"] == mt5_stub.ORDER_FILLING_FOK
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "filled"


# ---------------------------------------------------------------------------
# Step 4.8b — comment field 29-char limit + neutral prefix + last_error capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_comment_prefix_is_v3_not_hedge(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Step 4.8b — comment prefix must be ``v3:``, not the legacy
    ``hedge:`` which leaks the hedging strategy keyword into the
    broker's server-side audit trail (prop firms may flag detected
    hedging). Operational discovery from CEO smoke 2026-05-15."""
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    await handler.dispatch(
        {
            "request_id": "abc123",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"][0]
    assert sent["comment"].startswith("v3:")
    assert "hedge" not in sent["comment"].lower()


@pytest.mark.asyncio
async def test_open_comment_length_capped_at_29(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Step 4.8b — MT5 Python SDK silently rejects comments of 30+
    chars with ``last_error=(-2, 'Invalid "comment" argument')``.
    The handler must truncate to ``[:29]`` so a 32-char uuid hex
    request_id can't poison the request. Verified by CEO REPL
    2026-05-15."""
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    long_request_id = "abc123def456abc123def456abc123ab"  # 32 chars
    assert len(long_request_id) == 32
    await handler.dispatch(
        {
            "request_id": long_request_id,
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"][0]
    comment = sent["comment"]
    assert len(comment) <= 29
    assert len(comment) == 29  # worst case fully populated


@pytest.mark.asyncio
async def test_open_comment_short_request_id_under_29_chars_no_truncation(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Short request_ids produce comments shorter than 29 chars with
    no truncation artefacts."""
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    await handler.dispatch(
        {
            "request_id": "shortid10",  # 9 chars
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"][0]
    assert sent["comment"] == "v3:shortid10"
    assert len(sent["comment"]) == 12


@pytest.mark.asyncio
async def test_open_full_comment_assembly_matches_expected_format(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Lock the comment-assembly contract: ``v3:`` + first 26 chars of
    request_id == 29 chars total."""
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    request_id = "abc123def456abc123def456abc123ab"  # 32-char hex
    await handler.dispatch(
        {
            "request_id": request_id,
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"][0]
    # 3 chars prefix + 26 chars request_id slice = 29 total.
    expected = "v3:" + request_id[:26]
    assert sent["comment"] == expected
    assert len(sent["comment"]) == 29


@pytest.mark.asyncio
async def test_open_audit_no_hedge_keyword_in_request_dict(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Regression defence: ``hedge`` must not appear anywhere in the
    request dict shipped to ``mt5.order_send``. Stringify the whole
    dict and assert the substring is absent."""
    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(symbol_info={"EURUSDm": sym})
    await handler.dispatch(
        {
            "request_id": "r_audit",
            "action": "open",
            "symbol": "EURUSDm",
            "side": "buy",
            "volume": "0.10",
        }
    )
    sent = mt5_stub._state["order_send_calls"][0]
    serialized = " ".join(str(v) for v in sent.values()).lower()
    assert "hedge" not in serialized


@pytest.mark.asyncio
async def test_open_last_error_captured_on_none_return(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Step 4.8b core fix verification: when ``order_send`` returns
    ``None``, the handler captures ``mt5.last_error()`` and surfaces
    the tuple in both a WARNING log row AND the published response's
    ``error_msg`` field. Pre-4.8b operators saw only the opaque
    ``order_send_returned_none`` slug."""
    sym = _stub_symbol(filling_mode=mt5_stub.SYMBOL_FILLING_FOK)  # single mode -> no retry
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[None],
        last_error=(-2, 'Invalid "comment" argument'),
    )

    with caplog.at_level(logging.WARNING, logger="exness_client.action_handlers"):
        await handler.dispatch(
            {
                "request_id": "r_last_err",
                "action": "open",
                "symbol": "EURUSDm",
                "side": "buy",
                "volume": "0.10",
            }
        )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "error"
    assert payload["reason"] == "order_send_returned_none"
    # The captured last_error tuple appears verbatim in error_msg.
    assert "Invalid" in payload["error_msg"]
    assert "-2" in payload["error_msg"]
    # And the WARNING log carried the same.
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Invalid" in msg for msg in warn_msgs)


@pytest.mark.asyncio
async def test_open_last_error_captured_on_silent_none_retry_advance(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the first attempt returns None but the second succeeds,
    the WARNING log fires once (for attempt 0) and the published
    response is ``status=filled`` (no error)."""
    sym = _stub_symbol(filling_mode=3)
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[
            None,
            _send_result(retcode=mt5_stub.TRADE_RETCODE_DONE),
        ],
        last_error=(-2, 'Invalid "comment" argument'),
    )

    with caplog.at_level(logging.WARNING, logger="exness_client.action_handlers"):
        await handler.dispatch(
            {
                "request_id": "r_retry",
                "action": "open",
                "symbol": "EURUSDm",
                "side": "buy",
                "volume": "0.10",
            }
        )
    payload = await _read_one_response(fake_redis)
    assert payload["status"] == "filled"
    # Exactly one None-attempt's worth of WARNING for the retry advance.
    none_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "order_send_none" in r.getMessage()
    ]
    assert len(none_warnings) == 1


@pytest.mark.asyncio
async def test_open_last_error_not_queried_when_send_succeeds(
    handler: ActionHandler,
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Performance preservation: on the happy path (no None return) the
    handler does not waste an MT5 round-trip on ``last_error``. The stub
    cannot observe negative space directly, so we use absence of the
    ``open.order_send_none`` WARNING as a sufficient proxy."""

    sym = _stub_symbol()
    mt5_stub.set_state_for_tests(
        symbol_info={"EURUSDm": sym},
        order_send_response=[_send_result(retcode=mt5_stub.TRADE_RETCODE_DONE)],
    )
    with caplog.at_level(
        logging.DEBUG, logger="exness_client.action_handlers"
    ):
        await handler.dispatch(
            {
                "request_id": "r_happy",
                "action": "open",
                "symbol": "EURUSDm",
                "side": "buy",
                "volume": "0.10",
            }
        )
    for rec in caplog.records:
        assert "order_send_none" not in rec.getMessage()
