# FTMO Hedge Tool v3

Công cụ hedge thủ công giữa FTMO (cTrader) và Exness (MT5). Mỗi lệnh FTMO được hedge bằng một lệnh Exness ngược chiều ở volume tỉ lệ tính toán, để lỗ một bên được offset gần đủ bằng lãi bên kia.

## Documentation
- Master plan: [docs/MASTER_PLAN_v2.md](docs/MASTER_PLAN_v2.md)
- Trạng thái hiện tại: [docs/PROJECT_STATE.md](docs/PROJECT_STATE.md)
- Sổ quyết định: [docs/DECISIONS.md](docs/DECISIONS.md)
- Báo cáo phase 1: [docs/PHASE_1_REPORT.md](docs/PHASE_1_REPORT.md)
- Runbook (skeleton, sẽ điền dần ở Phase 5): [docs/RUNBOOK.md](docs/RUNBOOK.md)
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
