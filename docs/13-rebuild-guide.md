# 13 — Rebuild Guide (Module-based Phase Plan)

## 1. Triết lý

CEO yêu cầu rebuild với chiến lược **module-based**: build từ dễ → khó, mỗi module **test độc lập** trước khi tích hợp.

**Khác với plan feature-based v1**: thay vì "phase 1 = login + OAuth, phase 2 = chart, ..." (cắt theo feature), v2 cắt theo **module logic** (M0 foundation, M1 server core, M2 frontend shell, ...).

**Lợi thế module-based**:
- Mỗi module có acceptance criteria rõ ràng → dễ test pass/fail.
- Frontend dev song song với backend (M2 dùng MSW mock cho đến khi M5b tích hợp).
- Decoupling: bug ở M3 không block M4.
- Phase plan rõ thứ tự dependency.

## 2. Module overview

| Module | Tên | Phụ thuộc | Mục tiêu | Test độc lập |
| --- | --- | --- | --- | --- |
| **M0** | Foundation | — | Repo + tooling + Redis schema setup | Smoke: chạy tests, lint, format |
| **M1** | Server Core | M0 | FastAPI + Redis service + auth + accounts/pairs CRUD + symbol whitelist load | curl tests + Redis state |
| **M2** | Frontend Shell | M0 | React + Zustand + layout + login + Settings UI (CRUD pairs/accounts) với MSW | Browser visual check + form submit |
| **M3** | FTMO Client | M0, M1 | Twisted client process độc lập + cmd/resp/event streams | Unit test + integration với cTrader demo |
| **M4** | Exness Client | M0, M1 | asyncio client + MT5 lib + position_monitor_loop | Unit test + integration với MT5 demo |
| **M5a** | Server Integration | M1, M3, M4 | Order service + response handler + position tracker + cTrader market-data | E2E hedge order flow |
| **M5b** | Frontend Integration | M2, M5a | Bỏ MSW, dùng real backend; chart + order form full flow | E2E browser test |
| **M6** | Hardening | M5a, M5b | Edge cases (G1-G15), timeout, retry, recovery, security | Test matrix R+G full pass |

## 3. Phase plan chi tiết

Mỗi step đi qua workflow 1-step-1-branch-1-commit (xem `WORKFLOW.md`).

### M0 — Foundation

**M0.1 Repo skeleton**
- Branch: `step/00-repo-skeleton`
- Tạo: `apps/server/`, `apps/client-ftmo/`, `apps/client-exness/`, `apps/web/`, `docs/`.
- Pyproject.toml mỗi app, ruff config.
- README root + link docs.
- `.gitignore`, `.env.example` mỗi app.
- `docs/symbol_mapping_ftmo_exness.json` template (5 entries: EURUSD, USDJPY, XAUUSD, NAS100, BTCUSD).

**M0.2 Docker compose dev**
- Branch: `step/01-docker-compose-dev`
- `docker-compose.dev.yml` chạy Redis 7.
- Mọi developer có thể `docker compose up -d` để có Redis local.

**M0.3 Lint + format CI**
- Branch: `step/02-lint-format`
- ruff configured cho cả 3 Python apps.
- Vite ESLint cho web.
- Pre-commit hooks `pre-commit install`.

**M0.4 Phase docs sync**
- Branch: `step/phase-0-docs-sync`
- Update `docs/RUNBOOK.md` đoạn dev setup nếu cần.

---

### M1 — Server Core

**M1.1 FastAPI scaffold + lifespan**
- Branch: `step/03-server-fastapi-scaffold`
- `app/main.py` lifespan rỗng + CORS + uvicorn run.
- `app/config.py` pydantic-settings.
- `app/redis_client.py` aioredis pool.
- `GET /health` returns `{"ok": true}`.

**M1.2 Auth (JWT + bcrypt)**
- Branch: `step/04-server-auth`
- `app/auth.py` JWT create/decode + bcrypt verify.
- `POST /auth/login` returns access_token.
- Dependencies: `get_current_user_rest`, `get_current_user_ws`.
- Test: curl wrong password → 401; correct → token; protected endpoint với token → 200.

