# MASTER_PLAN_v2 — FTMO Hedge Tool

## 0. Bối cảnh & nguyên tắc

Plan này thay thế `13-rebuild-guide.md` (M0–M6 cũ). Cấu trúc theo **vertical slice 5 phase**, mỗi phase có deliverable CEO bật lên xem/test được ngay, không phải đợi tích hợp ở phase cuối.

### Nguyên tắc bất biến
- Mỗi step = 1 branch + 1 commit (squash khi merge `main`).
- Step REJECT → xóa branch, làm lại; **không patch chắp vá**.
- Claude Code chỉ commit trong branch step, **không push, không merge**.
- Branch naming: `step/<phase>.<num>-<slug>`. Ví dụ: `step/1.3-server-fastapi-scaffold`.
- CEO/CTO confirm PASS → user merge squash → tag `step-<phase>.<num>` → xóa branch.
- Mỗi phase kết thúc bằng 1 step `phase-<n>-docs-sync` để cập nhật docs + tạo report (xem Section 7).
- **Language convention**:
  - CEO ↔ CTO chat: **Tiếng Việt**.
  - CTO → Claude Code (prompt): **English** (token efficiency + better instruction following).
  - Claude Code → CTO (self-check report): **English**.
  - Code comments + commit messages: **English**.
  - Documentation files trong `docs/`: Tiếng Việt (knowledge cho CEO + CTO onboarding).
  - Telegram notify: **English** (concise).
- **Prompt format rule**: CTO viết toàn bộ prompt cho Claude Code trong **1 code block duy nhất** để CEO copy bằng 1 nút bấm. KHÔNG tách prompt thành nhiều section bằng heading markdown ngoài code block. Mọi section, bullet, table của prompt nằm bên trong cùng 1 fenced code block.

### Quyết định kiến trúc đã chốt (reference)
- Symbol mapping: file JSON cứng, load lúc startup.
- Cascade trigger: cTrader execution event (FTMO) + MT5 poll (Exness).
- Risk ratio: số global per pair.
- Conversion rate cache: Redis TTL 24h.
- Deploy target: Windows Server 2022.
- Redis: cùng máy server (mọi phase).
- Network: Tailscale VPN giữa server + clients (Phase 5 setup).
- Auth client-server: server-driven (account_id add ở UI trước, client deploy sau).
- Test môi trường: FTMO live (CEO đã có account).
- **Dev environment**: Linux devcontainer cho Phase 1-3. **Phase 4 trở đi cần máy Windows** cho Exness client (CEO đã có sẵn) — vì MT5 Python lib chỉ chạy Windows.
- **Git hooks**: viết bash duy nhất. Trên Windows dùng Git Bash bundled với Git for Windows (CEO cài Git for Windows là OK).
- **Telegram notify**: 3 trigger (commit done / need approve / inactivity stuck) qua wrapper script `claude-with-notify.sh`.
- **CTO continuity strategy**: documentation phải đủ để CTO chat instance mới onboard chỉ qua 3-4 file (MASTER_PLAN_v2.md + PROJECT_STATE.md + DECISIONS.md + PHASE_N_REPORT.md). Mỗi step PASS phải update PROJECT_STATE.md.

### 3 mục tiêu cốt lõi (kim chỉ nam mọi review)
1. **Sync 2 lệnh nhanh**: FTMO đặt trước → Exness đặt ngay sau, ratio chuẩn, ngược chiều.
2. **Cascade close**: 1 leg đóng → leg kia đóng theo, không để leg hở.
3. **P&L USD real-time**: đúng cho mọi asset class (forex/JPY/indices/crypto/commodities).

### Non-goals (REJECT nếu Claude Code làm)
- Auto-trading / strategy engine.
- Multi-user, multi-tenant.
- OCO, trailing stop, partial close.
- Broker khác cTrader & MT5.

---

## 1. Tổng quan 5 phase

| Phase | Tên | Step | Deliverable visual |
|---|---|---|---|
| 1 | Foundation | 8 | Login UI lên + 3 panel trống + Telegram notify |
| 2 | Market Data + Chart + Form | 10 | Chart live + form + volume calc + right-click |
| 3 | Single-leg Trading (FTMO only) | 14 | Đặt/đóng/SL-TP lệnh FTMO real, P&L USD real-time |
| 4 | Hedge + Cascade Close | 11 | 2 leg sync, cascade close, AccountStatus bar |
| 5 | Hardening + Deploy | 10 | Tool chạy production trên Windows Server 2022 |
| | **Total** | **53** | |

---

## 2. PHASE 1 — Foundation

### Mục tiêu
Setup repo + skeleton 3 service (server, web, redis) chạy được local, CEO login UI và thấy layout 3 panel. Setup Telegram notify để CEO biết khi Claude Code commit xong.

### Prerequisites
- CEO có Telegram bot token + chat_id (tạo qua @BotFather).

### Deliverable visual cho CEO
- `docker-compose up` lên 3 service.
- Mở `http://localhost:5173` → trang Login.
- Login `admin / admin` → JWT → trang chính với layout 3 panel **trống**.
- `curl http://localhost:8000/api/health` → `{"ok": true}`.
- `curl http://localhost:8000/api/symbols` (với JWT) → list symbol từ file JSON whitelist.
- **Telegram nhận 3 loại notify đúng trigger**:
  - 🔧 commit xong (mỗi khi Claude Code commit trong branch step).
  - ⚠️ cần approve manual (khi stdout match pattern).
  - 💤 có thể stuck (khi không có output >90s).
- README có hướng dẫn dùng `claude-with-notify.sh` wrapper.

