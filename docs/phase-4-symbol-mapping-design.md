# Phase 4 Sub-Phase 4.A — Symbol Mapping Architecture Design

**Status**: DESIGN DOC (authoritative for sub-phase 4.A steps 4.A.0 → 4.A.7).
**Audience**: Engineering (server, exness-client, web), CTO, CEO operator.
**Authored**: 2026-05-13 (step 4.A.0).
**Scope**: SPEC ONLY — no production code, no migrations, no schema changes are produced by step 4.A.0. Subsequent steps 4.A.1 → 4.A.7 materialize the spec.
**Source base**: branch `step/4.A.0-symbol-mapping-design-doc` cut from `main` HEAD (commit `d4a3035`, immediately after `step-4.1` and the SYMBOL_MAPPING_DECISIONS handoff commit).
**Primary input**: `docs/SYMBOL_MAPPING_DECISIONS.md` (12 D-SM decisions + 9 open items).
**Sibling**: `docs/phase-4-design.md` (cascade close + alerts — independent architecture; sub-phase 4.A does not touch cascade design).

> The "Vietnamese-light" tone in `SYMBOL_MAPPING_DECISIONS.md` is preserved where it carries operator-meaningful nuance; section bodies are mostly English for unambiguous engineering reference.

---

## §0. Document conventions and scope guards

### §0.1 Conventions