**M1.3 Redis service skeleton**
- Branch: `step/05-server-redis-service`
- `app/services/redis_service.py` với CRUD methods cho accounts, pairs, settings, symbol_config.
- `setup_consumer_groups()` idempotent.
- Unit tests với fakeredis.

**M1.4 Accounts CRUD API**
- Branch: `step/06-server-accounts-api`
- `app/api/accounts.py`: GET / POST / DELETE / PATCH.
- Validation account_id format.
- Test: add 2 ftmo + 2 exness, list, delete.

**M1.5 Pairs CRUD API**
- Branch: `step/07-server-pairs-api`
- `app/api/pairs.py`: GET / POST / DELETE / PATCH.
- Validation pair links existing accounts.
- Reject delete khi có open orders (skip ở phase này, chỉ TODO).

**M1.6 Symbol whitelist load**
- Branch: `step/08-server-symbol-whitelist`
- `app/services/symbol_whitelist.py` load file + filter API.
- Lifespan startup load file.
- `GET /symbols` returns symbols từ whitelist (chưa có market-data sync, dùng static data từ file).

**M1.7 Settings API**
- Branch: `step/09-server-settings-api`
- `GET /settings`, `PATCH /settings`.

**M1.8 WebSocket skeleton + auth**
- Branch: `step/10-server-ws-skeleton`
- `/ws?token=` endpoint.
- Subscribe/unsubscribe channels in-memory.
- Server send ping mỗi 30s.

**M1.9 Phase docs sync**
- Branch: `step/phase-1-docs-sync`
- Update README.md với endpoints đã build.
- Cập nhật `RUNBOOK.md`.

---

### M2 — Frontend Shell

**M2.1 React + Vite + TS scaffold**
- Branch: `step/11-web-scaffold`
- Vite + TS strict + ESLint.
- App.tsx hello world.

**M2.2 Zustand store + persist**
- Branch: `step/12-web-zustand-store`
- Single store `src/store/index.ts`.
- Persist `token`, `selectedSymbol`, `selectedTimeframe`, `riskAmount`, `selectedPairId`.

**M2.3 MSW setup + handlers cho M1 endpoints**
- Branch: `step/13-web-msw-setup`
- Install MSW.
- Handlers cho `/auth/login`, `/symbols`, `/accounts`, `/pairs`, `/settings`, `/positions` (empty), `/orders` (empty).
- `VITE_MOCK=true` env enables MSW.

**M2.4 Login page**
- Branch: `step/14-web-login`
- Form username/password.
- Call `/auth/login` → save token store.
- Navigate `/`.

**M2.5 Layout (resizable panels)**
- Branch: `step/15-web-layout`
- Top bar (placeholder AccountStatus).
- 3 panels: chart left-top, order form right-top, position list bottom.
- 2 resizers (vertical, horizontal).

**M2.6 Toast container**
- Branch: `step/16-web-toast`
- Inline component, store `toasts: Toast[]`.
- 4 type info/success/warning/error, auto-dismiss 5s.

**M2.7 Settings modal + Pairs management**
- Branch: `step/17-web-settings-pairs`
- Modal trigger từ ⚙ icon.
- Tab "Pairs": list + add + delete + edit.
- API call qua MSW.

**M2.8 Settings modal + Accounts management**
- Branch: `step/18-web-settings-accounts`
- Tab "Accounts": list FTMO + Exness, add + delete.

**M2.9 Settings modal + General tab**
- Branch: `step/19-web-settings-general`
- Tab "General": default_secondary_ratio, primary_fill_timeout.

**M2.10 Phase docs sync**
- Branch: `step/phase-2-docs-sync`
- Screenshot UI + cập nhật `09-frontend.md` nếu khác plan.

---

### M3 — FTMO Trading Client

**M3.1 Skeleton + entry point**
- Branch: `step/20-ftmo-client-skeleton`
- `apps/client-ftmo/client/main.py` reactor.run() rỗng.
- Config loader + Redis blocking client connect.

**M3.2 cTrader adapter — connect + auth**
- Branch: `step/21-ftmo-adapter-auth`
- `CTraderAdapter.connect_and_auth()`: app_auth + account_auth.
- Log "connected: ctidTraderAccountId=X".

