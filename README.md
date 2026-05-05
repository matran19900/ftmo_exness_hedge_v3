# FTMO Hedge Tool v3

Manual hedging tool between FTMO (cTrader) and Exness (MT5).

## Documentation

- [Master plan](docs/MASTER_PLAN_v2.md)
- [Runbook](docs/RUNBOOK.md)

## Dev environment

### Initial setup

1. Clone the repo: `git clone https://github.com/matran19900/ftmo_exness_hedge_v3.git`
2. Open in VS Code with the Dev Containers extension. Rebuild the container on first open.
3. Set up Telegram credentials (one-time per devcontainer volume):

   ```bash
   mkdir -p ~/.config/hedger-sandbox
   cat > ~/.config/hedger-sandbox/telegram.env <<EOF
   TELEGRAM_BOT_TOKEN=<your-bot-token>
   TELEGRAM_CHAT_ID=<your-chat-id>
   EOF
   chmod 600 ~/.config/hedger-sandbox/telegram.env
   ```

4. Install git hooks: `bash scripts/install-git-hooks.sh`
5. (Future steps will add Python venv activation, web npm install, etc.)

### First-time auth setup

Generate secrets and write the full `.env` at the repo root (do NOT commit `.env`):

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

Notes:

- All Python helper commands that import project deps (like `bcrypt`) must run inside the venv: `cd server && source .venv/bin/activate`.
- Settings loads `.env` from the repo root regardless of which directory you run uvicorn / pytest from (the path is resolved relative to `server/app/config.py`, not `cwd`).
- `CORS_ORIGINS` accepts comma-separated (`http://a,http://b`) or JSON-list (`["http://a","http://b"]`).

Default password is `admin`. Change it before any real deployment.

### Web frontend

After devcontainer rebuild, web deps install automatically via post-create.sh. To run dev server:

```bash
cd web
npm run dev
```

Open http://localhost:5173. Login form appears.

Default credentials (development): `admin` / `admin` (or whatever `ADMIN_PASSWORD_HASH` in `.env` was generated from).

After login:

- Token saved in localStorage (persists across page refresh).
- Symbol count loads from `/api/symbols/`.
- Logout button in top-right clears the token, returns to login.
- Session expiry (server returns 401) auto-logs out with toast notification.

If backend is not running, login submit will show a network error toast. Start it with:
`cd server && source .venv/bin/activate && uvicorn app.main:app --port 8000`

### Working with Claude Code

ALWAYS run Claude Code via the wrapper:

```bash
bash scripts/claude-with-notify.sh
```

Do NOT call `claude` directly — you would lose Telegram notifications for permission prompts and inactivity.

The wrapper sends 3 types of Telegram notifications:

- 🔧 Commit on a `step/*` branch (via the git post-commit hook).
- ⚠️ Permission prompt detected (via stdout pattern match).
- 💤 No output for 90s (via inactivity watchdog).

Throttle: max 1 notification per type per 3 minutes. State lives in `/tmp/claude_notify_throttle/`.
