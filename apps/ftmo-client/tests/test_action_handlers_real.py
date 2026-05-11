"""Action-handler tests for the real (step-3.4) dispatch path.

``handle_open`` / ``handle_close`` / ``handle_modify_sl_tp`` are tested
with a fake bridge that records the call args and returns the result we
want. Redis is fakeredis. After each call we drain
``resp_stream:ftmo:{acc}`` to assert the handler published exactly one
entry with the right fields.
"""

from __future__ import annotations

from typing import Any, cast

import fakeredis.aioredis
import pytest
import pytest_asyncio

from ftmo_client.action_handlers import (
    handle_close,
    handle_modify_sl_tp,
    handle_open,
)
from ftmo_client.ctrader_bridge import (
    ClosePositionResult,
    CtraderBridge,
    ModifySltpResult,
    OrderPlacementResult,
)

ACC = "ftmo_001"
RESP_STREAM = f"resp_stream:ftmo:{ACC}"


def _as_bridge(b: object) -> CtraderBridge:
    """Cast helper so handler call sites can pass ``FakeBridge`` (duck-typed)
    where ``CtraderBridge`` is the declared parameter type. mypy strict
    doesn't see FakeBridge as a subclass; runtime cares only about method
    presence, so we paper over the gap once instead of per-call.
    """
    return cast("CtraderBridge", b)


