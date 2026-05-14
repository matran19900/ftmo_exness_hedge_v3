# Step 4.A.2 — Mapping Cache Repository — Self-check

## 1. Coordinates

| Item | Value |
|---|---|
| Branch | `step/4.A.2-mapping-cache-repository` |
| Cut from | `main` HEAD `1d7b46e` (tag `step-4.A.1`) |
| Start | 2026-05-14T01:08:00Z |
| Finish | 2026-05-14T01:27:00Z |
| Commit | (created in step §7 below) |

## 2. Scope done

| § | Deliverable | Done |
|---|---|---|
| §2.1 | Folder `server/data/symbol_mapping_cache/` + `.gitkeep` | ✅ |
| §2.2 | Pydantic schemas (`RawSymbolEntry` 8 / `MappingEntry` 10 / `SymbolMappingCacheFile` 8 fields) | ✅ |
| §2.3 | `MappingCacheRepository` class + `compute_signature` free function | ✅ |
| §2.4 | Lifespan integration: instantiate + sweep + `list_all()` + log | ✅ |
| §2.5 | `Settings.symbol_mapping_cache_dir` + `.env.example` + `devcontainer.json` | ✅ |
| §2.6 | DI getter `get_mapping_cache_repository` registered (`app/dependencies/mapping_cache.py`) | ✅ |
| §2.7 | 50 unit tests (target ≥35) covering all sub-sections §2.7.1–§2.7.8 | ✅ |
| §2.8 | NO Redis interaction (Repository is file-layer only) | ✅ |

## 3. Acceptance criteria checklist

| # | Criterion | Evidence |
|---|---|---|
| 1 | Folder `server/data/symbol_mapping_cache/` exists | `ls server/data/symbol_mapping_cache/` → `.gitkeep` |
| 2 | `.gitkeep` committed | created at `server/data/symbol_mapping_cache/.gitkeep` (no content); `.gitignore` already has `!.gitkeep` whitelist line |
| 3 | `mapping_cache_schemas.py` exists | `server/app/services/mapping_cache_schemas.py` (90 lines) |
| 4 | `RawSymbolEntry` 8 fields, Pydantic v2 strict | `extra="forbid", strict=True`; fields = name/contract_size/digits/pip_size/volume_min/volume_step/volume_max/currency_profit |
| 5 | `MappingEntry` 10 fields, strict, D-4.A.0-2 volume_step/min/max | fields = ftmo/exness/match_type/contract_size/pip_size/pip_value/quote_ccy + exness_volume_step/min/max |
| 6 | `SymbolMappingCacheFile` 8 fields with `schema_version` | schema_version/signature/created_at/updated_at/created_by_account/used_by_accounts/raw_symbols_snapshot/mappings |
| 7 | `mapping_cache_repository.py` exists | 266 lines |
| 8 | Repository has 8 public methods | `read`, `read_filename`, `write`, `exists`, `list_all`, `list_filenames`, `signature_index`, `sweep_temp_artifacts` (+ `cache_dir` property) |
| 9 | `compute_signature` free function, sorted+JSON deterministic | `sorted(s.name) → json.dumps separators=(",",":") → sha256` |
| 10 | Atomic write: tempfile + rename + `.bak` + per-sig lock | §8 algorithm in `write()`; tests `test_write_overwrite_creates_bak`, `test_write_concurrent_*` |
| 11 | `sweep_temp_artifacts` removes `.tmp` >1h, `.bak` >7d | tests `test_old_tmp_removed`, `test_recent_tmp_preserved`, `test_old_bak_removed`, `test_recent_bak_preserved` (all pass) |
| 12 | Lifespan instantiates repo + logs counts | see §7 boot log |
| 13 | Lifespan calls `sweep_temp_artifacts` | log line `tmp_swept=0 bak_swept=0` |
| 14 | Lifespan calls `list_all()` + logs `cache_count` | log line `mapping_cache_repository.loaded cache_count=0` |
| 15 | `Settings.symbol_mapping_cache_dir` added | `server/app/config.py` lines 28-34 |
| 16 | `.env.example` updated | `SYMBOL_MAPPING_CACHE_DIR=...` line 12 |
| 17 | `devcontainer.json` updated | containerEnv key `SYMBOL_MAPPING_CACHE_DIR` |
| 18 | DI getter registered | `app/dependencies/mapping_cache.py::get_mapping_cache_repository` |
| 19 | ≥35 unit tests pass | **50 passed in 0.34s** (target met) |
| 20 | Schema validation tests (extra=forbid) pass | included in 19 |
| 21 | Concurrent same-sig serialised | `test_write_concurrent_same_signature_serialised` PASSED |
| 22 | Concurrent different-sig parallel | `test_write_concurrent_different_signatures_parallel` PASSED |
| 23 | Crashed tempfile sweep with synthetic age | `os.utime` + `test_old_tmp_removed` etc. PASSED |
| 24 | Server boots without error | §7 below |
| 25 | Total server tests pass (492 baseline + 50 new) | **542 passed in 9.23s** |
| 26 | mypy server clean (delta) | 3 pre-existing errors (`hedger_shared.*` import) verified via `git stash` to exist on `main` HEAD; **no new mypy errors** |
| 27 | ruff server clean | `All checks passed!` after autofix (UP017 timezone.utc → UTC) |
| 28 | FTMO 177 + Exness 33 regression untouched | `177 passed`, `33 passed` |
| 29 | Commit message per §0 format | done in step §7 |

