# cTrader Open API — Execution Event Behavior

> Tài liệu thực tế đúc kết từ smoke test trên FTMO live (cTrader free trial account 47247733).
> Update mỗi khi phát hiện behavior mới qua smoke test hoặc protobuf inspection.
> NGUỒN: empirical observation + protobuf DESCRIPTOR inspection. KHÔNG phải official docs.
> Mọi step tương lai touch cTrader API → check + update file này khi phát hiện behavior mới.

## 1. Object model

### 1.1 ID semantics — orderId vs positionId
- `order.orderId`: lifecycle ngắn (submit → fill hoặc cancel). Dùng cho cancel pending order.
- `position.positionId`: lifecycle dài (first fill → close). Dùng cho `close_position` + `modify_sl_tp`.
- Một position có thể tích lũy nhiều orderId qua các deals (partial fills, amends).
- D-061 + D-065: `broker_order_id` ở resp_stream là `position.positionId` cho market fills; là `order.orderId` cho pending limit/stop (cho đến khi pending fill — step 3.5 swap).

### 1.2 Deal vs Position vs Order
- `Order` = request to broker. Có `orderId`, `orderType` (MARKET/LIMIT/STOP), `requestedVolume`.
- `Position` = open trade after fill. Có `positionId`, `tradeData`, current SL/TP.
- `Deal` = execution leg. Có `dealId`, `executionPrice`, `executionTimestamp`, `commission`. Một Deal có thể là OPEN_POSITION (tăng vol) hoặc CLOSE_POSITION (giảm vol).
- `ProtoOAExecutionEvent` carries all three sub-messages khi relevant. `closePositionDetail` chỉ có trên close-side fills.

### 1.3 executionType enum
| Value | Name | Meaning |
|---|---|---|
| 2 | ORDER_ACCEPTED | Intermediate cho market open / close orders; terminal cho limit/stop pending |
| 3 | ORDER_FILLED | Terminal cho market open / close fill; unsolicited cho limit/stop fill |
| 4 | ORDER_REPLACED | Modify SL/TP success (single-event, no ACCEPTED intermediate) |
| 7 | ORDER_REJECTED | Order/modify rejected by broker |

Verified via DESCRIPTOR inspection of `OpenApiModelMessages_pb2.ProtoOAExecutionType` enum.

## 2. Order placement flows

### 2.1 Market order placement — successful flow (step 3.4 + 3.4b verified)
```
[client] submit ProtoOANewOrderReq (orderType=MARKET, NO stopLoss/takeProfit per D-058)
   ↓ ~50-200ms
[broker] ProtoOAExecutionEvent #1
   executionType = ORDER_ACCEPTED (2)
   order.orderId populated
   position NOT populated yet
   deal NOT populated yet
   ↓ ~50-200ms
[broker] ProtoOAExecutionEvent #2 (same clientMsgId as #1)
   executionType = ORDER_FILLED (3)
   order.orderId (same as #1)
   position.positionId = NEW (this is broker_order_id for future ops — D-061)
   deal.executionPrice (DOUBLE, NOT scaled — D-064)
   deal.executionTimestamp (int64 epoch ms)
   deal.commission (int64 raw, scaled at consumer per D-053)
```

**Bridge handling** (`place_market_order`, step 3.4b): pre-register a Future in `_pending_executions[clientMsgId]`. Send request. Inspect first response: if ORDER_FILLED → fast path return; if ORDER_ACCEPTED → await Future (resolved by `_on_message` when ORDER_FILLED arrives). REJECTED / unexpected → fall through to error parser.

### 2.2 Market order with absolute SL/TP — REJECTED
```
[client] submit ProtoOANewOrderReq (orderType=MARKET, stopLoss=X, takeProfit=Y)
   ↓
[broker] ProtoOAErrorRes
   description = "SL/TP in absolute values are allowed only for order types: [LIMIT, STOP, STOP_LIMIT]"
```

→ Workaround per D-058: submit market WITHOUT SL/TP, then `ProtoOAAmendPositionSLTPReq` after fill (with 100ms settling delay per D-063).

### 2.3 Limit/Stop order placement — successful flow
```
[client] submit ProtoOANewOrderReq (orderType=LIMIT or STOP, limitPrice/stopPrice, stopLoss, takeProfit)
   ↓ ~50-200ms
[broker] ProtoOAExecutionEvent (single event)
   executionType = ORDER_ACCEPTED (2) — TERMINAL for pending
   order.orderId populated (this is broker_order_id while pending)
   position NOT populated yet
   deal NOT populated yet
```

Later when price hits limit/stop:
```
[broker] unsolicited ProtoOAExecutionEvent (NO clientMsgId — push only)
   executionType = ORDER_FILLED (3)
   position.positionId = NEW
   deal.* populated
```

→ Server's `event_handler` (step 3.5) sẽ catch unsolicited fill này, swap `broker_order_id` trong order:{id} hash từ orderId sang positionId.

### 2.4 Order placement rejection cases
- Insufficient margin: `ProtoOAOrderErrorEvent(errorCode=NOT_ENOUGH_MONEY)`
- Market closed: `ProtoOAOrderErrorEvent(errorCode=MARKET_CLOSED)` hoặc `ProtoOAExecutionEvent(executionType=ORDER_REJECTED, errorCode=MARKET_CLOSED)`
- Invalid volume: `ProtoOAOrderErrorEvent(errorCode=TRADING_BAD_VOLUME)`
- Invalid SL distance: `ProtoOAOrderErrorEvent(errorCode=INVALID_STOPS_LEVEL)`
- Limit BUY entry >= ask: `ProtoOAOrderErrorEvent(errorCode=...?)` — [chưa observe trong smoke]
- Stop BUY entry <= ask: `ProtoOAOrderErrorEvent(errorCode=...?)` — [chưa observe trong smoke]

