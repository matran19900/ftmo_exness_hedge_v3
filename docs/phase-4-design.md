# Phase 4 — Cascade Close + Telegram Alerts Design Specification

**Status**: DESIGN DOC (authoritative for Phase 4 steps 4.1 → 4.12 implementation).
**Audience**: Engineering (server, ftmo-client, exness-client, web), CTO, CEO operator.
**Authored**: 2026-05-13 (step 4.0).
**Scope**: SPEC ONLY — no production code, no migrations, no schema changes are produced by step 4.0. Subsequent steps materialize the spec.
**Source base**: branch `step/4.0-cascade-and-alerts-design-doc` cut from tag `phase-3-complete` (commit `0b469cb`).
**Cross-doc references**: `docs/MASTER_PLAN_v2.md` §5, `docs/DECISIONS.md` D-047/D-070/D-072/D-086/D-088/D-090/D-118, `docs/12-business-rules.md` §7 G1+G5, `docs/ctrader-execution-events.md` (D-069 pattern), `docs/05-redis-protocol.md` §14, `docs/06-data-models.md` §15 OrderHash, `docs/07-server-services.md` §5.

> Throughout this document, **primary leg** ≡ FTMO leg (cTrader Open API) and **secondary leg** ≡ Exness leg (MT5 Python lib). The convention is fixed by R5 in `docs/12-business-rules.md`: secondary opens only after primary fills, never the reverse. Cascade close is direction-symmetric — the **first leg to terminate** triggers cascade on the **other leg**, regardless of which broker fired first.

---

## §0. Document conventions and scope guards

### §0.1 Document conventions

- **Decision labels**: New decisions emerging from this spec are labelled `D-4.0-<n>` until step 4.11 docs-sync promotes them to canonical `D-XXX` numbers in `docs/DECISIONS.md`.
- **Rule labels**: `R` = invariant (rule that must hold); `G` = edge case (situation the system must survive). Existing R/G IDs are reused; new edge cases discovered in step 4.0 are prefixed `G-4.0-<n>`.
- **Code references**: When this spec names a file path + approximate line number (`file.py:NNN`), the line number is the spec's *target* — actual implementation steps may land within ±20 lines. Steps 4.5/4.6/4.11 reconcile the exact lines in their self-check.
- **Time semantics**: All Redis stream IDs, timestamps, and TTLs use **server clock** (Linux devcontainer or Windows Server 2022 — Phase 5 deploy). Client-emitted events carry `event_ts_ms` from broker callback time when available.
- **Broker name strings**: `"ftmo"` and `"exness"` (lowercase, no separators). These names are wire constants in Redis stream keys and consumer-group names — `Broker` Literal type in `redis_service.py:_ALLOWED_BROKERS`.

### §0.2 What step 4.0 produces

Exactly two new files under `docs/`:

1. `docs/phase-4-design.md` (this file).
2. `docs/mt5-execution-events.md` (skeleton — D-069 pattern preview; populated mid-Phase-4 by steps 4.1/4.2/4.5/4.6 when MT5 quirks land).

### §0.3 What step 4.0 explicitly does NOT produce

- No new source files under `server/`, `apps/`, `web/`, or `shared/`.
- No edits to existing `docs/*.md` other than the two new files.
- No changes to `.env`, `.env.example`, `scripts/notify_telegram.sh`, `pyproject.toml`, `package.json`, `vite.config.ts`, or any IDE/runtime configuration.
- No Redis schema migrations; no API surface changes; no UI mockup artefacts.
- No test files.

Verification: `git diff --stat HEAD~1` after the step-4.0 commit must show **exactly two** added files, both under `docs/`. Any other delta is a step-4.0 bug.

### §0.4 Step numbering reconciliation

The spec body refers to future steps **4.1 through 4.12**. `docs/MASTER_PLAN_v2.md` §5 currently enumerates **4.1 through 4.11**, with step 4.11 being docs sync. Step 4.0 anticipates inserting a dedicated step for `alert_service.py` and Telegram wiring between current 4.10 (web hedge display) and 4.11 (docs sync), shifting docs sync to **4.12**. The renumber is performed by the step that introduces the alert service (proposed: step 4.11). Until then, references in this spec to "step 4.11 wires alert backend" and "step 4.12 docs sync" are forward-looking and will be reconciled in the same step that lands the renumber.

This delta is recorded as **D-4.0-1** at the foot of §3.

---

## §1. Cascade Close Architecture

### §1.A Trigger paths matrix (5 paths)

A "cascade trigger" is any signal that causes one leg of a 2-leg hedge order to transition toward `closed`. The cascade close subsystem must:

1. **Detect** the closing leg (the **primary trigger**) from one of five sources below.
2. **Acquire ownership** of the cascade via `cascade_lock:{order_id}` (§1.B).
3. **Drive** the **secondary close command** for the other leg.
4. **Update** the order hash and status indices atomically (Lua CAS on `s_status` or `p_status`).
5. **Broadcast** a `cascade_triggered` event on the `orders` WS channel.
6. **Dispatch** the appropriate Telegram alert (§2).

The five paths are independent ingress channels. Two or more can fire on the same `order_id` within the same millisecond (e.g., operator clicks Close FTMO at the exact instant SL hits). The cascade lock resolves the race.

| Path | Trigger source | Detection mechanism | Typical detection latency | Race risk vs other paths | Example scenario |
|---|---|---|---|---|---|
| **A** | UI click "Close FTMO" in PositionList | REST `DELETE /api/orders/{order_id}/legs/primary` → server pushes `close` cmd on `cmd_stream:ftmo:{account}` → response_handler picks up `close_response` and observes `p_status: closed` | <500 ms (HTTP RTT + cmd push) | A vs D (SL hit at same instant); A vs C (operator double-action) | Operator decides to take profit manually on FTMO leg. |
| **B** | UI click "Close Exness" in PositionList | REST `DELETE /api/orders/{order_id}/legs/secondary` → server pushes `close` cmd on `cmd_stream:exness:{account}` → response_handler picks up `close_response` and observes `s_status: closed` | <500 ms (HTTP RTT + cmd push) | B vs E (manual MT5 close at same instant); B vs C (operator double-action) | Operator decides to take profit manually on Exness leg. |
| **C** | Manual close in cTrader UI / mobile app / cTrader Desktop | FTMO client publishes `position_closed` event on `event_stream:ftmo:{account}` (unsolicited, no `request_id` in cmd correlation — D-070) → event_handler picks up | <2 s (cTrader push latency + event_handler XREADGROUP cycle) | C vs D (manual close at SL price); C vs A (operator double-action across UI and cTrader) | Operator opens cTrader Web during commute, closes FTMO leg from phone. |
| **D** | SL / TP hit on FTMO leg (broker auto-close) | FTMO client publishes `position_closed` event with `close_reason ∈ {sl, tp, stopout}` (D-071/D-074) on `event_stream:ftmo:{account}` → event_handler picks up | <2 s (broker push latency + event_handler XREADGROUP cycle) | D vs A (operator manual close at SL price); D vs E (correlated broker liquidations during news) | EURUSD breaks through SL during NFP release; FTMO closes leg automatically. |
| **E** | Manual close on MT5 terminal / Exness broker stopout / margin call | Exness client position monitor (2 s poll, D-090a spec) detects positionId absent from `positions_get()` snapshot → reconstructs close via deal history → publishes `position_closed_external` event on `event_stream:exness:{account}` → event_handler picks up | ≤3 s (max 2 s poll + 1 s deal-history fetch) | E vs B (operator UI vs operator MT5 click); E vs D (correlated liquidations) | Operator closes Exness leg from MT5 mobile app; or Exness account stopout fires during liquidity gap. |

#### §1.A.1 Path → handler routing decision table

The cascade entry point depends on which handler picks up the trigger first. There are exactly two server handlers per broker·account (D-086): `response_handler_loop` (consumes `resp_stream:{broker}:{account_id}`) and `event_handler_loop` (consumes `event_stream:{broker}:{account_id}`).

| Path | Server handler | Trigger payload | Cascade target |
|---|---|---|---|
| A | `response_handler:ftmo` | `close_response` with `status=ok`, includes `position_id` and `close_price` | secondary (Exness) |
| B | `response_handler:exness` | `close_response` with `status=ok`, includes `position_id` and `close_price` | primary (FTMO) |
| C | `event_handler:ftmo` | `position_closed` with `close_reason=manual` | secondary (Exness) |
| D | `event_handler:ftmo` | `position_closed` with `close_reason ∈ {sl, tp, stopout}` | secondary (Exness) |
| E | `event_handler:exness` | `position_closed_external` with `close_reason ∈ {manual, stopout, unknown}` | primary (FTMO) |

The cascade decision logic is identical across paths once the trigger is normalized into a tuple `(order_id, trigger_broker, trigger_leg, close_reason, close_price, close_ts_ms, request_id_or_event_id)`. Subsequent sections refer to this tuple as the **cascade trigger context** (CTC).

#### §1.A.2 Anti-patterns rejected

- **Polling**: We never poll Redis to detect closes. All cascade work is event-driven via `response_handler_loop` + `event_handler_loop` (D-086). The 2 s MT5 position monitor is the only poll in the system, and it exists because the MT5 Python lib does not expose a push API (D-118-equivalent for MT5, finalized as D-4.0-2 in §3).
- **REST → REST chaining**: We never re-enter `DELETE /api/orders/.../legs/...` from within a handler to trigger the other leg's close. Cascade commands are pushed directly via `redis.push_command(broker, account, fields)`. REST endpoints are operator-facing only.
- **Broker self-close coordination**: We do **not** wait for FTMO to confirm cascade close before starting Exness close (and vice versa). Cascade is fire-and-track: the lock owner pushes the secondary cmd within ~10 ms of acquiring the lock; the actual close completion is observed by the **other** handler in due course.

### §1.B `cascade_lock` Lua CAS specification

#### §1.B.1 Key design

```
Key:       cascade_lock:{order_id}
Type:      STRING
Value:     "{trigger_broker}:{request_id_or_event_id}:{ts_ms}"
TTL:       30 seconds (initial)
Encoding:  ASCII; trigger_broker ∈ {ftmo, exness}; request_id is the cmd
           XADD message ID for response-driven paths (A/B) or the event
           stream ID for event-driven paths (C/D/E).
```

**Why 30 s TTL?** Cascade close — including retry windows — must complete within 30 s or it is escalated to the **`leg_orphaned`** alert (type 1.5 in §2.A) and the order moves to the `error` state (D-090 row 9). 30 s is also long enough that a brief Redis hiccup during the handler's secondary push does not cause the lock to be re-acquired by a redundant trigger.

**Why STRING + SET NX EX, not a HASH or a Lua-managed `IF NOT EXISTS`?** Redis' native `SET key value NX EX seconds` is atomic at the server, requires no script registration, and survives Redis restart with TTL preserved. The Lua wrapper in §1.B.2 is needed not for atomicity of `SET NX EX` itself but to bundle the lock acquisition with an **order-hash status check** (we do not want to acquire the lock if `status` is already `closed` — the cascade is a no-op).

#### §1.B.2 Lua script: `acquire_cascade_lock.lua`

```lua
-- KEYS:
--   [1] cascade_lock:{order_id}
--   [2] order:{order_id}
--
-- ARGV:
--   [1] order_id
--   [2] trigger_broker  (ftmo | exness)
--   [3] request_id      (cmd msg id for paths A/B, event stream id for C/D/E)
--   [4] ts_ms           (server clock at lock attempt)
--   [5] ttl_seconds     (default 30)
--
-- Returns (Lua table-encoded):
--   {1, owner_value}              -- acquired (owner duty)
--   {0, existing_value}           -- already held (loser duty)
--   {-1, "order_missing"}         -- order hash missing — caller logs ERROR
--   {-2, "already_terminal"}      -- order is already closed/rejected/error;
--                                    cascade is a no-op (caller logs INFO)
--
-- Cluster note: keys share a hash tag — both contain {order_id}. To make this
-- Cluster-safe we use literal hash tags in the key naming convention:
--     cascade_lock:{order_id}      → slot = CRC16("order_id")
--     order:{order_id}             → slot = CRC16("order_id")
-- Project runs single-instance Redis (D-006) so this is precautionary.

local lock_key  = KEYS[1]
local order_key = KEYS[2]
local order_id  = ARGV[1]
local broker    = ARGV[2]
local req_id    = ARGV[3]
local ts_ms     = ARGV[4]
local ttl       = tonumber(ARGV[5]) or 30

-- 1. Order must exist.
if redis.call('EXISTS', order_key) == 0 then
  return {-1, 'order_missing'}
end

-- 2. Order must not already be terminal.
local status = redis.call('HGET', order_key, 'status')
if status == 'closed' or status == 'rejected' or status == 'error' then
  return {-2, 'already_terminal'}
end

-- 3. Attempt lock acquisition (atomic).
local owner_value = broker .. ':' .. req_id .. ':' .. ts_ms
local ok = redis.call('SET', lock_key, owner_value, 'NX', 'EX', ttl)
if ok then
  return {1, owner_value}
end

-- 4. Lock already held. Return current owner for caller log.
local existing = redis.call('GET', lock_key) or ''
return {0, existing}
```

