# FTMO Hedge Tool v3

Công cụ hedge thủ công giữa FTMO (cTrader) và Exness (MT5). Mỗi lệnh FTMO được hedge bằng một lệnh Exness ngược chiều ở volume tỉ lệ tính toán, để lỗ một bên được offset gần đủ bằng lãi bên kia.

## Documentation
- Master plan: [docs/MASTER_PLAN_v2.md](docs/MASTER_PLAN_v2.md)
- Trạng thái hiện tại: [docs/PROJECT_STATE.md](docs/PROJECT_STATE.md)
- Sổ quyết định: [docs/DECISIONS.md](docs/DECISIONS.md)
- Báo cáo phase 1: [docs/PHASE_1_REPORT.md](docs/PHASE_1_REPORT.md)
- Báo cáo phase 2: [docs/PHASE_2_REPORT.md](docs/PHASE_2_REPORT.md)
- Báo cáo phase 3: [docs/PHASE_3_REPORT.md](docs/PHASE_3_REPORT.md)
- Runbook (dev workflow + ops): [docs/RUNBOOK.md](docs/RUNBOOK.md)
- Template handoff CTO: [docs/CTO_HANDOFF_TEMPLATE.md](docs/CTO_HANDOFF_TEMPLATE.md)

## Quick start (Phase 1 features)

### Prerequisites
- Linux/WSL với Docker (cho devcontainer).
- VS Code + Dev Containers extension.
- LAN Redis tại `redis://192.168.88.4:6379/2` (hoặc đổi `REDIS_URL` trong `.env`).
- Telegram bot token + chat_id ở `~/.config/hedger-sandbox/telegram.env` (cho commit notification).

### Setup
1. Clone repo: `git clone https://github.com/matran19900/ftmo_exness_hedge_v3.git`
2. Mở trong VS Code → "Reopen in Container".
3. Đợi `post-create.sh` chạy xong (tạo Python venv + npm install).
4. Setup Telegram credentials (one-time per devcontainer volume):

   ```bash
   mkdir -p ~/.config/hedger-sandbox
   cat > ~/.config/hedger-sandbox/telegram.env <<EOF
   TELEGRAM_BOT_TOKEN=<your-bot-token>
   TELEGRAM_CHAT_ID=<your-chat-id>
   EOF
   chmod 600 ~/.config/hedger-sandbox/telegram.env
   ```

5. Install git hooks: `bash scripts/install-git-hooks.sh`
6. First-time auth setup (xem section bên dưới).
7. Chạy backend (terminal 1):

   ```bash
   cd server && source .venv/bin/activate
   uvicorn app.main:app --port 8000
   ```

8. Chạy frontend (terminal 2):

   ```bash
   cd web && npm run dev
   ```

9. Mở `http://localhost:5173`. Login với `admin` / `admin`.

### First-time auth setup

Sinh secrets và viết file `.env` ở repo root (KHÔNG commit `.env`):

```bash
# Generate JWT_SECRET (any directory):
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env

# Generate bcrypt hash (must run inside the server venv):
cd server && source .venv/bin/activate && cd ..
echo "ADMIN_PASSWORD_HASH=$(python -c 'import bcrypt; print(bcrypt.hashpw(b"admin", bcrypt.gensalt(rounds=12)).decode())')" >> .env
deactivate

# Add the remaining required vars:
cat >> .env <<'EOF'
ADMIN_USERNAME=admin
JWT_EXPIRES_MINUTES=60
REDIS_URL=redis://192.168.88.4:6379/2
SYMBOL_MAPPING_PATH=/workspaces/ftmo_exness_hedge_v3/symbol_mapping_ftmo_exness.json
CORS_ORIGINS=http://localhost:5173
LOG_LEVEL=INFO
EOF
```

Ghi chú:
- Mọi lệnh Python cần import deps của project (như `bcrypt`) phải chạy trong venv: `cd server && source .venv/bin/activate`.
- `Settings` tự load `.env` từ repo root bất kể bạn chạy `uvicorn`/`pytest` từ đâu (path resolve theo `server/app/config.py`, không theo `cwd` — sửa ở step 1.4a).
- `CORS_ORIGINS` chấp nhận comma-separated (`http://a,http://b`) hoặc JSON list (`["http://a","http://b"]`).

