# PROJECT_STATE — Live snapshot

> Cập nhật lần cuối: 2026-05-09 — Claude Code (step 2.10 phase-2 docs sync)
> ĐỌC FILE NÀY ĐẦU TIÊN khi mở phiên CTO chat mới.

## Vị trí hiện tại
- **Phase**: 2 — Market Data + Chart + Form — HOÀN THÀNH
- **Step vừa hoàn thành**: 2.10 — phase-2-docs-sync (chính step này)
- **Phase tiếp theo**: 3 — Single-leg Trading (FTMO only)
- **Step tiếp theo**: 3.1 — `redis_service.py` đầy đủ CRUD (accounts, pairs, settings, symbol_config, orders)
- **Blocker**: không (CEO cần FTMO live account credentials cho Phase 3 — xem prerequisites checklist)

## Tóm tắt Phase 2

21 commit đã merge: 2.1, 2.1a, 2.1b, 2.2, 2.2a, 2.3, 2.4, 2.5, 2.6, 2.6a, 2.7, 2.7a, 2.7b, 2.7c, 2.7e, 2.8, 2.8a, 2.9, 2.9a, 2.9b, 2.10. Step 2.7d (RAF throttle) làm xong nhưng REJECT — không merge.
Tag dự kiến: `step-2.x` per step + `phase-2-complete` (CEO sẽ tag thủ công sau khi merge step 2.10).

**Server (Phase 2 add)**:
- cTrader Twisted bridge (`market_data.py`) + OAuth flow (`auth_ctrader.py`).
- Symbol sync from broker — batch fetch ~1s sau D-039 optimization, cache vào `symbol_config:{sym}` + `symbols:active` set.
- `GET /api/charts/{sym}/ohlc` + Redis 60s cache + `digits` field cho frontend Y-axis.
- WS `/ws?token=` + tick + candle broadcast + diff subscribe (set_symbol).
- Conversion rate via `tick:*` 60s cache (D-034 thay vì 24h `rate:*`) + `POST /symbols/{sym}/calculate-volume`.
- Pairs CRUD (GET/POST/PATCH/DELETE).
- 110 tests passing, mypy strict 22 source files (3 errors pre-existing về `hedger_shared` import path env-only).

**Web (Phase 2 add)**:
- Lightweight Charts v5.2.x (D-035), HedgeChart với historical OHLC + live tick price line bid/ask + live candle update từ tick stream (D-040, D-041).
- useWebSocket hook + auto-reconnect với exponential backoff + ping/pong + auto-resend set_symbol.
- ChartContextMenu right-click → set Entry/SL/TP với coordinateToPrice.
- HedgeOrderForm: PairPicker, Symbol read-only, Side BUY/SELL, Entry/SL/TP/Risk inputs với clear `×` button + validation warnings + manual volume override.
- VolumeCalculator debounced 300ms POST /calculate-volume + Vol P/S + SL pip + Est. SL $ + manual mode.
- `lib/orderValidation.ts` — side direction HARD BLOCK SL (D-045); `volumeReady` flag cho Phase 3 submit gate.
- Layout 2 column: chart+positions trái 70%, OrderForm full-height phải 30% (D-042 override D-029).
- Bundle: 435.31 kB JS / 140.48 kB gzip (Phase 1 cuối: 253.47 kB → +181 kB chủ yếu từ lightweight-charts + form UI).

**Infra (Phase 2)**:
- Vite dev proxy `/ws` với `ws: true` cho WebSocket forwarding.
- Backend WS auth fail trả HTTP 403 thay vì WS close 4401 (D-033).

## Active context (state runtime để smoke)
- Backend chạy ở `http://localhost:8000`.
- Frontend dev server ở `http://localhost:5173` với Vite proxy `/api` + `/ws` → backend.
- Default credentials: `admin` / `admin`.
- cTrader OAuth done qua `GET /api/auth/ctrader` (CEO setup 1 lần ở Phase 2).
- 117 symbol mapped (whitelist) → 91 trong số đó match cTrader broker (sau symbol sync).
- 110 server tests passing, mọi build check (typecheck/lint/format/build) đều green.

## Known issues — hoãn cho Phase 5 hardening

Backlog tổng từ Phase 1 + Phase 2 (chi tiết trong `PHASE_1_REPORT.md` + `PHASE_2_REPORT.md`):

- Toast notification thiếu cho session-expired + network-error (Phase 1).
- Telegram wrapper script `claude-with-notify.sh` bị TTY pipe phá vỡ (D-019). Workaround: `claude --dangerously-skip-permissions` direct.
- Helper `_bootstrap_cors_origins` ở module level — refactor sang FastAPI Depends (Phase 1).
- Drag price line cho setup lines + open order SL/TP (Phase 2 hoãn).
- WS auth fail trả 403 thay vì WS close 4401 (D-033) — revisit nếu reconnect logic cần.
- sync_symbols 24h Redis cache (đã ~1s sau D-039; cache sẽ làm instant nhưng không critical).
- BUY/SELL arrows trên setup lines (visual polish).
- Measure tool, multi-timeframe live trendbar per symbol, WS reference counting subscriptions.
- Multi-currency conversion rate edge cases — exotic currencies trả 0.0 → UI 503.
- Volume Secondary editable (Phase 2 chỉ Vol P; Vol S derived).
- Ratio per pair — Phase 2 hardcode 1.0; Phase 4 sẽ đọc từ PairPicker.

## Recent decisions (top 5, full list trong DECISIONS.md)
- D-045: Side direction HARD BLOCK SL violation; soft TP warning; `volumeReady` flag (step 2.9b).
- D-042: Layout left 70% (chart+positions) / right 30% OrderForm full-height — override D-029 (step 2.8a).
- D-041: WS candle_update KHÔNG redraw in-bar; tick stream là single source of truth (step 2.7e — root cause flicker fix).
- D-039: sync_symbols batch fetch — ~90s → ~1s (step 2.7b).
- D-032: cTrader gửi raw price uniformly scaled by 10^5; `digits` chỉ là display (step 2.2a).

## Pending items / TODO
- Phase 3.1: bắt đầu với `redis_service.py` đầy đủ CRUD. Server-side foundation cho trading API.
- CEO chuẩn bị FTMO live account credentials + cTrader OAuth credentials cho FTMO trước step 3.3.
- CEO add account FTMO qua Redis CLI sau khi step 3.2 ready (script helper sẽ ship).
- Phase 5: Telegram wrapper rewrite, drag price line, sync_symbols 24h cache, missing toasts, ratio per pair.

## Quick reference
- Repo: https://github.com/matran19900/ftmo_exness_hedge_v3
- Workspace: `/workspaces/ftmo_exness_hedge_v3`
- Redis: `redis://192.168.88.4:6379/2` (LAN)
- Symbol count: 117 mapped (whitelist) → 91 match cTrader broker.
- Test accounts:
  - cTrader **demo** (Phase 2 market data only, OAuth done).
  - FTMO live (CEO có account, dùng từ Phase 3+ — execute real orders).
  - Exness live (Phase 4+ — máy Windows cần thiết, MT5 lib chỉ chạy Windows).
- Python: 3.12, Node: 24 (devcontainer cài v22 nhưng thực tế thấy v24), npm: 11.12.1.
- Stack versions: React 19.2, Vite 8.0, TypeScript 6.0, Tailwind 3.4, Zustand 5.0, Axios 1.16, react-hot-toast 2.6, Lightweight Charts 5.2.
