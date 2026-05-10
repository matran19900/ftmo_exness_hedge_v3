# Phase 2 — Báo cáo hoàn thành

> Thời điểm tạo: 2026-05-09
> Tag dự kiến: `phase-2-complete` (CEO sẽ tag thủ công sau khi merge step 2.10)
> Thời lượng thực tế: từ commit `b6067ac` (step 2.1, 2026-05-07) đến commit `d164b1f` (step 2.9b, 2026-05-09) — **3 ngày**, gồm 21 commit (10 step chính + 11 sub-fix).

## Acceptance criteria — kết quả

Acceptance Phase 2 theo `MASTER_PLAN_v2` Section 3:

| # | Test | Kết quả | Bằng chứng |
|---|---|---|---|
| 1 | Server start → cTrader connect OK | PASS | step 2.1 smoke (CEO verify), OAuth callback + symbol sync 91 symbol cache Redis |
| 2 | Chọn EURUSD → 200 historical candles + tick chạy + candle update real-time | PASS | step 2.7 + 2.7c + 2.7e smoke |
| 3 | Đổi USDJPY → digits change qua applyOptions, không recreate chart | PASS | step 2.7a (digits từ symbol_config + applyOptions priceFormat) |
| 4 | Right-click → "Set as Entry" → form Entry update + dashed line vẽ | PASS | step 2.9 smoke 17/17 |
| 5 | Drag dashed Entry line → form Entry update theo | DEFERRED | drag price line hoãn sang Phase 5 hardening (CEO directive). Setup line vẫn vẽ được, chỉ không drag được. |
| 6 | Volume calc: Risk + Entry + SL + BUY EURUSD → volume đúng | PASS | step 2.4 server endpoint + step 2.9 frontend integration; 19 test pass |
| 7 | Test USDJPY (JPY conversion) + XAUUSD (commodity contract size) | PASS | step 2.4 test_volume_calc 19 case (gồm USDJPY, XAUUSD, BTCUSD) |
| 8 | Conversion rate cache: lần đầu chậm, lần 2 trong 24h <50ms | SỬA LẠI | dùng `tick:*` 60s cache thay vì `rate:*` 24h cache (D-034). Đơn giản hơn, đủ tươi. |
| 9 | Submit form → toast "Phase 3", form không clear | SỬA LẠI | submit button **disabled** với title="Phase 3" thay vì toast (CEO directive step 2.8). Logic submit handler thuộc Phase 3.x. |
| 10 | Đổi symbol → WS subscribe diff đúng (DevTools) | PASS | step 2.7 useWebSocket + set_symbol auto-resend + clear latestTick |

**Acceptance bổ sung phát sinh trong Phase 2** (CEO yêu cầu):

| # | Test | Kết quả | Bằng chứng |
|---|---|---|---|
| A1 | Live tick price line bid/ask trên chart | PASS | step 2.7 |
| A2 | Live candle update từ tick stream (không chỉ từ server candle_update) | PASS | step 2.7c + 2.7e (single source of truth: tick) |
| A3 | Per-symbol Y-axis precision (5/3/2 digits) | PASS | step 2.7a |
| A4 | Pair picker từ `/api/pairs/` | PASS | step 2.8 |
| A5 | Layout OrderForm full-height, PositionList 70% | PASS | step 2.8a (D-042 override D-029) |
| A6 | Soft validation Entry/SL/TP + clear `×` button | PASS | step 2.9a |
| A7 | Manual volume override + Vol P editable, Vol S derived | PASS | step 2.9a + 2.9b |
| A8 | Side-direction HARD BLOCK (BUY: SL<Entry, SELL: SL>Entry) + soft TP warning | PASS | step 2.9b + `lib/orderValidation.ts` |
| A9 | Reset Entry/SL/TP/manualVolume khi đổi symbol | PASS | step 2.9a (prevSymbolRef guard) |
| A10 | sync_symbols batch fetch (~90s → ~1s) | PASS | step 2.7b (single ProtoOASymbolByIdReq với repeated symbolId) |
| A11 | volumeReady flag cho Phase 3 submit gate | PASS | step 2.9b |

## Decisions trong Phase 2

