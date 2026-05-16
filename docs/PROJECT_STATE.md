# PROJECT_STATE — Live snapshot

> Cập nhật lần cuối: 2026-05-16 — Claude Code (step 4.12 phase-4-docs-sync)
> ĐỌC FILE NÀY ĐẦU TIÊN khi mở phiên CTO chat mới.

## Vị trí hiện tại
- **Phase**: 4 — Hedge + Cascade Close (Exness leg) — HOÀN THÀNH
- **Step vừa hoàn thành**: 4.12 — phase-end docs sync (chính step này)
- **Step kế tiếp**: 5.0 — Phase 5 plan drafting (CTO sẽ ship design doc)
- **Phase tiếp theo**: 5 — Hardening + Deploy
- **Blocker**: không. Phase 4 acceptance pass, smoke 2026-05-16 verified 5 paths.

## Tóm tắt Phase 4

20 step actions trong ~32 commit substantive (5 step chính 4.1–4.5 + 8 sub-fix 4.5a/4.7a/4.7b/4.8/4.8a–g + 1 alert backend 4.11 + 8 symbol-mapping 4.A.0–7 + 1 docs sync 4.12). Tag dự kiến: `step-4.x` per step + `phase-4-complete` (CEO tag thủ công sau khi merge 4.12).

**Server (Phase 4 add)**:
- Exness account & pairs CRUD extension (step 4.5): hedge pairs (FTMO leg + Exness leg + risk_ratio), accounts:exness SET, mapping_status:{account_id} per-account state.
- HedgeService cascade orchestrator (step 4.7a + 4.8): cascade open Path A primary→secondary 3-retry exponential backoff, cascade close 5 trigger paths (A/B/C/D/E) with cascade_lock Lua SET-NX-EX serialization, cascade_cancel_pending transient state for primary-closed-mid-secondary-open race.
- AlertService publish-only + Telegram dispatch (steps 4.7b + 4.11): Redis HASH alert:{id} + WS `alerts` channel + 5-type registry (`hedge_leg_external_close_warning`, `hedge_leg_external_modify_warning`, `hedge_closed`, `leg_orphaned`, `secondary_liquidation`) + httpx POST sendMessage plain-text + bypass_cooldown param.
- Server external-close state sync Option C (steps 4.8e + 4.8f): external Exness close stamps `s_status="closed"` on order HASH but keeps composed `status="filled"` so operator UI close path stays available; cascade orchestrator detects orphan-close on subsequent FTMO close and finalizes composed `status="closed"` via short-circuit (no redundant cmd push).
- Symbol mapping cache + wizard backend (steps 4.A.0–5): per-Exness-account mapping cache files (file=truth, Redis=working cache), AutoMatchEngine 3-tier (whitelist/fuzzy/manual hints), mapping_status state machine (pending_mapping/active/spec_mismatch/disconnected), wizard REST surface.
- Frontend mapping_status boot seed (step 4.8g): `useMappingStatusSubscription` hoisted from `<AccountsTab>` to `<MainPage>` so REST seed + WS subscribe run at app boot, not gated on opening Settings → Accounts.
- ws.py whitelist: +`mapping_status:*` (step 4.A.4) + `alerts` (step 4.11).
- **940 server tests pass**, mypy strict + ruff clean.

**Exness client (Phase 4 NEW)**:
- MT5 Python lib bridge (Windows-only per D-010) — Redis + heartbeat + symbol sync + position monitor poll 2s + reconciliation.
- ActionHandler full open/close with **filling-mode bitmask** retry loop (FOK → IOC → BOC → RETURN fallback per symbol-specific bitmask).
- Comment field 29-char SDK limit (D-167) + `v3:` prefix neutral (was leaking strategy intent) + last_error capture on order_send None.
- Position monitor persistent snapshot + cmd ledger (4.3a) for restart resilience.
- account_info_loop publish `account:exness:{acc}` HASH mỗi 30s.
- 197 exness-client tests pass.

**FTMO client (Phase 4 add)**:
- Step 4.8c: solicited close paths also publish `position_closed` event so server can see operator-issued FTMO close (was only fast-path slow-path discrepancy). Single helper `_publish_position_closed_from_fill` reused by both paths.
- 181 ftmo-client tests pass.

