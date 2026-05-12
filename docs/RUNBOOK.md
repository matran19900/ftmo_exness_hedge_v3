# RUNBOOK

> Hướng dẫn vận hành: dev workflow, common operations, troubleshooting. Phase 3 production-ready scope; Phase 5 sẽ thêm production deploy + disaster recovery (NSSM, Memurai, Tailscale).

## 1. Dev environment startup

### Terminal 1 — Backend server

```bash
cd /workspaces/ftmo_exness_hedge_v3/server
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

`--reload` flag: uvicorn tự restart on file change. Khuyến nghị cho dev. Production omit (Phase 5 dùng NSSM service config).

Backend lifespan startup order (D-088):
1. Redis init + setup_consumer_groups (idempotent với BUSYGROUP swallow).
2. MarketDataService start (cTrader market-data bridge — yêu cầu OAuth done).
3. Per-FTMO-account loops: response_handler + event_handler + position_tracker.
4. Global loop: account_status_loop.

### Terminal 2 — FTMO client

```bash
cd /workspaces/ftmo_exness_hedge_v3/apps/ftmo-client
source ../../server/.venv/bin/activate  # shared venv với server (hedger-shared lib)
python -m ftmo_client.main
```

FTMO client process per FTMO account. Default config trong `.env` (ACCOUNT_ID, REDIS_URL, CTRADER_CLIENT_ID/SECRET/ACCESS_TOKEN). Heartbeat publishes `client:ftmo:{account_id}` HASH với TTL 30s (refresh mỗi 10s). Server check `EXISTS` để determine online/offline.

### Terminal 3 — Frontend dev server

```bash
cd /workspaces/ftmo_exness_hedge_v3/web
npm run dev
```

Vite serves at `http://localhost:5173`. Backend proxy `/api/*` + `/ws` → `http://localhost:8000` (xem `web/vite.config.ts`).

## 2. Common operations

### Restart backend sau merge code changes

Nếu uvicorn chạy WITHOUT `--reload`:
1. Ctrl+C trong Terminal 1.
2. Re-run uvicorn command.

Verify endpoint registered:
```bash
curl -s http://localhost:8000/openapi.json | python -m json.tool | grep -E "/api/(orders|positions|accounts|history)"
```

### Restart FTMO client sau merge

Backend `--reload` KHÔNG reload FTMO client process. Mỗi FTMO client code change:
1. Ctrl+C trong Terminal 2.
2. Re-run python module.

Sau restart, FTMO client sẽ:
- Reconnect cTrader OAuth (heartbeat resume).
- Trigger reconciliation snapshot (ReconcileOpenPositionsReq + ReconcilePendingOrdersReq) → publish `reconcile_state:ftmo:{acc}` stream → server consume idempotently.
- Backfill closed orders missing trong server cache via DealListByPositionIdReq (max 3 retry, exponential backoff 1s/2s — D-079).

### Frontend hard refresh sau merge

Vite HMR usually handles. Nếu state stale (vd Zustand persisted state shape thay đổi):
- Ctrl+Shift+R (Linux/Win) / Cmd+Shift+R (Mac) hard reload.
- Clear localStorage nếu persistence-related changes: DevTools → Application → Storage → Clear site data.

### Login defaults

- Username: `admin`
- Password: `admin`

Phase 5 sẽ ship password change UI + per-user config.

## 3. Redis inspection

### Inspect order state

```bash
python << 'EOF'
import redis
r = redis.Redis(host='192.168.88.4', port=6379, db=2, decode_responses=True)

# Open orders (filled status)
print("Filled orders:", r.smembers('orders:by_status:filled'))

# Single order detail
print("Order ord_xyz:", r.hgetall('order:ord_xyz'))

# Position cache (live P&L)
print("Position cache ord_xyz:", r.hgetall('position_cache:ord_xyz'))

# Account state
print("FTMO account info:", r.hgetall('account:ftmo:ftmo_acc_001'))
print("FTMO account meta:", r.hgetall('account_meta:ftmo:ftmo_acc_001'))

# Heartbeat (existence check)
print("FTMO client status:", r.hgetall('client:ftmo:ftmo_acc_001'))
print("EXISTS:", r.exists('client:ftmo:ftmo_acc_001'))

# Pair config
print("Pair pair_main:", r.hgetall('pair:pair_main'))

# Tick cache
print("EURUSD tick:", r.get('tick:EURUSD'))

# Streams
print("cmd_stream length:", r.xlen('cmd_stream:ftmo:ftmo_acc_001'))
print("resp_stream length:", r.xlen('resp_stream:ftmo:ftmo_acc_001'))
print("event_stream length:", r.xlen('event_stream:ftmo:ftmo_acc_001'))
EOF
```

### Inspect WS broadcast in dev

