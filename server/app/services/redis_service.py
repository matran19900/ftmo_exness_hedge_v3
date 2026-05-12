"""Redis access layer.

Phase 2.1 introduces this module with the minimum surface required to support
cTrader OAuth credential storage, OAuth CSRF state, and symbol-config caching.
Future phases will extend it (orders, accounts, pairs, etc.) per docs/07-server-services.md.

Phase 3.1 appends order CRUD, stream/consumer-group helpers, pending tracking,
position P&L cache, heartbeat lookup, account management, and settings — see
docs/06-data-models.md for the underlying schema. Existing Phase 1+2 methods
were left untouched (CTO requirement: append-only).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal, TypedDict, cast
from uuid import uuid4

import redis.asyncio as redis_asyncio
from redis.commands.core import AsyncScript
from redis.exceptions import ResponseError

from app.redis_client import get_redis
from app.services.redis_service_lua import UPDATE_ORDER_LUA

Broker = Literal["ftmo", "exness"]
StreamKind = Literal["cmd_stream", "resp_stream", "event_stream"]
LegPrefix = Literal["p", "s"]

# Phase 3.1 — `account_id` format. Lowercase alphanum + underscore, 3–64 chars.
# Note this is stricter than docs/05-redis-protocol.md §2 ("a-zA-Z0-9_-",
# 1–32) but matches the Phase 3 prompt; the looser doc rule is documented
# elsewhere and revisited in Phase 5 hardening.
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9_]{3,64}$")
_ALLOWED_BROKERS: tuple[str, ...] = ("ftmo", "exness")
# Stream cap so an unbounded producer can't blow up Redis memory. Keep in
# sync with docs/06-data-models.md §11 ("Streams MAXLEN ~ 10000").
_STREAM_MAXLEN = 10000
# Position cache TTL — see docs/06-data-models.md §8.
_POSITION_PNL_TTL_SECONDS = 600
# Snapshot ZSET TTL refresh on each add (lazy; see docs/06-data-models.md §8).
_SNAPSHOT_TTL_SECONDS = 600
# Side-index TTL for request_id → order_id lookup. 24h matches the
# practical lifetime of an in-flight command; orphan entries fall off
# automatically.
_REQUEST_ID_INDEX_TTL_SECONDS = 86400


class OrderHash(TypedDict, total=False):
    """Schema mirror of ``order:{order_id}`` HASH (docs/06-data-models.md §7).

    All fields are stored as strings in Redis; conversion to typed values
    is the caller's responsibility. ``total=False`` because HGETALL only
    returns fields that have been HSET — partial reads are normal (e.g.
    ``closed_at`` is empty until close).
    """

    order_id: str
    pair_id: str
    ftmo_account_id: str
    exness_account_id: str
    symbol: str
    side: str
    status: str
    risk_amount: str
    secondary_ratio: str
    sl_price: str
    tp_price: str
    order_type: str
    entry_price: str
    # Primary leg (FTMO)
    p_status: str
    p_volume_lots: str
    p_broker_order_id: str
    p_fill_price: str
    p_executed_at: str
    p_close_price: str
    p_closed_at: str
    p_close_reason: str
    p_realized_pnl: str
    p_commission: str
    # Secondary leg (Exness)
    s_status: str
    s_volume_lots: str
    s_broker_order_id: str
    s_fill_price: str
    s_executed_at: str
    s_close_price: str
    s_closed_at: str
    s_close_reason: str
    s_realized_pnl: str
    s_commission: str
    # Lifecycle
    created_at: str
    updated_at: str
    closed_at: str
    final_pnl_usd: str
    # Errors / retry
    s_error_msg: str
    s_retry_count: str
    # Step 3.7: response/event handler outputs.
    # Open-error path (cmd ACK with status=error).
    p_error_code: str
    p_error_msg: str
    # Close-error path (close ACK with status=error; position not closed).
    p_close_error_code: str
    p_close_error_msg: str
    # Modify-error path (amend rejected by broker).
    p_modify_error_code: str
    p_modify_error_msg: str
    # SL/TP attach warning (D-059: market fill OK but amend rejected).
    p_sl_tp_warning: str
    p_sl_tp_warning_msg: str
    # Extended close-detail fields from step 3.5a position_closed
    # payload (p_close_reason is also declared above in the primary-leg
    # block — kept there as the canonical site).
    p_swap: str
    p_balance_after_close: str
    p_money_digits: str
    p_closed_volume: str
    # Step 3.5b reconcile marker: order's close was reconstructed
    # from cTrader deal history, not received live.
    p_reconstructed: str


class PositionPnlSnapshot(TypedDict, total=False):
    """Schema mirror of ``position:{order_id}`` STRING JSON (docs §8)."""

    order_id: str
    symbol: str
    p_pnl_usd: float
    s_pnl_usd: float
    total_pnl_usd: float
    p_current_price: float
    s_current_price: float
    computed_at: int


def _validate_broker(broker: str) -> None:
    if broker not in _ALLOWED_BROKERS:
        raise ValueError(f"broker must be one of {_ALLOWED_BROKERS!r}, got {broker!r}")


def _validate_account_id(account_id: str) -> None:
    if not _ACCOUNT_ID_RE.match(account_id):
        raise ValueError(
            f"account_id {account_id!r} must match {_ACCOUNT_ID_RE.pattern} "
            "(lowercase alphanum + underscore, 3–64 chars)"
        )


class RedisService:
    """Thin async wrapper that owns a single Redis pool reference."""

    def __init__(self, redis: redis_asyncio.Redis) -> None:
        self._redis = redis
        # Pre-register the order-update Lua script. ``register_script`` is
        # synchronous and just builds a Script wrapper that lazily uploads on
        # first call (via EVALSHA, falling back to EVAL on NOSCRIPT). Doing
        # this in __init__ keeps the per-call path on the hot trading flow
        # free of branching/lazy init.
        self._update_order_script: AsyncScript = redis.register_script(UPDATE_ORDER_LUA)

    # ----- cTrader market-data credentials -----

    async def set_ctrader_market_data_creds(
        self,
        access_token: str,
        refresh_token: str,
        account_id: int,
        expires_at: int,
    ) -> None:
        """Persist OAuth tokens for the cTrader market-data account.

        `expires_at` is a unix timestamp in seconds.
        """
        await self._redis.hset(  # type: ignore[misc]
            "ctrader:market_data_creds",
            mapping={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": str(account_id),
                "expires_at": str(expires_at),
            },
        )

    async def get_ctrader_market_data_creds(self) -> dict[str, Any] | None:
        """Return stored OAuth credentials or None if not present."""
        data = await self._redis.hgetall("ctrader:market_data_creds")  # type: ignore[misc]
        if not data:
            return None
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "account_id": int(data["account_id"]),
            "expires_at": int(data["expires_at"]),
        }

    # ----- Symbol config / active symbols -----

    async def set_symbol_config(self, ftmo_symbol: str, config: dict[str, Any]) -> None:
        """Persist a synced symbol's broker-side details under its FTMO key."""
        await self._redis.hset(  # type: ignore[misc]
            f"symbol_config:{ftmo_symbol}",
            mapping={k: str(v) for k, v in config.items()},
        )

    async def get_symbol_config(self, ftmo_symbol: str) -> dict[str, str] | None:
        data = await self._redis.hgetall(f"symbol_config:{ftmo_symbol}")  # type: ignore[misc]
        if not data:
            return None
        return dict(data)

    async def add_active_symbol(self, ftmo_symbol: str) -> None:
        await self._redis.sadd("symbols:active", ftmo_symbol)  # type: ignore[misc]

    async def get_active_symbols(self) -> list[str]:
        members = await self._redis.smembers("symbols:active")  # type: ignore[misc]
        return sorted(members)

    async def clear_active_symbols(self) -> None:
        """Drop the active-symbols set so a re-sync rebuilds it cleanly."""
        await self._redis.delete("symbols:active")

    # ----- OHLC cache -----

    async def get_ohlc_cache(self, key: str) -> str | None:
        """Return the cached OHLC JSON string for ``key`` or None if missing/expired."""
        value = await self._redis.get(f"ohlc:{key}")
        if value is None:
            return None
        return str(value)

    async def set_ohlc_cache(self, key: str, json_str: str, ttl_seconds: int = 60) -> None:
        """Cache an OHLC JSON payload under ``ohlc:{key}`` with a TTL."""
        await self._redis.setex(f"ohlc:{key}", ttl_seconds, json_str)

    # ----- Tick cache (latest bid/ask per symbol) -----

    async def set_tick_cache(self, ftmo_symbol: str, json_str: str, ttl_seconds: int = 60) -> None:
        """Cache the latest tick under ``tick:{ftmo_symbol}`` with a TTL.

        Used by Phase 2.4 conversion-rate calc and Phase 3 P&L snapshots.
        """
        await self._redis.setex(f"tick:{ftmo_symbol}", ttl_seconds, json_str)

    async def get_tick_cache(self, ftmo_symbol: str) -> str | None:
        """Return the cached tick JSON string, or None if missing/expired."""
        value = await self._redis.get(f"tick:{ftmo_symbol}")
        if value is None:
            return None
        return str(value)

    # ----- Pairs CRUD -----

    async def create_pair(self, pair_id: str, fields: dict[str, Any]) -> None:
        """Atomically create a pair: HSET pair:{id} + SADD pairs:all."""
        pipe = self._redis.pipeline()
        pipe.hset(f"pair:{pair_id}", mapping={k: str(v) for k, v in fields.items()})
        pipe.sadd("pairs:all", pair_id)
        await pipe.execute()

    async def get_pair(self, pair_id: str) -> dict[str, str] | None:
        """Return the pair hash by id, or None if not present."""
        data = await self._redis.hgetall(f"pair:{pair_id}")  # type: ignore[misc]
        if not data:
            return None
        return dict(data)

    async def list_pairs(self) -> list[dict[str, str]]:
        """Return all pairs sorted by ``created_at`` desc (newest first)."""
        ids = await self._redis.smembers("pairs:all")  # type: ignore[misc]
        if not ids:
            return []
        out: list[dict[str, str]] = []
        for pid in ids:
            data = await self._redis.hgetall(f"pair:{pid}")  # type: ignore[misc]
            if data:
                out.append(dict(data))
        out.sort(key=lambda p: int(p.get("created_at", "0")), reverse=True)
        return out

    async def update_pair(self, pair_id: str, fields: dict[str, Any]) -> bool:
        """Patch a pair's fields in place. Return False if the pair doesn't exist."""
        exists = await self._redis.sismember("pairs:all", pair_id)  # type: ignore[misc]
        if not exists:
            return False
        await self._redis.hset(  # type: ignore[misc]
            f"pair:{pair_id}", mapping={k: str(v) for k, v in fields.items()}
        )
        return True

    async def delete_pair(self, pair_id: str) -> bool:
        """Atomically delete a pair. Return False if it didn't exist."""
        exists = await self._redis.sismember("pairs:all", pair_id)  # type: ignore[misc]
        if not exists:
            return False
        pipe = self._redis.pipeline()
        pipe.delete(f"pair:{pair_id}")
        pipe.srem("pairs:all", pair_id)
        await pipe.execute()
        return True

    # =========================================================================
    # Phase 3.1 additions — append-only.
    # Existing Phase 1+2 methods above stay untouched per CTO instruction.
    # =========================================================================

    # ----- Stream / consumer-group helpers (docs/05-redis-protocol.md §3) -----

    async def _create_group(self, stream: str, group: str) -> None:
        """Create consumer group; swallow BUSYGROUP so callers can run idempotently.

        ``mkstream=True`` so the call also creates the stream when missing —
        clients can join groups before the first XADD on a fresh deploy.
        """
        try:
            await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def setup_consumer_groups(self) -> tuple[int, int]:
        """Create the 3 streams × 2 brokers × N accounts of consumer groups.

        Idempotent: BUSYGROUP errors are swallowed in ``_create_group`` so
        repeated lifespan invocations don't fail. Wired to FastAPI lifespan
        in step 3.2.

        Returns ``(ftmo_count, exness_count)`` so the caller (lifespan,
        step 3.2) can log how many accounts were processed. ``(0, 0)`` is a
        valid result on first boot before any account is registered.
        """
        ftmo_accs = await self.get_all_account_ids("ftmo")
        for acc in ftmo_accs:
            await self._create_group(f"cmd_stream:ftmo:{acc}", f"ftmo-{acc}")
            await self._create_group(f"resp_stream:ftmo:{acc}", "server")
            await self._create_group(f"event_stream:ftmo:{acc}", "server")

        exness_accs = await self.get_all_account_ids("exness")
        for acc in exness_accs:
            await self._create_group(f"cmd_stream:exness:{acc}", f"exness-{acc}")
            await self._create_group(f"resp_stream:exness:{acc}", "server")
            await self._create_group(f"event_stream:exness:{acc}", "server")

        return len(ftmo_accs), len(exness_accs)

    async def push_command(
        self,
        broker: str,
        account_id: str,
        fields: dict[str, str],
    ) -> str:
        """Push a command into ``cmd_stream:{broker}:{account_id}``.

        Generates ``request_id`` (uuid4 hex) + ``created_at`` (epoch ms),
        XADDs with capped maxlen, and ZADDs the request into
        ``pending_cmds:{broker}:{account_id}`` with score = same epoch ms.
        Returns the generated request_id so the caller can correlate the
        eventual response.
        """
        _validate_broker(broker)
        _validate_account_id(account_id)

        request_id = uuid4().hex
        now_ms = int(time.time() * 1000)

        # Mutate a copy so the caller's dict isn't surprised by extra keys.
        # Typed as Any-valued so xadd's broader value-type hint accepts it
        # (redis-py's stub allows bytes/str/int/float values; dict is invariant).
        full_fields: dict[str, Any] = {
            **fields,
            "request_id": request_id,
            "created_at": str(now_ms),
        }

        stream = f"cmd_stream:{broker}:{account_id}"
        pending_key = f"pending_cmds:{broker}:{account_id}"

        # Cast: redis-py's xadd dict-arg type is invariant on values, but the
        # runtime accepts str values (which is all we ship here).
        await self._redis.xadd(stream, full_fields, maxlen=_STREAM_MAXLEN, approximate=True)  # type: ignore[arg-type]
        await self._redis.zadd(pending_key, {request_id: now_ms})
        return request_id

    async def read_responses(
        self,
        broker: str,
        account_id: str,
        count: int = 10,
        block_ms: int = 1000,
    ) -> list[Any]:
        """Read pending response messages for one (broker, account_id).

        Returns the raw redis-py XREADGROUP shape — caller iterates. Empty
        list when block elapses with no new messages. ``block_ms`` is
        configurable so unit tests can pass a small value (or 0 for
        non-blocking) without hanging.
        """
        _validate_broker(broker)
        _validate_account_id(account_id)
        stream = f"resp_stream:{broker}:{account_id}"
        result = await self._redis.xreadgroup(
            groupname="server",
            consumername="server",
            streams={stream: ">"},
            count=count,
            block=block_ms,
        )
        return result if result is not None else []

    async def read_events(
        self,
        broker: str,
        account_id: str,
        count: int = 10,
        block_ms: int = 1000,
    ) -> list[Any]:
        """Read pending event messages (unsolicited closes) for one account."""
        _validate_broker(broker)
        _validate_account_id(account_id)
        stream = f"event_stream:{broker}:{account_id}"
        result = await self._redis.xreadgroup(
            groupname="server",
            consumername="server",
            streams={stream: ">"},
            count=count,
            block=block_ms,
        )
        return result if result is not None else []

    async def ack(self, stream: str, group: str, msg_id: str) -> None:
        """XACK a single message id. Idempotent on the Redis side."""
        await self._redis.xack(stream, group, msg_id)

    # ----- Pending command tracking (docs §7) -----

    async def remove_pending(self, broker: str, account_id: str, request_id: str) -> None:
        """ZREM the request_id from the pending zset."""
        _validate_broker(broker)
        _validate_account_id(account_id)
        await self._redis.zrem(f"pending_cmds:{broker}:{account_id}", request_id)

    async def get_stuck_pending(
        self,
        broker: str,
        account_id: str,
        max_age_seconds: int,
    ) -> list[tuple[str, int]]:
        """Return ``[(request_id, age_ms), ...]`` older than max_age_seconds.

        Cutoff is exclusive (`> max_age_seconds`): a command exactly at the
        boundary is NOT considered stuck. Caller pages through to issue
        timeout responses.
        """
        _validate_broker(broker)
        _validate_account_id(account_id)
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - max_age_seconds * 1000
        # ``min='-inf'`` to '(cutoff_ms' (exclusive upper) → only "older than".
        raw = await self._redis.zrangebyscore(
            f"pending_cmds:{broker}:{account_id}",
            min=float("-inf"),
            max=f"({cutoff_ms}",
            withscores=True,
        )
        return [(rid, now_ms - int(score)) for rid, score in raw]

    async def get_all_account_pairs(self) -> list[tuple[str, str]]:
        """Return ``[("ftmo", acc), ("exness", acc), ...]`` for every account.

        Used by background loops that need to poll every (broker, account)
        independently (e.g. response/event reader fan-out).
        """
        out: list[tuple[str, str]] = []
        for broker in _ALLOWED_BROKERS:
            for acc in await self.get_all_account_ids(broker):
                out.append((broker, acc))
        return out

    # ----- Order CRUD (docs §7) -----

    async def create_order(self, order_id: str, fields: dict[str, str]) -> None:
        """Create a new order hash + add it to its status index in one pipeline.

        ``fields["status"]`` is required — the index entry depends on it.
        Callers should always include it (typical first status: 'pending').
        """
        if "status" not in fields:
            raise ValueError("create_order requires fields['status']")
        status = fields["status"]
        pipe = self._redis.pipeline()
        pipe.hset(
            f"order:{order_id}",
            mapping={k: str(v) for k, v in fields.items()},
        )
        pipe.sadd(f"orders:by_status:{status}", order_id)
        await pipe.execute()

    async def get_order(self, order_id: str) -> OrderHash | None:
        """Return the order hash or None if missing."""
        data = await self._redis.hgetall(f"order:{order_id}")  # type: ignore[misc]
        if not data:
            return None
        return _to_order_hash(data)

    async def update_order(
        self,
        order_id: str,
        patch: dict[str, str],
        old_status: str | None = None,
    ) -> bool:
        """Atomic patch + status-index swap, with optional CAS on status.

        - ``old_status=None``: unconditional patch. The Lua script still
          reads the current status itself before the swap so the old/new
          set keys are computed atomically (no client-side race window).
        - ``old_status="X"``: CAS — only apply when the order's current
          status matches ``X``. Returns False on CAS miss or when the
          order doesn't exist.

        Returns True on success, False on CAS miss or missing order.
        """
        new_status = patch.get("status", "")
        # Flatten patch into a flat field/value list for ARGV.
        patch_argv: list[str] = []
        for k, v in patch.items():
            patch_argv.append(k)
            patch_argv.append(str(v))

        cas_flag = "1" if old_status is not None else "0"
        result = await self._update_order_script(
            keys=[f"order:{order_id}"],
            args=[order_id, cas_flag, old_status or "", new_status, *patch_argv],
        )
        return int(result) == 1

    async def list_orders_by_status(self, status: str) -> list[OrderHash]:
        """Return all orders currently in the given status set.

        Order is unspecified (Set semantics); callers needing chronology
        should sort on ``created_at`` themselves.
        """
        ids = await self._redis.smembers(f"orders:by_status:{status}")  # type: ignore[misc]
        if not ids:
            return []
        out: list[OrderHash] = []
        for oid in ids:
            data = await self._redis.hgetall(f"order:{oid}")  # type: ignore[misc]
            if data:
                out.append(_to_order_hash(data))
        return out

    async def list_open_orders_by_account(
        self,
        broker: Broker,
        account_id: str,
    ) -> list[OrderHash]:
        """Return orders whose primary leg is still open on
        ``({broker}, {account_id})``.

        "Open" means ``status in {pending, filled}`` — the status the
        FTMO leg can be in BEFORE the close event lands. Used by the
        step-3.7 reconcile flow: for each Redis-open order, check
        whether the matching positionId / orderId is still present in
        ``ProtoOAReconcileRes``. If not, the order closed during the
        offline window and we dispatch ``fetch_close_history``.

        Filter is applied client-side (per-account membership of the
        global ``orders:by_status:*`` sets) — no SCAN, no KEYS. The
        cardinality of open orders per account is small (operator
        rarely runs >50 simultaneous hedges), so the linear scan is
        fine. If Phase 4+ ever sees this dominate latency, add a
        per-account index.
        """
        _validate_broker(broker)
        _validate_account_id(account_id)
        account_field = "ftmo_account_id" if broker == "ftmo" else "exness_account_id"
        out: list[OrderHash] = []
        for status in ("pending", "filled"):
            for order in await self.list_orders_by_status(status):
                if order.get(account_field) == account_id:
                    out.append(order)
        return out

    async def list_closed_orders(self, limit: int, offset: int) -> list[OrderHash]:
        """Page the closed-history ZSET, newest first.

        ``limit=0`` returns []. Negative inputs raise ValueError to keep
        callers honest about pagination math.
        """
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        if limit == 0:
            return []
        start = offset
        stop = offset + limit - 1
        ids = await self._redis.zrevrange("orders:closed_history", start, stop)
        if not ids:
            return []
        out: list[OrderHash] = []
        for oid in ids:
            data = await self._redis.hgetall(f"order:{oid}")  # type: ignore[misc]
            if data:
                out.append(_to_order_hash(data))
        return out

    async def add_to_closed_history(self, order_id: str, closed_at_ms: int) -> None:
        """Append an order_id to the closed-history ZSET with a timestamp score."""
        await self._redis.zadd("orders:closed_history", {order_id: closed_at_ms})

    # ----- Side indices (request_id / broker_order_id → order_id) -----
    #
    # ``order:{id}`` doesn't carry request_id and the broker-side ids land
    # asynchronously after fill, so we maintain narrow side indices that
    # callers populate from the appropriate handler. Doc/06 doesn't pin a
    # naming convention — these names are ours and live alongside the
    # broker_order_id flow handlers we'll add in step 3.7.

    async def link_request_to_order(
        self,
        request_id: str,
        order_id: str,
        ttl_seconds: int | None = _REQUEST_ID_INDEX_TTL_SECONDS,
    ) -> None:
        """Index ``request_id_to_order:{request_id}`` → order_id.

        Callers (order_service.create_hedge_order, modify_sl_tp, close)
        invoke this immediately after ``push_command`` so the eventual
        response can be routed back. TTL defaults to 24h — a stuck command
        will time out and clean up itself rather than linger.
        """
        key = f"request_id_to_order:{request_id}"
        if ttl_seconds is None:
            await self._redis.set(key, order_id)
        else:
            await self._redis.setex(key, ttl_seconds, order_id)

    async def find_order_by_request_id(self, request_id: str) -> str | None:
        """Return the order_id linked to ``request_id`` or None."""
        val = await self._redis.get(f"request_id_to_order:{request_id}")
        return val if val else None

    async def link_broker_order_id(
        self,
        leg: LegPrefix,
        broker_order_id: str,
        order_id: str,
    ) -> None:
        """Index ``{leg}_broker_order_id_to_order:{broker_order_id}`` → order_id.

        ``leg`` is "p" (primary / FTMO / cTrader positionId) or "s"
        (secondary / Exness / MT5 ticket). Populated by response_handler
        on fill so unsolicited close events can resolve back to our hedge
        order without scanning all open orders.
        """
        if leg not in ("p", "s"):
            raise ValueError(f"leg must be 'p' or 's', got {leg!r}")
        await self._redis.set(f"{leg}_broker_order_id_to_order:{broker_order_id}", order_id)

    async def find_order_by_p_broker_order_id(self, broker_order_id: str) -> OrderHash | None:
        """Resolve a primary-leg cTrader positionId back to the full order hash."""
        oid = await self._redis.get(f"p_broker_order_id_to_order:{broker_order_id}")
        if not oid:
            return None
        return await self.get_order(oid)

    async def find_order_by_s_broker_order_id(self, broker_order_id: str) -> OrderHash | None:
        """Resolve a secondary-leg MT5 ticket back to the full order hash."""
        oid = await self._redis.get(f"s_broker_order_id_to_order:{broker_order_id}")
        if not oid:
            return None
        return await self.get_order(oid)

    async def find_order_id_by_p_broker_order_id(self, broker_order_id: str) -> str | None:
        """Same as ``find_order_by_p_broker_order_id`` but returns the
        order_id string directly (saves one HGETALL when the caller
        only needs the id, e.g. ``event_handler`` resolving a
        ``position_closed`` event to dispatch updates).
        """
        oid = await self._redis.get(f"p_broker_order_id_to_order:{broker_order_id}")
        return oid if oid else None

    async def unlink_broker_order_id(self, leg: LegPrefix, broker_order_id: str) -> None:
        """Drop the ``{leg}_broker_order_id_to_order:{id}`` side-index entry.

        Step 3.7 uses this for two cases:
          - ``pending_filled``: drop the old orderId mapping and create
            the new positionId mapping (the broker_order_id swap per
            D-061).
          - ``order_cancelled`` for an order we own: drop the index so
            a future event with the same id doesn't accidentally route
            to a stale order row.
        """
        if leg not in ("p", "s"):
            raise ValueError(f"leg must be 'p' or 's', got {leg!r}")
        await self._redis.delete(f"{leg}_broker_order_id_to_order:{broker_order_id}")

    # ----- Position P&L cache + snapshots (docs §8) -----

    async def set_position_pnl(self, order_id: str, snapshot: dict[str, Any]) -> None:
        """Store the latest P&L snapshot under ``position:{order_id}`` with TTL.

        Encoded as JSON since the schema (mixed numeric + string fields)
        doesn't fit a flat hash neatly. TTL 600s — see docs/06 §11.
        """
        await self._redis.setex(
            f"position:{order_id}",
            _POSITION_PNL_TTL_SECONDS,
            json.dumps(snapshot),
        )

    async def get_position_pnl(self, order_id: str) -> dict[str, Any] | None:
        """Return the cached P&L snapshot or None on miss/expired."""
        raw = await self._redis.get(f"position:{order_id}")
        if raw is None:
            return None
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, dict) else None

    async def set_position_cache(
        self,
        order_id: str,
        fields: dict[str, str],
        ttl_seconds: int = _POSITION_PNL_TTL_SECONDS,
    ) -> None:
        """Step 3.8 unrealized-P&L snapshot.

        Stored as a HASH under ``position_cache:{order_id}`` so the
        frontend (and the future positions API in step 3.9) can do a
        single HGETALL without a JSON decode. Sits alongside the
        step-3.1 JSON ``position:{order_id}`` cache which is reserved
        for the realized-history per-order snapshot model.

        TTL defaults to 600s — same as ``set_position_pnl`` per docs
        §11. A position last polled >10 minutes ago is considered
        stale and the next position_tracker cycle refreshes it.
        """
        key = f"position_cache:{order_id}"
        # HSET dict-arg type is invariant; cast to satisfy strict mypy.
        await self._redis.hset(  # type: ignore[misc]
            key,
            mapping={k: str(v) for k, v in fields.items()},
        )
        await self._redis.expire(key, ttl_seconds)

    async def get_position_cache(self, order_id: str) -> dict[str, str] | None:
        """HGETALL ``position_cache:{order_id}`` or None on miss/expired."""
        data = await self._redis.hgetall(f"position_cache:{order_id}")  # type: ignore[misc]
        if not data:
            return None
        return dict(data)

    async def add_snapshot(self, order_id: str, ts_ms: int, pnl: float) -> None:
        """Append a P&L data point to the order's history ZSET.

        Score = ts_ms (epoch). Member = JSON-encoded ``{ts, pnl_usd}``
        because ZSETs reject duplicate members — encoding ts inside the
        JSON makes each tick unique even if pnl rounds to the same value.
        TTL refreshed on every add (lazy refresh per docs §11).
        """
        member = json.dumps({"ts": ts_ms, "pnl_usd": pnl})
        key = f"order:{order_id}:snaps"
        await self._redis.zadd(key, {member: ts_ms})
        await self._redis.expire(key, _SNAPSHOT_TTL_SECONDS)

    async def get_snapshots(self, order_id: str) -> list[tuple[int, float]]:
        """Return ``[(ts_ms, pnl_usd), ...]`` ordered by score ascending."""
        raw = await self._redis.zrange(f"order:{order_id}:snaps", 0, -1, withscores=True)
        out: list[tuple[int, float]] = []
        for member, score in raw:
            try:
                decoded = json.loads(member)
                pnl = float(decoded.get("pnl_usd", 0.0))
            except (ValueError, TypeError):
                continue
            out.append((int(score), pnl))
        return out

    # ----- Heartbeat & account info -----

    async def get_client_status(self, broker: str, account_id: str) -> str:
        """Return ``"online"`` if heartbeat key exists, else ``"offline"``.

        Heartbeat key has a 30s TTL (see docs §4); client is expected to
        refresh every 10s. Three missed → offline.
        """
        _validate_broker(broker)
        _validate_account_id(account_id)
        exists = await self._redis.exists(f"client:{broker}:{account_id}")
        return "online" if exists else "offline"

    async def get_all_client_statuses(self) -> dict[str, str]:
        """Return ``{ "ftmo:acc_001": "online", "exness:acc_001": "offline", ... }``.

        Composite key keeps it flat so the frontend can render a single
        AccountStatus bar without nested decoding.
        """
        result: dict[str, str] = {}
        for broker, acc in await self.get_all_account_pairs():
            result[f"{broker}:{acc}"] = await self.get_client_status(broker, acc)
        return result

    async def get_account_info(self, broker: str, account_id: str) -> dict[str, str] | None:
        """Return ``account:{broker}:{account_id}`` HASH (balance/equity etc.) or None."""
        _validate_broker(broker)
        _validate_account_id(account_id)
        data = await self._redis.hgetall(f"account:{broker}:{account_id}")  # type: ignore[misc]
        if not data:
            return None
        return dict(data)

    # ----- Account management (docs §4) -----

    async def add_account(
        self,
        broker: str,
        account_id: str,
        name: str,
        enabled: bool = True,
    ) -> None:
        """Register an account: SADD set + HSET account_meta in one pipeline."""
        _validate_broker(broker)
        _validate_account_id(account_id)
        now_ms = int(time.time() * 1000)
        pipe = self._redis.pipeline()
        pipe.sadd(f"accounts:{broker}", account_id)
        pipe.hset(
            f"account_meta:{broker}:{account_id}",
            mapping={
                "name": name,
                "created_at": str(now_ms),
                "enabled": "true" if enabled else "false",
            },
        )
        await pipe.execute()

    async def remove_account(self, broker: str, account_id: str) -> None:
        """Unregister an account.

        Drops: ``accounts:{broker}`` membership, ``account_meta:*``,
        ``account:*`` (balance/equity), and ``client:*`` (heartbeat).
        Does NOT touch ``order:*`` rows that reference this account —
        history is preserved; out of scope here.
        """
        _validate_broker(broker)
        _validate_account_id(account_id)
        pipe = self._redis.pipeline()
        pipe.srem(f"accounts:{broker}", account_id)
        pipe.delete(f"account_meta:{broker}:{account_id}")
        pipe.delete(f"account:{broker}:{account_id}")
        pipe.delete(f"client:{broker}:{account_id}")
        await pipe.execute()

    async def get_all_account_ids(self, broker: str) -> list[str]:
        """Return all account_ids for a broker, sorted asc.

        Sorted output keeps tests deterministic and gives a stable order
        for any UI listing. Empty list when no accounts registered.
        """
        _validate_broker(broker)
        members = await self._redis.smembers(f"accounts:{broker}")  # type: ignore[misc]
        return sorted(members)

    async def get_account_meta(self, broker: str, account_id: str) -> dict[str, str] | None:
        """Return account meta (name, created_at, enabled) or None if missing."""
        _validate_broker(broker)
        _validate_account_id(account_id)
        data = await self._redis.hgetall(f"account_meta:{broker}:{account_id}")  # type: ignore[misc]
        if not data:
            return None
        return dict(data)

    async def get_all_accounts_with_status(self) -> list[dict[str, str]]:
        """Return every registered account (ftmo first, exness after) with its
        meta + heartbeat status + balance / equity snapshot in one call.

        Used by step 3.12's ``GET /api/accounts`` REST endpoint and the
        ``account_status_loop`` broadcast — both consumers want the
        same shape, so the merge happens once here. Each entry is a
        flat ``dict[str, str]`` (Redis HASH-string convention) with:

            broker, account_id, name, enabled, status,
            balance_raw, equity_raw, margin_raw, free_margin_raw,
            currency, money_digits

        ``status`` is computed from two sources, in priority order:

          1. ``account_meta:{broker}:{id}.enabled == "false"`` → ``"disabled"``
             (operator-side override; takes precedence over heartbeat).
          2. ``client:{broker}:{id}`` key existence → ``"online"`` / ``"offline"``
             (heartbeat key has a 30s TTL; client refreshes every 10s).

        ``balance_raw`` etc. are the raw int-as-string values written by
        the FTMO client's ``account_info_loop`` (step 3.5) — they are
        ``money_digits``-scaled and must be divided at the render
        boundary (D-108). Defaults: ``"0"`` for the four money fields,
        ``"USD"`` for currency, ``"2"`` for money_digits, so a brand-new
        account whose first ``account_info_loop`` cycle hasn't run yet
        still renders sensibly in the UI.

        Sort order: ``("ftmo", "exness")`` tuple iteration → ftmo block
        first, exness block second; ``get_all_account_ids`` returns each
        block already sorted by account_id asc, so the result is
        deterministic for tests + stable for the UI.
        """
        result: list[dict[str, str]] = []
        for broker in ("ftmo", "exness"):
            account_ids = await self.get_all_account_ids(broker)
            for acc_id in account_ids:
                meta = await self.get_account_meta(broker, acc_id)
                if meta is None:
                    # SET membership without a meta hash shouldn't happen
                    # in practice (``add_account`` writes both in one
                    # pipeline) but skipping rather than failing keeps
                    # the loop resilient to half-rolled-back add_account.
                    continue
                enabled = meta.get("enabled", "true").lower() == "true"
                if not enabled:
                    status = "disabled"
                else:
                    status = await self.get_client_status(broker, acc_id)
                info = await self.get_account_info(broker, acc_id) or {}
                result.append(
                    {
                        "broker": broker,
                        "account_id": acc_id,
                        "name": meta.get("name", ""),
                        "enabled": "true" if enabled else "false",
                        "status": status,
                        "balance_raw": info.get("balance", "0"),
                        "equity_raw": info.get("equity", "0"),
                        "margin_raw": info.get("margin", "0"),
                        "free_margin_raw": info.get("free_margin", "0"),
                        "currency": info.get("currency", "USD"),
                        "money_digits": info.get("money_digits", "2"),
                    }
                )
        return result

    # ----- Settings (docs §3) -----

    async def get_settings(self) -> dict[str, str]:
        """Return ``app:settings`` HASH or ``{}`` when missing.

        We never seed defaults here — bootstrap is owned by step 3.2's
        init script. Returning an empty dict lets callers reason about
        defaults explicitly with their own merge logic.
        """
        data = await self._redis.hgetall("app:settings")  # type: ignore[misc]
        return dict(data) if data else {}

    async def patch_settings(self, patch: dict[str, str]) -> dict[str, str]:
        """HSET the supplied fields into ``app:settings`` and return the full hash."""
        if patch:
            await self._redis.hset(  # type: ignore[misc]
                "app:settings",
                mapping={k: str(v) for k, v in patch.items()},
            )
        return await self.get_settings()


def _to_order_hash(data: dict[str, str]) -> OrderHash:
    """Convert a raw HGETALL dict into the OrderHash TypedDict.

    TypedDict has no runtime check beyond mypy, so this is a thin cast —
    we trust HGETALL returns the schema we wrote in via create_order /
    update_order. Wrapping it in a helper centralizes the cast for mypy
    and gives us one place to add validation later if needed.
    """
    return cast(OrderHash, dict(data))


def get_redis_service() -> RedisService:
    """FastAPI dependency: build a service over the shared Redis pool."""
    return RedisService(get_redis())