### Scope IN
**Repo & infra:**
- Monorepo skeleton: `apps/server/`, `apps/web/`, `apps/ftmo-client/`, `apps/exness-client/`, `docs/`, `scripts/`.
- `docker-compose.yml` với 3 service: redis, server, web.
- Devcontainer config.
- Lint + format: `ruff` cho Python, `eslint` cho web, pre-commit hooks.

**Server (FastAPI):**
- `app/main.py` lifespan + CORS + uvicorn.
- `app/config.py` pydantic-settings từ env.
- `app/redis_client.py` aioredis pool.
- `GET /api/health`.
- JWT auth: `app/auth.py` create/decode + bcrypt.
- `POST /api/auth/login` (dummy `admin / admin`).
- Dependency `get_current_user_rest`, `get_current_user_ws`.
- Symbol whitelist loader: `app/services/symbol_whitelist.py` đọc file JSON.
- `GET /api/symbols` (auth required) trả symbols từ whitelist.

**Web (Vite + React + TS):**
- Vite scaffold + TS strict + ESLint.
- Zustand store `src/store/index.ts` với persist.
- Axios client với JWT interceptor.
- Login page → call `/auth/login` → save token → redirect `/`.
- Layout `App.tsx` 3 panel skeleton placeholder.

**Telegram notify:**
- `scripts/notify_telegram.sh` (reuse từ project files).
- `scripts/claude-with-notify.sh` wrapper script: bọc lệnh `claude --dangerously-skip-permissions` + monitor stdout pattern (need approve) + watchdog inactivity 90s.
- `.git/hooks/post-commit` template (bash) install qua bootstrap script: detect branch `step/*` → gọi notify (commit done).
- 3 trigger: commit done / need approve (stdout regex match) / inactivity 90s (watchdog timer).
- Throttle: max 1 notify / loại / 3 phút (ghi state vào tmp file).
- Bot token + chat_id qua env file (gitignored).
- README document cách dùng wrapper + lưu full code bash script để CEO reference.

### Scope OUT
- Market data subscribe.
- Chart rendering.
- Form fields functional.
- Trading clients.
- Settings UI.
- WS data flow.

### Acceptance test
1. Clone repo + `docker-compose up` → 3 container UP.
2. `curl /api/health` → 200.
3. `curl /api/symbols` → 401; với token → 200 list symbol.
4. Login UI flow OK, refresh page vẫn login (token persist).
5. `redis-cli ping` → PONG.
6. **Telegram trigger 1 (commit done)**: tạo branch `step/test-notify`, commit dummy → Telegram nhận message format `🔧 [STEP test-notify] commit xong` trong vòng 5s.
7. **Telegram trigger 2 (need approve)**: chạy Claude Code qua wrapper `claude-with-notify.sh`, mock 1 stdout line chứa "Allow Claude to ... (Y/n)" → Telegram nhận `⚠️ [CLAUDE CODE] Cần approve manual`.
8. **Telegram trigger 3 (inactivity)**: chạy wrapper, không có output stdout >90s → Telegram nhận `💤 [CLAUDE CODE] Có thể stuck`.
9. Throttle: 3 trigger cùng loại trong 3 phút → chỉ nhận 1 notify.
10. Pre-commit hook chạy lint/format khi commit.
11. README có section hướng dẫn dùng `claude-with-notify.sh` thay vì lệnh `claude` trực tiếp + lưu code bash wrapper để CEO reference.
12. `docs/PROJECT_STATE.md` tồn tại với initial state Phase 1 complete.
13. `docs/DECISIONS.md` tồn tại với initial decisions từ các thảo luận pre-Phase 1.
14. `docs/CTO_HANDOFF_TEMPLATE.md` tồn tại để CEO copy khi tạo CTO chat mới.

### Step breakdown

> Thứ tự step phản ánh thực tế đã thực hiện. Telegram setup được chuyển lên sớm hơn theo yêu cầu CEO. Chi tiết deviation xem `PHASE_1_REPORT.md`.

| # | Branch | Scope (1-line summary) |
|---|---|---|
| 1.1 | `step/1.1-repo-and-lint` | Monorepo skeleton + lint + devcontainer (docker-compose bỏ — D-006). |
| 1.2 | `step/1.2-server-fastapi-scaffold` | FastAPI + symbols whitelist + 2 endpoint (`/health`, `/symbols/`). |
| 1.3 | `step/1.3-telegram-notify-setup` | Telegram wrapper + post-commit hook (REORDERED — gốc là 1.7). Wrapper bị bypass trong thực tế (D-019). |
| 1.4 | `step/1.4-server-auth-jwt` | JWT auth + protect symbols endpoints. |
| 1.4a | `step/1.4a-config-fixes` | Sub-fix: CORS env parsing + `.env` path resolution. |
| 1.5 | `step/1.5-web-scaffold-store-axios` | Vite + React 19 + TS strict + Tailwind v3 + Zustand + Axios. |
| 1.6 | `step/1.6-web-login-page` | Login form + auth flow + react-hot-toast. |
| 1.7 | `step/1.7-web-layout-skeleton` | 3-panel layout + Open/History tabs (PositionList full-width — D-029). |
| 1.8 | `step/1.8-phase-1-docs-sync` | Phase 1 docs: DECISIONS, PROJECT_STATE, CTO_HANDOFF_TEMPLATE, PHASE_1_REPORT, RUNBOOK skeleton + cập nhật MASTER_PLAN_v2 + README. Tag `phase-1-complete` (CEO tag thủ công sau khi merge). |

---

## 3. PHASE 2 — Market Data + Chart + Form (Read-only)

