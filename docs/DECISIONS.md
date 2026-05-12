# DECISIONS

> Sổ ghi tích lũy các quyết định kiến trúc và vận hành xuyên suốt mọi phase.
> Đọc top-down để theo trình tự thời gian. Mỗi quyết định có ID `D-NNN` không tái sử dụng.

## Pre-Phase 1 (planning + architecture)

### D-001 — Lập kế hoạch theo vertical slice thay vì module-by-module
Lý do: 3 mục tiêu cốt lõi (đồng bộ 2 chân, cascade close, P&L USD) đều cross-module. Build từng module độc lập sẽ dồn rủi ro tích hợp vào cuối dự án. Vertical slice giúp CEO verify từng phase end-to-end.
Trade-off: mỗi module sẽ bị "động chạm" nhiều lần qua các phase thay vì hoàn thiện một lần.

### D-002 — Symbol mapping là file JSON tĩnh
Lý do: nguồn sự thật ship cùng repo, load lúc startup, không có UI chỉnh sửa runtime.
Trade-off: thay đổi schema phải redeploy + restart server. Chấp nhận được với công cụ single-admin.

### D-003 — Cascade trigger bất đối xứng: cTrader execution event vs MT5 poll
Lý do: cTrader Open API có execution event stream native; MT5 manager API phải poll.
Trade-off: phía Exness latency cao hơn (poll 2s) so với phía FTMO. Chấp nhận được vì FTMO leg là leg dẫn dắt strategy.

### D-004 — Risk ratio là một số global per-pair, áp dụng SAU bước symbol-map volume conversion
Công thức: `volume_exness = volume_ftmo × (ftmo_units_per_lot / exness_trade_contract_size) × risk_ratio`
Lý do: tách bạch hai concern — symbol-map xử lý unit conversion, risk_ratio xử lý chiến lược position sizing.

### D-005 — Conversion rate cache: Redis 24h TTL với subscribe-on-miss
Lý do: hầu hết cặp có giá trị USD ổn định trong 24h. Subscribe spot chỉ khi cache miss giúp giảm tải broker API.
Trade-off: rate có thể stale tới 24h. Chấp nhận được cho hiển thị P&L (không dùng cho execution).

### D-006 — Redis cùng máy với server, mọi phase
Lý do: đơn giản. Latency gần 0. Công cụ single-admin với <10 thao tác đồng thời không cần Redis riêng.
Trade-off: server crash = mất state in-memory của Redis. Mitigate bằng AOF persistence + script backup.

### D-007 — Mạng: VPN mesh Tailscale giữa server và client
Lý do: free tier đủ dùng (3 user, 100 thiết bị). Mã hóa mặc định. Đơn giản hơn self-hosted VPN.
Trade-off: phụ thuộc Tailscale. Rủi ro chấp nhận được.

### D-008 — Đăng ký client driven bởi server qua Redis consumer groups
Lý do: thêm `account_id` trong UI Settings → server tạo Redis consumer group → client kết nối với cùng `account_id` và pickup command. Không cần HTTP handshake.
Trade-off: server lúc startup phải biết tất cả `account_id` trước. Thêm account mới phải restart server (hoặc dùng API runtime ở Phase 4).

### D-009 — Test trên FTMO live account, không phải demo
Lý do: CEO xác nhận FTMO demo dùng cùng feed giá broker live; demo IS live về mặt kỹ thuật.
Trade-off: tiền thật chịu rủi ro trong dev. Mitigate bằng lot size cực nhỏ khi test.

### D-010 — Dev environment: devcontainer Linux cho Phase 1-3, máy Windows cho Phase 4+
Lý do: package `MetaTrader5` Python chỉ chạy được trên Windows. Cross-platform dev cho Exness client là không thể.
Trade-off: từ Phase 4 trở đi CEO phải chuyển ngữ cảnh giữa devcontainer và máy Windows.

### D-011 — Git hooks: chỉ dùng bash, Windows dùng Git Bash đi kèm Git for Windows
Lý do: đơn giản hơn việc duy trì song song script bash + PowerShell. Git for Windows ship sẵn Git Bash chạy bash hooks bình thường.
Trade-off: user Windows phải cài Git for Windows (không phải WSL git, không phải Cygwin).

### D-012 — Chiến lược continuity của CTO: documentation-first onboarding
Lý do: mỗi phiên CTO mới không có memory. Phải đọc MASTER_PLAN_v2 + PROJECT_STATE + DECISIONS + PHASE_N_REPORT mới nhất để onboard.
Trade-off: docs phải luôn được cập nhật ở mỗi step. PROJECT_STATE.md cập nhật sau MỖI step PASS/REJECT.

### D-013 — Quy ước ngôn ngữ
- CEO ↔ CTO: Tiếng Việt
- CTO → Claude Code (prompt): English
- Claude Code → CTO (report): English
- Code, comments, commit messages: English
- `docs/*.md`: Tiếng Việt
- Tin nhắn Telegram: English
Lý do: cân bằng giữa sự thoải mái của CEO, hiệu quả token và khả năng follow instruction của Claude Code.

### D-014 — Format prompt: một code block duy nhất cho mỗi step
Lý do: CEO copy prompt bằng một click thay vì select text thủ công.

## Phase 1 (Foundation)

### D-015 (Phase 1.1) — Layout monorepo: `server/`, `shared/`, `web/`, `ftmo-client/`, `exness-client/`
Lý do: khớp với thực tế file legacy (post-create.sh hardcode đường dẫn). Tránh layout `apps/server/` để khỏi tốn công refactor.

### D-016 (Phase 1.2) — Pydantic v2 strict models cho symbol mapping
Lý do: `extra="forbid"` ở cả top-level và per-mapping bắt sớm các schema drift.

