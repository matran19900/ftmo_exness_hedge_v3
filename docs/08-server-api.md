# 08 — Server API (REST + WebSocket)

Base path: `/api`. Auth: `Authorization: Bearer <JWT>` cho mọi endpoint trừ `/auth/login` và `/auth/ctrader*`.

Error format:
```json
{ "detail": "human readable message" }
```

## 1. Authentication

### `POST /auth/login`
```json
// Request
{ "username": "admin", "password": "secret" }

// Response 200
{ "access_token": "eyJ...", "token_type": "Bearer", "expires_in": 86400 }
```
401 nếu sai. JWT TTL = `JWT_EXPIRE_MINUTES` (default 24h).

### `GET /auth/ctrader`
Redirect 302 đến cTrader consent page (cho **market-data account demo only**).

### `GET /auth/ctrader/callback?code=XXX&state=YYY`
Server-side OAuth code exchange → lưu Redis hash `ctrader:market_data_creds` → redirect `/`.

### `GET /auth/ctrader/status`
```json
{ "has_credentials": true, "expires_at": 1735000000000, "expires_in_seconds": 86000 }
```

## 2. Accounts management

### `GET /accounts`
```json
{
  "ftmo": [
    {
      "broker": "ftmo",
      "account_id": "ftmo_acc_001",
      "name": "FTMO Challenge $100k",
      "enabled": true,
      "status": "online",
      "balance": 50012.34,
      "equity": 50050.12,
      "currency": "USD"
    }
  ],
  "exness": [
    {
      "broker": "exness",
      "account_id": "exness_acc_001",
      "name": "Exness Hedge",
      "enabled": true,
      "status": "online",
      "balance": 4988.66,
      "equity": 4970.00,
      "currency": "USD"
    }
  ]
}
```

### `POST /accounts`
```json
// Request
{ "broker": "ftmo", "account_id": "ftmo_acc_002", "name": "Backup", "enabled": true }
```
Server:
- Validate `account_id` chưa tồn tại.
- SADD `accounts:{broker}` + HSET `account_meta:{broker}:{account_id}`.
- Tạo consumer groups cho streams mới.

### `DELETE /accounts/{broker}/{account_id}`
Reject nếu còn pair đang dùng account này. Reject nếu còn open orders.

### `PATCH /accounts/{broker}/{account_id}`
```json
{ "name": "New name", "enabled": false }
```

## 3. Pairs management

### `GET /pairs`
```json
[
  {
    "pair_id": "pair_main",
    "name": "Main Hedge",
    "ftmo_account_id": "ftmo_acc_001",
    "exness_account_id": "exness_acc_001",
    "secondary_ratio": 1.0,
    "enabled": true,
    "ftmo_status": "online",
    "exness_status": "online"
  }
]
```

### `POST /pairs`
```json
{
  "pair_id": "pair_test",
  "name": "Test Hedge",
  "ftmo_account_id": "ftmo_acc_001",
  "exness_account_id": "exness_acc_002",
  "secondary_ratio": 1.0
}
```
Validate cả 2 accounts tồn tại + enabled.

### `DELETE /pairs/{pair_id}`
Reject nếu còn open orders với pair này.

### `PATCH /pairs/{pair_id}`
```json
{ "name": "Renamed", "secondary_ratio": 1.5, "enabled": false }
```
Đổi `secondary_ratio` chỉ áp dụng cho lệnh mới, không động đến orders đã có.

## 4. Symbols

### `GET /symbols?asset_class=forex|crypto|index|commodity|forex_jpy`
Trả symbols active = symbols sync xong từ market-data + có trong whitelist.

```json
{
  "symbols": [
    {
      "symbol": "EURUSD",
      "exness_symbol": "EURUSDm",
      "asset_class": "forex",
      "digits": 5,
      "pip_size": 0.0001,
      "ftmo_contract_size": 100000,
      "exness_contract_size": 100000,
      "pip_value": 10.0,
      "quote_asset": "USD",
      "min_volume_lots": 0.01,
      "max_volume_lots": 1000,
      "volume_step": 0.01,
      "synced_at": "2026-05-04T01:23:45Z"
    }
  ]
}
```

