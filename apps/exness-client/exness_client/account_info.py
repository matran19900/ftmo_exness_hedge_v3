"""Account info publisher for the Exness MT5 client (Phase 4.4).

Polls ``mt5.account_info()`` every ``POLL_INTERVAL_S`` seconds and
publishes the snapshot as a Redis HASH at
``account:exness:{account_id}``. The server-side position tracker
(step 4.9) reads this for unrealised P&L baselines and the frontend
account-status bar (step 4.10) reads it for the live balance / equity /
margin display.

Mirrors the FTMO ``account_info_loop`` Phase 3 pattern (D-122) — same
30-second cadence, same first-publish-immediate semantics, same flat
string-keyed HASH shape (every value stringified per Redis convention).
The Exness HASH adds ``margin_mode`` because Phase 4 R1 requires the
account to be in ``RETAIL_HEDGING`` mode and the server uses the
published value as a defence-in-depth check before issuing a hedge cmd.

All MT5 calls run via ``asyncio.to_thread`` (D-4.1.A). All exceptions
are logged and swallowed so a flaky broker / Redis blip never takes
down the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


POLL_INTERVAL_S = 30.0


class AccountInfoPublisher:
    """Periodic ``mt5.account_info()`` poll → Redis HASH publish.

    Public API:
      - ``run()``    — main loop; cancels via ``stop()``.
      - ``stop()``   — flips the internal asyncio.Event; the loop exits
                       on its next wake (worst case ``POLL_INTERVAL_S``).
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
        self._key = f"account:exness:{account_id}"
        self._stop_event = asyncio.Event()

    @property
    def key(self) -> str:
        return self._key

    async def run(self) -> None:
        """Drain publishes until ``stop()`` is called.

        The first publish runs *immediately* on entry so the server sees
        a populated HASH before the first 30-second interval elapses —
        otherwise the AccountStatusBar would render a "—" placeholder
        for half a minute on every client restart."""
        logger.info(
            "account_info.starting account_id=%s interval_s=%s",
            self._account_id,
            self._poll_interval_s,
        )
        try:
            await self._publish_once()
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._poll_interval_s,
                    )
                except TimeoutError:
                    pass
                if self._stop_event.is_set():
                    break
                await self._publish_once()
        finally:
            logger.info(
                "account_info.stopped account_id=%s", self._account_id
            )

    async def stop(self) -> None:
        """Flip the stop flag. Awaitable for ``ShutdownCoordinator``
        symmetry with the other lifecycle loops."""
        self._stop_event.set()

    async def _publish_once(self) -> None:
        """Single ``account_info`` fetch + Redis HSET. Returns silently
        on any error so the next interval still runs."""
        try:
            account_info = await asyncio.to_thread(self._mt5.account_info)
        except Exception:
            logger.exception(
                "account_info.account_info_exception account_id=%s",
                self._account_id,
            )
            return
        if account_info is None:
            logger.warning(
                "account_info.none_response account_id=%s",
                self._account_id,
            )
            return
        payload: dict[str, str] = {
            "broker": "exness",
            "account_id": self._account_id,
            "login": str(account_info.login),
            "balance": str(account_info.balance),
            "equity": str(account_info.equity),
            "margin": str(account_info.margin),
            "free_margin": str(account_info.margin_free),
            "leverage": str(account_info.leverage),
            "currency": account_info.currency,
            "server": account_info.server,
            "margin_mode": str(account_info.margin_mode),
            "synced_at_ms": str(int(time.time() * 1000)),
        }
        try:
            await self._redis.hset(self._key, mapping=payload)
            logger.debug(
                "account_info.published key=%s balance=%s equity=%s",
                self._key,
                account_info.balance,
                account_info.equity,
            )
        except Exception:
            logger.exception(
                "account_info.publish_failed key=%s", self._key
            )
