# 06 — Data Models (Redis Schema)

## 1. Tổng quan

Redis 7 là single source of truth cho mọi runtime state. Không có SQL DB. Data structures dùng:
- **Hash**: object với fields (orders, positions, accounts, settings).
- **Set**: collections không có order (symbols active, accounts list).
- **Sorted Set (ZSET)**: order theo score (pending orders, history, snapshots).
- **Stream**: event log (cmd/resp/event streams).
- **String**: cache đơn giản với TTL (ticks, OHLC).

## 2. Naming conventions

- Lowercase, snake_case.
- Hierarchy với `:` (namespace pattern).
- Account-scoped keys luôn có `{account_id}` ở cuối.

## 3. Settings & global config

### `app:settings` (HASH, no TTL)

```
HSET app:settings
  default_secondary_ratio "1.0"
  primary_fill_timeout_seconds "30"
  position_tracker_interval_seconds "1.0"
  symbol_map_version "1"
```

Patched qua `PATCH /settings`.

### `ctrader:market_data_creds` (HASH, no TTL)

Credentials của market-data cTrader connection (account demo).

```
HSET ctrader:market_data_creds
  access_token "<token>"
  refresh_token "<token>"            # lưu nhưng không auto-refresh ở v2
  expires_at "1735000000000"
  saved_at "1734000000000"
  ctid_trader_account_id "12345"
  account_currency "USD"
```

## 4. Accounts management

### `accounts:ftmo` (SET) — danh sách account_id của FTMO accounts

```
SADD accounts:ftmo "ftmo_acc_001"
SADD accounts:ftmo "ftmo_acc_002"
```

### `accounts:exness` (SET)

```
SADD accounts:exness "exness_acc_001"
```

### `account_meta:ftmo:{account_id}` (HASH) — metadata

```
HSET account_meta:ftmo:ftmo_acc_001
  name "FTMO Challenge $100k"
  created_at "1735000000000"
  enabled "true"
```

User-friendly name để frontend hiển thị. Field `enabled` để tạm tắt account mà không xóa hẳn.

### `account_meta:exness:{account_id}` (HASH)

Tương tự.

### `client:ftmo:{account_id}` (HASH, TTL 30s)

Heartbeat từ client. Server check existence để biết online/offline.

```
HSET client:ftmo:ftmo_acc_001
  status "online"
  last_seen "1735000000000"
  version "v2.0.0"
EXPIRE client:ftmo:ftmo_acc_001 30
```

### `client:exness:{account_id}` (HASH, TTL 30s)

Tương tự.

### `account:ftmo:{account_id}` (HASH, no TTL)

Balance/equity sync từ broker mỗi 30s.

```
HSET account:ftmo:ftmo_acc_001
  balance "50012.34"
  equity "50050.12"
  margin "1234.56"
  free_margin "48815.56"
  currency "USD"
  updated_at "1735000000000"
```

## 5. Pairs management

### `pairs:all` (SET) — danh sách pair_id

```
SADD pairs:all "pair_main"
SADD pairs:all "pair_test"
```

### `pair:{pair_id}` (HASH)

```
HSET pair:pair_main
  ftmo_account_id "ftmo_acc_001"
  exness_account_id "exness_acc_001"
  secondary_ratio "1.0"
  enabled "true"
  created_at "1735000000000"
  name "Main Account Hedge"
```

User pre-config trong settings UI. Khi đặt lệnh, frontend gửi `pair_id` → server lookup `pair:{pair_id}` → biết route command tới account nào.

## 6. Symbols

### `symbols:active` (SET)

Symbols đã pass whitelist + sync xong từ market-data cTrader. Frontend gọi `GET /symbols` → server iterate set này.

```
SADD symbols:active "EURUSD"
SADD symbols:active "USDJPY"
SADD symbols:active "XAUUSD"
```

### `symbol_config:{ftmo_symbol}` (HASH)

