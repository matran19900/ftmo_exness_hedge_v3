# MT5 (Exness) Execution Events — Skeleton

**Status**: SKELETON. Sections 1–8 are placeholders. Content populated mid-Phase-4 by steps 4.1 / 4.2 / 4.5 / 4.6 as the MT5 Python lib (`MetaTrader5` package) quirks are discovered empirically through smoke tests.

This document mirrors `docs/ctrader-execution-events.md` for the Exness MT5 side. It is the canonical reference for MT5 retcodes, deal/order/position semantics, symbol suffix handling, position monitor polling, and the MT5-specific quirks that bite us in production.

**See `docs/phase-4-design.md` for the cascade close + alerts architecture that consumes the events documented here.**

---

## 1. Overview

MT5 Python integration constraint: **`MetaTrader5` package is Windows-only**. There is no Linux or macOS build. Phase 4 Exness client runs exclusively on Windows (CEO's local Windows box for Phase 4 demo; Windows Server 2022 for Phase 5 production deploy).

The package exposes a synchronous, blocking API. The Exness client wraps blocking calls in `asyncio.run_in_executor` to keep the Redis event loop responsive (analogous to FTMO bridge_service Twisted-in-thread pattern from Phase 3).

Key entry points:

| Function | Purpose | Phase 4 step |
|---|---|---|
| `mt5.initialize(...)` | Connect to local MT5 terminal IPC | 4.1 |
| `mt5.login(account, password, server)` | Auth | 4.1 |
| `mt5.account_info()` | Balance, equity, margin (push every 30s) | 4.2 |
| `mt5.positions_get()` | Open positions snapshot (poll every 2s) | 4.2 |
| `mt5.history_deals_get(...)` | Closed deal history (for close reconstruction) | 4.2 |
| `mt5.symbol_info(symbol)` | Symbol metadata (digits, contract size, etc.) | 4.1 |
| `mt5.order_send(request)` | Place order or close position | 4.2 |
| `mt5.last_error()` | Last retcode + message | 4.2 |

(Populate empirically per smoke during step 4.1 / 4.2.)

---

## 2. Order flows

### 2.1 Market order placement — successful flow

*(Placeholder. Populated by step 4.2 smoke. Expected: `order_send(request_type=ORDER_TYPE_BUY)` returns OrderSendResult with `retcode=10009` (TRADE_RETCODE_DONE), `deal != 0`, `order != 0`, `position` populated. Document deal vs order vs position ID semantics, response fields, and timing.)*

### 2.2 Market order placement — failure cases

*(Placeholder. Common failures: retcode 10004 REQUOTE, 10006 REJECT, 10018 MARKET_CLOSED, 10027 POSITION_NOT_FOUND, 10030 UNSUPPORTED_FILLING_MODE. Populate the full mapping during step 4.2 retcode work.)*

### 2.3 Close position — successful flow

*(Placeholder. Closing a position is `order_send` with `request_type=DEAL_TYPE_BUY/SELL` opposite to position direction, `position=<position_id>`. Document the request shape, the OrderSendResult, and the deal that results.)*

### 2.4 Limit / Stop order placement

*(Placeholder. Phase 4 ships market-only on Exness leg per R5; limit/stop deferred to Phase 5. This section will note the deferral.)*

### 2.5 SL/TP modification

*(Placeholder. Phase 4 does NOT set SL/TP on Exness leg per R3 — SL is on the FTMO leg only; Exness leg uses pure volume hedge. This section will state the design decision.)*

---

## 3. Retcode mapping

Known retcodes Phase 4 will encounter. Populate empirical observation during step 4.2.

| Retcode | Constant | Meaning | Server handling | Step |
|---|---|---|---|---|
| 10009 | TRADE_RETCODE_DONE | Success | (no action — happy path) | 4.2 |
| 10018 | TRADE_RETCODE_MARKET_CLOSED | Market closed (off-hours) | Reject open; Alert if cascade close | 4.2 |
| 10027 | TRADE_RETCODE_POSITION_NOT_FOUND | Position closed externally before our cmd executed | D-4.0-7 force reconcile | 4.2 |
| 10030 | TRADE_RETCODE_UNSUPPORTED_FILLING | Filling mode (IOC/FOK) unsupported by broker | Retry with alternate filling mode (IOC → FOK once) | 4.2 |
| 10006 | TRADE_RETCODE_REJECT | Generic reject | Capture error_msg verbatim; surface to operator | 4.2 |
| 10004 | TRADE_RETCODE_REQUOTE | Price moved between request and execution | Retry once with fresh tick (Phase 5) | 4.2 |
| 10014 | TRADE_RETCODE_INVALID_VOLUME | Volume outside symbol limits | Validation should have caught; ERROR log | 4.2 |
| 10015 | TRADE_RETCODE_INVALID_PRICE | Price stale | Retry once with fresh tick (Phase 5) | 4.2 |
| 10016 | TRADE_RETCODE_INVALID_STOPS | SL/TP outside allowed band | Phase 5 only (Phase 4 no SL/TP on Exness) | 4.2 |
| 10019 | TRADE_RETCODE_NO_MONEY | Insufficient margin | Reject open; Alert path TBD step 4.7 | 4.2 |

Step 4.2 lock: ``exness_client/retcode_mapping.py::RETCODE_MAP`` is the single source of truth used by the action handlers.
``map_retcode(retcode)`` → ``RetcodeOutcome(status, reason, retry_strategy)``. Server-side response handler (step 4.7) reads the same vocab off ``resp_stream:exness:{account_id}``.

*(Populate observed retcodes mid-phase per D-069. Update entries above with `Step` column indicating where empirically verified.)*

---

## 4. Symbol suffix handling

Exness MT5 symbols carry broker-specific suffixes that differ from cTrader's canonical names. Examples:

| Canonical (FTMO/cTrader) | Exness MT5 | Suffix pattern |
|---|---|---|
| EURUSD | EURUSDm | `m` (mini/micro account variant) |
| XAUUSD | XAUUSDm | `m` |
| US30 | US30Cash | `Cash` (CFD vs futures distinction) |

Resolution: `symbol_mapping_ftmo_exness.json` (existing) provides the bidirectional map.

Lookup pattern (Phase 4):

```python
def resolve_exness_symbol(ftmo_symbol: str, mapping: dict) -> str:
    entry = mapping.get(ftmo_symbol)
    if entry is None:
        raise SymbolMappingMissing(ftmo_symbol)
    return entry["exness"]["symbol"]
```

Edge cases (populate during step 4.1 sync):

- Symbol not subscribed in MT5 terminal MarketWatch: `symbol_info()` returns None → must call `symbol_select()` first.
- Symbol exists in mapping but not in MT5 server's symbol list (broker delisted): startup check fails fast.
- Symbol name case sensitivity: MT5 names are case-sensitive on most brokers but tolerant on some; we always use the exact case from the mapping file.

---

## 5. Position monitor poll mechanics

The position_monitor loop runs every 2 seconds on the Exness client (D-4.0-2). Algorithm:

```
loop forever:
    sleep 2s
    snapshot = mt5.positions_get()              # blocking; via executor
    snapshot_ids = {p.ticket for p in snapshot}
    prev_ids = redis.smembers("exness_open_positions:{account}")

    closed = prev_ids - snapshot_ids
    opened = snapshot_ids - prev_ids            # rare; positions opened by us
                                                # should already be in Redis

    for pos_id in closed:
        # External close detected
        deals = mt5.history_deals_get(position=pos_id)
        close_info = reconstruct_close(deals)
        XADD event_stream:exness:{account}
              msg_type=position_closed_external
              position_id=pos_id
              close_price=close_info.price
              close_reason=close_info.reason
              closed_at=close_info.ts
              realized_pnl_raw=close_info.profit_raw
              commission=...
              swap=...
              money_digits=...

    redis.delete("exness_open_positions:{account}")
    redis.sadd("exness_open_positions:{account}", *snapshot_ids) if snapshot_ids
```

Edge cases (populate during step 4.2):

- `positions_get()` returns None (MT5 disconnected): retry; if 3 consecutive fails → Alert 4c broker_disconnect.
- `history_deals_get()` returns empty for a position that we just observed missing: likely a transient race; retry once after 500ms before publishing.
- Server restart: prev_ids HAS persisted state; reconstruction works across restart.

### 5.a Step 4.3 implementation summary

Step 4.3 ships a leaner version of the §5 sketch — no `exness_open_positions:{account}`
SET in Redis, no `history_deals_get` reconciliation. Step 4.4 adds the persistence + deal
reconstruction; step 4.3 is the in-process diff layer that fires the events.

| Position lifecycle event | mt5 API access | Step | Notes |
|---|---|---|---|
| Detect new position (open via cmd OR manual) | `positions_get()` poll diff (current − last) | 4.3 | 2 s interval; first poll = silent baseline |
| Detect closed externally (manual / SL-TP hit / stop-out) | `positions_get()` poll diff (last − current) | 4.3 | Triggers cascade Path B (server step 4.7/4.8) |
| Detect SL/TP modification (terminal-side edit) | `positions_get()` field diff against last snapshot | 4.3 | `changed_fields` enumerates `sl` / `tp` / `volume` |
| Volume change (partial close, future scope) | `positions_get()` field diff | 4.3 | Detected today; partial-close cmd-stream support is Phase 5 |
| Detect offline-period diff via persistent snapshot | snapshot vs current diff on first poll after restart | 4.3a | Closes the leg-open gap CEO surfaced in Windows smoke (modify→stop→manual-close→restart) |
| Enrich closed event with broker fill data | `history_deals_get(position=ticket)` | 4.3a | `close_price` / `realized_profit` / `commission` / `swap` / `close_time_ms`; falls back to `enrichment_source="snapshot_fallback"` on error |
| Classify close as `server_initiated` vs `external` | client-side `CmdLedger` Redis SET | 4.3a | Server step 4.7 will route `external` → WARNING alert (no FTMO cascade) |

Event stream key: `event_stream:exness:{account_id}`. Payload schema (flat string dict):
``event_type``, ``broker_position_id``, ``ts_ms``, ``symbol``, ``side`` (+ event-specific extras
listed in `exness_client/position_monitor.py`). Step 4.3a stamps every
``position_closed_external`` payload with ``close_reason`` (``server_initiated``
or ``external``) plus ``enrichment_source`` (``history_deals`` or
``snapshot_fallback``); the cascade orchestrator (step 4.7) pattern-matches
on ``close_reason``.

Persistent snapshot key: ``position_monitor:last_snapshot:{account_id}`` —
JSON STRING with 30-day TTL. Schema: ``{schema_version, last_poll_ts_ms,
positions: [{ticket, symbol, volume, sl, tp, position_type}]}``.

Cmd ledger key: ``cmd_ledger:exness:{account_id}:server_initiated`` —
Redis SET with 24-hour TTL. Members are stringified MT5 tickets.

### 5.b Step 4.4 terminal_info gate

| Gate behaviour | mt5 API | Step | Notes |
|---|---|---|---|
| Detect broker disconnect transient → skip poll entirely | `terminal_info().connected` check before `positions_get` | 4.4 | Refines D-4.3-4: ``positions_get → empty`` no longer treated as "every position closed" when the cause is a transient connection loss. The in-process snapshot, ``_baseline_done`` flag, and persisted Redis snapshot are all preserved across disconnected polls so the next reconnect resumes the diff against truth. |
| `terminal_info()` returns ``None`` → treated as disconnect | n/a | 4.4 | Same skip path; matches MT5 doc behaviour where ``terminal_info()`` returns ``None`` while the lib reconnects. |
| `terminal_info()` raises → treated as disconnect | n/a | 4.4 | Logged + skipped; the loop survives without crashing. |

### 5.c Step 4.4 account info publish

Per-account 30-second poll of ``mt5.account_info()`` → Redis HASH at
``account:exness:{account_id}``. Drives the server's position tracker
(step 4.9 — unrealised P&L baselines) and the frontend
``AccountStatusBar`` (step 4.10 — live balance / equity / margin).

| Field | mt5 API | Step | Notes |
|---|---|---|---|
| login | `account_info().login` | 4.4 | Broker account ID |
| balance | `account_info().balance` | 4.4 | Account base currency |
| equity | `account_info().equity` | 4.4 | Balance + unrealised P&L |
| margin | `account_info().margin` | 4.4 | Used margin |
| free_margin | `account_info().margin_free` | 4.4 | Available margin |
| leverage | `account_info().leverage` | 4.4 | 1:N ratio (e.g. 500) |
| currency | `account_info().currency` | 4.4 | Account base currency code (3-letter) |
| server | `account_info().server` | 4.4 | Broker server name |
| margin_mode | `account_info().margin_mode` | 4.4 | Must be ``ACCOUNT_MARGIN_MODE_RETAIL_HEDGING`` (Phase 4 R1); the server uses the published value as a defence-in-depth check before issuing a hedge cmd |
| broker | (literal "exness") | 4.4 | Routing key for server-side consumers |
| account_id | (settings.account_id) | 4.4 | Echo of the per-account routing |
| synced_at_ms | `time.time() * 1000` | 4.4 | Liveness; the AccountStatusBar greys-out the row when this is older than ~60 s |

Constants: ``POLL_INTERVAL_S = 30.0`` locked. The first publish runs
*immediately* on entry so the server sees a populated HASH before the
first 30-second interval elapses (otherwise the AccountStatusBar would
render a "—" placeholder for half a minute on every client restart).

---

## 6. MT5 quirks summary

Populate empirically mid-phase per D-069 pattern. Bullet list grows as smoke uncovers behavior.

| Quirk | Discovered step | Workaround / Reference |
|---|---|---|
| Filling mode mismatch: broker may reject IOC with retcode 10030 (`TRADE_RETCODE_UNSUPPORTED_FILLING`); FOK accepted. | 4.2 | `exness_client/action_handlers.py::_handle_open` retries the same request once with `ORDER_FILLING_FOK` after an IOC reject. Single retry only; further failures pass through. |
| Pip size derivation: `mt5.symbol_info` exposes `point` (smallest price increment), not `pip`. 5-digit forex + 3-digit JPY-quote → pip = `point * 10`; 2-digit metals/indices/crypto → pip = `point` as-is. | 4.2 | `exness_client/symbol_sync.py::_derive_pip_size`. CTO Phase 4 lock — see step 4.2 self-check. |
| Symbol subscription required before tick/positions visible: must call `mt5.symbol_select(name, True)` to add the symbol to MarketWatch before `symbol_info(name)` returns valid bid/ask. | 4.2 | `SymbolSyncPublisher.publish_snapshot` calls `symbol_select` per enumerated symbol before reading `symbol_info`. |
| Close action requires `position` ticket on the request dict alongside `type` (opposite of position direction); MT5 does NOT accept "close ticket X" as a single primitive. | 4.2 | `_handle_close` looks up `positions_get(ticket=...)` first to discover direction + volume, then issues opposite-direction `TRADE_ACTION_DEAL` with `position=ticket`. |
| Filling mode is a BITMASK on the symbol side (`symbol_info.filling_mode`) but a flat ENUM on the request side (`ORDER_FILLING_*`). Bit 1 = FOK, Bit 2 = IOC, Bit 4 = BOC. Bitmask=1 (FOK-only, common on Cent demos) silently rejects IOC submissions with `None`. | 4.8a | `_handle_open` queries `info.filling_mode` and builds the request `type_filling` list (preferred + fallback) accordingly. `_handle_close` uses position's original filling mode broker-internally — hardcoded `ORDER_FILLING_IOC` without bitmask query. See §6.a below + step 4.8a self-check D-4.8a-1..5. |
| Comment field actual broker limit is 29 chars (NOT 31 per official docs). 30+ chars triggers silent `None` + `last_error == (-2, 'Invalid "comment" argument')`. | 4.8b + 4.8d | All MT5 request dicts use `"comment": f"v3:{request_id}"[:29]`. `_handle_open` (4.8b) + `_handle_close` (4.8d) carry the fix; regression tests audit each handler. See §6.b below. |
| `order_send` returns `None` (not a struct with retcode) for terminal-side validator rejections. The reason lives in `mt5.last_error()` — a separate call. | 4.8b + 4.8d | Defensive pattern: capture `last_error` after every `None`, log at WARNING + surface in response `error_msg`. See §6.c below. |
| `mt5.order_check()` does NOT validate comment field length. 31-char comment passes `order_check` (retcode 0) but then fails `order_send` with `None`. | 4.8d (smoke 2026-05-16) | Phase 5 backlog: add `order_check` as defensive pre-flight; currently NOT called. See §6.d below. |

### 6.a Filling mode bitmask vs ORDER_FILLING_* enum (step 4.8a)

`mt5.symbol_info(symbol).filling_mode` returns an `int` **bitmask**:

| Bit value | Symbol-side constant | Meaning |
|---|---|---|
| 1 | `SYMBOL_FILLING_FOK` | Fill-Or-Kill supported |
| 2 | `SYMBOL_FILLING_IOC` | Immediate-Or-Cancel supported |
| 4 | `SYMBOL_FILLING_BOC` | Book-Or-Cancel (passive — not used by market DEAL action) |

**Request-side** values for `request["type_filling"]` use a DIFFERENT enum:

- `ORDER_FILLING_FOK = 0`
- `ORDER_FILLING_IOC = 1`
- `ORDER_FILLING_RETURN = 2`

The bitmask → request mapping that `_handle_open` applies (step 4.8a `action_handlers.py:158-194`):

| Bitmask | First attempt | Fallback | Source |
|---|---|---|---|
| 3 (FOK + IOC) | `ORDER_FILLING_IOC` | `ORDER_FILLING_FOK` | Standard Exness — preserves historical IOC-first |
| 1 (FOK only) | `ORDER_FILLING_FOK` | `ORDER_FILLING_IOC` (defensive) | **Exness Cent path** — D-SMOKE-2 initial hypothesis (turned out comment was the real bug, but bitmask query stays as defence-in-depth) |
| 2 (IOC only) | `ORDER_FILLING_IOC` | `ORDER_FILLING_FOK` | Uncommon |
| 5 (FOK + BOC) | `ORDER_FILLING_FOK` | `ORDER_FILLING_IOC` | BOC ignored for DEAL |
| 7 (all) | `ORDER_FILLING_IOC` | `ORDER_FILLING_FOK` | Historical IOC-first |
| 0 (none) | `ORDER_FILLING_RETURN` | — | Theoretical; fallback safety |

**Scope**: open action only. CLOSE action uses position's original filling mode (broker-internal — `position.type_filling` field in the MT5 server's bookkeeping), so `_handle_close` hardcodes `ORDER_FILLING_IOC` without a bitmask loop. Verified in step 4.8d (`test_close_no_filling_mode_loop`). If the broker rejects with retcode `UNSUPPORTED_FILLING` (10030) on a close, the server's cascade orchestrator's retry budget (step 4.8 `cascade_close_other_leg` 3-retry) handles it; no client-side filling adaptation needed.

