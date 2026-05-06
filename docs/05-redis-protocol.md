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