class FakeBridge:
    """Records every bridge call + returns a pre-set result.

    Replaces ``CtraderBridge`` in handler tests — we don't want to spin
    up Twisted or a fake cTrader server just to exercise the dispatch.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_result: Any = None
        self.next_exception: BaseException | None = None

    async def _invoke(self, method: str, **kwargs: Any) -> Any:
        self.calls.append((method, kwargs))
        if self.next_exception is not None:
            exc = self.next_exception
            self.next_exception = None  # one-shot
            raise exc
        return self.next_result

    async def place_market_order(self, **kwargs: Any) -> Any:
        return await self._invoke("place_market_order", **kwargs)

    async def place_market_order_with_sltp(self, **kwargs: Any) -> Any:
        # Step 3.4a composite: ``handle_open`` market branch routes here
        # now instead of the bare ``place_market_order``.
        return await self._invoke("place_market_order_with_sltp", **kwargs)

    async def place_limit_order(self, **kwargs: Any) -> Any:
        return await self._invoke("place_limit_order", **kwargs)

    async def place_stop_order(self, **kwargs: Any) -> Any:
        return await self._invoke("place_stop_order", **kwargs)

    async def close_position(self, **kwargs: Any) -> Any:
        return await self._invoke("close_position", **kwargs)

    async def modify_sl_tp(self, **kwargs: Any) -> Any:
        return await self._invoke("modify_sl_tp", **kwargs)


@pytest_asyncio.fixture
async def seeded_symbol(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> fakeredis.aioredis.FakeRedis:
    """Plant a symbol_config:EURUSD HASH in fakeredis the handlers can read."""
    await fake_redis.hset(  # type: ignore[misc]
        "symbol_config:EURUSD",
        mapping={
            "ftmo_symbol": "EURUSD",
            "ctrader_symbol_id": "1",
            "lot_size": "10000000",
            "digits": "5",
        },
    )
    return fake_redis


async def _drain_resp(redis: fakeredis.aioredis.FakeRedis) -> list[dict[str, str]]:
    """Read every entry on resp_stream — handlers should publish exactly one."""
    entries = await redis.xrange(RESP_STREAM, "-", "+")
    return [fields for _id, fields in entries]


def _ok_market_result() -> OrderPlacementResult:
    return {
        "success": True,
        "broker_order_id": "987654321",
        "fill_price": "1.08412",
        "fill_time": "1735000000123",
        "commission": "5",
        "error_code": "",
        "error_msg": "",
    }


def _err_market_result() -> OrderPlacementResult:
    return {
        "success": False,
        "broker_order_id": "",
        "fill_price": "",
        "fill_time": "",
        "commission": "",
        "error_code": "market_closed",
        "error_msg": "MARKET_CLOSED",
    }


# ---------- handle_open ----------


@pytest.mark.asyncio
async def test_handle_open_market_dispatches_and_publishes_success(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    bridge.next_result = _ok_market_result()

    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "req_001",
            "order_id": "ord_001",
            "action": "open",
            "order_type": "market",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "sl": "1.08000",
            "tp": "1.09000",
            "entry_price": "0",
        },
    )

    assert len(bridge.calls) == 1
    name, kw = bridge.calls[0]
    # Step 3.4a: market branch routes through the composite, not the bare
    # ``place_market_order`` — cTrader rejects absolute SL/TP on market sends.
    assert name == "place_market_order_with_sltp"
    assert kw["symbol_id"] == 1
    assert kw["lot_size"] == 10_000_000
    assert kw["side"] == "buy"
    assert kw["volume_lots"] == 0.01
    assert kw["sl_price"] == 1.08000
    assert kw["client_msg_id"] == "req_001"

    [resp] = await _drain_resp(seeded_symbol)
    assert resp["action"] == "open"
    assert resp["order_id"] == "ord_001"
    assert resp["request_id"] == "req_001"
    assert resp["status"] == "success"
    assert resp["broker_order_id"] == "987654321"
    assert resp["fill_price"] == "1.08412"
    # Step 3.4a: clean happy path → no amend-failure flag on the resp.
    assert "sl_tp_attach_failed" not in resp


@pytest.mark.asyncio
async def test_handle_open_market_sl_tp_amend_failure_marks_resp(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    """Step 3.4a: composite returned fill-success + amend-failure → the
    resp_stream entry has ``status=success`` (position IS open) but also
    carries ``sl_tp_attach_failed=True`` plus the amend error fields, so
    the server's response_handler can warn the operator."""
    bridge = FakeBridge()
    bridge.next_result = {
        "success": True,
        "broker_order_id": "987654321",
        "fill_price": "1.08412",
        "fill_time": "1735000000123",
        "commission": "5",
        "error_code": "",
        "error_msg": "",
        "sl_tp_attach_failed": True,
        "sl_tp_attach_error_code": "invalid_sl_distance",
        "sl_tp_attach_error_msg": "SL too close to price",
    }

    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "req_amend_fail",
            "order_id": "ord_amend_fail",
            "action": "open",
            "order_type": "market",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "sl": "1.08400",
            "tp": "1.09000",
            "entry_price": "0",
        },
    )

    [resp] = await _drain_resp(seeded_symbol)
    # Fill succeeded → status=success even though SL/TP didn't attach.
    assert resp["status"] == "success"
    assert resp["broker_order_id"] == "987654321"
    # The warning bits propagate so the server-side response_handler can
    # branch on them. Boolean → "True" string after Redis stringification.
    assert resp["sl_tp_attach_failed"] == "True"
    assert resp["sl_tp_attach_error_code"] == "invalid_sl_distance"
    assert "SL too close" in resp["sl_tp_attach_error_msg"]


@pytest.mark.asyncio
async def test_handle_open_market_bridge_error_publishes_error(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    bridge.next_result = _err_market_result()

    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "req_002",
            "order_id": "ord_002",
            "action": "open",
            "order_type": "market",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "sl": "0",
            "tp": "0",
            "entry_price": "0",
        },
    )

    [resp] = await _drain_resp(seeded_symbol)
    assert resp["status"] == "error"
    assert resp["error_code"] == "market_closed"


@pytest.mark.asyncio
async def test_handle_open_limit_passes_entry_price(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    bridge.next_result = {
        "success": True,
        "broker_order_id": "555",
        "fill_price": "",
        "fill_time": "",
        "commission": "",
        "error_code": "",
        "error_msg": "",
    }

    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "open",
            "order_type": "limit",
            "symbol": "EURUSD",
            "side": "sell",
            "volume_lots": "0.1",
            "sl": "1.09",
            "tp": "1.07",
            "entry_price": "1.08000",
        },
    )

    name, kw = bridge.calls[0]
    assert name == "place_limit_order"
    assert kw["entry_price"] == 1.08000
    assert kw["side"] == "sell"


