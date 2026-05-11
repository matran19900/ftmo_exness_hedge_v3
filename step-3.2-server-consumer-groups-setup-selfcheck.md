# Step 3.2 — Self-check report

- **Branch**: `step/3.2-server-consumer-groups-setup`
- **Commit**: `3584962` (full: `3584962d0cd890e46a5ed87182dbc870c5d7e934`)
- **Started**: 2026-05-10T13:50Z (right after step 3.1 merge)
- **Finished**: 2026-05-10T14:47Z

## Scope done

- §1.1 **Lifespan wired**. `app/main.py` calls
  `redis_svc.setup_consumer_groups()` immediately after `init_redis()`
  finishes (which already pings) and before the symbol-whitelist load /
  market-data start. Failure raises through lifespan startup — no try/except.
  `setup_consumer_groups()` now returns `tuple[int, int]` (ftmo, exness
  counts), logged at INFO. Step 3.1 test updated; new zero-account test added.
- §1.2 **`scripts/init_account.py`** CLI: argparse subcommands `add` /
  `remove` / `list`, broker + account-id validation mirroring the regex in
  `RedisService`, fresh Redis connection per invocation closed in a
  `finally`, all output via injected `out` / `err` streams (so tests can
  capture without subprocess), exit codes 0/1/2 per spec.
- §1.3 **README**: new top-level section
  `## Account Management (Phase 3+)` with the 3 example commands, a
  restart-required note, and the exit-code legend. 19 lines added.

## Acceptance criteria checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | All Phase 1+2+3.1 tests still pass (≥167) | ✅ | `pytest -q` → 181 passed (167 prior + 1 new redis_service test for the zero-accounts return + 3 lifespan integration + 10 CLI smoke). Zero failures, zero skips. |
| 2 | `mypy --strict` clean on touched server files | ✅ | `mypy --strict app/main.py app/services/redis_service.py tests/` → **8 errors, all pre-existing** (5 hedger_shared import-not-found, 3 test_config Settings call-arg). Baseline (verified by `git stash` pre-step) is identical. NO new errors. |
| 3 | `mypy --strict scripts/init_account.py` clean | ✅ | `Success: no issues found in 1 source file`. Single inline `# type: ignore[no-untyped-call]` on `redis_asyncio.from_url` mirrors the runtime shape used elsewhere in the project. |
| 4 | `setup_consumer_groups()` called once in lifespan, between ping and market-data | ✅ | `grep -n setup_consumer_groups server/app/main.py` → import on line 27 + 1 call on line 73. Sequence: `init_redis` (which pings) → `setup_consumer_groups` → `symbol_whitelist.load_whitelist` → `market_data.start()`. |
| 5 | `setup_consumer_groups` returns `tuple[int, int]` | ✅ | `redis_service.py:302-322` — return statement `return len(ftmo_accs), len(exness_accs)`. Step 3.1 test `test_setup_consumer_groups_creates_three_streams_per_account` now asserts `counts == (1, 1)`; idempotent test asserts both runs return `(1, 0)`. |
| 6 | Lifespan integration test (`test_lifespan_integration.py`) | ✅ | Three tests: `test_lifespan_creates_groups_for_seeded_accounts` (seeds 2 ftmo + 1 exness, drives lifespan via `app.router.lifespan_context(app)`, asserts every (stream, group) tuple via XINFO GROUPS), `test_lifespan_starts_with_zero_accounts` (no seeds → server still starts), `test_lifespan_setup_consumer_groups_idempotent_across_restarts` (lifespan run twice → BUSYGROUP swallow keeps groups intact). |
| 7 | `scripts/init_account.py` exists with all 3 subcommands | ✅ | `python -m scripts.init_account --help` lists `add`, `remove`, `list`. `scripts/__init__.py` created (empty). |
| 8 | CLI smoke tests in `server/tests/scripts/test_init_account.py` (≥8 cases) | ✅ | **10 tests**: add happy + add-bad-broker (argparse choices) + add-bad-account-id-format + add-duplicate + list-empty + list-populated-with-status + list-filtered-by-broker + remove-dry-run + remove-with-yes + remove-nonexistent. |
| 9 | All new tests pass | ✅ | `pytest -q tests/scripts/test_init_account.py tests/test_lifespan_integration.py tests/services/test_redis_service.py` → 71 passed. |
| 10 | README has `## Account Management (Phase 3+)` with 3 examples + restart note | ✅ | Section inserted before "Working with Claude Code". 19 lines added (under the 25-line cap). |
| 11 | Only allowed files touched | ✅ | `git diff --stat main..HEAD` shows: README.md (modified +19), scripts/__init__.py (new, empty), scripts/init_account.py (new), server/app/main.py (modified +14/-1), server/app/services/redis_service.py (modified +12/-3 — return type only), server/tests/scripts/__init__.py (new, sys.path shim), server/tests/scripts/test_init_account.py (new), server/tests/services/test_redis_service.py (modified +21/-4 — step 3.1 test update + zero-account test), server/tests/test_lifespan_integration.py (new). Zero files outside this list. |
| 12 | Single commit, message format matches | ✅ | `git log --oneline -3` shows exactly one new commit (`3584962`); message body matches the exact bullet list specified in the prompt. |
| 13 | `ruff check .` and `ruff format --check .` clean | ✅ | `ruff check .` → All checks passed. `ruff format --check .` → 42 files already formatted. |