### D-017 (Phase 1.2) — `shared/hedger_shared/symbol_mapping.py` nằm trong shared package
Lý do: server (Phase 1) và client (Phase 3+) đều load mapping. Shared package tránh duplicate.

### D-018 (Phase 1.2) — Helper bootstrap CORS để tránh instantiate Settings ở module import time
Lý do: `Settings()` cần JWT_SECRET + ADMIN_PASSWORD_HASH; instantiate khi load module sẽ crash khi env chưa set (vd lúc pytest collection).
Trade-off: helper nhỏ `_bootstrap_cors_origins()` đọc env trực tiếp. Đã được thay thế gọn ở step 1.4a.

### D-019 (Phase 1.3) — Telegram wrapper bị bypass thực tế; chỉ commit hook hoạt động
Lý do: `claude --dangerously-skip-permissions | while read` bị pipe phá vỡ chế độ TTY interactive của Claude Code. Wrapper script không dùng được.
Trade-off: CEO mất notification ⚠️ approve và 💤 stuck. Phase 5 hardening có thể xem lại bằng lệnh `script` để giữ TTY.

### D-020 (Phase 1.4) — JWT stateless single token (không refresh token)
Lý do: công cụ single-admin. Refresh complexity không xứng đáng.
Trade-off: token hết hạn buộc re-login. Default 60 phút.

### D-021 (Phase 1.4) — 401 message giống nhau cho sai username và sai password
Lý do: ngăn user enumeration. Thực hành bảo mật chuẩn.

### D-022 (Phase 1.4a) — `Annotated[list[str], NoDecode]` cho field `cors_origins`
Lý do: pydantic-settings v2 source layer eager `json.loads` các field `list[X]` TRƯỚC khi `field_validator` chạy. `NoDecode` opt-out field, để validator tự xử lý cả dạng comma-separated lẫn JSON.
Trade-off: yêu cầu pydantic-settings >= 2.2.

### D-023 (Phase 1.4a) — `Settings.env_file` dùng absolute path tính từ `__file__`
Lý do: relative `env_file` resolve theo cwd. Chạy `pytest`/`uvicorn` từ subdirectory `server/` không tìm được `.env` ở root.
Trade-off: coupling nhẹ với vị trí file, nhưng `Path(__file__).resolve().parents[2]` ổn định vì cấu trúc thư mục đã cố định.

### D-024 (Phase 1.5) — React 19 thay vì React 18 (lệch khỏi plan)
Lý do: `npm create vite@latest` mặc định scaffold React 19 tại thời điểm step 1.5. Instruction actionable (dùng Vite scaffold mới nhất) thắng version label trong header spec.
Trade-off: vài thư viện Phase 2-4 có thể chưa support React 19. Sẽ xem lại khi có thư viện chặn (downgrade khả thi).

### D-025 (Phase 1.5) — Tailwind v3 (KHÔNG dùng v4)
Lý do: v4 còn breaking changes chưa ổn định. v3.4 là bản stable mới nhất được hỗ trợ rộng.
Trade-off: việc migrate sang v4 hoãn lại.

### D-026 (Phase 1.5) — Conditional rendering thay vì React Router cho auth gate
Lý do: Phase 1 chỉ có 2 "page" (Login, MainPage). Router thêm moving parts mà không có lợi ích.
Trade-off: khi Phase 4+ thêm Settings modal, sẽ dùng modal overlay (vẫn không cần router).

### D-027 (Phase 1.5) — Zustand persist với `partialize` để loại bỏ setter functions
Lý do: serialize setter functions vào localStorage làm hỏng JSON. `partialize` whitelist chỉ các state field.

### D-028 (Phase 1.6) — Snapshot `hadToken` trước khi `logout()` trong 401 interceptor
Lý do: phân biệt "session expired" (toast) với "login attempt failed" (Login.tsx đã hiển thị error rồi, không double toast).

### D-029 (Phase 1.7) — PositionList full-width dưới chart+form (không nằm trong sidebar)
Lý do: CEO override khi planning step 1.7. Full-width có thêm chỗ cho hedge order rows (mỗi hedge gồm 2 leg).
Trade-off: ASCII diagram trong `docs/09-frontend.md` cần cập nhật theo (làm trong step 1.8 nếu file tồn tại).
> **OVERRIDDEN by D-042 (Phase 2.8a)**: PositionList chuyển từ full-width → 70% width (cột trái dưới chart); OrderForm chuyển sang full-height cột phải 30%.

### D-030 (Phase 1.7) — Tab state local (`useState`), không Zustand
Lý do: lựa chọn tab Open/History là ephemeral UI state. Không cần persist qua session hay share giữa component.

## Phase 2 (Market Data + Chart + Form)

### D-031 (Phase 2.1a) — cTrader OAuth không sử dụng state CSRF parameter
Lý do: cTrader Open API OAuth callback không echo `state` lại (chỉ trả `code`), khác với RFC 6749 standard. Nếu server require `state` ở callback → 422 validation error trước khi xử lý code. Đã verify bằng smoke test thực tế ở step 2.1.
Trade-off: mất CSRF protection cho OAuth flow. Risk chấp nhận được vì:
- Tool single-admin, không multi-tenant.
- OAuth flow chạy 1-2 lần trong vòng đời tool (initial setup + token refresh thủ công).
- Phase 5 deploy: server private VPS qua Tailscale, OAuth callback không expose internet public.
- Phase 5 hardening có thể revisit nếu cần (vd dùng PKCE thay state, nếu cTrader support).