## 3. Close flows

### 3.1 Close qua API (server initiated) — successful flow (step 3.4c verified)
```
[client] submit ProtoOAClosePositionReq (positionId, volume)
   ↓ ~50-200ms
[broker] ProtoOAExecutionEvent #1
   executionType = ORDER_ACCEPTED (2)  ← intermediate, no deal data yet
   ↓ ~50-200ms
[broker] ProtoOAExecutionEvent #2 (same clientMsgId as #1)
   executionType = ORDER_FILLED (3)  ← terminal
   deal.executionPrice = close price (DOUBLE — D-064 applies here too)
   deal.executionTimestamp = close time (epoch ms)
   deal.commission = close-side commission (int64 raw, D-053)
   deal.closePositionDetail
     .grossProfit (int64 raw, scaled at consumer per D-053)  ← realized_pnl
     .balance (new account balance after close)
     .swap, .commission (extra fees; separate from deal.commission)
     .quoteToDepositConversionRate (for non-USD quote currencies)
     .closedVolume (volume that actually closed)
     .moneyDigits (scale exponent for grossProfit/balance/commission)
     .balanceVersion, .pnlConversionFee, .entryPrice
```

**Bridge handling** (step 3.4c): same 2-event pattern as `place_market_order`. `_pending_executions` side channel + wait for FILLED via `_on_message` dispatch.

Step 3.4 sub-test 5 smoke evidence của bug trước fix: resp_stream `error_msg='executionType=2'` mặc dù cTrader UI shows position closed. Cause: bridge consumed ACCEPTED, dropped FILLED.

### 3.2 User close manual (cTrader UI / broker forced close) — bridge handling step 3.5a
```
[broker] ProtoOAExecutionEvent (UNSOLICITED — no clientMsgId)
  executionType = ORDER_FILLED (3)
  order.orderType = MARKET (=1)                    ← KEY INDICATOR
  order.closingOrder = true                        ← KEY INDICATOR
  position.positionId = <positionId being closed>
  deal.executionPrice = close price (DOUBLE — D-064)
  deal.executionTimestamp = close time (epoch ms)
  deal.closePositionDetail
    .grossProfit  (raw int, D-053) ← realized P&L (sign dep. on direction)
    .commission   (raw int)         ← close-side fee (canonical for close events)
    .swap         (raw int)         ← lifetime swap charges
    .balance      (raw int)         ← account balance AFTER this close settles
    .moneyDigits  (uint32)          ← scale exponent for grossProfit/commission/swap/balance
    .closedVolume (int64)           ← actual closed volume (D-057)
```

**Bridge handling** (step 3.5a): `_on_message` sees `clientMsgId == ""`
→ unsolicited path → `event_publisher.build_event_payload` →
XADD `event_stream:ftmo:{account_id}`:
- `event_type = "position_closed"`
- `broker_order_id = position.positionId`
- `close_reason = "manual"` (via §3.6 structured inference)
- five extended fields: `commission`, `swap`, `balance_after_close`,
  `money_digits`, `closed_volume`

Note: `"manual"` covers BOTH user manual close (clicking Close on
cTrader UI) AND broker-forced closes (margin call, stopout). cTrader
uses `orderType=MARKET` internally for both. The `order.isStopOut`
bool field exists on `ProtoOAOrder` (num=22) and could refine
"manual" → "stopout" in a future step — out of scope for 3.5a.

### 3.3 Auto-close SL hit — bridge handling step 3.5a
```
[broker] ProtoOAExecutionEvent (UNSOLICITED — no clientMsgId)
  executionType = ORDER_FILLED (3)
  order.orderType = STOP_LOSS_TAKE_PROFIT (=4)     ← KEY INDICATOR
  order.closingOrder = true                        ← KEY INDICATOR
  order.executionPrice ≈ position.stopLoss
    (informational — does NOT drive classification)
  position.positionId = <closing position>
  deal.executionPrice = close price (may slip several points off SL)
  deal.closePositionDetail.grossProfit < 0         ← SIGN classifies as "sl"
  (... other closePositionDetail fields as §3.2)
```

Bridge → event_stream → `event_type="position_closed"`,
`close_reason="sl"`.

### 3.4 Auto-close TP hit — bridge handling step 3.5a
```
[broker] ProtoOAExecutionEvent (UNSOLICITED — no clientMsgId)
  executionType = ORDER_FILLED (3)
  order.orderType = STOP_LOSS_TAKE_PROFIT (=4)
  order.closingOrder = true
  order.executionPrice ≈ position.takeProfit (with slippage)
  deal.closePositionDetail.grossProfit > 0         ← SIGN classifies as "tp"
```

Bridge → event_stream → `event_type="position_closed"`,
`close_reason="tp"`.

