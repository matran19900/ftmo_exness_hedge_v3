# Phase 3 — Báo cáo hoàn thành

> Trạng thái: **HOÀN THÀNH** (chờ tag `phase-3-complete` sau khi merge step 3.14b).
> Phạm vi: Single-leg trading FTMO (Exness leg Phase 4).
> Thời lượng thực tế: từ commit `c09ce73` (step 3.1) đến commit `227f589` (step 3.13a) — **~7 ngày**, gồm 29 commit substantive (13 step chính + 16 sub-fix) + 1 docs sync split 2 parts (3.14a + 3.14b).
> Test count cuối Phase 3: **650** (473 server + 177 ftmo-client). +540 so với Phase 2 cuối (110 server tests).

## §1. Mục tiêu Phase 3 (theo `MASTER_PLAN_v2` §4)

1. **Server-side order lifecycle**: cmd_stream → response_handler → event_handler → Redis state → WS broadcast.
2. **FTMO client**: place_market_order_with_sltp + place_pending + close + modify + reconciliation.
3. **Frontend**: full HedgeOrderForm submit + PositionList Open/History tabs + AccountStatusBar + Settings modal.
4. **Real-time unrealized P&L** per position với USD conversion (forex incl. JPY pairs + cross + inverse).

## §2. Acceptance Table

Acceptance Phase 3 theo `MASTER_PLAN_v2` §4 (15 test cases) + acceptance bổ sung phát sinh (Account status bar, Settings modal — CEO directive mid-phase).

| # | Test | Kết quả | Bằng chứng |
|---|---|---|---|
| 1 | FTMO client heartbeat OK | PASS | step 3.3 smoke; `client:ftmo:ftmo_001` HASH TTL 30s refresh mỗi 10s |
| 2 | Server restart → consumer groups created idempotent | PASS | step 3.2 lifespan; `setup_consumer_groups()` BUSYGROUP swallow |
| 3 | Submit BUY EURUSD market 0.01 SL/TP → cTrader broker thấy <2s | PASS | step 3.6 + 3.7 + 3.11 smoke; CEO live FTMO ctid=47247733 |
| 4 | Open tab + P&L USD update mỗi 1s, sai số <1% so cTrader | PASS | step 3.8 position_tracker 1s loop; P&L formula match (current-entry)×side×volume_base÷rate |
| 5 | Limit/Stop pending → fill khi giá hit → toast primary_filled | PASS | step 3.12b UI selector + step 3.4 + 3.7 backend; CEO smoke 3.12b session |
| 6 | USDJPY (JPY conversion), XAUUSD (commodity), US30/NAS100 (index): P&L đúng | PARTIAL | step 3.8 JPY via USDJPY bid PASS. XAUUSD + indices: Phase 5 hardening (D-094, D-095 contract_size/quote_currency persist explicit cho non-FX) |
| 7 | Drag SL → cTrader update <2s | DEFERRED | Phase 5 hardening (kế thừa từ Phase 2 deferred). UI có Modify modal (step 3.10) thay thế; drag UI Phase 5. |
| 8 | Drag SL invalid → server reject → revert | DEFERRED | Tương tự (7). Modify modal có client-side preflight + server reject với error_code revert toast. |
| 9 | UI close → cTrader đóng → row chuyển History | PASS | step 3.9 POST /orders/{id}/close + step 3.10 PositionRow close button + History tab |
| 10 | Manual close cTrader → server detect <5s → frontend update | PASS | step 3.5 unsolicited execution event → event_handler → WS broadcast → frontend reactive |
| 11 | SL hit → auto-close → frontend update | PASS | step 3.5a close_reason="sl" inference qua order.orderType + grossProfit sign |
| 12 | Stop client → submit lệnh → 503 "ftmo offline" | PASS | step 3.6 OrderService validation pipeline `client_offline` error_code |
| 13 | Restart client với pending command → resume PEL | PASS | step 3.4 + 3.7 XREADGROUP consumer group PEL semantics; idempotent ACK |
| 14 | Server crash với open order → restart → P&L resume | PASS | step 3.8 position_cache TTL 600s; sau restart, position_tracker_loop re-compute từ orders:by_status:filled SET |
| 15 | SL/TP modify ngoài cTrader → server detect ≤5s → frontend update | PASS | step 3.5 ORDER_REPLACED event → event_handler → WS position_event |