Xem `DECISIONS.md` từ D-031 đến D-045 (15 quyết định Phase 2).

Highlights:
- **D-032**: cTrader gửi raw integer prices uniformly scaled by 10^5 cho mọi instrument, `digits` chỉ là display.
- **D-033**: WS auth fail trả HTTP 403 (FastAPI/Starlette pre-accept) thay vì WS close 4401.
- **D-034**: Bỏ `rate:*` 24h cache — dùng `tick:*` 60s cache trực tiếp.
- **D-035**: Lightweight Charts v5 (5.2.x) thay v4 — API breaking `chart.addSeries(SeriesDef, opts)`.
- **D-039**: sync_symbols batch fetch — một ProtoOASymbolByIdReq với repeated symbolId.
- **D-041**: WS candle_update KHÔNG redraw in-bar — tick stream là single source of truth (root cause flicker fix).
- **D-042**: Layout override D-029 — OrderForm full-height right column, PositionList 70%.
- **D-045**: Side direction HARD BLOCK cho SL violation; volumeReady flag cho Phase 3.

## Lệch khỏi MASTER_PLAN_v2

1. **Re-scope step 2.7 ↔ step 2.9**: Plan gốc có 2.7=chart interactions + 2.9=volume calc + WS. Thực tế phân lại:
   - 2.7 = useWebSocket + live tick + live candle (data foundation).
   - 2.8 = order form UI (pair, side, inputs).
   - 2.9 = chart-form integration (setup lines + context menu + volume calc + WS already done in 2.7).
   Lý do: WS hook + live data cần chạy trước khi build interactions trên đó. Plan gốc nhầm thứ tự dependency.

2. **11 sub-fix steps được thêm vào** (chiếm 52% commit Phase 2): 2.1a, 2.1b, 2.2a, 2.6a, 2.7a, 2.7b, 2.7c, 2.7e, 2.8a, 2.9a, 2.9b. Pattern bug-fix-as-sub-step được CEO confirm acceptable từ Phase 1 (1.4a). Mỗi sub-fix một commit duy nhất, chứng minh root-cause focus thay vì patch chắp vá.

3. **step 2.7d KHÔNG merge**: branch `step/2.7d-raf-throttle-live-candle` (RAF throttle) làm xong nhưng REJECT sau khi 2.7e (root cause fix) chạy đủ smooth. RAF throttle không cần thiết khi root cause đã được fix.

4. **Drag price line hoãn sang Phase 5**: Plan gốc có "drag setup line" trong Phase 2. CEO directive: chỉ làm nếu có thời gian; mặc định hoãn để focus root cause flicker + form polish. Setup line vẫn vẽ được + reactive với form, chỉ không drag được trên chart.

5. **Submit handler stays disabled**: button disabled với title="Phase 3 will implement". Plan gốc có "submit form → toast Phase 3"; thực tế disabled cleaner hơn cho CEO smoke test.

6. **Server tests đếm 110 (không phải 107 như spec dự kiến)**: 107 ở step 2.5 + 3 test mới ở step 2.7b (`test_sync_symbols_batches_detail_request`, `test_sync_symbols_skips_symbols_missing_from_batch_response`, `test_sync_symbols_returns_zero_when_batch_fails`).

## Sub-fix summary