#### §1.B.3 Python wrapper signature

Lives in `server/app/services/redis_service.py` alongside `update_order_with_cas` (the §10 reference for this design — same `register_script` pattern):

```python
class CascadeLockOutcome(NamedTuple):
    code: Literal["acquired", "held", "missing", "terminal"]
    owner_value: str           # populated for "acquired" or "held"; "" otherwise
    detail: str                # raw payload from Lua return for "missing" or "terminal"

class RedisService:
    # ... (existing attrs)
    _acquire_cascade_lock_script: AsyncScript  # registered in __init__

    async def acquire_cascade_lock(
        self,
        order_id: str,
        trigger_broker: Literal["ftmo", "exness"],
        request_id: str,
        ts_ms: int,
        ttl_seconds: int = 30,
    ) -> CascadeLockOutcome:
        """Atomically attempt to acquire the cascade lock for {order_id}.

        Returns a CascadeLockOutcome with:
          - code='acquired' + owner_value=<own>  → caller is the OWNER and
            must drive secondary close + status transitions + alert dispatch.
          - code='held' + owner_value=<other>     → caller is a LOSER and
            must drop with INFO log (ACK upstream stream entry, continue).
          - code='missing'                        → order hash is gone; caller
            logs ERROR and ACKs the upstream entry to avoid retry storm.
          - code='terminal'                       → order is already closed/
            rejected/error; cascade is a no-op (INFO log, ACK).
        """
        ...

    async def release_cascade_lock(self, order_id: str) -> bool:
        """Delete the lock key.

        Called by the owner after both legs are observed in a terminal
        state (closed/error). Returns True if the key was present and
        deleted, False otherwise. The lock is also auto-released by the
        30 s TTL — release_cascade_lock is best-effort cleanup.
        """
        ...

    async def extend_cascade_lock(self, order_id: str, ttl_seconds: int = 30) -> bool:
        """Reset the lock TTL while a long-running secondary retry is in
        progress. Used by hedge_service._cascade_secondary_retry_loop
        between retry attempts so the lock survives the full retry window
        even when retries take ~3.5 s (0.5 + 1 + 2 s)."""
        ...
```

#### §1.B.4 Owner duties

When `acquire_cascade_lock` returns `code='acquired'`, the calling handler **owns** the cascade and must:

1. **Mark transient state**: Update `order:{order_id}` HASH via `update_order_with_cas`. The patch sets `status=half_closed` (or `error` if it is a `secondary_failed` cascade-via-orphan; see §1.E.6), updates the closed leg's `p_status`/`s_status` to `closed` (or `closing` if waiting for response), and writes the close metadata (`p_close_price` / `s_close_price`, `p_realized_pnl` / `s_realized_pnl`, etc.).
2. **Push the cascade command**: `redis.push_command(secondary_broker, secondary_account, {action: "close", position_id: ..., cascade_trigger: "true", origin_order_id: order_id})`. The `cascade_trigger` field is critical for §1.F.
3. **Broadcast `cascade_triggered`** on the `orders` WS channel with the CTC payload (operator UI shows the cascade-in-progress badge).
4. **Wait for secondary completion**: The owner does **not** block. The other broker's `response_handler` (path A/B) or `event_handler` (path C/D/E) will observe the secondary's close event and complete the order via `update_order_with_cas(... new_status='closed')`.
5. **On retry exhaustion**: If `hedge_service._cascade_secondary_retry_loop` exhausts 3 retries (0.5 / 1 / 2 s, mirroring G1 timing), the order transitions to `error` with `s_status=closing_failed` (or `p_status=closing_failed` for path E), Alert 1.5 fires (`leg_orphaned`), and the lock is released for operator manual recovery.
6. **Dispatch terminal alert**: When both legs are observed terminal, dispatch Alert 1 (`hedge_closed`) and release the lock.

#### §1.B.5 Loser duties

When `acquire_cascade_lock` returns `code='held'`:

1. **Log**: `INFO` level, structured payload `{event: 'cascade_lock_lost', order_id, my_trigger: <path>, my_request_id, current_owner: <owner_value>}`. The log line is the only artefact a loser produces — the cascade itself is unaffected.
2. **ACK the upstream stream entry**: The loser MUST still `XACK` its `resp_stream` / `event_stream` entry so it is not reprocessed. Skipping the ACK would cause the same trigger to re-fire on the next handler cycle (1 s later) and re-attempt lock acquisition (always failing while the owner is active), spamming the log.
3. **Skip all writes**: Do not touch `order:{order_id}`, do not push commands, do not broadcast.

Note: there is no "loser retries later" path. If the owner crashes mid-cascade, the lock TTL (30 s) expires and any future trigger on the same order re-attempts ownership.

#### §1.B.6 Re-entrancy edge: lock owner crash

If the owner crashes after acquiring the lock but before completing the secondary push:

1. The lock auto-releases at TTL=30 s.
2. The order is left in the transient state set in step §1.B.4 (1) — e.g., `status=half_closed`, `p_status=closed`, `s_status=filled`.
3. The closed primary leg's `position_closed` event has already been ACKed (the owner ACKed before the crash) — so the event_handler will not re-process it.
4. The secondary leg is still open at the broker. **The hedge is unbalanced.**

Detection: a background **`orphaned_order_sweep`** task (proposed for step 4.5, runs every 60 s) scans `orders:by_status:half_closed`. Any order whose `cascade_lock` key does not exist AND whose `updated_at` is >30 s ago is logged with WARN and Alert 1.5 (`leg_orphaned`) is dispatched. The operator manually closes the surviving leg via the UI.

Sweep is **not** an auto-retry of the cascade. We do not want a delayed cascade firing during low-liquidity / off-hours conditions when the operator may have already taken manual action.

### §1.C Expanded D-090 state machine

The Phase 3 `D-090` rule defines six base statuses: `pending`, `filled`, `closed`, `half_closed`, `rejected`, `error`. Phase 4 introduces **transient states** required to make atomic transitions safe under concurrent triggers.

#### §1.C.1 Status field hierarchy

Each `order:{order_id}` HASH carries three status-like fields:

- `status` — the **composed** status (the six-value enum). This is what `orders:by_status:*` indexes and what the UI displays. Mutated only via `update_order_with_cas` (Lua atomic).
- `p_status` — the **primary leg** status. Enum: `pending`, `filled`, `closing`, `closed`, `closing_failed`, `rejected`.
- `s_status` — the **secondary leg** status. Enum: `pending`, `pending_open`, `filled`, `closing`, `closed`, `closing_failed`, `rejected`, `secondary_failed`.

The composed `status` is **derived** from `(p_status, s_status)` per the table below. `update_order_with_cas` is the only place this derivation is computed; callers must pass both the leg patches AND the new `status` value together.

#### §1.C.2 Composition table

| # | composed `status` | `p_status` | `s_status` | Description | Valid next composed statuses |
|---|---|---|---|---|---|
| 1 | `pending` | `pending` | `pending` | Both legs requested, neither filled yet. | `filled`, `rejected`, `error` |
| 2 | *(transient — same composed `pending`)* | `filled` | `pending_open` | Primary fill OK, secondary push pending or in retry window (≤3.5 s). | `filled`, `secondary_failed`, `error` |
| 3 | `filled` | `filled` | `filled` | Both legs open. Steady state. | `half_closed` (primary), `half_closed` (secondary) |
| 4 | `secondary_failed` | `filled` | `secondary_failed` | Primary open; secondary failed all 3 retries. **LEG HỞ — operator-visible**. | `closed` (operator manual close of primary), `error` |
| 5 | `half_closed` | `closed` | `filled` | Primary closed (any path A/C/D), cascade dispatched, awaiting secondary close. | `closed`, `error` |
| 6 | `half_closed` | `filled` | `closed` | Secondary closed (path B/E), cascade dispatched, awaiting primary close. | `closed`, `error` |
| 7 | `closed` | `closed` | `closed` | Final terminal. Both legs closed, P&L finalized, Alert 1 dispatched. | *(terminal)* |
| 8 | `rejected` | `rejected` | `rejected` (or `pending` if primary rejected first) | Validation or broker-level rejection before any open. Phase 3 D-082 path. | *(terminal)* |
| 9 | `error` | (any) | (any) | Operator intervention required: orphaned leg, cascade timeout, inconsistent state. | *(operator-resolved → `closed`)* |

#### §1.C.3 Transient state `s_status=pending_open`

`pending_open` is distinct from `pending` because the cascade decision logic in path A/C/D needs to know whether the secondary leg has even been pushed to Exness yet.

| Phase 3 `s_status` | Phase 4 replacement | Meaning |
|---|---|---|
| `pending_phase_4` (D-083 placeholder) | `pending` | Order accepted, secondary leg not yet pushed. |
| *(N/A)* | `pending_open` | Secondary push attempted, awaiting `open_response` OR within retry window after a `secondary_failed` retry attempt. |
| *(N/A)* | `filled` | Secondary fill confirmed by `open_response`. |
| *(N/A)* | `secondary_failed` | All 3 retries exhausted. Composed `status=secondary_failed`. |

**Cascade interaction with `pending_open`**: If a path A/C/D trigger arrives while `s_status=pending_open` (i.e., primary is closing but secondary push is mid-flight), the owner:

1. Marks `s_status=cascade_cancel_pending` (NEW transient state — D-4.0-3).
2. Lets the in-flight secondary push complete (the `open_response` may still arrive).
3. On `open_response` with `status=ok`: immediately push secondary `close` (treat it as a freshly opened leg to cascade).
4. On `open_response` with `status=fail` (or timeout): mark `s_status=rejected`, composed `status=closed` (primary closed, secondary never opened — equivalent to half-closed cascade success).

This avoids the race where: (a) primary closes, (b) cascade owner reads `s_status=pending_open` and decides not to push a close, (c) secondary push lands and Exness leg opens, (d) leg is orphaned. The `cascade_cancel_pending` flag is the "stop, but if you've already started, also stop on the way back" signal.

#### §1.C.4 Transient state `p_status=closing` and `s_status=closing`

When the cascade owner pushes the secondary close cmd, it sets `s_status=closing` (mirror for primary). `closing` means "close cmd is on the wire; awaiting `close_response`". The composed `status` during this window is `half_closed`.

If the close fails (response with `status=fail` or 3-retry exhaustion), `s_status` transitions to `closing_failed` (or `p_status=closing_failed`), composed `status=error`, Alert 1.5 fires.

#### §1.C.5 `secondary_failed` is operator-visible

D-090 row 4 is the **leg hở** state — the system has a single open FTMO position with no Exness hedge, exposing the operator to one-sided market risk. This state is **never silent**:

- Composed `status=secondary_failed` (distinct, not `error`).
- Order remains in `orders:by_status:secondary_failed` SET (new index — D-4.0-4).
- UI PositionList row renders with red "LEG HỞ" badge and a "Retry Secondary" button (step 4.10 wiring).
- Alert 1.5 (`leg_orphaned`) is dispatched on entry (cooldown-gated per `order_id`).
- G1 hardening (Phase 5 backlog) may add an automated retry-on-reconnect path; Phase 4 expects manual operator action.

#### §1.C.6 Transition guards (Lua CAS contracts)

Each transition must be CAS-gated on the composed `status` AND optionally on the leg sub-status. The Lua script `update_order_with_cas` (existing) supports composed-status CAS only. Phase 4 may extend it to optionally CAS on `p_status` / `s_status` as well (proposed: `UPDATE_ORDER_LUA_V2` with optional `expected_p_status` and `expected_s_status` ARGV). D-4.0-5 captures this addition.

Until V2 lands, callers perform a **read-then-CAS** pattern: read the current `(p_status, s_status)` via `HGET`, validate, then call `update_order_with_cas` with the composed status CAS. The race window is small (sub-millisecond on local Redis) but non-zero; the cascade lock (§1.B) is the canonical guard against double-trigger writes, not the CAS.

### §1.D Volume formula lock

The secondary leg volume is computed from the primary leg volume plus a per-pair `risk_ratio` plus the contract-size delta between FTMO and Exness for the same underlying symbol.

#### §1.D.1 Inputs