### 3.5 Margin call / stop-out
[chưa observe — placeholder for Phase 4+ smoke]
Expected wire shape: combination of
`ProtoOAMarginCallTriggerEvent` (separate unsolicited event published
to the account auth'd connection) PLUS one or more
`ProtoOAExecutionEvent(ORDER_FILLED)` for each position closed by the
margin engine. Each close event likely uses
`order.orderType=MARKET` + `order.closingOrder=true` (same shape as
§3.2) WITH `order.isStopOut=true` set as the explicit signal.

The bridge currently classifies these as `close_reason="manual"`
because the §3.6 logic does not yet inspect `isStopOut`. Refinement
to `"stopout"` is reserved for a future step once smoke confirms the
field is reliably set by the broker.

## 3.6 Close-reason inference (step 3.5a — structured method)

cTrader does NOT provide a dedicated `closeReason` enum field —
verified by DESCRIPTOR inspection of `ProtoOADeal` and
`ProtoOAClosePositionDetail` in step 3.5 and re-confirmed in 3.5a.
Inference relies on **structured order metadata** instead:

| order.orderType | order.closingOrder | closePositionDetail.grossProfit | → close_reason |
|---|---|---|---|
| MARKET (1) | true | (any) | manual |
| STOP_LOSS_TAKE_PROFIT (4) | true | > 0 | tp |
| STOP_LOSS_TAKE_PROFIT (4) | true | < 0 | sl |
| STOP_LOSS_TAKE_PROFIT (4) | true | == 0 | unknown |
| any | false | n/a | unknown |
| LIMIT / STOP / STOP_LIMIT / MARKET_RANGE | true | n/a | unknown |
| order field absent | n/a | n/a | unknown |

**Why `grossProfit` sign instead of price comparison:**
- Avoids per-symbol pip-size tolerance issues (5-digit forex vs
  3-digit JPY vs non-forex). The previous heuristic used a hardcoded
  one-pip absolute tolerance that did not generalize.
- Reliable across all symbols including non-forex (crypto, indices,
  commodities).
- Single source of truth from cTrader's own bookkeeping — the same
  value that goes into the account balance line.

**Why `orderType + closingOrder` instead of `clientMsgId == ""`:**
- The empty-clientMsgId test only proves the event is unsolicited
  (not initiated by our bridge); it doesn't classify the close
  reason itself.
- `orderType=STOP_LOSS_TAKE_PROFIT` is cTrader's explicit synthetic
  order that the broker generates when an SL/TP price level is
  touched. `orderType=MARKET` is what cTrader uses for every other
  close path (UI close, margin call, account closure).

**CEO-provided canonical sample** (TP hit on a BUY position, BTC-style symbol):
```
order.orderType      = STOP_LOSS_TAKE_PROFIT
order.closingOrder   = true
order.stopPrice      = 90520.96    (SL price; informational)
order.limitPrice     = 90638.71    (TP price; informational)
order.executionPrice = 90640.27    (1.56-point slippage vs limitPrice)
deal.closePositionDetail.grossProfit = 98     → POSITIVE → "tp"
deal.closePositionDetail.commission  = -766
deal.closePositionDetail.balance     = 942589
deal.closePositionDetail.moneyDigits = 2
deal.closePositionDetail.closedVolume = 13
deal.closePositionDetail.swap        = 0
```
The 1.56-point gap between `executionPrice` and `limitPrice` is
broker slippage at the trigger moment. The old price-tolerance
heuristic would have misclassified this as `"manual"` because the
absolute difference (1.56) exceeded the configured tolerance.
The new `grossProfit`-sign method is robust.

**Limitations:**
- `"stopout"` reason (margin call / forced close) currently lumped
  into `"manual"`. Future hardening can split via `order.isStopOut`
  bool (verified to exist via DESCRIPTOR inspection in step 3.5a;
  not wired yet).
- `"unknown"` reserved for: (a) unexpected orderType,
  (b) `grossProfit=0` edge case (SL/TP exactly at entry),
  (c) `closingOrder=false`, (d) missing required protobuf fields.

Implementation: `ftmo_client/event_publisher.py::_infer_close_reason`.
Matrix test: `test_close_reason_matrix` parametrizes all 10 rows
above + the no-order-field case.

## 3.7 ORDER_CANCELLED noise — cTrader internal SL/TP cleanup (D-080)

cTrader maintains an internal **STOP_LOSS_TAKE_PROFIT** synthetic
order alongside any position that carries SL or TP. When that
position closes — for ANY reason, including the SL/TP trigger
itself, manual UI close, or our API close — the broker
cleans up the synthetic order and emits an `ORDER_CANCELLED`
execution event with the synthetic order's internal `orderId`.

The internal `orderId` has NO matching row in our Redis state — we
never opened it. Step 3.5 bridge translates every `ORDER_CANCELLED`
event into an `order_cancelled` event_stream entry; step 3.7
server `event_handler._handle_order_cancelled` silently drops any
event whose `broker_order_id` has no matching
`p_broker_order_id_to_order:{id}` side-index entry (logged at
DEBUG, not WARNING — these are routine).

Distinguishing legitimate cancellations (operator cancels their
own pending LIMIT/STOP order from the cTrader UI) from D-080
noise: a legitimate cancellation's `broker_order_id` IS in our
side-index (the pending order was opened by us). The handler
updates the order row to `status=cancelled` + drops the index in
that case.

Test: `test_order_cancelled_unknown_broker_order_id_silently_ignored`
pins the D-080 silent-drop contract.
Test: `test_order_cancelled_matching_pending_order_marks_cancelled`
pins the legitimate-cancel path.

## 3.8 Solicited close — event_stream publish on close-side FILL (step 4.8c)

Up to step 4.8b, **solicited** closes (operator clicks the UI Close
button → server `POST /api/orders/{id}/close` → close cmd via
`cmd_stream:ftmo:{account_id}`) only published to
`resp_stream:ftmo:{account_id}` (`action=close status=success`). The
`position_closed` event_stream entry was emitted ONLY by:

- **Unsolicited** closes (operator UI close, SL/TP hit, stop-out per
  §3.2-3.6) via `_publish_unsolicited_event` in `_on_message`.
- **Reconcile** reconstruction on offline-window closes
  (§3.7 / §12 — `reconstructed=true` payload).

This was the wrong design for Phase 4. The cascade-close orchestrator
(`HedgeService.cascade_close_other_leg`, step 4.8) is gated on
`event_type=position_closed` from the FTMO leg via
`event_handler._handle_position_closed`. Without the event for
solicited closes, **cascade Path A (server-initiated via API
endpoint) was completely broken** — the secondary Exness leg stayed
open as an orphan after the operator clicked Close. Discovered in
the Phase 4 smoke 2026-05-16 (D-SMOKE-9); full diagnostic trail in
`verify-ftmo-close-event-gap.md`.

Step 4.8c extends the bridge: `ctrader_bridge.close_position`
publishes `position_closed` to `event_stream:ftmo:{account_id}`
AFTER parsing the `ORDER_FILLED` event with `closePositionDetail`.
Applies to **both** fast path (cTrader returns `ORDER_FILLED`
synchronously via `_send_and_wait`) and slow path
(`ORDER_ACCEPTED` first, then `ORDER_FILLED` arrives via
`_on_message` resolving the `_pending_executions` Future). The
helper `_publish_position_closed_from_fill` is the single source of
truth — `_on_message` itself does NOT publish for solicited fills
to avoid double-publishing the slow-path branch.

The `close_reason` value is computed by the existing
`_infer_close_reason` (§3.6) — `sl|tp|manual|unknown` — and lands on
the event payload exactly as it does for unsolicited closes. The
server's cascade orchestrator does NOT depend on `close_reason`
matching `server_initiated`; it derives the cascade `trigger_path`
from the `close_trigger_initiated="A"` flag the API endpoint writes
on the order row (`OrderService.close_order`, step 4.8) — a Path A
marker independent of FTMO's `close_reason` vocabulary.

**Idempotency**: the server's `_handle_position_closed` flows into
`cascade_close_other_leg` which no-ops on terminal order status (step
4.8 `cascade_close.no_op_terminal`). A duplicate publish from
`reconcile_state` replay during the same close window is harmless.

**Guards** on `_publish_position_closed_from_fill`:

- Returns early when the event has no `deal.closePositionDetail`
  (defensive — the caller is supposed to only invoke for close fills,
  but the check keeps the helper safe to call unconditionally).
- Returns early when `self._redis is None` (test isolation).
- Returns early when `build_event_payload` returns `None` (an
  executionType the payload builder doesn't classify — logged at
  WARNING so operators notice).
- XADD failures are logged and swallowed.

Tests:

- `test_close_position_fast_path_publishes_position_closed` — pins
  the fast-path publish.
- `test_close_position_slow_path_publishes_position_closed` — pins
  the slow-path publish (Future-resolution branch).
- `test_close_position_no_publish_when_close_position_detail_missing`
  — defensive guard.
- `test_place_market_order_open_fill_does_not_publish` — regression
  on the Phase 3 invariant that open-side fills NEVER reach
  `event_stream` (open state drives via the `resp_stream` `open` ACK).
- `test_on_message_resolves_pending_future_when_client_msg_id_matches`
  — pins the single-source-of-truth contract: `_on_message` itself
  does NOT publish for solicited fills.

## 4. SL/TP modification flows

### 4.1 Modify qua API (server initiated) — successful flow (step 3.4 verified, step 3.4c documented)
```
[client] submit ProtoOAAmendPositionSLTPReq (positionId, stopLoss, takeProfit)
   ↓ ~50-100ms
[broker] ProtoOAExecutionEvent (SINGLE event, NO intermediate ACCEPTED)
   executionType = ORDER_REPLACED (4)  ← terminal
   position.* with updated stopLoss / takeProfit
   no deal data (no fill happened — modify doesn't create a deal)
```

**Bridge handling**: simple `_send_and_wait` — NO 2-event pattern, NO `_pending_executions` side channel. Verified empirically in step 3.4 smoke sub-test 4 (modify succeeded, returned `new_sl`/`new_tp` correctly with the current single-event handling).

Test contracts pinning this behavior (step 3.4c):
- `test_modify_sl_tp_does_not_use_pending_executions_channel` — assertion inside the stub that `_pending_executions` does NOT contain the modify's clientMsgId during the call.
- `test_modify_sl_tp_single_order_replaced_event_is_terminal` — exactly one `_send_and_wait` call, one event, bridge returns.

### 4.2 Failure cases
- `POSITION_NOT_FOUND`: position chưa settle hậu fill, hoặc đã close. → 100ms settling delay before amend (D-063) khi composite `place_market_order_with_sltp` chạy.
- `"New TP for BUY position should be >= current BID price"` → SL/TP direction violation. cTrader returns this as `ProtoOAErrorRes` hoặc `ProtoOAOrderErrorEvent` với errorCode tương ứng.
- **Atomicity quirk**: cTrader treats SL+TP as a single atomic amend — nếu 1 field invalid, reject toàn bộ (cả SL lẫn TP đều không set), KHÔNG partial. Documented; no bridge-side workaround (caller must send a valid pair).

### 4.3 User sửa SL/TP tay trên cTrader Desktop/Web — bridge handling step 3.5
```
[broker] ProtoOAExecutionEvent (UNSOLICITED — no clientMsgId)
  executionType = ORDER_REPLACED (4)
  position.positionId = <positionId being amended>
  position.stopLoss, position.takeProfit (the NEW values)
  (no deal data — modify doesn't create a deal)
```

**Bridge handling** (step 3.5): `_on_message` → unsolicited path →
XADD `event_stream:ftmo:{account_id}`:
- `event_type = "position_modified"`
- `broker_order_id = position.positionId`
- `new_sl = position.stopLoss` (empty string if cleared)
- `new_tp = position.takeProfit` (empty string if cleared)

Server's event_handler (step 3.7) uses this to update the
`order:{order_id}` hash + propagate to hedge leg via cmd_stream
modify on the Exness account.

[Empirical confirmation pending step 3.5 smoke run.]

## 5. Timing & latency (empirical from step 3.4 smoke)

| Event | Observed latency | Note |
|---|---|---|
| ORDER_ACCEPTED arrival | ~50-200ms | Network RTT + cTrader internal validation |
| ORDER_FILLED arrival (post-ACCEPTED, market) | ~50-200ms | After ACCEPTED |
| ORDER_FILLED arrival (post-ACCEPTED, close) | ~50-200ms | Same shape as market |
| Position amend-able after FILLED | ~50-100ms | D-063 → bridge hardcodes 100ms settling delay |
| Modify response (ORDER_REPLACED) | ~50-100ms | Single event, no intermediate |
| Unsolicited event broadcast | Real-time, <100ms | cTrader push channel |

## 6. clientMsgId correlation

- Bridge sets `clientMsgId` = `request_id` (uuid4 hex) on every send.
- cTrader echoes `clientMsgId` in solicited responses → bridge correlates via the cTrader library's Deferred + the `_pending_executions` side channel (for the 2nd event of any 2-event sequence).
- Suffix `_amend` used for amend's clientMsgId trong composite `place_market_order_with_sltp` để tách dedup (cTrader-side dedup không conflate fill với amend).
- Unsolicited events (manual close, SL/TP hit, pending fill) have NO `clientMsgId` → bridge routes via positionId/orderId lookup (step 3.5 wires this).

## 7. cTrader API quirks summary

| Quirk | Discovered step | Workaround / Reference |
|---|---|---|
| OAuth callback doesn't echo `state` | 2.1a | D-031: skip state CSRF |
| Wire prices scaled int ×10^5 (tick/trendbar only) | 2.2a | D-032: divide at boundary |
| Market order rejects absolute SL/TP | 3.4 smoke | D-058: post-fill amend with 100ms delay (D-063) |
| Position not amend-able immediately after fill | 3.4a smoke | D-063: 100ms settling delay |
| broker_order_id semantics: market=positionId, pending=orderId | 3.4 smoke + 3.4b inspection | D-061; step 3.5 swaps on pending fill |
| Market open returns 2-event sequence (ACCEPTED → FILLED) | 3.4 + 3.4b | D-062 (market); bridge waits for FILLED via `_pending_executions` |
| Close position returns 2-event sequence (ACCEPTED → FILLED) | 3.4 smoke + 3.4c | Same `_pending_executions` pattern as market open |
| Modify SL/TP returns single ORDER_REPLACED event (no ACCEPTED intermediate) | 3.4 smoke + 3.4c | Single `_send_and_wait` correct; tests pin the contract |
| `deal.executionPrice` is DOUBLE protobuf field, NOT int64 | 3.4b inspection | D-064: do NOT divide by 10^5 |
| Authoritative ID is `event.position.positionId`, not `event.deal.positionId` | 3.4b inspection | D-065 |
| sync_symbols sequential too slow | 2.7b | D-039: batch ProtoOASymbolByIdReq |
| SL+TP amend atomic (partial reject = full reject) | 3.4 smoke | Document only; caller must send a valid pair |
| cTrader library's `Client._received` pops Deferred on first match | 3.4b code dive | 2nd event lands in `_messageReceivedCallback` → use `_pending_executions` side channel |

## 8. Protobuf field paths (verified via DESCRIPTOR inspection — step 3.4c)

Inspected against the installed `ctrader_open_api` package via
`python -c "from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAClosePositionDetail; print([f.name + ':type=' + str(f.type) for f in ProtoOAClosePositionDetail.DESCRIPTOR.fields])"`.

```
ProtoOAExecutionEvent (OpenApiMessages_pb2):
  payloadType (enum)
  ctidTraderAccountId (int64)
  executionType (enum) — required
  position (message → ProtoOAPosition)  — optional, populated on FILLED
  order (message → ProtoOAOrder)        — populated on ACCEPTED + FILLED
  deal (message → ProtoOADeal)          — populated on FILLED only
  errorCode (string)                    — populated on REJECTED + some failures
  isServerEvent (bool)

ProtoOAPosition (OpenApiModelMessages_pb2):
  positionId (int64) ← AUTHORITATIVE open position ID (D-065)
  tradeData (message, required)
  positionStatus (enum, required)
  swap (int64, required)
  price (double, optional)
  stopLoss (double, optional)
  takeProfit (double, optional)
  utcLastUpdateTimestamp (int64)
  commission (int64)
  marginRate (double)
  ... (mirroringCommission, guaranteedStopLoss, usedMargin, etc.)
  moneyDigits (uint32) — scale exponent for swap/commission

ProtoOAOrder (step 3.5a — full field list verified):
  orderId (int64, required, num=1)        ← request-side ID
  tradeData (message, required, num=2)
  orderType (enum ProtoOAOrderType, required, num=3) ← step 3.5a close inference
  orderStatus (enum, required, num=4)
  expirationTimestamp (int64, optional, num=6)
  executionPrice (DOUBLE, optional, num=7)
  executedVolume (int64, optional, num=8)
  utcLastUpdateTimestamp (int64, optional, num=9)
  baseSlippagePrice (DOUBLE, optional, num=10)
  slippageInPoints (int64, optional, num=11)
  closingOrder (BOOL, optional, num=12)   ← step 3.5a close inference
  limitPrice (DOUBLE, optional, num=13)   ← informational (TP price echo)
  stopPrice (DOUBLE, optional, num=14)    ← informational (SL price echo)
  stopLoss (DOUBLE, optional, num=15)
  takeProfit (DOUBLE, optional, num=16)
  clientOrderId (string, optional, num=17)
  timeInForce (enum, optional, num=18)
  positionId (int64, optional, num=19)
  relativeStopLoss (int64, optional, num=20)
  relativeTakeProfit (int64, optional, num=21)
  isStopOut (BOOL, optional, num=22)       ← reserved for future "stopout" close_reason
  trailingStopLoss (BOOL, optional, num=23)
  stopTriggerMethod (enum, optional, num=24)

ProtoOAOrderType (step 3.5a — enum values verified):
  1 = MARKET                  ← step 3.5a: closingOrder=true → close_reason "manual"
  2 = LIMIT
  3 = STOP
  4 = STOP_LOSS_TAKE_PROFIT   ← step 3.5a: closingOrder=true →
                                close_reason "tp" or "sl" by grossProfit sign
  5 = MARKET_RANGE
  6 = STOP_LIMIT

  Critical: STOP_LOSS_TAKE_PROFIT = 4 (NOT 6 as some external docs suggest).
  6 is STOP_LIMIT.

ProtoOADeal:
  dealId (int64, required, num=1)
  orderId (int64, required, num=2)
  positionId (int64, required, num=3) ← redundant w/ event.position.positionId
  volume (int64, required, num=4)
  filledVolume (int64, required, num=5)
  symbolId (int64, required, num=6)
  createTimestamp (int64, required, num=7)
  executionTimestamp (int64, required, num=8)
  utcLastUpdateTimestamp (int64, optional, num=9)
  executionPrice (DOUBLE, optional, num=10) ← D-064: NOT scaled
  tradeSide (enum, required, num=11)
  dealStatus (enum, required, num=12)
  marginRate (double, optional, num=13)
  commission (int64, optional, num=14) ← raw, D-053
  baseToUsdConversionRate (double, optional, num=15)
  closePositionDetail (message → ProtoOAClosePositionDetail, optional, num=16)
                                                ← only on close-side fill
  moneyDigits (uint32, optional, num=17)

ProtoOAClosePositionDetail (step 3.4c — full set verified):
  entryPrice (double, required, num=1)
  grossProfit (int64, required, num=2) ← REALIZED_PNL (raw, D-053)
  swap (int64, required, num=3)
  commission (int64, required, num=4) — separate from deal.commission
  balance (int64, required, num=5) — new balance after close
  quoteToDepositConversionRate (double, optional, num=6)
  closedVolume (int64, optional, num=7) — vol that actually closed
  balanceVersion (int64, optional, num=8)
  moneyDigits (uint32, optional, num=9) — scale for grossProfit/swap/commission/balance
  pnlConversionFee (int64, optional, num=10)
```

**Field-type legend** (protobuf descriptor type ints):
- type=1: DOUBLE
- type=3: INT64
- type=8: BOOL
- type=9: STRING
- type=11: MESSAGE (sub-message)
- type=13: UINT32
- type=14: ENUM
- label=1: OPTIONAL, label=2: REQUIRED

**Bridge's parser choices** (step 3.4c):
- `_parse_filled_close.close_price` ← `event.deal.executionPrice` (NOT scaled).
- `_parse_filled_close.close_time` ← `event.deal.executionTimestamp` (epoch ms).
- `_parse_filled_close.realized_pnl` ← `event.deal.closePositionDetail.grossProfit` (raw int, consumer scales by moneyDigits). Defensive `HasField` check so a malformed event yields empty string instead of crashing.
- We do NOT extract `closePositionDetail.commission` separately — the close-side `deal.commission` carries the same value at the deal level (consistent with open path). Operator-facing net P&L (gross − commission − swap) is composed at the consumer (server or web layer), not in the bridge.

## 10. Account info polling (step 3.5)

cTrader does NOT expose `equity` / `margin` / `free_margin` directly
on `ProtoOATrader`. Verified via DESCRIPTOR inspection:

```
ProtoOATrader (full field list):
  ctidTraderAccountId (int64, required)
  balance (int64, required) ← raw, scaled by moneyDigits
  balanceVersion (int64, optional)
  managerBonus, ibBonus, nonWithdrawableBonus (int64, optional)
  accessRights (enum, optional)
  depositAssetId (int64, required) ← FK to asset list; "USD" for FTMO
  swapFree, leverageInCents, totalMarginCalculationType
  maxLeverage, frenchRisk, traderLogin, accountType
  brokerName (string), registrationTimestamp (int64)
  isLimitedRisk (bool), limitedRiskMarginCalculationStrategy (enum)
  moneyDigits (uint32, optional) ← scale exponent for balance
```

`equity` / `margin` are derived at the consumer:
- **balance** ← `ProtoOATraderReq` / `ProtoOATraderRes.trader.balance` (raw int)
- **margin** ← sum of `ProtoOAPosition.usedMargin` over the
  `ProtoOAReconcileReq` snapshot's `position` array (raw int)
- **free_margin** ← `balance - margin`
- **equity** ← step 3.5 limitation: set equal to balance. True equity
  requires `ProtoOAGetPositionUnrealizedPnLReq` (a 3rd round-trip per
  poll) or recomputing per-position P&L from spot ticks. Future step
  can wire in true equity if the operator needs intra-trade margin
  tracking.
- **currency** ← hard-coded `"USD"` (FTMO funded accounts are
  USD-denominated). Future step can swap in `ProtoOAAssetListReq` +
  asset lookup if non-USD products are ever supported.
- **money_digits** ← `trader.moneyDigits`; default 2 if the broker
  omits the optional field.

**Polling cadence**: 30s, configurable via
`ACCOUNT_INFO_INTERVAL_SECONDS` in `ftmo_client.account_info`. HSETs
`account:ftmo:{account_id}` with the seven fields above plus
`updated_at` (epoch ms). Consumer reads via `HGETALL`.

**Failure mode**: a single poll error (cTrader timeout, transport
error, Redis flap) is logged and the loop continues to the next
interval — same shape as `heartbeat_loop`.

## 11. Reconciliation on client restart (step 3.5b)

When the FTMO client reconnects after an offline window, broker state
may have advanced (SL/TP hits, manual closes, pending fills, margin
calls) without us seeing the corresponding execution events. The
bridge queries cTrader for current truth once, before any of the
heartbeat / command / account_info loops start, and publishes a
snapshot for the server's event_handler to diff against Redis.

### 11.1 ReconcileReq → reconcile_snapshot event

```
[client] ProtoOAReconcileReq(ctidTraderAccountId=X)
   ↓
[broker] ProtoOAReconcileRes
   position[]: list of currently OPEN positions
   order[]:    list of currently PENDING orders (limit/stop awaiting fill)

[bridge] event_stream:ftmo:{account_id} XADD:
   event_type        = "reconcile_snapshot"
   positions         = JSON array (see §11.3)
   pending_orders    = JSON array (see §11.4)
   position_count    = "N"
   order_count       = "M"
   ts_published      = "<epoch ms>"
```

### 11.2 Retry + failure semantics

- **3 attempts** with exponential backoff (sleep 1s after attempt 1
  failure, 2s after attempt 2). Default `_send_and_wait` timeout per
  attempt: 10s.
- **All 3 fail** → log error, skip publish, continue startup. The
  server's event_handler tolerates a missing snapshot (treats Redis
  state as source of truth in that case, accepting the drift risk).