### D-032 (Phase 2.2a) — cTrader Open API gửi raw integer prices uniformly scaled by 10^5
Lý do: bất kể symbol's `digits` field là gì (5 cho FX, 3 cho USDJPY, 2 cho XAUUSD), cTrader trả price/trendbar dưới dạng `int` đã nhân 10^5. Verified end-to-end trên EURUSD/USDJPY/XAUUSD/BTCUSD ở step 2.2 smoke. `digits` chỉ dành cho display formatting (priceFormat.precision) — KHÔNG dùng để rescale price từ broker. Server divide tất cả raw price cho `100000.0` ngay tại boundary; frontend format theo `digits` ở UI.
Trade-off: nếu broker đổi convention sau này (vd 10^pipPosition), phải update tại 1 chỗ duy nhất (`_PRICE_SCALE` constant trong `market_data.py`).

### D-033 (Phase 2.3) — WS auth fail trả HTTP 403 thay vì WS close 4401
Lý do: FastAPI/Starlette validate JWT trong `Depends(get_current_user_ws)` TRƯỚC khi `accept()` WS handshake. Khi token sai, framework trả HTTP 403 tự động → WebSocket browser API thấy `error` event với HTTP 403 thay vì `close` với code 4401. Spec docs ban đầu giả định mã 4401 sau accept; thực tế FastAPI flow là pre-accept.
Trade-off: smoke test step 2.3 PASS 10/11 với deviation này được CEO chấp nhận. Phase 5 hardening có thể revisit nếu frontend reconnect logic cần phân biệt close-code 1000/4401 (vd retry với fresh token vs hard logout). Hiện tại frontend chỉ xử lý 1 case "WS closed" và prompt re-login khi token hết hạn.

### D-034 (Phase 2.4) — Bỏ `rate:*` 24h cache; dùng `tick:*` 60s cache trực tiếp
Lý do: spec docs/07-server-services.md ban đầu đề xuất 2-tier cache (`tick:*` 60s cho live + `rate:*` 24h cho conversion rate). Thực tế: conversion rate cần real-time tick mới nhất (USD↔quote), không thể stale 24h khi market moving. Phase 2 single-admin load thấp (1 user × vài calc/giây), không cần tier 2.
Trade-off: mỗi calc gọi vào `tick:*` cache (60s TTL). Cache miss subscribe spot, populate cache, retry. P95 <50ms cho cache hit. Phase 5 revisit nếu multi-user hoặc multi-symbol concurrent calc tăng tải Redis.

### D-035 (Phase 2.6) — Lightweight Charts v5 (5.2.x) thay vì v4 trong skeleton spec
Lý do: `npm install lightweight-charts` ở step 2.6 default v5.2.x (latest stable). v5 có breaking change: `chart.addCandlestickSeries(opts)` (v4) → `chart.addSeries(CandlestickSeries, opts)` (v5). Migration cost ~3 lines per series.
Trade-off: chấp nhận v5 vì performance + bundle size cải thiện. v5 stable từ 2024, đã production-ready. v4 deprecated path.

### D-036 (Phase 2.6a) — Crosshair mode Normal thay vì v5 default Magnet
Lý do: v5 default `CrosshairMode.Magnet` snap crosshair vào candle close — dễ dùng cho chart phân tích nhưng cản trở set Entry/SL/TP ở giá tự do (cursor bay khỏi điểm user click). CEO directive: free cursor để pick đúng giá Y muốn. Set `crosshair: { mode: CrosshairMode.Normal }`.
Trade-off: cursor không snap candle — user thấy crosshair theo đúng pixel chuột. Đây là behavior mong muốn cho trading tool (không phải chart analysis tool).

### D-037 (Phase 2.7a) — Hide default candlestick series `priceLineVisible` và `lastValueVisible`
Lý do: Lightweight Charts v5 default render horizontal dotted line tại close của bar cuối cùng + axis label, nhằm marker last close. Trong tool hedge, chart đã có 2 explicit price line bid/ask + setup line Entry/SL/TP — line mặc định gây nhiễu visual + cạnh tranh với bid line khi giá gần nhau.
Trade-off: user mất "last close marker" nhưng tool đã có bid/ask + live candle close (tracked trong real-time), thông tin redundant.

### D-038 (Phase 2.7a) — OHLC response include `digits` field; frontend per-symbol priceFormat
Lý do: Y-axis precision cần khớp symbol (5 cho FX, 3 cho JPY, 2 cho XAU, ...). Server đã có `digits` trong `symbol_config:{sym}` (từ ProtoOASymbolByIdReq detail). Expose qua OHLC response → frontend `applyOptions({ priceFormat: { precision: digits, minMove: 10^-digits } })` ngay trước `setData()`. Pydantic optional default `digits=5` cho stale cache entries pre-2.7a deploy.
Trade-off: thêm 1 field response. Backward-compatible vì optional default. Phase 5 revisit nếu broker thêm symbol mới có digits không thuộc {2, 3, 5}.

### D-039 (Phase 2.7b) — sync_symbols batch fetch qua single ProtoOASymbolByIdReq
Lý do: cTrader `ProtoOASymbolByIdReq.symbolId` là `repeated` protobuf field — append nhiều id, server trả 1 response chứa details cho tất cả. Code ban đầu loop từng symbol (91 sequential RTT × ~700ms each = ~64s) + per-iteration `asyncio.sleep(0.25)` pacing → tổng ~90s mỗi server restart. Batch single RTT ~1-3s.
Trade-off: nếu broker hạn chế batch size, batch fail mất tất cả. Mitigate: `try/except` quanh batch RPC trả 0 cached symbols + log error → caller có thể retry on demand. Verified: 91 symbol batch chạy ổn trên cTrader Open API.

