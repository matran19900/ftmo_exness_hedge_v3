# Step 3.3a — Self-check report

- **Branch**: `step/3.3a-shared-pytyped-marker`
- **Commit**: `f26c37e` (full: `f26c37e7af063eeec94ee1d4a5428dd4d6a9b6fa`)
- **Started**: 2026-05-11T00:40Z (right after step 3.3 merge)
- **Finished**: 2026-05-11T01:00Z

## Scope done

- §1.1 **PEP 561 marker.** Created empty file
  `shared/hedger_shared/py.typed` (0 bytes verified with `wc -c`).
- §1.2 **Build-data registration.** `shared/pyproject.toml` is the
  setuptools backend; added `[tool.setuptools.package-data]` with
  `hedger_shared = ["py.typed"]` (8-line addition including a comment).
- §1.3 **Silencer removal.** All 5 `# type: ignore[import-not-found]`
  comments on `hedger_shared.ctrader_oauth` imports were stripped — one
  file per silencer, no other change in those files. `grep` confirms
  zero remaining `hedger_shared.*` silencers anywhere in the tree
  (the only remaining silencer is the unrelated step-3.2
  `from scripts import init_account` in `tests/scripts/test_init_account.py`
  — different root cause, left alone per §1.3 constraint).
- §1.4 **Bonus cleanup.** The 5 pre-existing
  `hedger_shared.symbol_mapping` errors (3 in `app/` + 2 in `tests/`)
  documented in PROJECT_STATE all disappeared automatically after the
  marker was honored by the installer — exactly the PEP 561 intent.
- §1.5 **Verified across all 3 packages.** `mypy --strict` runs are
  clean on `apps/ftmo-client/` and `shared/`; the 3 remaining errors on
  `server/` are unrelated `test_config.py:48 Settings` call-arg ones
  that predate this step.

## Acceptance criteria checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | `shared/hedger_shared/py.typed` exists, empty (0 bytes) | ✅ | `wc -c shared/hedger_shared/py.typed` → `0 shared/hedger_shared/py.typed`. |
| 2 | `shared/pyproject.toml` registers `py.typed` in package data | ✅ | Diff adds 8 lines: a `[tool.setuptools.package-data]` block with `hedger_shared = ["py.typed"]` + a 4-line comment explaining intent. |
| 3 | All 5 step-3.3 silencers on `hedger_shared.ctrader_oauth` are removed | ✅ | `grep -rn "type: ignore\[import-not-found\]" apps/ftmo-client/ server/ shared/` returns no `hedger_shared` matches (the only remaining hit is on `from scripts import init_account` in `tests/scripts/test_init_account.py` — step-3.2 leftover, different root cause, explicitly out of scope per §1.3). |
| 4 | Server mypy strict clean — 3 pre-existing `hedger_shared.symbol_mapping` errors are gone | ✅ | Before: 8 errors (5 hedger_shared + 3 test_config Settings). After: 3 errors, all on `test_config.py:48` (`call-arg`) — Phase-1 pre-existing, unrelated to step 3.3a. All 5 hedger_shared errors vanished as expected. |
| 5 | ftmo-client mypy strict clean — was 0 with silencers, now 0 WITHOUT silencers | ✅ | `mypy --strict ftmo_client/ tests/` → `Success: no issues found in 18 source files`. |
| 6 | Server tests still pass (181) | ✅ | `pytest -q` → 181 passed in 2.69s. |
| 7 | ftmo-client tests still pass (27) | ✅ | `pytest -q` → 27 passed in 1.21s. |
| 8 | Ruff check + format clean | ✅ | `ruff check server/ apps/ftmo-client/ shared/` → All checks passed. `ruff format --check` → 63 files already formatted. |
| 9 | No file outside the allowed scope was touched | ✅ | `git diff --stat main..HEAD` shows exactly: `shared/hedger_shared/py.typed` (new, empty), `shared/pyproject.toml` (+8 lines), 5 source files (one `# type: ignore` comment removed each, +1/-1 each). Zero other changes. |
| 10 | Single commit with the exact message format | ✅ | `git log --oneline -3` shows one new commit (`f26c37e`); message body matches §0 verbatim. |

## Files changed

```
$ git diff --stat main..HEAD
 apps/ftmo-client/ftmo_client/oauth_storage.py          | 2 +-
 apps/ftmo-client/ftmo_client/scripts/run_oauth_flow.py | 2 +-
 apps/ftmo-client/tests/test_main_wiring.py             | 2 +-
 apps/ftmo-client/tests/test_oauth_storage.py           | 2 +-
 server/app/api/auth_ctrader.py                         | 2 +-
 shared/hedger_shared/py.typed                          | 0
 shared/pyproject.toml                                  | 8 ++++++++
 7 files changed, 13 insertions(+), 5 deletions(-)
```

## Mypy BEFORE and AFTER

### Before (parent of this commit, with step-3.3 silencers in place)

```
$ cd server && mypy --strict app/ tests/
app/services/symbol_whitelist.py:5: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
app/services/volume_calc.py:19: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
tests/test_symbol_whitelist.py:10: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
tests/test_config.py:48: error: Missing named argument "redis_url" for "Settings"  [call-arg]
tests/test_config.py:48: error: Missing named argument "jwt_secret" for "Settings"  [call-arg]
tests/test_config.py:48: error: Missing named argument "admin_password_hash" for "Settings"  [call-arg]
app/api/symbols.py:16: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
tests/test_volume_calc.py:18: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
Found 8 errors in 6 files (checked 42 source files)

$ cd apps/ftmo-client && mypy --strict ftmo_client/ tests/
Success: no issues found in 18 source files     # (clean only because 4 silencers hid the real errors)
```

