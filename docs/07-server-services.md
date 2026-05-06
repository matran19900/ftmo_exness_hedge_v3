# 07 — Server Services

Module: `apps/server/app/services/`

## 1. `redis_service.py`

Lớp truy cập Redis duy nhất. Mọi service khác inject vào đây.

### 1.1 Setup consumer groups

```python
async def setup_consumer_groups(self):
    """Tạo consumer groups cho mọi account đã đăng ký."""
    ftmo_accs = await self.get_all_account_ids("ftmo")
    exness_accs = await self.get_all_account_ids("exness")
    
    for acc in ftmo_accs:
        await self._create_group(f"cmd_stream:ftmo:{acc}", f"ftmo-{acc}")
        await self._create_group(f"resp_stream:ftmo:{acc}", "server")
        await self._create_group(f"event_stream:ftmo:{acc}", "server")
    
    for acc in exness_accs:
        await self._create_group(f"cmd_stream:exness:{acc}", f"exness-{acc}")
        await self._create_group(f"resp_stream:exness:{acc}", "server")
        await self._create_group(f"event_stream:exness:{acc}", "server")

async def _create_group(self, stream, group):
    try:
        await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
```

### 1.2 Push command

```python
async def push_command(self, broker: str, account_id: str, fields: dict) -> str:
    """Returns request_id."""
    request_id = uuid4().hex
    fields["request_id"] = request_id
    fields["created_at"] = str(int(time.time() * 1000))
    
    stream = f"cmd_stream:{broker}:{account_id}"
    await self.redis.xadd(stream, fields, maxlen=10000, approximate=True)
    
    # Track pending
    await self.redis.zadd(
        f"pending_cmds:{broker}:{account_id}",
        {request_id: int(time.time() * 1000)},
    )
    return request_id
```

### 1.3 Read responses (loop helper)

```python
async def read_responses(self, broker: str, account_id: str, count: int = 10):
    """Used in response_reader_loop."""
    stream = f"resp_stream:{broker}:{account_id}"
    return await self.redis.xreadgroup(
        groupname="server",
        consumername="server",
        streams={stream: ">"},
        count=count,
        block=1000,
    )

async def read_events(self, broker: str, account_id: str, count: int = 10):
    stream = f"event_stream:{broker}:{account_id}"
    return await self.redis.xreadgroup(
        groupname="server",
        consumername="server",
        streams={stream: ">"},
        count=count,
        block=1000,
    )

async def ack(self, stream: str, group: str, msg_id: str):
    await self.redis.xack(stream, group, msg_id)
```

### 1.4 Order CRUD

```python
async def create_order(self, order_id: str, fields: dict) -> None: ...
async def get_order(self, order_id: str) -> dict | None: ...
async def update_order(self, order_id: str, patch: dict, old_status: str | None = None) -> bool:
    """Idempotent update. Returns False if old_status check fails."""
    ...
async def list_orders_by_status(self, status: str) -> list[dict]: ...
async def list_closed_orders(self, limit: int, offset: int) -> list[dict]: ...
```

### 1.5 Pending tracking

```python
async def remove_pending(self, broker: str, account_id: str, request_id: str): ...
async def get_stuck_pending(self, broker: str, account_id: str, max_age_seconds: int) -> list[tuple[str, int]]:
    """Returns [(request_id, age_ms)] for entries older than max_age_seconds."""
    ...
```

### 1.6 Tick / Position cache

```python
async def set_tick(self, symbol: str, bid: float, ask: float, ts: int): ...
async def get_tick(self, symbol: str) -> dict | None: ...
async def set_position_pnl(self, order_id: str, snapshot: dict): ...
async def get_position_pnl(self, order_id: str) -> dict | None: ...
```

### 1.7 Snapshots

```python
async def add_snapshot(self, order_id: str, ts: int, pnl: float): ...
async def get_snapshots(self, order_id: str) -> list[tuple[int, float]]: ...
```

### 1.8 Heartbeat & account info