| Sub-fix | Root cause | Fix |
|---|---|---|
| 2.1a | cTrader OAuth callback không echo `state` param | Bỏ state CSRF check (D-031) |
| 2.1b | Server response trong wrapper `ProtoMessage` không có `.symbol` attribute | Auto-unwrap qua `Protobuf.extract()` ở `_send_and_wait` |
| 2.2a | OHLC USDJPY hiển thị giá 100x quá lớn | cTrader scale price uniformly 10^5 cho mọi instrument; `digits` là display-only (D-032) |
| 2.6a | Crosshair v5 default Magnet snap khó set Entry/SL/TP chính xác | `crosshair: { mode: CrosshairMode.Normal }` (D-036) |
| 2.7a | (1) Default candle-series `priceLineVisible` clutters chart; (2) Y-axis 2-digit không phù hợp FX 5-digit | `lastValueVisible: false` + `priceLineVisible: false` + per-symbol `priceFormat.precision/minMove` từ `digits` field (D-037, D-038) |
| 2.7b | sync_symbols 91 sequential RTT → ~90s mỗi server restart | Single batch ProtoOASymbolByIdReq với all matched ids; ~1-3s (D-039) |
| 2.7c | Last candle không cập nhật giữa server `candle_update` broadcasts | Tick stream patches close/high/low của bar tracked (D-040) |
| 2.7e | Chart flicker: 2 sources redraw cùng bar (server + tick) | WS handler không call `series.update()` cho in-bar; chỉ sync ref O/H/L; tick stream là single source of truth (D-041) |
| 2.8a | OrderForm 380px width không đủ chỗ cho Pair + Side + Entry/SL/TP/Risk + Volume preview | Layout 2 column: left 70% (chart + positions stacked), right 30% (OrderForm full-height) (D-042) |
| 2.9a | (1) Form state retained on symbol switch không hợp lý — giá EURUSD vô nghĩa với USDJPY; (2) Cần soft validation; (3) Cần clear button; (4) Cần manual volume override | Symbol-switch effect reset entry/SL/TP/manualVolume (D-043); inline warnings; `×` clear button; manual mode (D-044) |
| 2.9b | (1) Manual mode mất SL pip + Est. SL $; (2) Side validation chỉ là warning, không block | SL pip + Est. SL $ shown khi `state.result` available (price-derived); side direction HARD BLOCK SL violation (D-045) |

## Phase 5 hardening backlog

Việc deferred trong Phase 2:

1. **Drag price line cho setup lines + open order SL/TP**: `chart.priceScale().coordinateToPrice()` + `priceToCoordinate()` API có sẵn; design pattern ~80-150 lines per docs/09-frontend.md. Hoãn vì root cause flicker fix (2.7e) ưu tiên hơn.

2. **WS auth fail trả HTTP 403 thay vì WS close 4401**: FastAPI/Starlette validate token trước khi `accept()` WS, nên client thấy 403 HTTP response thay vì close code 4401. Đã chấp nhận (D-033). Phase 5 revisit nếu frontend reconnect logic cần phân biệt close code.

3. **sync_symbols 24h Redis cache**: ~1s sau batch fix (2.7b) đã đủ nhanh cho mỗi server restart. Cache 24h sẽ làm instant nhưng không critical.

4. **Toast missing cho session-expired + network-error**: tồn tại từ Phase 1, vẫn chưa fix (PHASE_1_REPORT đã ghi).

5. **Telegram wrapper script `claude-with-notify.sh` broken**: TTY pipe issue (D-019). Workaround: `claude --dangerously-skip-permissions` direct.

6. **BUY/SELL arrows trên setup lines**: `docs/09-frontend.md` section 6.3 spec; chưa làm. Visual polish.

7. **Measure tool, multi-timeframe live trendbar per symbol, WS reference counting trên subscriptions**: Phase 5 nice-to-have.

8. **Multi-currency conversion rate edge cases**: exotic currencies không có trong broker symbol list silently trả 0.0; UI hiện 503. Chấp nhận được vì 99% pairs CEO trade là FX/JPY/XAU/index/crypto chính.

9. **Volume Secondary editable**: Phase 2 chỉ cho Vol P edit (Vol S derived). Phase 5 nếu CEO muốn split independent.

10. **Ratio per pair**: Phase 2 hardcode `ratio: 1.0` trong `calculateVolume()`. Phase 4 sẽ đọc `pair.ratio` từ PairPicker selection.

11. **Setup-line-on-chart vs cleared-form race**: nếu user clear form bằng F5 nhanh hơn chart unmount, setup lines có thể mồ côi 1 frame. Không trigger trong smoke; refactor sang derived rendering nếu cần.

## Step ledger