Default password là `admin`. Đổi trước khi deploy thật.

### Web frontend (sau khi setup ở trên)

Devcontainer rebuild sẽ tự `npm install` qua `post-create.sh`. Login form hiện ra ở `http://localhost:5173`.

Sau khi login:
- Token lưu trong `localStorage` (persist qua refresh trang).
- Symbol count load từ `/api/symbols/` (xác nhận token hoạt động end-to-end).
- Nút Logout góc trên-phải xoá token, quay lại login.
- Session expired (server trả 401) tự logout và hiện toast.

Backend không chạy → login sẽ báo network error inline. Khởi động lại bằng:
`cd server && source .venv/bin/activate && uvicorn app.main:app --port 8000`

### cTrader market-data setup (Phase 2)

Phase 2 cần một cTrader market-data account để feed live price + chart data.

**Prerequisites**:
1. Đăng ký cTrader Open API app tại https://connect.spotware.com/apps
2. Set Redirect URI: `http://localhost:8000/api/auth/ctrader/callback`
3. Mở DEMO account ở bất kỳ broker cTrader nào (vd IC Markets, Pepperstone). FTMO accounts không hoạt động trực tiếp với endpoint mặc định trong dev — chúng cần `demo.ctraderapi.com`.

**Setup**:
1. Thêm vào `.env`:

   ```
   CTRADER_CLIENT_ID=<your client_id>
   CTRADER_CLIENT_SECRET=<your client_secret>
   CTRADER_HOST=live.ctraderapi.com
   CTRADER_PORT=5035
   CTRADER_REDIRECT_URI=http://localhost:8000/api/auth/ctrader/callback
   ```

2. Restart server.
3. Mở browser: `http://localhost:8000/api/auth/ctrader`
4. Login bằng cTrader credentials, approve app.
5. Sẽ redirect về `/` (server origin), access token đã lưu vào Redis.
6. Verify: `curl http://localhost:8000/api/auth/ctrader/status` → `{"has_credentials": true, ...}`
7. Verify symbols synced: `curl -H "Authorization: Bearer <jwt>" http://localhost:8000/api/symbols/` → list symbols available trên YOUR cTrader account giao với whitelist.

**Note về availability**: mỗi broker có symbol khác nhau. Whitelist file có 117 symbol; account của bạn có thể ít hơn. Kết quả là intersection.

### Verify chart endpoint (sau khi OAuth xong)

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

curl -s "http://localhost:8000/api/charts/EURUSD/ohlc?timeframe=M15&count=20" \
  -H "Authorization: Bearer $TOKEN" | python -m json.tool | head -30
```

Trả về 20 candle OHLC. Lần gọi tiếp trong 60s sẽ hit Redis cache (`ohlc:EURUSD:M15:20`) và nhanh hơn rõ rệt.

### Verify WebSocket endpoint (sau khi step 2.3)

Cài `wscat` (1 lần / devcontainer):

```bash
npm install -g wscat
```

Connect và bật stream cho EURUSD M15:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

wscat -c "ws://localhost:8000/ws?token=$TOKEN"
> {"type":"set_symbol","symbol":"EURUSD","timeframe":"M15"}
< {"channel":"ticks:EURUSD","data":{"type":"tick","symbol":"EURUSD","bid":1.08412,"ask":1.08415,"ts":1735000000000}}
< {"channel":"candles:EURUSD:M15","data":{"type":"candle_update","time":1735000000,"open":1.08400,...}}
```

Tick stream chạy mỗi 0.1–1s trong giờ market mở. Switch sang USDJPY: gửi tiếp `{"type":"set_symbol","symbol":"USDJPY","timeframe":"M15"}` — server tự unsub EURUSD và sub USDJPY. Server gửi `{"type":"ping"}` mỗi 30s để giữ kết nối sống.

### Verify volume calculator (sau khi step 2.4)

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