- **Unexpected response type** (e.g. `ProtoOAErrorRes` from a stale
  auth) → no publish, log warning. The bridge does NOT retry on
  unexpected-type because the cTrader library has already burned the
  Deferred; a retry would just re-fail.
- **`self._redis is None`** (test fixtures) → warn + skip publish;
  the snapshot is still constructed in memory but discarded.
- **`main.amain` wraps the call in `try/except`** so any unexpected
  exception in `reconcile_state` does NOT block client startup; the
  loops still spin up.

### 11.3 Positions list schema

Each element of the `positions` JSON array:

| Field | Type | Source | Notes |
|---|---|---|---|
| `position_id` | str | `p.positionId` | int64 cast to string |
| `symbol_id` | str | `p.tradeData.symbolId` | int64 |
| `side` | str | `p.tradeData.tradeSide` | `"buy"` / `"sell"` |
| `volume` | str | `p.tradeData.volume` | cTrader wire units (consumer scales by lot_size) |
| `entry_price` | str | `p.price` if `HasField` else `""` | DOUBLE |
| `stop_loss` | str | `p.stopLoss` if `HasField` else `""` | DOUBLE |
| `take_profit` | str | `p.takeProfit` if `HasField` else `""` | DOUBLE |
| `open_timestamp` | str | `p.tradeData.openTimestamp` if set | epoch ms |
| `used_margin` | str | `p.usedMargin` if `HasField` else `""` | raw int (D-053) |