### `GET /symbols/{symbol}`
Detail 1 symbol. 404 if not in `symbol_config`.

### `GET /symbols/{symbol}/tick`
```json
{ "symbol": "EURUSD", "bid": 1.08412, "ask": 1.08415, "ts": 1735000000000, "stale": false }
```
`stale: true` nếu `now - ts > 5s`. Server tự subscribe + đợi tối đa 2s nếu missing.

### `POST /symbols/{symbol}/calculate-volume`
```json
// Request
{
  "risk_amount": 100,
  "entry_price": 0,
  "sl_price": 1.08200,
  "secondary_ratio": 1.0
}

// Response 200
{
  "volume_primary": 0.45,
  "volume_secondary": 0.45,
  "sl_pips": 21.5,
  "sl_usd_value": 215.0,
  "pip_value": 10.0,
  "asset_class": "forex",
  "quote_asset": "USD",
  "conversion_rate": null,
  "conversion_pair": null
}
```

`entry_price=0` ⇒ market: dùng tick hiện tại.

## 5. Orders

### `POST /orders/hedge`
```json
// Request
{
  "pair_id": "pair_main",
  "symbol": "EURUSD",
  "side": "buy",
  "risk_amount": 100,
  "sl_price": 1.08200,
  "tp_price": 1.09000,
  "order_type": "market",
  "entry_price": 0,
  "secondary_ratio": null
}
```
- `pair_id` **bắt buộc**: chọn pair user pre-config.
- `secondary_ratio` null → dùng từ pair config.
- 503 nếu ftmo/exness client của pair offline.

```json
// Response 200
{ "order_id": "ord_abc123", "status": "pending" }
```

### `GET /orders?status=closed&limit=50&offset=0`
List orders, default trả mọi status. Filter `status=open` cho Open Positions tab, `status=closed` cho History tab.

```json
[
  {
    "order_id": "ord_abc",
    "pair_id": "pair_main",
    "symbol": "EURUSD",
    "side": "buy",
    "status": "closed",
    "p_status": "closed",
    "s_status": "closed",
    "p_volume_lots": 0.45,
    "s_volume_lots": 0.45,
    "p_fill_price": 1.08412,
    "s_fill_price": 1.08410,
    "p_close_price": 1.08600,
    "s_close_price": 1.08605,
    "p_realized_pnl": 84.6,
    "s_realized_pnl": -87.75,
    "final_pnl_usd": -3.15,
    "created_at": "2026-05-04T01:00:00Z",
    "closed_at": "2026-05-04T01:30:00Z",
    "p_close_reason": "tp",
    "s_close_reason": "cascade"
  }
]
```

### `GET /orders/{id}`
Same as list element + `snapshots` array (cho mini-chart P&L history).

### `DELETE /orders/{id}`
Close lệnh đang `open` hoặc `primary_filled`. Server gửi cascade close. Hoặc cancel pending (nếu chưa fill).

### `PATCH /orders/{id}/sl-tp`
```json
{ "sl_price": 1.08100, "tp_price": 1.09100 }
```
Áp dụng cho **primary leg only** (R3: secondary không có SL/TP).

### `DELETE /orders/{id}/purge`
Force xóa khỏi Redis (vd lệnh `secondary_failed` cleanup). Manual only.

## 6. Positions

### `GET /positions`
```json
[
  {
    "order_id": "ord_abc",
    "symbol": "EURUSD",
    "side": "buy",
    "status": "open",
    "p_pnl_usd": 5.20,
    "s_pnl_usd": -5.10,
    "total_pnl_usd": 0.10,
    "p_current_price": 1.08512,
    "s_current_price": 1.08510,
    "computed_at": 1735000060000
  }
]
```
Server iterate `orders:by_status:open` → đọc `position:{id}` cache.

