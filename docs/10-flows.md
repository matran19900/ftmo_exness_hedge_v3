# 10 — End-to-End Flows

Mỗi flow mô tả tuần tự actor + tin nhắn + Redis side-effect. Dùng làm test scenarios khi rebuild.

## 1. Bootstrap: thêm 1 cặp account + pair

Trước khi đặt được lệnh, user phải:

1. **Setup FTMO client máy 1**:
   - Cài Python + clone repo `apps/client-ftmo/`
   - Tạo `.env` với `ACCOUNT_ID=ftmo_acc_001`, `CTRADER_*` credentials
   - Chạy NSSM service
2. **Setup Exness client máy 2**:
   - Windows + MT5 terminal đã login
   - `.env` với `ACCOUNT_ID=exness_acc_001`, `MT5_*` credentials
   - Chạy NSSM service
3. **Server frontend**:
   - Settings → Accounts → Add FTMO `ftmo_acc_001` + Add Exness `exness_acc_001`
   - Settings → Pairs → Create pair `pair_main` linking 2 accounts
4. **Verify**: AccountStatus bar shows both green dots.

## 2. Đặt hedge MARKET

```
Browser              Server                FTMO Client              Exness Client
   │                    │                        │                       │
   │ POST /orders/hedge │                        │                       │
   │ {pair_id:pair_main,│                        │                       │
   │  symbol:EURUSD,    │                        │                       │
   │  side:buy,         │                        │                       │
   │  risk:100, sl, tp} │                        │                       │
   ├───────────────────►│                        │                       │
   │                    │ 1. whitelist check (EURUSD trong file?)        │
   │                    │ 2. get_pair(pair_main) → ftmo_acc_001+exness_acc_001
   │                    │ 3. heartbeat check both clients online         │
   │                    │ 4. get tick(EURUSD) → entry=ask                │
   │                    │ 5. validate SL direction + min 5 pips          │
   │                    │ 6. calculate_volume → vol_p=0.45, vol_s=0.45   │
   │                    │ 7. HSET order:ord_xyz status=pending           │
   │                    │ 8. XADD cmd_stream:ftmo:ftmo_acc_001 *         │
   │                    │     action=open symbol=EURUSD side=buy         │
   │                    │     volume_lots=0.45 sl=1.082 tp=1.090         │
   │                    │     request_id=<uuid> order_id=ord_xyz         │
   │                    │ 9. ZADD pending_cmds:ftmo:ftmo_acc_001         │
   │ 200 {ord_xyz,      │                        │                       │
   │      status:pending}│                       │                       │
   │◄───────────────────┤                        │                       │
   │                    │                        │ XREADGROUP loop reads │
   │                    │                        │ Twisted: place_order  │
   │                    │                        │   ProtoOANewOrderReq  │
   │                    │                        │ (~1-2s for fill)      │
   │                    │                        │ on_message: filled    │
   │                    │                        │ XADD resp_stream:ftmo:ftmo_acc_001
   │                    │                        │   request_id=<>, order_id=ord_xyz
   │                    │                        │   action=open status=filled
   │                    │                        │   broker_order_id=987 fill_price=1.0841
   │                    │                        │ XACK cmd_stream                       │
   │                    │ response_reader_loop reads resp_stream         │
   │                    │ handle_response:       │                       │
   │                    │ - ZREM pending_cmds    │                       │
   │                    │ - HSET order:ord_xyz status=primary_filled,    │
   │                    │   p_status=filled, p_broker_order_id=987,      │
   │                    │   p_fill_price=1.0841, p_executed_at=...       │
   │                    │ - WS broadcast primary_filled                  │
   │ WS primary_filled  │                                                │
   │◄───────────────────┤                                                │
   │                    │ - whitelist.map_to_exness(EURUSD)=EURUSDm     │
   │                    │ - XADD cmd_stream:exness:exness_acc_001 *      │
   │                    │     action=open symbol=EURUSDm side=sell       │
   │                    │     volume_lots=0.45 (no SL/TP)                │
   │                    │ - HSET order s_status=pending                  │
   │                    │ - ZADD pending_cmds:exness                     │
   │                    ├────────────────────────────────────────────────►│
   │                    │                                                │ XREADGROUP
   │                    │                                                │ asyncio + executor
   │                    │                                                │ mt5.order_send IOC
   │                    │                                                │ (~50-300ms)
   │                    │                                                │ XADD resp_stream
   │                    │                                                │   status=filled fill=1.0840
   │                    │                                                │ XACK
   │                    │◄───────────────────────────────────────────────┤
   │                    │ handle_response:                               │
   │                    │ - ZREM pending_cmds                            │
   │                    │ - HSET status=open, s_status=filled,          │
   │                    │   s_broker_order_id, s_fill_price, ...        │
   │                    │ - SREM orders:by_status:primary_filled         │
   │                    │ - SADD orders:by_status:open                   │
   │                    │ - WS broadcast hedge_open                      │
   │ WS hedge_open      │                                                │
   │◄───────────────────┤                                                │
```