```python
async def get_client_status(self, broker: str, account_id: str) -> str:
    """Returns 'online' if HEARTBEAT key exists, else 'offline'."""
    ...
async def get_all_client_statuses(self) -> dict[str, str]: ...
async def get_account_info(self, broker: str, account_id: str) -> dict | None: ...
```

### 1.9 Settings

```python
async def get_settings(self) -> dict: ...
async def patch_settings(self, patch: dict) -> dict: ...
```

### 1.10 Symbol config

```python
async def set_symbol_config(self, symbol: str, config: dict): ...
async def get_symbol_config(self, symbol: str) -> dict | None: ...
async def list_active_symbols(self) -> list[dict]: ...
```

### 1.11 Account & pair management

```python
async def add_account(self, broker: str, account_id: str, name: str, enabled: bool): ...
async def remove_account(self, broker: str, account_id: str): ...
async def get_all_account_ids(self, broker: str) -> list[str]: ...
async def get_account_meta(self, broker: str, account_id: str) -> dict | None: ...

async def add_pair(self, pair_id: str, ftmo_acc: str, exness_acc: str, ratio: float, name: str): ...
async def remove_pair(self, pair_id: str): ...
async def get_pair(self, pair_id: str) -> dict | None: ...
async def list_pairs(self) -> list[dict]: ...
```

## 2. `symbol_whitelist.py`

```python
class SymbolWhitelist:
    def __init__(self, file_path: str):
        self._entries: dict[str, MappingRow] = {}
        self.load_from_file(file_path)
    
    def load_from_file(self, path: str):
        with open(path) as f:
            data = json.load(f)
        for entry in data["entries"]:
            self._entries[entry["ftmo_symbol"]] = MappingRow(**entry)
    
    def is_allowed(self, ftmo_symbol: str) -> bool:
        return ftmo_symbol in self._entries
    
    def get(self, ftmo_symbol: str) -> MappingRow | None:
        return self._entries.get(ftmo_symbol)
    
    def all_symbols(self) -> list[str]:
        return list(self._entries.keys())
    
    def map_to_exness(self, ftmo_symbol: str) -> str | None:
        entry = self._entries.get(ftmo_symbol)
        return entry.exness_symbol if entry else None
    
    def volume_conversion_ratio(self, ftmo_symbol: str) -> float:
        entry = self._entries.get(ftmo_symbol)
        if not entry: return 1.0
        return entry.ftmo_units_per_lot / entry.exness_trade_contract_size
```

## 3. `order_service.py`

### 3.1 `create_hedge_order(request, redis_svc, market_data, whitelist) -> CreateOrderResponse`