| Step | Branch | Commit | Date | Scope (1-line) |
|---|---|---|---|---|
| 2.1 | `step/2.1-server-ctrader-bridge-and-symbols` | `b6067ac` | 2026-05-07 | cTrader Twisted bridge + OAuth flow + symbol sync filter whitelist cache Redis |
| 2.1a | `step/2.1a-fix-oauth-state-param` | `12633af` | 2026-05-07 | Sub-fix: bỏ state CSRF check (cTrader không echo) |
| 2.1b | `step/2.1b-fix-protobuf-unwrap` | `79d2435` | 2026-05-07 | Sub-fix: auto-unwrap `ProtoMessage` qua `Protobuf.extract()` |
| 2.2 | `step/2.2-server-charts-ohlc` | `0702a6b` | 2026-05-08 | `GET /charts/:symbol/ohlc` + Redis 60s cache |
| 2.2a | `step/2.2a-fix-ohlc-price-scale` | `fe7a463` | 2026-05-08 | Sub-fix: uniform 10^5 scale (digits chỉ là display) |
| 2.3 | `step/2.3-server-ws-ticks-candles` | `316dd82` | 2026-05-08 | WS endpoint + tick + candle broadcast + diff subscribe |
| 2.4 | `step/2.4-server-rate-and-volume-calc` | `01e7eff` | 2026-05-08 | Conversion rate (`tick:*` 60s) + `POST /symbols/:sym/calculate-volume` |
| 2.5 | `step/2.5-server-pairs-api` | `8e47ef4` | 2026-05-08 | Pairs CRUD endpoints |
| 2.6 | `step/2.6-web-chart-base` | `eb09a62` | 2026-05-08 | HedgeChart + symbol picker + timeframe selector + Lightweight Charts v5 |
| 2.6a | `step/2.6a-fix-crosshair-magnet` | `9a94344` | 2026-05-08 | Sub-fix: crosshair Normal mode (free cursor cho set Entry/SL/TP) |
| 2.7 | `step/2.7-web-ws-live-data` | `0663397` | 2026-05-08 | useWebSocket hook + live tick price line bid/ask + live candle |
| 2.7a | `step/2.7a-fix-chart-priceline-precision` | `8bb461a` | 2026-05-08 | Sub-fix: hide default price line + per-symbol priceFormat |
| 2.7b | `step/2.7b-sync-symbols-batch` | `29bd215` | 2026-05-08 | Sub-fix: sync_symbols batch fetch (~90s → ~1s) |
| 2.7c | `step/2.7c-live-candle-from-tick` | `d355522` | 2026-05-08 | Sub-fix: live candle close patch từ tick stream |
| 2.7d | `step/2.7d-raf-throttle-live-candle` | _(rejected)_ | 2026-05-08 | RAF throttle — REJECT sau khi 2.7e fix root cause |
| 2.7e | `step/2.7e-sync-server-ohl-no-redraw` | `ebb88cb` | 2026-05-08 | Sub-fix: WS candle handler không redraw in-bar (root cause flicker) |
| 2.8 | `step/2.8-web-order-form-ui` | `b7a247e` | 2026-05-08 | OrderForm full UI: PairPicker + Symbol read-only + Side + Entry/SL/TP + Risk + Volume preview placeholder |
| 2.8a | `step/2.8a-layout-orderform-fullheight` | `7709fb1` | 2026-05-08 | Sub-fix: layout 2 column (OrderForm full-height, PositionList 70%) |
| 2.9 | `step/2.9-web-chart-form-integration` | `a98f9a0` | 2026-05-08 | Setup lines (Entry/SL/TP) + ChartContextMenu + debounced VolumeCalculator |
| 2.9a | `step/2.9a-form-enhancements` | `13a7860` | 2026-05-09 | Sub-fix: reset on symbol switch + soft validation + `×` clear + manual volume override |
| 2.9b | `step/2.9b-volume-metrics-and-side-validation` | `d164b1f` | 2026-05-09 | Sub-fix: SL pip + Est. SL $ shown trong manual mode + side direction HARD BLOCK + volumeReady flag |
| 2.10 | `step/2.10-phase-2-docs-sync` | _(this commit)_ | 2026-05-09 | Phase 2 docs sync: PHASE_2_REPORT + D-032..D-045 + PROJECT_STATE refresh |

## Smoke test summary

