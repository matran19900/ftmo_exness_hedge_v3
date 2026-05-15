"""Step 4.7b — Phase 4 alert publish-only service.

Two alert types are registered here; both surface as WARNING-severity
operator notifications when something on the Exness hedge leg happened
that the server did NOT initiate. Both are pure WARNING — no automatic
cascade, no auto-revert, no Telegram delivery yet (step 4.11).

Publish contract:
  1. Per-type cooldown via ``SET alert_cooldown:{type}:{key} NX EX`` —
     prevents duplicate alerts for the same (order_id, change_kind) tuple
     within ``ALERT_TYPES[type]['cooldown_seconds']``.
  2. ``alert:{alert_id}`` HASH written with TTL (audit trail).
  3. WS broadcast on the ``alerts`` channel for live frontend toasts.

Step 4.11 will extend this module with:
  - Telegram delivery loop (consume the WS channel or poll alert: keys).
  - Per-type Telegram cooldown separate from the publish cooldown.
  - Settings UI toggle per alert type.
  - Plain-text Telegram mode.
  - 6 more alert types from the design registry.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from app.services.broadcast import BroadcastService
    from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)


class AlertTypeSpec(TypedDict):
    severity: str
    emoji: str
    cooldown_seconds: int
    ttl_seconds: int


# Phase 4.7b registry — 2 types. Step 4.11 adds the rest.
ALERT_TYPES: dict[str, AlertTypeSpec] = {
    "hedge_leg_external_close_warning": {
        "severity": "WARN",
        "emoji": "⚠️",
        "cooldown_seconds": 300,
        "ttl_seconds": 7 * 86400,  # 7-day audit trail
    },
    "hedge_leg_external_modify_warning": {
        "severity": "WARN",
        "emoji": "⚠️",
        "cooldown_seconds": 300,
        "ttl_seconds": 7 * 86400,
    },
}

ALERTS_CHANNEL = "alerts"


@dataclass
class AlertPayload:
    """The shape written to Redis + published on the WS channel.

    ``context`` is the structured machine-readable payload (operator can
    still scan the human ``body_vi`` for a quick read). It is JSON-encoded
    in the Redis HASH (single string field) and kept as a dict on the WS
    envelope so the frontend can render without a parse step.
    """

    alert_id: str
    alert_type: str
    severity: str
    emoji: str
    title_vi: str
    body_vi: str
    context: dict[str, str] = field(default_factory=dict)
    created_at_ms: int = 0

    def to_redis_hash(self) -> dict[str, str]:
        return {
            "alert_id": self.alert_id,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "emoji": self.emoji,
            "title_vi": self.title_vi,
            "body_vi": self.body_vi,
            "context": json.dumps(self.context),
            "created_at_ms": str(self.created_at_ms),
        }

    def to_ws_message(self) -> dict[str, object]:
        return {
            "type": "alert",
            "alert_id": self.alert_id,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "emoji": self.emoji,
            "title_vi": self.title_vi,
            "body_vi": self.body_vi,
            "context": self.context,
            "created_at_ms": self.created_at_ms,
        }


class AlertService:
    """Publish-only alert orchestrator (step 4.7b).

    Built once in the FastAPI lifespan and injected into the per-Exness
    event_handler tasks. No background tasks; ``emit`` is synchronous
    from the caller's perspective (it returns once Redis + WS publish
    have completed or been cooldown-suppressed).
    """

    def __init__(
        self,
        redis_svc: RedisService,
        broadcast: BroadcastService,
    ) -> None:
        self.redis = redis_svc
        self.broadcast = broadcast

    async def emit(
        self,
        *,
        alert_type: str,
        cooldown_key: str,
        title_vi: str,
        body_vi: str,
        context: dict[str, str],
    ) -> str | None:
        """Try to emit a new alert. Returns the freshly-generated
        ``alert_id`` on success, ``None`` when the cooldown for
        ``(alert_type, cooldown_key)`` is still active OR when the
        ``alert_type`` is unknown.

        Atomicity: cooldown claim uses ``SET NX EX`` so concurrent emits
        of the same (type, key) cannot both succeed.
        """
        spec = ALERT_TYPES.get(alert_type)
        if spec is None:
            logger.error("alert.unknown_type alert_type=%s", alert_type)
            return None

        cooldown_redis_key = f"alert_cooldown:{alert_type}:{cooldown_key}"
        try:
            claimed = await self.redis._redis.set(
                cooldown_redis_key,
                "1",
                nx=True,
                ex=spec["cooldown_seconds"],
            )
        except Exception:
            logger.exception(
                "alert.cooldown_check_failed alert_type=%s cooldown_key=%s",
                alert_type, cooldown_key,
            )
            return None

        if not claimed:
            logger.info(
                "alert.cooldown_suppressed alert_type=%s cooldown_key=%s",
                alert_type, cooldown_key,
            )
            return None

        alert_id = uuid.uuid4().hex
        payload = AlertPayload(
            alert_id=alert_id,
            alert_type=alert_type,
            severity=spec["severity"],
            emoji=spec["emoji"],
            title_vi=title_vi,
            body_vi=body_vi,
            context=context,
            created_at_ms=int(time.time() * 1000),
        )

        # Redis HASH + TTL via pipeline so HSET + EXPIRE are atomic
        # against a concurrent reader.
        try:
            pipe = self.redis._redis.pipeline()
            pipe.hset(
                f"alert:{alert_id}", mapping=payload.to_redis_hash()
            )
            pipe.expire(f"alert:{alert_id}", spec["ttl_seconds"])
            await pipe.execute()
        except Exception:
            logger.exception(
                "alert.redis_publish_failed alert_type=%s alert_id=%s",
                alert_type, alert_id,
            )
            return None

        try:
            await self.broadcast.publish(
                ALERTS_CHANNEL, payload.to_ws_message()
            )
        except Exception:
            # Redis state is already persisted; the WS broadcast is a
            # nice-to-have for live frontend toasts. Log + continue.
            logger.exception(
                "alert.ws_broadcast_failed alert_type=%s alert_id=%s",
                alert_type, alert_id,
            )

        logger.warning(
            "alert.emitted alert_type=%s alert_id=%s severity=%s "
            "cooldown_key=%s context=%s",
            alert_type, alert_id, spec["severity"], cooldown_key, context,
        )
        return alert_id