## 7. Settings

### `GET /settings`
```json
{
  "default_secondary_ratio": 1.0,
  "primary_fill_timeout_seconds": 30,
  "position_tracker_interval_seconds": 1.0
}
```

### `PATCH /settings`
Patch fields. Validate range.

## 8. Charts

### `GET /charts/{symbol}/ohlc?timeframe=M15&count=200`
```json
{
  "symbol": "EURUSD",
  "timeframe": "M15",
  "candles": [
    { "time": 1735000000, "open": 1.08400, "high": 1.08450, "low": 1.08390, "close": 1.08412 }
  ]
}
```
Time field epoch seconds (Lightweight Charts convention). Server cache `ohlc:{sym}:{tf}` TTL 60s.

## 9. WebSocket `/ws`

### Connect
```
ws://host/ws?token=<JWT>
```
Auth fail → close code 4401.

### Client → Server messages
```json
// Subscribe
{ "type": "subscribe", "channels": ["positions", "ticks:EURUSD", "candles:EURUSD:M15", "agents"] }

// Unsubscribe  
{ "type": "unsubscribe", "channels": ["ticks:EURUSD"] }

// Set active symbol (server subscribes spots + conversion pair + live trendbar)
{ "type": "set_symbol", "symbol": "EURUSD", "timeframe": "M15" }

// Pong
{ "type": "pong" }
```

### Server → Client messages
```json
// Tick
{
  "channel": "ticks:EURUSD",
  "data": { "type": "tick", "symbol": "EURUSD", "bid": 1.08412, "ask": 1.08415, "ts": 1735000000000 }
}

// Live candle update
{
  "channel": "candles:EURUSD:M15",
  "data": { "type": "candle_update", "time": 1735000000, "open": 1.08400, "high": 1.08450, "low": 1.08390, "close": 1.08420 }
}

// P&L update
{
  "channel": "positions",
  "data": { "type": "pnl_update", "order_id": "ord_abc", "p_pnl_usd": 5.20, "s_pnl_usd": -5.10, "total_pnl_usd": 0.10, "computed_at": 1735000060000 }
}

// Lifecycle events (positions channel)
{ "channel": "positions", "data": { "type": "primary_filled", "order_id": "ord_abc" } }
{ "channel": "positions", "data": { "type": "hedge_open", "order_id": "ord_abc" } }
{ "channel": "positions", "data": { "type": "hedge_closed", "order_id": "ord_abc" } }
{ "channel": "positions", "data": { "type": "sl_tp_updated", "order_id": "ord_abc", "sl": 1.08100, "tp": 1.09100 } }
{ "channel": "positions", "data": { "type": "order_error", "order_id": "ord_abc", "msg": "..." } }
{ "channel": "positions", "data": { "type": "order_timeout", "order_id": "ord_abc" } }

// Agent status
{
  "channel": "agents",
  "data": {
    "type": "agent_status",
    "broker": "ftmo",
    "account_id": "ftmo_acc_001",
    "status": "offline"
  }
}

// Ping (keepalive)
{ "type": "ping" }
```

### Frontend rules
- Refetch `/positions` sau mọi lifecycle event (R25: WS event chỉ có `order_id`, không carry full state).
- Diff subscribe khi đổi symbol/timeframe (R24).
- Reconnect nếu close code != 1000.

## 10. Error responses

- `400` validation fail (sai format request, SL direction sai)
- `401` JWT invalid/expired
- `403` permission (không có ở v2 vì single admin)
- `404` resource not found (symbol, pair, order)
- `409` conflict (đổi primary_broker khi có open orders, xóa account đang dùng)
- `422` Pydantic validation fail
- `500` server internal
- `502` upstream broker error
- `503` client offline (FTMO/Exness)

---

## 11. Phase 3 additions

