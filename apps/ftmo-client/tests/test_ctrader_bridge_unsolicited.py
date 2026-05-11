"""Bridge ``_on_message`` unsolicited-publish path tests (step 3.5).

Exercises the new dispatch branch in ``CtraderBridge._on_message``:
when an execution event arrives with NO matching pending request, the
bridge translates it via ``event_publisher.build_event_payload`` and
XADDs to ``event_stream:ftmo:{account_id}``. fakeredis stands in for
real Redis so we can read back the published entries with XRANGE.

Tests directly invoke ``_on_message`` and ``_publish_unsolicited_event``
(no Twisted reactor involved). The bridge's existing tests in
``test_ctrader_bridge_actions.py`` cover the solicited path; this file
keeps the unsolicited path coverage colocated so the contract is
discoverable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis.aioredis
import pytest
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAExecutionType

from ftmo_client.ctrader_bridge import CtraderBridge


def _make_bridge(redis: Any | None) -> CtraderBridge:
    return CtraderBridge(
        account_id="ftmo_001",
        access_token="acc",
        ctid_trader_account_id=42,
        client_id="cid",
        client_secret="sec",
        redis=redis,
    )


def _exec_event(
    exec_type: int,
    *,
    position_id: int | None = None,
    order_id: int | None = None,
    deal_price: float = 0.0,
    deal_ts: int = 0,
    deal_commission: int | None = None,
    position_sl: float | None = None,
    position_tp: float | None = None,
    close_gross_profit: int | None = None,
) -> ProtoOAExecutionEvent:
    evt = ProtoOAExecutionEvent()
    evt.executionType = exec_type
    if position_id is not None or position_sl is not None or position_tp is not None:
        if position_id is not None:
            evt.position.positionId = position_id
        if position_sl is not None:
            evt.position.stopLoss = position_sl
        if position_tp is not None:
            evt.position.takeProfit = position_tp
    if order_id is not None:
        evt.order.orderId = order_id
    if deal_price > 0:
        evt.deal.executionPrice = deal_price
        evt.deal.executionTimestamp = deal_ts
        if deal_commission is not None:
            evt.deal.commission = deal_commission
        if position_id is not None:
            evt.deal.positionId = position_id
        if close_gross_profit is not None:
            evt.deal.closePositionDetail.grossProfit = close_gross_profit
    return evt


class _FakeWrapper:
    """Mimics the cTrader library's ProtoMessage envelope: has
    ``clientMsgId`` + ``payloadType`` attributes. ``Protobuf.extract``
    is monkeypatched to unwrap to the inner event we stash here."""

    def __init__(self, inner: Any, client_msg_id: str | None = None) -> None:
        self._inner = inner
        self.clientMsgId = client_msg_id or ""
        self.payloadType = inner.payloadType


@pytest.fixture
def patch_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Protobuf.extract a passthrough for _FakeWrapper instances."""
    from ctrader_open_api import Protobuf  # noqa: PLC0415

    real_extract = Protobuf.extract

    def extract(message: Any) -> Any:
        if isinstance(message, _FakeWrapper):
            return message._inner
        return real_extract(message)

    monkeypatch.setattr("ftmo_client.ctrader_bridge.Protobuf.extract", staticmethod(extract))


# ---------- Solicited path regression (step 3.4b/3.4c) ----------


