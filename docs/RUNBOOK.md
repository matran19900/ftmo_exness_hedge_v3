# RUNBOOK

> Hướng dẫn vận hành cho production deployment và disaster recovery.
> Phase 5 sẽ điền đầy đủ. Hiện tại là skeleton với các marker TODO.

## Thêm account mới
TODO Phase 4: viết flow Settings UI.
TODO Phase 5: viết flow restart server để tạo consumer group.

## Restart server
TODO Phase 5.

## Disaster recovery — server crash
TODO Phase 5.

## Disaster recovery — Redis crash
TODO Phase 5.

## Disaster recovery — client (FTMO/Exness) crash
TODO Phase 5.

## Backup procedures
TODO Phase 5.

## Health check checklist
TODO Phase 5.

## Tailscale setup (Windows Server 2022)
TODO Phase 5.

## Memurai installation (Redis on Windows)
TODO Phase 5.

## NSSM service config
TODO Phase 5.

## Smoke test full
TODO Phase 5: link tới test matrix trong `12-business-rules.md` Section 12 (sẽ tạo ở phase tương ứng).

## Phase 1 operational notes (hiện tại)

### Chạy local
- Backend: `cd server && source .venv/bin/activate && uvicorn app.main:app --port 8000`
- Frontend: `cd web && npm run dev`
- Browser: http://localhost:5173

### Dừng
- Cả 2: Ctrl+C trong terminal tương ứng.
- Port 8000 stuck: `fuser -k 8000/tcp`.

### Common issues
- `bcrypt ModuleNotFoundError`: phải chạy trong `server/.venv`.
- `.env` không tìm thấy: dùng absolute path hoặc chạy từ repo root (đã được fix ở step 1.4a, Settings tự resolve `.env` ở root).
- `CORS_ORIGINS` parse error: dùng comma-separated (`http://a,http://b`) hoặc JSON list (`["http://a","http://b"]`), tránh URL thô không quote.
- Telegram commit notify không fire: kiểm tra `~/.config/hedger-sandbox/telegram.env` có TOKEN + CHAT_ID, và `bash scripts/install-git-hooks.sh` đã chạy trên branch `step/*`.
