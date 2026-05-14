"""Position monitor poll loop for the Exness MT5 client (Phase 4.3 + 4.3a).

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
                                     Step 4.3a stamps every event with
                                     ``close_reason`` (``server_initiated``
                                     vs ``external``) via the ``CmdLedger``.
  - ``position_modified``          — a ticket is still present but at
                                     least one of ``sl`` / ``tp`` /
                                     ``volume`` changed (terminal-side
                                     edit or partial close).

Step 4.3a additions:

  * **Persistent snapshot**. After every successful poll the monitor
    SETs ``position_monitor:last_snapshot:{account_id}`` (JSON, 30-day
    TTL). On the next process start the loader reads this snapshot and
    diffs against the live broker view BEFORE marking the baseline —
    so a position closed during a client outage is detected on the
    very first poll after reconnect (closes the leg-open gap CEO
    surfaced in Windows smoke).

  * **history_deals_get enrichment**. Closed events carry the broker's
    actual fill data (close_price, realized_profit, commission, swap)
    when the deals query succeeds; falls back to the last-snapshot
    fields when it doesn't.

The first poll on an account that has *no* persisted snapshot is still
a silent baseline (we'd otherwise replay every existing position as
``position_new`` on a fresh install).

Synchronous MT5 calls run via ``asyncio.to_thread`` (D-4.1.A). All
exceptions are logged and swallowed so a flaky broker / Redis blip
never takes down the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from .cmd_ledger import CmdLedger

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

    # Step 4.3a — persistent snapshot Redis schema.
    SNAPSHOT_KEY_PREFIX = "position_monitor:last_snapshot"
    SNAPSHOT_TTL_SECONDS = 30 * 86400
    SNAPSHOT_SCHEMA_VERSION = 1

    def __init__(
        self,
        redis_client: Any,
        account_id: str,
        mt5_module: Any,
        cmd_ledger: CmdLedger,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._redis = redis_client
        self._account_id = account_id
        self._mt5 = mt5_module
        self._cmd_ledger = cmd_ledger
        self._poll_interval_s = poll_interval_s
        self._event_key = f"event_stream:exness:{account_id}"
        self._snapshot_key = f"{self.SNAPSHOT_KEY_PREFIX}:{account_id}"
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
        publish events, then store the new snapshot.

        Step 4.4 refines the D-4.3-4 "None positions_get → empty"
        contract: BEFORE calling ``positions_get`` we check
        ``terminal_info().connected``. A transient broker disconnect
        previously caused ``positions_get`` to return an empty tuple,
        which made the diff classify every tracked ticket as
        ``position_closed_external`` — false-alarm cascade WARN spam.
        With the gate, a disconnected poll cycle is skipped entirely:
        the in-process snapshot, ``_baseline_done`` flag, and persisted
        Redis snapshot are all preserved so the next reconnect resumes
        the diff against truth.
        """
        # Step 4.4 broker-connection gate.
        try:
            terminal = await asyncio.to_thread(self._mt5.terminal_info)
        except Exception:
            logger.warning(
                "position_monitor.terminal_info_exception account_id=%s",
                self._account_id,
                exc_info=True,
            )
            terminal = None
        if terminal is None or not terminal.connected:
            logger.warning(
                "position_monitor.broker_disconnected_skip_poll "
                "account_id=%s connected=%s",
                self._account_id,
                getattr(terminal, "connected", None),
            )
            return

        try:
            positions = await asyncio.to_thread(self._mt5.positions_get)
        except Exception:
            logger.exception(
                "position_monitor.positions_get_exception account_id=%s",
                self._account_id,
            )
            return

        # ``mt5.positions_get`` returns ``None`` even when ``terminal_info``
        # said connected (rare race the gate doesn't fully close). Still
        # treat None as empty here — same conservative D-4.3-4 behaviour
        # for the residual edge case.
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
            # Step 4.3a: try to load the persisted snapshot first. If it
            # exists, treat it as the previous-poll state and emit any
            # offline-period diff events on this very first poll. Closes
            # the leg-open gap CEO surfaced in the Windows smoke (client
            # restart while a position was still open then closed manually).
            loaded = await self._load_persisted_snapshot()
            if loaded is not None:
                self._last_snapshot = loaded
                self._baseline_done = True
                await self._emit_diff_events(loaded, current, raw_by_ticket)
                self._last_snapshot = current
                await self._persist_snapshot(current)
                return
            # No persisted snapshot → silent baseline (fresh install).
            self._last_snapshot = current
            self._baseline_done = True
            logger.info(
                "position_monitor.baseline_empty_init position_count=%d",
                len(current),
            )
            await self._persist_snapshot(current)
            return

        await self._emit_diff_events(self._last_snapshot, current, raw_by_ticket)
        self._last_snapshot = current
        await self._persist_snapshot(current)

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
            enrichment = await self._enrich_closed_position(ticket)
            extra: dict[str, str] = {
                "symbol": snap.symbol,
                "side": _side_label(snap.position_type, self._mt5),
                "last_volume": str(snap.volume),
                "last_sl": str(snap.sl),
                "last_tp": str(snap.tp),
            }
            extra.update(enrichment)
            await self._publish_event(
                event_type="position_closed_external",
                ticket=ticket,
                extra=extra,
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

    # ----- Step 4.3a: persistent snapshot -----

    async def _persist_snapshot(
        self, snapshot: dict[int, PositionSnapshot]
    ) -> None:
        """SET ``position_monitor:last_snapshot:{account_id}`` JSON with
        ``SNAPSHOT_TTL_SECONDS`` TTL. Called after every successful poll
        (post-emit) so the next process start sees the most recent
        broker view. Failure is logged but never raised — a missed save
        only loses the offline-diff coverage for the next restart."""
        payload: dict[str, Any] = {
            "schema_version": self.SNAPSHOT_SCHEMA_VERSION,
            "last_poll_ts_ms": int(time.time() * 1000),
            "positions": [
                {
                    "ticket": s.ticket,
                    "symbol": s.symbol,
                    "volume": s.volume,
                    "sl": s.sl,
                    "tp": s.tp,
                    "position_type": s.position_type,
                }
                for s in snapshot.values()
            ],
        }
        try:
            await self._redis.set(
                self._snapshot_key,
                json.dumps(payload),
                ex=self.SNAPSHOT_TTL_SECONDS,
            )
            logger.debug(
                "position_monitor.snapshot_persisted key=%s position_count=%d",
                self._snapshot_key,
                len(snapshot),
            )
        except Exception:
            logger.exception(
                "position_monitor.snapshot_persist_failed key=%s",
                self._snapshot_key,
            )

    async def _load_persisted_snapshot(
        self,
    ) -> dict[int, PositionSnapshot] | None:
        """Return the last persisted snapshot keyed by ticket, or ``None``
        when the key is missing / TTL-expired / malformed / schema-mismatched.

        Logs every failure mode so an operator can grep for the cause if
        the offline-diff path ever silently no-ops."""
        try:
            raw = await self._redis.get(self._snapshot_key)
        except Exception:
            logger.exception(
                "position_monitor.snapshot_load_failed key=%s",
                self._snapshot_key,
            )
            return None
        if raw is None:
            logger.info(
                "position_monitor.no_persisted_snapshot key=%s",
                self._snapshot_key,
            )
            return None
        try:
            text = raw if isinstance(raw, str) else raw.decode()
            payload = json.loads(text)
        except (json.JSONDecodeError, AttributeError):
            logger.warning(
                "position_monitor.snapshot_malformed_json key=%s",
                self._snapshot_key,
                exc_info=True,
            )
            return None
        if payload.get("schema_version") != self.SNAPSHOT_SCHEMA_VERSION:
            logger.warning(
                "position_monitor.snapshot_schema_mismatch expected=%s got=%s",
                self.SNAPSHOT_SCHEMA_VERSION,
                payload.get("schema_version"),
            )
            return None
        snapshot: dict[int, PositionSnapshot] = {}
        for entry in payload.get("positions", []):
            try:
                ticket = int(entry["ticket"])
                snapshot[ticket] = PositionSnapshot(
                    ticket=ticket,
                    symbol=entry["symbol"],
                    volume=float(entry["volume"]),
                    sl=float(entry["sl"]),
                    tp=float(entry["tp"]),
                    position_type=int(entry["position_type"]),
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "position_monitor.snapshot_entry_invalid entry=%r",
                    entry,
                    exc_info=True,
                )
                continue
        age_ms = int(time.time() * 1000) - int(payload.get("last_poll_ts_ms", 0))
        logger.info(
            "position_monitor.snapshot_loaded key=%s position_count=%d age_ms=%d",
            self._snapshot_key,
            len(snapshot),
            age_ms,
        )
        return snapshot

    # ----- Step 4.3a: closed-event enrichment -----

    async def _enrich_closed_position(self, ticket: int) -> dict[str, str]:
        """Build the close-side extras for a ``position_closed_external``
        payload. Two pieces:

          1. ``close_reason`` from the ``CmdLedger`` — ``server_initiated``
             when we issued the close, ``external`` for everything else.
             Server step 4.7 routes ``external`` → WARNING alert.
          2. Broker fill data (``close_price`` / ``close_time_ms`` /
             ``realized_profit`` / ``commission`` / ``swap``) via
             ``mt5.history_deals_get``. Falls back to
             ``enrichment_source="snapshot_fallback"`` when the deals
             query fails or has no DEAL_ENTRY_OUT row.
        """
        is_server = await self._cmd_ledger.is_server_initiated(ticket)
        if is_server:
            close_reason = "server_initiated"
            await self._cmd_ledger.clear(ticket)
        else:
            close_reason = "external"

        try:
            deals = await asyncio.to_thread(
                self._mt5.history_deals_get, position=ticket
            )
        except Exception:
            logger.warning(
                "position_monitor.history_deals_get_exception ticket=%s",
                ticket,
                exc_info=True,
            )
            return {
                "close_reason": close_reason,
                "enrichment_source": "snapshot_fallback",
            }
        if not deals:
            logger.warning(
                "position_monitor.history_deals_empty ticket=%s", ticket
            )
            return {
                "close_reason": close_reason,
                "enrichment_source": "snapshot_fallback",
            }
        close_deal = next(
            (d for d in deals if d.entry == self._mt5.DEAL_ENTRY_OUT),
            None,
        )
        if close_deal is None:
            logger.warning(
                "position_monitor.no_close_deal_found ticket=%s deal_count=%d",
                ticket,
                len(deals),
            )
            return {
                "close_reason": close_reason,
                "enrichment_source": "snapshot_fallback",
            }
        return {
            "close_price": str(close_deal.price),
            "close_time_ms": str(int(close_deal.time * 1000)),
            "realized_profit": str(close_deal.profit),
            "commission": str(close_deal.commission),
            "swap": str(close_deal.swap),
            "close_reason": close_reason,
            "enrichment_source": "history_deals",
        }

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