@pytest.mark.asyncio
async def test_on_message_resolves_pending_future_when_client_msg_id_matches(
    patch_extract: None,
) -> None:
    """Regression for the 3.4b/3.4c path: a matching clientMsgId →
    resolve the pending fill future, do NOT publish to event_stream."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)
    bridge._loop = asyncio.get_running_loop()

    filled = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=5451198,
        deal_price=1.08,
        deal_ts=1,
    )
    wrapper = _FakeWrapper(filled, client_msg_id="req_inflight")

    fill_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    bridge._pending_executions["req_inflight"] = fill_future

    bridge._on_message(None, wrapper)
    resolved = await asyncio.wait_for(fill_future, timeout=1.0)
    assert int(resolved.position.positionId) == 5451198

    # Yield to allow any scheduled tasks (there shouldn't be any) to run.
    await asyncio.sleep(0)
    # event_stream should be untouched on the solicited path.
    entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
    assert entries == []


# ---------- Unsolicited path (step 3.5) ----------


@pytest.mark.asyncio
async def test_on_message_publishes_position_closed_for_unsolicited_close(
    patch_extract: None,
) -> None:
    """User closed on cTrader UI: no clientMsgId, ORDER_FILLED with
    closePositionDetail. Bridge publishes position_closed to
    event_stream:ftmo:{acc}."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)
    bridge._loop = asyncio.get_running_loop()

    closed = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=5451198,
        position_sl=1.07000,
        position_tp=1.09000,
        deal_price=1.08000,  # between SL and TP → manual close
        deal_ts=1735000000456,
        deal_commission=5,
        close_gross_profit=1840,
    )
    wrapper = _FakeWrapper(closed, client_msg_id="")  # no correlation

    bridge._on_message(None, wrapper)

    # Drain the scheduled create_task — yield a couple of ticks.
    for _ in range(5):
        entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
        if entries:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("event_stream entry was not published")

    assert len(entries) == 1
    _entry_id, fields = entries[0]
    assert fields["event_type"] == "position_closed"
    assert fields["broker_order_id"] == "5451198"
    assert fields["close_reason"] == "manual"
    assert fields["realized_pnl"] == "1840"


@pytest.mark.asyncio
async def test_on_message_publishes_pending_filled_for_unsolicited_open_fill(
    patch_extract: None,
) -> None:
    """A pending LIMIT/STOP order finally filled — ORDER_FILLED without
    closePositionDetail. Bridge publishes pending_filled so the
    server's event_handler can migrate broker_order_id from orderId
    to positionId."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)
    bridge._loop = asyncio.get_running_loop()

    fill = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=5451300,
        order_id=8324918,
        deal_price=1.07000,
        deal_ts=1735000005000,
    )
    bridge._on_message(None, _FakeWrapper(fill, client_msg_id=""))

    for _ in range(5):
        entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
        if entries:
            break
        await asyncio.sleep(0)

    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["event_type"] == "pending_filled"
    assert fields["broker_order_id"] == "5451300"
    assert fields["order_id_old"] == "8324918"
    assert fields["fill_price"] == "1.07"


@pytest.mark.asyncio
async def test_on_message_publishes_position_modified_for_unsolicited_replace(
    patch_extract: None,
) -> None:
    """User changed SL/TP via cTrader UI — ORDER_REPLACED, no
    correlation. Bridge publishes position_modified."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)
    bridge._loop = asyncio.get_running_loop()

    replaced = _exec_event(
        ProtoOAExecutionType.ORDER_REPLACED,
        position_id=5451198,
        position_sl=1.07500,
        position_tp=1.09500,
    )
    bridge._on_message(None, _FakeWrapper(replaced, client_msg_id=""))

    for _ in range(5):
        entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
        if entries:
            break
        await asyncio.sleep(0)

    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["event_type"] == "position_modified"
    assert fields["new_sl"] == "1.075"
    assert fields["new_tp"] == "1.095"


@pytest.mark.asyncio
async def test_on_message_skips_publish_for_unhandled_execution_type(
    patch_extract: None,
) -> None:
    """ORDER_ACCEPTED arriving unsolicited (broker echo) → no publish.
    Same for SWAP / DEPOSIT_WITHDRAW etc."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)
    bridge._loop = asyncio.get_running_loop()

    accepted = _exec_event(ProtoOAExecutionType.ORDER_ACCEPTED, order_id=8324918)
    bridge._on_message(None, _FakeWrapper(accepted, client_msg_id=""))

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
    assert entries == []


@pytest.mark.asyncio
async def test_on_message_with_no_redis_is_safe_no_op(patch_extract: None) -> None:
    """A bridge constructed without redis (e.g. older tests, or
    standalone protocol probing) must not crash when an unsolicited
    event arrives. It logs at debug and returns."""
    bridge = _make_bridge(None)
    bridge._loop = asyncio.get_running_loop()

    closed = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        position_sl=1.07,
        deal_price=1.07,
        deal_ts=1,
        close_gross_profit=0,
    )
    # Should not raise.
    bridge._on_message(None, _FakeWrapper(closed, client_msg_id=""))
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_on_message_passes_through_non_execution_events(
    patch_extract: None,
) -> None:
    """A wrapper carrying a non-ProtoOAExecutionEvent payload (e.g.
    spot ticks, depth updates) is ignored without raising, and does
    NOT publish to event_stream."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)
    bridge._loop = asyncio.get_running_loop()

    # Use a non-execution-event proto (ProtoOAErrorRes is the closest
    # benign sibling that has a payloadType attribute). The bridge's
    # ``isinstance(..., ProtoOAExecutionEvent)`` guard must drop it.
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (  # noqa: PLC0415
        ProtoOAErrorRes,
    )

    err = ProtoOAErrorRes()
    err.errorCode = "GENERIC"
    err.description = "unrelated message"
    bridge._on_message(None, _FakeWrapper(err, client_msg_id=""))
    await asyncio.sleep(0)
    entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
    assert entries == []