**Acceptance bổ sung phát sinh trong Phase 3** (CEO directive mid-phase):

| # | Test | Kết quả | Bằng chứng |
|---|---|---|---|
| A1 | Order type selector Market/Limit/Stop | PASS | step 3.12b segmented buttons + auto-entry market mode |
| A2 | AccountStatusBar header với balance/equity + status dot real-time | PASS | step 3.12 (REST + WS) + 3.13a payload typed |
| A3 | Settings modal: Pairs CRUD + Accounts enable/disable | PASS | step 3.13 SettingsModal 2 tab + 3.13a tooltip clarity |
| A4 | Pair column trong Open + History tabs | PASS | step 3.12a PositionRow + OrderRow first column + pairs cache hoist MainPage |
| A5 | Form submit gating khi tất cả FTMO offline (red banner + tooltip) | PASS | step 3.12 + 3.13a 3-tier ftmoBlockMessage |
| A6 | VolumeCalculator no-flicker layout với 5s throttle | PASS | step 3.12c ApiState.refreshing variant + tick throttle 5s |
| A7 | BroadcastService coalesces cTrader delta ticks (root cause fix) | PASS | step 3.11b publish_tick fast-path + partial-merge + initial-drop |
| A8 | positions_tick payload full metadata (7 static fields) | PASS | step 3.11c + 3.11d (server + WS handler forward) |
| A9 | DELETE /api/pairs/{id} guarded khi có pending+filled orders reference | PASS | step 3.13 count_orders_by_pair + 409 pair_in_use |

## §3. Decisions trong Phase 3

Xem `DECISIONS.md` từ D-046 đến D-149 (104 quyết định Phase 3). Highlights chia theo impact:

**Order lifecycle foundations (D-058 → D-069)**:
- **D-058 / D-059** (3.4a): cTrader market orders **không** ship inline SL/TP; post-fill amend pattern là chuẩn. Foundation cho toàn bộ market order behavior sau đó.
- **D-061 → D-065** (3.4b): broker_order_id semantics (market=positionId, pending=orderId); 100ms settling delay sau ORDER_FILLED trước amend; deal.executionPrice là DOUBLE (D-032 wire scale chỉ apply ticks + trendbars).
- **D-066 → D-069** (3.4c): Close 2-event ACCEPTED→FILLED pattern; close_reason structured qua order.orderType + closingOrder + grossProfit sign; WORKFLOW exception cho `ctrader-execution-events.md` (append-only mid-phase per D-069).

**Realized P&L authoritative source (D-074)**:
- realized_pnl = `deal.closePositionDetail.grossProfit` raw int (money_digits-scaled). Server-authoritative single source of truth.

**Unrealized P&L + USD conversion (D-091 → D-098)**:
- 1s poll, formula `(current_price - entry_price) × side_mult × volume_base ÷ quote_to_usd_rate`. USD passthrough, JPY via USDJPY bid, cross via USD<QUOTE> direct hoặc inverse fallback. Stale threshold 5s.

**REST surface Phase 3 (D-099 → D-104)**:
- 6 endpoints: POST orders, GET list, GET detail, POST close, POST modify, GET positions, GET history. Full close only (D-100). modify_order None=keep / 0=remove / positive=set (D-101).

**Tick delta partial coalescing root cause + downstream (D-118 → D-120)**:
- D-118 (3.11b): BroadcastService.publish_tick coalesces partial cTrader delta ticks **at the source** (zero cache read fast path, partial-merge slow path, initial-drop edge). Root cause fix; 3.11a defensive guards retained as belt-and-suspenders (D-119).
- D-120 (3.11c): positions_tick payload kèm 7 static metadata fields **zero-cost** từ order HASH already-fetched.

