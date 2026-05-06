# 01 — Architecture

## 1. Triết lý kiến trúc

- **Decoupling tối đa**: server không gọi trực tiếp client nào. Mọi giao tiếp qua **Redis Streams** (cmd ↔ resp ↔ event).
- **Stream-per-account**: mỗi trading account có cmd/resp stream riêng → không có race condition giữa các client.
- **Server là orchestrator duy nhất**: cascade close, routing, P&L tracking đều ở server. Client chỉ làm 1 việc: thực thi command + gửi event.
- **Symbol whitelist**: file `symbol_mapping_ftmo_exness.json` là single source of truth cho symbols được phép trade. Symbol FTMO không có trong file → không hiển thị, không trade.
- **No SQL**: Redis làm message bus + state store. Không cần Postgres/SQLite.

## 2. Process layout

| Process | Vai trò | Host (đề xuất) | Run as |
| --- | --- | --- | --- |
| **Server (FastAPI)** | API + WS + market-data cTrader + orchestrator | 1 VPS hoặc máy local | uvicorn (NSSM nếu Windows) |
| **Redis 7** | Message bus + state store | Cùng máy server | Redis service / Memurai |
| **FTMO client #1..#N** | Trading cTrader cho 1 FTMO account | Mỗi client 1 máy riêng (Windows VPS hoặc local) | Python script / NSSM |
| **Exness client #1..#N** | Trading MT5 cho 1 Exness account | Mỗi client 1 máy riêng Windows | Python script + MT5 terminal / NSSM |
| **Frontend (built static)** | UI | Server đẻ ra qua FastAPI StaticFiles, hoặc nginx | — |

> **Tại sao mỗi client 1 máy riêng?**  
> 1. cTrader: kết nối TCP TLS, không bị giới hạn 1 máy nhưng tách ra dễ debug + isolation.  
> 2. MT5: bắt buộc Windows desktop session với MT5 terminal đã login. Mỗi MT5 instance = 1 user session → tách máy là cleanest.  
> 3. CEO yêu cầu rõ: "mỗi FTMO client phải chạy trên 1 máy khác nhau".

## 3. Single Source of Truth: Symbol Whitelist

File `symbol_mapping_ftmo_exness.json` đặt ở `docs/symbol_mapping_ftmo_exness.json`. Server đọc khi startup.

Cấu trúc 1 entry:
```json
{
  "ftmo_symbol": "EURUSD",
  "exness_symbol": "EURUSDm",
  "asset_class": "forex",
  "ftmo_units_per_lot": 100000,
  "exness_trade_contract_size": 100000,
  "ftmo_pip_size": 0.0001,
  "exness_pip_size": 0.0001,
  "quote_ccy": "USD",
  "match_type": "exact"
}
```

Server logic:
- Khi sync symbols từ market-data cTrader connection → lọc: chỉ giữ những symbols **có trong file** này.
- Volume conversion FTMO → Exness dùng ratio `ftmo_units_per_lot / exness_trade_contract_size`.
- Frontend chỉ hiển thị symbols pass whitelist.
- Symbol không có trong file → 404 nếu user gọi API.

## 4. Communication patterns

### 4.1 Browser ↔ Server

| Direction | Protocol | Purpose |
| --- | --- | --- |
| Browser → Server | REST `/api/*` | CRUD orders, fetch OHLC, login, settings, accounts management |
| Browser → Server | WS `/ws?token=<JWT>` | Subscribe channels, set_symbol, ping/pong |
| Server → Browser | WS push | tick, candle_update, position_update, hedge_open, primary_filled, hedge_closed, sl_tp_updated, order_error, agent_status, ping |

WS subscriptions per-channel:
- `positions` — broadcast P&L update + lifecycle event
- `ticks:{symbol}` — bid/ask của symbol active
- `candles:{symbol}:{tf}` — live trendbar cho timeframe đang xem
- `agents` — heartbeat status của tất cả clients (FTMO + Exness)

### 4.2 Server ↔ Market-data cTrader (single connection)

- TCP TLS tới `demo.ctraderapi.com:5035` (account demo).
- Protocol ProtoBuf qua `ctrader-open-api` lib.
- Twisted reactor + asyncio bridge **chỉ** ở chỗ này — vì server cần expose data qua FastAPI async.
- Auth: `client_id` + `client_secret` + access_token của account demo (lưu Redis hash `ctrader:market_data_creds`).
- Events nhận: `ProtoOASpotEvent` (tick), live trendbar.
- Requests gửi: `ProtoOAGetTrendbarsReq`, `ProtoOASubscribeSpotsReq`, `ProtoOASymbolsListReq`, `ProtoOASymbolByIdReq`.
- **KHÔNG** gửi `ProtoOANewOrderReq` qua connection này — trading luôn ở FTMO clients.

### 4.3 Server ↔ FTMO Trading Clients (Redis Streams)