**Web (Phase 4 add)**:
- Symbol mapping wizard UI (step 4.A.6) — full Settings → Accounts integration with raw-symbols / auto-match / spec-mismatch resolve / save flows; per-account mapping_status dot + Map / Edit / Re-sync / Resolve buttons gated by status.
- HedgeOrderForm pair-aware validation (step 4.A.7) — `isWizardNotRun` banner block when pair's Exness account has mapping_status ≠ "active"; `checkPairSymbol` pre-flight against `/api/pairs/{id}/check-symbol/{symbol}` before submit.
- Step 4.8g: mapping_status REST seed + WS subscribe hoisted to MainPage (boot-level) → wizard-not-run banner no longer false-fires on browser refresh.
- 77 web tests pass.

**Infra (Phase 4)**:
- Step 4.4a + 4.4b: vendor cTrader OAuth helper into ftmo-client tree + delete legacy hedger_shared directory (CPR consolidation).
- WS channel whitelist now 9 entries: ticks:*, candles:*, positions, orders, accounts, agents, mapping_status:*, alerts (4.11), + transitional.
- Tag plan: `step-4.X` per step, `phase-4-complete` after merge.

## Active context (state runtime để smoke)
- Backend chạy ở `http://localhost:8000`.
- Frontend dev server ở `http://localhost:5173` với Vite proxy `/api` + `/ws` → backend.
- Default credentials: `admin` / `admin`.
- FTMO cTrader OAuth done qua `GET /api/auth/ctrader/ftmo` (CEO setup 1 lần ở Phase 3, persisted).
- FTMO live account: ctid_trader_account_id=47247733 (CEO free trial).
- Exness MT5: demo 433590363 + Windows máy #2 demo Standard (CEO smoke 2026-05-15..16).
- Symbol mapping per-Exness-account files at `server/data/symbol_mapping_cache/{signature}.json`; signature = sha256 sorted raw symbol names (D-SM-03).
- 940 server + 181 ftmo-client + 197 exness-client + 77 web = **1395 tests** passing, mọi build check (typecheck/lint/format/build) đều green.
- Telegram production alert channel: gated by `TELEGRAM_ALERT_ENABLED=true` + `TELEGRAM_ALERT_BOT_TOKEN` + `TELEGRAM_ALERT_CHAT_ID` in `.env`. CEO chuẩn bị channel mới trước khi enable production.

## Recent decisions (top 5 Phase 4, full list trong DECISIONS.md)

1. **D-181 (4.8f)**: Server external close state sync revised Option A → Option C. Composed `status` stays `"filled"` on external Exness close; orphan stays visible on Open tab + API close path open. Cascade orchestrator detects on subsequent FTMO close and finalizes via short-circuit. Locks the orphan UX trap that 4.8e Option A introduced (row vanish + 400 not_closeable reject).
2. **D-167 (4.8b)**: MT5 SDK comment field actual 29-character limit (SDK docs claim 31). Comments >29 chars cause `order_send` to silent-fail with None return + no exception. Phase 4 ActionHandler now clamps to `[:29]` + captures `mt5.last_error()` for diagnostic surface.
3. **D-170 (4.8c)**: FTMO client solicited close paths also publish `position_closed` unsolicited event via single shared helper `_publish_position_closed_from_fill`. Pre-4.8c only manual closes published; server cascade couldn't detect operator-triggered FTMO close. Single source of truth: helper called from both fast-path + slow-path post-fill-parse.
4. **D-187..D-192 (4.11)**: AlertService extended with httpx Telegram dispatch + `bypass_cooldown` parameter + 3 new types (`hedge_closed` INFO, `leg_orphaned` + `secondary_liquidation` CRITICAL). 4.7b 2 WARN types preserved verbatim. Telegram errors are best-effort (logged WARNING, swallowed) so Redis + WS remain source of truth.
5. **D-185..D-186 (4.8g)**: Frontend `mappingStatusByAccount` boot seed hoisted from `<AccountsTab>` to `<MainPage>` via `useMappingStatusSubscription(exnessIds)` so REST seed + WS subscribe run at app boot. Pre-4.8g the "Hedge leg blocked — wizard not run" banner false-fired on every browser refresh because the seed was gated on opening Settings → Accounts.

## Pending items / TODO

- Phase 5 plan (step-5.0): CTO drafts design doc covering D-SMOKE-1, 3, 5, 6, 7, 8 + deferred alerts (server_error, client_offline/online, broker_disconnect/reconnect) + Settings API for alert toggles + alert templates module + frontend toggle UI + orphan_sweep module + MT5 multi-account hardening + cTrader market-data 24h cache + deploy Windows Server 2022 + Tailscale mesh.
- CEO: provision dedicated Telegram alert chat (separate from self-check chat per D-4.0-spec §2.E.1) + set `TELEGRAM_ALERT_ENABLED=true` in `.env` to go live with operator alerts.
- CEO: archive Phase 4 self-check files (`step-4.*-selfcheck.md` in repo root) sau khi tag `phase-4-complete` per WORKFLOW §6.1.