**M3.3 cTrader adapter — symbol lookup**
- Branch: `step/22-ftmo-adapter-symbols`
- Sync symbols list at startup, build `_symbol_id_by_name`.

**M3.4 cTrader adapter — place_market_order**
- Branch: `step/23-ftmo-adapter-place-market`
- Place market order, return Deferred fires with OrderResult.
- Test: place lệnh demo manual qua reactor.callLater.

**M3.5 cTrader adapter — close_position**
- Branch: `step/24-ftmo-adapter-close`
- Close position by positionId.

**M3.6 cTrader adapter — modify_sl_tp**
- Branch: `step/25-ftmo-adapter-modify`

**M3.7 Heartbeat loop**
- Branch: `step/26-ftmo-heartbeat`
- HSET client:ftmo:{acc} every 10s, EXPIRE 30.

**M3.8 Account sync loop**
- Branch: `step/27-ftmo-account-sync`
- HSET account:ftmo:{acc} balance/equity/margin every 30s.

**M3.9 Command dispatcher (XREADGROUP loop)**
- Branch: `step/28-ftmo-dispatcher`
- Reactor.callLater poll, dispatch action open/close/modify_sl_tp.
- Wire to ResponsePublisher.

**M3.10 Response publisher + unsolicited event publisher**
- Branch: `step/29-ftmo-publisher`
- XADD resp_stream + event_stream.
- on_unsolicited_event publishes position_closed event.

**M3.11 Phase docs sync**
- Branch: `step/phase-3-docs-sync`
- Verify `03-ftmo-client.md` matches implementation.

---

### M4 — Exness Trading Client

**M4.1 Skeleton + asyncio main**
- Branch: `step/30-exness-client-skeleton`
- `client/main.py` asyncio.run + redis aioredis.

**M4.2 MT5 adapter — connect**
- Branch: `step/31-exness-adapter-connect`
- mt5.initialize + mt5.login wrapped in executor.

**M4.3 MT5 adapter — place_market_order (IOC)**
- Branch: `step/32-exness-adapter-place-market`
- order_send với ORDER_FILLING_IOC.
- normalize_volume.

**M4.4 MT5 adapter — close_position**
- Branch: `step/33-exness-adapter-close`

**M4.5 MT5 adapter — get_account_info**
- Branch: `step/34-exness-adapter-account`

**M4.6 Heartbeat + AccountSync**
- Branch: `step/35-exness-heartbeat-account`

**M4.7 Command dispatcher (asyncio loop)**
- Branch: `step/36-exness-dispatcher`

**M4.8 Position monitor loop (NEW v2)**
- Branch: `step/37-exness-position-monitor`
- Poll `mt5.positions_get` every 2s.
- Detect closures → publish `position_closed_external` event.

**M4.9 Response publisher**
- Branch: `step/38-exness-publisher`

**M4.10 Phase docs sync**
- Branch: `step/phase-4-docs-sync`

---

### M5a — Server Integration

**M5a.1 Market-data cTrader connection (Twisted-asyncio bridge)**
- Branch: `step/39-server-market-data`
- `app/services/market_data.py`.
- OAuth flow `/auth/ctrader*` endpoints.
- start() spawn Twisted thread, app_auth + account_auth, sync symbols (filter qua whitelist).
- subscribe_spots → SETEX tick:{sym} TTL 5s + WS broadcast.

**M5a.2 GET /symbols/{}/tick + GET /charts/{}/ohlc**
- Branch: `step/40-server-tick-ohlc`
- Tick reads từ Redis cache.
- OHLC: market_data.get_trendbars + cache `ohlc:{}:{}` TTL 60s.

**M5a.3 calculate_volume helper + POST /symbols/{}/calculate-volume**
- Branch: `step/41-server-calc-volume`
- R6, R7 implementation.

**M5a.4 Order service — create_hedge_order**
- Branch: `step/42-server-order-create`
- Validate, push command FTMO, ZADD pending.

**M5a.5 Response reader loop**
- Branch: `step/43-server-response-reader`
- XREADGROUP all resp_stream:* + event_stream:*.
- handle_response cho action open (primary fill → push secondary).

