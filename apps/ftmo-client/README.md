# ftmo-client

Per-account FTMO trading client. One process drives one FTMO account
end-to-end: OAuth token → cTrader Open API connect → heartbeat publish
to Redis → XREADGROUP `cmd_stream:ftmo:{account_id}` for commands
pushed by the server.

Step 3.3 ships the **skeleton** — connect + heartbeat + command
dispatch with **stub action handlers** (log + ack only). Step 3.4 will
replace the stubs with real broker calls + response publishing.

## Install

The package is part of the monorepo and declares `hedger-shared` as a dep.
Since `hedger-shared` is a sibling package (not on PyPI), install via the
monorepo pattern that bypasses pip's resolver for sibling-package deps:

```bash
cd apps/ftmo-client

# Install ftmo-client itself, bypassing resolver
pip install --no-deps -e .

# Install runtime deps explicitly
pip install \
  "pydantic>=2.7,<3" \
  "pydantic-settings>=2.4,<3" \
  "redis[hiredis]>=5.0,<6" \
  "httpx>=0.27,<0.28" \
  "twisted>=23,<26" \
  "protobuf>=4.25,<6" \
  "service_identity>=24,<26" \
  "pyOpenSSL>=24,<26"

# Install dev/test deps
pip install \
  "fakeredis[lua]>=2.24" \
  "mypy>=1.10" \
  "pytest>=8" \
  "pytest-asyncio>=0.23" \
  "pytest-mock>=3.14" \
  "ruff>=0.5"
```

`hedger-shared` must be installed first from `../../shared/` (the devcontainer
post-create script handles this automatically; for ad-hoc shells outside the
devcontainer, run `pip install -e ../../shared` BEFORE the commands above).

Do NOT use `pip install -e .[dev]` — it fails because the resolver can't find
`hedger-shared` on PyPI.

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

## Smoke test: real trading (step 3.4)

Step 3.4 replaces the stub action handlers with real cTrader calls. After
running the step 3.3 connect smoke above and confirming the heartbeat is
green, exercise the trading path with these 7 sub-tests. Each is a
Python REPL snippet from the devcontainer — `redis-cli` isn't installed,
so we use `redis.asyncio.from_url` to XADD commands the same way the
server will in step 3.6+.

Prerequisite for every sub-test below: ftmo-client is running in another
terminal (`python -m ftmo_client.main`), and the cTrader UI is open
side-by-side so you can confirm broker-side state.

> **Note (step 3.4a)**: Market orders use a **2-RTT sequence** — the bridge
> sends the order without SL/TP, then issues `ProtoOAAmendPositionSLTPReq`
> against the resulting position. cTrader rejects absolute SL/TP on plain
> market sends (`SL/TP in absolute values are allowed only for order types:
> [LIMIT, STOP, STOP_LIMIT]`), so the split is required.
>
> If the amend step fails after the fill succeeds, the position stays
> **open WITHOUT SL/TP**. The response still has `status=success`, but
> with extra fields `sl_tp_attach_failed=True`,
> `sl_tp_attach_error_code=…`, `sl_tp_attach_error_msg=…`. The operator
> must attach SL/TP manually via the cTrader UI (or issue a fresh
> `modify_sl_tp` command) in that case. Phase 4+ will surface this as a
> frontend warning toast.
>
> Limit / stop orders unchanged — they accept absolute SL/TP in one RTT.
>
> **Note (step 3.4b)**: `broker_order_id` semantics depend on order
> type. For a **filled market order**, it's the cTrader **positionId**
> (lifecycle: open → close; what `modify_sl_tp` and `close` operate
> on). For a **pending limit/stop order**, it's the cTrader **orderId**
> (lifecycle: submit → fill or cancel). When the pending order
> eventually fills, step 3.5's event handler will issue an unsolicited
> resp_stream entry that swaps orderId → positionId for that order_id.
> Server-side order_service uses whichever id is current on the order
> hash to dispatch subsequent close / modify commands.
>
> **Note (step 3.4c)**: `close_position` now uses the same two-event
> handling as `place_market_order` — cTrader emits `ORDER_ACCEPTED`
> then `ORDER_FILLED` for a close, both carrying the same
> `clientMsgId`, and only the `ORDER_FILLED` event has the
> `deal.closePositionDetail` sub-message that carries `realized_pnl`.
> The bridge waits for `ORDER_FILLED` via the `_pending_executions`
> side channel (renamed from `_pending_market_fills` in 3.4c so it
> covers both open and close). `modify_sl_tp` is unchanged — it's a
> single-event response (`ORDER_REPLACED`).
>
> See `docs/ctrader-execution-events.md` for the documented cTrader
> event behavior reverse-engineered from smoke tests + protobuf
> DESCRIPTOR inspection. Update that file when you discover new
> behaviors (unsolicited fills from manual closes, SL/TP hits,
> margin-call stop-outs, etc.).

The Python preamble (run once per shell session):