| Field | Source | Type | Example |
|---|---|---|---|
| `primary_volume_lots` | Order form / API request | float | 0.01 |
| `ftmo_units_per_lot` | `symbol_config:{ftmo_symbol}` HASH, field `lot_size` (D-094) | int (FX standard 100,000) | 100000 |
| `exness_trade_contract_size` | `symbol_mapping_ftmo_exness.json`, entry's `exness.trade_contract_size` | int | 100000 (EURUSD m); 100 (some XAUUSD variants) |
| `pair.risk_ratio` | `pair:{pair_id}` HASH, field `risk_ratio` | float, range 0.1–2.0 typical | 1.0 |
| `exness_volume_step` | `symbol_mapping_ftmo_exness.json`, entry's `exness.volume_step` | float | 0.01 |
| `exness_volume_min` | same | float | 0.01 |
| `exness_volume_max` | same | float | 200.0 (broker dependent) |

#### §1.D.2 Formula

```python
# server/app/services/volume_service.py (step 4.5 — proposed)

def compute_secondary_volume(
    primary_volume_lots: float,
    ftmo_units_per_lot: int,
    exness_trade_contract_size: int,
    risk_ratio: float,
    exness_volume_step: float,
    exness_volume_min: float,
    exness_volume_max: float,
) -> float:
    raw = (
        primary_volume_lots
        * (ftmo_units_per_lot / exness_trade_contract_size)
        * risk_ratio
    )
    rounded = round_to_step(raw, exness_volume_step)
    if rounded < exness_volume_min:
        raise SecondaryVolumeTooSmall(
            primary=primary_volume_lots,
            computed=raw,
            rounded=rounded,
            exness_volume_min=exness_volume_min,
        )
    if rounded > exness_volume_max:
        raise SecondaryVolumeTooLarge(
            primary=primary_volume_lots,
            computed=raw,
            rounded=rounded,
            exness_volume_max=exness_volume_max,
        )
    return rounded


def round_to_step(value: float, step: float) -> float:
    """Round value DOWN to the nearest step (banker's rounding is wrong here —
    rounding UP could exceed broker max_volume; rounding DOWN could fall below
    min, which raises rather than silently mis-sizing)."""
    import math
    return math.floor(value / step) * step
```

#### §1.D.3 Worked example (CEO sample: EURUSD ↔ EURUSDm, BUY 0.01)

```
primary_volume_lots         = 0.01
ftmo_units_per_lot          = 100000   # FX standard
exness_trade_contract_size  = 100000   # EURUSDm at Exness
risk_ratio                  = 1.0
exness_volume_step          = 0.01
exness_volume_min           = 0.01
exness_volume_max           = 200.0

raw     = 0.01 × (100000 / 100000) × 1.0 = 0.01
rounded = floor(0.01 / 0.01) × 0.01 = 0.01

→ secondary leg: SELL EURUSDm 0.01 lots
```

#### §1.D.4 Edge cases

**E1 — `secondary_volume_lots == 0` after rounding**:

```
primary = 0.001 (FTMO accepts 0.001 lots on some symbols)
ftmo_units_per_lot = 100000, exness_contract_size = 1000  (rare; some CFD)
ratio = 1.0, step = 0.01
raw = 0.001 × 100 × 1.0 = 0.1
rounded = 0.10
→ OK

But on another mapping:
primary = 0.01, ratio = 0.05, step = 0.01
raw = 0.01 × 1 × 0.05 = 0.0005
rounded = floor(0.05) × 0.01 = 0.00
→ SecondaryVolumeTooSmall raised before primary is pushed.
```

Behavior: `OrderService.create_hedge_order` performs the volume calc in the **preflight validation pipeline** (D-081) and raises `OrderValidationError(error_code="secondary_volume_too_small")` with HTTP 400. The primary leg is NEVER pushed. UI surfaces the error inline on the form (step 4.10 wiring).

**E2 — `secondary_volume_lots > exness_max_volume`**:

```
primary = 50.0 (operator typo; FTMO accepts up to ~100 lots on some accounts)
contract delta = 1.0, ratio = 5.0  (operator misconfigured ratio)
raw = 250.0; max = 200.0
→ SecondaryVolumeTooLarge raised
```

Behavior: same as E1 — preflight validation, HTTP 400, primary NEVER pushed.

**E3 — `risk_ratio` invalid**:

The `pair:{pair_id}` HASH is created/updated via Settings UI step 4.8 (accounts/pairs CRUD). The CRUD layer validates `risk_ratio`:

- Must be a positive float.
- Range warning (UI level): `< 0.1` or `> 5.0` triggers a confirmation modal "are you sure?".
- Hard reject: `<= 0` or `> 100` → HTTP 422.

Phase 4 ships a single test pair `EURUSD ↔ EURUSDm` with `risk_ratio=1.0` (CEO confirmed). Multi-pair onboarding is Phase 5.

**E4 — `exness_trade_contract_size` missing from mapping**:

The `symbol_mapping_ftmo_exness.json` file ships with `exness.trade_contract_size` populated for all whitelisted symbols. If the lookup returns `None` (operator added a custom pair without updating the mapping), the OrderService raises `OrderValidationError(error_code="exness_symbol_mapping_missing")`. Step 4.5 includes a validation that all linked pairs have complete mapping entries on server startup; missing entries log ERROR and disable the pair (set `enabled=false`) until operator action.

**E5 — Rounding direction for non-integer-step ratios**:

We round **down** (toward zero). Rounding up could cause the secondary leg to exceed broker max_volume in edge cases (E2 boundary); rounding down can fall below min, which raises an explicit error rather than silently mis-sizing. The asymmetry is intentional: explicit rejection > silent under-hedge.

#### §1.D.5 Volume formula is locked

Once step 4.5 implements this formula, **changing the formula post-Phase-4-complete requires a new D-XXX decision and a documented migration**. Historical orders carry their computed `s_volume_lots` in the OrderHash and the formula is not re-applied retroactively.

### §1.E Five trigger paths × cascade_lock interaction trace

Each subsection traces end-to-end for one path: detection → lock acquisition → owner action → loser action (when applicable) → terminal state. The subsections share a common notation:

- `ts0` = trigger arrival at server.
- `ts0+N` = N milliseconds after `ts0`.
- Bullets prefixed with **▸** are owner duties; bullets prefixed with **▷** are loser duties.

#### §1.E.1 Path A — UI close FTMO leg

**Detection**:

1. `ts0`: Operator clicks "Close FTMO" in PositionList row for `order_id=ord_abc`.
2. `ts0+0`: Frontend sends `DELETE /api/orders/ord_abc/legs/primary` (FE step 4.10).
3. `ts0+15`: `OrderService.close_primary_leg(order_id)` validates `p_status ∈ {filled}` (D-082), reads `position_id` from `p_broker_order_id`.
4. `ts0+20`: `redis.push_command("ftmo", ftmo_account, {action: "close", position_id: ..., cascade_trigger: "false"})` → XADD on `cmd_stream:ftmo:{acc}`, returns `req_id=ts_seq` Redis stream message ID.
5. `ts0+25`: HTTP 202 Accepted returned with `{req_id, message: "close requested"}`. UI optimistically shows "Closing…" status.
6. `ts0+~500–1500`: FTMO client picks up cmd, executes `ProtoOAClosePositionReq`, receives ACCEPTED → FILLED 2-event sequence (D-062), publishes `close_response` on `resp_stream:ftmo:{acc}`.
7. `ts0+~1500–2000`: Server `response_handler:ftmo` reads `close_response` entry.

**Lock acquisition** (in `response_handler:ftmo._handle_close_response`):

```
ctc = (order_id="ord_abc", trigger_broker="ftmo", trigger_leg="primary",
       close_reason="manual", close_price=..., close_ts_ms=..., request_id=<resp_msg_id>)

outcome = await redis.acquire_cascade_lock(
    order_id="ord_abc",
    trigger_broker="ftmo",
    request_id=ctc.request_id,
    ts_ms=ctc.close_ts_ms,
    ttl_seconds=30,
)
```

**Owner branch** (`outcome.code == "acquired"`):

- **▸** Update order hash via Lua CAS (composed `status: filled → half_closed`; `p_status: filled → closed`; write `p_close_price`, `p_realized_pnl`, etc.; `s_status: filled → closing`).
- **▸** Push secondary close: `redis.push_command("exness", exness_account, {action: "close", position_id: s_broker_order_id, cascade_trigger: "true", origin_order_id: "ord_abc"})`.
- **▸** Broadcast `cascade_triggered` on `orders` WS channel with CTC.
- **▸** Start retry watcher: `asyncio.create_task(self._cascade_secondary_retry_watch("ord_abc", deadline=ts0+30000))`. The watcher polls for `s_status ∈ {closed, closing_failed}` and retries the close cmd on the schedule below.
- **▸** ACK the `close_response` stream entry.

**Loser branch** (`outcome.code == "held"`):

- **▷** Should not happen for path A unless a duplicate close response arrives (FTMO client retried internally — rare). Log INFO, ACK, drop.

**Cascade completion**:

- `ts0+~2000–4500`: Exness client `cmd_dispatcher` picks up close cmd, executes `mt5.Close()`, receives retcode 10009 (TRADE_RETCODE_DONE), publishes `close_response` on `resp_stream:exness:{acc}`.
- `ts0+~2000–4500`: `response_handler:exness._handle_close_response` reads response. Attempts `acquire_cascade_lock` — **gets `code='terminal'` because composed status was set to `half_closed` and now both legs are closing toward `closed`**. Actually, this is wrong: `half_closed` is not in the terminal list (`closed`, `rejected`, `error`). So the lock returns `code='held'` (the original owner has TTL ~28 s remaining). The response handler is a **loser** here.
- Wait — this means the close completion logic must run from the owner (path A) watcher, not from the path B response handler. **Correction**: the cascade owner's retry watcher polls for the secondary close completion and finalizes the order. The Exness response_handler still ACKs its `close_response` entry but does NOT compete for the lock. Lock check is purely advisory for it.