### Mục tiêu
Build full UI **không cần trading client**. CEO bật lên thấy chart live + form đầy đủ + volume calc real-time. Submit form chưa làm gì (toast "Phase 3").

### Prerequisites
- Phase 1 PASS.
- CEO có cTrader **demo account** + OAuth credentials (client_id, client_secret).

### Deliverable visual cho CEO
- Chọn symbol → chart hiện historical candles + tick chạy + candle update real-time.
- Form đầy đủ: pair picker, symbol, BUY/SELL, Order type (Market/Limit/Stop), Entry/SL/TP, Risk amount.
- Setup lines (Entry/SL/TP từ form) hiện trên chart, dashed light.
- Right-click chart → menu set Entry/SL/TP từ giá Y → form update.
- Drag setup line trên chart → form update.
- Volume calculator real-time hiển thị `volume_p` + `volume_s` + conversion rate USD.
- Submit form → toast "Phase 3 sẽ implement", không gọi API thật.

### Scope IN
**Server:**
- Market-data cTrader service (Twisted bridge), connect cTrader demo, OAuth flow endpoints.
- Symbol sync từ cTrader → filter whitelist → cache Redis.
- `GET /api/charts/{symbol}/ohlc?timeframe=&count=` + cache Redis 60s.
- WS endpoint + channel `ticks:{symbol}`, `candles:{symbol}:{tf}` + diff subscribe khi đổi symbol.
- Conversion rate service + cache 24h + auto-subscribe khi miss.
- `POST /api/symbols/{symbol}/calculate-volume` + dùng rate-cache + symbol_config.
- Pairs CRUD basic (`GET/POST /api/pairs`).

**Frontend:**
- HedgeChart: useChart hook (historical + live candle + tick price line ask/bid).
- Setup lines từ store + ChartContextMenu (right-click) + drag setup line.
- HedgeOrderForm full UI: PairPicker, symbol, order type tabs, BUY/SELL, Entry/SL/TP/Risk inputs.
- VolumeCalculator + ConversionRateInfo + debounced API call.
- useWebSocket hook + subscribe ticks/candles reactive với `selectedSymbol`.
- MSW dev fixtures.

### Scope OUT
- Trading clients (FTMO/Exness).
- PositionList với data thật.
- Order overlay (chưa có order).
- Drag SL/TP của open order.
- Submit form gọi API trading.
- AccountStatus bar.

### Acceptance test
1. Server start → cTrader connect OK.
2. Frontend: chọn EURUSD → chart historical 200 nến + tick chạy + candle update.
3. Đổi USDJPY → digits change qua applyOptions, không recreate chart.
4. Right-click chart → "Set as Entry" → form Entry update + dashed line vẽ.
5. Drag dashed Entry line → form Entry update theo.
6. Volume calc: Risk 100, Entry 1.08000, SL 1.07900, BUY EURUSD → volume đúng (compare manual).
7. Test USDJPY (JPY conversion) + XAUUSD (commodity contract size).
8. Conversion rate cache: lần đầu chậm, lần 2 trong 24h <50ms.
9. Submit form → toast "Phase 3", form không clear.
10. Đổi symbol → WS subscribe diff đúng (check DevTools).

### Step breakdown
| # | Branch | Scope |
|---|---|---|
| 2.1 | `step/2.1-server-ctrader-bridge-and-symbols` | Twisted bridge + OAuth flow + connect cTrader demo + symbol sync filter whitelist cache Redis + update `GET /symbols` lấy từ cache. |
| 2.2 | `step/2.2-server-charts-ohlc` | `GET /charts/:symbol/ohlc` + cache 60s. |
| 2.3 | `step/2.3-server-ws-ticks-candles` | WS endpoint + channel subscribe + diff sub + broadcast. |
| 2.4 | `step/2.4-server-rate-cache-and-volume-calc` | Conversion rate cache 24h + auto-subscribe miss + `POST /symbols/:symbol/calculate-volume`. |
| 2.5 | `step/2.5-server-pairs-api-basic` | CRUD pairs basic (GET/POST). |
| 2.6 | `step/2.6-web-chart-base` | HedgeChart + useChart hook (historical + live candle + tick price line). |
| 2.7 | `step/2.7-web-chart-interactions` | Setup lines (form draft) + ChartContextMenu right-click + drag setup line. |
| 2.8 | `step/2.8-web-order-form-ui` | HedgeOrderForm full UI (pair, symbol, order type, BUY/SELL, Entry/SL/TP/Risk). |
| 2.9 | `step/2.9-web-volume-calc-and-ws` | VolumeCalculator + ConversionRateInfo + debounced API + useWebSocket hook reactive với selectedSymbol. |
| 2.10 | `step/2.10-phase-2-docs-sync` | Update docs + `PHASE_2_REPORT.md` + append `DECISIONS.md`. Tag `phase-2-complete`. |

---

## 4. PHASE 3 — Single-leg Trading (FTMO only)

### Mục tiêu
Build trading pipeline end-to-end cho **1 leg FTMO**. Verify P&L USD chính xác. Phase 4 sẽ extend Exness, không refactor Phase 3.

### Prerequisites
- Phase 2 PASS.
- CEO có FTMO live account + cTrader credentials.
- CEO setup `.env` cho FTMO client.
- CEO add account FTMO qua Redis CLI (script helper):
  ```
  redis-cli HSET account:ftmo:ftmo_acc_001 type ftmo broker ftmo currency USD
  redis-cli SADD accounts:ftmo ftmo_acc_001
  ```
- Server restart sau khi add account → tạo consumer groups.