@pytest.mark.asyncio
async def test_on_message_extract_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protobuf.extract raising must not propagate out of the Twisted
    callback. Bridge logs the exception and returns."""

    def bad_extract(_message: Any) -> Any:
        raise RuntimeError("simulated extract failure")

    monkeypatch.setattr("ftmo_client.ctrader_bridge.Protobuf.extract", staticmethod(bad_extract))

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)
    bridge._loop = asyncio.get_running_loop()

    # Should not raise.
    bridge._on_message(None, object())
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unsolicited_close_after_pending_solicit_does_not_corrupt_future(
    patch_extract: None,
) -> None:
    """If a clientMsgId matches a pending request but the executionType
    is NOT ORDER_FILLED (e.g. a late REPLACED that landed on the same
    clientMsgId due to retries), the bridge falls through to the
    unsolicited publish path — it must NOT resolve the pending future
    with non-FILLED data."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = _make_bridge(redis)
    bridge._loop = asyncio.get_running_loop()

    pending: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    bridge._pending_executions["req_x"] = pending

    # REPLACED with the SAME clientMsgId as the pending request.
    replaced = _exec_event(
        ProtoOAExecutionType.ORDER_REPLACED,
        position_id=1,
        position_sl=1.07,
        position_tp=1.09,
    )
    bridge._on_message(None, _FakeWrapper(replaced, client_msg_id="req_x"))

    # Yield to let the publish task run.
    for _ in range(5):
        entries = await redis.xrange("event_stream:ftmo:ftmo_001", "-", "+")
        if entries:
            break
        await asyncio.sleep(0)

    # Pending future is NOT resolved (it's waiting for ORDER_FILLED, not REPLACED).
    assert not pending.done()
    # The REPLACED was published as a position_modified event_stream entry.
    assert len(entries) == 1
    assert entries[0][1]["event_type"] == "position_modified"


@pytest.mark.asyncio
async def test_publish_unsolicited_event_handles_redis_error(
    patch_extract: None,
) -> None:
    """A RedisError during XADD must not propagate. Bridge logs and
    returns so a flapping Redis doesn't crash the reactor."""
    from redis.exceptions import RedisError  # noqa: PLC0415

    class _FailingRedis:
        async def xadd(self, *_args: Any, **_kwargs: Any) -> str:
            raise RedisError("simulated outage")

    bridge = _make_bridge(_FailingRedis())
    closed = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        position_sl=1.07,
        deal_price=1.07,
        deal_ts=1,
        close_gross_profit=0,
    )
    # Direct call to the coroutine — exercise the error branch.
    await bridge._publish_unsolicited_event(closed)
    # No exception escaped → pass.


@pytest.mark.asyncio
async def test_event_stream_key_includes_account_id() -> None:
    """The XADD stream key uses ``event_stream:ftmo:{account_id}`` —
    pinning the contract so the server's event_handler subscription
    can be defined symmetrically."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bridge = CtraderBridge(
        account_id="acc_xyz",
        access_token="t",
        ctid_trader_account_id=42,
        client_id="cid",
        client_secret="sec",
        redis=redis,
    )
    closed = _exec_event(
        ProtoOAExecutionType.ORDER_FILLED,
        position_id=1,
        position_sl=1.07,
        deal_price=1.07,
        deal_ts=1,
        close_gross_profit=0,
    )
    await bridge._publish_unsolicited_event(closed)
    entries = await redis.xrange("event_stream:ftmo:acc_xyz", "-", "+")
    assert len(entries) == 1