```
HSET symbol_config:EURUSD
  ftmo_symbol "EURUSD"
  exness_symbol "EURUSDm"
  asset_class "forex"
  digits "5"
  pip_size "0.0001"
  ftmo_contract_size "100000"
  exness_contract_size "100000"
  pip_value "10.0"
  quote_asset "USD"
  ctrader_symbol_id "1"
  min_volume_lots "0.01"
  max_volume_lots "1000"
  volume_step "0.01"
  synced_at "1735000000000"
```

`ftmo_symbol` luôn là key chính (frontend gửi FTMO symbol, server map sang Exness khi push command Exness).

### `tick:{ftmo_symbol}` (STRING, TTL 5s)

JSON string:
```json
{"bid": 1.08412, "ask": 1.08415, "ts": 1735000000000}
```

### `ohlc:{ftmo_symbol}:{timeframe}` (STRING, TTL 60s)

JSON cached array of candles từ cTrader trendbars. Frontend `GET /charts/{sym}/ohlc` đọc cache trước, miss → fetch từ market-data adapter → SETEX.

## 7. Orders

### `order:{order_id}` (HASH, no TTL — vĩnh viễn)

Flat structure (không nested), 2 leg fields prefix `p_*` và `s_*`:

```
HSET order:ord_xyz
  order_id "ord_xyz"
  pair_id "pair_main"
  ftmo_account_id "ftmo_acc_001"
  exness_account_id "exness_acc_001"
  symbol "EURUSD"                       # FTMO symbol (server map cho Exness)
  side "buy"                            # primary side
  status "open"                         # pending | primary_filled | open | closing | closed | cancelled | timeout | secondary_failed
  risk_amount "100"
  secondary_ratio "1.0"
  sl_price "1.08200"
  tp_price "1.09000"
  order_type "market"
  entry_price "0"                       # 0 cho market, set cho limit/stop
  
  # Primary leg (FTMO)
  p_status "filled"                     # waiting | pending | filled | error | closing | closed
  p_volume_lots "0.45"
  p_broker_order_id "987654321"
  p_fill_price "1.08412"
  p_executed_at "1735000000123"
  p_close_price ""
  p_closed_at ""
  p_close_reason ""
  p_realized_pnl ""
  p_commission "0.5"
  
  # Secondary leg (Exness)
  s_status "filled"
  s_volume_lots "0.45"
  s_broker_order_id "12345678"
  s_fill_price "1.08410"
  s_executed_at "1735000000456"
  s_close_price ""
  s_closed_at ""
  s_close_reason ""
  s_realized_pnl ""
  s_commission "0.3"
  
  # Lifecycle
  created_at "1735000000000"
  updated_at "1735000000456"
  closed_at ""
  final_pnl_usd ""
  
  # Errors / retry
  s_error_msg ""
  s_retry_count "0"
```

### `orders:by_status:{status}` (SET)

Index per status để list nhanh.

```
SADD orders:by_status:pending "ord_xyz"
SADD orders:by_status:open "ord_abc"
```

Khi update status → SREM old + SADD new (atomic with MULTI/EXEC).

### `orders:closed_history` (ZSET)

Score = `closed_at` (epoch ms). Member = `order_id`. Để paginate History tab.

```
ZADD orders:closed_history 1735000300000 "ord_xyz"
```

`GET /orders?status=closed&limit=50&offset=0` → ZREVRANGE 0 49.

### `pending_cmds:ftmo:{account_id}` (ZSET)

Score = `created_at` (epoch ms). Member = `request_id`. Timeout checker scan để detect stuck commands.

```
ZADD pending_cmds:ftmo:ftmo_acc_001 1735000000000 "<request_id>"
```

ZREM khi nhận response.

### `pending_cmds:exness:{account_id}` (ZSET)

Tương tự.

## 8. Positions (P&L tracking cache)

### `position:{order_id}` (STRING, TTL 600s)

JSON snapshot do `position_tracker_loop` SETEX mỗi giây:

```json
{
  "order_id": "ord_xyz",
  "symbol": "EURUSD",
  "p_pnl_usd": 5.20,
  "s_pnl_usd": -5.10,
  "total_pnl_usd": 0.10,
  "p_current_price": 1.08512,
  "s_current_price": 1.08510,
  "computed_at": 1735000060000
}
```

