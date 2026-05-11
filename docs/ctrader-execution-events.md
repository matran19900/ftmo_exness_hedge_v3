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

### 3.2 User đóng lệnh tay trên cTrader Desktop/Web — bridge handling verified step 3.5
```
[broker] ProtoOAExecutionEvent (UNSOLICITED — no clientMsgId)
  executionType = ORDER_FILLED (3)
  position.positionId = <positionId being closed>
  position.stopLoss, position.takeProfit (echoed for reason inference)
  deal.executionPrice = close price (DOUBLE — D-064)
  deal.executionTimestamp = close time (epoch ms)
  deal.commission = close-side commission (raw int)
  deal.closePositionDetail.grossProfit = realized P&L (raw int, D-053)
```

**Bridge handling** (step 3.5): `_on_message` sees `clientMsgId == ""`
→ unsolicited path → ``event_publisher.build_event_payload`` →
XADD `event_stream:ftmo:{account_id}` with:
- `event_type = "position_closed"`
- `broker_order_id = position.positionId`
- `close_reason` inferred via price comparison (see §3.6 below)

### 3.3 SL hit auto-close — same wire shape as §3.2
Identical execution event shape to §3.2 (cTrader does NOT emit a
distinct event type or flag for SL-triggered closes). The bridge
distinguishes via the heuristic in §3.6: if `close_price ≈
position.stopLoss` within the configured tolerance,
`event_type = position_closed` and `close_reason = "sl"`.

[Empirical confirmation pending step 3.5 smoke run — placeholder
section reserved for SL/TP-close inspector logs if the heuristic
ever misclassifies.]

### 3.4 TP hit auto-close — same wire shape as §3.2
Same as §3.3 but matched against `position.takeProfit`:
`close_reason = "tp"`.

### 3.5 Margin call / stop-out
[chưa observe — placeholder for Phase 4+ smoke]
Expected wire shape: combination of
`ProtoOAMarginCallTriggerEvent` (separate unsolicited event published
to the account auth'd connection) PLUS one or more
`ProtoOAExecutionEvent(ORDER_FILLED)` for each position closed by the
margin engine. The bridge currently classifies the close events as
`close_reason = "manual"` since the heuristic doesn't correlate with
margin-call timing. A future step can layer margin-call event
correlation to upgrade these to `close_reason = "stopout"`.

### 3.6 Bridge close-reason heuristic (step 3.5)
cTrader does NOT expose a structured close-reason field — verified
via DESCRIPTOR inspection of `ProtoOADeal` and
`ProtoOAClosePositionDetail` (step 3.5). The bridge infers reason
from echoed position SL/TP:

| Heuristic | Resulting `close_reason` |
|---|---|
| `abs(close_price - position.stopLoss) ≤ 1e-4` (1 pip) | `"sl"` |
| `abs(close_price - position.takeProfit) ≤ 1e-4` | `"tp"` |
| No SL set AND no TP set | `"manual"` |
| All other cases (has SL/TP but no match within tolerance) | `"manual"` |

Implementation: `ftmo_client/event_publisher.py::_infer_close_reason`.
Tolerance constant `_SL_TP_PRICE_TOLERANCE = 1e-4`. The `"unknown"`
and `"stopout"` protocol values (see `docs/05-redis-protocol.md §5`)
are reserved for future enhancements; the heuristic does not emit
them today.

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

ProtoOAOrder:
  orderId (int64) ← request-side ID (LIMIT/STOP pending broker_order_id)
  orderType (enum)
  ...

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

## 9. Update log

| Date | Step | Update |
|---|---|---|
| 2026-05-11 | 3.4c | Initial creation. Sections 1, 2.1-2.4, 3.1, 4.1-4.2, 5, 6, 7, 8 verified empirically through smoke step 3.4 + protobuf DESCRIPTOR inspection. Sections 3.2-3.5, 4.3 placeholder for step 3.5+ (unsolicited events). |
| 2026-05-11 | 3.5 | Sections 3.2 (manual close), 3.3 (SL hit), 3.4 (TP hit), 4.3 (manual modify) populated with bridge handling. Section 3.6 added documenting the close-reason inference heuristic (no protobuf close_reason field exists — verified by DESCRIPTOR inspection). Section 10 added for account info polling (balance via ProtoOATraderReq, margin via ProtoOAReconcileReq sum). Section 3.5 (margin call / stop-out) remains a placeholder pending margin-call event correlation in Phase 4+. |
