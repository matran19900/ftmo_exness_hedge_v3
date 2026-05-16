"""Phase 4 cascade open + secondary retry orchestrator (step 4.7a).

Spec: docs/phase-4-design.md §1.E.

Lifecycle for a hedge order's secondary (Exness) leg:

  pending (created by OrderService)
    -> primary_filled                   (response_handler:ftmo on FTMO fill)
       -> s_status = pending_open       (HedgeService starts attempt 1)
          -> push cmd_stream:exness:{acc} action=open
          -> poll ``s_status`` for outcome:
               filled   -> status=filled, hedge_open broadcast
               rejected -> next attempt
               timeout  -> next attempt
  After RETRY_DELAYS_SECONDS (0.5s, 1s, 2s) are exhausted without a
  fill -> status=secondary_failed + s_status=secondary_failed +
  secondary_failed broadcast. The primary FTMO leg is left open (orphan
  leg, operator action required).

Phase 4 single-server assumption: ``s_status`` is observed via 100ms
polling. Phase 5 may swap this for Redis pubsub on per-order channels.

Out of scope: cascade close (step 4.8), cascade_lock (step 4.8),
cascade_cancel_pending (step 4.8 race handling).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.broadcast import BroadcastService
    from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)

# Retry budget per docs/phase-4-design.md §1.E: 3 retries after the
# initial attempt, with exponential backoff. Total attempts = 4.
RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)

# How long to wait for the Exness resp_stream entry to land before
# treating the attempt as a timeout. Real fills are sub-second; 15s
# absorbs broker latency + heartbeat slips without spuriously firing
# the next attempt while the prior one is still in flight.
SECONDARY_OUTCOME_TIMEOUT: float = 15.0

# Poll cadence on the order's ``s_status``. Phase 4 single-server, no
# pubsub yet (Phase 5).
SECONDARY_OUTCOME_POLL_INTERVAL: float = 0.1

ORDERS_CHANNEL = "orders"


class HedgeService:
    """Cascade orchestrator built once in the FastAPI lifespan.

    Holds references to ``RedisService`` + ``BroadcastService``; cheap
    to instantiate; safe to share across all incoming request tasks.
    """

    def __init__(
        self,
        redis_svc: RedisService,
        broadcast: BroadcastService,
    ) -> None:
        self.redis = redis_svc
        self.broadcast = broadcast

    async def cascade_secondary_open(
        self,
        order_id: str,
        order: dict[str, str],
    ) -> None:
        """Run the cascade open + 3-retry sequence for one hedge order.

        Caller (response_handler:ftmo on primary fill) invokes via
        ``asyncio.create_task`` so the reader loop is not blocked while
        we poll for the secondary outcome.

        On unrecoverable error the order is transitioned to
        ``status=secondary_failed`` and a ``secondary_failed`` WS message
        is broadcast. The primary FTMO leg is intentionally left open —
        cascade close to recover the orphan is operator-driven in Phase 4
        (step 4.8 will automate it).
        """
        exness_account_id = order.get("exness_account_id", "")
        s_volume_lots = order.get("s_volume_lots", "")
        s_exness_symbol = order.get("s_exness_symbol", "")
        primary_side = order.get("side", "")
        if not exness_account_id or not s_volume_lots or not s_exness_symbol:
            logger.error(
                "cascade_secondary_open.missing_fields order_id=%s "
                "exness_account_id=%s s_volume_lots=%s s_exness_symbol=%s",
                order_id, exness_account_id, s_volume_lots, s_exness_symbol,
            )
            await self._finalize_failure(
                order_id, error="missing_cascade_fields"
            )
            return
        secondary_side = "sell" if primary_side == "buy" else "buy"

        # Move s_status into the transient pending_open and broadcast so
        # the frontend can render "Hedging…" while we cascade.
        await self.redis.update_order(
            order_id,
            patch={
                "s_status": "pending_open",
                "updated_at": str(int(time.time() * 1000)),
            },
            old_status="primary_filled",
        )
        await self.broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "secondary_pending",
                "order_id": order_id,
            },
        )

        # Attempt sequence: initial push (delay=0), then 3 retries with
        # the exponential delays above.
        delays: tuple[float, ...] = (0.0, *RETRY_DELAYS_SECONDS)
        last_error: str | None = None
        for attempt_idx, delay in enumerate(delays):
            if delay > 0:
                await asyncio.sleep(delay)
                # Reset s_status from any prior 'rejected' so the poller
                # has a clean signal for this attempt's outcome.
                await self.redis.update_order(
                    order_id,
                    patch={
                        "s_status": "pending_open",
                        "updated_at": str(int(time.time() * 1000)),
                    },
                )

            cmd_fields = {
                "order_id": order_id,
                "action": "open",
                "symbol": s_exness_symbol,
                "side": secondary_side,
                # Exness ActionHandler reads ``volume`` (not volume_lots);
                # see apps/exness-client/exness_client/action_handlers.py
                # line ~112. Field name matters — mismatch -> rejected
                # by the client with bad_request_KeyError.
                "volume": s_volume_lots,
                "sl": "0",
                "tp": "0",
                "order_type": "market",
                "entry_price": "0",
                "cascade_trigger": "false",
                "attempt": str(attempt_idx + 1),
            }
            request_id = await self.redis.push_command(
                "exness", exness_account_id, cmd_fields
            )
            await self.redis.link_request_to_order(request_id, order_id)
            logger.info(
                "cascade_secondary_open.pushed order_id=%s attempt=%d "
                "request_id=%s exness_account=%s symbol=%s side=%s volume=%s",
                order_id, attempt_idx + 1, request_id, exness_account_id,
                s_exness_symbol, secondary_side, s_volume_lots,
            )

            outcome = await self._wait_for_secondary_outcome(order_id)
            if outcome == "filled":
                # response_handler:exness already wrote s_status=filled +
                # status=filled. Just broadcast the composed-status change.
                await self.broadcast.publish(
                    ORDERS_CHANNEL,
                    {
                        "type": "hedge_open",
                        "order_id": order_id,
                    },
                )
                return
            if outcome == "rejected":
                last_error = await self._read_last_error(order_id)
                logger.warning(
                    "cascade_secondary_open.attempt_failed order_id=%s "
                    "attempt=%d error=%s",
                    order_id, attempt_idx + 1, last_error,
                )
                continue

            last_error = "timeout_waiting_response"
            logger.warning(
                "cascade_secondary_open.timeout order_id=%s attempt=%d",
                order_id, attempt_idx + 1,
            )

        await self._finalize_failure(
            order_id, error=last_error or "exhausted_retries"
        )

    async def _wait_for_secondary_outcome(self, order_id: str) -> str:
        """Poll the order row for an ``s_status`` outcome.

        Returns:
          - ``"filled"``    — secondary leg succeeded (s_status==filled).
          - ``"rejected"``  — broker rejected (s_status in rejected/open_failed).
          - ``"timeout"``   — wall clock exceeded SECONDARY_OUTCOME_TIMEOUT
                              before either of the above landed.
        """
        deadline = time.monotonic() + SECONDARY_OUTCOME_TIMEOUT
        while time.monotonic() < deadline:
            order = await self.redis.get_order(order_id)
            s_status = (order or {}).get("s_status", "")
            if s_status == "filled":
                return "filled"
            if s_status in ("rejected", "open_failed"):
                return "rejected"
            await asyncio.sleep(SECONDARY_OUTCOME_POLL_INTERVAL)
        return "timeout"

    async def _read_last_error(self, order_id: str) -> str:
        order = await self.redis.get_order(order_id)
        return (order or {}).get("s_error_msg", "") or ""

    async def _finalize_failure(self, order_id: str, *, error: str) -> None:
        """Terminal: stamp secondary_failed everywhere + broadcast."""
        await self.redis.update_order(
            order_id,
            patch={
                "s_status": "secondary_failed",
                "status": "secondary_failed",
                "s_error_msg": error,
                "updated_at": str(int(time.time() * 1000)),
            },
        )
        await self.broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "secondary_failed",
                "order_id": order_id,
                "error": error,
            },
        )
        logger.error(
            "cascade_secondary_open.exhausted order_id=%s last_error=%s",
            order_id, error,
        )

    # ----- step 4.8: cascade close orchestrator -----

    async def cascade_close_other_leg(
        self,
        order_id: str,
        *,
        closed_leg: str,
        close_reason: str,
        trigger_path: str,
    ) -> None:
        """Cascade close the still-open leg after the other leg closes.

        Step 4.8 — invoked from:
          - ``event_handler:ftmo`` ``position_closed`` on a hedge order
            (trigger ``A`` if the Path A flag is set on the order;
            otherwise ``B`` / ``D`` / ``E`` per close_reason).
          - ``event_handler:exness`` ``position_closed_external`` with
            ``close_reason="server_initiated"`` (trigger ``C`` — completes
            an earlier Path A or Path D cascade we initiated).
          - ``_handle_cascade_cancel_pending`` recursive late-fill path
            (trigger ``cancel_late_fill``).

        Idempotency contract:
          - Lock contention → caller sees ``False`` from
            ``acquire_cascade_lock`` and returns early.
          - Order at terminal status (``closed`` / ``close_failed`` /
            ``rejected`` / ``cancelled``) → no-op, release lock.
          - Other-leg ticket missing → terminal ``close_failed`` with
            structured error.

        Retry budget matches cascade open (4 attempts: initial + 0.5/1/2s
        per R5 design lock).
        """
        acquired = await self.redis.acquire_cascade_lock(order_id, trigger_path)
        if not acquired:
            logger.info(
                "cascade_close.lock_contention order_id=%s trigger=%s",
                order_id, trigger_path,
            )
            return

        try:
            order = await self.redis.get_order(order_id)
            if order is None:
                logger.warning(
                    "cascade_close.order_not_found order_id=%s trigger=%s",
                    order_id, trigger_path,
                )
                return

            composed_status = order.get("status", "")

            # cascade_cancel_pending race: primary closed externally while
            # the secondary cascade-open task is still in flight.
            if (
                composed_status in ("pending", "primary_filled")
                and closed_leg == "p"
            ):
                await self._handle_cascade_cancel_pending(
                    order_id,
                    {k: str(v) for k, v in order.items()},
                    close_reason,
                )
                return

            # Idempotent no-op on terminal / already-cascading. Note:
            # ``cascade_cancel_pending`` is NOT in this list — it is a
            # transient state explicitly handed off via the
            # ``trigger_path="cancel_late_fill"`` recursive path, which
            # must advance to the cmd-push loop.
            if composed_status in (
                "closed", "close_failed", "rejected", "cancelled",
                "secondary_failed", "close_pending",
            ):
                logger.info(
                    "cascade_close.no_op_terminal order_id=%s status=%s "
                    "trigger=%s",
                    order_id, composed_status, trigger_path,
                )
                return

            # Step 4.8e — belt-and-suspenders short-circuit when the
            # secondary leg is already closed but the composed status
            # hasn't flipped yet (out-of-order event delivery during a
            # concurrent operator-click-Close + external-close race).
            # The 4.8e external-close stamp in event_handler writes
            # ``s_status=closed`` + composed ``status=closed`` together
            # via a single ``update_order`` patch, so under normal
            # conditions the composed_status check above already
            # short-circuits this case. This check guards the narrow
            # window between the two writes if a third party (or a
            # future code path) ever splits them.
            if closed_leg == "p" and order.get("s_status") == "closed":
                logger.info(
                    "cascade_close.no_op_secondary_already_closed "
                    "order_id=%s s_status=closed trigger=%s",
                    order_id, trigger_path,
                )
                return

            if closed_leg == "p":
                other_leg = "s"
                other_broker = "exness"
                other_account_id = order.get("exness_account_id", "")
                other_position_ticket = order.get("s_broker_order_id", "")
            elif closed_leg == "s":
                other_leg = "p"
                other_broker = "ftmo"
                other_account_id = order.get("ftmo_account_id", "")
                other_position_ticket = order.get("p_broker_order_id", "")
            else:
                logger.error(
                    "cascade_close.invalid_closed_leg order_id=%s leg=%s",
                    order_id, closed_leg,
                )
                return

            if not other_position_ticket or not other_account_id:
                logger.error(
                    "cascade_close.missing_other_position_ticket "
                    "order_id=%s leg=%s ticket=%r account=%r",
                    order_id, other_leg,
                    other_position_ticket, other_account_id,
                )
                await self._mark_close_failed(
                    order_id, "missing_position_ticket"
                )
                return

            # First, stamp the leg that already closed. composed_status moves
            # to close_pending so a re-trigger sees the cascade in flight.
            now_ms = str(int(time.time() * 1000))
            closed_leg_patch: dict[str, str] = {
                "status": "close_pending",
                f"{closed_leg}_status": "closed",
                f"{closed_leg}_close_reason": close_reason,
                f"{other_leg}_status": "close_pending",
                "updated_at": now_ms,
            }
            await self.redis.update_order(order_id, closed_leg_patch)
            await self.broadcast.publish(
                ORDERS_CHANNEL,
                {
                    "type": "close_initiated",
                    "order_id": order_id,
                    "closed_leg": closed_leg,
                    "trigger_path": trigger_path,
                },
            )

            # Push the cascade-close cmd, retry on rejected/timeout.
            delays: tuple[float, ...] = (0.0, *RETRY_DELAYS_SECONDS)
            last_error: str | None = None
            for attempt_idx, delay in enumerate(delays):
                if delay > 0:
                    await asyncio.sleep(delay)
                    # Reset the other-leg status for the new attempt so the
                    # poller has a clean signal.
                    await self.redis.update_order(
                        order_id,
                        patch={
                            f"{other_leg}_status": "close_pending",
                            "updated_at": str(int(time.time() * 1000)),
                        },
                    )

                cmd_fields = _build_close_cmd(
                    other_broker, order_id, other_position_ticket,
                    attempt_idx + 1,
                )
                request_id = await self.redis.push_command(
                    other_broker, other_account_id, cmd_fields
                )
                await self.redis.link_request_to_order(request_id, order_id)
                logger.info(
                    "cascade_close.pushed order_id=%s leg=%s attempt=%d "
                    "broker=%s ticket=%s request_id=%s trigger=%s",
                    order_id, other_leg, attempt_idx + 1, other_broker,
                    other_position_ticket, request_id, trigger_path,
                )

                outcome = await self._wait_for_close_outcome(
                    order_id, other_leg
                )
                if outcome == "closed":
                    await self.redis.update_order(
                        order_id,
                        patch={
                            "status": "closed",
                            f"{other_leg}_close_reason": "server_initiated",
                            "updated_at": str(int(time.time() * 1000)),
                        },
                    )
                    await self.broadcast.publish(
                        ORDERS_CHANNEL,
                        {
                            "type": "hedge_closed",
                            "order_id": order_id,
                            "outcome": "cascade_completed",
                        },
                    )
                    return
                if outcome == "rejected":
                    last_error = await self._read_close_error(
                        order_id, other_leg
                    )
                    logger.warning(
                        "cascade_close.attempt_failed order_id=%s "
                        "attempt=%d error=%s",
                        order_id, attempt_idx + 1, last_error,
                    )
                    continue

                last_error = "timeout_waiting_close_response"
                logger.warning(
                    "cascade_close.timeout order_id=%s attempt=%d",
                    order_id, attempt_idx + 1,
                )

            await self._mark_close_failed(
                order_id, last_error or "exhausted_retries"
            )
        finally:
            await self.redis.release_cascade_lock(order_id)

    async def complete_cascade_close(
        self,
        order_id: str,
        closed_leg: str,
        close_reason: str,
    ) -> None:
        """Path C completion: the secondary leg's server-initiated close
        landed via ``position_closed_external``. Stamp the order terminal
        ``closed`` + broadcast hedge_closed + release the cascade lock.

        Called from ``event_handler:exness`` when a ``position_closed_external``
        event carries ``close_reason="server_initiated"`` — i.e. an Exness
        ticket we ourselves issued via ``cascade_close_other_leg`` (or
        Path A's primary-then-secondary chain) finally closed at the
        broker.
        """
        order = await self.redis.get_order(order_id)
        if order is None:
            logger.warning(
                "complete_cascade_close.order_not_found order_id=%s",
                order_id,
            )
            return
        composed_status = order.get("status", "")
        if composed_status in ("closed", "close_failed"):
            logger.info(
                "complete_cascade_close.no_op_terminal order_id=%s status=%s",
                order_id, composed_status,
            )
            return

        await self.redis.update_order(
            order_id,
            patch={
                "status": "closed",
                f"{closed_leg}_status": "closed",
                f"{closed_leg}_close_reason": close_reason,
                "updated_at": str(int(time.time() * 1000)),
            },
        )
        await self.broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "hedge_closed",
                "order_id": order_id,
                "outcome": "cascade_completed",
            },
        )
        await self.redis.release_cascade_lock(order_id)

    # ----- step 4.8: cascade_cancel_pending race handling -----

    _LATE_FILL_WAIT_SECONDS: float = 2.0

    async def _handle_cascade_cancel_pending(
        self,
        order_id: str,
        order: dict[str, str],
        close_reason: str,
    ) -> None:
        """Primary closed externally while the secondary cascade-open task
        is still in flight (e.g. operator SL hit mid-retry).

        Strategy:
          1. Transition composed status to ``cascade_cancel_pending`` and
             stamp ``p_status=closed`` so any in-flight
             ``cascade_secondary_open`` observes the change.
          2. Wait briefly (2s) for the secondary leg to either fill (late
             arrival) or terminal-fail.
          3. If filled → recursive ``cascade_close_other_leg`` with
             ``trigger_path="cancel_late_fill"`` to close the orphan.
          4. Otherwise → mark order ``closed`` with ``s_status="never_filled"``
             so the operator sees primary-only outcome.
        """
        await self.redis.update_order(
            order_id,
            patch={
                "status": "cascade_cancel_pending",
                "p_status": "closed",
                "p_close_reason": close_reason,
                "updated_at": str(int(time.time() * 1000)),
            },
        )
        await self.broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "cascade_cancel_pending",
                "order_id": order_id,
                "close_reason": close_reason,
            },
        )

        await asyncio.sleep(self._LATE_FILL_WAIT_SECONDS)

        fresh = await self.redis.get_order(order_id)
        s_status = (fresh or {}).get("s_status", "")

        if s_status == "filled":
            # Late fill — must close the orphan secondary. Release the
            # current lock so the recursive cascade can acquire its own.
            await self.redis.release_cascade_lock(order_id)
            logger.info(
                "cascade_cancel_pending.late_fill_close order_id=%s",
                order_id,
            )
            await self.cascade_close_other_leg(
                order_id,
                closed_leg="p",
                close_reason=close_reason,
                trigger_path="cancel_late_fill",
            )
            return

        # No secondary position exists — primary-only terminal close.
        await self.redis.update_order(
            order_id,
            patch={
                "status": "closed",
                "s_status": "never_filled",
                "updated_at": str(int(time.time() * 1000)),
            },
        )
        await self.broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "hedge_closed",
                "order_id": order_id,
                "outcome": "primary_only_close",
            },
        )

    # ----- step 4.8: helpers -----

    async def _wait_for_close_outcome(
        self, order_id: str, leg: str
    ) -> str:
        """Poll ``{leg}_status`` for the cascade-close outcome.

        Mirrors ``_wait_for_secondary_outcome`` from cascade open but
        observes a different field. Returns ``"closed"`` on success,
        ``"rejected"`` on broker rejection, ``"timeout"`` when the wall
        clock exceeds ``SECONDARY_OUTCOME_TIMEOUT`` (same constant —
        retry timing is symmetric across cascade open + close).
        """
        deadline = time.monotonic() + SECONDARY_OUTCOME_TIMEOUT
        while time.monotonic() < deadline:
            order = await self.redis.get_order(order_id)
            leg_status = (order or {}).get(f"{leg}_status", "")
            if leg_status == "closed":
                return "closed"
            if leg_status in ("rejected", "close_failed"):
                return "rejected"
            await asyncio.sleep(SECONDARY_OUTCOME_POLL_INTERVAL)
        return "timeout"

    async def _read_close_error(self, order_id: str, leg: str) -> str:
        order = await self.redis.get_order(order_id)
        val = (order or {}).get(f"{leg}_close_error_msg", "")
        return str(val) if val else ""

    async def _mark_close_failed(
        self, order_id: str, error: str
    ) -> None:
        await self.redis.update_order(
            order_id,
            patch={
                "status": "close_failed",
                "close_error_msg": error,
                "updated_at": str(int(time.time() * 1000)),
            },
        )
        await self.broadcast.publish(
            ORDERS_CHANNEL,
            {
                "type": "close_failed",
                "order_id": order_id,
                "error": error,
            },
        )
        logger.error(
            "cascade_close.exhausted order_id=%s last_error=%s",
            order_id, error,
        )


def _build_close_cmd(
    broker: str,
    order_id: str,
    position_ticket: str,
    attempt: int,
) -> dict[str, str]:
    """Build the broker-specific cascade-close cmd payload.

    Field naming differs between brokers:
      - Exness ActionHandler reads ``broker_position_id`` for the MT5
        ticket (apps/exness-client/exness_client/action_handlers.py:237).
      - FTMO ActionHandler reads ``broker_order_id`` for the cTrader
        positionId.

    ``cascade_trigger="true"`` is the Phase 4 audit marker for cmd_ledger
    classification on the resulting position_closed_external event
    (step 4.3a contract).
    """
    fields: dict[str, str] = {
        "order_id": order_id,
        "action": "close",
        "order_type": "market",
        "cascade_trigger": "true",
        "attempt": str(attempt),
    }
    if broker == "exness":
        fields["broker_position_id"] = position_ticket
    else:
        fields["broker_order_id"] = position_ticket
    return fields