### 6.b Comment field 29-char limit (step 4.8b + 4.8d)

CEO REPL verification 2026-05-15 (Exness Standard demo, EURUSDm):

| Comment length | `order_send` result | `mt5.last_error()` |
|---|---|---|
| ≤ 29 chars | `OrderSendResult(retcode=10009, ...)` | `(0, "no error")` |
| 30 chars | `None` | `(-2, 'Invalid "comment" argument')` |
| 31 chars | `None` | `(-2, 'Invalid "comment" argument')` |
| 32+ chars | `None` | same |

Official MT5 docs claim 31 chars max. Real Exness validator enforces 29. The gap likely originates server-side (broker validator stricter than terminal-side). Other brokers may have different limits — 29 is the verified safe lower bound for Exness Standard demo + Cent demo.

**Operational discovery** (CEO 2026-05-15): the comment string lives in the broker's server-side trade audit. A prefix containing `hedge` (or any hedging-indicative keyword) may flag the account as running a detected hedging strategy → prop-firm ban risk. Neutral prefix required.

**Locked format across all MT5 handlers** (steps 4.8b + 4.8d):

```python
"comment": f"v3:{request_id}"[:29],
```

`v3:` (3 chars) + 26-char `request_id` slice = 29 chars exact for 32-char uuid hex inputs. Regression tests (`test_open_audit_no_hedge_keyword_in_request_dict`, `test_close_audit_no_hedge_keyword_in_request_dict`) stringify each request dict and assert `"hedge"` is absent — defence against any future regression.

