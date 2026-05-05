# Phase 1 — Báo cáo hoàn thành

> Thời điểm tạo: 2026-05-05
> Tag dự kiến: `phase-1-complete` (CEO sẽ tag thủ công sau khi merge step 1.8)
> Thời lượng thực tế: từ commit `ec0179a` (step 1.1, 2026-05-05 07:57:55+0700) đến commit `140179d` (step 1.7, 2026-05-05 11:06:57+0700) — **~3 giờ 9 phút**.

## Acceptance criteria — kết quả

Acceptance Phase 1 theo MASTER_PLAN_v2 Section 2:
- CEO có thể `docker-compose up` và truy cập UI ở localhost:5173 → SỬA LẠI sang devcontainer + LAN Redis (không docker-compose). PASS qua devcontainer + uvicorn + npm run dev.
- Login admin/admin → JWT → main layout với 3 panel. PASS (browser smoke test step 1.6 + 1.7).
- `/api/health` no auth → 200. PASS.
- `/api/symbols` yêu cầu auth → 401 / 200 với token. PASS.
- Telegram notify khi commit. PASS (chỉ commit hook; wrapper hoãn theo D-019).

| # | Test | Kết quả | Bằng chứng |
|---|---|---|---|
| 1 | docker-compose lifts services | SỬA LẠI | docker-compose bỏ theo D-006; dùng devcontainer + LAN Redis |
| 2 | localhost:5173 → Login UI | PASS | step 1.5+1.6 smoke test, browser verified |
| 3 | curl /api/health → 200 | PASS | step 1.2 + integration end-to-end |
| 4 | curl /api/auth/login admin/admin → token | PASS | step 1.4 |
| 5 | curl /api/symbols với token → 200 với 117 symbol | PASS | step 1.4 |
| 6 | Login UI auth flow | PASS | step 1.6 browser smoke (CEO verify), end-to-end backend curl đã pass |
| 7 | 3-panel layout với Open/History tabs | PASS | step 1.7 browser smoke (CEO verify), Tailwind arbitrary classes đã được JIT compile xác nhận |
| 8 | Telegram 🔧 commit notify | PASS | step 1.3 + ongoing |
| 9 | Mọi test pass + lint + typecheck + mypy strict | PASS | mọi step đều có check sạch |

## Decisions trong Phase 1
Xem `DECISIONS.md` từ D-015 đến D-030 (16 quyết định Phase 1).

Highlights:
- **D-019**: Telegram wrapper không dùng được vì TTY pipe issue. Chỉ commit hook hoạt động.
- **D-024**: React 19 default từ Vite scaffold (lệch khỏi React 18 trong plan).
- **D-029**: PositionList full-width layout (CEO override).

## Lệch khỏi MASTER_PLAN_v2

1. **Thứ tự step**: Telegram setup chuyển từ step 1.7 → step 1.3 (theo yêu cầu CEO để có notification sớm hơn). Step 1.3-1.6 cũ được renumber tích lũy thành 1.4-1.7. MASTER_PLAN_v2 Section 2 đã được cập nhật để phản ánh thứ tự thực tế.

2. **Sub-step 1.4a được thêm vào**: Bug fix về config robustness (CORS parsing + .env path) phát hiện trong smoke test step 1.4. Tách thành sub-step 1.4a với commit duy nhất. Pattern này được phép cho các bug fix không vừa với boundary của một step sạch.

3. **Không docker-compose**: Plan gốc dự kiến đặt Redis trong docker-compose. Thực tế dùng LAN Redis sẵn có ở `192.168.88.4`. Đơn giản hơn, không cần maintain thêm service.

4. **PositionList layout**: Diagram ASCII trong `docs/09-frontend.md` (sẽ tạo ở phase sau khi cần) đặt PositionList ở sidebar phải dưới HedgeOrderForm. CEO override step 1.7 → full-width dưới chart+form. Quyết định ghi tại D-029.

5. **React version**: Plan ghi React 18, scaffold đưa React 19. Chấp nhận (D-024).

