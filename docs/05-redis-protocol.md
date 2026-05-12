# 05 — Redis Protocol

## 1. Mục đích

Đây là spec **đầy đủ** giao thức Redis Streams giữa server ↔ N FTMO clients ↔ N Exness clients. Server và clients **PHẢI** tuân thủ chính xác để inter-op.

## 2. Stream naming convention

| Stream | Producer | Consumer | Purpose |
| --- | --- | --- | --- |
| `cmd_stream:ftmo:{account_id}` | server | FTMO client #N | Command → execute trên cTrader |
| `resp_stream:ftmo:{account_id}` | FTMO client | server | Response sau khi execute command |
| `event_stream:ftmo:{account_id}` | FTMO client | server | Unsolicited event (position closed bởi SL/TP/manual) |
| `cmd_stream:exness:{account_id}` | server | Exness client #N | Command → execute trên MT5 |
| `resp_stream:exness:{account_id}` | Exness client | server | Response |
| `event_stream:exness:{account_id}` | Exness client | server | Unsolicited (position closed bởi margin call/manual) |

`{account_id}` là user-defined string, vd `ftmo_acc_001`, `exness_main`. Ràng buộc: `[a-zA-Z0-9_-]{1,32}`.

## 3. Consumer groups

| Stream | Group name | Consumer name |
| --- | --- | --- |
| `cmd_stream:ftmo:{account_id}` | `ftmo-{account_id}` | `client` |
| `resp_stream:ftmo:{account_id}` | `server` | `server` |
| `event_stream:ftmo:{account_id}` | `server` | `server` |
| `cmd_stream:exness:{account_id}` | `exness-{account_id}` | `client` |
| `resp_stream:exness:{account_id}` | `server` | `server` |
| `event_stream:exness:{account_id}` | `server` | `server` |

> Server lúc startup phải tạo consumer groups cho **mọi** account đã đăng ký trong settings (`accounts:ftmo:*`, `accounts:exness:*`).

```python
# Server startup
for ftmo_acc in get_all_ftmo_accounts():
    XGROUP CREATE cmd_stream:ftmo:{acc} ftmo-{acc} 0 MKSTREAM
    XGROUP CREATE resp_stream:ftmo:{acc} server 0 MKSTREAM
    XGROUP CREATE event_stream:ftmo:{acc} server 0 MKSTREAM
    
for exness_acc in get_all_exness_accounts():
    XGROUP CREATE cmd_stream:exness:{acc} exness-{acc} 0 MKSTREAM
    XGROUP CREATE resp_stream:exness:{acc} server 0 MKSTREAM
    XGROUP CREATE event_stream:exness:{acc} server 0 MKSTREAM
```

Idempotent: nếu group đã tồn tại → ignore `BUSYGROUP` error.

## 4. Command format

### 4.1 Common fields (mọi command)

| Field | Type | Required | Mô tả |
| --- | --- | --- | --- |
| `request_id` | str (uuid hex) | Yes | UUID server gắn để correlate response |
| `order_id` | str | Yes | ID hedge order trong Redis (`order:{order_id}`) |
| `action` | str | Yes | `open` \| `close` \| `modify_sl_tp` |
| `created_at` | str (epoch ms) | Yes | Timestamp khi server push |

### 4.2 `action: open`

Áp dụng cả FTMO + Exness.

| Field | Type | Mô tả |
| --- | --- | --- |
| `symbol` | str | Symbol đã map về domain của broker đó (vd FTMO: `EURUSD`, Exness: `EURUSDm`) |
| `side` | str | `buy` \| `sell` |
| `volume_lots` | str (float) | Volume tính bằng lots, đã normalize cho broker |
| `sl` | str (float) | SL price. **CHỈ FTMO**. Exness ignore. `0` = no SL. |
| `tp` | str (float) | TP price. **CHỈ FTMO**. Exness ignore. `0` = no TP. |
| `order_type` | str | `market` \| `limit` \| `stop`. Mặc định `market`. |
| `entry_price` | str (float) | Bắt buộc nếu `limit`/`stop`. Bỏ qua nếu `market`. |