Total time: 1-3s.

## 3. Cascade close — TP hit ở primary

```
Market giá tới 1.0900 → cTrader auto-close primary với TP

FTMO Client                Server               Exness Client
   │                          │                       │
   │ on_message_received:     │                       │
   │  ProtoOAExecutionEvent   │                       │
   │  POSITION_CLOSED         │                       │
   │  (no clientMsgId match)  │                       │
   │                          │                       │
   │ XADD event_stream:ftmo:ftmo_acc_001 *           │
   │   event_type=position_closed                     │
   │   broker_order_id=987 close_price=1.0900         │
   │   close_reason=tp realized_pnl=88.20             │
   ├─────────────────────────►│                       │
   │                          │ response_reader_loop reads event
   │                          │ handle_event:         │
   │                          │ - find order by p_broker_order_id=987
   │                          │   → ord_xyz            │
   │                          │ - status guard: not closed/closing → proceed
   │                          │ - HSET p_status=closed, p_close_price=1.090,
   │                          │   p_close_reason=tp, p_realized_pnl=88.20,
   │                          │   status=closing       │
   │                          │ - cascade close secondary:
   │                          │   XADD cmd_stream:exness:exness_acc_001 *
   │                          │     action=close broker_order_id=12345
   │                          ├──────────────────────►│
   │                          │                       │ mt5.order_send opposite
   │                          │                       │ ~100ms
   │                          │                       │ XADD resp_stream
   │                          │                       │   status=filled close_price=1.0900
   │                          │◄──────────────────────┤
   │                          │ handle_response:      │
   │                          │ - HSET s_status=closed, s_close_price,
   │                          │   s_close_reason=cascade, s_realized_pnl=-87.30
   │                          │ - both legs closed → final_pnl_usd=88.20-87.30=0.90
   │                          │ - HSET status=closed, closed_at=<>      │
   │                          │ - SREM orders:by_status:open            │
   │                          │ - ZADD orders:closed_history score=closed_at
   │                          │ - WS broadcast hedge_closed             │
```

Total time from TP hit to secondary close: 200-500ms.

## 4. Cascade close — manual close trên cTrader UI

User mở cTrader desktop, click Close trên position.

Tương tự flow 3 nhưng `close_reason=manual`.

## 5. Cascade close — manual close from app

User click × trên Position List của frontend.

```
Browser               Server               FTMO Client            Exness Client
   │ DELETE /orders/{id}   │                  │                       │
   ├──────────────────────►│                  │                       │
   │                       │ get_order, status=open                   │
   │                       │ XADD cmd_stream:ftmo: action=close       │
   │                       │   broker_order_id=p_broker_order_id      │
   │                       ├─────────────────►│                       │
   │ 200                   │                  │                       │
   │◄──────────────────────┤                  │                       │
   │                       │                  │ ProtoOAClosePositionReq
   │                       │                  │ ProtoOAExecutionEvent  │
   │                       │                  │   POSITION_CLOSED      │
   │                       │                  │ (clientMsgId match: this is response, not event)
   │                       │                  │ XADD resp_stream       │
   │                       │◄─────────────────┤ status=filled         │
   │                       │ handle_response (close action):          │
   │                       │ - update p_status=closed, p_close_*       │
   │                       │ - cascade close secondary                 │
   │                       │ ────────────────────────────────────────►│
   │                       │                                         (similar)
```

## 6. Position closed externally on Exness (margin call / manual MT5 UI)

```
Exness Client                  Server              FTMO Client
   │ position_monitor_loop:    │                       │
   │  detect ticket gone        │                       │
   │ XADD event_stream:exness * │                       │
   │   event_type=position_closed_external             │
   │   broker_order_id=12345                            │
   ├──────────────────────────►│                       │
   │                           │ handle_event:         │
   │                           │ - find by s_broker_order_id           │
   │                           │ - HSET s_status=closed, s_close_reason=external
   │                           │ - cascade close primary               │
   │                           │   XADD cmd_stream:ftmo: action=close  │
   │                           ├──────────────────────►│               │
   │                           │                       │ ProtoOAClosePositionReq
   │                           │                       │ XADD resp_stream
   │                           │◄──────────────────────┤
   │                           │ handle_response:                      │
   │                           │ - HSET p_status=closed, status=closed │
   │                           │ - WS hedge_closed                     │
```

> **Đây là flow MỚI ở v2**. V1 không có position_monitor_loop → primary có thể bị mồ côi.

## 7. P&L update real-time

