"""Position monitor poll loop for the Exness MT5 client (Phase 4.3).

Polls ``mt5.positions_get()`` every ``POLL_INTERVAL_S`` seconds, diffs
against the previous snapshot, and publishes one of three event types
to ``event_stream:exness:{account_id}``:

  - ``position_new``               — a ticket appeared since the last poll.
                                     Either we opened it via ``cmd_stream``
                                     or the operator clicked Buy/Sell on
                                     the MT5 terminal.
  - ``position_closed_external``   — a ticket disappeared. Either an SL/TP
                                     hit, the operator manually closed,
                                     or the broker stopped us out. The
                                     server-side cascade orchestrator
                                     (step 4.7/4.8) reads this event to
                                     trigger Path B cascade per
                                     ``docs/phase-4-design.md`` §1.A.B.
  - ``position_modified``          — a ticket is still present but at
                                     least one of ``sl`` / ``tp`` /
                                     ``volume`` changed (terminal-side
                                     edit or partial close).

The first poll after construction is treated as a *baseline*: it stores
the snapshot but emits no events. Without this guard a freshly-started
client would re-emit a ``position_new`` for every existing position on
the broker, which the cascade orchestrator would mis-interpret as a
brand-new manual open.

Synchronous MT5 calls run via ``asyncio.to_thread`` (D-4.1.A). All
exceptions are logged and swallowed so a flaky broker / Redis blip
never takes down the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


POLL_INTERVAL_S = 2.0


@dataclass(frozen=True)
class PositionSnapshot:
    """The fields the monitor compares between polls.

    Frozen + value-equality so ``last == current`` short-circuits the
    no-change path without touching individual fields. The 6 fields
    cover everything the diff classifier needs (the wider ``Position``
    NamedTuple has more — we just don't care about ``magic`` /
    ``price_open`` / etc. for change detection).
    """

    ticket: int
    symbol: str
    volume: float
    sl: float
    tp: float
    position_type: int  # POSITION_TYPE_BUY (0) / POSITION_TYPE_SELL (1)


class PositionMonitor:
    """Periodic position-poll + diff + event-publish loop.

    Public API:
      - ``run()``    — main loop; cancels via ``stop()``.
      - ``stop()``   — flips the internal asyncio.Event; the loop exits
                       on its next wake (worst case POLL_INTERVAL_S).
    """

    def __init__(
        self,
        redis_client: Any,
        account_id: str,
        mt5_module: Any,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._redis = redis_client
        self._account_id = account_id
        self._mt5 = mt5_module
        self._poll_interval_s = poll_interval_s
        self._event_key = f"event_stream:exness:{account_id}"
        # ticket → snapshot from the previous poll
        self._last_snapshot: dict[int, PositionSnapshot] = {}
        self._baseline_done = False
        self._stop_event = asyncio.Event()

    # ----- lifecycle -----

    async def run(self) -> None:
        """Drain polls until ``stop()`` is called."""
        logger.info(
            "position_monitor.starting account_id=%s interval_s=%s",
            self._account_id,
            self._poll_interval_s,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("position_monitor.poll_exception")
                # Wait next interval OR stop signal — whichever fires first.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._poll_interval_s,
                    )
                except TimeoutError:
                    continue
                else:
                    break
        finally:
            logger.info(
                "position_monitor.stopped account_id=%s", self._account_id
            )

    async def stop(self) -> None:
        """Flip the stop flag. Awaitable for ``ShutdownCoordinator``
        symmetry with ``HeartbeatLoop.stop`` and
        ``CommandProcessor.stop``."""
        self._stop_event.set()

    # ----- internal -----

    async def _poll_once(self) -> None:
        """Single poll: fetch positions, diff against the last snapshot,
        publish events, then store the new snapshot."""
        try:
            positions = await asyncio.to_thread(self._mt5.positions_get)
        except Exception:
            logger.exception(
                "position_monitor.positions_get_exception account_id=%s",
                self._account_id,
            )
            return

        # ``mt5.positions_get`` returns ``None`` on connection loss; we
        # treat None as an empty list so an in-flight disconnect surfaces
        # as ``position_closed_external`` events for everything currently
        # tracked. The reconciler in step 4.4 owns the smarter "did the
        # broker actually drop everything?" question.
        if positions is None:
            positions = ()

        current: dict[int, PositionSnapshot] = {}
        # Keep the raw Position around so the ``position_new`` payload
        # can carry ``entry_price`` (price_open) without re-fetching.
        raw_by_ticket: dict[int, Any] = {}
        for pos in positions:
            current[pos.ticket] = PositionSnapshot(
                ticket=pos.ticket,
                symbol=pos.symbol,
                volume=pos.volume,
                sl=pos.sl,
                tp=pos.tp,
                position_type=pos.type,
            )
            raw_by_ticket[pos.ticket] = pos

        if not self._baseline_done:
            self._last_snapshot = current
            self._baseline_done = True
            logger.info(
                "position_monitor.baseline_set position_count=%d",
                len(current),
            )
            return

        await self._emit_diff_events(self._last_snapshot, current, raw_by_ticket)
        self._last_snapshot = current

    async def _emit_diff_events(
        self,
        last: dict[int, PositionSnapshot],
        current: dict[int, PositionSnapshot],
        raw_by_ticket: dict[int, Any],
    ) -> None:
        """Compare ``last`` vs ``current`` and publish one event per
        diff. Iteration is sorted by ticket so a multi-event poll has a
        deterministic order — useful for tests, useful for the cascade
        orchestrator which currently processes events one at a time."""
        last_tickets = set(last.keys())
        current_tickets = set(current.keys())

        for ticket in sorted(current_tickets - last_tickets):
            snap = current[ticket]
            raw = raw_by_ticket.get(ticket)
            await self._publish_event(
                event_type="position_new",
                ticket=ticket,
                extra={
                    "symbol": snap.symbol,
                    "side": _side_label(snap.position_type, self._mt5),
                    "volume": str(snap.volume),
                    "sl": str(snap.sl),
                    "tp": str(snap.tp),
                    "entry_price": str(getattr(raw, "price_open", 0.0)),
                },
            )

        for ticket in sorted(last_tickets - current_tickets):
            snap = last[ticket]
            await self._publish_event(
                event_type="position_closed_external",
                ticket=ticket,
                extra={
                    "symbol": snap.symbol,
                    "side": _side_label(snap.position_type, self._mt5),
                    "last_volume": str(snap.volume),
                    "last_sl": str(snap.sl),
                    "last_tp": str(snap.tp),
                },
            )

        for ticket in sorted(last_tickets & current_tickets):
            last_snap = last[ticket]
            current_snap = current[ticket]
            if last_snap == current_snap:
                continue
            changed_fields: list[str] = []
            if last_snap.sl != current_snap.sl:
                changed_fields.append("sl")
            if last_snap.tp != current_snap.tp:
                changed_fields.append("tp")
            if last_snap.volume != current_snap.volume:
                changed_fields.append("volume")
            await self._publish_event(
                event_type="position_modified",
                ticket=ticket,
                extra={
                    "symbol": current_snap.symbol,
                    "changed_fields": ",".join(changed_fields),
                    "new_sl": str(current_snap.sl),
                    "new_tp": str(current_snap.tp),
                    "new_volume": str(current_snap.volume),
                    "old_sl": str(last_snap.sl),
                    "old_tp": str(last_snap.tp),
                    "old_volume": str(last_snap.volume),
                },
            )

    async def _publish_event(
        self,
        *,
        event_type: str,
        ticket: int,
        extra: dict[str, str],
    ) -> None:
        """XADD a flat string-keyed payload to
        ``event_stream:exness:{account_id}``. The schema mirrors the
        action-handler response payload so the server's stream
        consumers can be written generically."""
        payload: dict[str, str] = {
            "event_type": event_type,
            "broker_position_id": str(ticket),
            "ts_ms": str(int(time.time() * 1000)),
        }
        payload.update(extra)
        try:
            await self._redis.xadd(self._event_key, payload)
            logger.info(
                "position_event.published event_type=%s ticket=%s",
                event_type,
                ticket,
            )
        except Exception:
            logger.exception(
                "position_event.xadd_failed event_type=%s ticket=%s",
                event_type,
                ticket,
            )


def _side_label(position_type: int, mt5_module: Any) -> str:
    """Translate the MT5 ``POSITION_TYPE_*`` int into the ``buy`` /
    ``sell`` slug that the server's response handler reads. Defensive
    on unknown ints — surfaces as ``unknown`` rather than crashing."""
    if position_type == mt5_module.POSITION_TYPE_BUY:
        return "buy"
    if position_type == mt5_module.POSITION_TYPE_SELL:
        return "sell"
    return "unknown"