```python
import asyncio, time, uuid
import redis.asyncio as r
client = r.from_url("redis://192.168.88.4:6379/2", decode_responses=True)

async def xadd(action, **fields):
    fields["action"] = action
    # Server normally sets request_id; for manual smoke we synthesize it.
    fields.setdefault("request_id", uuid.uuid4().hex)
    fields.setdefault("created_at", str(int(time.time() * 1000)))
    msg_id = await client.xadd(
        "cmd_stream:ftmo:ftmo_acc_001", fields, maxlen=10000, approximate=True
    )
    return msg_id

async def drain_resp(after_id="0"):
    return await client.xread({"resp_stream:ftmo:ftmo_acc_001": after_id}, block=2000)
```

### 1. Place market order

```python
await xadd("open",
    order_id="ord_test_market",
    symbol="EURUSD",
    side="buy",
    order_type="market",
    volume_lots="0.01",
    sl="1.07000",  # below current bid (check live spread first)
    tp="1.09500",
    entry_price="0",
)
await drain_resp()
```

Expected: ftmo-client log
`[STUB step 3.4]` is GONE; instead see `published response: action=open
order_id=ord_test_market status=success`. The `resp_stream` entry has
`status=success`, `broker_order_id=<positionId>`, `fill_price=<float>`,
`fill_time=<ms>`. cTrader UI shows the new open position.

### 2. Place limit order

```python
await xadd("open",
    order_id="ord_test_limit",
    symbol="EURUSD",
    side="buy",
    order_type="limit",
    volume_lots="0.01",
    entry_price="1.07000",  # well below current ask
    sl="1.06000",
    tp="1.08000",
)
await drain_resp()
```

Expected: `resp_stream` entry has `status=success` with
`broker_order_id=<pending orderId>`, `fill_price=""`, `fill_time=""`.
cTrader UI shows a pending limit order.

### 3. Place stop order

```python
await xadd("open",
    order_id="ord_test_stop",
    symbol="EURUSD",
    side="buy",
    order_type="stop",
    volume_lots="0.01",
    entry_price="1.10000",  # above current ask
    sl="1.09000",
    tp="1.11000",
)
await drain_resp()
```

Expected: same as limit — `status=success`, pending `broker_order_id`.

### 4. Modify SL/TP of the position from sub-test 1

```python
# Use the broker_order_id from sub-test 1's resp entry.
await xadd("modify_sl_tp",
    order_id="ord_test_market",
    broker_order_id="<positionId from #1>",
    sl="1.06800",
    tp="1.09800",
)
await drain_resp()
```

Expected: `resp_stream` entry `status=success`, `new_sl=1.068`,
`new_tp=1.098`. cTrader UI shows updated SL/TP on the position.

### 5. Close the position from sub-test 1

```python
await xadd("close",
    order_id="ord_test_market",
    symbol="EURUSD",  # needed for volume conversion
    broker_order_id="<positionId from #1>",
    volume_lots="0.01",
)
await drain_resp()
```

Expected: `status=success`, `close_price=<float>`, `close_time=<ms>`.
Position disappears from cTrader UI.

### 6. Error case — close a non-existent position

```python
await xadd("close",
    order_id="ord_test_bad_close",
    symbol="EURUSD",
    broker_order_id="99999999",
    volume_lots="0.01",
)
await drain_resp()
```

Expected: `status=error`,
`error_code=position_not_found` (or `broker_error` if cTrader returns a
different code — note the exact `error_msg` and feed it back to CTO so
`retcode_mapping.py` can be extended).

### 7. Error case — open with invalid SL distance

```python
# BUY with SL above current bid (invalid direction).
await xadd("open",
    order_id="ord_test_bad_sl",
    symbol="EURUSD",
    side="buy",
    order_type="market",
    volume_lots="0.01",
    sl="9.99999",  # nonsense, above any FX bid
    tp="0",
    entry_price="0",
)
await drain_resp()
```

Expected: `status=error` with `error_code` in
`{invalid_sl_distance, price_off, broker_error}`. Capture the exact
`error_msg` for `retcode_mapping.py` extension.

## Smoke test: events + account info (step 3.5)

Step 3.5 adds two new behaviors: unsolicited events from cTrader are
published to `event_stream:ftmo:{account_id}`, and account info
(balance/margin) is HSET to `account:ftmo:{account_id}` every 30s.
This smoke verifies both — same Python preamble as the step 3.4
section above; reuse the same shell session.

> See `docs/ctrader-execution-events.md §3.2-3.6, §4.3, §10` for the
> documented protobuf shapes that drive each sub-test. `close_reason`
> is inferred from price comparison vs `position.stopLoss` /
> `position.takeProfit` (cTrader does not provide a structured
> close-reason field — verified via DESCRIPTOR inspection in step 3.5).

Helpers (extend the step-3.4 preamble):