### D-040 (Phase 2.7c) — Live candle close tracks bid (industry convention)
Lý do: TradingView, MetaTrader, mọi chart platform sử dụng bid làm running close cho bar đang chạy. Tick stream arrival → patch close = tick.bid. High/low monotonic trong bar (Math.max / Math.min với tracked). Server `candle_update` broadcast vẫn authoritative tại bar boundary (corrects close, resets ref).
Trade-off: ask không hiển thị trong candle (chỉ trong price line). Spread visible qua bid line vs ask line; đủ thông tin cho hedge trading.

### D-041 (Phase 2.7e) — WS candle_update KHÔNG redraw chart in-bar; tick stream là single source of truth
Lý do: chart flicker do 2 source redraw cùng bar trong vài ms (server candle_update mỗi vài giây + tick stream ~10/s). Mỗi `series.update(...)` trigger Lightweight Charts repaint với close khác nhau → visual flicker. Fix: handler `candle_update` chỉ sync server's authoritative open + max(high) + min(low) vào `lastCandleRef`; KHÔNG call `series.update()` cho in-bar (msg.time === tracked.time). Tick effect là path duy nhất redraw bar đang chạy.
Trade-off: server's mid-bar close bị drop (stale snapshot). Bar boundary (msg.time > tracked.time) vẫn redraw + reset ref bình thường. Test: smooth visual end-to-end; step 2.7d (RAF throttle) reject vì 2.7e đã fix root cause.

### D-042 (Phase 2.8a) — Layout: left 70% (chart top + PositionList bottom), right 30% (OrderForm full-height)
Lý do: D-029 đặt PositionList full-width dưới chart+form, OrderForm width 380px. Step 2.8 thêm Pair + Symbol read-only + Side + Entry/SL/TP/Risk + Volume preview + Submit → form overflow theo chiều dọc, scroll khó chịu. Layout mới: OrderForm chiếm full height cột phải 30%, đủ chỗ cho mọi input + volume preview + submit không scroll. Chart vẫn chiếm 70% width như cũ.
Trade-off: PositionList narrow lại từ 100% → 70%. Phase 3 sẽ test xem rows hedge 2-leg có đủ chỗ; nếu không, có thể bỏ wrap hoặc compact column. Override D-029.

### D-043 (Phase 2.9a) — Đổi symbol → reset Entry/SL/TP/manualVolumePrimary trong form
Lý do: form state retain on symbol switch (D ban đầu trong step 2.9) khiến giá Entry EURUSD (vd 1.085) hiển thị khi user chuyển sang USDJPY (range 140-160) — vô nghĩa và misleading. CEO directive: reset tất cả price field + manual volume khi symbol thay đổi. Ratio + selectedPairId + side + riskAmount giữ nguyên (per-symbol agnostic). `prevSymbolRef` guard tránh wipe form lúc initial mount với persisted symbol.
Trade-off: user phải re-set price khi switch symbol — đây là behavior mong muốn vì context trade thay đổi hoàn toàn.

### D-044 (Phase 2.9a, refined 2.9b) — Manual volume override trong VolumeCalculator
Lý do: server's auto calc dựa vào risk/SL distance/conversion rate có thể sai (rate cache miss trả 503) hoặc user muốn override (vd risk lớn hơn calculated, deliberate over-leverage). Cần escape hatch. Manual mode: Vol P editable, Vol S derived (Vol P × ratio). SL distance + Est. SL $ shown khi state.result available (price-derived, valid trong cả 2 mode). "↻ Reset to auto" link reset.
Trade-off: thêm runtime state `manualVolumePrimary`. Reset trên symbol switch + F5 (NOT persisted) để tránh stale.

### D-045 (Phase 2.9b) — Side direction HARD BLOCK SL violation; soft TP warning; volumeReady flag
Lý do: Step 2.9a soft warning cho cả SL và TP. CEO directive: SL violation BUY (SL≥Entry) hoặc SELL (SL≤Entry) → block volume calc + force user fix. TP violation chỉ là warning vì TP optional. Validation utility extracted vào `web/src/lib/orderValidation.ts` (single source of truth — HedgeOrderForm display + VolumeCalculator block). `volumeReady` flag trong store cho Phase 3 submit handler.
Trade-off: side error path bypass API call (no POST while invalid) — tiết kiệm network + khẳng định invariant client-side. Server vẫn validate SL pip distance độc lập.

## Phase 3 (Single-leg Trading — FTMO only)

> Các entries Phase 3 (D-046 → D-149) commit verbatim từ working memory của CTO trong quá trình thực hiện 13 step chính + 16 sub-fix + 1 docs sync. Nội dung giữ nguyên tiếng Anh để bảo toàn ý nghĩa kỹ thuật chính xác (D-013 cho phép code/comments tiếng Anh; các entry này thuần technical context).

### D-046 (3.1) — RedisService full CRUD + Lua CAS update script
RedisService full CRUD with Lua CAS update script. Update accepts current-value witness; if mismatch, returns false → caller retries with re-read.

### D-047 (3.1) — Side-index prefixes
Side-index prefixes: `orders:by_status:{status}`, `request_id_to_order:{request_id}`. Single-writer per index keeps SET atomicity.

### D-048 (3.2) — Lifespan setup_consumer_groups
Lifespan setup_consumer_groups creates "server" consumer group on resp_stream + event_stream per FTMO account. MKSTREAM flag tolerant if stream key not yet exists.

### D-049 (3.3) — OAuth extraction shared package
OAuth extraction shared via `hedger-shared` package `ctrader_oauth.py`. Both server (Phase 2) and ftmo-client (Phase 3) reuse same OAuth flow.