# EURUSD: risk $100, entry 1.0850, SL 1.0800 (50 pips)
curl -s -X POST "http://localhost:8000/api/symbols/EURUSD/calculate-volume" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entry":1.0850,"sl":1.0800,"risk_amount":100,"ratio":1.0}' | python -m json.tool
```

Kết quả: `volume_primary` ≈ 0.20 lot, `sl_pips` = 50.0, `quote_ccy` = "USD", `quote_to_usd_rate` = 1.0.

USDJPY (`quote_ccy=JPY`) cần USDJPY tick trong cache để tính rate inverse. Nếu chưa có tick:

```bash
curl -s -X POST "http://localhost:8000/api/symbols/USDJPY/calculate-volume" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entry":156.50,"sl":156.00,"risk_amount":100,"ratio":1.0}'
```

Lần đầu trả 503 + server tự subscribe USDJPY spots; sau vài giây retry lại sẽ pass với `quote_to_usd_rate` ≈ 0.0064.

### Verify pairs CRUD (sau khi step 2.5)

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

# Create
PAIR=$(curl -s -X POST "http://localhost:8000/api/pairs/" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"FTMO ↔ Exness Test","ftmo_account_id":"ftmo_001","exness_account_id":"exness_001","ratio":1.0}')
PAIR_ID=$(echo "$PAIR" | python -c "import json,sys; print(json.load(sys.stdin)['pair_id'])")
echo "Created: $PAIR_ID"

# List, get, update ratio, delete
curl -s "http://localhost:8000/api/pairs/" -H "Authorization: Bearer $TOKEN" | python -m json.tool
curl -s "http://localhost:8000/api/pairs/$PAIR_ID" -H "Authorization: Bearer $TOKEN" | python -m json.tool
curl -s -X PATCH "http://localhost:8000/api/pairs/$PAIR_ID" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ratio":1.5}' | python -m json.tool
curl -s -X DELETE -i "http://localhost:8000/api/pairs/$PAIR_ID" -H "Authorization: Bearer $TOKEN" | head -1
# → HTTP/1.1 204 No Content
```

Phase 2 chưa validate `ftmo_account_id`/`exness_account_id` tồn tại — Phase 4 sẽ check sau khi accounts CRUD lên.

### Verify chart UI (sau khi step 2.6)

1. Backend chạy + cTrader OAuth done.
2. Frontend dev: `cd web && npm run dev`.
3. Mở http://localhost:5173, login `admin`/`admin`.
4. Top-left chart panel: click "Select symbol...", search "EUR", chọn EURUSD.
5. Chart load ~200 candle M15 trong 1–2 giây.
6. Click M5/M30/H1/D1 → chart reload theo timeframe mới.
7. Đổi symbol sang USDJPY → giá hiển thị trong khoảng 140–160 (KHÔNG 14000 — verify D-032 đã propagate sang frontend).
8. Resize browser → canvas chart tự co dãn theo.
9. Refresh page → `selectedSymbol` + `selectedTimeframe` persist (Zustand localStorage).

### Verify live data (sau khi step 2.7)

1. Backend chạy + cTrader OAuth done + market data đang tick.
2. `cd web && npm run dev`. Mở http://localhost:5173, login.
3. Top-right header: dot WS chuyển từ "WS: connecting..." (vàng) sang "WS: connected" (xanh) trong 1–2 giây.
4. Chọn EURUSD trên chart:
   - 200 candle history load.
   - Bid line đỏ (dashed) + Ask line xanh (dashed) hiện trên chart.
   - Top-right toolbar chart hiển thị live bid/ask (font mono, cập nhật 0.1–1 giây/lần).
   - Last candle update real-time (close price flicker khi tick mới).
5. Đổi sang USDJPY → bid/ask line snap về range 140–160.
6. Chờ ≥30s → server ping, client pong, WS giữ nguyên "connected".
7. Stop backend (Ctrl+C):
   - WS dot xám "WS: disconnected" trong 1–2s.
   - Bid/ask line biến mất (latestTick cleared).
   - Historical chart vẫn hiển thị.
