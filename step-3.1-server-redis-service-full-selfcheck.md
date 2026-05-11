# Step 3.1 — Self-check report

- **Branch**: `step/3.1-server-redis-service-full`
- **Commit**: `9f176f0` (full: `9f176f0377ff20c6105de83c72e3b0d8518ee880`)
- **Started**: 2026-05-10 (right after step 2.10 merge to `main`)
- **Finished**: 2026-05-10T12:08Z

## Scope done

- §3.1 Stream / consumer-group helpers — `_create_group`, `setup_consumer_groups`, `push_command`, `read_responses`, `read_events`, `ack`. BUSYGROUP swallowed; `read_*` use `xreadgroup`/groupname=`server`/consumername=`server`; both also accept an optional `block_ms` (default 1000) so unit tests don't hang.
- §3.2 Pending tracking — `remove_pending`, `get_stuck_pending` (cutoff exclusive), `get_all_account_pairs`.
- §3.3 Order CRUD — `create_order` (HSET + SADD orders:by_status), `get_order`, `update_order` (Lua, atomic CAS + index swap), `list_orders_by_status`, `list_closed_orders` (paginated ZREVRANGE), `add_to_closed_history`, `find_order_by_request_id`, `find_order_by_p_broker_order_id`, `find_order_by_s_broker_order_id`, plus the helpers the spec called for: `link_request_to_order`, `link_broker_order_id`.
- §3.4 Position P&L — `set_position_pnl` (SETEX 600s, JSON), `get_position_pnl`, `add_snapshot` (ZSET + EXPIRE 600s lazy refresh), `get_snapshots`.
- §3.5 Heartbeat / account info — `get_client_status`, `get_all_client_statuses`, `get_account_info`.
- §3.6 Account management — `add_account`, `remove_account`, `get_all_account_ids`, `get_account_meta`, with broker + account_id format validation.
- §3.7 Settings — `get_settings`, `patch_settings`.
- §3.8 TypedDicts — `OrderHash` and `PositionPnlSnapshot` (both `total=False`); module-level `Broker` / `StreamKind` / `LegPrefix` literals; `_to_order_hash` helper centralizes the cast.

Lua script lives in the new file `server/app/services/redis_service_lua.py` and is registered eagerly in `RedisService.__init__` so the hot path doesn't branch on lazy init.

## Acceptance criteria checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | All Phase 1+2 tests still pass (110) | ✅ | `pytest -q` → 167 passed (110 prior + 57 new). No prior tests removed or renamed. |
| 2 | `mypy --strict` clean on the three new/modified files | ✅ | `mypy --strict app/services/redis_service.py app/services/redis_service_lua.py tests/services/test_redis_service.py` → `Success: no issues found in 3 source files`. The 3 pre-existing `hedger_shared` import-path errors documented in PROJECT_STATE were NOT in scope for this step. |
| 3 | All §3 methods exist with correct signatures | ✅ | `git show HEAD:server/app/services/redis_service.py` shows 25 new public methods + 2 helper functions matching `docs/07-server-services.md §1` (with one extension noted below: `read_responses`/`read_events` accept a `block_ms` keyword). |
| 4 | New test file with ≥30 tests | ✅ | `server/tests/services/test_redis_service.py` — 57 test cases (counting parametrized variants). Includes parametrized account_id format check (7 cases), CAS hit/miss/non-existent, two-coroutine concurrency test for the Lua, stream BUSYGROUP idempotency, pending-tracking boundary check, full account remove sweep. |
| 5 | All new tests pass | ✅ | `pytest -q tests/services/test_redis_service.py` → 57 passed. |
| 6 | `redis_service_lua.py` exists with `UPDATE_ORDER_LUA` | ✅ | Created (73 lines). Handles 3 cases: (a) order missing → 0; (b) CAS check requested + status mismatch → 0; (c) order exists + (no CAS or CAS hit) → apply HSET pairs + index swap if status changed → 1. Index swap reads `cur_status` inside Lua so even no-CAS callers don't have a TOCTOU window. |
| 7 | No `KEYS *` / `KEYS order:*` scans | ✅ | `grep -nE '^[^#].*KEYS[* ]' app/services/*.py` returns no hits. The only `KEYS` references are (a) Lua's `KEYS[1]` (not a scan command) and (b) doc comments. Iteration is set-based via `accounts:*` SMEMBERS. |
| 8 | No Phase 1+2 method body modified | ✅ | `git diff main..HEAD -- server/app/services/redis_service.py` shows ADDITIONS plus the import line replacement (which the spec explicitly allows: "Imports at top can be added — that's fine"). All existing method bodies (creds, symbol_config, ohlc, tick, pair CRUD) are byte-identical. |
| 9 | `setup_consumer_groups` defined but NOT wired to lifespan | ✅ | `grep -n setup_consumer_groups server/app/main.py` → no matches. Only references are in the new service file + the new test file. |
| 10 | No new FastAPI route, no new Pydantic API DTO, no client-package change | ✅ | `git diff --stat main..HEAD` shows only 4 files: `redis_service.py` (modified), `redis_service_lua.py` (new), `tests/services/__init__.py` (new), `tests/services/test_redis_service.py` (new). No router/schema/client touched. |
| 11 | Tests use `fakeredis` per existing conftest pattern | ✅ | `svc` fixture wraps the autouse `fake_redis` fixture from `server/tests/conftest.py`. `fakeredis[lua]>=2.24` is already pulled in (`pyproject.toml`); verified end-to-end (Lua `register_script`, streams `xgroup_create`/`xreadgroup`/`xack`, BUSYGROUP error path) with a one-shot script before writing real tests — no workaround / mock / skip needed. |
| 12 | Single commit, message format matches | ✅ | `git log --oneline -3` shows exactly one new commit on the branch (`9f176f0`); message body includes the exact bullet list specified in the prompt (`- add order CRUD with atomic CAS update via Lua`, etc.). |