Ví dụ FTMO open:
```
XADD cmd_stream:ftmo:ftmo_acc_001 *
  request_id "a1b2c3..."
  order_id "ord_xyz"
  action "open"
  created_at "1735000000000"
  symbol "EURUSD"
  side "buy"
  volume_lots "0.45"
  sl "1.08200"
  tp "1.09000"
  order_type "market"
```

Ví dụ Exness open (không có SL/TP):
```
XADD cmd_stream:exness:exness_acc_001 *
  request_id "a1b2c3..."
  order_id "ord_xyz"
  action "open"
  created_at "1735000050000"
  symbol "EURUSDm"
  side "sell"
  volume_lots "0.45"
  order_type "market"
```

### 4.3 `action: close`

| Field | Type | Mô tả |
| --- | --- | --- |
| `broker_order_id` | str | ID position trên broker (cTrader positionId hoặc MT5 ticket) |

```
XADD cmd_stream:ftmo:ftmo_acc_001 *
  request_id "..."
  order_id "ord_xyz"
  action "close"
  created_at "1735000100000"
  broker_order_id "987654321"
```

### 4.4 `action: modify_sl_tp` (CHỈ FTMO)

| Field | Type | Mô tả |
| --- | --- | --- |
| `broker_order_id` | str | positionId |
| `sl` | str (float) | New SL. `0` = remove SL. |
| `tp` | str (float) | New TP. `0` = remove TP. |

## 5. Response format

### 5.1 Common fields

| Field | Type | Required | Mô tả |
| --- | --- | --- | --- |
| `request_id` | str | Yes | Match với command |
| `order_id` | str | Yes | |
| `action` | str | Yes | `open` \| `close` \| `modify_sl_tp` |
| `status` | str | Yes | `filled` \| `error` |
| `published_at` | str (epoch ms) | Yes | Timestamp client publish |

### 5.2 Khi `status: filled`

| Field | Type | Mô tả |
| --- | --- | --- |
| `broker_order_id` | str | Trả từ broker |
| `fill_price` | str (float) | Giá fill |
| `fill_time` | str (epoch ms) | Timestamp fill từ broker |
| `commission` | str (float) | Default `0` |

### 5.3 Khi `status: error`

| Field | Type | Mô tả |
| --- | --- | --- |
| `error_code` | str (int) | Broker error code (cTrader reason / MT5 retcode) |
| `error_msg` | str | Human readable |
| `retryable` | str | `true` \| `false` — client gợi ý có nên retry không |

Ví dụ:
```
XADD resp_stream:ftmo:ftmo_acc_001 *
  request_id "a1b2c3..."
  order_id "ord_xyz"
  action "open"
  status "filled"
  published_at "1735000000123"
  broker_order_id "987654321"
  fill_price "1.08412"
  fill_time "1735000000089"
  commission "0.5"
```

## 6. Event format (unsolicited)

Khi position bị đóng do SL/TP hit, manual close trên broker UI, margin call → client publish vào `event_stream:*`.

| Field | Type | Required | Mô tả |
| --- | --- | --- | --- |
| `event_type` | str | Yes | `position_closed` (cTrader) \| `position_closed_external` (Exness manual/margin) |
| `broker_order_id` | str | Yes | positionId / ticket |
| `close_price` | str (float) | If known | |
| `close_time` | str (epoch ms) | Yes | |
| `close_reason` | str | If known | `sl` \| `tp` \| `manual` \| `stopout` \| `unknown` |
| `realized_pnl` | str (float) | If known | Server tính lại từ tick để chắc chắn |
| `commission` | str (float) | If known | |

```
XADD event_stream:ftmo:ftmo_acc_001 *
  event_type "position_closed"
  broker_order_id "987654321"
  close_price "1.08600"
  close_time "1735000300000"
  close_reason "tp"
  realized_pnl "12.50"
  commission "0.5"
```

## 7. Idempotency rules

### R-IDM-1: Server gắn `request_id` (uuid hex 32 chars) khi push command

Mỗi command 1 `request_id` mới. Cùng `order_id` có thể có nhiều `request_id` (open + close = 2 commands, 2 request_ids).

### R-IDM-2: Client luôn XACK sau dispatch (kể cả error)