```python
async def create_hedge_order(req: CreateHedgeOrderRequest, ...):
    # 1. Whitelist check
    if not whitelist.is_allowed(req.symbol):
        raise HTTPException(404, f"symbol {req.symbol} not in whitelist")
    
    # 2. Pair config
    pair = await redis_svc.get_pair(req.pair_id)
    if not pair or not pair["enabled"]:
        raise HTTPException(404, f"pair {req.pair_id} not found or disabled")
    
    ftmo_acc = pair["ftmo_account_id"]
    exness_acc = pair["exness_account_id"]
    ratio = float(req.secondary_ratio or pair["secondary_ratio"])
    
    # 3. Heartbeat check both
    if await redis_svc.get_client_status("ftmo", ftmo_acc) != "online":
        raise HTTPException(503, f"ftmo client {ftmo_acc} offline")
    if await redis_svc.get_client_status("exness", exness_acc) != "online":
        raise HTTPException(503, f"exness client {exness_acc} offline")
    
    # 4. Get symbol config
    config = await redis_svc.get_symbol_config(req.symbol)
    if not config:
        raise HTTPException(404, f"symbol_config not synced for {req.symbol}")
    
    # 5. Resolve entry price
    if req.order_type == "market":
        tick = await redis_svc.get_tick(req.symbol)
        if not tick:
            raise HTTPException(503, "no tick available")
        entry = tick["ask"] if req.side == "buy" else tick["bid"]
    else:
        entry = req.entry_price
    
    # 6. Validate SL/TP direction
    validate_sl_tp(req.side, entry, req.sl_price, req.tp_price)
    
    # 7. Calc volume
    vol_p, vol_s = calculate_volume(
        risk_amount=req.risk_amount,
        entry=entry,
        sl=req.sl_price,
        symbol_config=config,
        whitelist_row=whitelist.get(req.symbol),
        ratio=ratio,
    )
    
    # 8. Create order in Redis
    order_id = generate_order_id()
    now_ms = int(time.time() * 1000)
    order_fields = {
        "order_id": order_id,
        "pair_id": req.pair_id,
        "ftmo_account_id": ftmo_acc,
        "exness_account_id": exness_acc,
        "symbol": req.symbol,
        "side": req.side,
        "status": "pending",
        "risk_amount": str(req.risk_amount),
        "secondary_ratio": str(ratio),
        "sl_price": str(req.sl_price),
        "tp_price": str(req.tp_price),
        "order_type": req.order_type,
        "entry_price": str(entry),
        "p_status": "pending",
        "p_volume_lots": str(vol_p),
        "s_status": "waiting_primary",
        "s_volume_lots": str(vol_s),
        "created_at": str(now_ms),
        "updated_at": str(now_ms),
    }
    await redis_svc.create_order(order_id, order_fields)
    
    # 9. Push command to FTMO
    exness_symbol = whitelist.map_to_exness(req.symbol)
    request_id = await redis_svc.push_command("ftmo", ftmo_acc, {
        "order_id": order_id,
        "action": "open",
        "symbol": req.symbol,
        "side": req.side,
        "volume_lots": str(vol_p),
        "sl": str(req.sl_price),
        "tp": str(req.tp_price),
        "order_type": req.order_type,
        "entry_price": str(entry) if req.order_type != "market" else "0",
    })
    
    return CreateOrderResponse(order_id=order_id, status="pending")
```

### 3.2 `calculate_volume(...)`

Implement R6, R15:
```
sl_pips = abs(entry - sl) / pip_size
pip_value (quote_ccy / lot) = pip_size × ftmo_contract_size
pip_value_usd = convert(pip_value, quote_ccy → USD using conversion_pair tick)
sl_usd_per_lot = sl_pips × pip_value_usd
volume_p_raw = risk_amount / sl_usd_per_lot
volume_p = clamp_round(volume_p_raw, min, max, step)

volume_s_raw = volume_p × ratio × (ftmo_units_per_lot / exness_contract_size)
volume_s = clamp_round_exness(volume_s_raw, exness min/max/step)
```

Min SL pips = 5 (configurable). Reject nếu nhỏ hơn.

### 3.3 Validators

- `validate_sl_tp(side, entry, sl, tp)`: BUY → sl<entry, tp>entry; SELL → sl>entry, tp<entry. tp=0 → skip.
- `validate_sl_distance(sl_pips, min_sl_pips)`: raise nếu sl_pips < min.

### 3.4 `close_order(order_id) -> None`

User clicks × hoặc cTrader close. Server gửi command close primary → callback → cascade close secondary.

## 4. `response_handler.py`

Đọc `resp_stream:*` và `event_stream:*`, update order state, trigger cascade.

### 4.1 Main loop

```python
async def response_reader_loop(redis_svc, broadcast):
    while True:
        all_accs = await redis_svc.get_all_account_pairs()  # [("ftmo", acc), ("exness", acc)]
        for broker, acc in all_accs:
            try:
                resp_entries = await redis_svc.read_responses(broker, acc, count=10)
                for stream, msgs in resp_entries:
                    for msg_id, fields in msgs:
                        await handle_response(broker, acc, fields, redis_svc, broadcast)
                        await redis_svc.ack(stream, "server", msg_id)
                
                event_entries = await redis_svc.read_events(broker, acc, count=10)
                for stream, msgs in event_entries:
                    for msg_id, fields in msgs:
                        await handle_event(broker, acc, fields, redis_svc, broadcast)
                        await redis_svc.ack(stream, "server", msg_id)
            except Exception:
                log.exception("response_reader error broker=%s acc=%s", broker, acc)
        await asyncio.sleep(0.1)
```

