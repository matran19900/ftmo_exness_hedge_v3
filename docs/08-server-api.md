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