## Files changed

```
$ git diff --stat main..HEAD
 README.md                                   |  19 ++
 scripts/__init__.py                         |   0
 scripts/init_account.py                     | 270 ++++++++++++++++++++++++++++
 server/app/main.py                          |  14 +-
 server/app/services/redis_service.py        |  12 +-
 server/tests/scripts/__init__.py            |  17 ++
 server/tests/scripts/test_init_account.py   | 220 +++++++++++++++++++++++
 server/tests/services/test_redis_service.py |  21 ++-
 server/tests/test_lifespan_integration.py   | 131 ++++++++++++++
 9 files changed, 696 insertions(+), 8 deletions(-)
```

## Test counts

- Before step 3.2: 167 passing (110 prior phases + 57 step 3.1).
- After step 3.2: **181 passing** (+14 net):
  - +1 in `tests/services/test_redis_service.py` (`test_setup_consumer_groups_zero_accounts_returns_zeros`)
  - +3 in `tests/test_lifespan_integration.py` (new file)
  - +10 in `tests/scripts/test_init_account.py` (new file)
- Zero pre-existing tests modified semantically (only `setup_consumer_groups`-return assertions adjusted — required by criterion 5).

## Mypy output (full)

```
$ mypy --strict app/main.py app/services/redis_service.py tests/
app/services/symbol_whitelist.py:5: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
app/services/volume_calc.py:19: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
tests/test_symbol_whitelist.py:10: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
tests/test_config.py:48: error: Missing named argument "redis_url" for "Settings"  [call-arg]
tests/test_config.py:48: error: Missing named argument "jwt_secret" for "Settings"  [call-arg]
tests/test_config.py:48: error: Missing named argument "admin_password_hash" for "Settings"  [call-arg]
tests/test_volume_calc.py:18: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
app/api/symbols.py:16: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
Found 8 errors in 6 files (checked 21 source files)

$ mypy --strict ../scripts/init_account.py
Success: no issues found in 1 source file
```

All 8 errors are pre-existing (5 `hedger_shared` import path env-only, 3 `Settings` call-arg in `test_config.py:48` from a Phase 1 test that never gets called with real settings). Baseline confirmed via `git stash` against the parent of this commit — identical 8 errors before any of my changes.

## Ruff / format

```
$ ruff check .
All checks passed!

$ ruff format --check .
42 files already formatted
```

## Sample CLI output (against fakeredis stub)

The smoke run drove the production parser + `_dispatch` paths against a
local fakeredis (no LAN Redis touched).