### Deliverable visual cho CEO
- FTMO client process start → connect cTrader → heartbeat lên Redis.
- Submit form → server push command → FTMO client execute → lệnh real lên broker.
- Position list "Open" tab hiện lệnh real-time với P&L USD update mỗi 1s.
- Click × close → lệnh đóng → row chuyển History.
- Order overlay vẽ trên chart: Entry/SL/TP open order primary, solid dark.
- Drag SL/TP open order → cTrader broker update SL/TP thật.
- Manual close trên cTrader → server detect event → frontend update.
- Lifecycle toasts: `primary_filled`, `order_closed`, `sl_tp_updated`.
- Order types Market/Limit/Stop đều work.

### Scope IN
**FTMO client process:**
- Main loop: connect Redis + heartbeat + cTrader OAuth + `XREADGROUP cmd_stream:ftmo:{acc}`.
- Action handlers: `open` (market/limit/stop), `close`, `modify_sl_tp` → response qua `resp_stream`.
- Execution event subscribe → unsolicited close → `event_stream`.
- Account info publish: `HSET account:ftmo:{acc}` mỗi N giây.
- Error code mapping (R-IDM-5).
- XACK idempotent với request_id.

**Server:**
- Redis service đầy đủ CRUD (accounts, pairs, settings, symbol_config, orders).
- Lifespan `setup_consumer_groups()` idempotent.
- `order_service.create_order` (single-leg, secondary disabled).
- Background tasks: response_handler + event_handler `XREADGROUP`.
- `position_tracker_loop` 1s: tính P&L USD + broadcast WS `positions:update`.
- API: `POST /orders`, `DELETE /orders/{id}`, `PATCH /orders/{id}/sl-tp`, `GET /positions`, `GET /positions/history`.
- WS broadcast: `order_created`, `primary_filled`, `order_closed`, `sl_tp_updated`, `positions:update`.
- Reject lệnh khi client offline.

**Frontend:**
- HedgeOrderForm submit → `POST /api/orders` real + lifecycle toasts.
- PositionList Open tab + History tab + close button + confirm modal + WS reactive.
- `useChartOrderOverlay`: filter R46/R47 + 3 trạng thái style R48 + map keyed `order_id`.
- Drag SL/TP open order → PATCH + revert nếu reject.

### Scope OUT
- Exness client.
- Hedge order 2 leg.
- Cascade close.
- AccountStatus bar.
- Settings UI accounts/pairs.
- Multi-account FTMO.

### Acceptance test
**Pipeline:**
1. FTMO client heartbeat OK.
2. Server restart → consumer groups created idempotent.
3. Submit BUY EURUSD market 0.01 SL/TP → cTrader broker thấy <2s.
4. Open tab + P&L USD update mỗi 1s, sai số <1% so cTrader.

**Order types:**
5. Limit/Stop pending → khi giá hit → fill → toast `primary_filled`.

**Edge case symbols:**
6. USDJPY (JPY conversion), XAUUSD (commodity), US30/NAS100 (index): P&L đúng.

**SL/TP modify:**
7. Drag SL → cTrader update <2s.
8. Drag SL invalid → server reject → revert.

**Close:**
9. UI close → cTrader đóng → row History.
10. Manual close cTrader → server detect <5s → frontend update.
11. SL hit → auto-close → frontend update.

**Robustness:**
12. Stop client → submit lệnh → 503 "ftmo offline".
13. Restart client với pending command → resume PEL.
14. Server crash với open order → restart → P&L resume.

### Step breakdown
| # | Branch | Scope |
|---|---|---|
| 3.1 | `step/3.1-server-redis-service-full` | `redis_service.py` đầy đủ CRUD. |
| 3.2 | `step/3.2-server-consumer-groups-setup` | Lifespan setup + init script add account vào Redis. |
| 3.3 | `step/3.3-ftmo-client-skeleton` | Connect Redis + heartbeat + cTrader OAuth + `XREADGROUP` loop (chưa execute). |
| 3.4 | `step/3.4-ftmo-client-actions` | Handlers `open` market/limit/stop + `close` + `modify_sl_tp` + response + retcode mapping. |
| 3.5 | `step/3.5-ftmo-client-events-and-account` | Execution event subscribe → event_stream + account info publish. |
| 3.6 | `step/3.6-server-orders-api` | `POST /orders` (single-leg) + `DELETE /orders/{id}` + `PATCH /orders/{id}/sl-tp`. |
| 3.7 | `step/3.7-server-resp-and-event-handlers` | Background `XREADGROUP resp_stream:*` + `event_stream:*` → update order, broadcast WS. |
| 3.8 | `step/3.8-server-position-tracker` | Loop 1s tính P&L USD + broadcast WS `positions:update`. |
| 3.9 | `step/3.9-server-positions-api` | `GET /positions` + `GET /positions/history`. |
| 3.10 | `step/3.10-web-position-list` | PositionList Open + History tab + close button + confirm modal + WS reactive. |
| 3.11 | `step/3.11-web-order-form-submit` | Submit `POST /orders` real + lifecycle toasts. |
| 3.12 | `step/3.12-web-chart-order-overlay` | `useChartOrderOverlay` + R46/R47 filter + R48 3 trạng thái + map keyed order_id. |
| 3.13 | `step/3.13-web-chart-drag-sltp` | Drag SL/TP open order → PATCH + revert on reject. |
| 3.14 | `step/3.14-phase-3-docs-sync` | Update docs + `PHASE_3_REPORT.md` + append `DECISIONS.md`. Tag `phase-3-complete`. |

---