### 6.c `mt5.last_error()` capture pattern (step 4.8b + 4.8d)

When `order_send` returns `None`, the reason is in `mt5.last_error()` — a separate sync call. Without capturing it, operators see only the opaque `order_send_returned_none` slug and have no way to root-cause silent validation failures.

Defensive pattern (applied in `_handle_open` 4.8b + `_handle_close` 4.8d):

```python
result = await _to_thread(self._mt5.order_send, request)
if result is None:
    last_err = await _to_thread(self._mt5.last_error)
    logger.warning(
        "{action}.order_send_none request_id=%s last_error=%s",
        request_id, last_err,
    )
    await self._publish_response(
        ...
        reason="order_send_returned_none",
        error_msg=f"order_send_returned_none last_error={last_err}",
        ...
    )
    return
```

`error_msg` lands on the resp_stream payload (Exness `_publish_response` accepts `**extras: str`), which the server's `response_handler:exness` routes into `s_error_msg` / `s_close_error_msg` (per step 4.7a v2-6). Operators see the actual reason on the order row, not the slug.

**Why D-SMOKE-2 took two smoke iterations**: 4.8b shipped the pattern for `_handle_open` but did NOT audit `_handle_close`. The 2026-05-16 cascade close smoke (post 4.8c FTMO fix) finally let close cmds reach the Exness client, surfacing the latent `_handle_close` instance of the same bug. Lesson — when fixing a bug class at one site, audit all sibling sites and apply the fix in a single step. This is what step 4.8d does (and this doc captures the lesson).

