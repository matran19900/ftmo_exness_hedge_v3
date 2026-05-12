# PROJECT_STATE — Live snapshot

> Cập nhật lần cuối: 2026-05-12 — Claude Code (step 3.14a phase-3-docs-sync-core)
> ĐỌC FILE NÀY ĐẦU TIÊN khi mở phiên CTO chat mới.

## Vị trí hiện tại
- **Phase**: 3 — Single-leg Trading (FTMO only) — HOÀN THÀNH
- **Step vừa hoàn thành**: 3.14a — phase-3-docs-sync-core (chính step này)
- **Step kế tiếp**: 3.14b — phase-3-docs-sync-tech (tech reference docs: 05-redis-protocol, 06-data-models, 07-server-services, 08-server-api, 09-frontend; README; RUNBOOK)
- **Phase tiếp theo**: 4 — Hedge + Cascade Close (Exness leg)
- **Blocker**: không (CEO cần Exness MT5 credentials + máy Windows trước step 4.1; xem Phase 4 prerequisites trong MASTER_PLAN_v2 §5)

## Tóm tắt Phase 3

30 step actions trong 29 commit substantive (13 step chính 3.1–3.13 + 16 sub-fix) + 1 docs sync (3.14, split 2 parts: 3.14a + 3.14b). Tag dự kiến: `step-3.x` per step + `phase-3-complete` (CEO tag thủ công sau khi merge 3.14b).

**Server (Phase 3 add)**:
- RedisService đầy đủ CRUD: orders (với Lua CAS), accounts, pairs, streams, heartbeat, position cache, settings.
- Lifespan `setup_consumer_groups()` idempotent, init account script `scripts/ops_init_account.py`.
- POST /api/orders endpoint với OrderService validation pipeline (pair → account → client → symbol → tick → SL/TP direction). Phase 3 full close only (D-100).
- response_handler + event_handler background loops (consumer group "server", XREADGROUP BLOCK 1000ms). Reconciliation infrastructure (reconcile_state stream + DealList history).
- position_tracker_loop 1s poll: unrealized P&L formula với USD conversion (JPY via USDJPY, cross via USD/QUOTE direct hoặc inverse).
- REST surface Phase 3: GET /api/orders (list), GET /api/orders/{id}, GET /api/positions, GET /api/history, POST /api/orders/{id}/close (202), POST /api/orders/{id}/modify (202). Plus step 3.12: GET /api/accounts. Plus step 3.13: PATCH /api/accounts/{broker}/{id}, DELETE /api/pairs guards.
- 6 WS channels: ticks:*, candles:*, positions (exact-match), orders, accounts, agents (Phase 1 legacy).
- BroadcastService coalesces partial cTrader delta ticks (3.11b root cause fix).
- account_status_loop 5s broadcast → header AccountStatusBar.
- **473 server test pass**, mypy strict clean.

**FTMO client (Phase 3 new)**:
- Twisted bridge connect cTrader Open API + OAuth (`hedger-shared`).
- Action handlers: open (market với post-fill amend D-058, limit, stop), close (2-event sequence D-066), modify_sl_tp.
- Unsolicited execution events → event_stream (position_closed, pending_filled, position_modified, order_cancelled).
- account_info_loop publish `account:ftmo:{acc}` HASH mỗi 30s.
- Reconciliation infrastructure: ReconcileReq + DealListByPositionIdReq + fetch_close_history.
- 177 test pass, mypy strict clean.