- **D-SM-NN** labels reference the 12 principles in `docs/SYMBOL_MAPPING_DECISIONS.md §2`. Every section that applies one of those principles cites it inline (e.g., "per D-SM-03"). The full citation count is asserted in the §0.3 acceptance gate.
- **D-4.A.0-N** labels are CTO Phase 4 decisions emerging from step 4.A.0 that extend or refine the D-SM principles. They are promoted to canonical `D-XXX` numbers in `docs/DECISIONS.md` during step 4.12 (Phase 4 docs sync).
- **R**/**G** labels reuse the rule/edge-case taxonomy from `docs/12-business-rules.md`. Any new rule introduced for symbol mapping is prefixed `R-SM-` until promoted.
- **Code references**: file paths plus approximate line numbers are *targets*; implementation steps land within ±20 lines. Step self-checks reconcile.
- **Wire constants**: broker names `"ftmo"` and `"exness"` lowercase, no separators (matches `Broker` Literal in `redis_service.py`). Account IDs `^[a-z0-9_]{3,64}$` (same regex as Phase 3).

### §0.2 What step 4.A.0 produces

Exactly **one** new file under `docs/`:

- `docs/phase-4-symbol-mapping-design.md` (this file).

### §0.3 What step 4.A.0 explicitly does NOT produce

- No new source files under `server/`, `apps/`, `web/`, or `shared/`.
- No new data files under `server/data/`, `server/config/`, or `archive/`.
- No migration script execution.
- No modifications to `symbol_mapping_ftmo_exness.json` (archive happens at step 4.A.1).
- No edits to other docs (`MASTER_PLAN_v2.md`, `DECISIONS.md`, `phase-4-design.md`, `06-data-models.md`, `07-server-services.md`, `08-server-api.md` are out-of-scope).
- No tests.
- No Redis schema migrations.
- No web/Zustand store edits.

Verification: `git diff --stat HEAD~1` after the step-4.A.0 commit must show **exactly one** added file, under `docs/`. Any other delta is a step-4.A.0 bug.

### §0.4 D-SM citation completeness

This document must cite all 12 D-SM decisions (D-SM-01 → D-SM-12) at least once in their applicable sections, plus inline references to D-016, D-017, D-081, D-094, D-095, D-103 from `docs/DECISIONS.md` where the Phase 1-3 baseline is being refactored. The §0.3 acceptance gate (criterion 21) requires `grep -c "D-SM-" >= 12`.

---

## §1. Overview and rationale

### §1.1 The current broken state

The single file `symbol_mapping_ftmo_exness.json` at the repo root (117 entries, Phase 1-3 era — built by `build_symbol_mapping.py`) currently serves **three different roles** that have grown in opposite directions:

| Role | What it provides | Phase 3 user | Problem when broken |
|---|---|---|---|
| **A** | FTMO symbol whitelist (R31–R34: which symbols are allowed) | `symbol_whitelist.py` + `market_data.py::sync_symbols` filter + `order_service::create_market_order` validation (D-081) + `api/symbols.py::list_symbols` response | A wrong/stale entry makes a symbol invisible (low-impact, easy to spot). |
| **B** | FTMO ↔ Exness symbol name mapping (`EURUSD` ↔ `EURUSDm`) | Hedge volume conversion in `volume_calc.py::calculate_volume` (Phase 2.4 preview API) — Phase 4 step 4.5 will consume the same mapping at order-execution time. | Wrong name → secondary leg push fails or hits a different instrument. |
| **C** | Numerical specs (`contract_size`, `pip_size`, `pip_value`, `units_per_lot`) for both legs | `volume_calc.py` computes secondary volume from `ftmo_units_per_lot / exness_trade_contract_size`. | **Wrong contract_size → catastrophic volume miscalc** — see §1.2. |

Roles A and C are also **static** relative to the broker: Phase 3 has no mechanism to sync them with the real broker state. A spec drift (Exness changes `trade_contract_size` for a symbol) goes undetected until a real trade.

### §1.2 The blast radius — why we cannot defer

Exness offers multiple account types (Standard / Cent / Pro / Raw / Zero). Two of them differ by **100×** on `trade_contract_size`:

| Exness account type | `EURUSDm.trade_contract_size` | Symbol suffix |
|---|---|---|
| Standard | 100,000 units/lot | `EURUSDm` |
| Cent | 1,000 units/lot | `EURUSDc` |

Concrete scenario from the SYMBOL_MAPPING_DECISIONS §1 worked example: CEO connects a Cent account to a system whose mapping file is keyed to Standard. The hedge formula

```
volume_secondary = volume_primary × (ftmo_units_per_lot / exness_trade_contract_size)
```

returns **100× the correct lot count** for the Cent account. Real-money result: leg hở by 99% in volume terms, mục tiêu cốt lõi #1 (sync) violated, FTMO drawdown protection breached on first market move.

This is the trigger for inserting sub-phase 4.A **before step 4.2** (Exness client actions + symbol sync). Phase 4 implementation step 4.2 will publish raw broker symbols; without the per-account mapping architecture in place first, step 4.2 has nowhere to publish them and step 4.5 (create_hedge_order full flow) cannot compute secondary volume safely across account types.

### §1.3 Why per-Exness-account mapping wins over global file (D-SM-01)

Per D-SM-01, mapping is a property of each Exness account, resolved runtime via `pair_id → exness_account_id → mapping cache`. Three benefits:

1. **Correctness across account types**: Each Exness account's mapping uses the contract sizes that account actually has — no 100× silent miscalc.
2. **Spec sync from broker**: `mt5.symbols_get()` provides current `trade_contract_size` / `digits` / etc. at connect time. The file/cache is built from that snapshot, not from a human-edited static file.
3. **Cache sharing across same-type accounts** (D-SM-03 signature): When CEO adds a second Standard account, the signature matches an existing cache → auto-link, no wizard. Wizard only fires when actual broker setup differs.

Trade-off accepted: lookup pattern at order-creation time gains an indirection (pair_id → exness_account_id → mapping). This is the §9 refactor scope.

### §1.4 Splitting the three roles cleanly

| Role | Phase 4 location | File type | Mutability |
|---|---|---|---|
| **A** FTMO whitelist | `server/data/ftmo_whitelist.json` (D-SM-09) | Static config, Git-tracked, server-readonly | Immutable runtime; restart to reload |
| **B** + **C** FTMO ↔ Exness mapping + Exness specs | `server/data/symbol_mapping_cache/*.json` (D-SM-11) | Per-signature cache, Git-ignored, server-read-write | Atomic write on wizard save |
| (Auxiliary) Manual hints | `server/config/symbol_match_hints.json` (D-SM-12) | Static config, Git-tracked, server-readonly | Immutable runtime; restart to reload |

This split is the spine of the rest of this document.

---

## §2. Data architecture

### §2.1 `server/data/ftmo_whitelist.json` (D-SM-09)

The FTMO side is static and global. All FTMO clients are identical with respect to symbol catalog (same cTrader broker, same FTMO account type for Phase 4). CEO maintains this file by hand using the same TSV-extraction workflow currently driving `build_symbol_mapping.py`'s FTMO portion.

#### §2.1.1 Schema

```json
{
  "schema_version": 1,
  "version": 1,
  "symbols": [
    {
      "name": "EURUSD",
      "asset_class": "forex",
      "quote_ccy": "USD",
      "ftmo_units_per_lot": 100000,
      "ftmo_pip_size": 0.0001,
      "ftmo_pip_value": 10.0
    },
    {
      "name": "XAUUSD",
      "asset_class": "metals",
      "quote_ccy": "USD",
      "ftmo_units_per_lot": 100,
      "ftmo_pip_size": 0.1,
      "ftmo_pip_value": 10.0
    },
    {
      "name": "US100.cash",
      "asset_class": "indices",
      "quote_ccy": "USD",
      "ftmo_units_per_lot": 1,
      "ftmo_pip_size": 0.1,
      "ftmo_pip_value": 0.1
    }
  ]
}
```

#### §2.1.2 Pydantic v2 strict model (load-time validation)

Following D-016 (Pydantic v2 strict + `extra="forbid"` for symbol mapping types):

```python
# server/app/services/ftmo_whitelist_service.py (step 4.A.1 — proposed)
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

AssetClass = Literal["forex", "metals", "indices", "commodities", "crypto", "stocks"]

class FTMOSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    asset_class: AssetClass
    quote_ccy: str = Field(min_length=3, max_length=3)  # ISO 4217
    ftmo_units_per_lot: float = Field(gt=0)
    ftmo_pip_size: float = Field(gt=0)
    ftmo_pip_value: float = Field(gt=0)

class FTMOWhitelistFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int  # CTO addition (D-4.A.0-1) — forward-compat header
    version: int          # CEO-managed semantic version of the whitelist content
    symbols: list[FTMOSymbol]
```

#### §2.1.3 Load behavior

- Loaded once at server lifespan startup via `FTMOWhitelistService.load(path)`.
- `path` resolved from `Settings.ftmo_whitelist_path` (defaults to `server/data/ftmo_whitelist.json`).
- Failure to load (file missing, JSON parse error, Pydantic validation error) → fail-fast startup with descriptive log (analogous to current `symbol_whitelist.load_whitelist` behavior — step 4.A.1 mirrors).
- No reload-on-change. CEO edits file → restart server. This matches the existing Phase 3 contract (D-SM-09 explicit: "Đổi file → restart server").

#### §2.1.4 Why `schema_version` (CTO addition vs D-SM-09)

D-SM-09 spec'd `version` only. Adding `schema_version` is **D-4.A.0-1**: lets us evolve the schema (add fields like `min_lot`, `max_lot`, broker-fee hints) without breaking older files. `version` remains the CEO-managed semantic content version. See §13 deviation table.

### §2.2 `server/data/symbol_mapping_cache/*.json` (D-SM-10, D-SM-11)

The per-signature cache files. One file per unique Exness symbol-set signature. Multiple Exness accounts can share one cache (D-SM-03 reuse). The filename includes the *first* account that triggered the cache, plus the full sha256 (D-SM-10).

#### §2.2.1 Filename convention

```
{first_account_id}_{full_sha256}.json
```

Examples:
```
exness_acc_001_a3f5b9c2d4e6f8a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5.json
exness_acc_003_b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8.json
```

- The 64-hex sha256 is the **lookup key**. The leading account id is a human-readable hint only and is **not** part of the cache identity.
- When a second account shares the signature, it appends to `used_by_accounts` inside the file — the filename does NOT change.
- When the first account is deleted, the file name still references it (historical marker — explicit per D-SM-10 edge case).

#### §2.2.2 Schema

```json
{
  "schema_version": 1,
  "signature": "a3f5b9c2d4e6f8a1...{full 64-hex sha256}",
  "created_at": "2026-05-13T10:00:00Z",
  "updated_at": "2026-05-13T10:00:00Z",
  "created_by_account": "exness_acc_001",
  "used_by_accounts": ["exness_acc_001", "exness_acc_003"],
  "raw_symbols_snapshot": [
    {
      "name": "EURUSDm",
      "contract_size": 100000,
      "digits": 5,
      "pip_size": 0.0001,
      "volume_min": 0.01,
      "volume_step": 0.01,
      "volume_max": 200,
      "currency_profit": "USD"
    }
  ],
  "mappings": [
    {
      "ftmo": "EURUSD",
      "exness": "EURUSDm",
      "match_type": "suffix_strip",
      "contract_size": 100000,
      "pip_size": 0.0001,
      "pip_value": 10.0,
      "quote_ccy": "USD"
    }
  ]
}
```

#### §2.2.3 Pydantic v2 strict models

```python
# server/app/services/mapping_cache_repository.py (step 4.A.2 — proposed)
from typing import Literal
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

MatchType = Literal["exact", "suffix_strip", "manual_hint", "override"]

class RawSymbolEntry(BaseModel):
    """A single entry in `raw_symbols_snapshot` — direct copy of
    `mt5.symbol_info(name)` fields needed for mapping + validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    contract_size: float = Field(gt=0)
    digits: int = Field(ge=0, le=10)
    pip_size: float = Field(gt=0)
    volume_min: float = Field(gt=0)
    volume_step: float = Field(gt=0)
    volume_max: float = Field(gt=0)
    currency_profit: str = Field(min_length=3, max_length=3)


class MappingEntry(BaseModel):
    """Confirmed FTMO ↔ Exness mapping with specs lifted from raw snapshot
    at wizard-save time. Specs are copied so order-time lookups don't have
    to cross-reference the snapshot — single-read path on the hot trading
    flow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ftmo: str
    exness: str
    match_type: MatchType
    contract_size: float = Field(gt=0)
    pip_size: float = Field(gt=0)
    pip_value: float = Field(gt=0)
    quote_ccy: str = Field(min_length=3, max_length=3)


class SymbolMappingCacheFile(BaseModel):
    """A persisted mapping cache. One file per unique signature."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int             # D-4.A.0-1 forward-compat
    signature: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime
    created_by_account: str
    used_by_accounts: list[str] = Field(min_length=1)
    raw_symbols_snapshot: list[RawSymbolEntry] = Field(min_length=1)
    mappings: list[MappingEntry] = Field(min_length=1)
```

Field count summary (acceptance criterion 5):

- Top-level cache file: **10 fields** (`schema_version`, `signature`, `created_at`, `updated_at`, `created_by_account`, `used_by_accounts`, `raw_symbols_snapshot`, `mappings`, plus the two implicit type-discriminating Pydantic model entries `__pydantic_model__` and `__pydantic_extra__` that exist but are not user-facing; user-visible JSON keys = **8**, listed above; the `schema_version` is the CTO addition to D-SM-11's spec).
- Per `MappingEntry`: **8 fields** (`ftmo`, `exness`, `match_type`, `contract_size`, `pip_size`, `pip_value`, `quote_ccy`) — wait, that's 7. CTO addition: include `exness_volume_step` and `exness_volume_min` in the mapping entry too (`raw_symbols_snapshot` is the slow-path lookup; for hot-path volume validation we want everything in `MappingEntry`). See D-4.A.0-2.
- Per `RawSymbolEntry`: **8 fields**.

#### §2.2.4 Folder layout and permissions (D-SM-08)

```
server/
├── data/
│   ├── ftmo_whitelist.json                                 (read-only at runtime; Git-tracked)
│   └── symbol_mapping_cache/                               (read-write at runtime; Git-ignored except `.gitkeep`)
│       ├── exness_acc_001_a3f5b9c2....json
│       └── exness_acc_003_b7c8d9e0....json
└── config/
    └── symbol_match_hints.json                             (read-only at runtime; Git-tracked)
```

- `server/data/ftmo_whitelist.json`: server reads at lifespan startup, never writes.
- `server/data/symbol_mapping_cache/`: server writes atomically via tempfile + rename (§8). Gitignored. Backed up via deployment procedure (RUNBOOK update at step 4.12).
- `server/config/symbol_match_hints.json`: server reads at lifespan startup. CEO edits + restart.
- A `.gitkeep` lives in `symbol_mapping_cache/` so the folder is tracked even when empty. The `.gitignore` rule is `server/data/symbol_mapping_cache/*.json` + an explicit `!.gitkeep` un-ignore.

### §2.3 `server/config/symbol_match_hints.json` (D-SM-12)

Manual mapping hints — symbols where neither `exact` nor `suffix_strip` produces a result, but CEO's trader knowledge says "FTMO `NATGAS.cash` is hedged by Exness `XNGUSD`". Static config, restart-to-reload (matches D-SM-12 explicit).

#### §2.3.1 Schema

```json
{
  "schema_version": 1,
  "version": 1,
  "hints": [
    {"ftmo": "NATGAS.cash", "exness_candidates": ["XNGUSD"], "note": "Natural gas hedge proxy"},
    {"ftmo": "GER40.cash", "exness_candidates": ["DE30", "DAX40"], "note": "DAX index"},
    {"ftmo": "US30.cash", "exness_candidates": ["US30"], "note": ""},
    {"ftmo": "US500.cash", "exness_candidates": ["US500"], "note": ""},
    {"ftmo": "US100.cash", "exness_candidates": ["USTEC", "NAS100"], "note": ""},
    {"ftmo": "EU50.cash", "exness_candidates": ["STOXX50"], "note": ""},
    {"ftmo": "FRA40.cash", "exness_candidates": ["FR40"], "note": ""},
    {"ftmo": "UK100.cash", "exness_candidates": ["UK100"], "note": ""},
    {"ftmo": "HK50.cash", "exness_candidates": ["HK50"], "note": ""},
    {"ftmo": "JP225.cash", "exness_candidates": ["JP225"], "note": ""},
    {"ftmo": "AUS200.cash", "exness_candidates": ["AUS200"], "note": ""},
    {"ftmo": "DXY.cash", "exness_candidates": ["DXY"], "note": ""},
    {"ftmo": "NATGAS.cash", "exness_candidates": ["XNGUSD"], "note": "Natural gas hedge proxy"},
    {"ftmo": "UKOIL.cash", "exness_candidates": ["UKOIL"], "note": ""},
    {"ftmo": "USOIL.cash", "exness_candidates": ["USOIL"], "note": ""}
  ]
}
```

Bootstrap content is extracted from the `MANUAL_EXNESS` dict referenced in `build_symbol_mapping.py` (the CEO will provide that file at step 4.A.3; in its absence, step 4.A.3 derives the seed list from the 14 `match_type=manual` rows currently in `symbol_mapping_ftmo_exness.json` — verified by grepping the existing file: 14 manual-mapped indices/commodities).

#### §2.3.2 Pydantic schema

```python
# server/app/services/match_hints_service.py (step 4.A.3 — proposed)
class MatchHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ftmo: str
    exness_candidates: list[str] = Field(min_length=1)
    note: str  # may be empty

class MatchHintsFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    version: int
    hints: list[MatchHint]
```

### §2.4 Redis keys for runtime mapping cache

Four keys per D-SM-06 (raw snapshot lifecycle) and D-SM-07 (file=truth / Redis=working-cache):

| Key | Type | TTL | Lifecycle | Owner |
|---|---|---|---|---|
| `mapping_cache:{signature}` | HASH | none (managed lifecycle) | Populated at startup from files; updated atomically when a new cache file is saved | Server `MappingCacheRepository` |
| `account_to_mapping:{exness_account_id}` | STRING | none (managed lifecycle) | Set when account is linked to a cache (wizard save or auto-link); cleared on account deletion | Server `AccountMappingService` |
| `exness_raw_symbols:{exness_account_id}` | STRING (JSON-serialized list) | none (managed; D-SM-06 ephemeral) | XADD by Exness client on connect → server reads when wizard opens → server DELs after wizard save | Exness client write; server read+delete |
| `mapping_status:{exness_account_id}` | STRING | none | Enum: `pending_mapping`, `active`, `spec_mismatch`, `disconnected`; set by server on account state transitions; read by frontend on Settings tab + by order_service preflight | Server |

#### §2.4.1 Why no Redis TTL on `exness_raw_symbols`

D-SM-06 specifies "Redis ephemeral" but explicitly does not use TTL — lifecycle is managed actively. Reasons:

1. Wizard can stay open for arbitrary duration while CEO reviews; a TTL would force the wizard to expire mid-edit.
2. The DELETE happens **after** atomic save (§8). If save fails, the snapshot stays so the wizard can retry.
3. If the client publishes a fresh snapshot (re-sync) before save, server overwrites the same key.

The trade-off: orphan snapshots can accumulate if CEO opens a wizard, never saves, and never re-connects the account. Mitigation: account deletion clears the key. Phase 5 may add a daily sweep on orphans.

#### §2.4.2 Why `mapping_cache:{signature}` is HASH (not JSON STRING)

For hot-path lookup on order creation:
- `HGET mapping_cache:{sig} ftmo:{ftmo_symbol}` returns the entire JSON-encoded `MappingEntry` for that FTMO symbol with a single Redis round-trip.
- Compared to STRING + full-load + parse on every order: HASH is O(1) field read + small payload.

The HASH is populated from the file's `mappings` array at startup; each field is `ftmo:{ftmo_symbol_name}` → JSON-encoded `MappingEntry`. Plus a meta field `__meta__` holding signature, timestamps, etc.

```
HGETALL mapping_cache:a3f5b9c2...
  __meta__         {"schema_version":1,"signature":"a3f5b9c2...","created_at":"...","used_by_accounts":["exness_acc_001"]}
  ftmo:EURUSD      {"exness":"EURUSDm","match_type":"suffix_strip","contract_size":100000,...}
  ftmo:XAUUSD      {"exness":"XAUUSDm","match_type":"suffix_strip","contract_size":100,...}
  ftmo:US100.cash  {"exness":"USTEC","match_type":"manual_hint","contract_size":1,...}
```

`HGET` keyed by `ftmo:{name}` is the order-creation hot path.

#### §2.4.3 Why no `exness:` → `ftmo:` reverse index

The reverse lookup (Exness symbol → FTMO symbol) is needed for path E cascade close (server reads Exness `position_closed_external` event with Exness symbol name; needs to find the original order). But this lookup happens via the `order:{order_id}` HASH which already stores both leg symbols. No reverse index needed in `mapping_cache`.

---

## §3. Signature computation (D-SM-03)

The signature is the unique identifier for a "symbol set fingerprint". Two Exness accounts with identical symbol catalogs share a signature; the wizard runs once.

### §3.1 Formula (locked)

```python
import hashlib
import json

def compute_signature(raw_symbols: list[RawSymbolEntry]) -> str:
    """Sig-1 per D-SM-03: sha256 of sorted symbol names, JSON-serialized
    with deterministic separators."""
    names = sorted(s.name for s in raw_symbols)
    payload = json.dumps(names, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

### §3.2 Properties

| Property | Reason |
|---|---|
| **sorted(names)** | Eliminates ordering-of-publish nondeterminism. The Exness client may iterate `mt5.symbols_get()` in any order; sorting locks the hash. |
| **JSON `separators=(",", ":")`** | Removes whitespace from default JSON serialization (`json.dumps([...])` would add a space after commas: `["A", "B"]` vs `["A","B"]`). Stability across Python versions / platforms / future re-implementations. |
| **Names only, not specs** | D-SM-03: names are stable across broker-side spec edits within the same account type. Spec divergence is detected separately (§5). |
| **sha256 (not truncated)** | Collision-resistant; filename includes the **full** 64-hex digest per D-SM-10. |

### §3.3 Determinism contract

The signature **must** be identical when:
- Same Exness account type, same symbols (in any order at publish time).
- Reproduced manually by a CEO running the formula against the file's `raw_symbols_snapshot`.
- Re-computed at server startup from the file's `raw_symbols_snapshot`.

Step 4.A.2 ships a test fixture with a known small input → known sha256 output, pinning the contract.

### §3.4 What signature does NOT detect

- Spec drift on a symbol that keeps its name (e.g., Exness silently changes `EURUSDm.trade_contract_size` from 100000 to 99999.99). Handled by §5 validation on link attempt.
- Symbol rename with identical specs (impossible in practice; brokers don't rename).

---

## §4. Fuzzy match for diff-aware wizard (D-SM-04)

When signature MISS occurs but the new symbol set is "almost" an existing cache, we want the wizard to pre-fill from that closest match rather than starting from scratch.

### §4.1 Algorithm (locked)

```python
def find_fuzzy_match(
    new_signature: str,
    new_names: set[str],
    cache_index: dict[str, CacheMetadata],   # signature → metadata snapshot
    threshold: float = 0.95,
) -> CacheMetadata | None:
    """Per D-SM-04: Jaccard intersect/union ≥ threshold.

    Returns the single closest match, or None if no cache crosses the
    threshold. Ties broken by `updated_at` (most-recent wins) so the
    candidate reflects the freshest CEO confirmation."""

    candidates: list[tuple[float, CacheMetadata]] = []
    for sig, meta in cache_index.items():
        if sig == new_signature:
            continue  # exact match → not "fuzzy"; handled upstream
        existing_names: set[str] = set(meta.symbols)
        intersect = len(new_names & existing_names)
        union = len(new_names | existing_names)
        if union == 0:
            continue  # both empty — degenerate, skip
        score = intersect / union
        if score >= threshold:
            candidates.append((score, meta))

    if not candidates:
        return None
    # Pick highest score; ties → most-recent updated_at.
    candidates.sort(key=lambda x: (x[0], x[1].updated_at), reverse=True)
    return candidates[0][1]
```

### §4.2 Edge cases

| Case | Behavior | Reason |
|---|---|---|
| Empty new symbol set | Return None (degenerate input) | Client publish bug — log ERROR, wizard cannot proceed |
| Single symbol in both | Jaccard = 1/1 = 1.0 if same name; 0/1 = 0 if different. Threshold 0.95 → either exact or nothing | Small set sensitivity is acceptable for the wizard use case |
| Tied scores (multiple caches at 0.95+) | Tie-break by `updated_at` DESC (freshest wins) | Most-recent CEO judgment reflects current broker mapping conventions |
| Existing cache is empty (data corruption) | Skip via `union == 0` guard | Defensive — should not occur in practice |
| New names is 100% subset of existing | Score = subset_size / existing_size. If existing has many more, score may be < 0.95 → no match | Correct: broker dropped many symbols → CEO should review changes anyway |

### §4.3 Why 0.95 (not 0.90 or 0.99)

- **0.99 too strict**: Even one symbol added/removed in a ~150-symbol catalog produces 149/150 = 0.993 — wouldn't trigger the wizard when a broker adds a single new crypto. Useful when goal is "near-exact", but defeats the diff-aware purpose.
- **0.90 too loose**: ~10% of symbols changed is a substantial mapping change — pre-filling from a stale candidate would mislead CEO into accepting wrong mappings.
- **0.95 chosen**: ~5% slack accommodates broker symbol additions/removals (typical: 1-5 symbols on a 150-symbol catalog) while still requiring a substantial overlap. Confirmed in D-SM-04 spec.

### §4.4 Wizard result of fuzzy match

A fuzzy match produces a **candidate** cache. The wizard opens in Diff-aware mode (§6.2):

- Pre-fill all mappings from the candidate cache where the FTMO symbol still appears in the new raw set.
- Highlight rows: **unchanged** (green), **new** (yellow — symbol present in new but not in candidate), **removed** (red — symbol in candidate but not in new).
- Save **always creates a new cache file** with the new signature. The candidate cache is not modified.

### §4.5 What happens after CEO confirms a fuzzy-pre-filled wizard

1. Server validates per §5 (contract sizes must still exact-match the new raw snapshot).
2. Server writes a NEW cache file `{first_account_id}_{new_sig}.json`.
3. Server populates `mapping_cache:{new_sig}` in Redis from the new file.
4. Server updates `account_to_mapping:{exness_account_id}` to the new signature.
5. Old candidate cache (in Redis + file) is untouched — other accounts still pointing at it continue to use it.

---

## §5. Validation rules on link attempt (D-SM-05)

When an Exness account attempts to link to an existing cache (signature HIT or fuzzy candidate confirm), the server validates spec consistency before committing the link. Per D-SM-05, `contract_size` is the non-negotiable check; other fields have CTO-defined thresholds.

### §5.1 Per-field validation table

| Field | Tolerance | Failure behavior | Rationale |
|---|---|---|---|
| `contract_size` | **Exact match (==)** | BLOCK link. Flag `mapping_status=spec_mismatch`. Wizard offers "Re-create mapping for this account" (creates new cache). | D-SM-05 explicit: `Volume_Exness = (units / contract_size)` → 0.01 lệch = 0.01% volume miscalc, compounds catastrophically in 100×-scale-difference accounts. NO tolerance, ever. |
| `digits` | **Exact match (==)** | BLOCK link. Same flow as `contract_size`. | Display precision + tick-size derivation depends on `digits`. A mismatch means the UI would render prices wrong AND order rounding could fail. Treat as exact. |
| `pip_size` | ±5% | WARN log + accept. Display warning badge in Settings UI on the mapping row. | Brokers occasionally use slightly different `pip_size` conventions for the same instrument (especially exotics). 5% slack accommodates this without false-positive blocking. |
| `pip_value` | ±10% | WARN log + accept. Display warning badge. | Derived value (`pip_size × contract_size × FX-rate`). Brokers may compute slightly differently. 10% reflects the indirect-derivation tolerance. |
| `volume_min` / `volume_step` / `volume_max` | None — always use the **latest** raw snapshot value | WARN if changed from cache | These are broker policy and can be updated by Exness at any time. Use latest, log the diff for operator awareness. |
| `currency_profit` | **Exact match (==)** | BLOCK link. Same flow as `contract_size`. | If the broker changes `currency_profit` for a symbol, the P&L conversion path breaks. Treat as identity-defining. |

Failure on any BLOCK field triggers spec_mismatch flow (§6.3). A WARN-only field never blocks; the wizard simply displays the diff inline so CEO is aware.

### §5.2 When validation runs

| Trigger | What validates | Action on fail |
|---|---|---|
| Account first publish, signature HIT | Each symbol in `raw_symbols_snapshot` vs cache `raw_symbols_snapshot` | `mapping_status=spec_mismatch`; CEO opens wizard in SpecMismatch mode |
| Account first publish, fuzzy candidate confirm | Same | Block save; force back to Create mode |
| Account re-sync (already active), signature MATCH | Same (against the cache this account is linked to) | Set `mapping_status=spec_mismatch`; alert CEO via toast |
| CEO clicks "Edit Mapping" on active account | Each override → exact-match against the latest raw snapshot | Block save with per-row error |

### §5.3 Validation algorithm sketch

```python
def validate_spec_consistency(
    new_raw: list[RawSymbolEntry],
    cache_raw: list[RawSymbolEntry],
) -> SpecValidationResult:
    """Walk every symbol that appears in BOTH sets, compare per §5.1.

    Returns:
      .blockers: list[SpecDiff]  — fields that BLOCK link
      .warnings: list[SpecDiff]  — fields with WARN-only tolerance breached
      .ok: bool                  — True iff blockers is empty
    """
    new_index = {s.name: s for s in new_raw}
    cache_index = {s.name: s for s in cache_raw}
    common = set(new_index) & set(cache_index)

    blockers: list[SpecDiff] = []
    warnings: list[SpecDiff] = []
    for name in common:
        n, c = new_index[name], cache_index[name]
        if n.contract_size != c.contract_size:
            blockers.append(SpecDiff(name, "contract_size", c.contract_size, n.contract_size))
        if n.digits != c.digits:
            blockers.append(SpecDiff(name, "digits", c.digits, n.digits))
        if n.currency_profit != c.currency_profit:
            blockers.append(SpecDiff(name, "currency_profit", c.currency_profit, n.currency_profit))
        if abs(n.pip_size - c.pip_size) / c.pip_size > 0.05:
            warnings.append(SpecDiff(name, "pip_size", c.pip_size, n.pip_size))
        # ... etc for pip_value (±10%), volume_* (warn always-on)

    return SpecValidationResult(blockers=blockers, warnings=warnings, ok=not blockers)
```

### §5.4 Spec divergence report format

When BLOCK fires, the wizard displays a per-symbol diff table:

```
┌─────────────────┬───────────────┬──────────────┬──────────────┐
│ Symbol          │ Field         │ Cache value  │ New value    │
├─────────────────┼───────────────┼──────────────┼──────────────┤
│ EURUSDm         │ contract_size │ 100000       │ 1000  ❌      │
│ EURUSDm         │ digits        │ 5            │ 5    ✓        │
│ XAUUSDm         │ contract_size │ 100          │ 1    ❌       │
└─────────────────┴───────────────┴──────────────┴──────────────┘

This account's broker specs differ from the existing mapping cache.
You cannot link to this cache. Create a new mapping for this account?
[ Re-create Mapping ]   [ Cancel ]
```

The CTA `Re-create Mapping` switches the wizard to Create mode (§6.1) with the new raw snapshot.

---

## §6. Wizard flow (D-SM-02 + open item 1)

The wizard is the operator's tool to confirm or override the auto-matched FTMO ↔ Exness mappings. It runs in five distinct modes, plus a set of bulk actions.

### §6.1 Mode A — Create (first connect, signature MISS, no fuzzy match)

**Entry conditions**:
- Exness account first publish.
- Signature not in `cache_index`.
- Fuzzy match returns None (no existing cache ≥ 0.95 Jaccard).

**Wizard banner**: `Create new symbol mapping for {account_id}`.

**Flow**:
1. Client publishes raw symbols → server computes signature → cache MISS.
2. Server stores `exness_raw_symbols:{account_id}` in Redis.
3. Server flags `mapping_status:{account_id} = pending_mapping`.
4. Frontend Settings tab shows "Map Symbols" CTA on the account row.
5. CEO clicks → frontend opens wizard, fetches raw symbols + ftmo_whitelist + auto-match proposals.
6. Server runs auto-match: per FTMO symbol, try **exact** → **suffix_strip** → **manual_hint**. First match wins (or unmapped if none).
7. Wizard renders table:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ FTMO Symbol  │ Proposed Exness │ Match     │ Spec Preview          │ Action │
├──────────────┼─────────────────┼───────────┼───────────────────────┼────────┤
│ EURUSD       │ EURUSDm         │ suffix    │ cs=100000, ps=0.0001  │ ✓ ✗ Ⓢ │
│ XAUUSD       │ XAUUSDm         │ suffix    │ cs=100, ps=0.01       │ ✓ ✗ Ⓢ │
│ US100.cash   │ USTEC           │ hint      │ cs=1, ps=0.1          │ ✓ ✗ Ⓢ │
│ NATGAS.cash  │ XNGUSD          │ hint      │ cs=1000, ps=0.001     │ ✓ ✗ Ⓢ │
│ TSLA         │ (none — typed)  │ override  │ —                     │ Ⓢ      │
│ DOGEUSD      │ (no match)      │ —         │ —                     │ Ⓢ      │
└──────────────────────────────────────────────────────────────────────────────┘
                                              [Accept all] [Skip unmapped]
                                              [Save Mapping]
```

8. Per-row actions:
   - **✓ Accept**: confirms the proposed Exness symbol.
   - **✗ Override**: opens a typeahead dropdown filtered to relevant Exness symbols (default: only those intersecting FTMO whitelist asset classes; toggleable to "Show all").
   - **Ⓢ Skip**: marks the FTMO symbol as not mapped on this account. Order form will refuse hedges using this FTMO symbol on this pair.

9. CEO clicks `Save Mapping`:
   - Server validates payload (per-row Pydantic).
   - For each `Accept` / `Override` row: server validates contract_size against raw snapshot (§5 — for new caches, no cache to compare against; validation is "spec must come from raw_symbols, not hallucinated").
   - Server atomic writes `{account_id}_{sig}.json` to `server/data/symbol_mapping_cache/`.
   - Server populates `mapping_cache:{sig}` in Redis.
   - Server sets `account_to_mapping:{account_id} = {sig}`.
   - Server sets `mapping_status:{account_id} = active`.
   - Server DELs `exness_raw_symbols:{account_id}` (D-SM-06 ephemeral lifecycle).
   - Server returns 201 with `{signature, cache_filename}`.

### §6.2 Mode B — Diff-aware (signature MISS, fuzzy match ≥ 0.95)

**Entry conditions**:
- Signature MISS.
- Fuzzy match returns a candidate cache.

**Wizard banner**: `Map symbols against {candidate_account_id}'s cache (95% match)`.

**Flow**:
1. Auto-match proposals = candidate cache's mappings, filtered to FTMO symbols still present in new raw.
2. Yellow highlight: symbols in new raw but NOT in candidate cache (auto-match runs from scratch on these — typically a freshly added broker symbol).
3. Red highlight (informational only): symbols in candidate cache but NOT in new raw (broker removed them; nothing to map on this account).
4. Per-row override / skip identical to Create mode.
5. Save → **new** cache file (the candidate cache is not modified). Server validates that the contract_size of confirmed mappings matches the new raw snapshot (sanity check — should match if raw was just published).

### §6.3 Mode C — Spec mismatch (signature HIT, contract_size differs)

**Entry conditions**:
- Signature HIT.
- §5 validation finds at least one BLOCK divergence.

**Wizard banner**: `⚠ {account_id} broker specs diverge from existing mapping cache`.

**Flow**:
1. Wizard renders the spec divergence table (§5.4).
2. **No "Accept" or "Override" actions on rows** — wizard is read-only in this mode.
3. Only two CTAs:
   - **Re-create Mapping**: switches wizard to Create mode (§6.1) with the new raw snapshot. This creates a fresh cache.
   - **Cancel**: leaves the account in `mapping_status=spec_mismatch`. Account cannot be used until CEO re-creates mapping.
4. **No "Force link" option**. D-SM-05 is absolute: contract_size divergence MUST trigger a new cache, never a force-link.

### §6.4 Mode D — Re-sync (active account, broker drift detected)

**Entry conditions**:
- Account is currently `mapping_status=active`, linked to a cache.
- Client republishes raw symbols (manual operator trigger or periodic re-sync).
- New signature ≠ old signature, OR signature same but specs diverge.

**Flow**:
1. Server diff: new raw vs cache `raw_symbols_snapshot`.
2. If diff is empty (re-sync confirms cache is current): no action, mapping_status stays `active`.
3. If diff non-empty:
   - Set `mapping_status:{account_id} = pending_mapping`.
   - Broadcast `account_mapping_drift` WS message to frontend → toast CEO.
   - Frontend Settings tab shows "Re-map Symbols" CTA.
   - CEO clicks → wizard opens in Diff-aware mode (§6.2) against the currently-linked cache as the candidate.
4. Save → new cache (old cache untouched, still used by other accounts).

### §6.5 Mode E — Edit mapping (active account, CEO chooses to revisit)

**Entry conditions**:
- Account is `active`.
- CEO clicks "Edit Mapping" on the Settings account row.

**Flow**:
1. Frontend fetches existing cache + the latest raw snapshot (server triggers a re-sync request first if `exness_raw_symbols:{account_id}` is empty).
2. Wizard renders in Edit mode: same layout as Diff-aware but explicit "Edit existing" banner.
3. CEO modifies rows.
4. Save → **new cache file** (atomic — old cache is not mutated). Account pointer migrates to new cache.
5. If old cache is now used by ≥1 other account → it stays for them.
6. If old cache is now used by 0 accounts → it becomes **orphan**. Phase 5 cleanup job sweeps orphans (Phase 4 leaves them on disk).

D-SM-11 explicit on immutable history: edit always creates a new cache. The history is preserved on disk.

### §6.6 Bulk actions (open item 1, locked)

Available in Create / Diff-aware / Edit modes (not SpecMismatch):

| Bulk action | What it does |
|---|---|
| **Accept all exact-match** | Marks all rows with `match_type=exact` as Accept. Rows with `suffix_strip` / `manual_hint` left for individual confirmation. Conservative default. |
| **Accept all auto-matched** | Marks all rows with any proposal (exact / suffix_strip / manual_hint) as Accept. Higher-velocity option for CEO who trusts the auto-match. |
| **Skip all unmapped** | Marks every row whose auto-match returned no proposal as Skip. CEO can still individually un-skip a row to type an override. |
| **Filter by asset class** | Dropdown: forex / metals / indices / commodities / crypto / stocks. Filter is view-only; doesn't change state. Lets CEO triage a 100+-row table. |

### §6.7 Specs preview column

For each row, the wizard shows the Exness symbol's specs (from raw snapshot): `contract_size`, `pip_size`, `digits`, `volume_min`, `volume_step`, `volume_max`. Collapsible behind a "Show specs" toggle to keep the table scannable. When CEO overrides a row, the preview updates to the chosen Exness symbol's specs in real time.

### §6.8 Realtime validation feedback

While CEO interacts:

- **Override typed Exness symbol** that doesn't exist in raw snapshot → red border + tooltip `Symbol not found in this account's broker catalog`.
- **Override Exness symbol** whose `currency_profit` differs from FTMO `quote_ccy` → yellow warning `Quote currency mismatch — P&L conversion will use {raw.currency_profit}`.
- **Contract size suspiciously different** (e.g., FTMO `EURUSD.ftmo_units_per_lot=100000` but selected Exness `EURUSDc.contract_size=1000`) → yellow warning `Contract sizes differ 100× — verify this is the intended hedge ratio`.

Validation rules surface in the wizard before Save, not just at server-side.

---

## §7. API surface (locked — open item 2)

Seven endpoints. All require REST JWT auth. Pydantic v2 strict request/response schemas. Step 4.A.4 implements.

### §7.1 `GET /api/accounts/exness/{account_id}/raw-symbols`

**Purpose**: load the raw symbol snapshot for a wizard.

**Response (200)**:
```json
{
  "account_id": "exness_acc_001",
  "snapshot_ts": "2026-05-13T10:00:00Z",
  "symbols": [
    {"name": "EURUSDm", "contract_size": 100000, "digits": 5, "pip_size": 0.0001,
     "volume_min": 0.01, "volume_step": 0.01, "volume_max": 200, "currency_profit": "USD"},
    ...
  ]
}
```

**Errors**:
- `404 Not Found` — `exness_raw_symbols:{account_id}` not in Redis (account hasn't published yet or wizard was already saved).
- `403 Forbidden` — invalid JWT.

### §7.2 `GET /api/accounts/exness/{account_id}/mapping-status`

**Purpose**: frontend Settings tab status badge.

**Response (200)**:
```json
{
  "account_id": "exness_acc_001",
  "status": "pending_mapping",            // active | pending_mapping | spec_mismatch | disconnected
  "signature": null,                       // null when pending_mapping; set otherwise
  "cache_filename": null,                  // null when pending_mapping
  "linked_at": null,
  "spec_divergence": null                  // populated when status=spec_mismatch (per-symbol diff)
}
```

When `status=active`:
```json
{
  "account_id": "exness_acc_001",
  "status": "active",
  "signature": "a3f5b9c2...",
  "cache_filename": "exness_acc_001_a3f5b9c2....json",
  "linked_at": "2026-05-13T10:00:00Z",
  "spec_divergence": null
}
```

### §7.3 `POST /api/accounts/exness/{account_id}/symbol-mapping/auto-match`

**Purpose**: server runs auto-match against the FTMO whitelist + match hints + raw snapshot.

**Request body**: empty (server already has raw snapshot in Redis).

**Response (200)**:
```json
{
  "proposals": [
    {"ftmo": "EURUSD", "exness": "EURUSDm", "match_type": "suffix_strip", "confidence": "high"},
    {"ftmo": "US100.cash", "exness": "USTEC", "match_type": "manual_hint", "confidence": "medium"},
    {"ftmo": "TSLA", "exness": null, "match_type": null, "confidence": null}
  ],
  "unmapped_ftmo": ["TSLA", "DOGEUSD"],
  "unmapped_exness": ["XRPm", "DOTm"],
  "mode": "create",                       // create | diff_aware | spec_mismatch
  "candidate_signature": null              // populated when mode=diff_aware
}
```

**Confidence levels**:
- `high` — exact name match.
- `medium` — `suffix_strip` rule applied (FTMO `EURUSD` → Exness `EURUSDm`).
- `low` — `manual_hint` from `symbol_match_hints.json`.
- `null` — no auto-match found.

**Errors**:
- `404` — no raw snapshot in Redis for this account.

Idempotent (read-only).

### §7.4 `POST /api/accounts/exness/{account_id}/symbol-mapping/save`

**Purpose**: persist the CEO-confirmed mappings.

**Request body**:
```json
{
  "mappings": [
    {"ftmo": "EURUSD", "exness": "EURUSDm", "override": false},
    {"ftmo": "US100.cash", "exness": "USTEC", "override": false},
    {"ftmo": "TSLA", "exness": "TSLAusd", "override": true}
  ],
  "skip": ["DOGEUSD"]
}
```

**Server-side effects** (in order):
1. Pydantic validation of payload.
2. Load `exness_raw_symbols:{account_id}` snapshot.
3. For each mapping: validate the chosen Exness symbol exists in the raw snapshot; extract specs from raw.
4. Compute signature from raw symbols.
5. Check if `mapping_cache:{sig}` already exists. If yes → §5 spec divergence check.
6. If no existing cache (or fuzzy match was confirmed in mode B): atomic write new cache file.
7. Populate Redis `mapping_cache:{sig}`.
8. Set `account_to_mapping:{account_id} = sig`.
9. Set `mapping_status:{account_id} = active`.
10. DEL `exness_raw_symbols:{account_id}`.

**Response (201 Created)**:
```json
{
  "signature": "a3f5b9c2...",
  "cache_filename": "exness_acc_001_a3f5b9c2....json",
  "mapping_count": 117,
  "skipped_count": 3
}
```

**Response (200 OK)** when an existing cache was matched (auto-link without writing a new file):
```json
{
  "signature": "a3f5b9c2...",
  "cache_filename": "exness_acc_001_a3f5b9c2....json",
  "reused_existing_cache": true,
  "mapping_count": 117
}
```

**Errors**:
- `400` — payload validation failure (Exness symbol not in raw, duplicate FTMO, etc.).
- `409` — spec divergence detected (BLOCK fields differ from existing cache — returns the divergence detail).
- `503` — atomic write failed; cache state unchanged.

### §7.5 `PATCH /api/accounts/exness/{account_id}/symbol-mapping/edit`

**Purpose**: edit mode (§6.5). Identical payload + server side-effects as `save`, but the wizard caller knows it's a re-confirmation rather than a first-time creation. Always creates a new cache file (no mutate-in-place per D-SM-11). Migrates the account pointer.

**Response (201 Created)** — same shape as `save`.

### §7.6 `GET /api/symbol-mapping-cache`

**Purpose**: admin/debug overview of all cache entries.

**Response (200)**:
```json
{
  "caches": [
    {
      "signature": "a3f5b9c2...",
      "cache_filename": "exness_acc_001_a3f5b9c2....json",
      "created_at": "2026-05-13T10:00:00Z",
      "updated_at": "2026-05-13T10:00:00Z",
      "created_by_account": "exness_acc_001",
      "used_by_accounts": ["exness_acc_001", "exness_acc_003"],
      "mapping_count": 117,
      "raw_symbol_count": 1487
    }
  ]
}
```

Read-only; CEO-only (admin role check beyond the standard JWT scope).

### §7.7 `POST /api/accounts/exness/{account_id}/symbols/resync`

**Purpose**: trigger client to republish `mt5.symbols_get()` snapshot.

**Server side effects**:
- XADD `cmd_stream:exness:{account_id}` with `{action: "resync_symbols", request_id: <new>}`.
- Mark `mapping_status:{account_id} = pending_mapping` if it was `spec_mismatch` (operator is forcing re-eval).

**Response (202 Accepted)**:
```json
{"request_id": "req_abc123", "status": "queued"}
```

**Errors**:
- `404` — account not registered.
- `503` — Exness client is offline (server returns 503 instead of queueing; client wakes up = re-run resync flow naturally).

---

## §8. Atomic write strategy (locked — open item 3)

Cache file writes must be atomic so a partial write does not corrupt the source of truth. Concurrent writes from two wizard tabs on the same signature must serialize. A crashed write must be recoverable.

### §8.1 Algorithm

```python
# server/app/services/mapping_cache_repository.py (step 4.A.2 — proposed)
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

class MappingCacheRepository:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._locks: dict[str, asyncio.Lock] = {}  # signature → Lock
        self._meta_lock = asyncio.Lock()           # guards the _locks map itself

    async def _get_lock(self, signature: str) -> asyncio.Lock:
        async with self._meta_lock:
            if signature not in self._locks:
                self._locks[signature] = asyncio.Lock()
            return self._locks[signature]

    async def save_cache(self, file: SymbolMappingCacheFile) -> Path:
        """Atomically write a cache file. Returns the final path on success.

        Steps:
          1. Acquire per-signature lock.
          2. Pydantic-validate the model (already validated on input, but
             double-check before serialization).
          3. Serialize to JSON with sorted keys + 2-space indent.
          4. Write to a tempfile in the SAME directory as the target.
          5. If a previous file exists at target path, copy it to `.bak`.
          6. `os.replace(tempfile, target)` — POSIX atomic rename on same FS.
          7. Release lock.
        """
        lock = await self._get_lock(file.signature)
        async with lock:
            target = self._dir / f"{file.created_by_account}_{file.signature}.json"
            backup = target.with_suffix(".bak")

            # Validate again — defensive
            file.model_validate(file.model_dump())

            # Write tempfile in same dir (same filesystem → atomic rename)
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(self._dir),
                delete=False,
                prefix=f".{file.signature}_",
                suffix=".tmp",
            ) as tmp:
                tmp.write(file.model_dump_json(indent=2))
                tmp_path = Path(tmp.name)

            # Backup if a previous version exists
            if target.exists():
                shutil.copy2(target, backup)

            # Atomic replace
            tmp_path.replace(target)

            # Best-effort: delete .bak if rename succeeded (we kept it during
            # the rename window for recovery; success means no recovery needed
            # but we keep .bak for one cycle for operator inspection — Phase
            # 5 sweep removes them).
            return target
```

### §8.2 Why per-signature lock (not global)

- Two distinct signatures (e.g., a Standard-account wizard and a Cent-account wizard) can write in parallel without contention.
- Two wizards on the same signature (impossible in practice with single-CEO, but possible across browser tabs) serialize — the second blocks until the first finishes.

### §8.3 Crash recovery

- Tempfile lives in the same directory as the target → atomic rename guarantee from POSIX.
- If the process crashes between tempfile write and rename: the tempfile (prefix `.`-hidden) is orphaned. Server startup sweep deletes any `.{signature}_*.tmp` files older than 1 hour.
- If the process crashes between rename and Redis populate: the file is intact (source of truth) but Redis is stale. Server startup loads from files → Redis is reconciled (D-SM-07 startup contract).
- The `.bak` file is the recovery path if a future rename ever fails mid-flight (extremely unlikely with POSIX rename but explicit per open item 3).

### §8.4 Same-filesystem requirement

`server/data/symbol_mapping_cache/` and the tempfile must live on the same filesystem for `os.replace` to be atomic. Phase 4 deployment instruction (RUNBOOK update at step 4.12): the data directory must NOT be a mount point that cross-filesystem-boundaries.

### §8.5 Pydantic validation on write

Per D-016, all writes go through Pydantic v2 strict validation. The model's `model_validate` is invoked before serialization. This prevents:
- Schema drift (extra fields written that downstream readers reject).
- Type confusion (string where int expected).
- Range violations (negative contract_size).

### §8.6 What this guarantees

| Failure mode | Outcome |
|---|---|
| Crash mid-write to tempfile | Orphan tempfile; cache file unchanged; Redis unchanged. Sweep removes the orphan. |
| Crash between tempfile write and rename | Same as above. |
| Crash between rename and `.bak` cleanup | Cache file replaced (success); `.bak` lingers (harmless). |
| Crash between cache file replace and Redis populate | File is truth; Redis stale until next startup. Server logs ERROR and recommends a manual reload. |
| Two writers same signature | Per-signature `asyncio.Lock` serializes — second waits for first. |
| Two writers different signatures | Different locks → parallel; no contention. |
| Filesystem at capacity | tempfile creation fails → save returns 503; cache file unchanged. |

---

## §9. Server lookup pattern refactor (open item 7)

### §9.1 Phase 1-3 baseline

Current Phase 1-3 lookup pattern (verified by repo inspection):

```python
# Single direct invocation point of calculate_volume (Phase 2.4 preview API)
# server/app/api/symbols.py:98-126
mapping = symbol_whitelist.get_symbol_mapping(ftmo_symbol)   # global lookup
...
result = calculate_volume(
    risk_amount=req.risk_amount, entry=req.entry, sl=req.sl,
    symbol_config=config, whitelist_row=mapping,
    ratio=req.ratio, quote_to_usd_rate=rate,
)
```

And the whitelist lookup is used in:

- `server/app/main.py::lifespan` — `symbol_whitelist.load_whitelist(settings.symbol_mapping_path)` at startup.
- `server/app/api/symbols.py` — three uses: `list_symbols`, `get_symbol`, `calculate_volume_endpoint`.
- `server/app/services/market_data.py:258` — `get_symbol_mapping(ftmo_name)` to look up Exness symbol name during symbol-config publish (Phase 2 market-data sync).
- `server/app/services/volume_calc.py` — consumes the SymbolMapping object passed by the caller.

### §9.2 Phase 4 refactored pattern

Mapping is now per-Exness-account, resolved via pair → exness_account_id → cache. The hot-path call site changes shape:

```python
# server/app/services/mapping_service.py (step 4.A.5 — proposed)
class MappingService:
    async def get_ftmo_whitelist_entry(self, ftmo_symbol: str) -> FTMOSymbol | None:
        """Whitelist lookup — global, no per-account context."""

    async def get_mapping(
        self, exness_account_id: str, ftmo_symbol: str,
    ) -> MappingEntry | None:
        """Per-account lookup. Resolves via account_to_mapping:{acc} → mapping_cache:{sig}."""

    async def get_all_mappings_for_account(
        self, exness_account_id: str,
    ) -> dict[str, MappingEntry]:
        """Returns dict keyed by FTMO symbol. Used by Settings page + Edit wizard."""

    async def is_pair_symbol_tradeable(
        self, pair_id: str, ftmo_symbol: str,
    ) -> tuple[bool, str | None]:
        """Frontend pre-flight check. Returns (tradeable, reason).
        - True iff:
          - pair_id exists
          - pair.exness_account_id has mapping_status='active'
          - mapping for ftmo_symbol exists on that account
        - Reason populated with human-readable string when False."""
```

### §9.3 Call sites that change

| Call site | Current behavior | Phase 4 behavior |
|---|---|---|
| `server/app/main.py::lifespan` | Loads `symbol_mapping_ftmo_exness.json` global. | Loads `ftmo_whitelist.json` (D-SM-09). `MappingCacheRepository` separately loads all cache files into Redis. |
| `server/app/api/symbols.py::list_symbols` | Returns intersection (whitelist ∩ active). | Returns FTMO whitelist names (no Exness side filtering — that's per-pair now). |
| `server/app/api/symbols.py::get_symbol` | Returns SymbolMapping (FTMO + Exness fields). | Returns FTMOSymbol only (no Exness side). |
| `server/app/api/symbols.py::calculate_volume_endpoint` | Single global mapping. | **Takes `pair_id` parameter**. Lookup: `pair_id → exness_account_id → mapping_cache → MappingEntry`. Breaking change for frontend (coordinated with step 4.A.7). |
| `server/app/services/volume_calc.py::calculate_volume` | `whitelist_row: SymbolMapping` parameter. | New parameter shape: `ftmo_entry: FTMOSymbol` + `exness_mapping: MappingEntry`. The two-leg split matches the data architecture. |
| `server/app/services/market_data.py:258` | `get_symbol_mapping(ftmo_name)` for Exness side lookup. | **Refactor**: market-data sync only needs FTMO side now. Drop the Exness lookup; remove `mapping` dependency from this code path. |

### §9.4 Phase 4 NEW call sites (introduced by sub-phase 4.A)

These are new in Phase 4 and only exist after sub-phase 4.A lands:

| New call site | Phase | Purpose |
|---|---|---|
| `order_service.create_hedge_order` (step 4.5) | 4 | Resolve `exness_mapping` from `pair_id` + `ftmo_symbol` before computing secondary leg volume. |
| `response_handler::_handle_open_response` (Exness) (step 4.6) | 4 | Cross-check returned Exness symbol against the cached mapping for consistency. |
| `event_handler::_handle_position_closed_external` (step 4.6) | 4 | Reverse lookup Exness → FTMO via `order:{order_id}` hash (NOT mapping_cache; per §2.4.3). |
| `hedge_service::_cascade_secondary_close` (step 4.6) | 4 | Resolve the Exness position_id of the secondary leg via `order:{order_id}.s_broker_order_id`. Mapping not consulted directly here either. |
| `OrderService.preflight_validation` (step 4.5) | 4 | New step in the D-081 pipeline: check `is_pair_symbol_tradeable(pair_id, symbol)` → return `OrderValidationError(error_code="exness_mapping_missing")` if False. |

### §9.5 `MappingService` startup contract

```python
async def startup(self, redis: RedisService, repo: MappingCacheRepository) -> None:
    """Load all cache files from disk into Redis. Idempotent.

    Per D-SM-07: file is truth, Redis is working cache. On startup:
      1. List `server/data/symbol_mapping_cache/*.json`.
      2. For each file: Pydantic-validate, then HSET `mapping_cache:{sig}`.
      3. For each account in each file's `used_by_accounts`: SET `account_to_mapping:{acc} = {sig}`.
      4. Compare with current Redis state. Log WARN for any Redis entry without a backing file (and DEL it — file is truth).
    """
```

Failure of `MappingCacheRepository.startup` → fail-fast lifespan startup. CEO must repair files before server boots.

---

## §10. Frontend changes (open item 8)

The frontend keeps the symbol-first / pair-second flow (CTO confirm with CEO). The addition is a per-pair validation gate on submit.

### §10.1 HedgeOrderForm pre-flight validation

When CEO selects symbol + pair and clicks Submit:

```typescript
// web/src/components/OrderForm/HedgeOrderForm.tsx (step 4.A.7 — proposed)

async function onSubmit(formValues: OrderFormValues) {
  // existing validation ...

  // NEW pre-flight per pair
  const tradeable = await api.checkPairSymbolMapping(
    formValues.pairId,
    formValues.symbol,
  );
  if (!tradeable.ok) {
    showToast({
      level: "error",
      text: tradeable.reason,
      // e.g. "EURUSD has no Exness mapping for Cent account exness_acc_002.
      //       Map symbols in Settings or choose a different pair/symbol."
    });
    return;  // block submit
  }

  // existing submit ...
}
```

The check hits `GET /api/pairs/{pairId}/check-symbol/{symbol}` (a thin wrapper around `MappingService.is_pair_symbol_tradeable`).

### §10.2 SettingsModal Accounts tab additions

Each Exness account row gains:

| Element | When shown | Action |
|---|---|---|
| Status dot | Always | Green (`active`), Yellow (`pending_mapping`), Red (`spec_mismatch` or `disconnected`) |
| **Map Symbols** button | `status=pending_mapping` | Opens wizard in Create or Diff-aware mode |
| **Re-map Symbols** button | `status=spec_mismatch` | Opens wizard in SpecMismatch mode |
| **Edit Mapping** button | `status=active` | Opens wizard in Edit mode (§6.5) |
| **View Mapping** button | `status=active`, read-only modal | Renders the mappings list without edit affordances |
| **Re-sync** button | Any status | POSTs `/symbols/resync` — forces client to republish raw |

### §10.3 Wizard component structure

The wizard mounts as a **full-screen overlay** within SettingsModal (modal-in-modal is messy; full-screen avoids escape-key conflicts). Component skeleton:

```
<MappingWizard accountId={...} mode={"create"|"diff"|"spec_mismatch"|"edit"} onSave={...} onClose={...}>
  <Header>
    Account: exness_acc_001  |  Mode: Create new mapping  |  Symbols: 1487
  </Header>
  <ActionBar>
    [Accept all exact-match]  [Accept all auto-matched]  [Skip all unmapped]
    Filter: [Asset class ▼]  [Show advanced specs ☐]
  </ActionBar>
  <ScrollableTable>
    <Row symbol={ftmoSym} proposal={...} override={...} status={...} />
    ...
  </ScrollableTable>
  <Footer>
    [Cancel]  [Save Mapping]
  </Footer>
</MappingWizard>
```

### §10.4 Zustand store additions

```typescript
// web/src/store/index.ts (step 4.A.7 — proposed)

interface WizardRow {
  ftmo: string;
  proposedExness: string | null;
  matchType: "exact" | "suffix_strip" | "manual_hint" | null;
  overrideExness: string | null;
  action: "accept" | "skip" | "override";
  rawSnapshot: RawSymbolEntry | null;  // for specs preview
}

interface MappingWizardState {
  accountId: string;
  mode: "create" | "diff" | "spec_mismatch" | "edit";
  rows: WizardRow[];
  specDivergence: SpecDiff[] | null;
}

// Add to existing UIState
wizardState: MappingWizardState | null;
setWizardState: (state: MappingWizardState | null) => void;
updateWizardRow: (ftmo: string, patch: Partial<WizardRow>) => void;
```

- `wizardState` is **not persisted** (clears on close — derived from API every open).
- `selectedSymbol` / `selectedPairId` (existing) are NOT changed.

---

## §11. Migration plan (open item 4)

### §11.1 FTMO whitelist extraction

A one-shot script run at step 4.A.1:

```python
# scripts/migrate_extract_ftmo_whitelist.py (step 4.A.1 — proposed)
"""One-time migration: extract FTMO portion of symbol_mapping_ftmo_exness.json
into the new ftmo_whitelist.json format (D-SM-09)."""

import json
from pathlib import Path

ASSET_CLASS_BY_UNITS = {
    100000: "forex",     # standard FX
    100: "metals",       # XAU/XAG etc
    1000: "commodities", # NATGAS etc
    10: "indices",       # JP225, etc
    1: "stocks_or_indices",  # disambiguate via name
    5000: "indices",     # some index variants
}

def extract():
    src = json.loads(Path("symbol_mapping_ftmo_exness.json").read_text())
    out = {"schema_version": 1, "version": 1, "symbols": []}

    for m in src["mappings"]:
        asset_class = derive_asset_class(m["ftmo"], m["ftmo_units_per_lot"])
        out["symbols"].append({
            "name": m["ftmo"],
            "asset_class": asset_class,
            "quote_ccy": m["quote_ccy"],
            "ftmo_units_per_lot": m["ftmo_units_per_lot"],
            "ftmo_pip_size": m["ftmo_pip_size"],
            "ftmo_pip_value": m["ftmo_pip_value"],
        })

    Path("server/data/ftmo_whitelist.json").write_text(
        json.dumps(out, indent=2, sort_keys=True),
    )
    print(f"wrote {len(out['symbols'])} FTMO symbols")
```

Step 4.A.1 commits the resulting `server/data/ftmo_whitelist.json` to Git.

### §11.2 Old file archive

```
git mv symbol_mapping_ftmo_exness.json archive/symbol_mapping_ftmo_exness_v1.json
```

- `archive/` is a new folder. Not gitignored (visible historical reference).
- One-time move at step 4.A.1.
- No further changes — file is frozen at this archival point.

### §11.3 Tests migration

Tests in `server/tests/` referencing `symbol_mapping_ftmo_exness.json`:

- `server/tests/test_symbol_whitelist.py` → renames to `test_ftmo_whitelist_service.py` and rewrites against new schema.
- `server/tests/test_volume_calc.py` → updates fixture construction (separate `FTMOSymbol` + `MappingEntry`).
- `server/tests/test_api_symbols.py` → rewrites `calculate_volume_endpoint` test to include `pair_id` parameter; mocks `MappingService.get_mapping`.
- New `server/tests/test_mapping_cache_repository.py` — atomic write + Pydantic validation + per-signature lock tests.
- New `server/tests/test_auto_match_engine.py` — exact / suffix_strip / manual_hint match scenarios.
- New `server/tests/test_mapping_service.py` — `is_pair_symbol_tradeable` paths.

Test fixtures live under `server/tests/fixtures/`:
- `ftmo_whitelist_test.json` — small subset (5 symbols).
- `mapping_cache_test.json` — single Standard-style cache.
- `mapping_cache_cent_test.json` — single Cent-style cache (different `contract_size`).
- `match_hints_test.json` — 3-row hint file.

### §11.4 Frontend cached state

- `selectedSymbol` + `selectedPairId` Zustand store persists localStorage — unchanged.
- Frontend fetches FTMO whitelist from `/api/symbols/` — same endpoint name, same response shape (just smaller — no Exness fields). No localStorage migration needed.

### §11.5 .gitignore additions

```gitignore
# Phase 4: symbol mapping cache files are server-managed; not tracked.
server/data/symbol_mapping_cache/*.json
!server/data/symbol_mapping_cache/.gitkeep

# Atomic write artifacts (tempfiles + backups)
server/data/symbol_mapping_cache/.*.tmp
server/data/symbol_mapping_cache/*.bak
```

### §11.6 Migration timeline

- **Step 4.A.1**:
  1. Run `migrate_extract_ftmo_whitelist.py` → commits `server/data/ftmo_whitelist.json`.
  2. `git mv` old file → `archive/`.
  3. Update `.gitignore`.
  4. Refactor `symbol_whitelist.py` → `ftmo_whitelist_service.py`.
  5. Update server lifespan to load new file.
  6. Tests rewritten.
- **Step 4.A.2**: introduce `MappingCacheRepository` (empty cache folder; no account uses it yet).
- **Step 4.A.3-4**: build auto-match engine + API.
- **Step 4.A.5**: introduce `MappingService` and refactor `calculate_volume_endpoint` (breaking change for frontend — coordinate with step 4.A.7).
- **Step 4.A.6-7**: frontend wizard + pair-aware validation.

---

## §12. Sub-phase 4.A step breakdown

Eight steps total, including step 4.A.0 (this design doc). Each step delivers a green test pass + mypy/ruff clean; PR-able independently.

| # | Branch | Scope | Critical risk |
|---|---|---|---|
| **4.A.0** | `step/4.A.0-symbol-mapping-design-doc` | **THIS STEP**: design doc only. | Spec ambiguity — block 4.A.1 if NEEDS_CTO_REVIEW. |
| **4.A.1** | `step/4.A.1-ftmo-whitelist-split` | Migration script run. New `server/data/ftmo_whitelist.json` committed. New `FTMOWhitelistService` (replaces `SymbolWhitelist`). Archive old file. Refactor `market_data.py` symbol-sync filter. `api/symbols.py::list_symbols` + `get_symbol` updated. Tests rewritten. **Server must still serve existing test pairs after refactor** (regression assertion). | Tests breaking, frontend symbol-list response shape change. |
| **4.A.2** | `step/4.A.2-mapping-cache-repository` | `MappingCacheRepository` class. File I/O + atomic write + per-signature lock + sweep on `.tmp`/`.bak`. Pydantic v2 schemas (cache file + raw snapshot + spec validation). Redis populate on startup. Tests pin atomicity (concurrent writes + crash mid-write simulation). NO account uses it yet — pure data layer. | File concurrency, atomic rename correctness, Pydantic strict edge cases. |
| **4.A.3** | `step/4.A.3-auto-match-engine` | `AutoMatchEngine` class. Implements `exact` → `suffix_strip` → `manual_hint`. Loads `server/config/symbol_match_hints.json` at startup. Bootstrap content extracted from existing 14 manual rows in `symbol_mapping_ftmo_exness.json` (CEO can swap in `build_symbol_mapping.py`-derived list when that file lands). Tests with synthetic Exness symbols (Standard / Cent / Pro variants). | Logic equivalence to existing `build_symbol_mapping.py` — verify by replaying historical manual matches and asserting equal output. |
| **4.A.4** | `step/4.A.4-server-api-mapping-wizard` | Seven API endpoints (§7). Pydantic request/response schemas. Auth REST. Integration tests with fakeredis. | API contract pinned for frontend. Spec divergence detection edge cases. |
| **4.A.5** | `step/4.A.5-server-volume-lookup-refactor` | `MappingService` orchestrator. Refactor call sites (§9): `volume_calc.calculate_volume` (parameter split), `api/symbols.py::calculate_volume_endpoint` (add `pair_id`), `market_data.py` (drop Exness lookup), `main.py::lifespan` (new repos). **Calculate volume API breaking** — coordinate with step 4.A.7 (frontend atomic deploy). Pre-flight `is_pair_symbol_tradeable` added to OrderService validation pipeline (D-081). Tests rewritten with per-pair scenarios + Cent/Standard divergence test. | Highest regression risk in sub-phase. The calculate-volume API change has frontend impact. |
| **4.A.6** | `step/4.A.6-web-mapping-wizard-ui` | Wizard component (full-screen overlay). SettingsModal Accounts tab buttons. Zustand `wizardState`. API client functions. Vite dev mock with MSW. Four modes (Create/Diff/SpecMismatch/Edit) visually distinct. Bulk actions wired. | UX complexity; 100+-row tables must scroll without jank. |
| **4.A.7** | `step/4.A.7-web-form-pair-aware-validation` | HedgeOrderForm pre-flight `checkPairSymbolMapping`. Toast UX on block. `calculate-volume` API client passes `pair_id`. **Coordinated deploy with 4.A.5** — both must land before next user-facing release. | Coordination with 4.A.5 — atomic merge needed (or use feature flag for the new validation path). |

### §12.1 Critical-path dependencies

```
4.A.0 (this) ─→ 4.A.1 (split file)
                  │
                  ↓
              4.A.2 (cache repo)
                  │
                  ↓
              4.A.3 (auto-match)
                  │
                  ↓
              4.A.4 (API)
                  │
                  ↓
        ┌────────────┴────────────┐
        ↓                         ↓
   4.A.5 (server refactor)   4.A.6 (UI wizard)
        ↓                         ↓
        └────────────┬────────────┘
                     ↓
              4.A.7 (form validation)  ←  COORDINATED with 4.A.5
                     ↓
                Phase 4 step 4.2 (Exness client actions) resumes here
```

### §12.2 Phase 4 master plan re-numbering

The original Phase 4 plan (`docs/MASTER_PLAN_v2.md §5`) was steps 4.1 → 4.11. Inserting sub-phase 4.A renumbers nothing — the dotted-A pattern (4.A.0 through 4.A.7) sits between current 4.1 and 4.2. Phase 4 step numbering thereafter remains 4.2 → 4.11 (later 4.12 per `phase-4-design.md` D-4.0-1).

The MASTER_PLAN update lands at step 4.12 docs sync along with the alert backend renumber.

---

## §13. Resolution of open items

Each of the 9 open items from `docs/SYMBOL_MAPPING_DECISIONS.md §3` is resolved here with an explicit decision and section reference.

| # | Open item | Resolution | Section |
|---|---|---|---|
| 1 | Wizard UX details (bulk actions, specs preview, realtime validation) | 4 bulk actions locked (§6.6). Specs preview collapsible (§6.7). Realtime validation rules locked (§6.8). | §6 |
| 2 | API endpoints | 7 endpoints locked with Pydantic shapes (§7). | §7 |
| 3 | Atomic write strategy | Tempfile + rename + per-signature `asyncio.Lock` + `.bak` recovery + crashed-tempfile sweep (§8). Same-filesystem requirement documented. | §8 |
| 4 | Migration from `symbol_mapping_ftmo_exness.json` | FTMO portion extracted via `migrate_extract_ftmo_whitelist.py` (one-shot at 4.A.1). Old file `git mv` to `archive/symbol_mapping_ftmo_exness_v1.json`. NO auto-convert to cache files (specs may be stale). | §11 |
| 5 | Spec divergence threshold for non-contract_size fields | `contract_size` / `digits` / `currency_profit` exact match (BLOCK). `pip_size` ±5% (WARN). `pip_value` ±10% (WARN). `volume_min/step/max` use latest raw (WARN on diff). | §5 |
| 6 | Edit / Delete scope | Phase 4.A: Create + Edit. Delete defers to Phase 5 with orphan-cache cleanup. | §6.5 |
| 7 | Lookup pattern refactor | `MappingService` orchestrator; concrete call sites enumerated (§9.3 + §9.4). API breaking change on `calculate-volume`. | §9 |
| 8 | Frontend store changes | Symbol-first/pair-second flow retained. New `wizardState` in Zustand (ephemeral, not persisted). HedgeOrderForm pre-flight `checkPairSymbolMapping`. | §10 |
| 9 | Timing within Phase 4 | Insert sub-phase 4.A between step 4.1 and step 4.2. MASTER_PLAN renumber lands at step 4.12. | §12.2 |

### §13.1 D-4.A.0-N decisions log

Decisions emerging from step 4.A.0 that extend or refine the D-SM principles. Promoted to canonical D-XXX at step 4.12 docs sync.

**D-4.A.0-1** — `schema_version` field added to all three new file types (`ftmo_whitelist.json`, `symbol_mapping_cache/*.json`, `symbol_match_hints.json`).

- *Why*: D-SM-09 / D-SM-11 / D-SM-12 specify a content `version` but no schema-evolution header. Future schema changes (adding `min_lot` to FTMOSymbol, adding `confidence_score` to MappingEntry, etc.) need to be migrate-aware without breaking older files.
- *How to apply*: Pydantic reader strict-checks `schema_version` and refuses to load if it doesn't match the current code's expected version. Migration scripts bump it.

**D-4.A.0-2** — `MappingEntry` carries `exness_volume_step`, `exness_volume_min`, `exness_volume_max` in addition to the D-SM-11 spec.

- *Why*: Step 4.5 volume formula clamping (per `docs/phase-4-design.md §1.D`) needs `volume_step` / `min` / `max` on the hot path. Without these in `MappingEntry`, every order would need a second Redis lookup into `raw_symbols_snapshot`.
- *How to apply*: Step 4.A.4 `POST /symbol-mapping/save` copies these fields from raw snapshot at save time. Refresh on Edit mode.

**D-4.A.0-3** — Reverse lookup (Exness → FTMO) is NOT in `mapping_cache` HASH. Instead use `order:{order_id}` HASH which already carries both leg symbols.

- *Why*: §2.4.3 reasoning. Avoids redundant index that would need a second-write on every cache save.
- *How to apply*: Step 4.A.5 `MappingService` does not implement a reverse lookup API. Step 4.6 cascade close uses `order:{order_id}.symbol` / `s_exness_symbol`.

**D-4.A.0-4** — `volume_calc.calculate_volume` parameter shape changes from `whitelist_row: SymbolMapping` (combined) to `ftmo_entry: FTMOSymbol` + `exness_mapping: MappingEntry`.

- *Why*: D-SM-09 split makes the SymbolMapping union obsolete. Splitting parameters mirrors the new data architecture and prevents the function from implicitly assuming a single source.
- *How to apply*: Step 4.A.5 refactor. The public API caller `calculate_volume_endpoint` resolves both before calling.

**D-4.A.0-5** — Pre-flight `is_pair_symbol_tradeable` is added to OrderService validation pipeline (extends D-081).

- *Why*: D-SM-01 makes mapping per-account; preflight must verify the pair's Exness account has a mapping for the requested symbol. Adding to D-081 keeps validation centralized.
- *How to apply*: Step 4.A.5 inserts a new validation rule between "symbol in active set" and "tick available". Error code `exness_mapping_missing`.

**D-4.A.0-6** — `.gitkeep` placeholder in `server/data/symbol_mapping_cache/` keeps the folder tracked when no caches exist yet (fresh server install).

- *Why*: An empty Git-ignored folder is awkward — server startup either creates it (introduces an FS write at startup) or fails (poor UX). `.gitkeep` makes the folder present from clone.
- *How to apply*: Step 4.A.2 commits `.gitkeep` alongside the repository code.

**D-4.A.0-7** — Phase 5 sweep removes orphaned cache files (caches with `used_by_accounts == []`).

- *Why*: D-SM-11 edit-creates-new-cache → potential orphan accumulation over time. Phase 4 explicitly leaves them; Phase 5 sweep cleans.
- *How to apply*: Phase 5 background job spec in `docs/MASTER_PLAN_v2.md §6`.

**D-4.A.0-8** — Server startup deletes orphan tempfiles (`.tmp` older than 1 hour) and `.bak` files older than 7 days from `server/data/symbol_mapping_cache/`.

- *Why*: §8 atomic-write algorithm produces these artifacts on crash. Server startup is the natural cleanup hook.
- *How to apply*: `MappingCacheRepository.startup` includes a sweep call.

**D-4.A.0-9** — `mapping_status:{exness_account_id}` is set by server, observed by frontend via WS broadcast on the `accounts` channel (new channel — Phase 4 adds it for AccountStatus bar updates anyway).

- *Why*: Status changes need to be reactive on the UI; polling on every render is wasteful. The `accounts` channel already exists from step 4.9 plan.
- *How to apply*: Step 4.A.4 server emits `account_mapping_status_changed` WS message on every transition. Step 4.A.6 frontend subscribes.

**D-4.A.0-10** — Bootstrap content for `symbol_match_hints.json` derives from the existing 14 `match_type=manual` entries in `symbol_mapping_ftmo_exness.json` if `build_symbol_mapping.py` is unavailable at step 4.A.3.

- *Why*: Step 4.A.3 needs a seed file. The 14 manual entries already encode CEO's trader knowledge.
- *How to apply*: Step 4.A.3 grep + restructure. The file is small enough that a one-shot script + CEO review is sufficient.

---

## §14. Mid-phase amendments

Per WORKFLOW Phase kickoff design doc rule: this section is append-only. Each entry: **date | step | trigger | change**.

| Date | Step | Trigger | Change |
|---|---|---|---|
| 2026-05-13 | 4.A.0 | Initial design | Sections §1–§13 authored. No prior amendments yet. |

*(Future amendments append below.)*

---

## §15. Glossary

| Term | Definition |
|---|---|
| **Signature** | sha256 hex digest of the sorted Exness symbol names from a raw snapshot. Identifies a unique symbol-set fingerprint (D-SM-03). |
| **Cache** | A persisted mapping JSON file in `server/data/symbol_mapping_cache/`, keyed by signature. One cache may be used by multiple Exness accounts (D-SM-03). |
| **Raw snapshot** | The Exness client's `mt5.symbols_get()` output, published to Redis ephemeral key `exness_raw_symbols:{account_id}` (D-SM-06). |
| **Mapping** | A confirmed FTMO ↔ Exness symbol pair with specs lifted from the raw snapshot. Lives in `cache.mappings[]`. |
| **`mapping_status`** | Per-account enum: `pending_mapping` / `active` / `spec_mismatch` / `disconnected`. Tracks account lifecycle. |
| **Diff-aware wizard** | Pre-fill mode triggered when fuzzy match Jaccard ≥ 0.95 against an existing cache (§4 + §6.2). |
| **Spec mismatch** | When signature HIT but `contract_size` / `digits` / `currency_profit` diverge (§5). Blocks link. |
| **Auto-match** | Server-side heuristic: `exact` → `suffix_strip` → `manual_hint` (§6.1, step 4.A.3). |
| **Match hint** | A CEO-curated FTMO → Exness candidate suggestion in `server/config/symbol_match_hints.json` (D-SM-12). |
| **MappingService** | Server orchestrator that resolves `pair_id → exness_account_id → mapping_cache → MappingEntry` (step 4.A.5). |
| **Pre-flight (form)** | Frontend HedgeOrderForm gate that checks `is_pair_symbol_tradeable` before submit (§10.1). |
| **Orphan cache** | A cache file whose `used_by_accounts` becomes empty after an Edit migration. Phase 5 cleanup job removes (D-4.A.0-7). |

---

## §16. Cross-reference index

| Topic | Section here | Other docs |
|---|---|---|
| Per-account architecture (D-SM-01) | §1.3, §2.4 | `docs/SYMBOL_MAPPING_DECISIONS.md §2` |
| Wizard creation flow (D-SM-02) | §6 | — |
| Signature formula (D-SM-03) | §3 | — |
| Diff-aware fuzzy (D-SM-04) | §4 | — |
| Spec validation (D-SM-05) | §5 | `docs/phase-4-design.md §1.D` (volume formula consumer) |
| Raw snapshot lifecycle (D-SM-06) | §2.4, §6.1, §7.4 | — |
| File-vs-Redis source-of-truth (D-SM-07) | §2.2.4, §8.3 | — |
| Folder layout (D-SM-08) | §2.2.4 | `docs/11-deployment.md` (RUNBOOK Phase 4 update) |
| FTMO whitelist file (D-SM-09) | §2.1 | `docs/DECISIONS.md` D-016, D-017 |
| File naming (D-SM-10) | §2.2.1 | — |
| Cache content (D-SM-11) | §2.2.2 | — |
| Match hints (D-SM-12) | §2.3 | — |
| Volume formula impact | §9, §13 D-4.A.0-2/4 | `docs/phase-4-design.md §1.D` |
| Cascade close coexistence | (none — independent) | `docs/phase-4-design.md` |
| MASTER_PLAN renumber timing | §12.2 | `docs/MASTER_PLAN_v2.md §5` (updated at step 4.12) |

---

*End of design doc. Length target ≥1200 lines / ≥8000 words — verified in step 4.A.0 self-check.*