## Files changed

```
$ git diff --stat main..HEAD
 server/app/services/redis_service.py        | 629 ++++++++++++++++++++++++++-
 server/app/services/redis_service_lua.py    |  73 +++
 server/tests/services/__init__.py           |   0
 server/tests/services/test_redis_service.py | 679 ++++++++++++++++++++++++++++
 4 files changed, 1380 insertions(+), 1 deletion(-)
```

The single deletion is the imports line (`-from typing import Any` → `+from typing import Any, Literal, TypedDict, cast`), explicitly allowed by the spec.

## Test results

```
$ pytest -q
........................................................................ [ 86%]
.......................                                                  [100%]
167 passed in 2.65s

$ pytest -q tests/services/test_redis_service.py
.........................................................                [100%]
57 passed in 0.37s
```

110 prior tests (Phase 1+2) + 57 new = 167. Zero failures, zero skips, zero xfails.

## Mypy result

```
$ mypy --strict app/services/redis_service.py app/services/redis_service_lua.py tests/services/test_redis_service.py
Success: no issues found in 3 source files
```

## Lint / format

```
$ ruff check .
All checks passed!

$ ruff format --check .
39 files already formatted
```

## Deviations from spec

1. **`read_responses` / `read_events` accept an optional `block_ms` keyword** (default 1000, matching spec). This is an additive parameter — callers that follow the spec exactly still work. Necessary so unit tests can pass `block_ms=10` to verify wiring without hanging the suite for 1s × number-of-tests.
2. **Account-id format `^[a-z0-9_]{3,64}$`** comes from this prompt and is *stricter* than `docs/05-redis-protocol.md §2` (`[a-zA-Z0-9_-]{1,32}`). Followed the prompt; logged as inline comment in `redis_service.py` near the regex constant. Flag for CTO: should `05-redis-protocol.md` be reconciled? Not changed in this step (would touch a doc outside the diff scope).
3. **Side-index key naming** (`request_id_to_order:{request_id}`, `{leg}_broker_order_id_to_order:{broker_order_id}`) is not pinned in `docs/06-data-models.md`. I picked names that read clearly and grouped them in dedicated helpers (`link_request_to_order`, `link_broker_order_id`, plus matching `find_*`). Documented inline.
4. **Lua single-instance assumption**: the script touches both `order:{id}` and `orders:by_status:{status}` keys but only declares `order:{id}` in `KEYS`. That's fine for single-instance Redis (D-006) but would break under Redis Cluster's slot routing. Inline comment flags this — Phase 5 hardening can revisit if Cluster is ever introduced.

## Issues / questions for CTO

1. `docs/05-redis-protocol.md §2` permits `account_id` as `[a-zA-Z0-9_-]{1,32}` while step 3.1 prompt + this implementation enforce `[a-z0-9_]{3,64}`. Which should `02-overview.md` / `06-data-models.md` ultimately reflect? I'd suggest aligning the protocol doc to the prompt (stricter) but didn't touch docs in this step.
2. The Lua script reads `cur_status` to compute index swap keys even when no CAS was requested. That's intentional (avoids a client-side TOCTOU race) but means the Lua does an extra HGET on every `update_order` whose patch doesn't include `status`. Trivial cost on Redis but noting in case a future profiler flags it.
3. `find_order_by_p_broker_order_id` / `find_order_by_s_broker_order_id` use separate side-indices (`p_broker_order_id_to_order:*` / `s_broker_order_id_to_order:*`) instead of a unified one. Reason: a primary cTrader positionId and a secondary MT5 ticket could in principle collide on the same numeric value, and an answer "which leg does this id refer to?" is cheaper to encode in the key prefix than in the value. Confirm CTO is happy with this naming before step 3.7 (response_handler) calls these.

## Self-verdict

**PASS.** All 12 acceptance criteria met. Single commit on the correct branch with the exact message format. Open items are advisory (deviation 1, 4, and the questions above) and don't block step 3.2.