8. Restart backend → dot vàng → xanh trong 2–5s; `set_symbol` auto-resent; tick line trở lại.
9. Logout → WS đóng sạch (code 1000), không reconnect.
10. Login lại → WS connect; symbol/timeframe restore từ localStorage; live data tiếp tục.
11. DevTools Network → WS → thấy connection persistent + JSON frames (set_symbol, ticks, candles, ping/pong).

### Verify order form UI (sau khi step 2.8)

1. Backend chạy. Tạo ít nhất 1 pair qua curl (snippet bên dưới) rồi `cd web && npm run dev` và login.
2. Right panel hiện form với:
   - Dropdown "Pair" — populated từ `/api/pairs/` (auto-select pair đầu tiên nếu chưa có selection).
   - "Symbol" read-only — hiển thị symbol đang xem trên chart.
   - "Side" — 2 button BUY (xanh) / SELL (đỏ).
   - Entry / Stop Loss / Take Profit — input số (`step` = `10^-digits`).
   - Risk Amount (USD) — default 100.
   - Placeholder "Volume preview: step 2.9".
   - Button "Place Hedge Order" disabled (Phase 3 sẽ wire).
3. Click BUY → highlight xanh đậm/trắng. Click SELL → đổi đỏ.
4. Type Entry "1.08500" → state cập nhật. Type SL/TP tương tự.
5. F5 reload → Entry/SL/TP/side reset (NOT persisted). Risk Amount + selectedPairId persist (Zustand whitelist).
6. Đổi symbol trên chart EURUSD → USDJPY: "Symbol" trong form đổi theo, `step` của input chuyển từ `0.00001` sang `0.001`.
7. DevTools Network: chỉ 1 GET `/api/pairs/` lúc form mount. Submit button không POST (disabled).
8. Xoá hết pair qua curl rồi reload → form hiện "No pairs configured".

Tạo pair test qua curl:
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