### 11.4 Pending orders list schema

Each element of the `pending_orders` JSON array:

| Field | Type | Source |
|---|---|---|
| `order_id` | str | `o.orderId` (request-side ID) |
| `order_type` | str | `_order_type_name(o.orderType)` — one of `market` / `limit` / `stop` / `stop_limit` / `stop_loss_take_profit` / `market_range` / `unknown` |
| `symbol_id` | str | `o.tradeData.symbolId` |
| `side` | str | `"buy"` / `"sell"` |
| `volume` | str | `o.tradeData.volume` |
| `limit_price` | str | `o.limitPrice` if `HasField` else `""` |
| `stop_price` | str | `o.stopPrice` if `HasField` else `""` |
| `stop_loss` | str | `o.stopLoss` if `HasField` else `""` |
| `take_profit` | str | `o.takeProfit` if `HasField` else `""` |
| `open_timestamp` | str | `o.tradeData.openTimestamp` if set |

## 12. Deal history query — fetch_close_history (step 3.5b)

After receiving a `reconcile_snapshot`, the server's event_handler
(step 3.7) diffs against Redis state. For each position that was OPEN
in Redis but is MISSING from the snapshot, the server publishes a
`fetch_close_history` command to the FTMO client; the client queries
cTrader's deal history for that position and publishes a
reconstructed `position_closed` event so the server can drive the
same close-flow logic it uses for live unsolicited closes.

