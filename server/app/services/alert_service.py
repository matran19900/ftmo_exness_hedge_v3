"""Phase 4 alert dispatcher.

Step 4.7b shipped the publish-only contract (Redis HASH + WS broadcast
on the ``alerts`` channel + per-(alert_type, cooldown_key) SET NX EX
cooldown). Step 4.11 extends it with:

  - Telegram delivery for INFO / WARN / CRITICAL alerts. Sends to a
    dedicated chat (see ``config.telegram_alert_*``) using plain-text
    bodies (no Markdown / HTML parse_mode). Errors are logged WARNING
    and never fail ``emit`` — Redis + WS remain the source of truth.
  - ``bypass_cooldown`` parameter on ``emit`` for recovery-style alerts
    that must always land even within a parent alert's cooldown window
    (design §2.A.3). Default False preserves the 4.7b contract.
  - 3 new alert types wired at existing broadcast sites in
    ``hedge_service`` (cascade success + secondary_failed) and
    ``event_handler`` (Exness stop_out split from the generic external-
    close WARNING). The 2 4.7b types
    (``hedge_leg_external_close_warning`` +
    ``hedge_leg_external_modify_warning``) are preserved verbatim so
    existing call sites and tests stay unchanged.

Step 4.11 deliberately leaves the remaining design types
(``server_error``, ``client_offline`` + recovery, ``broker_disconnect``
+ recovery) for follow-up sub-steps — each needs an upstream wire
(FastAPI exception middleware, heartbeat staleness state, cross-
process client emit) that is out of scope for a minimal dispatch
landing.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

import httpx

if TYPE_CHECKING:
    from app.services.broadcast import BroadcastService
    from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)


class AlertTypeSpec(TypedDict):
    severity: str
    emoji: str
    cooldown_seconds: int
    ttl_seconds: int


# Registry. 4.7b shipped the first two entries; 4.11 adds the next
# three. Each new type uses ``cooldown_seconds=0`` — these are
# one-shot terminal events per order_id, so the rate limit is already
# enforced by the underlying order state machine (one hedge_closed per
# order, one leg_orphaned per order, etc.). Setting cooldown=0 keeps
# the SET NX EX call but with TTL=0 the key never claims a window;
# ``bool(claimed)`` evaluates True only on the first emit per process
# unless someone passes ``bypass_cooldown=True`` (see below).
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
    # Step 4.11: hedge_closed — INFO terminal for a completed cascade.
    # cooldown_seconds=0 because the upstream state machine guarantees
    # one terminal event per order_id; we want every completion to
    # land. ttl 7 days mirrors 4.7b — sufficient for operator audit.
    "hedge_closed": {
        "severity": "INFO",
        "emoji": "✅",
        "cooldown_seconds": 0,
        "ttl_seconds": 7 * 86400,
    },
    # Step 4.11: leg_orphaned — CRITICAL terminal for a cascade-open
    # failure after the 3-retry budget exhausts. Operator must intervene
    # manually; the longer 30-day TTL gives a wider audit window for
    # post-mortem.
    "leg_orphaned": {
        "severity": "CRITICAL",
        "emoji": "🚨",
        "cooldown_seconds": 0,
        "ttl_seconds": 30 * 86400,
    },
    # Step 4.11: secondary_liquidation — CRITICAL split from the
    # generic ``hedge_leg_external_close_warning`` for the specific
    # ``close_reason="stop_out"`` case (Exness leg liquidated by the
    # broker; cascade close on FTMO leg is required immediately).
    # Same 30-day TTL as leg_orphaned.
    "secondary_liquidation": {
        "severity": "CRITICAL",
        "emoji": "🚨",
        "cooldown_seconds": 0,
        "ttl_seconds": 30 * 86400,
    },
}

ALERTS_CHANNEL = "alerts"

# HTTP timeout for the Telegram Bot API call. Plenty for sendMessage
# in practice; the design §2.B.3 picks 5s. We use 10s as a soft cap
# to absorb the occasional ~3s spike without spurious "timeout"
# warnings, while still bounding the emit() call so a hung API can't
# stall the caller for the full asyncio task lifetime.
_TELEGRAM_HTTP_TIMEOUT_SECONDS: float = 10.0


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
    """Publish + Telegram-dispatch alert orchestrator (steps 4.7b + 4.11).

    Built once in the FastAPI lifespan and injected into the per-Exness
    event_handler tasks plus HedgeService. ``emit`` is synchronous from
    the caller's perspective: it returns once Redis + WS publish have
    completed or been cooldown-suppressed AND the best-effort Telegram
    POST has either landed or been logged on failure. Telegram failures
    NEVER raise; ``emit`` always returns the freshly-issued alert_id
    when the publish path succeeds.
    """

    def __init__(
        self,
        redis_svc: RedisService,
        broadcast: BroadcastService,
        *,
        http_client: httpx.AsyncClient | None = None,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
        telegram_enabled: bool = False,
    ) -> None:
        self.redis = redis_svc
        self.broadcast = broadcast
        self._http_client = http_client
        self._telegram_bot_token = telegram_bot_token
        self._telegram_chat_id = telegram_chat_id
        self._telegram_enabled = telegram_enabled

    async def emit(
        self,
        *,
        alert_type: str,
        cooldown_key: str,
        title_vi: str,
        body_vi: str,
        context: dict[str, str],
        bypass_cooldown: bool = False,
    ) -> str | None:
        """Try to emit a new alert. Returns the freshly-generated
        ``alert_id`` on success, ``None`` when the cooldown for
        ``(alert_type, cooldown_key)`` is still active OR when the
        ``alert_type`` is unknown.

        Atomicity: cooldown claim uses ``SET NX EX`` so concurrent emits
        of the same (type, key) cannot both succeed.

        ``bypass_cooldown=True`` (design §2.A.3 — recovery alerts) skips
        the cooldown SET entirely so a recovery message lands even within
        the original outage alert's cooldown window. The cooldown key
        from a prior non-bypass emit is left untouched.
        """
        spec = ALERT_TYPES.get(alert_type)
        if spec is None:
            logger.error("alert.unknown_type alert_type=%s", alert_type)
            return None

        # Cooldown gate: skipped when ``bypass_cooldown=True`` (recovery
        # alerts per design §2.A.3) OR when the alert type's
        # ``cooldown_seconds`` is 0 (one-shot terminal types — upstream
        # state machine guarantees no duplicate emit per cooldown_key).
        if not bypass_cooldown and spec["cooldown_seconds"] > 0:
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

        # Step 4.11: best-effort Telegram delivery. Errors are logged
        # WARNING but never propagate — Redis + WS already captured
        # the alert, and the design §2.B.4 explicitly chose
        # fire-and-forget over a retry queue for Phase 4.
        await self._dispatch_telegram(payload)

        logger.warning(
            "alert.emitted alert_type=%s alert_id=%s severity=%s "
            "cooldown_key=%s context=%s",
            alert_type, alert_id, spec["severity"], cooldown_key, context,
        )
        return alert_id

    async def _dispatch_telegram(self, payload: AlertPayload) -> None:
        """Plain-text Telegram POST (design §2.E.4).

        No-op when the service is configured without Telegram (dev / CI),
        when the http_client wasn't injected, or when the bot_token /
        chat_id are missing. Any HTTPError, 4xx, or 5xx is logged WARNING
        and swallowed — the caller still gets the alert_id back.
        """
        if not self._telegram_enabled:
            return
        if (
            self._http_client is None
            or not self._telegram_bot_token
            or not self._telegram_chat_id
        ):
            # The model_validator on Settings forbids enabled=True without
            # token+chat. If we got here it means the operator built
            # AlertService outside the FastAPI lifespan (e.g. a test
            # forgot the http_client). Log once at INFO and skip.
            logger.info(
                "alert.telegram_skipped_no_client alert_id=%s",
                payload.alert_id,
            )
            return

        url = (
            f"https://api.telegram.org/bot{self._telegram_bot_token}"
            "/sendMessage"
        )
        text = self._format_telegram_text(payload)
        try:
            response = await self._http_client.post(
                url,
                json={"chat_id": self._telegram_chat_id, "text": text},
                timeout=_TELEGRAM_HTTP_TIMEOUT_SECONDS,
            )
            if response.status_code >= 400:
                logger.warning(
                    "alert.telegram_dispatch_failed alert_id=%s "
                    "alert_type=%s status=%d body=%s",
                    payload.alert_id, payload.alert_type,
                    response.status_code, response.text[:200],
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "alert.telegram_dispatch_exception alert_id=%s "
                "alert_type=%s exc=%s",
                payload.alert_id, payload.alert_type, exc,
            )

    @staticmethod
    def _format_telegram_text(payload: AlertPayload) -> str:
        """Plain-text rendering per design §2.E.4.

        No parse_mode (no Markdown / HTML) so caller templates carry
        verbatim, including Vietnamese diacritics and special characters
        like ``*``, ``_``, ``[``, ``]``. Telegram's plain-text body
        budget is 4096 chars; templates are well under that.

        Caller templates already embed the emoji at the head of
        ``title_vi`` (matches the 4.7b convention — e.g. ``"⚠️ Lệnh
        Exness đóng ngoài hệ thống"``), so we render the title verbatim
        rather than prepending ``payload.emoji`` again.
        """
        lines = [payload.title_vi, "", payload.body_vi]
        if payload.context:
            lines.append("")
            for k, v in payload.context.items():
                lines.append(f"{k}: {v}")
        return "\n".join(lines)
