# PROJECT_STATE — Live snapshot

> Cập nhật lần cuối: 2026-05-05 — Claude Code (step 1.8 phase-1 docs sync)
> ĐỌC FILE NÀY ĐẦU TIÊN khi mở phiên CTO chat mới.

## Vị trí hiện tại
- **Phase**: 1 — Foundation — HOÀN THÀNH
- **Step vừa hoàn thành**: 1.8 — phase-1-docs-sync (chính step này)
- **Phase tiếp theo**: 2 — Market Data + Chart + Form (read-only)
- **Step tiếp theo**: 2.1 — server cTrader bridge + symbol sync from broker
- **Blocker**: không

## Tóm tắt Phase 1
9 step đã merge: 1.1, 1.2, 1.3, 1.4, 1.4a, 1.5, 1.6, 1.7, 1.8 (1.4a là sub-fix, không phải step độc lập trong plan).
Tag dự kiến: `step-1.1` ... `step-1.7`, cộng `phase-1-complete` (CEO sẽ tag thủ công sau khi merge step 1.8).

Server: FastAPI + JWT + symbols whitelist + 3 endpoint (`/health` unauth, `/auth/login`, `/symbols/*` protected).
Web: React 19 + TS strict + Tailwind v3 + Zustand + Axios + Login + 3-panel layout (header / chart 70% + form 380px / PositionList full-width có Open+History tabs).
Infra: devcontainer Linux, Redis LAN tại `192.168.88.4`, Telegram commit notify (chỉ commit hook, wrapper bị bypass — xem D-019).

## Active context (state runtime để smoke)
- Backend chạy ở `http://localhost:8000`.
- Frontend dev server ở `http://localhost:5173` với Vite proxy `/api` → backend.
- Default credentials: `admin` / `admin` (hash trong `.env` → `ADMIN_PASSWORD_HASH`).
- Symbol mapping: 117 symbol load từ `symbol_mapping_ftmo_exness.json`.
- 27 server-side tests passing, mọi build check (typecheck/lint/format/build) đều green.

## Known issues — hoãn cho Phase 5 hardening
- Toast notification thiếu cho 2 edge case:
  - Session expired (token bị hỏng → 401 → logout thực hiện nhưng toast không hiện)
  - Network error khi login (backend down → inline error hiện nhưng toast không hiện)
  Cần reproduce + debug; nghi React 19 + react-hot-toast race hoặc StrictMode quirk.
- Telegram wrapper script bị pipe phá vỡ TTY interactive của Claude Code. Chỉ commit hook hoạt động. Approve/stuck trigger không khả dụng. Workaround: CEO chạy `claude --dangerously-skip-permissions` trực tiếp.
- Helper `_bootstrap_cors_origins` ở module level là workaround cho crash lúc import. Refactor sang FastAPI dependency injection — priority thấp, không có functional issue.

## Recent decisions (top 5, full list trong DECISIONS.md)
- D-029: PositionList full-width dưới chart+form (CEO override step 1.7)
- D-024: React 19 thay vì React 18 (Vite scaffold default)
- D-022: `NoDecode` annotation cho CORS env parsing (pydantic-settings v2)
- D-019: Telegram wrapper bị bypass (TTY pipe issue)
- D-013: Quy ước ngôn ngữ — CEO↔CTO Vietnamese, CTO→Claude English

## Pending items / TODO
- Phase 2.1: cTrader OAuth flow setup endpoint (CEO cần cung cấp credential cTrader demo account).
- Phase 5: viết lại Telegram wrapper bằng `script` command để giữ TTY.
- Phase 5: debug missing toast cho session-expired và network-error.
- Phase 5: refactor Settings để inject env qua FastAPI Depends thay vì `_bootstrap_cors_origins` module-level.

## Quick reference
- Repo: https://github.com/matran19900/ftmo_exness_hedge_v3
- Workspace: `/workspaces/ftmo_exness_hedge_v3`
- Redis: `redis://192.168.88.4:6379/2` (LAN)
- Symbol count: 117 mapped (chưa kiểm tra count unmapped trong Phase 1)
- Test accounts: FTMO live (CEO có account, dùng từ Phase 3+)
- Python: 3.12, Node: 24 (devcontainer cài v22 nhưng thực tế thấy v24), npm: 11.12.1
- Stack versions: React 19.2, Vite 8.0, TypeScript 6.0, Tailwind 3.4, Zustand 5.0, Axios 1.16, react-hot-toast 2.6