### 12.1 Command shape

```
cmd_stream:ftmo:{account_id} XADD:
  action            = "fetch_close_history"
  order_id          = "<server-side order id>"
  broker_order_id   = "<cTrader positionId>"
  symbol            = "<symbol name>" (optional; informational)
  request_id        = "<uuid>"
  created_at        = "<epoch ms>"
```

### 12.2 Bridge query

```
[client] ProtoOADealListByPositionIdReq(
            ctidTraderAccountId = X,
            positionId          = Y,
            fromTimestamp       = 0,
            toTimestamp         = now_ms,
         )
   ↓
[broker] ProtoOADealListByPositionIdRes
   deal[]: full deal history for position Y, oldest first

[bridge] iterate deals → pick first deal with closePositionDetail set
   build position_closed payload (same shape as §3.2 live close
   event) PLUS reconstructed=true field + close_reason="unknown"
   (deal history lacks the source order's orderType / closingOrder
   metadata that §3.6 needs for live classification)
```

**Critical DESCRIPTOR finding** (step 3.5b): both `fromTimestamp` and
`toTimestamp` are REQUIRED (label=2) on
`ProtoOADealListByPositionIdReq`, NOT optional. Bridge always sets
them — `fromTimestamp=0` (epoch start) and `toTimestamp=now_ms`
covers all of position's history.