### D-050 (3.3) — Twisted reactor in FTMO client separate thread
Twisted reactor in FTMO client runs in separate thread; asyncio main thread bridges via run_coroutine_threadsafe. Duplicate library + reactor lifecycle managed per-client process.

### D-051 (3.3) — FTMO trading credentials dedicated namespace
FTMO trading credentials in dedicated namespace `ctrader:ftmo:{acc}:creds` separate from Phase 2 market-data creds. Allows future market-data-only sub-account separation.

### D-052 (3.3a) — hedger-shared py.typed marker
hedger-shared package ships py.typed marker so consumers (server + ftmo-client) get strict mypy across boundary.

### D-053 (3.4) — Volume wire = int(volume_lots × lot_size)
Volume on wire = `int(volume_lots * lot_size)`. lot_size persisted in symbol_config from Phase 2 sync. Server stores volume_lots as user-facing decimal; FTMO client converts at command construction.

### D-054 (3.4) — Money fields stored raw int (D-032 scale convention)
Money fields stored raw int (cent-style scaled by money_digits) per D-032 wire convention. Display scales by 10^money_digits at render boundary (D-108).

### D-055 (3.4) — Trading prices stored raw DOUBLE
Trading prices stored raw DOUBLE (Phase 2 wire convention D-032 only applies to tick + trendbar). Execution event prices are double.

### D-056 (3.4) — Response timeout 30s for cTrader async ops
Response timeout 30s for cTrader async ops. Aligns with cTrader connection-pool default.

### D-057 (3.4) — Error vocab lowercase snake_case + full close only
Error vocab lowercase snake_case (account_offline, invalid_request, sl_tp_attach_failed, partial_close_unsupported, etc.). Phase 3 full close only — close cmd payload requires `symbol` field for cTrader bridge.

### D-058 (3.4a) — cTrader market orders post-fill amend pattern
cTrader does NOT accept inline SL/TP on market orders. Pattern: place_market_order → wait ORDER_FILLED → amend_order with sl/tp. Skip SL/TP in _build_new_order_req builder for market type.

### D-059 (3.4a) — Fill OK + amend fail → no rollback, operator-decided recovery
Fill OK + amend fail → no rollback. order_metadata.sl_tp_attach_failed=true. Operator decides recovery (re-amend or close).

### D-060 (3.4a) — Phase 4 hardening: post-fill amend retry after POSITION_LOCKED
Phase 4 hardening item: post-fill amend retry after POSITION_LOCKED transient error.

### D-061 (3.4b) — broker_order_id semantic: market=positionId, pending=orderId
broker_order_id semantic: market type = positionId (from event.position), pending types = orderId (from event.order.orderId). Naming preserved across step 3.7 response_handler.

### D-062 (3.4b) — Market wait ORDER_FILLED, limit/stop wait ORDER_ACCEPTED
Market orders wait ORDER_FILLED event; limit/stop wait ORDER_ACCEPTED. Different event types signal different lifecycle stages.

### D-063 (3.4b) — 100ms settling delay before amend SL/TP
100ms settling delay after ORDER_FILLED before amend SL/TP. cTrader requires position fully registered server-side before modification.

### D-064 (3.4b) — deal.executionPrice is DOUBLE (not int64)
deal.executionPrice is DOUBLE (NOT int64). D-032 wire scale only applies to ticks + trendbars. Execution event prices are raw doubles.

### D-065 (3.4b) — Authoritative position id = event.position.positionId
Authoritative ID for position is event.position.positionId (NOT event.deal.positionId). cTrader sometimes ships deal with different positionId in edge cases (refunds, partial fills).

### D-066 (3.4c) — Close position 2-event sequence
Close position = 2-event sequence: ORDER_ACCEPTED (broker accepts request) → ORDER_FILLED (position truly closed). Wait second event for confirmation.

### D-067 (3.4c) — modify_sl_tp single ORDER_REPLACED event
modify_sl_tp = single ORDER_REPLACED event (not 2-event like close).

### D-068 (3.4c) — realized_pnl from grossProfit raw
realized_pnl = `deal.closePositionDetail.grossProfit` raw int (money_digits scaled by account). Server uses authoritative single source.

### D-069 (3.4c) — WORKFLOW exception for ctrader-execution-events.md (append-only mid-phase)
WORKFLOW §6.1 exception for `docs/ctrader-execution-events.md` — append-only knowledge file, update mid-phase whenever cTrader API behavior discovered. Mọi step touching cTrader có acceptance criterion "update doc if new behavior found".

### D-070 (3.5) — event_stream message shapes
event_stream message shapes: position_closed, pending_filled, position_modified, order_cancelled. Each emitted from FTMO client when broker pushes unsolicited execution event.

### D-071 (3.5a) — close_reason inference via order.orderType + closingOrder + grossProfit sign
close_reason inference structured via order.orderType + closingOrder + grossProfit sign: MARKET + closingOrder=true → manual close; STOP_LOSS_TAKE_PROFIT enum=4 + grossProfit > 0 → tp; STOP_LOSS_TAKE_PROFIT enum=4 + grossProfit < 0 → sl.

### D-072 (3.5) — account:ftmo:{acc} HASH publish every 30s
account:ftmo:{acc} HASH publish every 30s by FTMO client account_info_loop. equity = balance approx Phase 3 (no margin calc detail per ProtoOAAccountInfo limitations).

### D-073 (3.5) — BridgeService Optional redis kwarg pattern
BridgeService Optional redis kwarg pattern — bridge testable without Redis dependency.

### D-074 (3.5+3.5a) — position_closed extended with 5 close-detail fields
position_closed event_stream message extended with 5 fields: commission, swap, balance_after_close, money_digits, closed_volume. From deal.closePositionDetail.

