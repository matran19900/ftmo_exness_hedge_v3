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