curl -X POST http://localhost:8000/api/pairs/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Test Pair","ftmo_account_id":"ftmo_001","exness_account_id":"exness_001","ratio":1.0}'
```

### Verify chart-form integration (sau khi step 2.9)

1. Backend chạy + cTrader OAuth + ticks streaming. Tạo ít nhất 1 pair (curl snippet ở step 2.8).
2. `cd web && npm run dev`. Login. Chọn EURUSD → live data flowing.
3. Right-click trên chart canvas → menu hiện 3 mục:
   - Set as Entry (price, 5 digits)
   - Set as SL (price, 5 digits)
   - Set as TP (price, 5 digits)
4. Click "Set as Entry" → form Entry populated; line **xanh dashed** ("Entry") hiện trên chart đúng giá đó.
5. Right-click dưới Entry → "Set as SL" → form SL populated; line **đỏ dashed** ("SL") xuất hiện.
6. Right-click trên Entry → "Set as TP" → form TP populated; line **xanh lá dashed** ("TP") xuất hiện.
7. Sau ~300ms, Volume Preview cập nhật: Vol P, Vol S, SL distance (pips), Est. SL $.
8. Type tay vào Entry/SL/TP → setup line trên chart đổi reactive; volume preview tính lại.
9. Xóa Entry (delete value) → blue line biến mất.
10. Đổi sang USDJPY → context menu hiển thị giá 3 digits. Setup line giữ nguyên (form state retained per CEO).
11. Set SL = Entry → volume preview về "idle" (Fill Entry, SL, Risk message).
12. DevTools Network: mỗi thay đổi form chỉ trigger 1 POST `/api/symbols/{symbol}/calculate-volume` SAU 300ms (debounce), KHÔNG mỗi keystroke.
13. Click ngoài menu → menu đóng. Right-click → menu mở lại.
14. F5 reload → Entry/SL/TP/side reset; risk + selectedPairId persist; setup lines biến mất theo form state.
15. Server thiếu rate cache (cold restart) → preview hiển thị "Conversion rate not ready, retry..." trong vài giây, sau đó tự thành công khi server subscribe-on-miss xong.

### Verify form enhancements (sau khi step 2.9a)

1. Login. Chọn EURUSD. Right-click set Entry/SL/TP → volume preview hiển thị auto values.
2. Đổi sang USDJPY → Entry/SL/TP **reset hết** + manual volume override (nếu có) cũng reset.
3. Click **BUY**. Set Entry, sau đó set SL **above** Entry → cảnh báo đỏ "SL must be below Entry for BUY" hiện dưới SL input. Volume calc vẫn chạy.
4. Click **SELL** → cảnh báo đảo chiều (TP > Entry sẽ trigger warning). Đặt SL > Entry → không còn warning.
5. Click nút × bên phải Entry → Entry clear, blue line mất khỏi chart.
6. Với auto volume hiển thị, click **"Override manually"** → Vol Primary thành input edit được (viền xanh). SL distance / Est. SL $ ẩn đi (không còn chính xác trong manual mode).
7. Sửa Vol Primary thành 0.50 → Vol Secondary auto cập nhật 0.50 (ratio 1.0).
8. Click **"↻ Reset to auto"** → quay về calculated value.
9. Tạo lỗi: Entry=1.08 SL=1.07999 (1 pip < min 5 pips) → server trả 400, error hiện kèm link "Override manually" (user vẫn có thể trade thủ công nếu chủ ý).
10. Trong manual override, đổi symbol → manual override cũng clear (auto mode resume).
11. F5 reload → toàn bộ field reset (entry/SL/TP/manualVolume KHÔNG persist; risk + selectedPairId persist).

### Verify side validation + manual metrics (sau khi step 2.9b)

1. Set BUY + Entry=1.085 + SL=1.090 (above Entry) → SL warning đỏ + Volume Preview hiện "BUY: SL must be below Entry". DevTools Network: KHÔNG có POST `/calculate-volume`.
2. Đổi SL=1.080 (dưới Entry) → error clear, volume calc chạy.
3. Đổi sang SELL với SL=1.080 (dưới Entry) → "SELL: SL must be above Entry" — không có POST.
4. Set TP sai chiều (BUY với TP < Entry) → soft warning bên cạnh TP input, volume calc VẪN CHẠY (TP optional).
5. Click "Override manually" với valid auto result → manual mode hiển thị Vol P (editable) + Vol S + **SL distance pips + Est. SL $** (manual mode giờ giữ metrics).
6. Sửa Vol P thành 0.50 → Est. SL $ cập nhật theo volume mới (sl_usd_per_lot × 0.50).
7. Tạo lỗi auto (503 / 400) trong khi manual mode → SL distance + Est. SL $ ẩn (không stale).
8. Side error xảy ra trong manual mode: đổi SL violate side → Volume Preview hiện side error; Vol P input vẫn edit được nhưng metrics ẩn. Fix SL → metrics quay lại NGAY (state.result preserved).
9. Phase-3 prep: Zustand `volumeReady` = true khi (auto ready hoặc manual > 0) AND không có side error; false trong các trường hợp khác.

## Phase 3 features (production-ready)

Phase 3 ship **single-leg FTMO trading** end-to-end. Exness hedging cascade defer Phase 4.

### Operator flow

1. **Login** — `admin` / `admin` (Phase 5 sẽ ship password change UI).
2. **Main page layout** — Header (title + WS pill + AccountStatusBar + Settings gear + Logout), HedgeChart (left 70%), HedgeOrderForm (right 30%), PositionList (bottom 35% của left column với Open + History tabs).
3. **AccountStatusBar header** — per-FTMO-account dot màu (online green / offline red / disabled gray) + balance + equity, refresh mỗi 5s qua WS broadcast.
4. **Order form** — Pair picker (dropdown từ `/api/pairs`), Symbol read-only (linked to pair primary symbol), Side BUY/SELL toggle, Order Type segmented Market/Limit/Stop (Market default), Entry/SL/TP inputs (auto-hidden trong Market mode — entry auto-drives từ throttled tick), Risk Amount USD input, VolumeCalculator (auto risk-based hoặc manual override), Submit button (disabled khi FTMO client offline/disabled với 3-tier tooltip).
5. **PositionList Open tab** — live P&L update mỗi 1s với USD conversion (JPY pairs via USDJPY bid). Mỗi row: Pair, Symbol, Side, Volume, Entry, Current Price, P&L, SL, TP, Close + Modify action buttons.
6. **PositionList History tab** — closed orders với time-range filter (default 7 ngày trailing). Mỗi row: Pair, Symbol, Side, Volume, Entry, Close, P&L, Close Reason (manual/sl/tp/stopout/unknown), Closed at.
7. **Settings modal** (gear icon header) — 2 tabs Pairs + Accounts. Pairs CRUD (create/edit/delete với 409 pair_in_use guard nếu có orders reference). Accounts toggle enabled (PATCH endpoint).
8. **Real-time updates** — order_updated + positions_tick + account_status broadcasts qua WebSocket, frontend reactive store updates.

### Phase 3 limitations

- Single FTMO account workflow (Phase 4 sẽ add Exness hedging cascade).
- Full position close only — no partial close (Phase 4+ scope).
- No drag-to-modify SL/TP trên chart (Phase 5 hardening backlog).
- No row click → chart overlay entry/SL/TP highlight (Phase 5).
- FTMO account create cần OAuth flow CLI Phase 3 (Settings UI defer Phase 5).
- Exness account dropdown Phase 3 vẫn text-input free-form (Phase 4 widens to dropdown khi accounts:exness populated).
- Single admin user (multi-user defer per non-goals).

### Stack version (production-tested)

- **Backend**: Python 3.12 + FastAPI + Pydantic v2 + redis.asyncio + uvicorn.
- **FTMO client**: Python 3.12 + Twisted (cTrader Open API protobuf bridge) + hedger-shared (OAuth + symbol mapping).
- **Frontend**: React 19 + TypeScript strict + Vite 8 + Tailwind 3 + Zustand 5 + Axios 1.16 + Lightweight Charts 5.2 + react-hot-toast.
- **Redis**: 7.x.
- **Tests**: server 473 (pytest + fakeredis), ftmo-client 177, web tsc + eslint + vite build clean.

Xem `docs/PHASE_3_REPORT.md` cho full acceptance table + step ledger + Phase 5 hardening backlog.

## Account Management (Phase 3+)

Trước khi server có thể route command đến FTMO/Exness client, account đó phải được đăng ký vào Redis. Step 3.2 giới thiệu CLI ops `scripts/init_account.py` để add / list / remove account. Sau khi `add` hoặc `remove`, **restart server** để `setup_consumer_groups()` picks up changes (Phase 4 sẽ làm runtime).

Chạy từ repo root (`/workspaces/ftmo_exness_hedge_v3`):

```bash
# Add account (validate broker + account_id format ^[a-z0-9_]{3,64}$)
python -m scripts.init_account add --broker ftmo --account-id ftmo_acc_001 --name "FTMO Challenge $100k"