```python
async def drain_event(n: int = 1):
    # Read the next n entries from event_stream (block 2s for activity).
    entries = await client.xread({f"event_stream:ftmo:{ACC}": "$"}, block=2000, count=n)
    print(entries)

async def show_account_info():
    info = await client.hgetall(f"account:ftmo:{ACC}")
    print(info)
```

### Sub-test 1 — Pending limit fill → `pending_filled`

Place a LIMIT well below market (so it sits pending), then either
wait for market to retrace or move the cTrader UI's limit price to
trigger an instant fill.

```python
await xadd("open",
    order_id="ord_pf",
    symbol="EURUSD",
    side="buy",
    order_type="limit",
    volume_lots="0.01",
    entry_price="1.00000",  # well below market
    sl="0.99500",
    tp="1.01000",
)
await drain_resp()  # status=success, broker_order_id=<orderId>
# Wait for fill (or amend the limit price on cTrader UI to current bid).
await drain_event()
```

Expected event_stream entry:
```
event_type=pending_filled
broker_order_id=<positionId>   ← NEW; replaces orderId on order:{id}
position_id=<positionId>
order_id_old=<original orderId>
fill_price=<float>
fill_time=<epoch ms>
```

### Sub-test 2 — User close on cTrader UI → `position_closed, close_reason=manual`

Open a position via FTMO client (step 3.4 path), then close it on the
cTrader UI.

```python
await xadd("open",
    order_id="ord_close",
    symbol="EURUSD",
    side="buy",
    order_type="market",
    volume_lots="0.01",
    sl="0",
    tp="0",          # no SL/TP set → heuristic returns "manual"
    entry_price="0",
)
await drain_resp()
# Now close via cTrader UI (right-click position → Close)
await drain_event()
```

Expected:
```
event_type=position_closed
broker_order_id=<positionId>
close_price=<actual close>
close_time=<epoch ms>
realized_pnl=<raw int, scale by money_digits>
close_reason=manual
```

### Sub-test 3 — SL hit → `close_reason=sl`

Open a position with a tight SL just outside spread; wait for market
to drift through.

```python
await xadd("open",
    order_id="ord_sl",
    symbol="EURUSD",
    side="buy",
    order_type="market",
    volume_lots="0.01",
    sl="1.08000",    # set 5-10 pips below current bid
    tp="1.10000",
    entry_price="0",
)
await drain_resp()
# Wait for market move (may take minutes; skip if smoke time-boxed).
await drain_event()
```

Expected `close_reason=sl` (close_price within 1 pip of SL).

### Sub-test 4 — TP hit → `close_reason=tp`

Same as sub-test 3 but with a tight TP.

```python
await xadd("open",
    order_id="ord_tp",
    symbol="EURUSD",
    side="buy",
    order_type="market",
    volume_lots="0.01",
    sl="1.06000",
    tp="1.08500",    # 5-10 pips above current ask
    entry_price="0",
)
```

Expected `close_reason=tp`.

### Sub-test 5 — User modify SL/TP on cTrader UI → `position_modified`

Open a position, then drag the SL or TP line on the cTrader UI to a
new price.

```python
await xadd("open",
    order_id="ord_mod",
    symbol="EURUSD",
    side="buy",
    order_type="market",
    volume_lots="0.01",
    sl="1.07000",
    tp="1.09000",
    entry_price="0",
)
await drain_resp()
# Drag SL on cTrader UI to e.g. 1.06500.
await drain_event()
```

Expected:
```
event_type=position_modified
broker_order_id=<positionId>
new_sl=<new SL>
new_tp=<existing TP, or empty if cleared>
```

### Sub-test 6 — Account info publishing after 35s

After the FTMO client has run for >30s post-startup, verify the
account info hash is populated.

```python
await show_account_info()
```

Expected fields:
```
balance=<raw int>        ← e.g. "100000" for $1000.00 with money_digits=2
equity=<same as balance> ← step 3.5 limitation; documented in docs/ctrader-execution-events.md §10
margin=<raw int>         ← sum of usedMargin across open positions
free_margin=<balance - margin>
currency=USD
money_digits=2
updated_at=<epoch ms>
```

### Sub-test 7 — Account info updates every poll

Capture `updated_at` from sub-test 6, wait 31s, re-read. The value
must change (otherwise the loop isn't running or is stuck).

```python
import time
before = (await client.hgetall(f"account:ftmo:{ACC}"))["updated_at"]
await asyncio.sleep(31)
after = (await client.hgetall(f"account:ftmo:{ACC}"))["updated_at"]
assert int(after) > int(before), f"updated_at didn't change: {before} → {after}"
```

## Tests

```bash
cd apps/ftmo-client
pytest -q
```

Uses `fakeredis[lua]` so no live Redis is needed for unit tests. The
cTrader bridge is mocked in `test_main_wiring.py` +
`test_ctrader_bridge_actions.py`; wire-level tests against the real
broker are CEO-driven via the smoke section above.