## Phase 5 hardening backlog summary

Xem `docs/PHASE_4_REPORT.md` §9 cho full ~30 items danh sách. Highlights:

- **Safety / correctness**: D-SMOKE-8 cascade retry redesign (1×5s pre-write cmd_ledger vs 3×0.5/1/2 stricter ledger semantics), force-close FTMO orphan endpoint, orphan_sweep module, alert_templates module extraction.
- **Operations**: D-SMOKE-5 consumer groups runtime API (Account CRUD POST creates group dynamically; current lifespan-only setup requires server restart), D-SMOKE-6 sequential ID schema (order_id, pair_id, alert_id), MT5 multi-account on single Windows host.
- **Frontend**: D-SMOKE-1 balance display bug Exness Standard (renders 9.99 instead of 998.77 — money_digits mismatch), D-SMOKE-3 PositionList Open tab non-filled status render gap, D-SMOKE-7 pair column "—" instead of pair.name.
- **Alerts system**: 5 deferred types (`server_error`, `client_offline`, `client_online`, `broker_disconnect`, `broker_reconnect`) + Settings API `GET/PUT /api/settings/alerts` + `POST /api/alerts/test` + frontend toggle UI tab + recovery-alert bypass_cooldown wiring + alert_templates.py + alert_service_keys.py modules.
- **Cleanup / refactor**: hedge_leg_external_close_warning → secondary_close_manual rename per design §2.A canonical (defer Phase 5 to avoid breaking 4.7b call sites mid-phase), Pydantic schemas extract to `app/api/schemas/`.
- **Workflow / process**: bug-class fix audit sibling handlers (now codified in WORKFLOW.md §10 per D-4.8d lesson + D-SMOKE-10 incident).

## Quick reference
- Repo: https://github.com/matran19900/ftmo_exness_hedge_v3
- Workspace: `/workspaces/ftmo_exness_hedge_v3`
- Redis: `redis://192.168.88.4:6379/2` (LAN)
- Symbol count: 117 FTMO whitelist (mapping_ftmo_whitelist.json) → per-Exness-account raw-symbol files at `server/data/symbol_mapping_cache/{sig}.json`.
- Test accounts:
  - cTrader **demo** (Phase 2 market data only, OAuth done).
  - FTMO live free trial cTrader (Phase 3+ trading, ctid_trader_account_id=47247733, OAuth done).
  - Exness MT5 demo 433590363 + Standard demo Windows máy #2 (Phase 4 smoke).
- Python: 3.12, Node: 24, npm: 11.12.1.
- Stack versions: React 19.2, Vite 8.0, TypeScript 6.0, Tailwind 3.4, Zustand 5.0, Axios 1.16, react-hot-toast 2.6, Lightweight Charts 5.2, FastAPI, Pydantic v2 (+ `model_validator` for cross-field check), redis.asyncio, Twisted (FTMO client only), `MetaTrader5` package (Exness client only, Windows), httpx (Phase 4 AlertService Telegram + FTMO OAuth), fakeredis (tests).
- WS channels (9): `ticks:{symbol}`, `candles:{symbol}:{tf}`, `positions`, `orders`, `accounts`, `agents` (legacy), `mapping_status:{exness_account_id}` (Phase 4.A.4), `alerts` (Phase 4.11).
- REST surface Phase 4 cumulative (~18 endpoints): /api/orders (POST + GET list + GET /id + POST /id/close + POST /id/modify), /api/positions (GET), /api/history (GET), /api/pairs (GET POST PATCH DELETE + GET /id/metadata + GET /id/check-symbol/{symbol}), /api/accounts (GET + PATCH /broker/id + DELETE /broker/id), /api/accounts/exness/{id}/raw-symbols, /api/accounts/exness/{id}/mapping-status, /api/accounts/exness/{id}/symbol-mapping/auto-match, /api/accounts/exness/{id}/symbol-mapping/save, /api/accounts/exness/{id}/symbol-mapping/edit, /api/accounts/exness/{id}/symbols/resync, /api/symbol-mapping-cache (GET), /api/symbols (GET), /api/symbols/{symbol}/calculate-volume (POST), /api/charts/:symbol/ohlc (GET), /api/auth/login (POST), /api/auth/ctrader/... (Phase 2).
