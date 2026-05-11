# ftmo-client

Per-account FTMO trading client. One process drives one FTMO account
end-to-end: OAuth token → cTrader Open API connect → heartbeat publish
to Redis → XREADGROUP `cmd_stream:ftmo:{account_id}` for commands
pushed by the server.

Step 3.3 ships the **skeleton** — connect + heartbeat + command
dispatch with **stub action handlers** (log + ack only). Step 3.4 will
replace the stubs with real broker calls + response publishing.

## Install

The package is part of the monorepo. Install in editable mode from the
repo root:

```bash
cd apps/ftmo-client
pip install -e .[dev]
# Or install the runtime-only deps:
pip install -e .
```

`hedger-shared` is a sibling package and must be installed as well —
the devcontainer post-create script handles this; for ad-hoc shells:

```bash
pip install -e ../../shared
```

## Configure

Copy the example env file and fill in the placeholders:

```bash
cp .env.example .env
# Edit .env with FTMO_ACCOUNT_ID, REDIS_URL, CTRADER_CLIENT_ID,
# CTRADER_CLIENT_SECRET, CTRADER_REDIRECT_URI.
```

Required keys:

| Key | Purpose |
|---|---|
| `FTMO_ACCOUNT_ID` | Local account id (lowercase alphanum + underscore, 3–64 chars) — must match what you registered via `python -m scripts.init_account add`. |
| `REDIS_URL` | LAN Redis URL, e.g. `redis://192.168.88.4:6379/2`. |
| `CTRADER_CLIENT_ID` / `CTRADER_CLIENT_SECRET` | Open API app creds (apply at <https://openapi.ctrader.com>). |
| `CTRADER_REDIRECT_URI` | OAuth callback target. Must match the value registered on the cTrader app page. Default `http://localhost:8765/callback`. |

Optional: `CTRADER_HOST`, `CTRADER_PORT`, `LOG_LEVEL`.

## Smoke test: live FTMO cTrader connect + heartbeat

This is the first step that touches FTMO live. **No orders are placed**
— only OAuth + heartbeat.

### Prerequisites

- FTMO live account with cTrader trading platform enabled.
- cTrader Open API client_id + client_secret (apply at <https://openapi.ctrader.com>).
- Redis running (LAN at `192.168.88.4:6379` db 2 per dev convention).
- Server with step 3.2 merged (consumer groups for `ftmo_acc_001` will
  be created on server start).

### Steps

1. **Add the account in Redis** (if not already done):

   ```bash
   python -m scripts.init_account add \
     --broker ftmo --account-id ftmo_acc_001 \
     --name "FTMO Live $100k"
   ```

   Restart the server → consumer groups for `ftmo_acc_001` are created.

2. **Configure ftmo-client `.env`**:

   ```bash
   cd apps/ftmo-client
   cp .env.example .env
   # Edit FTMO_ACCOUNT_ID=ftmo_acc_001, REDIS_URL, CTRADER_CLIENT_ID,
   # CTRADER_CLIENT_SECRET, CTRADER_REDIRECT_URI=http://localhost:8765/callback
   ```

3. **Run the OAuth flow once** (browser opens, grant access):

   ```bash
   python -m ftmo_client.scripts.run_oauth_flow --account-id ftmo_acc_001
   ```

   On success the token is saved to `ctrader:ftmo:ftmo_acc_001:creds`
   in Redis (HASH; HGETALL to inspect).

4. **Start the FTMO client**:

   ```bash
   python -m ftmo_client.main
   ```

   Expected log lines:
   - `ftmo-client starting: account=ftmo_acc_001`
   - `redis connected`
   - `oauth token loaded (ctid_trader_account_id=...)`
   - `cTrader TCP connected; sending app auth`
   - `CtraderBridge authenticated for account=ftmo_acc_001 ...`
   - `heartbeat_loop starting for account=ftmo_acc_001 (interval=10s, ttl=30s)`
   - `command_loop starting: stream=cmd_stream:ftmo:ftmo_acc_001 ...`

5. **Verify heartbeat in Redis** (separate terminal):

   ```bash
   redis-cli -h 192.168.88.4 -p 6379 -n 2 HGETALL client:ftmo:ftmo_acc_001
   redis-cli -h 192.168.88.4 -p 6379 -n 2 TTL client:ftmo:ftmo_acc_001
   ```

   Expect: `status=online`, `last_seen=<recent epoch ms>`, `version=0.3.0`,
   TTL between 20 and 30 seconds.

6. **Verify command stub via fake command** (optional):

   ```bash
   redis-cli -h 192.168.88.4 -p 6379 -n 2 XADD cmd_stream:ftmo:ftmo_acc_001 '*' \
     order_id ord_test001 action open symbol EURUSD side buy volume_lots 0.01 \
     sl 1.08000 tp 0 order_type market entry_price 0 \
     request_id req_test001 created_at $(date +%s%3N)
   ```

   Expected ftmo-client log:

   ```
   [STUB step 3.4] open: account=ftmo_acc_001 order_id=ord_test001 symbol=EURUSD side=buy ...
   ```

   **No real order placed** — the stub only logs and XACKs.

7. **Graceful shutdown**: Ctrl+C. Expect:
   - `shutdown signal received`
   - `shutdown initiated; cancelling tasks`
   - `heartbeat_loop exiting (account=ftmo_acc_001)`
   - `command_loop exiting (account=ftmo_acc_001)`
   - `cTrader bridge stopped for account=ftmo_acc_001`
   - `ftmo-client shutdown complete`
   - Process exits with code 0.

### Common errors

- **`no OAuth token in Redis at ctrader:ftmo:...:creds`** — Step 3
  hasn't been run for this account, or the token was deleted. Re-run
  `run_oauth_flow`.
- **`cTrader connect/app-auth timed out after 30s`** — Verify
  `CTRADER_CLIENT_ID` + `CTRADER_CLIENT_SECRET` are correct, and that
  the FTMO account has cTrader Open API enabled.
- **`xreadgroup failed: NOGROUP No such key 'cmd_stream:ftmo:...' or
  consumer group 'ftmo-...' in XREADGROUP with GROUP option`** — Server
  hasn't been restarted after the account was added. Restart the server
  so its lifespan calls `setup_consumer_groups()`.
- **Heartbeat key TTL = -1 (no expiry)** — Bug in `heartbeat.py`.
  Report to CTO; the EXPIRE call should run on every beat.
- **`OAuth token at ctrader:ftmo:...:creds is expired (or within
  skew)`** — Re-run `run_oauth_flow`. Step 3.5 will add automatic
  refresh using the stored `refresh_token`.

## Tests

```bash
cd apps/ftmo-client
pytest -q
```

Uses `fakeredis[lua]` so no live Redis is needed for unit tests. The
cTrader bridge is mocked in `test_main_wiring.py`; wire-level tests
against the real broker are CEO-driven via the smoke test above.
