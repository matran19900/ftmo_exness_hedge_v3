"""Exness client entry point.

Wires Settings → MT5 module selection → Redis → bridge connect →
heartbeat + command_processor tasks. Exits 0 on graceful shutdown, 1
on generic MT5 connect failure, 2 when the MT5 account is in netting
mode (hedging mode is mandatory — see ``bridge_service.py``).

Module selection happens up front: on Windows we import the real
``MetaTrader5`` package; on Linux (dev / CI) we fall back to
``exness_client.mt5_stub`` which mirrors the API surface tests need.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import ModuleType

import redis.asyncio as redis_asyncio

from exness_client.action_handlers import ActionHandler
from exness_client.bridge_service import (
    MT5BridgeService,
    MT5ConnectError,
    MT5HedgingModeRequiredError,
)
from exness_client.cmd_ledger import CmdLedger
from exness_client.command_processor import CommandProcessor
from exness_client.config import ExnessClientSettings
from exness_client.heartbeat import HeartbeatLoop
from exness_client.position_monitor import PositionMonitor
from exness_client.shutdown import ShutdownCoordinator
from exness_client.symbol_sync import SymbolSyncPublisher

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONNECT_FAILED = 1
EXIT_NETTING_MODE = 2


def _select_mt5_module() -> ModuleType:
    """Return ``MetaTrader5`` on Windows, ``exness_client.mt5_stub`` elsewhere.

    Production runtime is Windows-only (see ``docs/phase-4-design.md``
    D-4.0-2). The stub keeps unit tests + Linux dev viable; importing
    it on Windows would shadow the real lib, so the platform check is
    strict.
    """
    if sys.platform == "win32":
        import MetaTrader5 as mt5_module  # type: ignore[import-not-found]  # noqa: PLC0415

        return mt5_module
    from exness_client import mt5_stub as mt5_module  # noqa: PLC0415

    logger.warning(
        "running_with_mt5_stub: platform=%s — cannot place real orders; "
        "for production use Windows.",
        sys.platform,
    )
    return mt5_module


async def _connect_redis(redis_url: str) -> redis_asyncio.Redis:
    """Open a Redis connection and verify with PING."""
    client: redis_asyncio.Redis = redis_asyncio.from_url(  # type: ignore[no-untyped-call]
        redis_url, decode_responses=True, max_connections=8
    )
    await client.ping()
    return client


async def amain(
    settings: ExnessClientSettings | None = None,
    mt5_module: ModuleType | None = None,
) -> int:
    """Main coroutine. Tests call directly with a custom Settings + stub mt5."""
    if settings is None:
        settings = ExnessClientSettings()  # type: ignore[call-arg]
    if mt5_module is None:
        mt5_module = _select_mt5_module()

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("exness-client starting: account=%s", settings.account_id)

    redis = await _connect_redis(settings.redis_url)
    logger.info("redis connected")

    bridge = MT5BridgeService(settings, mt5_module=mt5_module)

    try:
        await bridge.connect()
    except MT5HedgingModeRequiredError:
        logger.critical("mt5_account_netting_mode_not_supported")
        await redis.aclose()
        return EXIT_NETTING_MODE
    except MT5ConnectError:
        logger.exception("mt5_connect_failed")
        await redis.aclose()
        return EXIT_CONNECT_FAILED

    # Step 4.2: publish the broker's tradeable symbols once on connect so
    # the server's wizard sees them on first paint. Failure is non-fatal —
    # the operator can re-trigger via the ``resync_symbols`` cmd, and we
    # don't want a transient Redis blip to kill the bridge.
    symbol_sync = SymbolSyncPublisher(redis, settings.account_id, mt5_module)
    try:
        published = await symbol_sync.publish_snapshot()
        logger.info("initial_symbol_sync_done count=%d", published)
    except Exception:
        logger.exception("initial_symbol_sync_failed_continuing")

    # Step 4.3a: per-account ledger of server-issued close cmds. The
    # action handler marks tickets here BEFORE issuing the close to MT5
    # so the position monitor can stamp ``close_reason="server_initiated"``
    # on the resulting ``position_closed_external`` event.
    cmd_ledger = CmdLedger(redis, settings.account_id)

    action_handler = ActionHandler(
        redis, settings.account_id, mt5_module, symbol_sync, cmd_ledger
    )

    cmd_proc = CommandProcessor(
        redis,
        bridge,
        settings.account_id,
        action_handler=action_handler,
        block_ms=settings.cmd_stream_block_ms,
    )
    await cmd_proc.ensure_consumer_group()

    heartbeat = HeartbeatLoop(
        redis, bridge, settings.account_id, settings.heartbeat_interval_s
    )

    # First heartbeat publish before entering the loop so the server
    # sees the client as ``online`` before the next interval elapses.
    try:
        await heartbeat.publish_once()
    except Exception:
        logger.exception("initial heartbeat write failed; loop will retry")

    # Step 4.3: position monitor — 2s poll loop that diffs MT5
    # positions and publishes ``event_stream:exness:{account_id}``
    # events. Drives the server-side cascade orchestrator (step 4.7/4.8).
    position_monitor = PositionMonitor(
        redis, settings.account_id, mt5_module, cmd_ledger
    )

    coord = ShutdownCoordinator(
        cmd_proc, heartbeat, bridge, redis, position_monitor=position_monitor
    )
    coord.install_signal_handlers()

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(cmd_proc.run(), name="cmd_processor"),
        asyncio.create_task(position_monitor.run(), name="position_monitor"),
        asyncio.create_task(heartbeat.run(), name="heartbeat"),
    ]
    logger.info("exness_client.started account_id=%s", settings.account_id)

    try:
        await coord.wait_for_shutdown()
    finally:
        await coord.shutdown(tasks)
    logger.info("exness-client shutdown complete")
    return EXIT_OK


def main() -> None:
    """Sync entry point used by the ``exness-client`` console script."""
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