### 6.d `mt5.order_check` does NOT validate comment length (smoke 2026-05-16)

CEO REPL verification 2026-05-16: a 31-char comment passes `mt5.order_check(request)` with `retcode == 0` ("Done"), but then `mt5.order_send(request)` rejects with `None` + `last_error == (-2, 'Invalid "comment" argument')`.

So `order_check` is an INCOMPLETE pre-flight — useful for margin / volume validation but NOT for terminal-side validator constraints like comment length. Currently NOT called by `_handle_open` / `_handle_close`.

**Phase 5 backlog**: add `order_check` as a defensive pre-flight to surface margin / volume errors before the submit. Until then, the `last_error` capture in §6.c is the only safety net for silent validation rejections.

Examples of expected entries (verify empirically):

- Filling mode mismatch: broker requires FOK but our request asks IOC → 10030 retcode.
- Hedging vs netting account mode: Phase 4 assumes hedging mode (multiple positions per symbol); netting account would merge legs.
- Symbol subscription required before tick/positions visible.
- `account_info()` margin fields can be 0 outside trading hours.

---

## 7. MT5 native struct field paths (positions / orders / deals)

`MetaTrader5` returns NamedTuples (not protobuf). Field paths for the Phase 4 event flows. Populate with verified field names mid-phase.

