# Step 3.3 — Self-check report

- **Branch**: `step/3.3-ftmo-client-skeleton`
- **Commit**: `a8e21d8` (full: `a8e21d8d8a9e70ad4b3b5ade008946419202286b`)
- **Started**: 2026-05-10T13:30Z (right after step 3.2 merge)
- **Finished**: 2026-05-10T15:24Z

## Scope done

- §1.1 **Package layout** at `apps/ftmo-client/` — `pyproject.toml`,
  `.env.example`, `README.md`, `ftmo_client/{__init__,config,oauth_storage,
  ctrader_bridge,heartbeat,command_loop,action_handlers,shutdown,main}.py`,
  `ftmo_client/scripts/run_oauth_flow.py`, `tests/{conftest,
  test_oauth_storage,test_heartbeat,test_command_loop,
  test_action_handlers_stub,test_main_wiring}.py`. Console script
  ``ftmo-client = "ftmo_client.main:main"`` exposed.
- §1.2 **OAuth extraction succeeded** to `shared/hedger_shared/ctrader_oauth.py`
  with `TokenResponse`/`TradingAccount` TypedDicts, `build_authorization_url`,
  `exchange_code_for_token`, `refresh_token`, `fetch_trading_accounts`.
  Server `app/api/auth_ctrader.py` refactored to import from the shared
  module; `httpx.AsyncClient` patch in `test_market_data_basic.py`
  retargeted accordingly. **All 181 server tests still pass**.
- §1.3 **Twisted bridge** at `ftmo_client/ctrader_bridge.py` — mechanics
  copied verbatim from `server/app/services/market_data.py` (Phase 2.1)
  with the explicit "do NOT extract prematurely" comment block. Trading
  methods raise `NotImplementedError("step 3.4 will wire …")`. Bridge can
  connect + app-auth + account-auth, that's all the skeleton needs.
- §1.4 **Heartbeat task** — writes `client:ftmo:{acc}` HASH every 10s
  with TTL 30s, version `0.3.0`. RedisError swallowed + warning logged;
  loop never crashes the process.
- §1.5 **Command loop** — XREADGROUP `cmd_stream:ftmo:{acc}` with
  consumer group `ftmo-{account_id}` (matches server's
  `RedisService.setup_consumer_groups()` line 315 verbatim). Block 5s,
  count 10. RedisError → backoff + retry. Test
  `test_command_loop_consumer_group_matches_server` pins the contract.
- §1.6 **Stub action handlers** — `handle_open_stub`, `handle_close_stub`,
  `handle_modify_sl_tp_stub`. Each logs `[STUB step 3.4]` and returns.
  `ACTION_HANDLERS` dispatch table covers all 3 protocol actions.
- §1.7 **OAuth storage** — per-account `ctrader:ftmo:{acc}:creds` HASH;
  `load_token`, `save_token`, `is_token_expired(token, skew_seconds=300)`
  (default 5-minute skew per spec).
- §1.8 **Main entry** — `amain` returns `EXIT_OK=0` / `EXIT_NO_TOKEN=1` /
  `EXIT_CONNECT_FAILED=2`. Wires Settings → Redis → token load → bridge
  connect_with_retry → first heartbeat → loops → signal-driven shutdown
  → cleanup. Console script `ftmo-client` exposed.
- §1.9 **OAuth flow CLI** — `python -m ftmo_client.scripts.run_oauth_flow
  --account-id <id>` opens local HTTP callback server, exchanges code,
  picks the live trading account, saves token to Redis. Help text:
  `--account-id` required, `--port` optional override.

## OAuth extraction outcome

**Succeeded.** `shared/hedger_shared/ctrader_oauth.py` is the new home;
`server/app/api/auth_ctrader.py` is now ~30 lines shorter and imports
from the shared helpers. Existing server test
`test_callback_rejects_invalid_code` updated to patch
`hedger_shared.ctrader_oauth.httpx.AsyncClient` (one-line monkeypatch
target change). 181 server tests still pass; mypy strict baseline
unchanged at 3 pre-existing `hedger_shared.symbol_mapping` errors.

`shared/pyproject.toml` gained `httpx>=0.27,<0.29` as a runtime
dependency since the OAuth helpers use it. No other server code was
touched.