> Phase 3 implement spec từ §1-§10. Mục này ghi nhận **deltas thực tế** trong Phase 3 — chi tiết quyết định xem `DECISIONS.md` D-046 → D-149.

### 11.1 Path prefix unification

Phase 3 endpoints prefix `/api/` (mọi router). Phase 1/2 docs trong §1-§9 thỉnh thoảng dùng `/` không prefix; codebase thực tế **luôn** `/api/`.

### 11.2 Structured error format (D-082, D-111)

Phase 3 error format mở rộng từ Phase 1/2 `{ "detail": "human readable" }` sang structured cho machine-readable + Vietnamese i18n:

```json
{
  "detail": {
    "error_code": "client_offline",
    "message": "FTMO client offline cho account ftmo_001"
  }
}
```

`error_code` lowercase snake_case (D-057, D-111). Frontend `ORDER_ERROR_MESSAGES` map (D-111) translate sang Vietnamese tooltip + toast (xem `09-frontend.md` §5.5). Fallback chain 3 levels: structured detail → raw message → generic "Lỗi kết nối server".

### 11.3 POST /api/orders (D-081, D-110)

Phase 3 main endpoint. Phase 2 placeholder `POST /orders/hedge` (spec §5) **REPLACED** bởi `POST /api/orders` với Phase 3 schema:

```json
// Request
{
  "pair_id": "<uuid>",
  "symbol": "EURUSD",
  "side": "buy",
  "order_type": "market",          // market | limit | stop
  "volume_lots": 0.01,
  "sl": 1.07900,
  "tp": 1.08500,
  "entry_price": 0                  // 0 cho market (D-110, D-137), required > 0 cho limit/stop
}

// Response 202
{
  "order_id": "<uuid>",
  "request_id": "<uuid>",
  "status": "accepted",
  "message": "Order dispatched to FTMO client"
}
```

**Validation pipeline** (D-081): pair → account → client → symbol → config → volume → entry → tick → sl_tp_direction. Mỗi step có dedicated error_code (xem §11.7 vocab). Server normalizes SL/TP/entry rounded to `symbol_config.digits` post-validation (D-115).

### 11.4 GET /api/orders (D-099)

```json
// Response 200
{
  "orders": [
    {
      "order_id": "<uuid>",
      "pair_id": "<uuid>",
      "symbol": "EURUSD",
      "side": "buy",
      "order_type": "market",
      "status": "filled",
      "p_status": "filled",
      "p_volume_lots": "0.01",
      "p_fill_price": "1.08412",
      "p_executed_at": "1735000000123",
      // ... full OrderHash fields
    }
  ],
  "total": 42,
  "limit": 50,
  "offset": 0
}
```

Query params: `status`, `symbol`, `account_id` (filters), `limit` (1-200 default 50), `offset`. Sort: `created_at DESC` (D-102).

### 11.5 GET /api/orders/{id}

Detail endpoint. Returns single Order entity (same shape as list element). 404 nếu không tồn tại.

### 11.6 POST /api/orders/{id}/close (D-100)

```json
// Request body (empty or empty object)
{}

// Response 202
{ "order_id": "...", "request_id": "<uuid>", "status": "accepted", "message": "..." }
```

**Full close only Phase 3** (D-100). Partial close (volume_lots != current) → 400 với `partial_close_unsupported`. Phase 4+ có thể support partial khi cascade ratio scenarios cần.

### 11.7 POST /api/orders/{id}/modify (D-101)

```json
// Request
{ "sl": 1.07800, "tp": 1.08600 }  // both, or
{ "sl": 0 }                        // sl=0 removes existing SL
{ "tp": null }                     // tp=null keeps existing TP (no change)
```

Semantic: `None` = keep existing, `0` = remove (skip direction validation), positive = set with direction validation. Pydantic root validator rejects both `None` (must provide at least one).

### 11.8 GET /api/positions (D-099, D-104)