**UX + frontend stale state (D-130, D-131, D-134-D-141, D-144-D-148)**:
- D-130 (3.12): Extend Phase 1 AccountStatusBar placeholder thay vì tạo file mới (avoid name collision).
- D-131 (3.12a): pairs cache hoisted MainPage useEffect single mount fetch.
- D-134-D-138 (3.12b): Order type selector Market/Limit/Stop; market auto-entry từ tickThrottled symbol-at-copy-time.
- D-139-D-141 (3.12c): tick throttle 1s → 5s; ApiState.refreshing variant holds prev result; subtle decorative dot indicator.
- D-144-D-146 (3.13): SettingsModal via gear icon; Phase 3 Exness account text-input free-form; FTMO create UI defer Phase 5.
- D-147-D-149 (3.13a): row_to_entry centralized helper (typed REST+WS single source of truth); OrderForm 3-tier ftmoBlockMessage; function-local imports để break circular.

**Phase 4 forward-looking (D-090, D-145)**:
- D-090: Status composition rule cho 2-leg orders (pending/filled/closed/half_closed/rejected/error). Locked trước khi cascade implement.
- D-145: Exness dropdown defer — text-input free-form Phase 3, widens to `<select>` Phase 4 khi accounts:exness populated.

## §4. Lệch khỏi MASTER_PLAN_v2

1. **Re-scope step 3.10 vs 3.12**: Plan gốc có step 3.10=PositionList full + step 3.12=Chart order overlay + step 3.13=Drag SL/TP open order. Thực tế phân lại:
   - 3.10 = PositionList Open + History tabs + WS reactive (orders + positions channels).
   - 3.11 = HedgeOrderForm submit (market-only).
   - 3.12 = **Account status indicator** (REST + WS broadcast + header bar) — added per CEO directive khi smoke phát hiện thiếu visibility "FTMO client có alive không?".
   - 3.13 = **Settings modal** pair CRUD + account enable/disable — added per CEO directive khi smoke phát hiện chỉ có CLI để manage pairs.
   - **Chart order overlay (R46/R47 filter, R48 3 trạng thái) + Drag SL/TP open order**: defer Phase 5 hardening cùng với drag setup line (Phase 2 kế thừa). PositionList có Modify modal thay thế cho việc drag-on-chart.

2. **16 sub-fix steps được thêm vào** (chiếm 55% commit Phase 3): 3.3a, 3.3b, 3.4a, 3.4b, 3.4c, 3.5a, 3.5b, 3.10a, 3.11a, 3.11b, 3.11c, 3.11d, 3.12a, 3.12b, 3.12c, 3.13a. Pattern bug-fix-as-sub-step kế thừa từ Phase 1+2 (acceptable per CEO). Mỗi sub-fix một commit duy nhất; root-cause focus thay vì patch chắp vá.

3. **D-090 status composition rule locked early**: Phase 4 prep decision được lock ngay trong Phase 3 (sau step 3.6 review) để Phase 4 cascade logic không cần rework. Decision documents 6 trạng thái (pending, filled, closed, half_closed, rejected, error) — Phase 4 implement.

4. **Phase 3 step plan dùng 14 step nhưng thực hiện 13 chính + 1 docs sync (split 2)**: Step 3.14 docs sync split thành 3.14a (core state) + 3.14b (tech reference + README + RUNBOOK) per WORKFLOW Option B size constraint. 30 step actions = 13 + 16 + 1 (split 2 commits = 3.14a + 3.14b).

5. **D-080 order_cancelled noise filter**: Plan gốc không lường trước cTrader auto-cancel internal STOP_LOSS_TAKE_PROFIT order khi position close → spam order_cancelled events với orderId không có trong Redis. Mid-phase added filter "ignore if no Redis match".

6. **D-098 prompt-typo**: Math typo trong prompt arithmetic example cho step 3.8. Claude Code dùng formula chuẩn correct. Lesson: prompt examples cần double-check, không phải tất cả arithmetic trong prompt là authoritative.

## §5. Sub-fix summary (16 sub-fix)

Phân theo nguyên nhân gốc:

**cTrader API behavior nuances (5 sub-fix)**:

| Sub-fix | Root cause | Fix |
|---|---|---|
| 3.4a | cTrader market order rejects inline SL/TP | Post-fill amend pattern (D-058) |
| 3.4b | Execution event parsing: positionId vs orderId; deal.executionPrice double vs int; amend POSITION_NOT_FOUND | broker_order_id semantic split (D-061); price = double raw (D-064); 100ms settling delay (D-063) |
| 3.4c | Close = single event assumption; close_reason inference unstable | 2-event sequence ACCEPTED→FILLED (D-066); seed ctrader-execution-events.md (D-069) |
| 3.5a | close_reason inference qua price tolerance bị unstable cho price near-stop scenarios | Inference qua order.orderType + closingOrder + grossProfit sign (D-071) |
| 3.5b | No reconciliation infrastructure — close events offline lost forever | ReconcileReq + DealList + fetch_close_history với retry (D-076, D-077, D-079) |

**Frontend stale state / type alignment (3 sub-fix)**:

| Sub-fix | Root cause | Fix |
|---|---|---|
| 3.10a | WS "orders" channel chưa whitelist server-side; subscribe error | VALID_CHANNEL_PREFIXES += "orders" (D-109) |
| 3.11a (pair portion) | PairPicker selectedPairId stale từ Phase 2 localStorage không có trong fetched list → 404 pair_not_found khi submit | Membership check + auto-select first pair (D-114) |
| 3.13a | WS account_status payload ship `enabled: "false"` (string) → JS `Boolean("false") === true` → UI render ON cho disabled accounts | row_to_entry helper centralized cho REST + WS single source of truth (D-147) |

**Tick partial data: root cause + defensive guards (3 sub-fix)**:

| Sub-fix | Root cause | Fix |
|---|---|---|
| 3.11a (defensive portion) | cTrader delta ticks có bid=None hoặc ask=None tùy update → crash spam `_compute_pnl(float(None))` | Defensive guard: ValueError raise → WARNING log + loop continue (D-116, D-117) |
| 3.11b (root cause) | Delta ticks không coalesce với prev state ở BroadcastService boundary | publish_tick coalesces với last cached tick (D-118); defensive guards retained belt-and-suspenders (D-119) |
| 3.11d | WS handler stripped 6 fields trước upsertPositionTick → new rows render empty cells until REST refresh | Spread + post-spread coercions (D-124); WsPositionsTickMessage type extended (D-123) |

**UX polish (4 sub-fix)**:

| Sub-fix | Root cause | Fix |
|---|---|---|
| 3.11c | positions_tick payload thiếu static metadata → new fill rows render empty until REST | 7 static fields zero-cost from order HASH (D-120); upsert prepend (D-121) |
| 3.12a | PositionRow + OrderRow show pair_id UUID khi operator cần pair name | pairs cache hoist MainPage + lookupPairName helper với truncated-UUID fallback (D-131, D-132) |
| 3.12b | Submit ships market-only; Limit/Stop backend ready nhưng UI selector chưa có | Order type selector + market-mode auto-entry từ throttled tick (D-134, D-135) |
| 3.12c | VolumeCalculator flicker mỗi 1s do swap "Calculating..." ↔ result block | ApiState.refreshing variant holds prev result (D-140); 1s → 5s throttle (D-139) |

**Build/install (2 sub-fix early phase)**:

| Sub-fix | Root cause | Fix |
|---|---|---|
| 3.3a | hedger_shared package thiếu py.typed marker → mypy bypass strict cross-boundary | Add py.typed (D-052) |
| 3.3b | ftmo-client install path không khớp monorepo pattern (deps duplication) | `--no-deps` + explicit deps |

## §6. Smoke Test Summary

End-to-end smoke verified trên FTMO cTrader free trial (ctid_trader_account_id=47247733) trong các session sau:

**Step-level smoke (29 self-check files trong repo root chứa sample dry-runs chi tiết)**:
- Order submit Market + Limit + Stop work end-to-end (frontend → REST → cmd_stream → FTMO client → cTrader → resp_stream → response_handler → WS → frontend reactive).
- Live P&L update mỗi 1s với USD conversion (JPY pairs via USDJPY bid; verified với USDJPY position; cross + commodity Phase 5).
- Close + modify via UI button + cmd_stream dispatch + WS broadcast.
- History tab populates từ closed orders (REST refresh on tab switch).
- Pair management (create/update/delete) via Settings modal; delete guarded khi reference exists.
- Account enable/disable toggle via Settings modal → status disabled flag + OrderForm submit gate.
- WS reactive: order_updated + positions_tick + account_status broadcasts received và dispatched đúng.

**Smoke artifacts**:
- 29 self-check files (repo root, pattern `step-3.*-selfcheck.md`).
- Live positions positionId từ CEO smoke sessions visible trong cTrader UI history.
- Tests: 473 server (mypy strict + ruff clean) + 177 ftmo-client.

## §7. Files Changed (Phase 3 cumulative)

Phase 3 ~30 commits substantive + 1 docs sync. Net file impact (so với `step-3.1~1`):

- **server/app/**: ~14 new files / ~10 modified. New: api/orders.py, api/positions.py, api/history.py, api/accounts.py, services/order_service.py, services/response_handler.py, services/event_handler.py, services/position_tracker.py, services/account_status.py, services/account_helpers.py. Modified: api/ws.py, api/pairs.py, services/redis_service.py, services/broadcast.py, main.py, dependencies/auth.py.
- **server/tests/**: ~12 new files. New: test_orders_api.py, test_orders_list_api.py, test_orders_close_modify_api.py, test_positions_api.py, test_history_api.py, test_response_handler.py, test_event_handler.py, test_position_tracker.py, test_broadcast_coalesce.py, test_accounts_api.py, test_account_status_loop.py, test_account_helpers.py, test_redis_service.py, test_lifespan_handlers.py, test_lifespan_integration.py.
- **apps/ftmo-client/ftmo_client/**: ~12 new files. NEW package: bridge_service.py, action_handler.py, command_processor.py, event_processor.py, account_info.py, reconciliation.py, ctrader_protobuf_helpers.py, heartbeat.py, shutdown.py, main.py.
- **apps/ftmo-client/tests/**: ~12 new files matching the module structure.
- **shared/hedger_shared/**: ctrader_oauth.py extracted (Phase 2 server-only → Phase 3 shared); + py.typed marker.
- **web/src/**: ~10 new components (PositionList/*, Settings/*) + Header/AccountStatusBar.tsx (rewrite); ~6 modified (api/client.ts, store/index.ts, hooks/useWebSocket.ts + useTickThrottle.ts NEW, components/MainPage.tsx, components/OrderForm/HedgeOrderForm.tsx, components/Chart/HedgeChart.tsx, components/Header/Header.tsx). NEW: lib/pairHelpers.ts.
- **docs/**: 1 file modified mid-phase per D-069 — `ctrader-execution-events.md` (append-only knowledge file). 3.14a touches PROJECT_STATE.md, DECISIONS.md, MASTER_PLAN_v2.md, PHASE_3_REPORT.md (NEW). 3.14b touches 05-redis-protocol.md, 06-data-models.md, 07-server-services.md, 08-server-api.md, 09-frontend.md, README, RUNBOOK.
- **scripts/**: minimal — possibly `ops_init_account.py` (step 3.2).

## §8. Phase 4 Prerequisites Checklist

Phase 4 = Exness MT5 client + hedge cascade close (parallel pattern to Phase 3 FTMO). Phase 4 không thể start cho đến khi:

- [x] FTMO client production-stable across full Phase 3 smoke matrix.
- [x] BroadcastService coalescing pattern documented (3.14b sẽ document trong 07-server-services.md).
- [x] response_handler + event_handler patterns extensible to second broker (adapter-layer abstraction trong step 3.7).
- [x] Phase 4 status composition rule confirmed (D-090 lock).
- [x] cTrader execution event docs production-ready (mid-phase append per D-069).
- [ ] CEO chuẩn bị Exness MT5 credentials + máy Windows local cho dev/test (MT5 lib Windows-only).
- [ ] Symbol mapping JSON có entry FTMO ↔ Exness cho test pair (vd EURUSD ↔ EURUSDm, USDJPY ↔ USDJPYm).

Phase 4 starts với: `step/4.1-exness-client-skeleton` matching step 3.3 FTMO pattern.

## §9. Phase 5 Hardening Backlog (kế thừa từ working memory)

**Server**:
- Per-account/per-pair SET index cho orders (list_orders performance ở scale lớn; pair_orders:{pair_id} SET update trong create_order + status-transition Lua).
- Idempotency-Key header cho POST endpoints.
- Dead-letter sweeper cho stuck consumer group PEL.
- Migrate Phase 2 pair rows: set enabled=true explicitly (D-085).
- Fix pre-existing test_config.py mypy errors.
- pyproject.toml pythonpath consolidation.
- p_money_digits fallback từ account:ftmo:{acc}.money_digits (khi position_cache thiếu).
- sync_symbols persist contract_size + quote_currency cho non-FX symbols (D-094, D-095).
- Auto-subscribe USD-cross tick streams khi first position open trên non-USD-quote.
- Consolidate position:{id} JSON với position_cache:{id} HASH.

**FTMO client**:
- Protocol-level disconnect trước TCP close (shutdown order).
- disconnect() cancel _pending_executions với timeout.
- Retry amend sau POSITION_LOCKED (D-060).
- closePositionDetail.moneyDigits vs account-level reconcile.
- ProtoOAMarginCallTriggerEvent cho stopout close_reason.
- Pending orders reconciliation full handling.
- hasMore pagination DealListByPositionIdRes.

**Frontend**:
- Vitest + React Testing Library setup (Phase 3 deferred consistently across 8+ steps).
- Custom ConfirmModal thay window.confirm (Phase 3 delete + close confirms).
- Typed API client với conversion layer (giảm cast bool/str ad-hoc).
- Optimistic UI cho Close + Modify.
- History pagination UI.
- WS tick dedup nếu bandwidth concern.
- Multi-draft order form (multiple tabs).
- PairPicker toast on stale re-select (D-114 hiện silent).
- Row click selection + chart overlay (entry/SL/TP overlay cho selected order — Phase 2+3 backlog hợp nhất).
- Banker's rounding exotic symbols (D-122 mở rộng).
- Memoize Map<order_id, Order> selector (D-133 join performance).
- Hybrid UUID display first-4+last-4 (D-132 mở rộng cho collision-resistance).
- Reactive pairs cache refresh sau CRUD (3.12a hiện mount-once fetch).
- Initial-state observability: one-shot WARN per (symbol, process) khi tickThrottled lần đầu null.
- TTL refresh on tick cache stale prev edge (D-118 hardening).
- entryPrice subscriber memoization nếu widgets multiplied (3.12b).
- Distance-from-market sanity check broker-aware (3.12b Limit/Stop hardening; broker-specific N% threshold trong symbol_config).
- Escape-to-close on SettingsModal (D-144 deferred per spec).

**Operations**:
- Document `uvicorn --reload` flag trong RUNBOOK (3.14b).
- Document FTMO client restart workflow (3.14b).
- Browser hard refresh checklist sau frontend merge.
- Multi-FTMO mixed-state OrderForm message refinement (D-148 hiện 3-tier; mixed states Phase 5).
- Extract Pydantic schemas sang `app/api/schemas/` để break D-149 function-local imports cleanly.

## §10. Phase 3 Step Ledger

| Step | Branch | Commit (main) | Scope (1-line) |
|---|---|---|---|
| 3.1 | step/3.1-server-redis-service-full | c09ce73 | RedisService đầy đủ CRUD + Lua CAS update |
| 3.2 | step/3.2-server-consumer-groups-setup | 6d67a2e | lifespan setup_consumer_groups + init_account script |
| 3.3 | step/3.3-ftmo-client-skeleton | beb72f4 | ftmo-client skeleton + OAuth extraction shared lib |
| 3.3a | step/3.3a-shared-pytyped-marker | 0f46f88 | py.typed marker cho hedger-shared + remove type-ignore silencers |
| 3.3b | step/3.3b-ftmo-client-install-fix | 7a1b108 | ftmo-client install fix (--no-deps + explicit deps) |
| 3.4 | step/3.4-ftmo-client-actions | 129c10e | FTMO client real action handlers (open/close/modify) |
| 3.4a | step/3.4a-market-order-amend-sltp | 1765fba | Market order SL/TP via post-fill amend (D-058) |
| 3.4b | step/3.4b-fix-execution-event-parsing | 12ae377 | Execution event parsing (positionId vs orderId) + 100ms settling delay |
| 3.4c | step/3.4c-close-2-event-and-ctrader-doc | 381e983 | Close 2-event + seed docs/ctrader-execution-events.md |
| 3.5 | step/3.5-ftmo-client-events-and-account | a87cb48 | Unsolicited execution events + account_info_loop |
| 3.5a | step/3.5a-close-reason-via-order-metadata | 2c2c5aa | close_reason via order.orderType + grossProfit sign |
| 3.5b | step/3.5b-ftmo-client-reconciliation | d5cea29 | Reconciliation (ReconcileReq + DealList + fetch_close_history) |
| 3.6 | step/3.6-server-orders-api | 35753d4 | POST /api/orders endpoint + order_service.create_order |
| 3.7 | step/3.7-server-response-event-handlers | 0065721 | response_handler + event_handler + reconciliation consume + WS broadcast |
| 3.8 | step/3.8-position-tracker | c01dec5 | position_tracker + unrealized P&L compute + WS broadcast batch |
| 3.9 | step/3.9-orders-positions-history-api | 615d490 | REST API orders/positions/history + close/modify actions |
| 3.10 | step/3.10-web-orders-positions-history-shell | 8f5df28 | Web frontend wire orders/positions/history to backend API + WS |
| 3.10a | step/3.10a-ws-orders-channel-whitelist | 707bf8a | WS orders channel whitelist fix |
| 3.11 | step/3.11-web-order-form-submit-wiring | 42af40e | HedgeOrderForm submit wiring to POST /api/orders (market-only) |
| 3.11a | step/3.11a-pair-picker-validate-and-precision-and-tick-defensive | acd6447 | PairPicker stale validate + SL/TP precision normalize + position_tracker None tick defensive |
| 3.11b | step/3.11b-market-data-tick-coalescing | da0a4cb | BroadcastService coalesces cTrader delta ticks (ROOT CAUSE FIX) |
| 3.11c | step/3.11c-positions-tick-full-metadata-and-upsert | cf18bd6 | positions_tick full metadata + frontend upsert insert |
| 3.11d | step/3.11d-ws-handler-forward-metadata | 14f6ec3 | WS handler forward 7 metadata fields |
| 3.12 | step/3.12-account-status-indicator | 478e37f | Account status indicator + REST + WS broadcast + frontend header bar |
| 3.12a | step/3.12a-pair-name-in-position-and-order-rows | 53bbf15 | Pair column in PositionRow + OrderRow + hoist pairs fetch |
| 3.12b | step/3.12b-order-type-selector-and-market-tick-entry | 0460936 | Order type selector + market mode auto-entry từ throttled tick |
| 3.12c | step/3.12c-volume-calculator-no-flicker-and-5s-throttle | 82154c4 | VolumeCalculator stable layout + 5s throttle |
| 3.13 | step/3.13-settings-modal-pair-account-management | 143f815 | Settings modal pair CRUD + account toggle |
| 3.13a | step/3.13a-account-status-payload-type-fix-and-offline-tooltip-clarify | 227f589 | WS account_status payload typed entries + tooltip clarify |
| 3.14a | step/3.14a-phase-3-docs-sync-core | (this commit) | Phase 3 docs sync part 1 — PROJECT_STATE + DECISIONS + PHASE_3_REPORT + MASTER_PLAN_v2 |
| 3.14b | step/3.14b-phase-3-docs-sync-tech | (pending) | Phase 3 docs sync part 2 — tech reference docs (05-redis, 06-data, 07-services, 08-api, 09-frontend) + README + RUNBOOK |