### D-075 (3.5a) — STOP_LOSS_TAKE_PROFIT enum value = 4
STOP_LOSS_TAKE_PROFIT enum value = 4 (verified against cTrader protobuf descriptor).

### D-076 (3.5b) — Reconciliation infrastructure
Reconciliation infrastructure: reconcile_state stream + fetch_position_close_history + fetch_close_history command from server side. FTMO client snapshot on connect.

### D-077 (3.5b) — ProtoOADealListByPositionIdReq requires from+to timestamps
ProtoOADealListByPositionIdReq requires fromTimestamp + toTimestamp (cannot omit, will reject).

### D-078 (3.5b) — Reconstructed events close_reason="unknown"
Reconstructed events (close history backfill) have close_reason="unknown" — original event lost.

### D-079 (3.5b) — fetch_close_history retry 3 attempts exponential backoff
fetch_close_history retry 3 attempts, exponential backoff 1s/2s.

### D-080 (3.5b) — order_cancelled noise filter cho cTrader internal SL/TP orders
cTrader auto-cancels internal STOP_LOSS_TAKE_PROFIT order when position closes → publishes order_cancelled event with internal orderId. Server event_handler ignores if no Redis match.

### D-081 (3.6) — POST /api/orders 202 Accepted async + validation pipeline
POST /api/orders 202 Accepted async. Validation pipeline: pair → account → client → symbol → config → volume → entry → tick → sl_tp_direction.

### D-082 (3.6) — OrderValidationError structured exception
OrderValidationError structured exception with http_status + error_code (lowercase snake_case). FastAPI HTTPException detail = `{error_code, message}`.

### D-083 (3.6) — s_status = "pending_phase_4" until Phase 4 cascade
s_status (Exness leg) = "pending_phase_4" until Phase 4 cascade implementation.

### D-084 (3.6) — pair["enabled"] default true backward compat
pair["enabled"] default true backward compat — Phase 2 pairs may lack field.

### D-085 (3.6) — Phase 5 hardening: migrate Phase 2 pair rows to set enabled=true explicitly
Phase 5 hardening: migrate existing Phase 2 pair rows to set enabled=true explicitly.

### D-086 (3.7) — response_handler + event_handler 2 tasks per FTMO account
response_handler_loop + event_handler_loop = 2 background tasks per FTMO account. Consumer group "server", consumer name "server-{account_id}". XREADGROUP BLOCK 1000ms. ACK only on successful handle. Skip ACK on entry-level exception → retry next read. 1s backoff on stream-level error.

### D-087 (3.7) — WS broadcast 2 channels orders + positions
WS broadcast 2 channels: `orders` (order_updated messages), `positions` (position_event messages). Channel constants TBD step 3.10.

### D-088 (3.7) — Lifespan task ORDER critical
Lifespan task placement ORDER critical: response/event handlers create AFTER setup_consumer_groups succeeded. Shutdown ORDER: handlers cancel BEFORE MarketDataService stop + Redis close. Prevent "talking to closing Redis" race.

### D-089 (3.7) — list_open_orders_by_account client-side filter Phase 3
list_open_orders_by_account client-side filter acceptable Phase 3 (operator <50 hedges). Per-account SET index defer Phase 4+ if performance issue.

### D-090 (Phase 4 prep) — status composition rule for 2-leg orders
status field composition rule for 2-leg orders Phase 4: Both legs pending → pending; both filled → filled; both closed → closed; one closed + one filled → half_closed (cascade in progress); either rejected at open → rejected; inconsistent state → error (manual intervention).

### D-091 (3.8) — position_tracker_loop per FTMO account, P&L formula
position_tracker_loop per FTMO account, 1s poll. Compute unrealized P&L formula: `(current_price - entry_price) × side_mult × volume_base / quote_to_usd_rate`. Close-side: BUY uses bid, SELL uses ask.

### D-092 (3.8) — USD conversion routing
USD conversion routing: USD passthrough, JPY via USDJPY bid, other via USD<QUOTE> cross direct or <QUOTE>USD inverse fallback. Conversion miss → is_stale=true flag, fallback raw value.

### D-093 (3.8) — Stale tick threshold 5s, do NOT drop position
Stale tick threshold 5s. is_stale=true does NOT drop position — compute proceeds with last-known price, flag for frontend warning render.

### D-094 (3.8) — contract_size = lot_size / 100 (FX); Phase 5 hardening for non-FX
contract_size derived `lot_size / 100` per cTrader FX convention. Phase 5 hardening: persist explicit contract_size for non-FX symbols (indices, crypto, commodities).

### D-095 (3.8) — quote_currency derived from symbol[-3:] for 6-char FX
quote_currency derived from symbol `[-3:]` for 6-char FX. Phase 5 hardening: persist explicit for non-FX.

### D-096 (3.8) — position_cache:{order_id} HASH separate from legacy position:{order_id} JSON
Position cache key prefix `position_cache:{order_id}` HASH separate from legacy `position:{order_id}` JSON. TTL 600s.

### D-097 (3.8) — WS broadcast batched per account 1 message/cycle
WS broadcast batched per account 1 message/cycle. Empty batch → no broadcast.

### D-098 (3.8 prompt-typo) — Math typo in 3.8 prompt arithmetic example
Math typo in 3.8 prompt arithmetic example. Claude Code used correct standard formula. Lesson: prompt arithmetic examples need double-check.

### D-099 (3.9) — REST surface Phase 3: 6 endpoints
REST surface Phase 3: GET /api/orders (list), GET /api/orders/{id} (detail), GET /api/positions (live P&L enriched), GET /api/history (time-range filter), POST /api/orders/{id}/close (202 async), POST /api/orders/{id}/modify (202 async). All require JWT auth via get_current_user_rest.