### 4.2 `handle_response`

```python
async def handle_response(broker, account_id, resp, redis_svc, broadcast):
    order_id = resp["order_id"]
    request_id = resp["request_id"]
    action = resp["action"]
    status = resp["status"]
    
    # Remove pending tracking
    await redis_svc.remove_pending(broker, account_id, request_id)
    
    order = await redis_svc.get_order(order_id)
    if not order:
        log.warning("response for unknown order: %s", order_id)
        return
    
    if action == "open":
        if broker == "ftmo":
            await _handle_primary_open_response(order, resp, redis_svc, broadcast)
        else:
            await _handle_secondary_open_response(order, resp, redis_svc, broadcast)
    elif action == "close":
        if broker == "ftmo":
            await _handle_primary_close_response(order, resp, redis_svc, broadcast)
        else:
            await _handle_secondary_close_response(order, resp, redis_svc, broadcast)

async def _handle_primary_open_response(order, resp, redis_svc, broadcast):
    if resp["status"] == "error":
        await redis_svc.update_order(order["order_id"], {
            "status": "cancelled",
            "p_status": "error",
            "p_error_msg": resp["error_msg"],
        })
        await broadcast.send("positions", {
            "type": "order_error", "order_id": order["order_id"], "msg": resp["error_msg"]
        })
        return
    
    # filled
    await redis_svc.update_order(order["order_id"], {
        "status": "primary_filled",
        "p_status": "filled",
        "p_broker_order_id": resp["broker_order_id"],
        "p_fill_price": resp["fill_price"],
        "p_executed_at": resp["fill_time"],
        "p_commission": resp.get("commission", "0"),
    }, old_status="pending")
    
    await broadcast.send("positions", {
        "type": "primary_filled", "order_id": order["order_id"]
    })
    
    # Trigger secondary open
    await _send_secondary_open(order, redis_svc)

async def _send_secondary_open(order, redis_svc):
    whitelist = get_symbol_whitelist()
    exness_symbol = whitelist.map_to_exness(order["symbol"])
    secondary_side = "sell" if order["side"] == "buy" else "buy"
    
    await redis_svc.push_command("exness", order["exness_account_id"], {
        "order_id": order["order_id"],
        "action": "open",
        "symbol": exness_symbol,
        "side": secondary_side,
        "volume_lots": order["s_volume_lots"],
        "order_type": "market",
    })
    await redis_svc.update_order(order["order_id"], {"s_status": "pending"})
```

### 4.3 `handle_event` — cascade close trigger