# List all (or --broker ftmo|exness)
python -m scripts.init_account list

# Remove (dry-run preview; pass --yes to actually delete)
python -m scripts.init_account remove --broker ftmo --account-id ftmo_acc_001 --yes
```

Exit codes: `0` success, `1` unexpected error, `2` validation error (bad input, duplicate add, missing `--yes`, account not found).

### Working with Claude Code

Chạy Claude Code trực tiếp:

```bash
claude --dangerously-skip-permissions
```

Lưu ý: `scripts/claude-with-notify.sh` wrapper hoãn lại — xem D-019 trong `docs/DECISIONS.md`. Hiện tại chỉ post-commit hook fire Telegram notification (🔧 commit done trên branch `step/*`). Approve/inactivity trigger không hoạt động. Phase 5 sẽ viết lại wrapper bằng `script` để giữ TTY.

## Project structure

```
.
├── server/            # FastAPI backend (Python 3.12)
├── web/               # Vite + React 19 + TS + Tailwind frontend
├── shared/            # hedger_shared package (symbol mapping, dùng bởi server + clients)
├── ftmo-client/       # cTrader bridge (Phase 2+)
├── exness-client/     # MT5 bridge (Phase 4)
├── docs/              # Plan + state + decisions + reports
├── scripts/           # notify_telegram.sh, install-git-hooks.sh, ...
├── hooks/             # post-commit hook template
└── symbol_mapping_ftmo_exness.json   # 117 symbol mapping
```

## License
Internal tool — no public license.
