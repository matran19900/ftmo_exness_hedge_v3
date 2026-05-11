"""XREADGROUP loop for ``cmd_stream:ftmo:{account_id}``.

Reads commands the server pushed via ``RedisService.push_command``,
dispatches them to the appropriate stub handler in
``action_handlers.py``, and XACKs after the handler returns.

Consumer group naming is locked to ``ftmo-{account_id}`` to match what
``RedisService.setup_consumer_groups()`` creates in the server lifespan
(``server/app/services/redis_service.py``). Drift between the two
strings would silently break delivery — the test
``test_command_loop_consumer_group_matches_server`` pins the contract.
"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError

from ftmo_client.action_handlers import ACTION_HANDLERS
from ftmo_client.shutdown import ShutdownController

logger = logging.getLogger(__name__)

# How many messages to drain per XREADGROUP call. 10 is a comfortable
# size — small enough to keep latency low on a single command, large
# enough to amortize the round-trip when a burst arrives.
XREADGROUP_COUNT = 10
# Block timeout (ms) inside XREADGROUP. Any value > 0 keeps the loop
# from busy-spinning when no commands are queued; small enough that
# shutdown signal is honored within the same window.
XREADGROUP_BLOCK_MS = 5000
# Sleep between recoverable RedisError retries so a flap doesn't busy
# loop log warnings.
ERROR_BACKOFF_SECONDS = 1.0


def _stream_name(account_id: str) -> str:
    return f"cmd_stream:ftmo:{account_id}"


def _group_name(account_id: str) -> str:
    return f"ftmo-{account_id}"


def _consumer_name(account_id: str) -> str:
    # Single-consumer per process. If we ever fan-out across multiple
    # workers per account, the consumer name should include a worker
    # index — but step 3.3 has 1 process per account.
    return f"ftmo-{account_id}"


async def command_loop(
    redis: redis_asyncio.Redis,
    account_id: str,
    shutdown: ShutdownController,
) -> None:
    """Run XREADGROUP → dispatch → XACK forever, until shutdown is requested."""
    stream = _stream_name(account_id)
    group = _group_name(account_id)
    consumer = _consumer_name(account_id)
    logger.info(
        "command_loop starting: stream=%s group=%s consumer=%s",
        stream,
        group,
        consumer,
    )
    while not shutdown.is_requested:
        try:
            entries = await redis.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream: ">"},
                count=XREADGROUP_COUNT,
                block=XREADGROUP_BLOCK_MS,
            )
        except asyncio.CancelledError:
            raise
        except RedisError as exc:
            logger.warning("xreadgroup failed: %s", exc)
            await asyncio.sleep(ERROR_BACKOFF_SECONDS)
            continue

        for _stream, msgs in entries or []:
            for msg_id, fields in msgs:
                await _dispatch_one(redis, account_id, stream, group, msg_id, fields)
    logger.info("command_loop exiting (account=%s)", account_id)


async def _dispatch_one(
    redis: redis_asyncio.Redis,
    account_id: str,
    stream: str,
    group: str,
    msg_id: str,
    fields: dict[str, str],
) -> None:
    """Run the matching handler; XACK after it returns (success or known-skip).

    Unknown actions are XACKed so they don't accumulate in pending — the
    server-side push contract limits ``action`` to known values; an
    unknown one is a bug, not a transient fault.

    Handler exceptions are logged but the message is still XACKed —
    leaving it pending would block the consumer (XPENDING grows) and
    the server would retry, but step 3.3 stubs can't fail meaningfully
    anyway. Step 3.4 will introduce a dead-letter or no-ack path for
    handler failures that warrant retry.
    """
    action = fields.get("action", "")
    request_id = fields.get("request_id", "")
    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        logger.warning(
            "unknown action %r request_id=%s msg_id=%s; XACKing",
            action,
            request_id,
            msg_id,
        )
    else:
        try:
            await handler(redis, account_id, fields)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "handler %r raised; XACKing message msg_id=%s request_id=%s",
                action,
                msg_id,
                request_id,
            )

    try:
        await redis.xack(stream, group, msg_id)
    except RedisError as exc:
        logger.warning("xack failed for %s/%s/%s: %s", stream, group, msg_id, exc)