## 5. PHASE 4 — Hedge + Cascade Close

### Mục tiêu
Add Exness leg + cascade close. Verify mục tiêu cốt lõi #1 (sync) và #2 (cascade close) end-to-end với 1 pair.

### Prerequisites
- Phase 3 PASS.
- CEO có Exness live account + MT5 credentials.
- **CEO dùng máy Windows local đã có sẵn** cho dev + test Exness client + run MT5 terminal. **Lý do**: MT5 Python lib (`MetaTrader5` package) chỉ chạy Windows, không có Linux build → devcontainer Linux KHÔNG chạy được Exness client.
- **Workflow Phase 4**:
  - Code Exness client viết được trên Linux devcontainer (Python source cross-platform OK).
  - Nhưng test/run/debug Exness client phải pull branch về máy Windows.
  - Server + Web + FTMO client vẫn dev trên Linux devcontainer như Phase 1-3.
- Symbol mapping JSON có entry cho symbol pair test (vd EURUSD ↔ EURUSDm).

### Deliverable visual cho CEO
- Settings UI: 3 tab Accounts / Pairs / General.
- Add 1 FTMO + 1 Exness + 1 pair link 2 cái qua UI.
- AccountStatus bar top: 2 account với balance/equity + status dot.
- Submit form → FTMO trước, fill xong → push Exness → 2 leg cùng open.
- Position list 1 hedge order với 2 leg, P&L riêng + total.
- Close FTMO → Exness cascade <3s.
- Close Exness manual → FTMO cascade <5s (poll 2s).
- Toasts: `hedge_open`, `cascade_triggered`, `hedge_closed`.

### Scope IN
**Exness client process:**
- Main loop: Redis + heartbeat + MT5 connect + `XREADGROUP cmd_stream:exness:{acc}`.
- Action handlers: `open` market only + `close` + response.
- **Position monitor loop** poll 2s + diff snapshot → publish `event_stream` nếu position closed.
- Account info publish + retcode mapping MT5 + volume normalize.

**Server:**
- `order_service.create_hedge_order` full flow: primary → wait fill → tính secondary volume → push secondary → retry 0.5/1/2s nếu fail (max 3 lần).
- **Cascade close logic**: response_handler + event_handler trigger close leg còn lại + idempotent flag.
- Settings API CRUD: accounts (FTMO + Exness), pairs (link + risk_ratio).
- Consumer groups runtime tạo khi add account (không restart).
- WS events: `secondary_pending`, `secondary_filled`, `secondary_failed`, `cascade_triggered`, `hedge_closed`.

**Frontend:**
- Settings modal 3 tab.
- AccountStatus bar component + heartbeat WS.
- PositionList row hedge: 2 leg P&L + total + status indicator.
- Order form validate cả 2 client online trước submit.
- Lifecycle toasts mới.

### Scope OUT
- Multi-account (Phase 5).
- Limit/Stop Exness (Phase 5 nếu cần).
- Edge case G1 retry sophisticated (Phase 5).
- Slippage warning UI (Phase 5).
- Disconnect recovery script (Phase 5).
- Tailscale deploy (Phase 5).

### Acceptance test
**Setup:**
1. UI Settings add ftmo_acc_001 → consumer groups created runtime.
2. Add exness_acc_001 + pair_001 risk_ratio=1.0.
3. AccountStatus bar 2 dot xanh.

**Hedge open:**
4. Submit BUY EURUSD 0.01 pair_001 → FTMO fill <2s → push secondary → Exness fill <3s → toast hedge_open.
5. PositionList 1 hedge order 2 leg, volume Exness = volume FTMO × ratio.
6. P&L total ≈ 0 khi market đứng yên.

**Cascade close:**
7. UI close FTMO leg → cascade Exness <3s.
8. Manual close cTrader → cascade Exness <5s.
9. SL hit FTMO → auto-close → cascade Exness.
10. Manual close MT5 → poll detect ≤2s → cascade FTMO <5s.
11. Margin call MT5 → Exness auto-close → cascade FTMO.

**Failure modes:**
12. Stop Exness client → submit hedge → form validate fail "exness offline".
13. Mock Exness fail open 3 lần → status `secondary_failed`, FTMO leg vẫn open.
14. Cascade idempotent: trigger 2 lần → đóng 1 lần.

**P&L correctness:**
15. P&L 2 leg với USDJPY pair → đúng.
16. P&L 2 leg với XAUUSD ↔ GOLD (symbol mapping khác name) → đúng.

### Step breakdown
| # | Branch | Scope |
|---|---|---|
| 4.1 | `step/4.1-exness-client-skeleton` | Connect Redis + heartbeat + MT5 connect + `XREADGROUP` loop. **Note**: code viết trên Linux devcontainer OK, nhưng test/run phải pull về máy Windows (MT5 lib Windows-only). |
| 4.2 | `step/4.2-exness-client-actions-and-monitor` | Handlers `open` market + `close` + response + retcode mapping + position monitor poll 2s + account publish. |
| 4.3 | `step/4.3-server-accounts-pairs-api` | CRUD accounts (FTMO + Exness) + CRUD pairs full với risk_ratio. |
| 4.4 | `step/4.4-server-consumer-groups-runtime` | Tạo consumer group runtime khi add account (không cần restart). |
| 4.5 | `step/4.5-server-create-hedge-order` | `create_hedge_order` full flow primary→secondary với retry 3 lần. |
| 4.6 | `step/4.6-server-cascade-close` | Cascade logic + idempotent flag + integrate vào response/event handlers. |
| 4.7 | `step/4.7-server-position-tracker-2legs` | Extend tracker tính P&L 2 leg + total. |
| 4.8 | `step/4.8-web-settings-modal` | Settings modal 3 tab Accounts/Pairs/General. |
| 4.9 | `step/4.9-web-account-status-bar` | AccountStatus bar + heartbeat WS subscribe. |
| 4.10 | `step/4.10-web-hedge-display-and-toasts` | PositionList row hedge 2 leg + total + status indicator + form validate clients online + toast cascade events. |
| 4.11 | `step/4.11-phase-4-docs-sync` | Update docs flows hedge + cascade + `PHASE_4_REPORT.md` + append `DECISIONS.md`. Tag `phase-4-complete`. |