### D-100 (3.9) — Phase 3 full close only
Phase 3 only supports full close (D-057 expanded). Partial volume_lots != current → 400 partial_close_unsupported.

### D-101 (3.9) — modify_order semantic: None=keep, 0=remove, positive=set
modify_order semantic — sl/tp None = keep existing, 0 = remove (skip direction validation), positive = set with direction validation. Pydantic rejects both None case.

### D-102 (3.9) — Sort defaults per view
Sort defaults per view: /orders created_at DESC, /positions p_executed_at DESC, /history p_closed_at DESC. Pagination limit 1-200 default 50.

### D-103 (3.9) — Default history window now - 7 days
Default history window = now - 7 days. from_ts > to_ts → 400 invalid_time_range.

### D-104 (3.9) — list_positions just-filled race → is_stale=true placeholder
list_positions just-filled race — order in orders:by_status:filled but position_cache:{id} not yet computed → return with is_stale=true + empty live fields. Tracker catches up next cycle.

### D-105 (3.10) — useWebSocket hook hoisted to MainPage
useWebSocket hook hoists from HedgeChart → MainPage. Single WS connection per session. Chart accepts candle handler via prop.

### D-106 (3.10) — Frontend api/client.ts single-file convention preserved
Frontend api/client.ts single file (not split per-resource). Phase 1 convention preserved.

### D-107 (3.10) — Frontend NOT optimistic prepend history on close
Frontend does NOT optimistic prepend history on close. REST refresh on tab switch. Acceptable Phase 3 operator cadence.

### D-108 (3.10) — Money scaling at render boundary
Money scaling at render boundary via `scaleMoney(raw, money_digits)`. Server stores raw int (D-053), display divides 10^digits.

### D-109 (3.10a) — WS VALID_CHANNEL_PREFIXES += "orders" (exact-match)
WS VALID_CHANNEL_PREFIXES extended with "orders". Channel validator uses `startswith(p) or channel == p` supporting both prefix-match (ticks:, candles:) and exact-match (positions, orders, agents, accounts).

### D-110 (3.11) — Submit form market-only at 3.11
Submit form market-only at 3.11. Limit/stop order types backend ready but UI selector ships at 3.12b.

### D-111 (3.11) — Server error_code → Vietnamese ORDER_ERROR_MESSAGES + 3-level fallback
Server error_code (lowercase snake_case) maps to Vietnamese user-facing messages via ORDER_ERROR_MESSAGES table. Fallback 3 levels: structured detail → raw message → generic "Lỗi kết nối server".

### D-112 (3.11) — preflight() 2-layer client-side validation
preflight() client-side validation pattern: pair/symbol/volume readiness → Entry-vs-SL (limit/stop semantics, Phase 2 helper) → bid/ask direction (market semantics). 2 layers separated by order_type.

### D-113 (3.11) — Form partial reset on success
Form partial reset on success: clear Entry/SL/TP. KEEP pair/symbol/side/riskAmount/volumeLots for re-submit same setup.

### D-114 (3.11a) — PairPicker validate selectedPairId membership in fetched list
PairPicker validate selectedPairId membership in fetched data list. Stale ID from localStorage Phase 2 → auto-select first pair. Silent re-selection.

### D-115 (3.11a) — OrderService silent normalize SL/TP/entry_price to symbol_config.digits
Server OrderService silent normalize SL/TP/entry_price rounded to symbol_config.digits (default 5) before push_command. Direction validation uses raw values; normalize after validation pass. Order row + cmd_stream payload use normalized.

### D-116 (3.11a) — _compute_pnl defensive guard for None bid/ask
_compute_pnl defensive guard: None bid (BUY) or None ask (SELL) raises ValueError. Caller catches → WARNING log + loop continue. Does NOT spam ERROR.

### D-117 (3.11a) — _convert_to_usd defensive guard JPY/cross/inverse paths
_convert_to_usd defensive guard for JPY/cross/inverse paths. None bid → fallback raw + is_stale=true.

### D-118 (3.11b) — BroadcastService.publish_tick coalesces cTrader delta ticks (ROOT CAUSE FIX)
BroadcastService.publish_tick coalesces delta cTrader ticks with prev cached state. Fast path (full delta): identity return, zero cache read. Partial path: merge with prev. Initial state (no prev + partial): drop publish + cache write.

### D-119 (3.11b) — Defensive guards retained as belt-and-suspenders post-coalescing
3.11a defensive guards (OrderService SL/TP direction + position_tracker._compute_pnl + _convert_to_usd) retained as belt-and-suspenders post-coalescing. Cover startup race + future API changes.

### D-120 (3.11c) — positions_tick payload + 7 static metadata fields zero-cost
positions_tick WS payload extended with 7 static metadata fields (side, volume_lots, entry_price, money_digits, sl_price, tp_price, p_executed_at) sourced from already-fetched order HASH zero-cost.

### D-121 (3.11c) — upsertPositionTick true upsert (prepend)
Frontend upsertPositionTick true upsert — insert prepend when findIndex === -1, match REST sort p_executed_at DESC.

### D-122 (3.11c) — current_price broadcast type unified to str
current_price broadcast type unified to str(current_price) matching position_cache HASH + Position TS interface. WS contract `string | number` accepts both.

### D-123 (3.11d) — WsPositionsTickMessage.data.positions[i] += 7 optional metadata fields
WsPositionsTickMessage.data.positions[i] extended with 7 optional metadata fields. Optional for backward compat pre-3.11c servers.