```python
try:
    result = await execute(command)
    await publish_response(result)
except Exception as e:
    await publish_error(e)
finally:
    await xack(stream, group, msg_id)  # ALWAYS
```

Nếu client crash giữa execute và XACK → message re-delivered → có thể double-execute. Server phòng vệ:
- Server check `pending_request_ids:{order_id}` set trước khi process response. Nếu đã process trước đó → skip.

### R-IDM-3: Server check status trước khi process response

```python
async def handle_response(resp):
    order = await redis.hgetall(f"order:{resp['order_id']}")
    
    # State guard
    if resp['action'] == 'open' and order['status'] not in ('pending', 'primary_filled'):
        log.warning("ignored stale open response: order=%s status=%s", 
                    resp['order_id'], order['status'])
        return
    
    # Process...
```

### R-IDM-4: Pending tracking ZSET

Khi push command:
```
ZADD pending_cmds:{broker}:{account_id}  <created_at_ms>  <request_id>
```

Khi nhận response:
```
ZREM pending_cmds:{broker}:{account_id}  <request_id>
```

Server timeout_checker scan ZSET, nếu entry > 30s → mark order timeout, ZREM, broadcast WS.

### R-IDM-5: Client KHÔNG retry tự động

Nếu open fail (vd retcode trading_hours) → client publish error, **KHÔNG retry**. Server quyết định retry hay fail order.

> Ngoại lệ: server có retry logic cho secondary open (retry 0.5/1/2s) — nhưng đây là server-side decision, mỗi retry có `request_id` mới.

## 8. Error codes mapping (non-retryable hint)

Client set field `retryable` dựa trên error code/retcode để hint cho server:

### FTMO (cTrader) — non-retryable
- `ORDER_REJECTED` reason `MARKET_CLOSED`
- `ORDER_REJECTED` reason `INVALID_PARAMS`
- `ACCESS_DENIED`

### Exness (MT5) — non-retryable
- `10030` (unsupported filling mode)
- `10013` (invalid request)
- `10014` (invalid volume)
- `10015` (invalid price)
- `10016` (invalid stops)
- `10017` (trade disabled)
- `10018` (market closed)
- `10019` (no money)

### Retryable (client set `retryable=true`)
- Network errors
- `10004` (requote — Exness)
- `10006` (request rejected — Exness, có thể retry)
- `10009` (request executed — không phải error)
- cTrader timeout không có response

## 9. Heartbeat keys

Format Redis hash, TTL 30s:

```
HSET client:ftmo:{account_id}
  status "online"            # online | error | offline
  last_seen "<epoch_ms>"
  version "v2.0.0"
EXPIRE client:ftmo:{account_id} 30
```

```
HSET client:exness:{account_id}
  status "online"
  last_seen "<epoch_ms>"
  version "v2.0.0"
EXPIRE client:exness:{account_id} 30
```

Server check `EXISTS client:ftmo:{account_id}` trước khi push command. Không exist → reject với 503 "ftmo client offline".

## 10. Account sync keys

Format Redis hash, no TTL (last write wins):

```
HSET account:ftmo:{account_id}
  balance "50012.34"
  equity "50050.12"
  margin "1234.56"
  free_margin "48815.56"
  currency "USD"
  updated_at "<epoch_ms>"
```

## 11. Stream length limits

Để Redis không phình bộ nhớ vô tận:

- `cmd_stream:*` và `resp_stream:*`: `MAXLEN ~ 10000` (xấp xỉ, mỗi message nhỏ ~200 bytes → 2MB cap mỗi stream).
- `event_stream:*`: `MAXLEN ~ 1000`.

Server và client dùng `XADD ... MAXLEN ~ N ...` syntax (giảm CPU vì không cần exact trim).

## 12. Stream cleanup

Sau 7 ngày, server cron job (hoặc CEO chạy thủ công) xóa stream cũ:
```
XTRIM cmd_stream:ftmo:{account_id} MINID <epoch_ms_7d_ago>
```

## 13. Test với redis-cli

Server developer có thể smoke-test client mà không cần frontend:

```bash
# Push fake open command
redis-cli XADD cmd_stream:ftmo:test_acc \* \
  request_id $(uuidgen | tr -d '-') \
  order_id "test_001" \
  action "open" \
  created_at $(date +%s000) \
  symbol "EURUSD" \
  side "buy" \
  volume_lots "0.01" \
  sl "0" \
  tp "0" \
  order_type "market"

# Read response
redis-cli XREAD COUNT 10 STREAMS resp_stream:ftmo:test_acc 0
```

---

## 14. Phase 3 additions (Single-leg Trading — FTMO only)

> Phase 3 implement spec từ §1-§13. Mục này ghi nhận **deltas thực tế** trong Phase 3 — chi tiết quyết định xem `DECISIONS.md` D-046 → D-149.

### 14.1 New keys

**`order:{order_id}` (HASH, no TTL)** — flat structure ~30+ fields. Phase 3 chỉ populate primary (`p_*`); secondary (`s_*`) defer Phase 4. Một số field chính:

```
HSET order:ord_xyz
  order_id "ord_xyz"
  pair_id "<uuid>"
  ftmo_account_id "ftmo_001"
  exness_account_id "exness_001"
  symbol "EURUSD"
  side "buy"
  order_type "market"
  sl_price "1.07900"
  tp_price "1.08500"
  entry_price "0"             # 0 cho market, set cho limit/stop (D-110, D-137)
  status "filled"             # pending | filled | closed | rejected | cancelled
  p_status "filled"
  p_volume_lots "0.01"
  p_broker_order_id "987..."  # positionId cho market, orderId cho pending (D-061)
  p_fill_price "1.08412"      # raw DOUBLE (D-055, D-064)
  p_executed_at "1735000000123"
  p_commission "..."
  p_swap "0"
  p_closed_at ""
  p_close_reason ""           # manual | sl | tp | stopout | unknown (D-071, D-078)
  p_realized_pnl ""           # raw int scaled by money_digits (D-068)
  p_money_digits "2"          # account money_digits (D-053)
  s_status "pending_phase_4"  # Phase 3 placeholder (D-083)
  s_volume_lots ""
  created_at "1735000000000"
  updated_at "1735000000123"
```

**`position_cache:{order_id}` (HASH, TTL 600s)** — written mỗi 1s bởi `position_tracker_loop` (D-096). Fields: `order_id, symbol, side, volume_lots, entry_price, current_price, unrealized_pnl, money_digits, is_stale ("true"|"false"), tick_age_ms, computed_at`. Tách khỏi legacy `position:{order_id}` JSON (Phase 5 consolidate).

**`orders:by_status:{status}` (SET)** — index per status để list nhanh (D-047). Phase 3 statuses: `pending`, `filled`, `closed`, `rejected`, `cancelled`. Atomic update khi status flip: SREM old + SADD new trong single pipeline.

**`request_id_to_order:{request_id}` (STRING)** — mapping `request_id → order_id` (D-047). Idempotency proxy: response_handler lookup khi resp_stream entry không có order_id (rare). Phase 3 hiện chưa actively dùng; Phase 4 cascade close cần.

### 14.2 Stream additions Phase 3

**`reconcile_state:{broker}:{account_id}` (STREAM)** — FTMO client → server reconciliation snapshot on connect (D-076).

Messages:
- `position_snapshot` — active positions từ ReconcileOpenPositionsRes.
- `pending_snapshot` — pending orders từ ReconcilePendingOrdersRes.
- `close_history` — close events backfill từ DealListByPositionIdRes per missing order.

```
XADD reconcile_state:ftmo:ftmo_001 *
  message_type "close_history"
  positionId "987654321"
  closePrice "1.08600"
  closeTime "1734900000000"
  grossProfit "150"
  commission "5"
  swap "0"
  closed_volume "100"
  money_digits "2"
  close_reason "unknown"              # reconstructed events luôn unknown (D-078)
  p_reconstructed "true"              # marker flag
```

Server consume qua handler riêng (step 3.7 `reconciliation.py`). Idempotent: skip nếu order_id đã có `p_status=closed` trong Redis.

### 14.3 Phase 3 expanded resp_stream + event_stream