Refining the response_handler logic: it must distinguish between **operator-initiated close response** (lock should be acquired — path A/B owner) and **cascade-initiated close response** (lock is already held by the OTHER broker's handler — just update the order hash without competing for the lock). The discriminator is the `cascade_trigger` field on the originating cmd (§1.F).

**Restated cascade completion**:

- The Exness `response_handler` reads the close_response, reads the cmd context (via `request_id_to_order:{request_id}` index plus a new `cmd_metadata:{request_id}` lookup — D-4.0-6), sees `cascade_trigger="true"`, and:
  - **DOES NOT** attempt lock acquisition.
  - Updates `s_status: closing → closed`, writes `s_close_price` / `s_realized_pnl` / etc.
  - **Re-reads** the order to check if composed `status` should now flip to `closed` (both legs terminal): if `p_status == s_status == "closed"`, update `status: half_closed → closed`, compute `final_pnl_usd = p_realized_pnl_usd + s_realized_pnl_usd`, dispatch Alert 1.
  - Release cascade lock.
- `ts0+~2000–4500`: UI receives `hedge_closed` WS event with final P&L; toast displays.

**Race example — A vs D**: Operator clicks Close FTMO at `ts0` and SL hits at `ts0+10ms`.

- FTMO client receives both: (a) the API close cmd (operator) and (b) the broker-side SL execution.
- cTrader semantics: the API close cmd MAY race with the SL hit. One of two outcomes:
  - **(i)** API close arrives first at broker → executes manual close at market; SL event arrives later as a no-op (position already gone).
  - **(ii)** SL fires first → position closes at SL; API close arrives at a non-existent position → `ProtoOAErrorRes` with `errorCode=POSITION_NOT_FOUND`.
- In case (ii), FTMO client publishes both: a `close_response` (with error) AND an unsolicited `position_closed` event (path D). On the server, **path D arrives via event_handler first** (the event stream entry is XADDed by the bridge in the same callback). The event_handler acquires the lock (owner of cascade). The response_handler arrives next, attempts lock acquisition, gets `code='held'`, drops as loser.
- In case (i), only the close_response arrives (no unsolicited event — manual API close is correlated). Path A owner proceeds normally.
- **Correctness invariant**: regardless of which arrives first, the lock ensures exactly one cascade close is pushed to Exness.

#### §1.E.2 Path B — UI close Exness leg

**Detection**:

1. `ts0`: Operator clicks "Close Exness" in PositionList row.
2. `ts0+15`: `OrderService.close_secondary_leg(order_id)` validates `s_status == filled`, reads `s_broker_order_id`.
3. `ts0+20`: `redis.push_command("exness", exness_account, {action: "close", position_id: ..., cascade_trigger: "false"})` → XADD on `cmd_stream:exness:{acc}`, returns `req_id`.
4. `ts0+25`: HTTP 202.
5. `ts0+~1000–3000`: Exness client `cmd_dispatcher` executes `mt5.Close()`, publishes `close_response` on `resp_stream:exness:{acc}`.
6. `ts0+~3000`: `response_handler:exness._handle_close_response` reads response.

**Lock acquisition**: identical to §1.E.1 but with `trigger_broker="exness"`.

**Owner branch**:

- **▸** Update order hash (`status: filled → half_closed`; `s_status: filled → closed`; `p_status: filled → closing`; write secondary close metadata).
- **▸** Push primary close: `redis.push_command("ftmo", ftmo_account, {action: "close", position_id: p_broker_order_id, cascade_trigger: "true", origin_order_id: order_id})`.
- **▸** Broadcast `cascade_triggered`.
- **▸** Start retry watcher.
- **▸** ACK.

**Cascade completion**: symmetric to §1.E.1, with FTMO response_handler observing the cascade close response and finalizing.

**Race example — B vs E**: Operator clicks Close Exness AND closes the position manually on MT5 mobile within the same 200 ms window.

- Two ingress channels: (a) API-driven cmd_stream entry (path B); (b) position_monitor 2 s poll detects external close (path E).
- The position_monitor cycle has at most 2 s detection latency, so the operator's API click usually wins on time.
- If API click wins: `close_response` arrives → response_handler acquires lock → cascade owner.
- If MT5 close wins: `position_closed_external` event arrives via event_handler → event_handler acquires lock → cascade owner. The subsequent `close_response` from MT5's actual API close (executed by the client because the cmd was queued before the position vanished) — wait, this needs thought.

Reformulating: when the operator closes on MT5, the position vanishes from `mt5.positions_get()` snapshot on the next 2 s poll. The Exness client publishes `position_closed_external` on the event stream. The server cmd that was XADDed (path B) is still in `cmd_stream:exness`. The client's `cmd_dispatcher` reads it, calls `mt5.Close(position_id=...)`, which returns retcode 10027 (TRADE_RETCODE_POSITION_NOT_FOUND) or 10018 (TRADE_RETCODE_INVALID). The client publishes `close_response` with `status=fail, error="position_not_found"`.

Sequencing on server:
- **(i)** If `position_closed_external` (event) arrives first: event_handler acquires lock, drives cascade to FTMO. Later `close_response` (response) arrives at response_handler: cascade_trigger marker is `"false"` (operator click) but the order is already half_closed → response handler does NOT compete; instead it observes `status != filled`, logs INFO "secondary already closed externally", and ACKs.
- **(ii)** If `close_response` arrives first: response_handler reads `status=ok` (rare — MT5 has not vanished the position yet because the close cmd executed *before* the operator's MT5 click) — proceeds as normal path B. The later `position_closed_external` event arrives, event_handler reads order status as already half_closed, drops as loser.
- **(iii)** If `close_response` arrives first with `status=fail, error="position_not_found"`: response_handler does not own the cascade. It logs WARN "close_response fail but external close suspected", does not acquire lock, does not push primary close — it WAITS for the position_closed_external event to drive cascade. Risk: if for some reason position_monitor missed the external close (very unlikely with 2 s poll), the cascade never fires. Mitigation: response_handler with `error="position_not_found"` proactively calls `client.reconcile_position(position_id)` which forces an immediate position_monitor pass (D-4.0-7).

This is the trickiest race in the spec. The cascade lock alone is not sufficient — the `cascade_trigger` field on the originating cmd plus the order's current composed status both contribute to the decision.

#### §1.E.3 Path C — Manual close in cTrader UI

**Detection**:

1. `ts0`: Operator closes position from cTrader Desktop or cTrader Web.
2. `ts0+~200`: cTrader broker fires an unsolicited `ProtoOAExecutionEvent` with `executionType=FILLED` and `order.closingOrder=true` to all subscribed clients (including our FTMO client).
3. `ts0+~250`: FTMO client's `_messageReceivedCallback` parses the event (D-070 + D-071), publishes `position_closed` on `event_stream:ftmo:{acc}` with `close_reason="manual"` (D-074 schema).
4. `ts0+~250–1250`: Server `event_handler:ftmo` reads the entry (XREADGROUP BLOCK 1000 ms).

**Lock acquisition**: `acquire_cascade_lock(order_id, "ftmo", event_stream_msg_id, close_ts_ms)`.

**Owner branch**:

- Same shape as §1.E.1 path A. The difference is the **CTC source** (event stream id, not response stream id) and the cascade target's `origin_order_id`.

**No loser branch** (path C does not race with itself).

**Race vs path A**: handled in §1.E.1 case (i)/(ii) discussion.

**Race vs path D**: Operator manually closes at the same instant SL hits.

- cTrader broker decides which fires first server-side. The losing event is silently dropped at the broker (the position is gone). The FTMO client receives ONE unsolicited event. close_reason is determined by `order.orderType` (`MARKET` + `closingOrder=true` → `manual`; `STOP_LOSS_TAKE_PROFIT` + `grossProfit<0` → `sl`).
- Race is resolved at the broker, not at our server. The cascade lock is unaffected.

#### §1.E.4 Path D — SL/TP hit on FTMO

**Detection**: identical mechanism to path C; differs only in `close_reason`.

1. `ts0`: Market price crosses SL or TP on FTMO position.
2. `ts0+~50`: cTrader broker fires `ProtoOAExecutionEvent` with `order.orderType=STOP_LOSS_TAKE_PROFIT` (enum 4 per D-075).
3. `ts0+~100`: FTMO client publishes `position_closed` with `close_reason ∈ {sl, tp, stopout}` (D-071).
4. `ts0+~100–1100`: Server `event_handler:ftmo` reads.

**Lock acquisition + owner branch**: identical to path C.

**Alert routing**: same Alert 1 dispatch on terminal state. No special alert for SL/TP hit on FTMO (operator expected the SL — no anomaly).

**Note**: `close_reason=stopout` on FTMO is rare (FTMO is a prop firm; stopout is unusual). If it occurs, no special alert — the close metadata in Alert 1 names the reason ("FTMO ${p_pnl} stopout") and the operator can investigate.

#### §1.E.5 Path E — Manual MT5 close / margin call / Exness stopout

**Detection**:

1. `ts0`: Operator closes position from MT5 mobile/desktop OR Exness server fires stopout for the account.
2. `ts0+~0–2000`: Exness client's `position_monitor_loop` (2 s poll) executes `mt5.positions_get()` and observes the position is missing from the snapshot.
3. `ts0+~2000`: Client fetches deal history for the missing positionId via `mt5.history_deals_get(...)` (analogous to `fetch_close_history` D-076 path on FTMO side; spec'd in `mt5-execution-events.md` §11 placeholder).
4. `ts0+~2100`: Client publishes `position_closed_external` on `event_stream:exness:{acc}` with reconstructed `close_price`, `close_reason` ∈ {`manual`, `stopout`, `unknown`}, `closed_at`, `realized_pnl_raw`, `commission`, `swap`, `money_digits`.
5. `ts0+~2100–3100`: Server `event_handler:exness` reads.

**`close_reason` derivation on MT5**:

| MT5 deal signal | Mapped close_reason |
|---|---|
| `deal.reason == DEAL_REASON_CLIENT` (mobile/desktop manual) | `manual` |
| `deal.reason == DEAL_REASON_SO` (stopout) | `stopout` |
| `deal.reason == DEAL_REASON_SL` | `sl` (note: not used in current Phase 4 spec since MT5 legs have no SL per R3, but reserved for Phase 5) |
| `deal.reason == DEAL_REASON_TP` | `tp` (same — reserved) |
| `deal.reason` is anything else (`DEAL_REASON_EXPERT`, `DEAL_REASON_VMARGIN`, etc.) | `unknown` |

This table is provisional and is verified empirically during step 4.2 smoke; mid-phase findings update `docs/mt5-execution-events.md` §3 per D-069.

**Lock acquisition + owner branch**: identical structure to path C, with `trigger_broker="exness"`, cascade target = FTMO.

**Alert routing**:

- `close_reason=stopout` → Alert 2 (`secondary_liquidation`, CRITICAL, always-on).
- `close_reason=manual` AND originating cmd had `cascade_trigger="false"` (i.e., this is a fresh manual close, not a cascade) → Alert 3 (`secondary_close_manual`, WARN, toggleable).
- `close_reason=unknown` → Alert 3 with reason annotation (operator can investigate).

**Important — path E vs path B race**: handled in §1.E.2.

#### §1.E.6 Cascade from `secondary_failed` (G1 + cascade-via-orphan)

Special case: operator decides to close the FTMO leg of a `secondary_failed` order (D-090 row 4). This is **not** a normal path A — there is no Exness leg to cascade to.

Flow:

1. `OrderService.close_primary_leg("ord_xyz")` reads `status=secondary_failed`.
2. Allow the close (D-082 validation pipeline includes `secondary_failed → closing_primary` transition).
3. Push close cmd to FTMO (no cascade target).
4. On `close_response` ok: update `p_status: filled → closed`, composed `status: secondary_failed → closed`.
5. Alert 1 dispatched with `final_pnl_usd = p_realized_pnl_usd` (no secondary contribution).
6. No cascade lock acquired (cascade has no target).

This path is the operator's escape hatch from a leg-hở state. It does NOT cancel the secondary failed retry — the secondary push was already abandoned at 3 retries; there is nothing to cancel.

### §1.F `cascade_trigger` marker propagation

The `cascade_trigger` field is a wire-level discriminator that lets the server distinguish:

- **Operator-initiated close** (`cascade_trigger="false"` or absent): an explicit operator action via REST or the broker UI. Triggers Alert 1 on terminal; may trigger Alert 3 if path E with `close_reason=manual`.
- **Cascade-initiated close** (`cascade_trigger="true"`): pushed by the cascade owner as the secondary leg of an already-locked cascade. Does NOT trigger Alert 3 (the operator did not directly close this leg; the system did).

#### §1.F.1 Where the marker lives

| Stage | Carrier | Field path | Value |
|---|---|---|---|
| 1. Cascade owner pushes cmd | `cmd_stream:{broker}:{account}` XADD entry | top-level field `cascade_trigger` | `"true"` or `"false"` |
| 2. Client receives cmd | Client-side cmd dict | `cmd["cascade_trigger"]` | passthrough |
| 3. Client emits `close_response` | `resp_stream:{broker}:{account}` XADD entry | NEW field `cascade_trigger` (D-4.0-8) | passthrough |
| 4. Server `response_handler` reads | `entry["cascade_trigger"]` | normalize "true"/"false" → bool | used for branching |
| 5. Client emits unsolicited `position_closed_external` | `event_stream:{broker}:{account}` XADD entry | NOT applicable (event was not preceded by a server cmd; field absent → treat as `"false"`) | — |

Note that for paths C and D (FTMO unsolicited events), there is no cmd that preceded the event — the close was broker-driven. The `cascade_trigger` field is absent on the event stream entry; the handler treats absence as `"false"` (operator/broker action). This is correct: a cTrader manual close is operator action; an SL/TP hit is broker action that we treat as operator-acceptable.

For path E (Exness unsolicited position monitor close detection), there are two sub-cases:

- **E.1**: The position was closed externally without any server-pushed close cmd. `cascade_trigger` is absent on the event → treated as `"false"`. Alert routing depends on `close_reason` (Alert 2 / Alert 3).
- **E.2**: The position was closed externally AT THE SAME TIME a cascade close cmd was already on the wire (rare race). The event is still emitted by the position_monitor (it cannot know the cmd is in flight). When the `close_response` arrives later with `status=fail, error="position_not_found"`, the response_handler correlates by `position_id` and sees the order is already `half_closed`. No double-cascade, no double-alert (Alert 2/3 fired from the event handler; response_handler drops).

#### §1.F.2 Why we cannot just check the cmd_stream after-the-fact

A naive design would have the response_handler **look up the originating cmd** in `cmd_stream` by `request_id` to read its `cascade_trigger` field. This fails because:

1. Streams are append-only and entries are not indexed by content; you would need `XRANGE` with the exact ID, which works but adds 1 round-trip on the hot path.
2. The cmd may have been trimmed already (we cap `cmd_stream` at MAXLEN ~1000 per D-001-like sizing). Lookup may miss.
3. Carrying the field forward on resp_stream is 1 extra HSET-equivalent field (~30 bytes) per close response — negligible.

So we propagate the marker explicitly. Step 4.2 (Exness client cmd_dispatcher) and step 3.7-equivalent server response_handler are updated to read/write the field.

#### §1.F.3 Marker on event_stream — `cascade_trigger` enrichment

For path E, we additionally enrich the `position_closed_external` event with a `correlated_cmd_id` field IF the position_monitor can correlate the missing position with a recently-pushed cascade cmd. This is best-effort and Phase 4 may defer to Phase 5 hardening. The simpler Phase 4 behavior:

- If `position_closed_external` arrives AND a cascade cmd for the same position_id is in the in-flight cmd ledger (a new in-memory dict in the server, keyed by `position_id`, populated when cascade owner pushes the cmd, cleared on response receipt): treat the close as cascade-driven (do not double-trigger Alert 2/3).
- Else: treat as operator/external close.

D-4.0-9: In-flight cmd ledger lives in the server process memory, keyed `cmd_ledger[position_id] = {request_id, broker, ts_ms}`. Cleared on response receipt or 30 s TTL. Server restart loses the ledger — restart edge case handled by the orphaned_order_sweep (§1.B.6).

#### §1.F.4 Marker flow diagram

```
┌───────────────────────────────────────────────────────────────────┐
│ Cascade owner (e.g., response_handler:ftmo) detects path A.       │
│ Acquires cascade_lock.                                            │
└───────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                XADD cmd_stream:exness:{acc}
                  fields: { action="close",
                            position_id=...,
                            cascade_trigger="true",  ◄── marker injected
                            origin_order_id=... }
                              │
                              ▼
┌──────────────────────────────────────────────┐
│ Exness client cmd_dispatcher reads cmd.      │
│ Passes cascade_trigger through to handler.   │
│ Executes mt5.Close().                        │
└──────────────────────────────────────────────┘
                              │
                              ▼
                XADD resp_stream:exness:{acc}
                  fields: { msg_type="close_response",
                            status="ok",
                            position_id=...,
                            close_price=...,
                            cascade_trigger="true" } ◄── marker propagated
                              │
                              ▼
┌──────────────────────────────────────────────┐
│ Server response_handler:exness reads resp.   │
│ Sees cascade_trigger="true".                 │
│ Skips lock acquisition (already held by      │
│ FTMO-side owner).                            │
│ Updates s_status: closing → closed.          │
│ Checks composed status — if both legs closed,│
│ flips to closed + dispatches Alert 1.        │
└──────────────────────────────────────────────┘
```

Compare to operator-initiated close:

```
DELETE /api/orders/.../legs/secondary
        │
        ▼
XADD cmd_stream:exness:{acc}
  fields: { action="close",
            position_id=...,
            cascade_trigger="false" } ◄── operator action
        │
        ▼
... (client executes) ...
        │
        ▼
XADD resp_stream:exness:{acc}
  fields: { msg_type="close_response", status="ok",
            cascade_trigger="false" }
        │
        ▼
┌──────────────────────────────────────────────┐
│ Server response_handler:exness reads resp.   │
│ Sees cascade_trigger="false".                │
│ ATTEMPTS cascade_lock acquisition. Acquires. │
│ Pushes primary close cmd (cascade target).   │
│ ... etc                                      │
└──────────────────────────────────────────────┘
```

#### §1.F.5 Marker absence on event_stream entries

Phase 3 D-070 defines four event types (`position_closed`, `pending_filled`, `position_modified`, `order_cancelled`) and does NOT carry `cascade_trigger`. Phase 4 adds:

- `position_closed_external` (path E; Exness client emit) — `cascade_trigger` field semantics per §1.F.3 enrichment.
- `cascade_triggered` (server emit to WS; not Redis-stream event) — out-of-band; not carrying the marker.

The Phase 3 `position_closed` event (FTMO unsolicited; paths C and D) does NOT need the marker — it represents broker-side closure not initiated by our cmd. Absence is the correct semantics (treat as operator/broker action).

---

## §2. Telegram Alerts Architecture

The Phase 4 alert subsystem provides operator-grade visibility into critical events: completed hedges, orphaned legs, secondary liquidations, manual external closes, server errors, client offline, and broker disconnects. Alerts are pushed to a dedicated Telegram channel via the existing bot token (`TELEGRAM_BOT_TOKEN` reused) and a new chat (`TELEGRAM_ALERT_CHAT_ID` separate from the Claude Code self-check chat).

### §2.A Alert types specification

The complete alert catalog. Each alert has a unique numeric ID, a fixed severity, a stable message template, and a toggle policy.

| ID | Trigger | Severity | Message template | Toggleable |
|---|---|---|---|---|
| **1** | `hedge_closed` — both legs reach `closed` and final P&L is computed | INFO | `[INFO] Hedge {pair} {side} {vol} closed | P&L total ${pnl_total} (FTMO ${p_pnl} {p_reason} / Exness ${s_pnl} {s_reason})` | Yes |
| **1.5** | `leg_orphaned` — `secondary_failed` after 3 retries OR cascade timeout (§1.B.6 sweep) | CRITICAL | `[CRITICAL] Hedge {pair} {side} {vol} — primary closed but secondary cascade FAILED. Order {order_id} needs manual intervention.` | No (always on) |
| **2** | `secondary_liquidation` — path E with `close_reason=stopout` | CRITICAL | `[CRITICAL] Exness leg liquidated by stopout. Order {order_id} | Loss ${s_pnl}. FTMO leg cascade closing.` | No |
| **3** | `secondary_close_manual` — path E with `close_reason=manual` AND `cascade_trigger="false"` | WARN | `[WARN] Operator closed Exness leg manually on MT5. Order {order_id}. FTMO leg cascade closing.` | Yes |
| **4a** | `server_error` — uncaught exception in FastAPI handler | CRITICAL | `[CRITICAL] Server error: {exception_class} in {handler}. Trace: {short_trace}` | No |
| **4b** | `client_offline` — heartbeat key missing for >60 s | WARN | `[WARN] {broker} client {account_id} offline >60s. Last heartbeat: {ts}` | Yes |
| **4b_recovery** | `client_online` — heartbeat resumes after >60 s outage | INFO | `[INFO] {broker} client {account_id} back online after {duration}s offline` | Linked to 4b toggle, bypasses cooldown |
| **4c** | `broker_disconnect` — cTrader OAuth fail OR MT5 lib disconnect | CRITICAL | `[CRITICAL] {broker} broker disconnect: {reason}` | No |
| **4c_recovery** | `broker_reconnect` — broker connection restored | INFO | `[INFO] {broker} broker reconnected after {duration}s` | Linked to 4c toggle, bypasses cooldown |

#### §2.A.1 Severity → log routing

| Severity | Telegram | Server stdout/file log | Frontend toast |
|---|---|---|---|
| INFO | ✓ (dispatched) | INFO | optional (already covered by `hedge_closed` WS event) |
| WARN | ✓ (dispatched) | WARNING | yes (yellow toast) |
| CRITICAL | ✓ (dispatched) | ERROR | yes (red toast, dismiss-only) |

Frontend toast wiring is step 4.10. Telegram dispatch is step 4.11 (or 4.11-renamed; see §0.4).

#### §2.A.2 Always-on alerts (no toggle)

Types 1.5, 2, 4a, 4c are deliberately not toggleable. Rationale:

- **1.5 (leg orphaned)**: leg hở is one-sided market risk; suppressing it is operationally dangerous.
- **2 (secondary liquidation)**: stopout means real loss + cascade close in progress; operator must know.
- **4a (server error)**: any unhandled exception is a defect; suppression hides defects.
- **4c (broker disconnect)**: orders cannot be placed/closed; operator must know immediately.

#### §2.A.3 Recovery alerts bypass cooldown

The 4b_recovery and 4c_recovery alerts use `bypass_cooldown=True`. Rationale: if the operator silenced the outage alert (manual snooze) and the system recovers shortly after, we want to confirm recovery even within the cooldown window. Suppressing the recovery message because "I just told you about the outage" defeats the point.

#### §2.A.4 4b single-threshold decision

CEO confirmed: type 4b uses a single 60-second offline threshold (no WARN → CRITICAL escalation). Rationale: typical operator response is to check the client process / VPN; a tiered escalation adds complexity without changing the action.

The corresponding 4c (broker disconnect) has no threshold — alert fires immediately on detection. Rationale: broker disconnects are immediately actionable (restart client / re-authenticate) and rare; we want instant signal.

### §2.B Alert dispatcher service specification

#### §2.B.1 Module location and structure

```
server/app/services/alert_service.py        # NEW (step 4.11)
server/app/services/alert_service_keys.py   # NEW; Redis key naming constants
server/app/services/alert_templates.py      # NEW; message template renderers
```

Why three modules: separating templates from dispatch logic lets us version templates (e.g., adding a "secondary venue" field in Phase 5) without touching the dispatcher. Separating keys from service follows the existing pattern (`redis_service_lua.py` separated from `redis_service.py`).

#### §2.B.2 Public API

```python
from typing import Literal

Severity = Literal["INFO", "WARN", "CRITICAL"]

class AlertService:
    def __init__(
        self,
        redis: redis_asyncio.Redis,
        bot_token: str | None,
        chat_id: str | None,
        http_client: httpx.AsyncClient | None = None,
        cooldown_seconds: int = 300,
    ) -> None: ...

    async def dispatch(
        self,
        alert_type: str,
        severity: Severity,
        message: str,
        dedup_key: str,
        bypass_cooldown: bool = False,
    ) -> bool:
        """Returns True if dispatched, False if suppressed (toggle off,
        cooldown hit, or missing chat_id)."""

    async def is_enabled(self, alert_type: str) -> bool: ...
    async def set_enabled(self, alert_type: str, enabled: bool) -> None: ...
```

#### §2.B.3 Dispatch algorithm (4-step)

```python
async def dispatch(self, alert_type, severity, message, dedup_key, bypass_cooldown=False) -> bool:
    # 1. Toggle check — settings HASH lookup
    if alert_type in TOGGLEABLE_ALERTS and not await self.is_enabled(alert_type):
        logger.debug("alert.dispatch.skipped_toggle", alert_type=alert_type)
        return False

    # 2. Cooldown check — Redis STRING with SET NX EX
    if not bypass_cooldown:
        cooldown_key = f"alert_cooldown:{dedup_key}"
        acquired = await self._redis.set(
            cooldown_key, "1", nx=True, ex=self._cooldown_seconds
        )
        if not acquired:
            logger.debug("alert.dispatch.skipped_cooldown", dedup_key=dedup_key)
            return False

    # 3. Missing config → dev/test fallback
    if not self._bot_token or not self._chat_id:
        logger.warning("alert.dispatch.disabled_no_chat", alert_type=alert_type)
        return False

    # 4. HTTP POST Telegram sendMessage
    try:
        resp = await self._http.post(
            f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
            data={"chat_id": self._chat_id, "text": message},
            timeout=5.0,
        )
        resp.raise_for_status()
        logger.info("alert.dispatch.sent", alert_type=alert_type, severity=severity)
        return True
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.error("alert.dispatch.failed", alert_type=alert_type, error=str(e))
        # NO RETRY in Phase 4. Phase 5 hardening adds an outbox + retry queue.
        return False
```

#### §2.B.4 Why no retry in Phase 4

Telegram Bot API has occasional 5xx blips. A retry queue (Phase 5 backlog) would buffer failed dispatches and replay them. Phase 4 keeps it simple: best-effort fire-and-forget. Rationale:

- The cooldown key was already set (step 2) — a retry would have to NOT reset it.
- Server logs always carry the alert payload — the operator can grep the log post-hoc if Telegram drop occurred.
- WS toasts cover the same events (for WARN/CRITICAL) on the frontend — operator who has the UI open sees the alert even on Telegram failure.

If Telegram outage becomes a recurring issue post-Phase-4, Phase 5 hardens this with a Redis stream `alert_outbox` + a background `alert_retry_loop`.

#### §2.B.5 Lifecycle integration

`AlertService` is instantiated in `server/app/main.py` lifespan startup, after `RedisService` and before handlers start:

```python
# server/app/main.py — lifespan (step 4.11)
alert_service = AlertService(
    redis=redis_client,
    bot_token=settings.telegram_bot_token,
    chat_id=settings.telegram_alert_chat_id,
)
app.state.alert_service = alert_service
```

Handlers receive it via DI (FastAPI `Depends(get_alert_service)`). Background loops (event_handler, response_handler, account_status_loop, position_monitor sweep) receive it as a constructor arg.

Lifespan shutdown closes the shared `httpx.AsyncClient`. AlertService does not own background tasks itself.

### §2.C Cooldown and dedup specification

The dedup key is the unit of cooldown. Two alerts with the same `dedup_key` within 300 s collapse to one; the second is silently suppressed unless `bypass_cooldown=True`.

#### §2.C.1 dedup_key construction patterns

| Alert | dedup_key pattern | Rationale |
|---|---|---|
| 1 (`hedge_closed`) | `"hedge_closed:{order_id}"` | Each order's hedge_closed is a one-time terminal; even if duplicate event fires (handler bug), suppress. |
| 1.5 (`leg_orphaned`) | `"leg_orphaned:{order_id}"` | One alert per orphan. Operator either fixes or doesn't; spam is unhelpful. |
| 2 (`secondary_liquidation`) | `"secondary_liquidation:{order_id}"` | One alert per liquidation event. |
| 3 (`secondary_close_manual`) | `"secondary_close_manual:{order_id}"` | One alert per order's manual close. |
| 4a (`server_error`) | `"server_error:{handler_name}:{exception_class}"` | Anti-spam during cascading exception storms (NOT per-`order_id` — operator wants to know the handler+exception class, not 500 messages about it). |
| 4b (`client_offline`) | `"client_offline:{broker}:{account_id}"` | Anti-flap: if connection drops every 30 s, we don't want 12 alerts/hour. |
| 4b_recovery (`client_online`) | `"client_online:{broker}:{account_id}"` (with bypass_cooldown=True) | Recovery always lands. |
| 4c (`broker_disconnect`) | `"broker_disconnect:{broker}:{account_id}"` | Same anti-flap as 4b. |
| 4c_recovery (`broker_reconnect`) | `"broker_reconnect:{broker}:{account_id}"` (with bypass_cooldown=True) | Recovery always lands. |
| (custom test alert) | `"test:{ad_hoc_id}"` | For operator-triggered diagnostic via Settings UI "Send test alert" button (step 4.10). |

#### §2.C.2 Cooldown TTL

- Default: 300 s (5 minutes). CEO confirmed.
- Configurable per environment via `TELEGRAM_ALERT_COOLDOWN_SECONDS` env var (range 30–3600). Default value baked in if unset.
- Cooldown applies on the `dispatch()` call site, not the underlying event. If the underlying event re-fires after 301 s and the alert dispatches again, that is the intended behavior (operator gets re-notified that the situation persists).

#### §2.C.3 Cooldown key TTL semantics

- Stored as `alert_cooldown:{dedup_key}` STRING with `SET NX EX 300`. Value is `"1"` (the value is meaningless; existence is the signal).
- Redis auto-evicts on TTL expiry. No periodic cleanup needed.
- `bypass_cooldown=True` skips both the GET and the SET — the cooldown key is unaffected. This means after a `bypass_cooldown` recovery, the original cooldown key (from the offline alert) is still in place for its remaining TTL. If the system flaps again, the offline alert is still suppressed for the remainder of the original cooldown. **This is by design**: bypass is for recovery confirmations, not for resetting the anti-flap window.

### §2.D Settings schema

#### §2.D.1 Redis key

```
alert_settings  HASH
  Fields (Phase 4):
    hedge_closed            "true" | "false"   # Alert 1
    secondary_close_manual  "true" | "false"   # Alert 3
    client_offline          "true" | "false"   # Alert 4b (and 4b_recovery linked)
                                               # Note: setting 4b to false also
                                               # silences 4b_recovery, but
                                               # 4b_recovery uses bypass_cooldown
                                               # only for the cooldown, not for
                                               # the toggle. Toggle is the
                                               # operator's master switch.

  Fields NOT in HASH (always on; toggle would be ignored):
    leg_orphaned (Alert 1.5)
    secondary_liquidation (Alert 2)
    server_error (Alert 4a)
    broker_disconnect (Alert 4c)  (4c_recovery linked similarly)
```

#### §2.D.2 Defaults on first install

All three toggleable alerts default to `"true"`. Logic in `AlertService.is_enabled`:

```python
async def is_enabled(self, alert_type: str) -> bool:
    if alert_type not in TOGGLEABLE_ALERTS:
        return True  # always-on alerts ignore toggle
    val = await self._redis.hget("alert_settings", alert_type)
    if val is None:
        return True  # missing field = default-on
    return val.lower() == "true"
```

#### §2.D.3 Frontend UI (Settings → General tab → Alerts section)

Step 4.10 wires the UI into the existing SettingsModal (Phase 3 step 3.13 ships the modal shell). New UI section:

```
┌── Settings: General ──────────────────────────────────────┐
│                                                            │
│  Alerts                                                    │
│  ┌──────────────────────────────────────────────────────┐ │
│  │  [✓] Hedge closed (Alert 1)                          │ │
│  │      Notifies on every completed hedge with P&L.     │ │
│  │                                                       │ │
│  │  [✓] Secondary closed manually on MT5 (Alert 3)      │ │
│  │      Notifies when Exness leg is closed via the MT5  │ │
│  │      terminal (cascade still fires automatically).   │ │
│  │                                                       │ │
│  │  [✓] Client offline >60s (Alert 4b)                  │ │
│  │      Notifies when FTMO or Exness client heartbeat   │ │
│  │      misses for more than 60 seconds.                │ │
│  │                                                       │ │
│  │  ──────────────────────────────────────────────       │ │
│  │  Always-on (cannot be disabled):                     │ │
│  │    • Orphaned leg (Alert 1.5)                        │ │
│  │    • Secondary liquidation (Alert 2)                 │ │
│  │    • Server error (Alert 4a)                         │ │
│  │    • Broker disconnect (Alert 4c)                    │ │
│  │  ──────────────────────────────────────────────       │ │
│  │                                                       │ │
│  │  [ Send test alert ] [ Save ]                        │ │
│  └──────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

The "Send test alert" button calls a server endpoint (step 4.11 wiring) `POST /api/alerts/test` which dispatches a known test alert (`alert_type="test"`, `dedup_key="test:{user_id_or_random}"`, `bypass_cooldown=True`). Confirms wiring without mutating any state.

#### §2.D.4 Settings API surface

```
GET  /api/settings/alerts
     → { "hedge_closed": true, "secondary_close_manual": true, "client_offline": true }

PUT  /api/settings/alerts
     body: { "hedge_closed": false, "secondary_close_manual": true, "client_offline": true }
     → 200 OK
     Side effect: HSET alert_settings field <field> "true"|"false"

POST /api/alerts/test
     → 202 Accepted  (dispatch outcome logged; no body returned synchronously)
```

These endpoints are step 4.11 work.

### §2.E Telegram client configuration

#### §2.E.1 .env additions (DOCUMENT ONLY in step 4.0 — actual edit in step 4.11)

```
# Existing (Phase 1-3): used by scripts/notify_telegram.sh for Claude Code self-check
TELEGRAM_BOT_TOKEN=<existing_bot_token>
TELEGRAM_CHAT_ID=<existing_chat_id_self_check>   # NOT changed; remains for self-check

# NEW (Phase 4): used by AlertService for production alerts
TELEGRAM_ALERT_CHAT_ID=<new_chat_id_production_alerts>

# OPTIONAL (Phase 4): cooldown override
TELEGRAM_ALERT_COOLDOWN_SECONDS=300
```

Note that the *existing* `TELEGRAM_CHAT_ID` is **not** reused by AlertService. The Claude Code self-check chat receives noisy step self-checks (every step ships a long markdown report); the production alert channel must remain quiet so operator can act on each notification. Hence two separate chats.

CEO will create the new Telegram channel and provide `TELEGRAM_ALERT_CHAT_ID` at step 4.11 (or whichever step lands the alert backend).

#### §2.E.2 server/app/config.py additions (DOCUMENT ONLY)

```python
class Settings(BaseSettings):
    # ... existing fields ...

    # Phase 4 Telegram alert system (step 4.11)
    telegram_bot_token: str | None = None
    telegram_alert_chat_id: str | None = None
    telegram_alert_cooldown_seconds: int = 300

    @field_validator("telegram_alert_cooldown_seconds")
    @classmethod
    def _cooldown_range(cls, v: int) -> int:
        if v < 30 or v > 3600:
            raise ValueError("TELEGRAM_ALERT_COOLDOWN_SECONDS must be in [30, 3600]")
        return v
```

#### §2.E.3 Behavior when chat_id is missing

If `telegram_alert_chat_id` is `None` or empty string (e.g., dev environment, CI), `AlertService.dispatch` logs `WARN alert.dispatch.disabled_no_chat` once per alert type per process lifetime (in-memory dedup; not Redis cooldown — we want to know dev wiring is missing without flooding the log), then returns `False`.

This makes Phase 4 testable in a CI without Telegram secrets. Production deploy **must** set the chat_id; the runbook (step 4.12 docs sync) will list this as a mandatory deploy step.

#### §2.E.4 No reuse of `notify_telegram.sh`

The script remains unchanged. It serves Claude Code self-check delivery only. Reasons we do NOT extend it for production alerts:

- Server is a Python process; AlertService should be in-process (no shell-out latency on the hot path).
- `notify_telegram.sh` requires a host file at `$HOME/.config/hedger-sandbox/telegram.env` — Windows deploy (Phase 5) would not have a Linux-style home dir. AlertService reads from pydantic Settings (cross-platform).
- The two chats and the two flows (operational vs developmental) are deliberately separated; sharing transport invites accidental cross-firing.

### §2.F Where alerts fire (code locations)

For each alert, the spec target file + approximate line. Steps that introduce the file land within ±20 lines of these targets.

| Alert | File | Approx line | Trigger condition | Data inputs at fire site |
|---|---|---|---|---|
| 1 (`hedge_closed`) | `server/app/services/event_handler.py::_finalize_hedge_closed` | ~280 | Both `p_status` and `s_status` observed `closed` after a status update; composed `status` flipped `half_closed → closed`. | `order`, `final_pnl_usd`, `p_pnl`, `s_pnl`, `p_reason`, `s_reason` |
| 1.5 (`leg_orphaned`) | `server/app/services/hedge_service.py::_handle_secondary_failed` | ~150 | 3 retries exhausted in `_cascade_secondary_retry_loop`. | `order_id`, `pair_name`, `side`, `volume`, `failure_reason` |
| 1.5 (`leg_orphaned` — sweep path) | `server/app/services/orphan_sweep.py::_sweep_once` | ~80 | `cascade_lock` missing AND `updated_at` >30 s ago AND composed `status=half_closed`. | `order`, `last_known_status`, `staleness_seconds` |
| 2 (`secondary_liquidation`) | `server/app/services/event_handler.py::_handle_position_closed_external` | ~410 | `broker="exness"` AND `close_reason="stopout"`. | `order_id`, `s_pnl`, `pair_name` |
| 3 (`secondary_close_manual`) | `server/app/services/event_handler.py::_handle_position_closed_external` | ~440 | `broker="exness"` AND `close_reason="manual"` AND cmd_ledger lookup returns no in-flight cascade (i.e., not cascade-initiated). | `order_id`, `pair_name` |
| 4a (`server_error`) | `server/app/main.py::generic_exception_handler` (FastAPI exception middleware) | ~120 | Uncaught exception in any request handler. | `handler_name`, `exception_class`, `short_trace` (truncated to ~500 chars) |
| 4b (`client_offline`) | `server/app/services/account_status_loop.py::_check_heartbeat_staleness` | ~95 | Heartbeat key `client_heartbeat:{broker}:{account_id}` missing OR `last_seen_ts < now - 60`. | `broker`, `account_id`, `last_seen_ts` |
| 4b_recovery (`client_online`) | `server/app/services/account_status_loop.py::_check_heartbeat_staleness` | ~135 | Heartbeat resumes after a prior offline alert. | `broker`, `account_id`, `offline_duration_seconds` |
| 4c (`broker_disconnect`) — FTMO | `server/app/services/event_handler.py::_handle_broker_disconnect_event` | ~520 | FTMO client publishes `broker_disconnect` event (new event type — D-4.0-10). | `broker="ftmo"`, `account_id`, `reason` |
| 4c (`broker_disconnect`) — Exness | `server/app/services/event_handler.py::_handle_broker_disconnect_event` | ~520 | Exness client publishes `broker_disconnect` event. | `broker="exness"`, `account_id`, `reason` |
| 4c_recovery | `server/app/services/event_handler.py::_handle_broker_reconnect_event` | ~560 | Reconnect event. | `broker`, `account_id`, `disconnect_duration_seconds` |
| Test alert | `server/app/api/alerts.py::send_test_alert` | ~30 | `POST /api/alerts/test` from Settings UI. | (none — fixed test message) |

#### §2.F.1 FTMO broker_disconnect — Phase 4 includes this

Phase 3 FTMO client (`bridge_service.py`) already detects cTrader OAuth fail and connectionLost via Twisted callbacks (D-050 / D-073 patterns). Phase 4 adds:

- Client emits `broker_disconnect` event on `event_stream:ftmo:{account}` with `reason` (OAuth fail / TCP reset / heartbeat miss).
- Client emits `broker_reconnect` event on restoration.
- Server `event_handler:ftmo` adds two new branches for these event types.
- AlertService dispatches Alert 4c / 4c_recovery accordingly.

Rationale (CEO confirmed): the Phase 3 infrastructure already detects disconnect; emitting an event stream entry and adding a single server handler branch is cheap. Defer the Exness side to step 4.2 (Exness client introduces the disconnect detection symmetrically).

#### §2.F.2 Approximate lines reflect target, not certainty

The file:line table is the spec's intent. The implementation step lands within a small window of the target; the step self-check reconciles actual lines and corrects the spec via append-only update to this section (analogous to D-069 mid-phase update pattern).

---

## §3. Decisions log (D-4.0-N) + open questions for CTO

This section captures decisions emerging from step 4.0 design work. Promoted to canonical `D-XXX` in step 4.12 docs sync.

### §3.1 Decisions

**D-4.0-1** — Phase 4 step renumber: insert dedicated alert backend step between current 4.10 and 4.11; old 4.11 (docs sync) becomes 4.12.

- *Why*: Alert backend is non-trivial (service + tests + env config) and deserves its own step boundary. Lumping it into 4.11 docs sync blurs scopes.
- *How to apply*: Step that lands `alert_service.py` performs the MASTER_PLAN_v2 §5 renumber in the same commit (it is the natural anchor).

**D-4.0-2** — MT5 position monitoring is poll-based (2 s) by necessity.

- *Why*: MT5 Python lib (`MetaTrader5` package) exposes no callback/push API. Only synchronous `positions_get()`, `history_deals_get()`, etc.
- *How to apply*: Exness client step 4.2 implements `position_monitor_loop` as a 2 s asyncio sleep loop wrapping blocking calls in `run_in_executor`. This is the only non-event-driven detection in the system; all other broker integrations remain push-based.

**D-4.0-3** — Transient `s_status=cascade_cancel_pending` for path A/C/D when secondary push is in-flight.

- *Why*: Avoid orphan leg if primary closes while secondary `open` cmd is on the wire.
- *How to apply*: `update_order_with_cas` allowed transitions: `s_status: pending_open → cascade_cancel_pending`. On subsequent `open_response`, the response_handler reads this transient state and: (a) if open succeeded, immediately push close cmd; (b) if open failed, mark order `closed` (clean close, no orphan).

**D-4.0-4** — New status index: `orders:by_status:secondary_failed`.

- *Why*: PositionList must filter to leg-hở orders quickly. Per-account client-side filter (D-089 Phase 3 ok) is acceptable for ≤50 orders.
- *How to apply*: `update_order_with_cas` Lua already maintains `orders:by_status:*` indices for any new status value — no Lua change needed.

**D-4.0-5** — `UPDATE_ORDER_LUA_V2` extends CAS to leg sub-statuses (optional).

- *Why*: Phase 4 transitions involve `p_status` / `s_status` simultaneously with composed `status`. Compound CAS reduces race window.
- *How to apply*: Step 4.6 evaluates whether the small race window is operationally acceptable. If yes, defer V2 to Phase 5. If no, ship V2.

**D-4.0-6** — `cmd_metadata:{request_id}` HASH for cmd context lookup from response_handler.

- *Why*: Carrying `cascade_trigger` (and possibly more fields) on resp_stream is sufficient; the metadata HASH is a fallback for fields we don't want to wire onto every response. Phase 4 may skip this and propagate the marker on resp_stream only (the simpler path).
- *How to apply*: Step 4.6 chooses. Defaulted to "resp_stream propagation only, no metadata HASH" pending CTO concurrence.

**D-4.0-7** — Response with `error="position_not_found"` triggers immediate position_monitor pass.

- *Why*: §1.E.2 case (iii) safety net — if position vanished externally and the response_handler observes a `position_not_found` close failure, force a reconcile rather than wait up to 2 s for the regular poll.
- *How to apply*: Server pushes a `force_reconcile_position` cmd to the client; client executes immediate `positions_get()` + deal history fetch.

**D-4.0-8** — `cascade_trigger` field propagated through resp_stream.

- *Why*: §1.F.2 — server discriminator without round-trip lookup.
- *How to apply*: Step 4.2 (Exness client) + step 3.7-equivalent (FTMO client + server handlers) updated to write/read field.

**D-4.0-9** — In-process `cmd_ledger` for in-flight cascade cmd tracking by `position_id`.

- *Why*: §1.F.3 — disambiguate "external close + race with cascade cmd" without an extra Redis round-trip.
- *How to apply*: Server-side dict, populated on cascade owner push, cleared on response or 30 s timeout. Single-server assumption (Phase 4 ships single FastAPI process; Phase 5 multi-instance deploy revisits via Redis-backed ledger).

**D-4.0-10** — New event types: `broker_disconnect`, `broker_reconnect`, `position_closed_external`.

- *Why*: Phase 3 D-070 enumerates 4 event types; Phase 4 adds 3 more for disconnect/external-close signaling.
- *How to apply*: `docs/06-data-models.md` §15 schema additions in step 4.12 docs sync.

### §3.2 Open questions for CTO

1. **Cooldown env var name**: `TELEGRAM_ALERT_COOLDOWN_SECONDS` is verbose. Acceptable, or prefer `ALERT_COOLDOWN_S`? (Spec uses the long form; trivial to change at impl time.)
2. **Path E close_reason mapping for `DEAL_REASON_EXPERT`**: Some Exness brokers tag stopout deals as `DEAL_REASON_EXPERT` (EA-driven). Spec treats `unknown` and routes Alert 3. Acceptable, or treat as Alert 2? Empirical verification deferred to step 4.2 smoke.
3. **Cascade lock TTL extension on retry**: Spec lets `extend_cascade_lock` reset TTL during the 3.5 s retry window. Should the lock also extend on `force_reconcile_position` (D-4.0-7) to avoid orphan_sweep mis-firing?
4. **Alert 4a — short_trace truncation length**: 500 chars is a guess. Telegram caption limit is 4096; we should fit the full trace. Adjust to 2000?
5. **Test alert dedup_key**: Spec uses `"test:{ad_hoc_id}"`. If `ad_hoc_id` is user-controlled, this is a low-stakes injection vector for cooldown key namespace pollution. Lock to `"test:{operator_id_or_ts}"`?
6. **`leg_orphaned` alert wording**: The current template names the order_id but not the broker the operator should act on. Consider adding `Broker {ftmo|exness} leg still open` line?
7. **MT5 hedging mode requirement**: Spec assumes Exness account is in hedging mode (multiple positions per symbol). Should we add a startup check + fail-fast if account is netting-mode? Currently assumed Phase 5 hardening (G13-adjacent).
8. **`cmd_ledger` (D-4.0-9) survival across server restart**: Spec accepts that ledger is lost on restart; orphan_sweep + cascade_lock TTL handle the recovery edge. Acceptable, or invest in Redis-backed ledger now?

### §3.3 Phase 4 step plan after step 4.0

Restated for clarity (the renumber per D-4.0-1 happens at step 4.11 land):

| # | Branch | Scope |
|---|---|---|
| 4.0 | `step/4.0-cascade-and-alerts-design-doc` | (this step) |
| 4.1 | `step/4.1-exness-client-skeleton` | MT5 connect + Redis + heartbeat + XREADGROUP |
| 4.2 | `step/4.2-exness-client-actions-and-monitor` | Open market + close + position monitor + retcode + account info |
| 4.3 | `step/4.3-server-accounts-pairs-api` | Accounts CRUD + pairs link + risk_ratio |
| 4.4 | `step/4.4-server-consumer-groups-runtime` | Runtime consumer group creation on add-account |
| 4.5 | `step/4.5-server-create-hedge-order` | Full primary→secondary flow with 3-retry |
| 4.6 | `step/4.6-server-cascade-close` | cascade_lock + 5 paths + state machine + cascade_trigger marker |
| 4.7 | `step/4.7-server-position-tracker-2legs` | P&L 2 leg + total |
| 4.8 | `step/4.8-web-settings-modal` | 3 tabs Accounts/Pairs/General (alerts UI shell only) |
| 4.9 | `step/4.9-web-account-status-bar` | AccountStatus bar + heartbeat WS |
| 4.10 | `step/4.10-web-hedge-display-and-toasts` | PositionList 2 leg + toasts + form validate |
| 4.11 | `step/4.11-server-alerts-backend` | AlertService + 4 alert types + Telegram wire + Settings API |
| 4.12 | `step/4.12-phase-4-docs-sync` | PHASE_4_REPORT, MASTER_PLAN renumber, DECISIONS append, tag `phase-4-complete` |

---

## §3.4 ASCII sequence diagrams (per path)

The diagrams below render the most important paths end-to-end. They are intended for engineer onboarding and step-4.6 test fixture authoring.

### §3.4.1 Path A — UI close FTMO (happy path)

```
Operator UI   Frontend     Server          Redis        FTMO client    cTrader API     Exness client    MT5 API
    │            │            │               │              │              │                │              │
    │  click     │            │               │              │              │                │              │
    │ Close FTMO │            │               │              │              │                │              │
    ├───────────►│            │               │              │              │                │              │
    │            │  DELETE    │               │              │              │                │              │
    │            │  /api/...  │               │              │              │                │              │
    │            ├───────────►│               │              │              │                │              │
    │            │            │ XADD cmd_str  │              │              │                │              │
    │            │            ├──────────────►│              │              │                │              │
    │            │            │               │  XREADGROUP  │              │                │              │
    │            │            │               ├─────────────►│              │                │              │
    │            │            │               │              │ ClosePosReq  │                │              │
    │            │            │               │              ├─────────────►│                │              │
    │            │            │               │              │ ACCEPTED     │                │              │
    │            │            │               │              │◄─────────────┤                │              │
    │            │            │               │              │ FILLED       │                │              │
    │            │            │               │              │◄─────────────┤                │              │
    │            │            │               │ XADD resp    │              │                │              │
    │            │            │               │◄─────────────┤              │                │              │
    │            │            │ XREADGROUP    │              │              │                │              │
    │            │            │◄──────────────┤              │              │                │              │
    │            │            │ acquire_cas_  │              │              │                │              │
    │            │            │  lock         │              │              │                │              │
    │            │            ├──────────────►│              │              │                │              │
    │            │            │ {1, owner}    │              │              │                │              │
    │            │            │◄──────────────┤              │              │                │              │
    │            │            │ update_order  │              │              │                │              │
    │            │            │  CAS half_cl  │              │              │                │              │
    │            │            ├──────────────►│              │              │                │              │
    │            │            │ XADD cmd_str  │              │              │                │              │
    │            │            │ (cascade=true)│              │              │                │              │
    │            │            ├──────────────►│              │              │                │              │
    │            │            │ WS cascade_   │              │              │                │              │
    │            │            │  triggered    │              │              │                │              │
    │            │◄────────────┤              │              │              │                │              │
    │            │            │               │              │              │                │  XREADGROUP  │
    │            │            │               │              │              │                ├─────────────►│
    │            │            │               │              │              │                │ mt5.Close()  │
    │            │            │               │              │              │                ├─────────────►│
    │            │            │               │              │              │                │ retcode 10009│
    │            │            │               │              │              │                │◄─────────────┤
    │            │            │               │  XADD resp   │              │                │              │
    │            │            │               │  (cascade=t) │              │                │              │
    │            │            │               │◄─────────────┴──────────────┴────────────────┤              │
    │            │            │ XREADGROUP    │              │              │                │              │
    │            │            │◄──────────────┤              │              │                │              │
    │            │            │ skip lock     │              │              │                │              │
    │            │            │ (cascade=t)   │              │              │                │              │
    │            │            │ update_order  │              │              │                │              │
    │            │            │  s_status←cls │              │              │                │              │
    │            │            ├──────────────►│              │              │                │              │
    │            │            │ both legs cls │              │              │                │              │
    │            │            │ status←closed │              │              │                │              │
    │            │            ├──────────────►│              │              │                │              │
    │            │            │ release_lock  │              │              │                │              │
    │            │            ├──────────────►│              │              │                │              │
    │            │            │ Alert 1 disp  │              │              │                │              │
    │            │            │ WS hedge_clsd │              │              │                │              │
    │            │◄────────────┤              │              │              │                │              │
    │  toast     │            │               │              │              │                │              │
    │ hedge_clsd │            │               │              │              │                │              │
    │◄───────────┤            │               │              │              │                │              │
    │            │            │               │              │              │                │              │
```

### §3.4.2 Path E — MT5 manual close (cascade to FTMO)

```
Operator   MT5 Terminal   Exness client                Redis           Server          FTMO client
    │           │              │                          │               │                 │
    │ tap Close │              │                          │               │                 │
    ├──────────►│              │                          │               │                 │
    │           │ position_id  │                          │               │                 │
    │           │ removed from │                          │               │                 │
    │           │ positions(   │                          │               │                 │
    │           │ )            │                          │               │                 │
    │           │              │ poll_loop 2s tick        │               │                 │
    │           │              │  positions_get()         │               │                 │
    │           │              ├─────────────────────────►│ (n/a — local) │                 │
    │           │              │ diff: pos_id missing     │               │                 │
    │           │              │ history_deals_get()      │               │                 │
    │           │              ├─────────────────────────►│ (n/a — local) │                 │
    │           │              │ reconstruct close info   │               │                 │
    │           │              │ XADD event_stream        │               │                 │
    │           │              │  (position_closed_ext,   │               │                 │
    │           │              │   close_reason=manual)   │               │                 │
    │           │              ├─────────────────────────►│               │                 │
    │           │              │                          │ XREADGROUP    │                 │
    │           │              │                          │◄──────────────┤                 │
    │           │              │                          │ check cmd_    │                 │
    │           │              │                          │  ledger by    │                 │
    │           │              │                          │  pos_id: none │                 │
    │           │              │                          │ Alert 3 disp  │                 │
    │           │              │                          │ acquire_lock  │                 │
    │           │              │                          │◄──────────────┤                 │
    │           │              │                          │ {1, owner}    │                 │
    │           │              │                          │──────────────►│                 │
    │           │              │                          │ update_order  │                 │
    │           │              │                          │  status←      │                 │
    │           │              │                          │  half_closed  │                 │
    │           │              │                          │  s_status←cls │                 │
    │           │              │                          │  p_status←clg │                 │
    │           │              │                          │◄──────────────┤                 │
    │           │              │                          │ XADD cmd_str  │                 │
    │           │              │                          │  ftmo close   │                 │
    │           │              │                          │  cascade=true │                 │
    │           │              │                          │◄──────────────┤                 │
    │           │              │                          │ XREADGROUP    │                 │
    │           │              │                          │──────────────────────────────►│ │
    │           │              │                          │               │ ProtoOAClose    │
    │           │              │                          │               │  PositionReq    │
    │           │              │                          │               │ ACCEPTED+FILLED │
    │           │              │                          │ XADD resp     │                 │
    │           │              │                          │  cascade=true │                 │
    │           │              │                          │◄──────────────────────────────┤ │
    │           │              │                          │ XREADGROUP    │                 │
    │           │              │                          │◄──────────────┤                 │
    │           │              │                          │ skip lock     │                 │
    │           │              │                          │ p_status←cls  │                 │
    │           │              │                          │ both closed   │                 │
    │           │              │                          │ status←closed │                 │
    │           │              │                          │ Alert 1       │                 │
    │           │              │                          │ release_lock  │                 │
    │           │              │                          │               │                 │
```

### §3.4.3 Race example — Path A vs Path D (triple action)

```
Operator         Frontend    Server                cTrader broker
   │                │           │                       │
   │ tick at SL     │           │                       │
   │ (market)       │           │                       │
   │                │           │                       │
   │ click close    │           │                       │
   │ FTMO at t0     │           │                       │
   ├───────────────►│           │                       │
   │                │ DELETE    │                       │
   │                ├──────────►│                       │
   │                │           │ XADD cmd_str          │
   │                │           ├──────────────────────►│
   │                │           │                       │
   │                │           │                       │ market crosses
   │                │           │                       │  SL at t0+5ms
   │                │           │                       │ broker fires
   │                │           │                       │  ProtoOAExec
   │                │           │                       │  closingOrder=t
   │                │           │                       │  orderType=SLTP
   │                │           │ (FTMO client)         │
   │                │           │ recv event            │
   │                │           │ XADD event_str        │
   │                │           │ (position_closed,     │
   │                │           │  reason=sl)           │
   │                │           │◄──────────────────────┤
   │                │           │                       │
   │                │           │ broker rejects        │
   │                │           │  api close cmd        │
   │                │           │ (POSITION_NOT_FOUND)  │
   │                │           │ XADD resp_str         │
   │                │           │ (close fail)          │
   │                │           │◄──────────────────────┤
   │                │           │                       │
   │                │           │ event_handler reads   │
   │                │           │ acquire_lock {1}      │
   │                │           │ OWNER → cascade       │
   │                │           │                       │
   │                │           │ response_handler reads│
   │                │           │ acquire_lock {0,owner}│
   │                │           │ LOSER → drop, ACK     │
   │                │           │                       │
```

The lock ensures the SL hit owns the cascade; the operator's API close loses cleanly with an INFO log.

---

## §4. Testing strategy preview

The implementation steps (4.1–4.12) will produce test artifacts; this section previews what step 4.6 (cascade) and step 4.11 (alerts) MUST cover so step 4.0 does not under-spec.

### §4.1 Cascade close test surface

| # | Test | Type | Verifies |
|---|---|---|---|
| C1 | Single trigger Path A → cascade close Exness | integration | Owner duties + lock released |
| C2 | Single trigger Path B → cascade close FTMO | integration | Symmetric to C1 |
| C3 | Single trigger Path C (manual cTrader) → cascade close Exness | integration | event_handler path |
| C4 | Single trigger Path D (SL hit) → cascade close Exness | integration | close_reason=sl propagation |
| C5 | Single trigger Path E (MT5 manual) → cascade close FTMO | integration | position_monitor detection |
| C6 | Concurrent A + D on same order | integration | Lock ownership; loser drops cleanly |
| C7 | Concurrent B + E on same order | integration | Symmetric to C6 |
| C8 | Cascade with secondary in `pending_open` (race during retry) | unit + integration | `cascade_cancel_pending` transient state |
| C9 | Cascade lock TTL extension during retry | unit | `extend_cascade_lock` keeps lock alive |
| C10 | Lock owner crash → orphan_sweep fires Alert 1.5 | integration | Sweep covers crash recovery edge |
| C11 | Volume formula edge: zero rounded | unit | Raise `secondary_volume_too_small` |
| C12 | Volume formula edge: over max | unit | Raise `secondary_volume_too_large` |
| C13 | Cascade from `secondary_failed` (operator escape hatch) | integration | No lock acquired; primary closes alone |
| C14 | Cascade response with `cascade_trigger=true` does NOT acquire lock | unit | Marker propagation |
| C15 | `position_not_found` close fail forces reconcile (D-4.0-7) | integration | Reconcile cmd injected |

### §4.2 Alert test surface

| # | Test | Type | Verifies |
|---|---|---|---|
| A1 | Alert 1 fires once on `hedge_closed` | integration | Dedup by `order_id` |
| A2 | Alert 1.5 fires on `secondary_failed` | integration | Always-on; ignores toggle |
| A3 | Alert 2 fires on path E stopout | integration | Severity CRITICAL |
| A4 | Alert 3 fires on path E manual; suppressed on cascade-driven manual | integration | Marker discriminator |
| A5 | Alert 4a fires on uncaught exception | integration | Exception middleware path |
| A6 | Alert 4b fires after 60s heartbeat miss | integration | Threshold + dedup |
| A7 | Alert 4b_recovery bypasses cooldown | unit | `bypass_cooldown=True` path |
| A8 | Alert 4c fires on broker disconnect | integration | New event types |
| A9 | Cooldown suppresses duplicate within 300s | unit | SET NX EX behavior |
| A10 | Toggle off skips dispatch | unit | HGET + branch |
| A11 | Missing chat_id → return False + WARN log | unit | Dev/CI fallback |
| A12 | Telegram HTTP 5xx → ERROR log, no retry | unit | Best-effort dispatch |
| A13 | Settings PUT updates HASH | integration | API contract |
| A14 | Test alert endpoint dispatches with bypass | integration | UI wiring |

### §4.3 Test environment notes

- **FTMO client** runs on Linux devcontainer in CI; integration tests use a stubbed cTrader server (existing pattern from Phase 3 step 3.4 smoke).
- **Exness client** runs only on Windows (D-4.0-2 — MT5 lib Windows-only). Phase 4 CI runs unit tests for Exness client logic on Linux; integration tests for the MT5 boundary run manually on CEO's Windows box.
- Cross-broker integration tests (C1–C7) use **dual stub**: stub FTMO bridge + stub Exness adapter. The stubs emit the same Redis stream entries a real client would.

---

## §5. Risk register (cascade + alerts subsystem)

Risks identified during step 4.0 design that require step-level mitigations.

| ID | Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|---|
| R1 | Cascade lock acquired but owner crashes before push | low | high | orphan_sweep (§1.B.6) + Alert 1.5 |
| R2 | `cmd_ledger` lost on server restart → false Alert 3 fires | medium | low | Accept; alert is informational. Phase 5 Redis-backed ledger. |
| R3 | MT5 position_monitor poll misses external close (race with broker stopout) | low | medium | 2s poll + history_deals_get fallback; D-4.0-7 reconcile force |
| R4 | Volume formula computed differently across server restart (config drift) | low | high | Symbol mapping checksum on startup; refuse to start if mismatch (step 4.5 acceptance) |
| R5 | Telegram outage hides critical alerts | medium | medium | Server log always carries payload; WS toasts independent path |
| R6 | Cooldown key conflict (test alert vs production alert collision) | low | low | Namespace prefix `test:` enforced; never reused for production |
| R7 | FTMO `position_not_found` race produces leg orphan in rare 3-way race | very low | high | orphan_sweep periodic + Alert 1.5 |
| R8 | Operator double-clicks "Close FTMO" → 2 cmds pushed | medium | low | `OrderService` checks `p_status` before push; second click 409s |
| R9 | Cascade close cmd succeeds at broker but resp lost in transit | very low | high | orphan_sweep + manual reconcile via UI button |
| R10 | Settings UI toggle race with in-flight alert | low | low | `is_enabled` read at dispatch time; brief race acceptable |

---

## §6. Glossary

| Term | Definition |
|---|---|
| **Cascade close** | Server-driven close of the surviving leg of a 2-leg hedge when one leg has closed. |
| **cascade_lock** | Redis STRING key serializing cascade ownership per `order_id` (§1.B). |
| **cascade_trigger** | Wire-level boolean field on cmd/resp stream entries indicating cascade vs operator origin (§1.F). |
| **CTC (Cascade Trigger Context)** | Normalized tuple `(order_id, broker, leg, reason, price, ts, request_id)` consumed by cascade owner. |
| **Composed status** | `order:{order_id}.status` derived from `(p_status, s_status)` per §1.C.2. |
| **Half-closed** | Composed status during the cascade window — one leg closed, other pending close. |
| **Leg hở (open leg)** | The orphan state: one leg open, other failed to open or close — operator-visible risk. |
| **Loser** | Handler that lost the `acquire_cascade_lock` race; logs INFO, ACKs, drops. |
| **Orphan sweep** | 60 s background scan for `half_closed` orders with expired cascade locks. |
| **Owner** | Handler that won the cascade lock; drives the secondary close. |
| **Path A/B/C/D/E** | Five cascade trigger ingress paths enumerated in §1.A. |
| **Pending open** | Transient `s_status` while secondary push is in-flight or in retry window. |
| **Risk ratio** | Per-pair multiplier scaling secondary volume relative to primary (§1.D). |
| **Self-check** | Markdown report produced at the end of each step, posted to Telegram self-check chat. |
| **Toggleable** | Alert type honoring `alert_settings` HASH; always-on alerts ignore the toggle. |

---

## §7. Cross-reference index

| Topic | Section here | Other docs |
|---|---|---|
| Cascade trigger paths | §1.A | `docs/10-flows.md` (Phase 4 update — step 4.12) |
| cascade_lock Lua spec | §1.B | `server/app/services/redis_service_lua.py` (existing UPDATE_ORDER_LUA reference) |
| D-090 expanded state machine | §1.C | `docs/DECISIONS.md` D-090, `docs/12-business-rules.md` §3 R-rules |
| Volume formula | §1.D | `docs/06-data-models.md` §pair_schema, `symbol_mapping_ftmo_exness.json` |
| `cascade_trigger` marker | §1.F | `docs/05-redis-protocol.md` §14.2 (Phase 4 extension) |
| Alert types catalog | §2.A | `docs/12-business-rules.md` §7 G1+G5 (alert triggers) |
| AlertService API | §2.B | `server/app/services/alert_service.py` (Phase 4 step 4.11) |
| Cooldown spec | §2.C | (none — new in Phase 4) |
| Settings schema | §2.D | `docs/06-data-models.md` §pair_schema (analogous), `docs/08-server-api.md` §settings |
| Telegram client config | §2.E | `server/app/config.py`, `.env.example` |
| Where alerts fire | §2.F | `docs/07-server-services.md` §5 order lifecycle |
| MT5 quirks | (spec preview) | `docs/mt5-execution-events.md` (skeleton; populated mid-phase per D-069) |

---

*End of design doc. Length target ≥1500 lines, ≥8000 words — see step-4.0 self-check.*
