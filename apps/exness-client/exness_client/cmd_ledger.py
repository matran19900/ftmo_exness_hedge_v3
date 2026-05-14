"""Client-side ledger of server-issued close commands (Phase 4.3a).

When the server issues a ``close`` cmd via ``cmd_stream:exness:{account_id}``,
the action handler marks the target ticket in this ledger BEFORE calling
``mt5.order_send``. The position monitor checks the ledger when it sees a
ticket disappear and stamps the resulting ``position_closed_external``
event with one of two values:

  - ``close_reason="server_initiated"``  — ledger hit (we issued the close)
  - ``close_reason="external"``           — ledger miss (manual close,
                                            SL/TP hit, stop-out, EA close)

Per CEO policy (Phase 4 working memory): the secondary (Exness) leg is
always passive. Anything stamped ``external`` will be turned into a
WARNING alert by the server-side cascade orchestrator (step 4.7) — we
don't fire a cascade close on the FTMO side because the operator (or
the broker) already moved the leg.

Storage: Redis SET ``cmd_ledger:exness:{account_id}:server_initiated``.
TTL: 24h auto-expire — the server processes resp_stream replies in
seconds, so a 24h ceiling is generous and prevents a rare leak from
growing unboundedly.

All Redis operations swallow exceptions: a flaky Redis must NOT block
the close-cmd flow or crash the monitor poll loop. ``mark_*`` failures
log a warning and proceed (worst case the close gets misclassified as
``external`` and triggers a benign WARNING alert). ``is_server_initiated``
defaults to ``False`` on failure (conservative — assume external).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CmdLedger:
    """Per-account Redis-SET ledger of in-flight server-issued close cmds."""

    KEY_PATTERN = "cmd_ledger:exness:{account_id}:server_initiated"
    TTL_SECONDS = 24 * 3600

    def __init__(self, redis_client: Any, account_id: str) -> None:
        self._redis = redis_client
        self._account_id = account_id
        self._key = self.KEY_PATTERN.format(account_id=account_id)

    @property
    def key(self) -> str:
        return self._key

    async def mark_server_initiated(self, ticket: int) -> None:
        """Add ``ticket`` to the ledger. Idempotent — repeated marks are
        cheap. Refreshes the 24h TTL on every mark so a long-running
        server stays healthy."""
        try:
            await self._redis.sadd(self._key, str(ticket))
            await self._redis.expire(self._key, self.TTL_SECONDS)
            logger.debug(
                "cmd_ledger.mark_server_initiated ticket=%s key=%s",
                ticket,
                self._key,
            )
        except Exception:
            logger.warning(
                "cmd_ledger.mark_failed ticket=%s key=%s",
                ticket,
                self._key,
                exc_info=True,
            )

    async def is_server_initiated(self, ticket: int) -> bool:
        """Return ``True`` iff ``ticket`` is in the ledger. On Redis
        failure, return ``False`` so the monitor classifies the close
        as ``external`` — the conservative choice (worst case the
        operator gets a WARNING alert for a close they actually
        triggered)."""
        try:
            return bool(await self._redis.sismember(self._key, str(ticket)))
        except Exception:
            logger.warning(
                "cmd_ledger.check_failed ticket=%s key=%s",
                ticket,
                self._key,
                exc_info=True,
            )
            return False

    async def clear(self, ticket: int) -> None:
        """Remove ``ticket`` from the ledger after the corresponding
        ``position_closed_external`` event has been published. Failure
        is logged but never raised — a leaked entry will TTL-expire
        within 24h."""
        try:
            await self._redis.srem(self._key, str(ticket))
            logger.debug(
                "cmd_ledger.cleared ticket=%s key=%s",
                ticket,
                self._key,
            )
        except Exception:
            logger.warning(
                "cmd_ledger.clear_failed ticket=%s key=%s",
                ticket,
                self._key,
                exc_info=True,
            )
