"""Tests for ``ActionHandler`` (Phase 4.2).

The handler talks to MT5 via the stub + Redis via fakeredis. We assert
the resulting ``resp_stream:exness:{account_id}`` payload because that's
the contract the server's response handler (step 4.7) consumes.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from exness_client import mt5_stub
from exness_client.action_handlers import ActionHandler
from exness_client.symbol_sync import SymbolSyncPublisher

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def handler(fake_redis: fakeredis.aioredis.FakeRedis) -> ActionHandler:
    sync = SymbolSyncPublisher(fake_redis, "exness_001", mt5_stub)
    return ActionHandler(fake_redis, "exness_001", mt5_stub, sync)


def _stub_symbol(
    name: str = "EURUSDm",
    *,
    bid: float = 1.0850,
    ask: float = 1.0852,
    trade_mode: int | None = None,
) -> mt5_stub.SymbolInfo:
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
    handler = ActionHandler(fake_redis, "exness_001", mt5_stub, sync)
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