---

## 6. PHASE 5 — Hardening + Deploy

### Mục tiêu
Production-ready: edge cases, multi-account test, deploy Windows Server 2022 + Tailscale, runbook hoàn thiện.

### Prerequisites
- Phase 4 PASS.
- CEO có Windows Server 2022 access.
- CEO có Tailscale account (free tier).
- (Optional) FTMO + Exness account thứ 2 để test multi-account.

### Deliverable visual cho CEO
- Tool deploy lên Windows Server 2022 (Memurai cho Redis, NSSM cho server + clients).
- Tailscale mesh: server + clients VPS đều join 1 tailnet.
- Frontend access qua Tailscale IP từ máy CEO.
- Multi-account: 2 pair chạy đồng thời (2 FTMO + 2 Exness).
- Edge cases handled (G1–G15).
- Backup Redis daily cron.
- Runbook hoàn chỉnh.

### Scope IN
**Hardening:**
- Recovery script `recover-secondaries.py` + cron stream trim + order archive `secondary_failed` >24h.
- Slippage warning: server tính delta entry 2 leg + WS event + frontend toast.
- Symbol mapping validation lúc startup.
- Volume rounding log + warn.
- Reconnection logic clients exponential backoff.

**Multi-account test:**
- Add ftmo_acc_002 + exness_acc_002 + pair_002.
- Deploy 2 client process thêm.
- Smoke test isolation 2 pair đồng thời.

**Deploy:**
- Memurai setup Windows Server.
- NSSM service config server + clients.
- Tailscale install + auth + Redis bind tailscale IP + requirepass mạnh.
- Frontend build static.
- Backup script + Windows Task Scheduler.
- Logs rotation `C:\logs\`.

**Runbook:**
- Add new pair, restart procedures, disaster recovery, backup/restore, health check, smoke test full.

### Scope OUT
- Auto-trading.
- Multi-user / multi-tenant.
- OCO / trailing stop.
- Web public access (chỉ qua Tailscale).
- Prometheus / Grafana.

### Acceptance test
1. Tool deploy thành công Windows Server 2022 + 2 client VPS qua Tailscale.
2. Frontend access từ máy CEO qua Tailscale IP.
3. Smoke test full theo checklist 12-business-rules section 12.
4. Multi-account: 2 hedge order khác pair đồng thời → isolated.
5. Recovery: kill server giữa primary_filled → run script → secondary push tiếp.
6. Edge cases: slippage warning + symbol mismatch warning trigger đúng.
7. Backup script chạy → file dump trong `C:\backup\` timestamp.
8. Tool stable >7 ngày không crash.

### Step breakdown
| # | Branch | Scope |
|---|---|---|
| 5.1 | `step/5.1-server-recovery-and-cleanup` | `recover-secondaries.py` + cron stream trim + order archive >24h + volume rounding log. |
| 5.2 | `step/5.2-slippage-warning-end-to-end` | Server tính delta entry 2 leg + WS event nếu > threshold + setting config + frontend toast. |
| 5.3 | `step/5.3-server-symbol-mismatch-validation` | Lifespan startup validate symbol JSON vs cTrader+MT5. |
| 5.4 | `step/5.4-clients-reconnection-logic` | Exponential backoff cho FTMO + Exness clients. |
| 5.5 | `step/5.5-deploy-windows-services` | Memurai install + NSSM config server + clients + frontend serve. |
| 5.6 | `step/5.6-deploy-tailscale` | Tailscale install server + clients + Redis bind tailscale IP + requirepass. |
| 5.7 | `step/5.7-deploy-backup-cron` | Backup script + Windows Task Scheduler daily. |
| 5.8 | `step/5.8-multi-account-test` | Add 2nd account + 2nd pair + smoke test isolation. |
| 5.9 | `step/5.9-runbook-complete` | RUNBOOK.md với tất cả procedures. |
| 5.10 | `step/5.10-phase-5-final-docs-sync` | Final docs sync + `PHASE_5_REPORT.md` + append `DECISIONS.md`. Tag `v2.0.0`. |

---

## 7. Workflow vận hành plan

### 7.1 Quy trình 1 step

```
CTO ra prompt cho step N
       │
       ▼
User copy → Claude Code (devcontainer Linux)
[Phase 4+ Exness client: pull về máy Windows test]
       │
       ▼
Claude Code:
  - git checkout -b step/<N>-<slug>
  - implement code
  - git commit (post-commit hook tự gửi Telegram 🔧)
  [Nếu cần approve → wrapper script gửi ⚠️]
  [Nếu stuck → wrapper script gửi 💤]
       │
       ▼
CEO nhận Telegram → mở chat copy kết quả về CTO
       │
       ▼
CTO review (6 tiêu chí Section 7.2)
       │
       ├─ ✅ PASS → User merge squash + tag step-<N> + xóa branch
       │           CTO update docs/PROJECT_STATE.md (last_step, next_step)
       │           CTO ra prompt step tiếp theo
       │
       └─ ❌ REJECT → User xóa/archive branch
                     CTO update docs/PROJECT_STATE.md (blocker)
                     CTO ra prompt mới làm lại