| Step | Smoke pass | Note |
|---|---|---|
| 2.1 | PASS | cTrader OAuth + sync 91 symbol từ broker |
| 2.2 | PASS | OHLC EURUSD/USDJPY/XAUUSD; Redis cache hit <50ms |
| 2.3 | 10/11 | WS auth fail trả 403 thay vì 4401 (chấp nhận, D-033) |
| 2.4 | 19/19 | volume calc test cho FX/JPY/XAU/BTC/index |
| 2.5 | PASS | pairs CRUD: GET/POST/PATCH/DELETE |
| 2.6 | PASS | chart historical EURUSD M15 200 candles |
| 2.6a | PASS | crosshair Normal mode (free cursor) |
| 2.7 | PASS | WS connect + tick + candle live; reconnect |
| 2.7a | PASS | hide default price line + 5/3/2 digit Y-axis |
| 2.7b | PASS | sync_symbols ~1s sau batch fix |
| 2.7c | PASS | tick stream patch live candle close |
| 2.7d | _(rejected)_ | RAF throttle không cần thiết sau 2.7e |
| 2.7e | PASS | flicker fix — single source of truth: tick |
| 2.8 | 12/12 | order form UI; PairPicker, Side, inputs |
| 2.8a | PASS | layout 2 column |
| 2.9 | 17/17 | setup lines + context menu + volume calc |
| 2.9a | PASS | reset on symbol switch + validation + clear + manual override |
| 2.9b | PASS | side block + manual metrics + volumeReady |

## Files added/modified summary

### New folders trong Phase 2
- `web/src/hooks/` — `useWebSocket.ts`, `useDebouncedValue.ts`
- `web/src/lib/` — `orderValidation.ts`
- `server/app/services/` (existed) — added `market_data.py`, `broadcast.py`, `volume_calc.py`

### Stats commit (đo bằng `git log b6067ac..HEAD`)
- Tổng commit Phase 2 (gồm cả step 2.10): **22** (10 step chính + 11 sub-fix + 1 doc sync). step 2.7d không merge (branch local-only).
- Server tests: **110** (`pytest --collect-only`). Phase 1 cuối: 27 → +83 trong Phase 2.
- Frontend tests: 0 (Vitest setup hoãn — như Phase 1).
- Mypy strict: 22 source files. 3 errors pre-existing về `hedger_shared.symbol_mapping` import path (env-only).
- Bundle web/dist (sau step 2.9b):
  - JS: **435.31 kB** / **140.48 kB gzip**
  - CSS: **13.75 kB** / **3.47 kB gzip**
  - Phase 1 cuối: 253.47 kB JS / 8.32 kB CSS → +181 kB JS (lightweight-charts ~120 kB + form/chart UI ~60 kB).

## Files modified xuyên suốt Phase 2
- `README.md`: 4 verify section mới (chart, live data, order form UI, chart-form, side validation).
- `web/vite.config.ts`: WS proxy `/ws` với `ws: true`.
- `web/src/store/index.ts`: thêm 9 field runtime + `OrderSide` type.
- `web/src/api/client.ts`: thêm types cho WS messages, OHLC, pairs, calculate-volume.
- `web/src/components/Chart/HedgeChart.tsx`: trung tâm Phase 2 — xuyên suốt 2.6, 2.6a, 2.7, 2.7a, 2.7c, 2.7e, 2.9.

## Next phase prerequisites checklist

Trước khi bắt đầu Phase 3 (Single-leg Trading FTMO only), CEO cần:

- [ ] **FTMO live account credentials** + cTrader OAuth credentials cho FTMO.
  - Phase 2 dùng cTrader **demo** chỉ cho market data. Phase 3 cần FTMO **live** để execute real orders.
- [ ] **Setup `.env` cho FTMO client** (sẽ ship trong step 3.3).
- [ ] **Add account FTMO qua Redis CLI** (sau khi server Phase 3 ready):
  ```bash
  redis-cli HSET account:ftmo:ftmo_acc_001 type ftmo broker ftmo currency USD
  redis-cli SADD accounts:ftmo ftmo_acc_001
  ```
- [ ] **Server restart** sau khi add account → `setup_consumer_groups()` tạo consumer group.
- [ ] **Test position size cực nhỏ** (vd 0.01 lot) khi smoke Phase 3 — tiền thật chịu rủi ro (D-009).
- [ ] **Verify symbol whitelist có symbol CEO muốn trade** (đã verify ở Phase 1: EURUSD, USDJPY, XAUUSD, US30, BTCUSD trong 117 mapping).