### After (this commit, py.typed + compat editable install)

```
$ cd server && mypy --strict app/ tests/
tests/test_config.py:48: error: Missing named argument "redis_url" for "Settings"  [call-arg]
tests/test_config.py:48: error: Missing named argument "jwt_secret" for "Settings"  [call-arg]
tests/test_config.py:48: error: Missing named argument "admin_password_hash" for "Settings"  [call-arg]
Found 3 errors in 1 file (checked 42 source files)

$ cd apps/ftmo-client && mypy --strict ftmo_client/ tests/
Success: no issues found in 18 source files     # genuinely clean now

$ cd shared && mypy --strict hedger_shared/
Success: no issues found in 3 source files
```

**Delta**: 5 `hedger_shared.symbol_mapping` errors eliminated on the
server. 4 ftmo-client silencers eliminated. 1 server silencer
eliminated. Total: 10 type-system loose ends closed.

The 3 remaining server errors (`test_config.py:48` Settings call-arg)
are pre-existing, unrelated to this step, and explicitly out of scope
per §2 of the prompt.

## Test counts before / after

| | Before | After |
|---|---|---|
| Server tests | 181 passed | 181 passed |
| ftmo-client tests | 27 passed | 27 passed |
| **Total** | **208 passed** | **208 passed** |

Zero regressions.

## The exact 5 lines that had silencers removed

### 1. `server/app/api/auth_ctrader.py:20`

```diff
-from hedger_shared.ctrader_oauth import (  # type: ignore[import-not-found]
+from hedger_shared.ctrader_oauth import (
     build_authorization_url,
     exchange_code_for_token,
     fetch_trading_accounts,
 )
```

### 2. `apps/ftmo-client/ftmo_client/oauth_storage.py:20`

```diff
-from hedger_shared.ctrader_oauth import TokenResponse  # type: ignore[import-not-found]
+from hedger_shared.ctrader_oauth import TokenResponse
```

### 3. `apps/ftmo-client/ftmo_client/scripts/run_oauth_flow.py:38`

```diff
-from hedger_shared.ctrader_oauth import (  # type: ignore[import-not-found]
+from hedger_shared.ctrader_oauth import (
     build_authorization_url,
     exchange_code_for_token,
     fetch_trading_accounts,
 )
```

### 4. `apps/ftmo-client/tests/test_oauth_storage.py:9`

```diff
-from hedger_shared.ctrader_oauth import TokenResponse  # type: ignore[import-not-found]
+from hedger_shared.ctrader_oauth import TokenResponse
```

### 5. `apps/ftmo-client/tests/test_main_wiring.py:15`

```diff
-from hedger_shared.ctrader_oauth import TokenResponse  # type: ignore[import-not-found]
+from hedger_shared.ctrader_oauth import TokenResponse
```

## Deviations / install-mode caveat

**Setuptools editable mode and PEP 561.** The committed code change is
the correct PEP 561 fix (marker + package-data). However, **modern
setuptools editable installs (PEP 660, default since setuptools 64)
use a custom `MetaPathFinder` that mypy can't follow** — `py.typed`
is honored only when the editable install uses the legacy `.pth`
mechanism. I verified this by hand:

```bash
$ pip install -e shared/ --force-reinstall --no-deps        # modern (default)
# Result: mypy still reports 5 hedger_shared.symbol_mapping errors.

$ pip install -e shared/ --force-reinstall --no-deps \
    --config-settings editable_mode=compat                  # legacy .pth
# Result: mypy strict clean, all hedger_shared imports resolve.
```

I ran the `compat` form in this dev environment, which is why the
mypy output above is clean. **This is the working state in this
container.**

`/.devcontainer/post-create.sh:33` currently calls `pip install -e .`
in `shared/` without `editable_mode=compat`. A future devcontainer
rebuild would regress mypy back to the pre-fix state until the next
hand-run of the compat install — even though the marker + package-data
are correctly committed. Touching `post-create.sh` was outside the
scope-9 file list, so I deferred it.

### Recommended follow-up for CTO

One of the following one-line changes makes the fix durable across
container rebuilds:

1. **Update `.devcontainer/post-create.sh:33`** —
   `pip install -e . --config-settings editable_mode=compat`
   (smallest blast radius; same install mode the dev environment is
   already using).
2. **Add a `MYPYPATH` setting** to each consuming package — e.g. in
   `server/pyproject.toml` `[tool.mypy]` add `mypy_path = "../shared"`
   so mypy resolves `hedger_shared` directly from source regardless of
   install mode. Likewise in `apps/ftmo-client/pyproject.toml`.

Either is a tiny chore commit. The current commit's runtime + test
behavior is correct regardless — the caveat is purely about mypy's
discovery path in modern editable installs.

## Self-verdict

**PASS.** All 10 acceptance criteria met. Single commit on the correct
branch with the exact message format. Mypy delta: −5 errors
(server hedger_shared) and −5 hidden silencers (now genuinely
clean). The post-create.sh follow-up flagged in "Deviations" is
advisory and doesn't block step 3.4.