```python
async def handle_event(broker, account_id, ev, redis_svc, broadcast):
    if ev["event_type"] not in ("position_closed", "position_closed_external"):
        return
    
    broker_order_id = ev["broker_order_id"]
    
    # Find order by broker_order_id
    if broker == "ftmo":
        order = await find_order_by_p_broker_order_id(redis_svc, broker_order_id)
    else:
        order = await find_order_by_s_broker_order_id(redis_svc, broker_order_id)
    
    if not order or order["status"] in ("closed", "closing"):
        return  # idempotency
    
    # Update closed leg
    if broker == "ftmo":
        leg_patch = {
            "p_status": "closed",
            "p_close_price": ev["close_price"],
            "p_closed_at": ev["close_time"],
            "p_close_reason": ev["close_reason"],
            "p_realized_pnl": ev.get("realized_pnl", "0"),
        }
    else:
        leg_patch = {
            "s_status": "closed",
            "s_close_price": ev["close_price"],
            "s_closed_at": ev["close_time"],
            "s_close_reason": ev["close_reason"],
            "s_realized_pnl": ev.get("realized_pnl", "0"),
        }
    
    leg_patch["status"] = "closing"
    await redis_svc.update_order(order["order_id"], leg_patch)
    
    # Trigger cascade close on the other leg
    if broker == "ftmo" and order["s_status"] == "filled":
        await _cascade_close_secondary(order, redis_svc)
    elif broker == "exness" and order["p_status"] == "filled":
        await _cascade_close_primary(order, redis_svc)
    
    # If both legs closed → finalize
    fresh = await redis_svc.get_order(order["order_id"])
    if fresh["p_status"] == "closed" and fresh["s_status"] == "closed":
        final_pnl = float(fresh["p_realized_pnl"]) + float(fresh["s_realized_pnl"])
        await redis_svc.update_order(order["order_id"], {
            "status": "closed",
            "closed_at": str(int(time.time() * 1000)),
            "final_pnl_usd": str(final_pnl),
        })
        await redis_svc.add_to_closed_history(order["order_id"], int(time.time() * 1000))
        await broadcast.send("positions", {"type": "hedge_closed", "order_id": order["order_id"]})

async def _cascade_close_secondary(order, redis_svc):
    if order["s_broker_order_id"]:
        await redis_svc.push_command("exness", order["exness_account_id"], {
            "order_id": order["order_id"],
            "action": "close",
            "broker_order_id": order["s_broker_order_id"],
        })

async def _cascade_close_primary(order, redis_svc):
    if order["p_broker_order_id"]:
        await redis_svc.push_command("ftmo", order["ftmo_account_id"], {
            "order_id": order["order_id"],
            "action": "close",
            "broker_order_id": order["p_broker_order_id"],
        })
```

## 5. `position_tracker.py`

```python
async def position_tracker_loop(redis_svc, market_data, broadcast):
    while True:
        try:
            open_orders = await redis_svc.list_orders_by_status("open")
            for order in open_orders:
                await update_pnl(order, redis_svc, market_data, broadcast)
        except Exception:
            log.exception("tracker error")
        await asyncio.sleep(1.0)

async def update_pnl(order, redis_svc, market_data, broadcast):
    symbol = order["symbol"]
    tick = await redis_svc.get_tick(symbol)
    if not tick:
        return
    
    config = await redis_svc.get_symbol_config(symbol)
    
    # P&L per leg in quote currency
    p_pnl_quote = leg_pnl(
        side=order["side"], entry=float(order["p_fill_price"]),
        current_bid=tick["bid"], current_ask=tick["ask"],
        volume=float(order["p_volume_lots"]),
        contract_size=float(config["ftmo_contract_size"]),
    )
    s_side = "sell" if order["side"] == "buy" else "buy"
    s_pnl_quote = leg_pnl(
        side=s_side, entry=float(order["s_fill_price"]),
        current_bid=tick["bid"], current_ask=tick["ask"],
        volume=float(order["s_volume_lots"]),
        contract_size=float(config["exness_contract_size"]),
    )
    
    # Convert to USD
    quote_ccy = config["quote_asset"]
    rate = await get_quote_to_usd_rate(quote_ccy, redis_svc, market_data)
    p_pnl_usd = p_pnl_quote * rate
    s_pnl_usd = s_pnl_quote * rate
    
    snapshot = {
        "order_id": order["order_id"],
        "symbol": symbol,
        "p_pnl_usd": p_pnl_usd,
        "s_pnl_usd": s_pnl_usd,
        "total_pnl_usd": p_pnl_usd + s_pnl_usd,
        "computed_at": int(time.time() * 1000),
    }
    
    await redis_svc.set_position_pnl(order["order_id"], snapshot)
    await broadcast.send("positions", {"type": "pnl_update", **snapshot})
    
    # Snapshot mỗi 30s
    if should_snapshot(order):
        await redis_svc.add_snapshot(order["order_id"], snapshot["computed_at"], p_pnl_usd + s_pnl_usd)


def leg_pnl(side, entry, current_bid, current_ask, volume, contract_size) -> float:
    """P&L in quote currency."""
    if side == "buy":
        return (current_bid - entry) * volume * contract_size
    else:
        return (entry - current_ask) * volume * contract_size


async def get_quote_to_usd_rate(quote_ccy, redis_svc, market_data):
    if quote_ccy == "USD":
        return 1.0
    if quote_ccy == "JPY":
        usdjpy = await redis_svc.get_tick("USDJPY")
        if not usdjpy:
            await market_data.subscribe_spots(["USDJPY"])
            return 0.0  # skip this round
        return 1.0 / usdjpy["bid"]
    # Other: implement as needed (EUR, GBP, AUD, ...)
    pair = f"{quote_ccy}USD"
    tick = await redis_svc.get_tick(pair)
    if tick:
        return tick["bid"]
    inv_pair = f"USD{quote_ccy}"
    tick = await redis_svc.get_tick(inv_pair)
    if tick:
        return 1.0 / tick["bid"]
    await market_data.subscribe_spots([pair, inv_pair])
    return 0.0
```

