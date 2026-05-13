# exness-client

Per-account Exness MT5 trading client. One process drives one Exness
account end-to-end: MT5 terminal connect → heartbeat publish to Redis →
`XREADGROUP cmd_stream:exness:{account_id}` for commands pushed by the
server.

Step 4.1 ships the **skeleton** — connect (with hedging-mode assertion)
+ heartbeat + command dispatch as a **stub** (log + ack only). Step 4.2
fills in the real action handlers, position monitor poll, and account
info publish.

## Platform constraint

**The `MetaTrader5` Python package is Windows-only.** Production
runtime must be Windows 10/11 or Windows Server 2022 — `pip install
MetaTrader5` fails outright on Linux/macOS because no wheel exists.

For Linux development and CI, this package ships `exness_client.mt5_stub`
— a module-level stub mirroring the MT5 API surface used by step 4.1.
`main.py` selects the module via `sys.platform`:

| Platform | mt5 module |
|---|---|
| `win32` | `MetaTrader5` (real lib) |
| anything else | `exness_client.mt5_stub` (test stub) |

Unit tests on Linux exercise the stub. **Live MT5 smoke tests are
Windows-only.**

## Install (Linux dev)

```bash
# 1. hedger-shared must be installed first.
pip install -e /workspaces/ftmo_exness_hedge_v3/shared

# 2. Install exness-client without resolving sibling-package deps.
cd /workspaces/ftmo_exness_hedge_v3/apps/exness-client
pip install --no-deps -e .

# 3. Install runtime deps explicitly (MetaTrader5 skipped via marker).
pip install \
  "pydantic>=2.7,<3" \
  "pydantic-settings>=2.4,<3" \
  "redis[hiredis]>=5.0,<6"

# 4. Install dev/test deps.
pip install \
  "fakeredis[lua]>=2.24" \
  "mypy>=1.10" \
  "pytest>=8" \
  "pytest-asyncio>=0.23" \
  "pytest-mock>=3.14" \
  "ruff>=0.5"
```

Do **NOT** use `pip install -e .[dev]` — the resolver tries to fetch
`hedger-shared` from PyPI and fails.

## Install (Windows operator)

```cmd
:: 1. Install Python 3.12+ (https://www.python.org/downloads/).
:: 2. Install Exness-branded MT5 terminal and log into the account
::    interactively at least once so the credentials cache is warm.
:: 3. Clone the repo to e.g. C:\hedger-sandbox.
:: 4. Open PowerShell:

cd C:\hedger-sandbox\apps\exness-client
pip install -e ..\..\shared
pip install --no-deps -e .
pip install "pydantic>=2.7,<3" "pydantic-settings>=2.4,<3" "redis[hiredis]>=5.0,<6" "MetaTrader5>=5.0.45"
copy .env.example .env
:: Edit .env with credentials, then:
exness-client
```

## Configure

Copy `.env.example` to `.env` and fill in the placeholders:

| Key | Purpose |
|---|---|
| `ACCOUNT_ID` | Local account id (lowercase alphanum + underscore, 3-64 chars) — must match what you registered server-side via `python -m scripts.init_account add`. |
| `REDIS_URL` | LAN Redis URL, e.g. `redis://192.168.88.4:6379/2`. |
| `MT5_LOGIN` | Exness MT5 login (integer). |
| `MT5_PASSWORD` | Exness MT5 password. SecretStr — never logged. |
| `MT5_SERVER` | Exness MT5 server name, e.g. `Exness-MT5Trial7`. |
| `MT5_PATH` | Optional path to MT5 terminal exe. Used when multiple MT5 builds are installed. |

Optional: `HEARTBEAT_INTERVAL_S` (default 5.0), `CMD_STREAM_BLOCK_MS`
(default 1000), `LOG_LEVEL` (default INFO).

## Hedging mode requirement

The Exness MT5 account **MUST** be in hedging margin mode (not netting).
Cascade close in `docs/phase-4-design.md §1.E` assumes per-position
handles; netting mode collapses positions into one net handle per symbol
and silently breaks cascade close.

The client fails fast on startup if `mt5.account_info().margin_mode` is
not `ACCOUNT_MARGIN_MODE_RETAIL_HEDGING`. Symptom: process exits with
code 2 and logs `mt5_account_netting_mode_not_supported`.

To check / change mode: open the Exness web cabinet → Account →
Settings → "Margin Mode" → select Hedging.

## Tests

```bash
cd /workspaces/ftmo_exness_hedge_v3/apps/exness-client
pytest -v
```

Uses `fakeredis[lua]` so no live Redis is needed. The MT5 lib is
swapped for `exness_client.mt5_stub`. No live broker connection at any
point during unit tests.

## Step 4.1 scope (this skeleton)

| Module | Status |
|---|---|
| `config.py` | DONE — pydantic-settings, 7 fields, account-id format validator |
| `mt5_stub.py` | DONE — initialize / shutdown / account_info / terminal_info / last_error + test helpers |
| `bridge_service.py` | DONE — `connect` (with hedging-mode assertion), `disconnect`, `is_connected`, `health_check` |
| `command_processor.py` | DONE (skeleton) — XREADGROUP loop reads cmd, logs `action_not_implemented_phase_4_1`, XACKs |
| `heartbeat.py` | DONE — 5 s HSET `client:exness:{acc}` with 30 s TTL |
| `shutdown.py` | DONE — `ShutdownCoordinator` with D-088 teardown order |
| `main.py` | DONE — lifecycle orchestrator + module selection + exit codes 0/1/2 |

## Step 4.2+ (NOT in this skeleton)

- Real action handlers (`open` / `close` / `modify_sl_tp`) — step 4.2.
- Position monitor poll loop (2 s) for external close detection — step 4.2.
- Account info publish (30 s HSET `account:exness:{acc}`) — step 4.2.
- Retcode mapping for MT5 trade retcodes — step 4.2.
- Reconciliation on connect — step 4.4+ hardening.

See `docs/phase-4-design.md` and `docs/mt5-execution-events.md` for the
full design.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Graceful shutdown (SIGINT / SIGTERM received). |
| 1 | MT5 connect failed (`mt5.initialize` returned False or `account_info` is None). Check `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` and that the MT5 terminal can connect. |
| 2 | MT5 account is in netting margin mode. Switch to hedging mode in Exness cabinet. |