```python
# mt5.positions_get() entry — TradePosition NamedTuple
#   ticket          : position id (int)
#   time            : open epoch
#   type            : POSITION_TYPE_BUY|SELL
#   magic           : 0 (we don't use)
#   identifier      : ?
#   reason          : POSITION_REASON_CLIENT|EXPERT|...
#   volume          : float (lots)
#   price_open      : float
#   sl, tp          : float (always 0 in Phase 4)
#   price_current   : float (broker server's view)
#   swap, profit    : float
#   symbol          : str
#   comment         : str
#   external_id     : str
# (verify field names against mt5 package version during step 4.1)

# mt5.history_deals_get() entry — TradeDeal NamedTuple
#   ticket          : deal id
#   order           : order id that produced the deal
#   time            : exec epoch
#   type            : DEAL_TYPE_BUY|SELL
#   entry           : DEAL_ENTRY_IN|OUT|INOUT (IN=open, OUT=close)
#   magic           : 0
#   position_id     : position id this deal affected
#   reason          : DEAL_REASON_CLIENT|EXPERT|SL|TP|SO|...
#   volume          : float
#   price           : float
#   commission      : float
#   swap            : float
#   profit          : float
#   fee             : float
#   symbol          : str

# mt5.order_send() result — OrderSendResult
#   retcode         : int
#   deal            : deal id (0 on fail)
#   order           : order id (0 on fail)
#   volume          : actual filled volume
#   price           : actual fill price
#   bid, ask        : tick at execution
#   comment         : broker comment string
#   request_id      : echo of our request id
#   retcode_external: broker-specific extended code
#   request         : echo of original request dict
```