Backend logs WS subscribe/unsubscribe + broadcast count. Frontend DevTools Network → WS tab → frame inspector. Channels Phase 3:
- `ticks:{symbol}` — per WS broadcast tick (~5-10Hz raw, sau D-118 coalesce có thể skip một số partial).
- `candles:{symbol}:{tf}` — per candle update.
- `positions` — positions_tick từ position_tracker_loop mỗi 1s; position_event từ unsolicited execution events.
- `orders` — order_updated từ response_handler + event_handler.
- `accounts` — account_status từ account_status_loop mỗi 5s.

## 4. API operations

### Login + get JWT

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' \
  | python -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

echo $TOKEN
```

### FTMO account enable/disable toggle

```bash
# Disable
curl -X PATCH http://localhost:8000/api/accounts/ftmo/ftmo_acc_001 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# Re-enable
curl -X PATCH http://localhost:8000/api/accounts/ftmo/ftmo_acc_001 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

Status precedence (D-128): `enabled=false` → `disabled` overrides heartbeat. `enabled=true` + heartbeat alive → `online`; `enabled=true` + heartbeat dead → `offline`.

### List orders + positions

```bash
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/orders?limit=10" | python -m json.tool
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/positions" | python -m json.tool
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/accounts" | python -m json.tool
```

### Close + modify order

```bash
# Close (full close only Phase 3 D-100)
curl -X POST "http://localhost:8000/api/orders/<order_id>/close" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'

# Modify SL/TP (None=keep, 0=remove, positive=set D-101)
curl -X POST "http://localhost:8000/api/orders/<order_id>/modify" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sl": 1.07800, "tp": 1.08600}'
```

### FTMO account initial setup

```bash
# 1. Add to Redis via ops script
python -m scripts.init_account add --broker ftmo --account-id ftmo_acc_001 --name "FTMO Challenge $100k"

# 2. Restart server → setup_consumer_groups picks up new account
# (Phase 4 sẽ làm runtime via API)

# 3. FTMO client (Terminal 2) start với ACCOUNT_ID=ftmo_acc_001 trong .env
# → cTrader OAuth flow một lần qua browser → access_token paste vào .env
```

## 5. Troubleshooting

### Submit button disabled với tooltip

Phase 3 OrderForm 3-tier ftmoBlockMessage (D-148):

| Tooltip | Cause | Fix |
|---|---|---|
| "Chưa có FTMO account được cấu hình" | No FTMO accounts trong Redis | `python -m scripts.init_account add --broker ftmo ...` |
| "FTMO account đã bị vô hiệu hóa (mở Settings → Accounts để bật lại)" | All FTMO accounts có `enabled=false` | Open Settings modal (gear icon) → Accounts tab → toggle ON |
| "FTMO client offline (heartbeat đã expired)" | Heartbeat key `client:ftmo:{acc}` không tồn tại trong Redis | Restart FTMO client (Terminal 2) |

### Order stuck pending

1. Check order state: `r.hgetall('order:<order_id>')` — `status=pending` + `p_status=pending`.
2. Check command queued: `r.xrange('cmd_stream:ftmo:<acc>', '-', '+')` — should see entry với command fields.
3. Check FTMO client logs (Terminal 2) cho error response.
4. Check `resp_stream:ftmo:<acc>` đã có response chưa: `r.xrange('resp_stream:ftmo:<acc>', '-', '+')`.
5. Nếu response có status=error → check `error_code` + `error_msg` fields cho cause (validation fail, cTrader rejected, etc.).

### P&L showing $0 hoặc wrong value

1. Check tick freshness: `r.get('tick:<symbol>')` — JSON parse, check `ts` field within 5s từ `int(time.time()*1000)`.
2. Check USD conversion route (D-092):
   - Symbol quote currency derived từ `symbol[-3:]` cho 6-char FX (D-095).
   - JPY quote: requires `USDJPY` tick. Subscribe nếu missing.
   - Cross quote: tries `USD{quote}` direct first, then `{quote}USD` inverse.
3. Check position_cache: `r.hgetall('position_cache:<order_id>')` — `is_stale` field, `tick_age_ms` field.
4. Check defensive guards (D-117): nếu `is_stale=true` với conv_stale fallback, server logs WARNING.

### WS reconnect issues

1. Check backend log cho WS handshake errors (token decode, channel validation).
2. Browser DevTools → Network → WS → status code (101 = upgrade success).
3. Frontend pill nên show 🟢 connected. Nếu yellow/red → wsState slice = connecting/disconnected → useWebSocket reconnect logic exponential backoff.
4. JWT expired (mỗi 60 phút mặc định) → 401 → useEffect dispatch logout → re-login.

### Pair delete bị 409 pair_in_use

Phase 3 guard (D-142): DELETE /api/pairs/{id} reject nếu có pending/filled orders reference. Server message format `Cannot delete pair: N order(s) reference it. Close them first.` Operator close those orders trước → retry delete.

### Backend không reload sau code change

- `--reload` flag trên uvicorn enabled chưa? (Section 1).
- `--reload-include` patterns matching changed file? (default `*.py`).
- Manual Ctrl+C + re-run nếu hot-reload skip.