Live positions enriched với P&L từ `position_cache:{order_id}`:

```json
// Response 200
{
  "positions": [
    {
      "order_id": "<uuid>",
      "symbol": "EURUSD",
      "side": "buy",
      "volume_lots": "0.01",
      "entry_price": "1.08412",
      "current_price": "1.08512",
      "unrealized_pnl": "1234",         // raw int (D-053)
      "money_digits": "2",
      "is_stale": "false",
      "tick_age_ms": "500",
      "sl_price": "1.07900",
      "tp_price": "1.08500",
      "p_executed_at": "1735000000123"
    }
  ],
  "total": 1
}
```

Sort: `p_executed_at DESC` (D-102). Query params: `account_id`, `symbol`. **Just-filled race** (D-104): order in `orders:by_status:filled` but `position_cache:{id}` not yet computed → entry returned với `is_stale=true` + empty live fields. Tracker catches up next cycle.

### 11.9 GET /api/history (D-099, D-103)

Closed orders time-range filter:

```json
// Response 200
{
  "history": [/* full Order entities với p_closed_at, p_realized_pnl, p_close_reason */],
  "total": 23,
  "limit": 50,
  "offset": 0
}
```

Query params: `from_ts`, `to_ts` (epoch ms), `symbol`, `account_id`, `limit`, `offset`. Default window: last 7 days (D-103). `from_ts > to_ts` → 400 với `invalid_time_range`. Sort: `p_closed_at DESC` (D-102).

### 11.10 GET /api/accounts (D-125)

```json
// Response 200
{
  "accounts": [
    {
      "broker": "ftmo",
      "account_id": "ftmo_001",
      "name": "FTMO Challenge $100k",
      "enabled": true,
      "status": "online",        // online | offline | disabled (D-128 precedence)
      "balance_raw": "5001234",  // raw int (money_digits-scaled)
      "equity_raw": "5005012",
      "margin_raw": "123456",
      "free_margin_raw": "4881556",
      "currency": "USD",
      "money_digits": "2"
    }
  ],
  "total": 1
}
```

Sort: ftmo first, exness after, account_id asc (D-125). Status precedence (D-128): `enabled=false` → `disabled` overrides; else heartbeat EXISTS → `online` else `offline`.

### 11.11 PATCH /api/accounts/{broker}/{account_id} (D-143)

```json
// Request
{ "enabled": false }

// Response 200 (returns updated entry)
{ "broker": "ftmo", "account_id": "ftmo_001", "enabled": false, "status": "disabled", ... }
```

Authoritative `update_account_meta` HSET + auto-stamp `updated_at`. WS `account_status_loop` next cycle (~5s) sẽ broadcast snapshot updated; frontend cũng có thể optimistic update từ PATCH response.

### 11.12 DELETE /api/pairs/{id} (D-142 guard)

Phase 1/2 spec §3 `DELETE /pairs/{pair_id}` mention "Reject nếu còn open orders". Phase 3 implement guard explicitly:

```json
// 204 No Content nếu OK
// 409 Conflict nếu còn pending/filled orders reference
{
  "detail": {
    "error_code": "pair_in_use",
    "message": "Cannot delete pair: 1 order(s) reference it. Close them first."
  }
}
```

