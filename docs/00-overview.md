# 00 — System Overview

## 1. Bài toán

Trader trên prop firm (FTMO) bị giới hạn drawdown rất chặt. Khi đặt 1 lệnh thật trên FTMO, ta đồng thời mở **1 lệnh ngược chiều** trên broker thường (Exness) với volume tính theo **ratio mapping** → P&L hai bên gần triệt tiêu khi market dịch chuyển.

- **Lệnh nào thắng** → giữ.
- **Lệnh nào thua** → đóng cùng lúc với lệnh thắng (cascade close) hoặc khi SL hit ở primary.
- **Hai bên cùng volume theo ratio** → khi market di chuyển, P&L hai bên gần triệt tiêu, nhưng phía nào thắng vẫn rút được tiền.

Ý nghĩa thực tế: **chuyển rủi ro của FTMO challenge sang broker thường** — nếu fail challenge, lãi tương đương ở Exness; nếu pass, lãi gấp đôi (Exness lỗ + FTMO payout).

## 2. Mục tiêu hệ thống (3 mục tiêu cốt lõi)

1. **Sync 2 lệnh nhanh**: FTMO trước → Exness sau cùng symbol mapping, volume theo ratio, ngược chiều.
2. **Cascade close**: 1 leg đóng (SL/TP/manual) → leg còn lại đóng theo, **không sót**.
3. **Real-time P&L USD chính xác** cho mọi asset class: forex (bao gồm JPY pairs), indices, crypto, commodities.

Mục tiêu phụ:
- **Single-pane UI** kiểu TradingView: chart + form + position list + history + settings.
- **Multi-pair**: N pair (FTMO + Exness), user chọn manual pair khi đặt lệnh.
- **Robust to disconnect**: client offline → server reject lệnh mới với pair đó; reconnect tự động.
- **No SQL DB** — chỉ Redis.

## 3. Non-goals

- KHÔNG auto-trading / strategy engine. Tool **thủ công**.
- KHÔNG multi-user / multi-tenant — single admin (nhưng admin có thể có **N FTMO accounts × N Exness accounts**).
- KHÔNG OCO, trailing stop, partial close (phase 1).
- KHÔNG broker khác ngoài cTrader (FTMO) + MT5 (Exness).
- KHÔNG HTTPS/nginx/LetsEncrypt — chạy HTTP localhost / Tailscale / Cloudflare Tunnel.
- KHÔNG OAuth refresh token logic phức tạp (đơn giản hóa).
- KHÔNG structured JSON logging.

## 4. Tech Stack

| Tier | Stack |
| --- | --- |
| Frontend | React 18 + TypeScript + Vite + Lightweight Charts v5 + Zustand + Axios |
| Server | Python 3.11 + FastAPI + uvicorn + Twisted (chỉ cho 1 market-data cTrader connection) + asyncio |
| FTMO client | Python 3.11 + ctrader-open-api + Twisted **thuần** (không bridge asyncio) + redis-py |
| Exness client | Python 3.11 + MetaTrader5 lib + redis-py + asyncio |
| Storage / Bus | Redis 7 (streams, hashes, ZSETs) |
| Auth | JWT + bcrypt |
| Deploy | Server: 1 VPS / Local. Mỗi FTMO client + Exness client: 1 máy riêng (Windows VPS hoặc local) |

## 5. Kiến trúc tổng thể (high-level)

```
┌─────────────────────────────────────────────────────────────────────┐
│                  Browser (React SPA)                                │
│   Chart + Order Form (pair picker) + Position List + History        │
└──────────────┬──────────────────────────────┬──────────────────────┘
               │ REST /api/*                  │ WebSocket /ws (JWT)
               ▼                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Server (FastAPI, single process)                       │
│  ┌────────────────────────────────────────────────────────────┐     │
│  │ 1 cTrader market-data connection (account demo)            │     │
│  │   - Lấy chart OHLC, tick, live trendbar, symbols list      │     │
│  │   - KHÔNG đặt lệnh                                         │     │
│  │   - Twisted thread + asyncio bridge (chỉ chỗ này)          │     │
│  └────────────────────────────────────────────────────────────┘     │
│  ┌────────────────────────────────────────────────────────────┐     │
│  │ Order Service (orchestrator)                               │     │
│  │   - Validate, route lệnh tới đúng pair (ftmo+exness)       │     │
│  │   - Cascade close logic                                    │     │
│  │   - Symbol whitelist filter (symbol_mapping_ftmo_exness.json)│   │
│  └────────────────────────────────────────────────────────────┘     │
│  ┌────────────────────────────────────────────────────────────┐     │
│  │ Background tasks: position_tracker, response_reader,       │     │
│  │   timeout_checker, broadcast (WS)                          │     │
│  └────────────────────────────────────────────────────────────┘     │
└─────┬───────────────────────────────────────────────────────┬──────┘
      │ Redis Streams                                         │ Redis Streams
      ▼                                                       ▼
┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐
│ FTMO Client #1      │ │ FTMO Client #N      │ │ Exness Client #1..N │
│ (máy riêng)         │ │ (máy riêng)         │ │ (Windows máy riêng) │
│                     │ │                     │ │                     │
│ - Twisted thuần     │ │ - Twisted thuần     │ │ - asyncio + MT5 lib │
│ - cTrader place/    │ │ - cTrader place/    │ │ - place/close MT5   │
│   close trading     │ │   close trading     │ │                     │
│ - heartbeat         │ │ - heartbeat         │ │ - heartbeat         │
│ - account sync      │ │ - account sync      │ │ - account sync      │
└────────┬────────────┘ └────────┬────────────┘ └────────┬────────────┘
         │ TCP                   │ TCP                   │
         ▼                       ▼                       ▼
   FTMO cTrader server     FTMO cTrader server     Exness MT5 terminal
   (account #1)            (account #N)            (account #1..N)
```

Chi tiết → [`01-architecture.md`](./01-architecture.md).

## 6. Glossary

| Term | Nghĩa |
| --- | --- |
| Hedge order | 1 logic order user tạo, gồm 2 legs (primary FTMO + secondary Exness) |
| Leg | 1 vế của hedge |
| Primary | FTMO leg — có SL/TP, P&L thật |
| Secondary | Exness leg — KHÔNG có SL/TP, market-only, đóng theo cascade |
| Pair | 1 cặp (FTMO account + Exness account) user pre-config trong settings |
| Cascade close | Khi 1 leg đóng → server gửi command đóng leg còn lại |
| Pending order | Limit/stop chưa fill |
| `primary_filled` | Primary đã fill, đang chờ secondary fill |
| `open` | Cả 2 legs đã fill |
| Market-data conn | 1 cTrader connection riêng trong server, chỉ market data, không trade |
| Symbol whitelist | File `symbol_mapping_ftmo_exness.json` — chỉ symbols trong file mới hiển thị + trade được |
| Conversion pair | Pair phụ (vd USDJPY) cần subscribe để quy đổi P&L → USD |
| Pip value | Tiền tệ /1 pip /1 lot, ở deposit asset |
| Risk amount | USD user sẵn sàng mất nếu SL hit (driver tính volume) |
| Tick | Snapshot bid/ask + timestamp |
| Trendbar | OHLC candle do cTrader gửi (live cho timeframe đang xem) |
| `account_id` | ID nội bộ của 1 trading account (FTMO hoặc Exness) — user đặt trong settings |