@pytest.mark.asyncio
async def test_handle_open_stop_passes_entry_price(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    bridge.next_result = {
        "success": True,
        "broker_order_id": "777",
        "fill_price": "",
        "fill_time": "",
        "commission": "",
        "error_code": "",
        "error_msg": "",
    }

    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "open",
            "order_type": "stop",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "entry_price": "1.10000",
        },
    )
    name, kw = bridge.calls[0]
    assert name == "place_stop_order"
    assert kw["entry_price"] == 1.10000


@pytest.mark.asyncio
async def test_handle_open_limit_without_entry_publishes_invalid_request(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "open",
            "order_type": "limit",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
            "entry_price": "0",
        },
    )
    assert bridge.calls == []  # bridge never called
    [resp] = await _drain_resp(seeded_symbol)
    assert resp["status"] == "error"
    assert resp["error_code"] == "invalid_request"
    assert "entry_price required for limit" in resp["error_msg"]


@pytest.mark.asyncio
async def test_handle_open_missing_symbol_config_publishes_symbol_not_synced(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_open(
        fake_redis,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "open",
            "order_type": "market",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
        },
    )
    assert bridge.calls == []
    [resp] = await _drain_resp(fake_redis)
    assert resp["status"] == "error"
    assert resp["error_code"] == "symbol_not_synced"


@pytest.mark.asyncio
async def test_handle_open_unknown_order_type_publishes_invalid_request(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "open",
            "order_type": "wat",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
        },
    )
    assert bridge.calls == []
    [resp] = await _drain_resp(seeded_symbol)
    assert resp["error_code"] == "invalid_request"
    assert "unknown order_type" in resp["error_msg"]


@pytest.mark.asyncio
async def test_handle_open_invalid_side_publishes_invalid_request(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "open",
            "order_type": "market",
            "symbol": "EURUSD",
            "side": "WRONG",
            "volume_lots": "0.01",
        },
    )
    [resp] = await _drain_resp(seeded_symbol)
    assert resp["error_code"] == "invalid_request"
    assert "side must be" in resp["error_msg"]


@pytest.mark.asyncio
async def test_handle_open_zero_volume_publishes_invalid_request(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "open",
            "order_type": "market",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0",
        },
    )
    [resp] = await _drain_resp(seeded_symbol)
    assert resp["error_code"] == "invalid_request"
    assert "volume_lots must be > 0" in resp["error_msg"]


@pytest.mark.asyncio
async def test_handle_open_bridge_raises_publishes_broker_error(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    bridge.next_exception = RuntimeError("connection lost")

    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "open",
            "order_type": "market",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
        },
    )
    [resp] = await _drain_resp(seeded_symbol)
    assert resp["error_code"] == "broker_error"
    assert "connection lost" in resp["error_msg"]


@pytest.mark.asyncio
async def test_handle_open_bridge_timeout_publishes_timeout(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    bridge.next_exception = TimeoutError("wait_for elapsed")

    await handle_open(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "open",
            "order_type": "market",
            "symbol": "EURUSD",
            "side": "buy",
            "volume_lots": "0.01",
        },
    )
    [resp] = await _drain_resp(seeded_symbol)
    assert resp["error_code"] == "timeout"


# ---------- handle_close ----------


def _ok_close_result() -> ClosePositionResult:
    return {
        "success": True,
        "close_price": "1.08600",
        "close_time": "1735000000456",
        "realized_pnl": "10",
        "error_code": "",
        "error_msg": "",
    }