*(Verify against installed `MetaTrader5` package via `python -c "import MetaTrader5 as mt5; print(dir(mt5))"` during step 4.1.)*

---

## 8. SL/TP modification flows

*(Placeholder. Phase 4 design: NO SL/TP on Exness leg per R3 — Exness leg is pure volume hedge tracking FTMO. This section will document the explicit design decision + reference to `phase-4-design.md` §1.D. Phase 5 may add SL/TP for advanced hedging strategies.)*

---

## 9. Update log

Append-only mid-phase per D-069 exception (mirrors `docs/ctrader-execution-events.md` §9 pattern). Each entry: **date | step | trigger | finding**.

| Date | Step | Trigger | Finding |
|---|---|---|---|
| 2026-05-13 | 4.0 | Design doc skeleton creation | Initial structure. Sections 1–8 placeholders; will populate during steps 4.1 (connect+symbol sync), 4.2 (actions+monitor+retcodes), 4.5 (full hedge flow), 4.6 (cascade integration). |
| 2026-05-14 | 4.2 | Initial action-handler implementation | §3 retcode table marked with `Step` column = 4.2 for the 10 mapped retcodes (DONE / REJECT / INVALID_VOLUME / INVALID_PRICE / INVALID_STOPS / MARKET_CLOSED / NO_MONEY / POSITION_NOT_FOUND / UNSUPPORTED_FILLING / REQUOTE). §6 quirks populated: IOC→FOK retry pattern, pip-size point*10 derivation for 3/5-digit symbols, MarketWatch `symbol_select` requirement, close-needs-position-ticket. ``RETCODE_MAP`` is the single source of truth (`exness_client/retcode_mapping.py`). |
| 2026-05-14 | 4.3 | Position monitor poll loop | §5.a populated: 3 event types (`position_new`, `position_closed_external`, `position_modified`) on `event_stream:exness:{account_id}`. Baseline pattern locked: first poll = silent snapshot (no replay events on client restart). `POLL_INTERVAL_S = 2.0` locked. SL/TP/volume diff detection runs entirely off the in-process snapshot — no `history_deals_get` reconciliation in this step (deferred to step 4.4). Event order is deterministic: news first (sorted by ticket), then closed, then modified. |
| 2026-05-14 | 4.3a | Persistent snapshot + cmd ledger | Closes the Windows-smoke leg-open gap: persistent snapshot at `position_monitor:last_snapshot:{account_id}` (JSON STRING, 30-day TTL) saved after every poll and loaded on the first poll of the next process so offline closes/modifies/opens replay correctly. `position_closed_external` events now carry `close_reason` (`server_initiated` via `CmdLedger` lookup OR `external`) + `enrichment_source` (`history_deals` OR `snapshot_fallback`) + broker fill fields (`close_price` / `realized_profit` / `commission` / `swap` / `close_time_ms`) when `mt5.history_deals_get(position=ticket)` succeeds. CEO policy: secondary leg always passive — server step 4.7 will turn every `external` close on a hedge leg into a WARNING alert (no FTMO cascade). |
| 2026-05-14 | 4.4 | Account info publish + terminal_info gate | §5.b populated: ``terminal_info().connected`` check now gates the position-monitor poll. Refines D-4.3-4 — a transient broker disconnect previously caused ``positions_get → empty → emit position_closed_external for every snapshot ticket`` (false WARN-alert spam); the gate now skips the poll entirely and preserves both the in-process and Redis snapshots. ``terminal_info() == None`` and exceptions are also treated as disconnect. §5.c populated: ``AccountInfoPublisher`` 30-second loop publishes ``account:exness:{account_id}`` HASH (12 fields incl. ``balance``/``equity``/``margin``/``free_margin`` for the AccountStatusBar + position-tracker P&L baselines). First publish runs immediately on entry. ``ShutdownCoordinator`` order: cmd_proc → position_monitor → account_info_publisher → heartbeat → bridge.disconnect → redis.aclose. |
| 2026-05-15 | 4.8a | Filling-mode bitmask discovery | §6 quirks table appended: `symbol_info.filling_mode` is a bitmask (bit 1 = FOK, bit 2 = IOC, bit 4 = BOC) distinct from the request-side `ORDER_FILLING_*` enum (FOK=0, IOC=1, RETURN=2). `_handle_open` queries the bitmask + builds the request `type_filling` fallback list per §6.a mapping table. Exness Cent demo EURUSDm reports `filling_mode=1` (FOK-only) which silently rejected IOC submissions — initial hypothesis for D-SMOKE-2. Also added belt-and-suspenders `mt5.symbol_select(symbol, True)` per order so a silent symbol-sync miss can't strand an order. |
| 2026-05-15 | 4.8b | Comment 29-char limit + `last_error` pattern | §6 quirks table appended + §6.b/§6.c populated. CEO REPL verified: 30+ char comments trigger silent `None` + `last_error == (-2, 'Invalid "comment" argument')` — official MT5 docs claim 31 chars max; real Exness validator enforces 29. Locked format: `"comment": f"v3:{request_id}"[:29]`. Operational discovery: comment prefix MUST NOT contain `hedge` keyword (prop-firm ban risk for detected hedging). `last_error` capture pattern: WARNING log + surface in response `error_msg` so silent validation rejections don't masquerade as opaque `order_send_returned_none`. Initial application: `_handle_open` only. |
| 2026-05-16 | 4.8c | (Cross-broker context) | FTMO client `ctrader_bridge.close_position` started publishing `position_closed` to `event_stream:ftmo` for solicited closes (D-SMOKE-9 fix unblocks Phase 4 cascade close Path A). Documented separately in `docs/ctrader-execution-events.md` §3.8 — relevant here because the Phase 4 cascade-close smoke that this fix unblocked then surfaced D-SMOKE-10 (the Exness close-path equivalent of D-SMOKE-2) which step 4.8d addresses below. |
| 2026-05-16 | 4.8d | `_handle_close` mirrors 4.8b + lesson | §6 quirks table appended + §6.d added. `_handle_close` had the same comment-31-char bug as pre-4.8b `_handle_open` — only with `"close:"` prefix instead of `"hedge:"`, same 31-char silent-None failure mode (D-SMOKE-10). 4.8b should have audited all handlers; this step ships the audit + fix + the lesson: when fixing a bug class at one site, audit sibling sites. Now BOTH `_handle_open` AND `_handle_close` use the locked `f"v3:{request_id}"[:29]` format. Single prefix across handlers gives one grep target. §6.d documents that `mt5.order_check` does NOT validate comment length — Phase 5 backlog. |

*(Append entries below.)*