**M5a.6 handle_response cho action close + cascade trigger**
- Branch: `step/44-server-cascade`
- Primary close response → cascade close secondary.
- Secondary close response → finalize order.

**M5a.7 handle_event (unsolicited close)**
- Branch: `step/45-server-event-handler`
- FTMO position_closed → cascade secondary.
- Exness position_closed_external → cascade primary.

**M5a.8 Position tracker loop**
- Branch: `step/46-server-position-tracker`
- 1s loop, calc P&L USD, SETEX position:{id}, broadcast WS.

**M5a.9 Timeout checker loop**
- Branch: `step/47-server-timeout-checker`

**M5a.10 PATCH /orders/{}/sl-tp**
- Branch: `step/48-server-modify-sltp`

**M5a.11 DELETE /orders/{}**
- Branch: `step/49-server-close-order`

**M5a.12 GET /orders + /orders/{}**
- Branch: `step/50-server-orders-list`

**M5a.13 GET /positions**
- Branch: `step/51-server-positions`

**M5a.14 Phase docs sync**
- Branch: `step/phase-5a-docs-sync`

---

### M5b — Frontend Integration (real backend)

**M5b.1 Bỏ MSW, axios real**
- Branch: `step/52-web-real-backend`
- VITE_MOCK=false.
- API client trỏ thật.

**M5b.2 Chart component (Lightweight Charts)**
- Branch: `step/53-web-chart`
- HedgeChart fetch /charts + WS subscribe ticks/candles.
- Render historical candles + live trendbar update + tick price line (ask/bid).
- Setup lines (form draft Entry/SL/TP) — dashed, light color.
- Right-click context menu → set Entry/SL/TP từ Y coordinate.
- Order overlay: vẽ Entry/SL/TP cho leg primary của order match `selectedSymbol + selectedPairId`. 3 trạng thái style (setup/pending/open). Quản lý price line keyed by `order_id` để tránh full redraw (R31, R32, R33).

> **Note**: Trong MASTER_PLAN_v2 (vertical slice 5 phase), các tính năng chart trên sẽ được **tách thành nhiều sub-step nhỏ** để verify độc lập:
> - `chart-base`: render historical + live candle + tick price line.
> - `chart-setup-lines`: setup line từ form draft.
> - `chart-right-click`: right-click context menu set Entry/SL/TP.
> - `chart-order-overlay`: vẽ open/pending order lines (filter symbol+pair, primary only).
> - `chart-drag-sltp`: drag SL/TP của open order → PATCH API.

**M5b.3 Symbol picker**
- Branch: `step/54-web-symbol-picker`
- Dropdown từ /symbols, store.selectedSymbol.

**M5b.4 PairPicker**
- Branch: `step/55-web-pair-picker`
- Dropdown từ /pairs với status dots.

**M5b.5 HedgeOrderForm — market**
- Branch: `step/56-web-order-form-market`
- Tabs market/limit/stop, BUY/SELL, SL/TP/risk inputs.
- Validate R13, R16.
- Submit POST /orders/hedge.

**M5b.6 VolumeCalculator + ConversionRateInfo**
- Branch: `step/57-web-volume-calc`
- POST /symbols/{}/calculate-volume on input change.

**M5b.7 Position list — Open tab**
- Branch: `step/58-web-position-open`
- Subscribe positions channel, render keyed by order_id.

**M5b.8 Position list — History tab**
- Branch: `step/59-web-position-history`
- Fetch /orders?status=closed, paginate.

**M5b.9 Close button + cascade visual**
- Branch: `step/60-web-close-action`

**M5b.10 SL/TP drag on chart + edit modal**
- Branch: `step/61-web-sltp-edit`
- ChartContextMenu + drag price line → PATCH /orders/{}/sl-tp.

**M5b.11 AccountStatus bar**
- Branch: `step/62-web-account-status`
- Top bar all FTMO + Exness with balance/equity + status dot.

**M5b.12 Phase docs sync**
- Branch: `step/phase-5b-docs-sync`

---

### M6 — Hardening