Frontend đọc qua `GET /positions` (server iterate `orders:by_status:open` → đọc `position:{id}` cache).

### `order:{order_id}:snaps` (ZSET, TTL 600s sau closed_at)

Score = epoch ms. Member = JSON `{"pnl_usd": ..., "ts": ...}`. Snapshot mỗi 30s để vẽ mini-chart P&L history trong order detail modal.

## 9. WebSocket subscriptions tracking (in-memory)

Server giữ trong RAM (không Redis), reset khi restart:

```python
ws_subscriptions: dict[WebSocket, set[str]] = {}
ws_active_symbol: dict[WebSocket, str] = {}
```

Vì <10 user, không cần persist subscription state.

## 10. Time & timestamps

- **Mọi timestamp lưu epoch ms (int as string trong Redis)**.
- **ISO format chỉ ở display layer** (frontend convert).
- Server tin **server clock**. Broker timestamps (`fill_time`) chỉ tham khảo, không dùng cho ordering.

## 11. TTL strategy summary

| Key pattern | TTL | Lý do |
| --- | --- | --- |
| `tick:{sym}` | 5s | Stale > 5s coi như không tin cậy |
| `client:ftmo:{acc}`, `client:exness:{acc}` | 30s | Heartbeat 10s, allow 3 lost → mark offline |
| `position:{id}` | 600s | Sau order close, vẫn cho UI lazy đọc 10 phút |
| `ohlc:{sym}:{tf}` | 60s | Cache để giảm cTrader call |
| `order:{id}:snaps` | 600s sau closed | Đủ cho user xem detail |
| Streams | MAXLEN ~ 10000 | Phòng phình bộ nhớ |
| Tất cả khác | None | Vĩnh viễn |

## 12. Backup strategy (đơn giản hóa v2)

CEO chạy thủ công khi cần:

```bash
redis-cli BGSAVE
# wait until LASTSAVE returns new timestamp
cp /var/lib/redis/dump.rdb /backup/dump-$(date +%Y%m%d-%H%M).rdb
```

KHÔNG có cron tự động ở phase 1.

## 13. DTO / Pydantic schemas

Server dùng Pydantic cho REST request/response. Schemas đặt ở `app/schemas/`.

### Order schemas

```python
class CreateHedgeOrderRequest(BaseModel):
    pair_id: str
    symbol: str
    side: Literal["buy", "sell"]
    risk_amount: float
    sl_price: float
    tp_price: float = 0
    order_type: Literal["market", "limit", "stop"] = "market"
    entry_price: float = 0
    secondary_ratio: float | None = None  # Override pair default

class OrderResponse(BaseModel):
    order_id: str
    pair_id: str
    symbol: str
    side: str
    status: str
    p_status: str
    s_status: str
    # ... all fields from order:{id} hash, typed
```

### Pair schemas

```python
class CreatePairRequest(BaseModel):
    pair_id: str        # user-provided
    name: str
    ftmo_account_id: str
    exness_account_id: str
    secondary_ratio: float = 1.0

class PairResponse(BaseModel):
    pair_id: str
    name: str
    ftmo_account_id: str
    exness_account_id: str
    secondary_ratio: float
    enabled: bool
    ftmo_status: Literal["online", "offline", "error"]
    exness_status: Literal["online", "offline", "error"]
```

### Account schemas

```python
class CreateAccountRequest(BaseModel):
    broker: Literal["ftmo", "exness"]
    account_id: str       # user-provided
    name: str
    enabled: bool = True

class AccountResponse(BaseModel):
    broker: str
    account_id: str
    name: str
    enabled: bool
    status: Literal["online", "offline", "error"]
    balance: float | None
    equity: float | None
    currency: str | None
```

## 14. Migration từ v1 (KHÔNG áp dụng — rebuild from scratch)

Không có migration. Project rebuild from scratch, Redis bắt đầu rỗng.