`count_orders_by_pair(pair_id)` scan `orders:by_status:{pending,filled}` only. Closed/cancelled NOT counted (historical references frozen — pair deletion won't corrupt order HASH).

### 11.13 WS channels Phase 3 (D-105, D-126, D-127)

VALID_CHANNEL_PREFIXES: `("ticks:", "candles:", "positions", "orders", "accounts", "agents")`. Channel validator: `startswith(p) or channel == p` (D-109 — prefix-match for `ticks:`/`candles:`, exact-match for others).

**Subscribe** (single hoisted WebSocket from MainPage, D-105):
```json
{ "type": "subscribe", "channels": ["ticks:EURUSD", "candles:EURUSD:H1", "positions", "orders", "accounts"] }
```

**New message types Phase 3**:

```json
// positions_tick (D-091, D-120) — from position_tracker_loop 1s batched
{
  "channel": "positions",
  "data": {
    "type": "positions_tick",
    "account_id": "ftmo_001",
    "ts": 1735000000000,
    "positions": [
      {
        "order_id": "<uuid>",
        "symbol": "EURUSD",
        "current_price": "1.08512",
        "unrealized_pnl": "1234",
        "is_stale": false,
        "tick_age_ms": 500,
        "side": "buy", "volume_lots": "0.01", "entry_price": "1.08412",
        "money_digits": "2", "sl_price": "1.07900", "tp_price": "1.08500",
        "p_executed_at": "1735000000123"
      }
    ]
  }
}

// order_updated (D-087) — from response_handler + event_handler
{
  "channel": "orders",
  "data": {
    "type": "order_updated",
    "order_id": "<uuid>",
    "status": "filled",
    "p_status": "filled",
    "p_broker_order_id": "987654321",
    "p_fill_price": "1.08412",
    "p_executed_at": "1735000000123"
  }
}

// account_status (D-126, D-147) — from account_status_loop 5s
{
  "channel": "accounts",
  "data": {
    "type": "account_status",
    "ts": 1735000005000,
    "accounts": [/* AccountStatusEntry[] với typed enabled bool, status Literal */]
  }
}
```

### 11.14 Error code vocab Phase 3 (D-057, D-111)

Common error_codes + frontend Vietnamese mapping (`ORDER_ERROR_MESSAGES` map):

| `error_code` | Vietnamese frontend message |
|---|---|
| `client_offline` | "FTMO client offline — không thể gửi lệnh" |
| `invalid_pair` | "Pair không hợp lệ" |
| `invalid_volume` | "Volume không hợp lệ (min/max/step)" |
| `invalid_entry_direction` | "Hướng entry không hợp lệ" |
| `invalid_sl_direction` | "Hướng SL không hợp lệ" |
| `pair_in_use` | server message used directly (with count) |
| `account_not_found` | "Account không tồn tại" |
| `partial_close_unsupported` | "Phase 3 chỉ hỗ trợ close full position" |
| `sl_tp_attach_failed` | "Không thể attach SL/TP sau fill — operator decides" |
| `invalid_time_range` | "Khoảng thời gian không hợp lệ" |
| `invalid_request` | "Yêu cầu không hợp lệ" |
| `missing_entry_price` | "Limit/Stop cần entry_price > 0" |
| `no_tick_available` | "Chưa có tick dữ liệu — chờ broker sync" |

Frontend fallback 3 levels (D-111): structured `detail.error_code` → raw `detail.message` → generic "Lỗi kết nối server".

### 11.15 Phase 3 endpoint summary

8 Phase 3 endpoints aggregate (vs Phase 1/2 spec §5 placeholders):

| Method | Path | Auth | Purpose | Decision |
|---|---|---|---|---|
| POST | `/api/orders` | JWT | Create order async | D-081, D-099 |
| GET | `/api/orders` | JWT | List orders với filter | D-099, D-102 |
| GET | `/api/orders/{id}` | JWT | Order detail | D-099 |
| POST | `/api/orders/{id}/close` | JWT | Close dispatch async | D-099, D-100 |
| POST | `/api/orders/{id}/modify` | JWT | Modify SL/TP async | D-099, D-101 |
| GET | `/api/positions` | JWT | Live positions với P&L | D-099, D-104 |
| GET | `/api/history` | JWT | Closed orders time-range | D-099, D-103 |
| GET | `/api/accounts` | JWT | List accounts với status | D-125 |
| PATCH | `/api/accounts/{broker}/{account_id}` | JWT | Toggle enabled | D-143 |
| DELETE | `/api/pairs/{id}` | JWT | Delete pair (guarded) | D-142 |