**M6.1 G1 secondary_failed retry logic**
- Branch: `step/63-harden-g1-retry`
- Retry 0.5/1/2s khi secondary open fail.

**M6.2 G5 cascade close retry**
- Branch: `step/64-harden-g5-cascade-retry`

**M6.3 G6 PATCH SL/TP race guard**
- Branch: `step/65-harden-g6-sltp-race`

**M6.4 G10 Server restart recovery script**
- Branch: `step/66-harden-g10-recovery-script`

**M6.5 G15 Market-data reconnect logic**
- Branch: `step/67-harden-g15-market-data-reconnect`

**M6.6 Backup script + cron**
- Branch: `step/68-harden-backup-script`

**M6.7 Smoke test runbook (`docs/RUNBOOK.md`)**
- Branch: `step/69-harden-runbook`
- Procedure: setup new pair, check health, manual recovery.

**M6.8 Phase docs sync (final)**
- Branch: `step/phase-6-docs-sync`

---

## 4. Estimate

| Module | Steps | Est days (1 dev) |
| --- | --- | --- |
| M0 | 4 | 1 |
| M1 | 9 | 4 |
| M2 | 10 | 5 |
| M3 | 11 | 6 |
| M4 | 10 | 5 |
| M5a | 14 | 7 |
| M5b | 12 | 6 |
| M6 | 8 | 4 |
| **Total** | **78** | **~38 days** |

> 38 ngày = 1 dev fulltime. CTO + Claude Code cùng làm thì ~25 ngày.

## 5. Critical path & parallelism

```
M0 → M1 ──→ M3 ──┐
       └──→ M4 ──┴──→ M5a ──→ M6
       └──→ M2 ──────→ M5b ──┘
```

**Có thể chạy song song**:
- M2 (frontend shell với MSW) song song M3+M4 (clients).
- M3 và M4 song song.

CEO + Claude Code có thể split: 1 instance làm M3, 1 instance làm M4 → tiết kiệm ~5 ngày.

## 6. Acceptance criteria mỗi module (gate review)

### M0 — Pass khi
- `docker compose up -d` chạy → Redis ping OK.
- `ruff check apps/` không lỗi.
- README đọc được, link docs đúng.

### M1 — Pass khi
- Login → token, GET /accounts với token → 200.
- Add account, add pair, list verify Redis state.
- GET /symbols trả 5 entries từ whitelist.
- WS connect, subscribe `agents` → nhận ping every 30s.

### M2 — Pass khi (MSW mode)
- Login form → token saved.
- Layout 3 panel + resize OK.
- Settings modal: add/delete pair, account verify localStorage.
- Toast hiện đúng 4 type.

### M3 — Pass khi
- Connect cTrader demo account → log "connected".
- redis-cli XADD command → place lệnh demo thành công, response trong resp_stream.
- Heartbeat key tồn tại, expire khi process kill.
- Manual close trên cTrader UI → event_stream có position_closed.

### M4 — Pass khi
- Connect MT5 demo → log "connected".
- redis-cli XADD command → MT5 terminal hiển thị position mới.
- position_monitor_loop detect manual close → event_stream có position_closed_external.

### M5a — Pass khi
- POST /orders/hedge với pair_id → 2 leg fill < 5s.
- P&L USD update mỗi 1s.
- Click X close → cascade close.
- Test JPY pair → P&L USD đúng (subscribe USDJPY conversion).

### M5b — Pass khi
- Browser end-to-end: login → chọn pair → đặt market BUY EURUSD → thấy 2 leg fill, P&L tăng/giảm.
- History tab show order đã close.
- Drag SL line → backend update.

### M6 — Pass khi
- Smoke test full checklist `10-flows.md` section 12 pass.
- Test matrix R+G section 11 `12-business-rules.md` pass.

## 7. Khi nào declare "production ready"

**Tất cả** điều kiện:
1. Smoke test full pass.
2. CEO đặt 5 lệnh thật trên FTMO live + Exness live, mỗi asset class 1 lệnh, không có bug.
3. Run hệ thống 1 tuần liên tục, không có incident lớn.
4. Backup script test recovery thành công.
5. CEO confirm OK.