```
$ python -m scripts.init_account add --broker ftmo --account-id ftmo_acc_001 --name "FTMO Challenge $100k"
OK Account added: ftmo / ftmo_acc_001 (enabled=true)
  Meta key: account_meta:ftmo:ftmo_acc_001
-> Restart server now so setup_consumer_groups() picks up this new account.
(exit code: 0)

$ python -m scripts.init_account list
== ftmo (1 accounts) ==
  ftmo_acc_001  "FTMO Challenge $100k"  enabled=true  status=offline
== exness (0 accounts) ==
(exit code: 0)

$ python -m scripts.init_account remove --broker ftmo --account-id ftmo_acc_001
Would remove: ftmo / ftmo_acc_001 (set membership + meta hash + heartbeat key)
Pass --yes to confirm.
(exit code: 2)

$ python -m scripts.init_account remove --broker ftmo --account-id ftmo_acc_001 --yes
OK Account removed: ftmo / ftmo_acc_001
  Note: existing orders referencing this account are NOT deleted (out of scope per docs).
(exit code: 0)
```

(`OK` / `->` substitute for `✓` / `→` to dodge any Windows console encoding surprises.)

## Deviations from spec

1. **CLI tests bypass `main()`'s `asyncio.run`.** `pytest-asyncio` already
   runs tests inside an event loop, and `asyncio.run` rejects re-entry
   into a running loop. The tests parse argv via the production parser
   then `await init_account._dispatch(...)` directly. Argparse + handlers
   are still the production code paths; only the outer `asyncio.run`
   wrapper isn't exercised. The `argparse choices=` rejection test
   (`test_add_rejects_bad_broker_pre_argparse`) is sync because argparse
   raises `SystemExit` during `parse_args` before any async code runs.
2. **Patching `init_redis` in two namespaces.** `app/main.py` uses
   `from app.redis_client import init_redis`, so the lifespan looks up
   `app.main.init_redis`. The lifespan integration test patches both
   `app.main.init_redis` and `app.redis_client.init_redis` so any future
   refactor that moves the import keeps the test working. Documented in
   the test's fixture docstring.
3. **`tests/scripts/__init__.py` mutates `sys.path`** to add the repo
   root so `from scripts import init_account` resolves under pytest's
   `server/` rootdir. Alternative would have been `pythonpath = [".."]`
   in `pyproject.toml`, but criterion 11 lists "ONLY" the files in the
   diff — adjusting pytest config wasn't in scope. The shim is localized
   to the test subpackage. Flag for CTO if a different placement is
   preferred (e.g. moving to a top-level conftest in step 3.3).

## Issues / questions for CTO

1. **Should `pyproject.toml` get `pythonpath = [".."]` instead of the
   `tests/scripts/__init__.py` sys.path shim?** Both work; the shim is
   narrower but the config setting is more idiomatic. The shim avoids
   touching `pyproject.toml` (which the criterion 11 file list doesn't
   list as in-scope). Happy to swap if CTO prefers.
2. **Restart-required UX after `init_account add/remove`.** Phase 4 was
   already going to do runtime account add/remove via API, so this is
   just a Phase-3 placeholder. The CLI prints an explicit `-> Restart
   server now…` reminder so CEO doesn't wonder why a freshly-added
   account isn't getting commands. Confirm CTO is happy with that
   message before step 3.3 builds on it.
3. **Step 3.1 self-check leftover file.** I noticed
   `step-3.1-server-redis-service-full-selfcheck.md` still sitting at
   the repo root from the previous step. Left it untracked (NOT in this
   commit). Should we add a `.gitignore` rule for `step-*-selfcheck.md`?
   Out of scope here; flagging in case CTO wants it consolidated in step
   3.3 or 3.14 docs sync.

## Self-verdict

**PASS.** All 13 acceptance criteria met. Single commit on the correct
branch with the exact message format. The three deviations above are
test-side workarounds that preserve the production code paths; the
three CTO questions are advisory and don't block step 3.3.
