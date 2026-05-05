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