## 4. Files changed

```
 .devcontainer/devcontainer.json |  3 ++-
 .env.example                    |  1 +
 server/app/config.py            |  6 ++++++
 server/app/main.py              | 20 ++++++++++++++++++++
```

New files:
```
 server/app/dependencies/mapping_cache.py        (   21 lines)
 server/app/services/mapping_cache_repository.py (  266 lines)
 server/app/services/mapping_cache_schemas.py    (   90 lines)
 server/data/symbol_mapping_cache/.gitkeep       (    0 lines)
 server/tests/test_mapping_cache_repository.py   (  690 lines)
```

## 5. Test counts

| Suite | Before | After | Δ |
|---|---|---|---|
| Server (`server/tests/`) | 492 | **542** | +50 |
| FTMO client (`apps/ftmo-client/tests/`) | 177 | 177 | 0 (untouched) |
| Exness client (`apps/exness-client/tests/`) | 33 | 33 | 0 (untouched) |

## 6. Toolchain output

### 6.1 pytest server (full)
```
=========== 542 passed in 9.23s ===========
```

### 6.2 pytest mapping_cache_repository.py (focused)
```
collected 50 items
... 50 PASSED ...
=========== 50 passed in 0.34s ===========
```

### 6.3 mypy server
```
$ .venv/bin/mypy app/
app/services/volume_calc.py:19: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
app/api/symbols.py:23: error: Cannot find implementation or library stub for module named "hedger_shared.symbol_mapping"  [import-not-found]
app/api/auth_ctrader.py:20: error: Cannot find implementation or library stub for module named "hedger_shared.ctrader_oauth"  [import-not-found]
Found 3 errors in 3 files (checked 37 source files)
```
**3 errors are pre-existing on `main` HEAD** — verified by `git stash; mypy app/` (same 3 errors). No new mypy errors introduced by step 4.A.2. Recorded as deviation D-4.A.2-1 below.

### 6.4 ruff server
```
$ .venv/bin/ruff check .
All checks passed!
```

### 6.5 FTMO + Exness client regression
```
apps/ftmo-client/tests:   177 passed in 2.78s
apps/exness-client/tests:  33 passed in 0.67s
```

## 7. Server boot verification

Driven via `app.router.lifespan_context(app)` with `init_redis` monkeypatched to fakeredis (same pattern as `test_lifespan_integration.py`). Captured stdout:

```
2026-05-14 08:25:54,035 [INFO] app.main: setup_consumer_groups: created groups for 0 ftmo + 0 exness accounts
2026-05-14 08:25:54,036 [INFO] app.main: Server ready (redis=redis://192.168.88.4:6379/2, symbols=117)
2026-05-14 08:25:54,037 [INFO] app.main: mapping_cache_repository.initialized cache_dir=/workspaces/ftmo_exness_hedge_v3/server/data/symbol_mapping_cache tmp_swept=0 bak_swept=0
2026-05-14 08:25:54,037 [INFO] app.main: mapping_cache_repository.loaded cache_count=0
2026-05-14 08:25:54,038 [INFO] app.main: Server shutdown complete
```