```
Server (loop 1s):
   for each order in orders:by_status:open:
       tick = GET tick:{symbol}
       config = HGETALL symbol_config:{symbol}
       p_pnl_quote = (current - entry) * vol * contract_size
       s_pnl_quote = (entry - current) * vol * contract_size
       
       if quote == "USD":  rate = 1
       elif quote == "JPY": rate = 1 / tick(USDJPY).bid
       else: rate = ...
       
       p_pnl_usd = p_pnl_quote * rate
       s_pnl_usd = s_pnl_quote * rate
       
       SETEX position:{id} 600 JSON({pnl, computed_at})
       WS broadcast positions {type:pnl_update, p_pnl_usd, s_pnl_usd, total}
       
       every 30s: ZADD order:{id}:snaps score=now value=JSON({pnl, ts})
```

## 8. Timeout

```
timeout_checker_loop (every 60s):
   for each (broker, account_id):
       stuck = ZRANGEBYSCORE pending_cmds:{broker}:{account_id} 0 (now - 30000)
       for request_id, age in stuck:
           order_id = lookup_order_by_request_id(request_id)
           order = HGETALL order:{order_id}
           if order.status in (open, closed, cancelled, timeout):
               ZREM pending_cmds → cleanup
               continue
           HSET order:{order_id} status=timeout
           ZREM pending_cmds
           WS broadcast positions {type:order_timeout, order_id}
```

## 9. SL/TP drag

```
Browser                Server                  FTMO Client
   │ PATCH /orders/{id}/sl-tp │                    │
   │  {sl, tp}                │                    │
   ├─────────────────────────►│                    │
   │                          │ get_order, status check (must be open)
   │                          │ XADD cmd_stream:ftmo:* action=modify_sl_tp
   │                          │   broker_order_id, sl, tp
   │                          ├───────────────────►│
   │ 200 (optimistic)         │                    │ ProtoOAAmendPositionSLTPReq
   │◄─────────────────────────┤                    │ ProtoOAExecutionEvent
   │                          │                    │ XADD resp_stream filled
   │                          │◄───────────────────┤
   │                          │ HSET sl_price, tp_price
   │                          │ WS broadcast sl_tp_updated
   │ WS sl_tp_updated         │                    │
   │◄─────────────────────────┤                    │
```

Race (G6): user drag while primary đã đóng. PATCH fail (status != open) → frontend revert + toast.

## 10. Client offline → reject new orders

```
FTMO client #1 dies (process killed):
   client:ftmo:ftmo_acc_001 expires after 30s

Browser POST /orders/hedge with pair_id=pair_main:
   Server checks ftmo_acc_001 status → offline
   Return 503 "ftmo client ftmo_acc_001 offline"
   Frontend toast error
```

Note: existing open orders với pair này KHÔNG bị động. Khi client reconnect → continue serving.

## 11. Server restart recovery

```
Server crash + restart:
   1. lifespan() runs → setup_consumer_groups (idempotent)
   2. Read all orders với status NOT IN (closed, cancelled, timeout)
   3. For each, log "active order on restart: ord_xyz status=open"
   4. Position tracker resumes from Redis state
   5. Pending commands trong cmd_stream chưa XACK → client re-process khi reconnect
   
Risk: nếu server crash giữa primary fill và secondary push → primary đã có nhưng secondary chưa.
   - Restart: order status=primary_filled, s_status=waiting_primary
   - Manual recovery: CTO viết script `python -m apps.server.recovery resend-secondaries` 
     (hoặc CEO đóng manual primary trên cTrader)

→ Trong runbook (`docs/RUNBOOK.md`) ghi rõ procedure.
```

## 12. Smoke test checklist (post-rebuild)

Phải pass trước khi declare "production ready":

- [ ] Setup 2 accounts (1 FTMO + 1 Exness) + 1 pair.
- [ ] Login UI → token persist + reload không mất.
- [ ] Symbol search → chọn EURUSD → chart load 200 candles.
- [ ] Tick live update mỗi giây.
- [ ] Đặt market BUY EURUSD risk $100 → 2 leg fill < 5s.
- [ ] P&L update real-time, sai số < 1% so với cTrader/MT5 platform.
- [ ] Đặt USDJPY → P&L USD đúng (test JPY conversion).
- [ ] Đặt XAUUSD → P&L USD đúng (test commodity).
- [ ] Click × → cascade close → final P&L hiển thị.
- [ ] Đóng manual trên cTrader UI → cascade close → final P&L hiển thị.
- [ ] Đóng manual trên MT5 UI → cascade close primary (test position_monitor flow).
- [ ] Stop FTMO client → place lệnh → 503.
- [ ] Stop Exness client → place lệnh → 503.
- [ ] Drag SL line → server update + WS event → frontend đồng bộ.
- [ ] Right-click chart → set entry → form update.
- [ ] Tab History show orders đã close, expand row → đủ legs detail.
- [ ] Server restart → orders đang open vẫn track P&L sau khi back online.
- [ ] Symbol KHÔNG có trong whitelist → 404 khi gọi `/symbols/XXX`.