**Web (Phase 3 add)**:
- HedgeOrderForm submit `POST /api/orders` real với 3 layer preflight (pair/symbol/volume, Entry-vs-SL, bid/ask direction). Order type selector Market/Limit/Stop (3.12b). Market mode auto-entry từ throttled tick (5s).
- PositionList Open + History tabs với PositionRow + OrderRow, Pair column từ pairs cache (3.12a). Modify modal + Close confirm.
- AccountStatusBar header với 5s WS broadcast (3.12) — dot + balance/equity per account.
- Settings modal (3.13): Pairs CRUD + Accounts enable/disable toggle.
- useWebSocket hook hoisted MainPage (3.10): single shared connection, 6 channels subscribed.
- VolumeCalculator state machine với `refreshing` variant giữ prev result trong recalc (3.12c — no flicker).
- Bundle: 463.91 kB JS / 147.11 kB gzip.

**Infra (Phase 3)**:
- WS VALID_CHANNEL_PREFIXES = (ticks:, candles:, positions, orders, accounts, agents).
- Step 3.13a: row_to_entry helper centralized cho REST + WS payload single source of truth.
- 6 router include trong main.py (auth, auth_ctrader, charts, health, history, orders, pairs, positions, symbols, accounts, ws).

## Active context (state runtime để smoke)
- Backend chạy ở `http://localhost:8000`.
- Frontend dev server ở `http://localhost:5173` với Vite proxy `/api` + `/ws` → backend.
- Default credentials: `admin` / `admin`.
- FTMO cTrader OAuth done qua `GET /api/auth/ctrader/ftmo` (CEO setup 1 lần ở Phase 3).
- FTMO live account: ctid_trader_account_id=47247733 (CEO free trial).
- 117 symbol mapped (whitelist) → ~91 match cTrader broker.
- 473 server tests + 177 ftmo-client tests passing, mọi build check (typecheck/lint/format/build) đều green.

## Known issues — hoãn cho Phase 5 hardening

Backlog tổng Phase 1 + 2 + 3 (chi tiết trong PHASE_3_REPORT.md §Phase 5 hardening backlog):

**Server**:
- Per-account/per-pair SET index cho orders (list performance ở scale lớn).
- Idempotency-Key header cho POST endpoints.
- Dead-letter sweeper cho stuck consumer group PEL.
- Migrate Phase 2 pair rows: set enabled=true explicitly (D-085).
- pyproject.toml pythonpath consolidation; fix pre-existing test_config.py mypy errors.
- p_money_digits fallback từ account:ftmo:{acc}.money_digits.
- sync_symbols persist contract_size + quote_currency cho non-FX symbols.
- Auto-subscribe USD-cross tick streams khi first position open.
- Consolidate position:{id} JSON với position_cache:{id} HASH.
- count_orders_by_pair: thay O(N) scan bằng pair_orders:{pair_id} SET index.

**FTMO client**:
- Protocol-level disconnect trước TCP close (shutdown order).
- disconnect() cancel _pending_executions với timeout.
- Retry amend sau POSITION_LOCKED transient error.
- closePositionDetail.moneyDigits vs account-level reconcile.
- ProtoOAMarginCallTriggerEvent cho stopout close_reason.
- Pending orders reconciliation full handling.
- hasMore pagination DealListByPositionIdRes.

**Frontend**:
- Vitest + React Testing Library setup.
- Custom ConfirmModal thay window.confirm.
- Typed API client với conversion layer (giảm cast bool/str).
- Optimistic UI cho Close + Modify.
- History pagination UI.
- WS tick dedup nếu bandwidth concern.
- Multi-draft order form (multiple tabs).
- PairPicker toast on stale re-select (silent today).
- Row click selection + chart overlay (entry/SL/TP overlay cho selected order — backlog kế thừa từ Phase 2 + 3).
- Banker's rounding cho exotic symbols (D-122 mở rộng).
- Memoize Map<order_id, Order> selector (D-133 join performance).
- Hybrid UUID display first-4+last-4 (D-132 mở rộng).
- Reactive pairs cache refresh sau CRUD (3.12a chỉ mount-once fetch).
- Initial-state observability: one-shot WARN per (symbol, process) khi tickThrottled lần đầu null.
- TTL refresh on tick cache stale prev edge (3.11b root cause hardening).
- entryPrice subscriber memoization nếu widgets mở rộng (3.12b).
- Distance-from-market sanity check broker-aware (3.12b limit/stop validation hardening).