## 6. `timeout_checker.py`

```python
async def timeout_checker_loop(redis_svc, broadcast):
    while True:
        try:
            all_accs = await redis_svc.get_all_account_pairs()
            for broker, acc in all_accs:
                stuck = await redis_svc.get_stuck_pending(broker, acc, max_age_seconds=30)
                for request_id, age_ms in stuck:
                    await handle_timeout(broker, acc, request_id, redis_svc, broadcast)
        except Exception:
            log.exception("timeout_checker error")
        await asyncio.sleep(60)

async def handle_timeout(broker, account_id, request_id, redis_svc, broadcast):
    # Find order by request_id (lookup in created cmds — can store hash request_to_order)
    order_id = await redis_svc.find_order_by_request_id(request_id)
    if not order_id:
        return
    order = await redis_svc.get_order(order_id)
    if order["status"] in ("open", "closed", "cancelled", "timeout"):
        await redis_svc.remove_pending(broker, account_id, request_id)
        return
    
    await redis_svc.update_order(order_id, {"status": "timeout"})
    await redis_svc.remove_pending(broker, account_id, request_id)
    await broadcast.send("positions", {"type": "order_timeout", "order_id": order_id})
    log.warning("order timeout: order=%s broker=%s acc=%s", order_id, broker, account_id)
```

## 7. `broadcast.py`

```python
class BroadcastManager:
    def __init__(self):
        self._subs: dict[WebSocket, set[str]] = {}
    
    def add(self, ws): self._subs[ws] = set()
    def remove(self, ws): self._subs.pop(ws, None)
    
    def subscribe(self, ws, channels):
        self._subs[ws].update(channels)
    
    def unsubscribe(self, ws, channels):
        self._subs[ws].difference_update(channels)
    
    async def send(self, channel: str, payload: dict):
        msg = json.dumps({"channel": channel, "data": payload})
        dead = []
        for ws, channels in self._subs.items():
            if channel in channels or any(channel.startswith(c.split(":")[0]) for c in channels):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self.remove(ws)
```

## 8. `market_data.py`

Wrapper Twisted-asyncio bridge cho 1 cTrader market-data connection. (Chi tiết implementation copy từ `03-server-brokers.md` của docs v1, vì pattern bridge này vẫn cần ở 1 chỗ duy nhất này.) Public API:

```python
class MarketDataService:
    async def start(self): ...
    async def stop(self): ...
    
    async def get_trendbars(self, symbol, timeframe, count=200) -> list[Candle]: ...
    async def subscribe_spots(self, symbols: list[str]): ...
    async def subscribe_live_trendbar(self, symbol, timeframe): ...
    async def get_symbol_info(self, symbol) -> SymbolInfo: ...
    async def sync_symbols(self):
        """
        1. Fetch all symbols from cTrader.
        2. Filter by whitelist (only keep symbols in symbol_mapping_ftmo_exness.json).
        3. HSET symbol_config:{sym} for each.
        4. SADD symbols:active.
        """
        ...
```

## 9. Order ID generation

```python
def generate_order_id() -> str:
    """Format: ord_<8-char-base32>. Sortable by time roughly."""
    return f"ord_{secrets.token_urlsafe(6).replace('_', '').replace('-', '')[:8]}"
```
