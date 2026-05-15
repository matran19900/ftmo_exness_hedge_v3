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