**Operations**:
- Document `uvicorn --reload` flag trong RUNBOOK (3.14b).
- Document FTMO client restart workflow (3.14b).
- Browser hard refresh checklist sau frontend merge.
- Multi-FTMO mixed-state OrderForm message refinement (3.13a hiện 3-tier; mixed state Phase 5).
- Extract Pydantic schemas sang `app/api/schemas/` để break D-149 function-local imports.

## Recent decisions (top 5, full list trong DECISIONS.md)

1. **D-149 (3.13a)**: Function-local imports trong accounts.py để break circular dependency accounts ↔ account_helpers. Codebase convention từ ws.py. Phase 5 cleanup: extract schemas sang dedicated module.
2. **D-148 (3.13a)**: OrderForm 3-tier ftmoBlockMessage priority (no account > all disabled > offline). Mỗi message actionable (configure / Settings / investigate process).
3. **D-147 (3.13a)**: row_to_entry helper centralized trong app/services/account_helpers.py. Single source of truth cho REST + WS payload conversion. Pre-3.13a regression Boolean("false") === true fixed permanently.
4. **D-146 (3.13)**: FTMO account create UI defer Phase 5 (OAuth flow integration). Bootstrap path qua FTMO client first-time write.
5. **D-145 (3.13)**: Phase 3 Exness account dropdown defer — text input free-form. Phase 4 widens to <select> khi accounts:exness SET populated.

## Pending items / TODO

- 3.14b: Cập nhật tech reference docs (05-redis-protocol, 06-data-models, 07-server-services, 08-server-api, 09-frontend) + README + RUNBOOK với deltas Phase 3.
- Phase 4: CEO chuẩn bị Exness MT5 credentials + máy Windows local trước step 4.1 (MT5 lib Windows-only).
- Phase 4: Symbol mapping JSON có entry FTMO ↔ Exness cho test pair (vd EURUSD ↔ EURUSDm).
- Phase 5 backlog: Telegram wrapper rewrite, drag price line, missing toasts, sync_symbols 24h cache, ratio per pair, plus Phase 3 hardening items liệt kê ở Known Issues.

## Quick reference
- Repo: https://github.com/matran19900/ftmo_exness_hedge_v3
- Workspace: `/workspaces/ftmo_exness_hedge_v3`
- Redis: `redis://192.168.88.4:6379/2` (LAN)
- Symbol count: 117 mapped (whitelist) → ~91 match cTrader broker.
- Test accounts:
  - cTrader **demo** (Phase 2 market data only, OAuth done).
  - FTMO live free trial cTrader (Phase 3 trading, ctid_trader_account_id=47247733, OAuth done).
  - Exness live (Phase 4+ — máy Windows cần thiết, MT5 lib Windows-only).
- Python: 3.12, Node: 24, npm: 11.12.1.
- Stack versions: React 19.2, Vite 8.0, TypeScript 6.0, Tailwind 3.4, Zustand 5.0, Axios 1.16, react-hot-toast 2.6, Lightweight Charts 5.2, FastAPI, Pydantic v2, redis.asyncio, Twisted (FTMO client only), fakeredis (tests).
- WS channels (6): ticks:{symbol}, candles:{symbol}:{tf}, positions, orders, accounts, agents (legacy).
- REST surface Phase 3 (8 endpoints aggregate): /api/orders (POST + GET list + GET /id + POST /id/close + POST /id/modify), /api/positions (GET), /api/history (GET), /api/pairs (GET POST PATCH DELETE), /api/accounts (GET + PATCH /broker/id), /api/symbols (GET), /api/charts/:symbol/ohlc (GET), /api/symbols/:symbol/calculate-volume (POST), /api/auth/login (POST), /api/auth/ctrader/... (Phase 2).