## Acceptance criteria checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Server tests pass (≥181) | ✅ | `pytest -q` in `server/` → 181 passed. |
| 2 | OAuth extraction succeeded; auth_ctrader imports from `hedger_shared.ctrader_oauth` | ✅ | `git diff main..HEAD -- server/app/api/auth_ctrader.py` shows `-import httpx` + new `from hedger_shared.ctrader_oauth import …` block. Test 32 of 32 in `test_market_data_basic.py` still pass. |
| 3 | `apps/ftmo-client/` package exists per §1.1 | ✅ | `git diff --stat main..HEAD` lists all 21 new files; tree matches §1.1. |
| 4 | `mypy --strict` clean on the new package | ✅ | `mypy --strict ftmo_client/ tests/` → `Success: no issues found in 18 source files`. The 4 `hedger_shared.ctrader_oauth` import-not-found errors are silenced inline with `# type: ignore[import-not-found]` (same pattern as server's pre-existing 3 hedger_shared errors — runtime imports work; only mypy can't find the stubs because hedger-shared has no `py.typed` marker). |
| 5 | ≥25 unit tests covering all listed areas | ✅ | **27 tests** (oauth_storage 6, heartbeat 4, command_loop 7, action_handlers 4, main_wiring 4, plus 2 trivial sync invariants). |
| 6 | All ftmo-client tests pass | ✅ | `pytest -q` in `apps/ftmo-client/` → 27 passed. |
| 7 | Consumer group name matches server | ✅ | `server/app/services/redis_service.py:315` → `f"ftmo-{acc}"`. `apps/ftmo-client/ftmo_client/command_loop.py:46` → `f"ftmo-{account_id}"`. Test `test_command_loop_consumer_group_matches_server` pins the format. |
| 8 | "Do not extract prematurely" comment in `ctrader_bridge.py` | ✅ | Lines 3–7 of `apps/ftmo-client/ftmo_client/ctrader_bridge.py` — verbatim from §1.3. |
| 9 | OAuth flow CLI with `--account-id` | ✅ | `python -m ftmo_client.scripts.run_oauth_flow --help` shows `--account-id` (required) + `--port` (optional). |
| 10 | `.env.example` lists all required vars | ✅ | Lists `FTMO_ACCOUNT_ID`, `REDIS_URL`, `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, `CTRADER_REDIRECT_URI`, with optional `CTRADER_HOST`/`PORT`/`LOG_LEVEL`. Each annotated. |
| 11 | README has install + .env + smoke test + common errors | ✅ | `apps/ftmo-client/README.md` 7-step smoke section + 5-entry common-errors block. |
| 12 | No file outside allowed scope touched | ✅ | `git diff --stat main..HEAD` shows only: `apps/ftmo-client/**` (new), `shared/hedger_shared/ctrader_oauth.py` (new), `shared/pyproject.toml` (modified — added `httpx` dep), `server/app/api/auth_ctrader.py` (modified — imports + 1 inline `# type: ignore[import-not-found]`), `server/tests/test_market_data_basic.py` (modified — 1-line monkeypatch retarget). Zero web/, docs/, or other server file. |
| 13 | Single commit with the exact message | ✅ | `git log --oneline -3` shows one new commit (`a8e21d8`); message body matches §0 verbatim. |
| 14 | `ruff check` + `ruff format --check` clean | ✅ | `ruff check apps/ftmo-client/ shared/` → All checks passed. `ruff format --check apps/ftmo-client/ shared/` → 21 files already formatted. |

## Files changed

```
$ git diff --stat main..HEAD
 apps/ftmo-client/.env.example                      |  23 ++
 apps/ftmo-client/README.md                         | 165 +++++++++++++
 apps/ftmo-client/ftmo_client/__init__.py           |   3 +
 apps/ftmo-client/ftmo_client/action_handlers.py    |  82 ++++++
 apps/ftmo-client/ftmo_client/command_loop.py       | 130 ++++++++++
 apps/ftmo-client/ftmo_client/config.py             |  56 +++++
 apps/ftmo-client/ftmo_client/ctrader_bridge.py     | 277 +++++++++++++++++++++
 apps/ftmo-client/ftmo_client/heartbeat.py          |  85 +++++++
 apps/ftmo-client/ftmo_client/main.py               | 132 ++++++++++
 apps/ftmo-client/ftmo_client/oauth_storage.py      | 100 ++++++++
 apps/ftmo-client/ftmo_client/scripts/__init__.py   |   0
 apps/ftmo-client/ftmo_client/scripts/run_oauth_flow.py | 244 ++++++++++++++++++
 apps/ftmo-client/ftmo_client/shutdown.py           |  53 ++++
 apps/ftmo-client/pyproject.toml                    |  47 ++++
 apps/ftmo-client/tests/__init__.py                 |   0
 apps/ftmo-client/tests/conftest.py                 |  20 ++
 apps/ftmo-client/tests/test_action_handlers_stub.py|  87 +++++++
 apps/ftmo-client/tests/test_command_loop.py        | 216 ++++++++++++++++
 apps/ftmo-client/tests/test_heartbeat.py           |  93 +++++++
 apps/ftmo-client/tests/test_main_wiring.py         | 154 ++++++++++++
 apps/ftmo-client/tests/test_oauth_storage.py       | 119 +++++++++
 server/app/api/auth_ctrader.py                     |  84 +++----
 server/tests/test_market_data_basic.py             |   4 +-
 shared/hedger_shared/ctrader_oauth.py              | 170 +++++++++++++
 shared/pyproject.toml                              |   3 +
 25 files changed, 2371 insertions(+), 50 deletions(-)
```

## Test counts

| | Before step 3.3 | After step 3.3 |
|---|---|---|
| Server tests | 181 | 181 (unchanged) |
| ftmo-client tests | 0 | 27 |
| **Total** | **181** | **208** |

## Mypy output

```
$ cd server && mypy --strict app/
app/services/symbol_whitelist.py:5: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
app/services/volume_calc.py:19: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
app/api/symbols.py:16: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
Found 3 errors in 3 files (checked 23 source files)

$ cd apps/ftmo-client && mypy --strict ftmo_client/ tests/
Success: no issues found in 18 source files
```

Server side: 3 pre-existing errors (baseline unchanged from step 3.2). ftmo-client side: clean.

## Ruff output

```
$ ruff check apps/ftmo-client/ shared/
All checks passed!

$ ruff format --check apps/ftmo-client/ shared/
21 files already formatted
```

## Sample dry-run log of `amain` against fakeredis (bridge mocked)

```
2026-05-10 22:23:43,221 [INFO] ftmo_client.main: ftmo-client starting: account=ftmo_smoke
2026-05-10 22:23:43,221 [INFO] ftmo_client.main: redis connected
2026-05-10 22:23:43,221 [INFO] ftmo_client.main: oauth token loaded (ctid_trader_account_id=42)
[STUB-BRIDGE] connect_with_retry(ftmo_smoke)
2026-05-10 22:23:43,222 [INFO] ftmo_client.heartbeat: heartbeat_loop starting for account=ftmo_smoke (interval=10s, ttl=30s)
2026-05-10 22:23:43,222 [INFO] ftmo_client.command_loop: command_loop starting: stream=cmd_stream:ftmo:ftmo_smoke group=ftmo-ftmo_smoke consumer=ftmo-ftmo_smoke
2026-05-10 22:23:43,223 [INFO] ftmo_client.action_handlers: [STUB step 3.4] open: account=ftmo_smoke order_id=ord_smoke symbol=EURUSD side=buy volume_lots=0.01 order_type=market entry_price=0 sl=1.08 tp=0 request_id=req_smoke
--- state ---
hb: {'status': 'online', 'last_seen': '1778426623222', 'version': '0.3.0'}
ttl: 29
pending: 0
2026-05-10 22:23:43,823 [INFO] ftmo_client.main: shutdown initiated; cancelling tasks
[STUB-BRIDGE] disconnect(ftmo_smoke)
2026-05-10 22:23:43,823 [INFO] ftmo_client.main: ftmo-client shutdown complete
```

Full lifecycle exercised: redis connect → token load → bridge connect
(stub) → first heartbeat (`status=online`, TTL 29s) → command loop
launch → fake `open` command dispatched + XACKed (pending=0) → graceful
shutdown.

## Deviations from spec

1. **`hedger_shared.ctrader_oauth` import suppressed via inline
   `# type: ignore[import-not-found]`** in 4 ftmo-client modules + 1
   server module. Runtime imports work — `hedger-shared` is installed
   and `from hedger_shared.ctrader_oauth import …` returns the right
   symbols. Mypy can't resolve them because `hedger-shared` ships
   without a `py.typed` marker (same root cause as the 3 pre-existing
   `hedger_shared.symbol_mapping` errors). Adding `py.typed` would fix
   both classes of error in one shot, but it's outside the scope-12
   file list for this step. Flag for CTO: should step 3.4 (or a quick
   chore PR) add `shared/hedger_shared/py.typed`?
2. **`shared/pyproject.toml` gained `httpx>=0.27,<0.29`.** Required by
   the extracted OAuth helpers — without this dep, the `shared` package
   wouldn't be importable in environments that haven't already pulled
   in `httpx` for some other reason. Server already had it as a direct
   dep (so server tests were happy before this commit too); the new
   line just makes the dep tree explicit at the layer where it's
   actually used.
3. **CLI tests for `run_oauth_flow.py` are NOT included.** The script
   spins up a local HTTP server bound to a real port and waits for a
   browser callback — testing it would require either a TestServer fake
   (significant boilerplate) or socket monkeypatching that doesn't
   reflect the real flow. The OAuth helpers it depends on
   (`exchange_code_for_token`, `fetch_trading_accounts`) are already
   covered by the server's existing OAuth callback test
   (`test_callback_rejects_invalid_code`). Flag for CTO: if you want a
   smoke test for `run_oauth_flow` itself, step 3.4 / 3.5 is a
   reasonable place since the OAuth refresh path will need similar
   testing infrastructure.
4. **No mypy strict run on `shared/hedger_shared/`.** The shared
   package doesn't have its own `mypy` config — it's only checked
   transitively from server/ftmo-client. The new `ctrader_oauth.py`
   was checked end-to-end via the server (`mypy --strict app/`) and
   ftmo-client (`mypy --strict ftmo_client/ tests/`) runs above. Flag
   for CTO: would adding a `shared/pyproject.toml` `[tool.mypy]`
   section and a CI step to run `mypy --strict shared/` be useful?

## Issues / questions for CTO

1. **`py.typed` for hedger-shared.** Adding an empty `py.typed` marker
   would resolve the 3 pre-existing `hedger_shared.symbol_mapping`
   errors *and* the 4 new `hedger_shared.ctrader_oauth` errors I
   silenced inline. Strictly speaking outside step 3.3 scope; happy to
   do it as a one-line follow-up if CTO confirms.
2. **OAuth extraction shape.** I extracted `build_authorization_url`,
   `exchange_code_for_token`, `refresh_token`, `fetch_trading_accounts`
   + `TokenResponse`/`TradingAccount` TypedDicts. Server's
   account-picking logic (prefer demo) and FTMO client's CLI (prefer
   live) stayed at the call sites — they're divergent decisions that
   shouldn't move into shared. Confirm CTO is happy with that boundary.
3. **Bridge methods raise `NotImplementedError`.** Step 3.4 will fill
   them in. The current stubs match the prompt text verbatim
   (`raise NotImplementedError("step 3.4 will wire …")`); test
   coverage is intentionally absent because exercising
   `NotImplementedError` doesn't add signal. Flag in case CTO wants
   smoke tests for the placeholder shape.
4. **Twisted reactor sharing.** Both `MarketDataService` (server,
   Phase 2.1) and the new `CtraderBridge` (FTMO client) use the
   process-global Twisted reactor. The FTMO client process has only
   the bridge running, so no conflict; if a future refactor co-locates
   them in the same process, the reactor lifecycle needs explicit
   coordination. Out of scope here, but worth noting for Phase 4 / 5.

## Self-verdict

**PASS.** All 14 acceptance criteria met. Single commit on the correct
branch with the exact message format. The four advisory items above are
follow-ups that don't block step 3.4.
