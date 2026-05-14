"""XREADGROUP loop for ``cmd_stream:exness:{account_id}``.

Step 4.1 ships the SKELETON: the loop reads entries, logs each action,
and XACKs. Real handlers (open / close / modify) land in step 4.2 along
with response publishing on ``resp_stream:exness:{account_id}``.

Consumer group naming follows the D-086 convention used by the FTMO
client (``ftmo-{acc}``); the Exness side mirrors with ``exness-{acc}``.
``RedisService.setup_consumer_groups`` on the server is responsible for
``XGROUP CREATE`` on lifespan startup — this module also calls
``ensure_consumer_group`` so the client can be tested + run standalone
without a hot server.
"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError, ResponseError

from exness_client.action_handlers import ActionHandler
from exness_client.bridge_service import MT5BridgeService

logger = logging.getLogger(__name__)


XREADGROUP_COUNT = 10
ERROR_BACKOFF_SECONDS = 1.0


def _stream_name(account_id: str) -> str:
    return f"cmd_stream:exness:{account_id}"


def _group_name(account_id: str) -> str:
    return f"exness-{account_id}"


def _consumer_name(account_id: str) -> str:
    # Single-consumer per process; matches the FTMO client convention.
    return f"exness-{account_id}"


class CommandProcessor:
    """XREADGROUP → dispatch → XACK loop.

    Step 4.2: an ``ActionHandler`` instance is passed in at construction
    time and ``_dispatch_one`` delegates to ``handler.dispatch(fields)``.
    The handler is the one place that knows about MT5 — keeping that
    coupling out of the loop lets us unit-test the loop without an MT5
    stub when only the XREADGROUP/XACK semantics matter.
    """

    def __init__(
        self,
        redis: redis_asyncio.Redis,
        bridge: MT5BridgeService,
        account_id: str,
        action_handler: ActionHandler | None = None,
        block_ms: int = 1000,
    ) -> None:
        self._redis = redis
        self._bridge = bridge
        self._account_id = account_id
        self._action_handler = action_handler
        self._stream = _stream_name(account_id)
        self._group = _group_name(account_id)
        self._consumer = _consumer_name(account_id)
        self._block_ms = block_ms
        self._running = False
        self._stopped = asyncio.Event()

    async def ensure_consumer_group(self) -> None:
        """``XGROUP CREATE`` with MKSTREAM. Idempotent (catches BUSYGROUP).

        Server lifespan also creates the group when the account is
        registered; calling this here makes the client robust to
        standalone smoke runs and order-of-startup edge cases.
        """
        try:
            await self._redis.xgroup_create(
                name=self._stream,
                groupname=self._group,
                id="0",
                mkstream=True,
            )
            logger.info(
                "xgroup created: stream=%s group=%s", self._stream, self._group
            )
        except ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                logger.debug(
                    "xgroup already exists: stream=%s group=%s",
                    self._stream,
                    self._group,
                )
                return
            raise

    async def run(self) -> None:
        """Drain commands until ``stop()`` is called.

        Per-iteration:
          - XREADGROUP COUNT 10 BLOCK ``block_ms`` against ``>``.
          - For each entry: call ``self._dispatch``, then XACK.
          - On RedisError: sleep ``ERROR_BACKOFF_SECONDS`` and continue.
          - On ``CancelledError``: re-raise (asyncio teardown path).
        """
        logger.info(
            "command_processor starting: stream=%s group=%s consumer=%s",
            self._stream,
            self._group,
            self._consumer,
        )
        self._running = True
        self._stopped.clear()
        try:
            while self._running:
                try:
                    entries = await self._redis.xreadgroup(
                        groupname=self._group,
                        consumername=self._consumer,
                        streams={self._stream: ">"},
                        count=XREADGROUP_COUNT,
                        block=self._block_ms,
                    )
                except asyncio.CancelledError:
                    raise
                except RedisError as exc:
                    logger.warning("xreadgroup failed: %s", exc)
                    await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                    continue

                for _stream, msgs in entries or []:
                    for msg_id, fields in msgs:
                        await self._dispatch_one(msg_id, fields)
        finally:
            logger.info("command_processor exiting (account=%s)", self._account_id)

    async def stop(self) -> None:
        """Flip the running flag; the XREADGROUP BLOCK returns within
        ``block_ms`` and the loop exits."""
        self._running = False
        self._stopped.set()

    # ----- Internal -----

    async def _dispatch_one(
        self, msg_id: str, fields: dict[str, str]
    ) -> None:
        """Step 4.2: route to ``ActionHandler.dispatch`` then XACK.

        The XACK runs unconditionally so an unrecognized action does not
        accumulate in XPENDING — matches the FTMO command_loop contract.
        Handler exceptions are logged but never block the XACK; the
        handler is responsible for publishing an ``error`` response on
        its own resp_stream so the server can correlate.

        When ``action_handler`` is ``None`` (the step-4.1 skeleton path,
        kept alive for the existing dispatch-skeleton tests), we fall
        back to the old log + XACK behaviour.
        """
        if self._action_handler is None:
            action = fields.get("action", "")
            request_id = fields.get("request_id", "")
            order_id = fields.get("order_id", "")
            logger.warning(
                "action_not_implemented_phase_4_1: action=%r order_id=%s "
                "request_id=%s msg_id=%s",
                action,
                order_id,
                request_id,
                msg_id,
            )
        else:
            try:
                await self._action_handler.dispatch(fields)
            except Exception:
                # The handler should publish its own error response; we
                # log here as a defensive backstop and still XACK so the
                # cmd doesn't loop forever.
                logger.exception(
                    "action_handler.dispatch raised: msg_id=%s action=%s",
                    msg_id,
                    fields.get("action", ""),
                )
        try:
            await self._redis.xack(self._stream, self._group, msg_id)
        except RedisError as exc:
            logger.warning(
                "xack failed for %s/%s/%s: %s",
                self._stream,
                self._group,
                msg_id,
                exc,
            )