**Phase 3 thêm action subtypes**:
- `place_market_order_with_sltp` (D-058): place_market_order → wait ORDER_FILLED → 100ms delay → amend_order. 1 cmd → 2 internal cTrader requests → 1 resp_stream entry.
- `place_pending_order`: place_market_order với type limit/stop, single ORDER_ACCEPTED event (D-062).
- `close_position`: 2-event sequence ACCEPTED → FILLED (D-066), realized_pnl từ `deal.closePositionDetail.grossProfit` raw (D-068).
- `modify_sl_tp`: single ORDER_REPLACED event (D-067).
- `fetch_close_history`: trigger reconciliation cho closed order missing — 3 retry attempts, exponential backoff 1s/2s (D-079).

**resp_stream message types**: `order_placed`, `order_rejected`, `close_dispatched`, `close_failed`, `modify_dispatched`, `modify_failed`. Khi error, kèm field `error_code` (lowercase snake_case, D-057 vocab — xem `08-server-api.md` §14.4).

**event_stream message types** (unsolicited từ FTMO client): `position_closed`, `pending_filled`, `position_modified`, `order_cancelled` (D-070).

**`position_closed` extended fields Phase 3** (D-074): `commission`, `swap`, `balance_after_close`, `money_digits`, `closed_volume` — từ `deal.closePositionDetail`. close_reason inference structured (D-071):
- `order.orderType == MARKET` AND `closingOrder == true` → `manual`.
- `order.orderType == STOP_LOSS_TAKE_PROFIT` (enum=4, D-075):
  - `grossProfit > 0` → `tp`.
  - `grossProfit < 0` → `sl`.

**`order_cancelled` noise filter (D-080)**: cTrader auto-cancels internal `STOP_LOSS_TAKE_PROFIT` order khi position close → publishes `order_cancelled` với internal `orderId` không có trong Redis cache. Server event_handler ignore nếu no Redis match.

### 14.4 WS channel whitelist Phase 3 (D-127)

```python
# server/app/api/ws.py
VALID_CHANNEL_PREFIXES = ("ticks:", "candles:", "positions", "orders", "accounts", "agents")
```

Channel validator hỗ trợ cả prefix-match (`ticks:`, `candles:`) và exact-match (`positions`, `orders`, `accounts`, `agents`) — `startswith(p) or channel == p` (D-109).

| Channel | Match type | Broadcast source | Cadence |
|---|---|---|---|
| `ticks:{symbol}` | prefix | MarketDataService spot subscribe | per tick (~5-10 Hz) |
| `candles:{symbol}:{tf}` | prefix | MarketDataService trendbar | per bar boundary |
| `positions` | exact | `position_tracker_loop` (Phase 3 new) | 1s |
| `orders` | exact | `response_handler` + `event_handler` (Phase 3 new) | per order state change |
| `accounts` | exact | `account_status_loop` (Phase 3 new) | 5s |
| `agents` | exact | Phase 1 legacy | (Phase 1) |

**WS message envelope** (D-105): `{ "channel": "<name>", "data": { "type": "<message_type>", ...payload } }`. Cụ thể message shapes xem `06-data-models.md` §15 + `08-server-api.md` §9.

### 14.5 Tick coalescing layer (D-118 root cause fix)

`BroadcastService.publish_tick(symbol, tick_data)` coalesces partial cTrader delta ticks với prev cached state:

1. **Fast path** (both `bid` + `ask` present): identity return, zero cache read.
2. **Partial path** (`bid` hoặc `ask` missing): merge với cached prev → emit complete tick.
3. **Initial state** (no prev + partial): drop publish + write to cache. Operator sẽ thấy first tick sau full update.

Defensive guards downstream (D-119) retained as belt-and-suspenders trong `position_tracker._compute_pnl` + `_convert_to_usd`.

### 14.6 Phase 3 expanded heartbeat schema

`client:{broker}:{account_id}` HASH unchanged from §9 but Phase 3 actively used cho:
- Account online/offline status precedence (D-128): `enabled=false` → disabled > heartbeat EXISTS → online else offline.
- OrderService validation pipeline: reject với `client_offline` error_code nếu offline (D-081).
- AccountStatusBar frontend rendering dot màu (online green / offline red / disabled gray).