6. **Một commit `test` không tuân thủ commit message convention**: commit `719d36f` với message `test` nằm giữa step 1.3 và 1.4. Đây là commit experimental của user, không phải step output. Phase 5 retrospective có thể làm sạch lịch sử bằng interactive rebase nếu cần.

7. **Một commit lẻ về MASTER_PLAN format**: commit `3f69892` ("docs: add prompt format rule to MASTER_PLAN_v2") nằm giữa step 1.1 và 1.2. Đây là cập nhật doc nhỏ, không thuộc step nào, được phép theo project convention.

## Known issues / TODO

Hoãn cho Phase 5 hardening:

1. **Toast notification thiếu cho 2 edge case**:
   - Session expired (token hỏng → 401 path)
   - Network error khi login (backend down)
   Cả 2 flow vẫn hoạt động về logic (logout fire, inline error hiện), chỉ thiếu toast. Nghi React 19 + react-hot-toast race hoặc StrictMode quirk. Reproduce + debug hoãn lại.

2. **Telegram wrapper script không dùng được**. Chỉ post-commit hook hoạt động. Approve/stuck trigger không thể fire vì pipe stdout monitor của wrapper phá vỡ TTY interactive của Claude Code. Workaround: CEO chạy `claude --dangerously-skip-permissions` trực tiếp. Phase 5 có thể viết lại bằng `script` để giữ TTY.

3. **`Settings._bootstrap_cors_origins` ở module level** là workaround cho crash lúc import khi env chưa set. Khuyến nghị refactor sang FastAPI dependency injection. Priority thấp, không có functional issue.

## Files added/modified summary

### New folders trong Phase 1
- `server/app/api/`, `server/app/services/`, `server/app/dependencies/`, `server/tests/`
- `shared/hedger_shared/`
- `web/src/components/{Header,Chart,OrderForm,PositionList}/`
- `web/src/{store,api}/`
- `scripts/`, `hooks/`

### Stats commit (đo bằng `git log` từ ec0179a đến HEAD trên `main`)
- Tổng commit Phase 1: **10** (gồm step 1.1 + 1 commit doc nhỏ + 1 commit `test` experimental + 7 step commit chính + step 1.4a sub-fix). Sau khi loại 2 commit lẻ, chỉ 8 step commit.
- Lines added: **8,673** (1,313 từ step 1.1 + 7,360 từ các commit sau).
- Lines deleted: **118**.
- Server tests: **27** (collected by `pytest --collect-only`).
- Web tests: 0 (Vitest setup hoãn lại).
- Bundle web/dist: **253.47 kB JS + 8.32 kB CSS** (sau step 1.7).
- Mypy strict: pass trên toàn bộ source server.

## Files modified xuyên suốt Phase 1
- `README.md` (giàu lên dần qua 1.1, 1.3, 1.4a, 1.5, 1.6, 1.8)
- `docs/MASTER_PLAN_v2.md` (rule additions Section 0 + reorder Section 2 + status tracker Section 8 ở step 1.8)

## Next phase prerequisites checklist

Trước khi bắt đầu Phase 2 (Market Data + Chart + Form), CEO cần có:

- [ ] **Credential cTrader demo account** (tách biệt với FTMO trading account).
  - Dùng cho market-data feed only. Không trade qua account này.
- [ ] **cTrader OAuth application đã đăng ký**:
  - Vào https://connect.spotware.com/apps
  - Tạo app mới, lấy `client_id` + `client_secret`.
- [ ] Quyết định nguồn demo account:
  - Option A: mở demo cTrader mới qua bất kỳ broker cTrader (vd IC Markets, Pepperstone).
  - Option B: dùng FTMO demo nếu đã có.
- [ ] Xác nhận `symbol_mapping_ftmo_exness.json` có các symbol CEO muốn test (đã verify: 117 symbol mapped, gồm EURUSD, USDJPY, XAUUSD, US30, BTCUSD).