| Stream | Direction | Producer | Consumer group |
| --- | --- | --- | --- |
| `cmd_stream:ftmo:{account_id}` | server → client | order_service / response_handler | `ftmo-{account_id}` (consumer = client #N) |
| `resp_stream:ftmo:{account_id}` | client → server | FTMO client | `server` |
| `event_stream:ftmo:{account_id}` | client → server | FTMO client (position closed events not tied to a request) | `server` |

`{account_id}` là user-defined ID của FTMO account, vd `ftmo_acc_001`, `ftmo_acc_002`.

Mỗi FTMO client chỉ subscribe stream của chính nó. Server biết route command tới `account_id` nào dựa vào pair user chọn.

### 4.4 Server ↔ Exness Trading Clients (Redis Streams)

Tương tự 4.3 với prefix `exness`:
- `cmd_stream:exness:{account_id}`
- `resp_stream:exness:{account_id}`
- `event_stream:exness:{account_id}`

### 4.5 Server ↔ Redis

Mọi state quan trọng đều ở Redis. Server không có in-memory state ngoài cache market-data adapter (symbol map, asset names, ticks).

## 5. Sequence: Đặt hedge order

```
Browser              Server                FTMO Client #N         Exness Client #N
   │                    │                         │                       │
   │ POST /orders/hedge │                         │                       │
   │ {pair_id, symbol,  │                         │                       │
   │  side, sl, tp,     │                         │                       │
   │  risk_amount}      │                         │                       │
   ├───────────────────►│                         │                       │
   │                    │ 1. Validate pair_id config                       │
   │                    │ 2. Whitelist symbol check                        │
   │                    │ 3. Heartbeat check both clients online           │
   │                    │ 4. Calc volume primary/secondary                 │
   │                    │ 5. HSET order:{id} status=pending                │
   │                    │ 6. XADD cmd_stream:ftmo:{ftmo_account_id}        │
   │                    │     {action:open, symbol, side, volume_p, sl, tp}│
   │                    ├────────────────────────►│                       │
   │ 200 {order_id}     │                         │                       │
   │◄───────────────────┤                         │                       │
   │                    │                         │ XREADGROUP cmd_stream │
   │                    │                         │ Twisted reactor:      │
   │                    │                         │ - place_order cTrader │
   │                    │                         │ - wait fill           │
   │                    │                         │ - XADD resp_stream:   │
   │                    │                         │   {status:filled,     │
   │                    │                         │    fill_price, broker_order_id}
   │                    │◄────────────────────────┤                       │
   │                    │ response_handler:                                │
   │                    │ - HSET order p_status=filled, status=primary_filled│
   │                    │ - XADD cmd_stream:exness:{exness_account_id}     │
   │                    │     {action:open, symbol, side(opposite), volume_s}│
   │                    ├──────────────────────────────────────────────────►│
   │ WS primary_filled  │                                                  │
   │◄───────────────────┤                                                  │
   │                    │                                                  │ XREADGROUP
   │                    │                                                  │ MT5 place_order
   │                    │                                                  │ XADD resp_stream
   │                    │◄─────────────────────────────────────────────────┤
   │                    │ response_handler:                                │
   │                    │ - HSET order s_status=filled, status=open        │
   │ WS hedge_open      │                                                  │
   │◄───────────────────┤                                                  │
```

Chi tiết → [`10-flows.md`](./10-flows.md).

## 6. Tại sao FTMO client chạy Twisted thuần (không bridge asyncio)?

Lessons learned từ docs v1: bridge Twisted ↔ asyncio trong server là pain point lớn (deadlocks, threading bugs, complex Future correlation).

**v2 đơn giản hóa**:
- FTMO client = Python process độc lập, chỉ chạy Twisted reactor.
- Redis client của FTMO process dùng **redis-py blocking** (không async). Twisted thread gọi blocking Redis call → OK vì process này không cần serve HTTP.
- 1 thread Twisted = main reactor, gọi `XREADGROUP` blocking với timeout 1s, dispatch command, gọi `XADD` blocking.
- Không có asyncio. Không có bridge. Không có Future correlation.

Trade-off:
- **Lợi**: code đơn giản, không bug threading.
- **Mất**: nếu cần phục vụ nhiều account trong 1 process → phải thread riêng. Nhưng docs v2 mỗi process chỉ 1 account → không cần.

Server vẫn cần Twisted-asyncio bridge cho 1 cTrader connection market-data. Đây là điểm duy nhất còn dùng bridge — chấp nhận được vì connection này không trade, không có order Future.

## 7. Frontend architecture

```
src/
├── main.tsx
├── App.tsx                          ← layout (resizable panels)
├── store/index.ts                   ← Zustand single store + persist middleware
├── api/
│   ├── index.ts                     ← axios + JWT interceptor
│   ├── auth.ts orders.ts pairs.ts symbols.ts
│   ├── positions.ts settings.ts charts.ts accounts.ts
├── hooks/
│   ├── useWebSocket.ts              ← single shared WS, diff subscribe
│   └── useChart.ts                  ← Lightweight Charts setup (anti-flash)
├── components/
│   ├── Login.tsx
│   ├── Settings.tsx                 ← pair management (add/remove/edit pairs)
│   ├── ToastContainer.tsx
│   ├── Chart/
│   │   ├── HedgeChart.tsx
│   │   ├── ChartContextMenu.tsx
│   │   └── PriceMeasureTool.tsx
│   ├── OrderForm/
│   │   ├── HedgeOrderForm.tsx       ← + PairPicker dropdown
│   │   ├── PairPicker.tsx
│   │   ├── VolumeCalculator.tsx
│   │   └── ConversionRateInfo.tsx
│   └── Dashboard/
│       ├── PositionList.tsx         ← Open + History tabs
│       └── AccountStatus.tsx        ← bar of all accounts (FTMO+Exness clients)
└── types/index.ts
```

Chi tiết → [`09-frontend.md`](./09-frontend.md).