### D-124 (3.11d) — useWebSocket positions_tick spread + post-spread coercions
useWebSocket positions_tick handler uses spread + post-spread coercions for 3 wire-type fields (current_price, is_stale, tick_age_ms). Property-merge order: spread → coercions (coercions override).

### D-125 (3.12) — GET /api/accounts endpoint with sorted snapshot
GET /api/accounts endpoint returns list of accounts with meta + heartbeat status + balance/equity. Sort ftmo first, exness after, account_id asc.

### D-126 (3.12) — account_status_loop 5s broadcast single global task
account_status_loop background task 5s interval, broadcasts full snapshot to "accounts" WS channel each cycle. Single global task (not per-account).

### D-127 (3.12) — VALID_CHANNEL_PREFIXES += "accounts" (keep "agents" legacy)
WS VALID_CHANNEL_PREFIXES extends "accounts" (keep "agents" Phase 1 legacy backward compat).

### D-128 (3.12) — Status precedence: disabled > online/offline
Status precedence: enabled=false → disabled > heartbeat EXISTS → online else offline.

### D-129 (3.12) — OrderForm submit gating with hasOnlineFtmoAccount
Frontend OrderForm submit gating with hasOnlineFtmoAccount from accountStatuses store slice. Disabled button + native tooltip + inline red banner backup (cross-browser accessibility).

### D-130 (3.12) — AccountStatusBar extends Phase 1 placeholder in-place
AccountStatusBar component extends existing Phase 1 placeholder rather than create new file. Stable import path via Header.tsx.

### D-131 (3.12a) — Frontend pairs cache hoisted to MainPage useEffect
Frontend pairs cache slice hoisted to MainPage useEffect single fetch on mount. PairPicker reads from store. PositionRow + OrderRow + future settings UI share cache.

### D-132 (3.12a) — lookupPairName helper centralized with truncated-UUID fallback
lookupPairName(pairs, pair_id) helper centralized in web/src/lib/pairHelpers.ts. Fallback hierarchy: pair found → name; missing pair_id → "—"; pair not in cache → truncated UUID xxxxxxxx....

### D-133 (3.12a) — PositionRow joins via orders slice
PositionRow joins via orders slice (Position type doesn't carry pair_id). OrderRow uses order.pair_id directly.

### D-134 (3.12b) — Order type selector Market/Limit/Stop (persisted)
Order type selector segmented Market/Limit/Stop. Default Market. Persisted across reloads.

### D-135 (3.12b) — Market mode auto-entry from tickThrottled
Market mode → Entry input hidden, auto-driven from tickThrottled (ask BUY / bid SELL). Throttle 5s avoid volume jitter.

### D-136 (3.12b) — Limit/Stop direction preflight checks
Limit direction: BUY entry < ask, SELL entry > bid. Stop direction: BUY entry > ask, SELL entry < bid. Client preflight + server validate authoritative.

### D-137 (3.12b) — Submit entry_price=0 for market, user input for limit/stop
Submit payload entry_price=0 for market, user input for limit/stop. Form reset clears entry only if non-market.

### D-138 (3.12b) — tickThrottled snapshot symbol-at-copy-time
tickThrottled snapshot strategy — symbol attached at copy time (defensive check skips stale snapshot on symbol switch race).

### D-139 (3.12c) — useTickThrottle interval bumped 1s → 5s
useTickThrottle interval 5s. Reduce 5x flicker frequency, acceptable preview lag for manual trading cadence.

### D-140 (3.12c) — VolumeCalculator ApiState.refreshing variant
VolumeCalculator state machine ApiState.refreshing variant carries previous result. Recalc holds prev result instead of swap layout. Initial layout swap only first-load.

### D-141 (3.12c) — Subtle refreshing indicator (decorative dot)
Subtle refreshing indicator (6×6px blue dot top-right) with aria-hidden + Vietnamese tooltip. Decorative only, not announced to screen reader.

### D-142 (3.13) — DELETE /api/pairs/{id} guarded against pending + filled references
DELETE /api/pairs/{id} guards against pending + filled orders referencing. 409 pair_in_use with count. Closed/cancelled orders NOT counted (historical references frozen).

### D-143 (3.13) — PATCH /api/accounts/{broker}/{account_id}
PATCH /api/accounts/{broker}/{account_id} accepts {enabled: bool}. Authoritative update_account_meta HSET + auto-stamp updated_at.

### D-144 (3.13) — SettingsModal via Header gear icon
SettingsModal access via Header gear icon. 2 tabs Pairs + Accounts. Click-outside + × close (Escape deferred Phase 5).

### D-145 (3.13) — Phase 3 Exness account dropdown defer
Phase 3 Exness account dropdown defer — text input free-form. Phase 4 widens to <select> when accounts:exness SET populated.

### D-146 (3.13) — FTMO account create UI defer Phase 5
FTMO account create UI defer Phase 5 (OAuth flow integration). Bootstrap path via FTMO client first-time write.

### D-147 (3.13a) — row_to_entry helper centralized for REST + WS single source of truth
row_to_entry helper centralized in app/services/account_helpers.py. Single source of truth for REST + WS payload conversion. Pre-3.13a regression Boolean("false") === true permanently fixed via typed Pydantic round-trip.

### D-148 (3.13a) — OrderForm 3-tier ftmoBlockMessage priority
OrderForm 3-tier ftmoBlockMessage priority: no account > all disabled > offline. Each message actionable (configure / Settings / investigate process).

### D-149 (3.13a) — Function-local imports break circular dependency accounts ↔ account_helpers
Function-local imports in accounts.py to break circular dependency accounts ↔ account_helpers. Codebase convention from ws.py. Phase 5 cleanup: extract schemas to dedicated module.