### 12.3 Reconstructed event publish

Bridge publishes to `event_stream:ftmo:{account_id}` with
`event_type=position_closed` and the extra `reconstructed=true`
field. The server's event_handler treats reconstructed events
identically to live close events for state-update purposes, except:

- Audit logs mark the event as backfilled (provenance).
- WS broadcast distinguishes "Closed during offline window" toast
  from "Closed just now" toast.
- `close_reason` is `"unknown"` because the deal history doesn't
  include the source order's `orderType` / `closingOrder` metadata.
  The server consumer may re-infer from `grossProfit` sign +
  last-known SL/TP if higher fidelity is needed; the bridge stays
  agnostic.

The handler also publishes a regular ACK to
`resp_stream:ftmo:{account_id}` so the server's response_handler
closes out the `request_id` tracking on the
`fetch_close_history` command.

### 12.4 Error paths

| Bridge result | resp_stream `status` / `error_code` | event_stream |
|---|---|---|
| Payload returned (close deal found) | `success` | XADD reconstructed position_closed |
| `None` (no close deal — position still open or doesn't exist) | `error` / `not_found` | (no publish) |
| Bridge raised unexpectedly | `error` / `broker_error` | (no publish) |
| `broker_order_id` missing or non-numeric | `error` / `invalid_request` | (no publish) — bridge not called |

### 12.5 Partial-close caveat

Phase 3 doesn't support partial closes, so the bridge picks the FIRST
deal with `closePositionDetail` set and ignores any subsequent
close-side deals. If a future phase introduces partial-close support,
this method would need to aggregate `grossProfit` / `commission` /
`closedVolume` across all close deals rather than returning just the
first.

## 9. Update log

| Date | Step | Update |
|---|---|---|
| 2026-05-11 | 3.4c | Initial creation. Sections 1, 2.1-2.4, 3.1, 4.1-4.2, 5, 6, 7, 8 verified empirically through smoke step 3.4 + protobuf DESCRIPTOR inspection. Sections 3.2-3.5, 4.3 placeholder for step 3.5+ (unsolicited events). |
| 2026-05-11 | 3.5 | Sections 3.2 (manual close), 3.3 (SL hit), 3.4 (TP hit), 4.3 (manual modify) populated with bridge handling. Section 3.6 added documenting the close-reason inference heuristic (no protobuf close_reason field exists — verified by DESCRIPTOR inspection). Section 10 added for account info polling (balance via ProtoOATraderReq, margin via ProtoOAReconcileReq sum). Section 3.5 (margin call / stop-out) remains a placeholder pending margin-call event correlation in Phase 4+. |
| 2026-05-11 | 3.5a | Rewrite close_reason via structured order metadata (`order.orderType` + `order.closingOrder` + `closePositionDetail.grossProfit` sign); replace the 1-pip price-tolerance heuristic. Add extended `closePositionDetail` field publishing on `position_closed` payloads (`commission`, `swap`, `balance_after_close`, `money_digits`, `closed_volume`). §3.2-3.4 rewritten to show the structured signals. §3.6 replaced with the new mapping table + CEO sample. §8 ProtoOAOrder full field list (including `closingOrder`, `isStopOut`) + ProtoOAOrderType enum verified (STOP_LOSS_TAKE_PROFIT=4, NOT 6). |
| 2026-05-11 | 3.5b | §11 added — reconciliation flow on client restart (`ProtoOAReconcileReq` → `reconcile_snapshot` event_stream entry; 3-attempt exponential backoff retry; failure-tolerant). §12 added — `fetch_close_history` command flow that pulls deal history via `ProtoOADealListByPositionIdReq` and publishes reconstructed `position_closed` events with `reconstructed=true`. DESCRIPTOR inspection: `ProtoOADealListByPositionIdReq.fromTimestamp`/`toTimestamp` are REQUIRED (label=2), not optional — bridge sets `fromTimestamp=0` / `toTimestamp=now_ms` for full history. |
| 2026-05-11 | 3.7 | §3.7 added — D-080 ORDER_CANCELLED noise pattern: cTrader emits an internal-orderId `ORDER_CANCELLED` event whenever a position with SL/TP closes (cleanup of the synthetic STOP_LOSS_TAKE_PROFIT order). Server's event_handler silently drops these by comparing `broker_order_id` against the `p_broker_order_id_to_order:{id}` side-index — a legitimate operator-cancelled pending order has a hit; the D-080 noise events don't. Logged at DEBUG only. |
| 2026-05-16 | 4.8c | §3.8 added — solicited closes (`close_position` via cmd_stream) now ALSO publish `position_closed` to `event_stream:ftmo:{account_id}` via the new `_publish_position_closed_from_fill` helper. Called from both close_position branches (fast path + slow-path Future resolution); `_on_message` itself stays publish-free for solicited fills (single source of truth, no double-publish). Fixes Phase 4 cascade close Path A starvation discovered in the 2026-05-16 smoke (D-SMOKE-9). Open-side solicited fills (place_market_order) remain publish-free — Phase 3 invariant. Full diagnostic in `verify-ftmo-close-event-gap.md`. |
