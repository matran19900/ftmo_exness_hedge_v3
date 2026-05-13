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

| Retcode | Constant | Meaning | Server handling |
|---|---|---|---|
| 10009 | TRADE_RETCODE_DONE | Success | (no action — happy path) |
| 10018 | TRADE_RETCODE_MARKET_CLOSED | Market closed (off-hours) | Reject open; Alert if cascade close |
| 10027 | TRADE_RETCODE_POSITION_NOT_FOUND | Position closed externally before our cmd executed | D-4.0-7 force reconcile |
| 10030 | TRADE_RETCODE_UNSUPPORTED_FILLING | Filling mode (IOC/FOK) unsupported by broker | Retry with alternate filling mode |
| 10006 | TRADE_RETCODE_REJECT | Generic reject | Capture error_msg verbatim; surface to operator |
| 10004 | TRADE_RETCODE_REQUOTE | Price moved between request and execution | Retry once with fresh tick |
| 10014 | TRADE_RETCODE_INVALID_VOLUME | Volume outside symbol limits | Validation should have caught; ERROR log |
| 10015 | TRADE_RETCODE_INVALID_PRICE | Price stale | Retry once with fresh tick |
| 10016 | TRADE_RETCODE_INVALID_STOPS | SL/TP outside allowed band | Phase 5 only (Phase 4 no SL/TP on Exness) |
| 10019 | TRADE_RETCODE_NO_MONEY | Insufficient margin | Alert 2 path? — TBD step 4.2 |

*(Populate observed retcodes mid-phase per D-069. Update entries below with `step` column indicating where empirically verified.)*

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

---

## 6. MT5 quirks summary

Populate empirically mid-phase per D-069 pattern. Bullet list grows as smoke uncovers behavior.

| Quirk | Discovered step | Workaround / Reference |
|---|---|---|
| *(placeholder — populate as found)* | | |
| *(placeholder — populate as found)* | | |
| *(placeholder — populate as found)* | | |
| *(placeholder — populate as found)* | | |
| *(placeholder — populate as found)* | | |
| *(placeholder — populate as found)* | | |

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

*(Append entries below.)*