Both required log lines present. `tmp_swept=0 bak_swept=0` because the cache dir contains only `.gitkeep`.

## 8. Sample tests inline

### 8.1 Concurrent same-sig serialisation (`test_write_concurrent_same_signature_serialised`)
```python
@pytest.mark.asyncio
async def test_write_concurrent_same_signature_serialised(
    self, repo: MappingCacheRepository, tmp_path: Path
) -> None:
    cf = _cache_file(signature="ser")
    held = repo._lock_for("ser")
    await held.acquire()
    try:
        task = asyncio.create_task(repo.write(cf))
        await asyncio.sleep(0.05)
        assert not task.done(), "writer should be blocked on locked sig"
    finally:
        held.release()
    await task
    assert (tmp_path / "exn_001_ser.json").is_file()
```
Strategy: pre-acquire the per-signature lock manually, fire a writer, assert it's still pending, release, then wait. Deterministic — no race.

### 8.2 Sweep tempfile age threshold (`test_old_tmp_removed`)
```python
def test_old_tmp_removed(
    self, repo: MappingCacheRepository, tmp_path: Path
) -> None:
    old = tmp_path / "exn_x_oldsig.json.tmp"
    old.write_text("crashed", encoding="utf-8")
    _set_mtime(old, 7200)  # 2h > 1h threshold
    result = repo.sweep_temp_artifacts()
    assert result["tmp_removed"] == 1
    assert not old.exists()
```
Uses `os.utime` to backdate mtime to 2h ago, then asserts sweep removes it.

### 8.3 Schema strict mode (`test_extra_top_level_field_forbidden`)
```python
def test_extra_top_level_field_forbidden(self) -> None:
    cf_dict = _cache_file().model_dump(mode="json")
    cf_dict["unknown_top_level"] = "boom"
    with pytest.raises(ValidationError):
        SymbolMappingCacheFile.model_validate(cf_dict)
```
`extra="forbid"` → schema drift fails loud at parse time.

## 9. Deviations + rationale

### D-4.A.2-1 — pre-existing mypy errors not introduced by this step
Server mypy reports 3 `hedger_shared.*` `import-not-found` errors. Verified pre-existing on `main` HEAD (`git stash; mypy app/` showed identical 3 errors). Not in scope for 4.A.2 to fix the `hedger_shared` packaging gap. Acceptance criterion #26 reads "mypy strict server clean" — interpreting as **delta clean** (no new errors), which is true.

### D-4.A.2-2 — `_read_file` uses `model_validate_json` instead of `json.load + model_validate`
Plan §2.3 sketched `json.load(f); model_validate(raw)`. Pydantic v2 strict mode rejects ISO-8601 datetime *strings* on the `model_validate(dict)` path because the in-memory dict has them as `str`. Switched to `model_validate_json(text)` which routes through the JSON-aware datetime parser. Behaviour identical for valid input; surfaces ValidationError identically for drift. Same change applied to the in-`write()` re-validation step.

### D-4.A.2-3 — lock dict not protected by a parent lock
`_lock_for(signature)` performs `dict.get` + `dict[key] = lock` without an outer lock. This is safe under CPython's GIL for the synchronous part of `_lock_for`, and asyncio is single-threaded by definition for any single event loop, so two coroutines cannot race the dict write. If we later use multi-loop or threaded executors, this needs revisiting. Documented for future-me; not a 4.A.2 blocker.

### D-4.A.2-4 — DI getter lives in new module `app/dependencies/mapping_cache.py`
Plan §2.6 said "in `app/dependencies/__init__.py`". The existing `__init__.py` is empty (1 byte) and the only sibling is `auth.py` (one getter per file). Followed the existing one-getter-per-file convention rather than introducing dependencies in `__init__`. No behavioural difference.

## 10. Issues / questions for CTO

1. None blocking. Repository is dormant — no consumer until step 4.A.4 wires API endpoints. Suggest reviewing schema field set + atomic-write algorithm now so step 4.A.4 doesn't have to revisit.

## 11. Self-verdict

**PASS** — all 29 acceptance criteria met; 50 new tests; total 542 server + 177 FTMO + 33 Exness all green; ruff clean; mypy delta-clean; server boots and emits both required log lines.