## 6. Phase 3 known issues / Phase 5 backlog

Xem `docs/PHASE_3_REPORT.md` §9 cho full backlog list. Highlight:

- **Server**: pair_orders:{pair_id} SET index, idempotency-key headers, dead-letter sweeper, contract_size persist non-FX.
- **FTMO client**: protocol-level disconnect order, retry amend after POSITION_LOCKED, hasMore pagination DealListByPositionIdRes.
- **Frontend**: Vitest setup, custom ConfirmModal thay window.confirm, drag SL/TP trên chart, row click → chart overlay.
- **Operations**: Telegram wrapper rewrite TTY-safe, Phase 5 production deploy NSSM + Memurai + Tailscale.

## 7. CTO chat workflow

Tạo CTO chat mới: paste `docs/CTO_HANDOFF_TEMPLATE.md` template.

CTO reads (mandatory order):
1. `MASTER_PLAN_v2.md` — plan tổng thể.
2. `docs/PROJECT_STATE.md` — snapshot hiện tại (cập nhật sau mỗi step PASS).
3. `docs/DECISIONS.md` — cumulative decisions D-001 → D-149 (Phase 3 complete).
4. `docs/PHASE_<N>_REPORT.md` — latest phase report (Phase 3 = `PHASE_3_REPORT.md`).

Quy trình step (mỗi step):
1. CTO viết prompt 1 code block duy nhất.
2. CEO copy → Claude Code (devcontainer Linux).
3. Claude Code: `git checkout -b step/N.M-slug` + implement + `git commit` (post-commit hook fires Telegram 🔧).
4. CEO nhận Telegram, copy kết quả về CTO.
5. CTO review (6 tiêu chí trong WORKFLOW §7.2).
6. PASS → user merge squash + tag `step-N.M` + xóa branch + CTO update PROJECT_STATE → next prompt.
7. REJECT → user xóa/archive branch + CTO update blocker → fix prompt.

## 8. Repo layout reference

```
/workspaces/ftmo_exness_hedge_v3/
├── server/                      # FastAPI backend (Python 3.12)
│   ├── app/
│   │   ├── api/                 # REST routers (orders, positions, history, accounts, pairs, symbols, charts, ws, auth)
│   │   ├── services/            # business logic (redis_service, market_data, broadcast, order_service, response_handler, event_handler, position_tracker, account_status, account_helpers, symbol_whitelist)
│   │   ├── dependencies/        # FastAPI DI (auth)
│   │   ├── main.py              # lifespan + router include
│   │   └── config.py            # pydantic-settings
│   └── tests/                   # pytest (473 tests Phase 3)
├── apps/ftmo-client/            # FTMO trading client (Python + Twisted)
│   ├── ftmo_client/             # main, bridge_service, action_handler, command_processor, event_processor, account_info, reconciliation, heartbeat, shutdown
│   └── tests/                   # pytest (177 tests Phase 3)
├── apps/exness-client/          # Exness MT5 client (Phase 4)
├── shared/hedger_shared/        # Symbol mapping + cTrader OAuth (shared package)
├── web/                         # React 19 + TS + Vite frontend
│   └── src/                     # api/client.ts, components/, hooks/, lib/, store/
├── docs/                        # MASTER_PLAN_v2 + PROJECT_STATE + DECISIONS + PHASE_*_REPORT + WORKFLOW + RUNBOOK + tech docs 02-13
├── scripts/                     # notify_telegram.sh, init_account.py, install-git-hooks.sh
├── hooks/                       # post-commit hook template
└── symbol_mapping_ftmo_exness.json   # 117 symbol whitelist
```

## 9. Phase 5 deployment (placeholders — sẽ điền)

### Tailscale setup (Windows Server 2022)
TODO Phase 5.

### Memurai installation (Redis on Windows)
TODO Phase 5.

### NSSM service config
TODO Phase 5.

### Backup procedures
TODO Phase 5 (cron daily backup script).

### Disaster recovery
TODO Phase 5 (server crash, Redis crash, client crash recovery procedures).

### Health check checklist
TODO Phase 5 (smoke test matrix `docs/12-business-rules.md` Section 12).

## 10. Legacy Phase 1 operational notes (preserved)

### Common issues từ Phase 1

- `bcrypt ModuleNotFoundError`: phải chạy trong `server/.venv`.
- `.env` không tìm thấy: dùng absolute path hoặc chạy từ repo root (đã được fix ở step 1.4a — Settings tự resolve `.env` ở root via `Path(__file__).resolve().parents[2]`).
- `CORS_ORIGINS` parse error: dùng comma-separated (`http://a,http://b`) hoặc JSON list (`["http://a","http://b"]`), tránh URL thô không quote.
- Telegram commit notify không fire: kiểm tra `~/.config/hedger-sandbox/telegram.env` có TOKEN + CHAT_ID, và `bash scripts/install-git-hooks.sh` đã chạy trên branch `step/*`.
- Port 8000 stuck: `fuser -k 8000/tcp`.