@pytest.mark.asyncio
async def test_handle_close_happy_path(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    bridge.next_result = _ok_close_result()

    await handle_close(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "close",
            "symbol": "EURUSD",
            "broker_order_id": "987654321",
            "volume_lots": "0.01",
        },
    )
    name, kw = bridge.calls[0]
    assert name == "close_position"
    assert kw["position_id"] == 987654321
    assert kw["volume_lots"] == 0.01
    assert kw["lot_size"] == 10_000_000

    [resp] = await _drain_resp(seeded_symbol)
    assert resp["status"] == "success"
    assert resp["close_price"] == "1.08600"


@pytest.mark.asyncio
async def test_handle_close_missing_broker_order_id(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_close(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "close",
            "symbol": "EURUSD",
            "volume_lots": "0.01",
        },
    )
    assert bridge.calls == []
    [resp] = await _drain_resp(seeded_symbol)
    assert resp["error_code"] == "invalid_request"
    assert "broker_order_id missing" in resp["error_msg"]


@pytest.mark.asyncio
async def test_handle_close_missing_volume(
    seeded_symbol: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_close(
        seeded_symbol,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "close",
            "symbol": "EURUSD",
            "broker_order_id": "987654321",
        },
    )
    [resp] = await _drain_resp(seeded_symbol)
    assert resp["error_code"] == "invalid_request"


@pytest.mark.asyncio
async def test_handle_close_missing_symbol_publishes_invalid_request(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_close(
        fake_redis,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "close",
            "broker_order_id": "987654321",
            "volume_lots": "0.01",
        },
    )
    [resp] = await _drain_resp(fake_redis)
    assert resp["error_code"] == "invalid_request"


# ---------- handle_modify_sl_tp ----------


def _ok_modify_result() -> ModifySltpResult:
    return {
        "success": True,
        "new_sl": "1.07000",
        "new_tp": "1.09000",
        "error_code": "",
        "error_msg": "",
    }


@pytest.mark.asyncio
async def test_handle_modify_sl_tp_happy_path(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    bridge.next_result = _ok_modify_result()
    await handle_modify_sl_tp(
        fake_redis,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "modify_sl_tp",
            "broker_order_id": "987",
            "sl": "1.07000",
            "tp": "1.09000",
        },
    )
    name, kw = bridge.calls[0]
    assert name == "modify_sl_tp"
    assert kw["position_id"] == 987
    assert kw["sl_price"] == 1.07
    assert kw["tp_price"] == 1.09

    [resp] = await _drain_resp(fake_redis)
    assert resp["status"] == "success"
    assert resp["new_sl"] == "1.07000"


@pytest.mark.asyncio
async def test_handle_modify_sl_tp_missing_broker_order_id(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_modify_sl_tp(
        fake_redis,
        _as_bridge(bridge),
        ACC,
        {"request_id": "r", "order_id": "o", "action": "modify_sl_tp", "sl": "1.07", "tp": "1.09"},
    )
    assert bridge.calls == []
    [resp] = await _drain_resp(fake_redis)
    assert resp["error_code"] == "invalid_request"
    assert "broker_order_id missing" in resp["error_msg"]


@pytest.mark.asyncio
async def test_handle_modify_sl_tp_non_int_broker_id(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    await handle_modify_sl_tp(
        fake_redis,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "modify_sl_tp",
            "broker_order_id": "not_an_int",
            "sl": "1.07",
        },
    )
    [resp] = await _drain_resp(fake_redis)
    assert resp["error_code"] == "invalid_request"


@pytest.mark.asyncio
async def test_handle_modify_sl_tp_bridge_error_publishes_broker_error(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    bridge = FakeBridge()
    bridge.next_exception = RuntimeError("disconnected")
    await handle_modify_sl_tp(
        fake_redis,
        _as_bridge(bridge),
        ACC,
        {
            "request_id": "r",
            "order_id": "o",
            "action": "modify_sl_tp",
            "broker_order_id": "987",
            "sl": "1.07",
        },
    )
    [resp] = await _drain_resp(fake_redis)
    assert resp["error_code"] == "broker_error"