```

### 7.2 6 tiêu chí CTO review
1. **Scope compliance**: có làm ngoài scope IN không?
2. **Acceptance criteria**: đủ tất cả tiêu chí?
3. **3 mục tiêu cốt lõi**: leg hở / sai ratio / sai P&L?
4. **Adapter layer**: hardcode broker logic sai chỗ?
5. **Edge case**: đã xử lý các case flag trong doc?
6. **Git compliance**: branch đúng tên + 1 commit + chưa push/merge?

### 7.3 Telegram notify

**3 loại trigger** qua wrapper script `scripts/claude-with-notify.sh`:

| Loại | Trigger | Format |
|---|---|---|
| 🔧 Commit done | post-commit hook trên branch `step/*` | `🔧 [STEP N.M] Claude Code đã commit xong\nBranch: ...\nCommit: <hash>\nFiles changed: <count>\n→ Mở chat CTO review` |
| ⚠️ Need approve | Stdout regex match pattern (allow/approve/permission/y/n/confirm) | `⚠️ [CLAUDE CODE] Cần approve manual\nLast line: <line>\n→ Mở devcontainer xem` |
| 💤 Possibly stuck | No stdout output >90s + process alive | `💤 [CLAUDE CODE] Có thể stuck\nLast active: <time>\n→ Check devcontainer` |

**Throttle**: max 1 notify / loại / 3 phút. State lưu vào `/tmp/notify_throttle_<type>` để check timestamp lần gửi cuối.

**Mục đích**: CEO biết trạng thái Claude Code khi không watch terminal devcontainer → tránh chờ lãng phí.

**Wrapper script `scripts/claude-with-notify.sh`** (CEO dùng wrapper này thay lệnh `claude` trực tiếp):

```bash
#!/bin/bash
# scripts/claude-with-notify.sh
# Wrapper cho Claude Code với Telegram notify cho permission prompt + inactivity

LAST_OUTPUT_TIME=$(date +%s)
WATCHDOG_PID=""
THROTTLE_DIR="/tmp/claude_notify_throttle"
mkdir -p "$THROTTLE_DIR"

# Hàm throttle: check xem đã notify loại này trong 3 phút qua chưa
should_notify() {
  local type=$1
  local file="$THROTTLE_DIR/$type"
  local now=$(date +%s)
  if [[ -f "$file" ]]; then
    local last=$(cat "$file")
    if (( now - last < 180 )); then
      return 1  # too soon
    fi
  fi
  echo "$now" > "$file"
  return 0
}

# Watchdog: check inactivity mỗi 30s
watchdog() {
  while true; do
    sleep 30
    local now=$(date +%s)
    local elapsed=$((now - LAST_OUTPUT_TIME))
    if (( elapsed > 90 )); then
      if should_notify "stuck"; then
        ./scripts/notify_telegram.sh "💤 [CLAUDE CODE] Có thể stuck
Last active: $((elapsed))s ago
→ Check devcontainer"
      fi
    fi
  done
}

watchdog &
WATCHDOG_PID=$!
trap "kill $WATCHDOG_PID 2>/dev/null" EXIT

# Run Claude Code, monitor stdout
claude --dangerously-skip-permissions "$@" 2>&1 | while IFS= read -r line; do
  echo "$line"  # passthrough
  LAST_OUTPUT_TIME=$(date +%s)
  
  # Detect permission prompt patterns
  if echo "$line" | grep -qiE "(allow.*\?|approve|permission|\(y/n\)|confirm)"; then
    if should_notify "approve"; then
      ./scripts/notify_telegram.sh "⚠️ [CLAUDE CODE] Cần approve manual
Last line: $line
→ Mở devcontainer xem"
    fi
  fi
done
```

**Post-commit hook `.git/hooks/post-commit`**:

```bash
#!/bin/bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" == step/* ]]; then
  COMMIT_HASH=$(git rev-parse --short HEAD)
  COMMIT_MSG=$(git log -1 --pretty=%s)
  FILES_CHANGED=$(git show --stat --name-only HEAD | tail -n +2 | wc -l)
  
  ./scripts/notify_telegram.sh "🔧 [$BRANCH] Claude Code đã commit xong
Commit: $COMMIT_HASH — $COMMIT_MSG
Files changed: $FILES_CHANGED
→ Mở chat CTO review"
fi
```

**Cross-platform note**: bash hooks chạy được trên Windows nhờ Git Bash bundled với Git for Windows. CEO chỉ cần đảm bảo Windows machine (Phase 4+) đã cài Git for Windows.

KHÔNG có notify cho PASS/REJECT/PHASE_COMPLETE — đó là việc CTO+CEO làm tay, không cần auto.

### 7.4 Phase docs sync (step cuối mỗi phase)

Mỗi step `phase-N-docs-sync` PHẢI produce:

**1. Update docs hiện có** (00-overview.md, 01-architecture.md, ..., 12-business-rules.md): bất kỳ change nào so với design ban đầu cần cập nhật vào file tương ứng.

**2. Tạo `docs/PHASE_N_REPORT.md`** với template:
```markdown
# Phase N — Completion Report

## Acceptance criteria results
| # | Test | Result | Evidence |
|---|---|---|---|
| 1 | ... | ✅ PASS | ... |

## Decisions made during phase
[Quyết định kỹ thuật phát sinh không có trong plan ban đầu]

## Deviations from plan
[Bất kỳ scope IN/OUT thay đổi nào vs MASTER_PLAN_v2]

## Known issues / TODO
[Bug/limitation phát hiện nhưng đẩy sang phase sau]

## Files added/modified summary
[List file mới + file thay đổi nhiều]

## Next phase prerequisites checklist
[CEO cần chuẩn bị gì trước phase tiếp theo]
```

**3. Append vào `docs/DECISIONS.md`** (cumulative, file nối tiếp qua mọi phase):
```markdown
## D-<NNN> (Phase N.M) — <decision title>
Reason: <lý do ngắn gọn>
Trade-off: <nếu có>
```

**4. Update `docs/PROJECT_STATE.md`**: phase complete, set next phase, clear blocker, snapshot mới.

**5. Update Section 8 (Status tracker)** trong file MASTER_PLAN_v2.md này.

**6. Tag git release**: `git tag phase-<N>-complete` (Phase 5 thì tag `v2.0.0`).

### 7.5 CTO continuity — onboard CTO chat instance mới

**Vấn đề**: mỗi CTO chat instance không có memory từ chat trước. CEO không thể paste lại toàn bộ context mỗi lần.

**Solution**: documentation đủ để CTO mới onboard chỉ qua đọc file. CEO chỉ cần copy template ngắn khi tạo chat mới.

**Mandatory reading order cho CTO instance mới:**
1. `MASTER_PLAN_v2.md` — plan tổng thể.
2. `docs/PROJECT_STATE.md` — snapshot hiện tại (cập nhật **liên tục sau mỗi step PASS**).
3. `docs/DECISIONS.md` — log mọi quyết định kỹ thuật.
4. `docs/PHASE_<N>_REPORT.md` — report của các phase đã hoàn thành (nếu có).

**`docs/PROJECT_STATE.md`** — file quan trọng nhất, format chuẩn:

```markdown
# PROJECT_STATE — Live snapshot

> Last updated: YYYY-MM-DD HH:MM by CTO chat #<id>
> Read me FIRST khi start CTO instance mới.

## Current position
- **Phase**: <N> — <tên phase>
- **Last completed step**: <step ID> (PASS, merged) hoặc REJECTED
- **Next step**: <step ID>
- **Blocker**: <none | mô tả blocker>

## Active context (hot info, có thể thay đổi)
- <Bullet points context đang nóng, vd issue đang debug, pending discussion>

## Recent decisions (top 5, full list ở DECISIONS.md)
- D-<NNN>: ...
- D-<NNN>: ...

## Pending items (TODO/parked)
- [ ] <item>: <ngắn gọn lý do park>

## Quick reference
- Repo: <link>
- Telegram chat ID: <ref>
- Test accounts setup: <ref>
- Test symbols: <list>
```

**Quy ước update PROJECT_STATE.md**:
- Sau **mỗi step PASS**: update `Last completed step`, `Next step`, refresh `Active context` nếu có thay đổi.
- Sau **mỗi step REJECT**: update `Blocker` + lý do reject.
- Sau **mỗi phase complete**: refresh toàn bộ file, archive context phase cũ vào PHASE_N_REPORT.md.

**`docs/CTO_HANDOFF_TEMPLATE.md`** — template CEO copy khi tạo chat CTO mới:

```markdown
# CTO Chat Handoff Template

Khi tạo CTO instance mới, paste đoạn dưới (project files đã có sẵn nên CTO đọc được):

---
[Context handoff]
Project: FTMO Hedge Tool. Bạn là CTO, follow project instructions đã có.

Mandatory reading order:
1. MASTER_PLAN_v2.md
2. docs/PROJECT_STATE.md (state hiện tại — đọc kỹ)
3. docs/DECISIONS.md (cumulative decisions)
4. docs/PHASE_<N>_REPORT.md (latest report nếu có)

Last action by previous CTO: <CEO điền 1 dòng, vd "review PASS step 2.4">
Current focus: <CEO điền 1 dòng, vd "ra prompt step 2.5">
---
```

CEO chỉ điền 2 dòng cuối → CTO mới có đủ context bắt đầu work ngay.

---

## 8. Status tracker

| Phase | Status | Steps | Tag | Report |
|---|---|---|---|---|
| 1 — Foundation | ✅ done | 9/9 | `phase-1-complete` | `docs/PHASE_1_REPORT.md` |
| 2 — Market Data + Chart + Form | ✅ done | 21/10 | `phase-2-complete` | `docs/PHASE_2_REPORT.md` |
| 3 — Single-leg Trading | ⏳ pending | 0/14 | — | — |
| 4 — Hedge + Cascade | ⏳ pending | 0/11 | — | — |
| 5 — Hardening + Deploy | ⏳ pending | 0/10 | — | — |

> Phase 1 đếm 9 step khi tính cả 1.4a (sub-fix). Plan gốc có 8.
> Phase 2 đếm 21 step khi tính 10 step chính + 11 sub-fix (2.1a, 2.1b, 2.2a, 2.6a, 2.7a, 2.7b, 2.7c, 2.7e, 2.8a, 2.9a, 2.9b). Plan gốc có 10. Step 2.7d (RAF throttle) làm xong nhưng REJECT — không merge. Chi tiết deviation xem `PHASE_2_REPORT.md`.

CTO update tracker này sau mỗi phase PASS.

---

## 9. Đổi/bổ sung plan

Bất kỳ thay đổi scope/phase phải:
1. CTO + CEO thảo luận, document lý do.
2. Update file này (commit riêng vào `main`, không qua branch step).
3. Note vào status tracker + `DECISIONS.md`.

KHÔNG sửa scope giữa step đang chạy.
