# 04 — Exness Trading Client

## 1. Tổng quan

Exness trading client là **process Python độc lập** chạy trên Windows VPS (vì MT5 lib chỉ chạy Windows). Trách nhiệm tương tự FTMO client nhưng dùng MT5 Python lib.

## 2. Khác biệt so với FTMO client

| Aspect | FTMO client | Exness client |
| --- | --- | --- |
| Broker API | cTrader Open API (ProtoBuf) | MT5 Python lib (sync function calls) |
| OS | Bất kỳ (Linux/Windows) | **Windows only** |
| Async model | Twisted reactor | asyncio + executor thread (MT5 lib blocking) |
| Auth | OAuth access_token | MT5 login + password (terminal-side) |
| SL/TP | Hỗ trợ | **KHÔNG dùng** (per business rule R3 — secondary không có SL/TP) |
| Hedging mode | (không liên quan) | MT5 account **PHẢI** ở hedging mode (Exness mặc định) |

## 3. Tại sao asyncio + executor (không Twisted thuần như FTMO)?

MT5 lib là **synchronous Python function calls** (`mt5.order_send(...)` trả thẳng). Không có event-driven framework như cTrader. Để giữ Redis loop responsive khi MT5 call block 100ms+, dùng asyncio + `loop.run_in_executor(...)`:

```python
result = await loop.run_in_executor(executor, mt5.order_send, request_dict)
```

Asyncio ở đây là pattern đơn giản, không phức tạp như bridge Twisted-asyncio ở server v1.

## 4. Cấu trúc thư mục

```
apps/client-exness/
├── client/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── mt5_adapter.py             ← MT5 connect + place/close + normalize_volume
│   ├── command_dispatcher.py      ← XREADGROUP loop
│   ├── response_publisher.py      ← XADD resp/event
│   ├── heartbeat.py               ← async task 10s
│   ├── account_sync.py            ← async task 30s
│   └── types.py
├── pyproject.toml
└── .env.example
```

## 5. Entry point

```python
async def main():
    config = Config.from_env()
    redis_client = await aioredis.from_url(config.REDIS_URL, decode_responses=True)
    
    adapter = MT5Adapter(
        login=config.MT5_LOGIN,
        password=config.MT5_PASSWORD,
        server=config.MT5_SERVER,
        terminal_path=config.MT5_TERMINAL_PATH,
    )
    await adapter.connect()
    
    publisher = ResponsePublisher(redis_client, config.ACCOUNT_ID)
    dispatcher = CommandDispatcher(redis_client, adapter, publisher, config.ACCOUNT_ID)
    
    await asyncio.gather(
        dispatcher.run_loop(),
        Heartbeat(redis_client, config.ACCOUNT_ID).run_loop(),
        AccountSync(redis_client, adapter, config.ACCOUNT_ID).run_loop(),
        position_monitor_loop(redis_client, adapter, publisher, config.ACCOUNT_ID),
    )

if __name__ == "__main__":
    asyncio.run(main())
```

## 6. Config (`.env`)

```
ACCOUNT_ID=exness_acc_001
REDIS_URL=redis://server-host:6379/0
MT5_LOGIN=12345678
MT5_PASSWORD=...
MT5_SERVER=Exness-MT5Real            # VD: Exness-MT5Trial cho demo
MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
LOG_LEVEL=INFO
```

> **Mỗi máy 1 MT5 terminal đã login + 1 client process** với `ACCOUNT_ID` riêng.

## 7. `MT5Adapter`

### 7.1 Connect

```python
class MT5Adapter:
    async def connect(self):
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, mt5.initialize, self.terminal_path)
        if not ok:
            raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
        ok = await loop.run_in_executor(None, mt5.login, self.login, self.password, self.server)
        if not ok:
            raise RuntimeError(f"mt5.login failed: {mt5.last_error()}")
        log.info("MT5 connected: login=%s server=%s", self.login, self.server)
```

### 7.2 Place market order (NO SL/TP per R3)

```python
async def place_market_order(self, symbol: str, side: str, volume_lots: float) -> OrderResult:
    loop = asyncio.get_running_loop()
    
    # Get current tick for fill price
    tick = await loop.run_in_executor(None, mt5.symbol_info_tick, symbol)
    if tick is None:
        return OrderResult(success=False, error_msg=f"no tick for {symbol}")
    
    price = tick.ask if side == "buy" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    
    # Normalize volume
    volume_lots = self.normalize_volume(symbol, volume_lots)
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume_lots,
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": 0,
        "comment": "hedge",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,   # IOC, KHÔNG dùng FOK (retcode 10030)
    }
    
    result = await loop.run_in_executor(None, mt5.order_send, request)
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return OrderResult(
            success=False,
            error_code=result.retcode,
            error_msg=result.comment,
        )
    
    return OrderResult(
        success=True,
        broker_order_id=str(result.order),     # ticket
        fill_price=result.price,
        commission=getattr(result, "commission", 0.0) or 0.0,
    )
```

### 7.3 Normalize volume

```python
def normalize_volume(self, symbol: str, volume_lots: float) -> float:
    """Clamp + round theo volume_min, volume_max, volume_step."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return volume_lots
    
    v = max(info.volume_min, min(info.volume_max, volume_lots))
    step = info.volume_step
    return round(v / step) * step
```

### 7.4 Close position

```python
async def close_position(self, broker_order_id: str) -> OrderResult:
    loop = asyncio.get_running_loop()
    ticket = int(broker_order_id)
    
    positions = await loop.run_in_executor(None, mt5.positions_get, ticket=ticket)
    if not positions:
        return OrderResult(success=False, error_msg=f"position {ticket} not found")
    
    pos = positions[0]
    tick = await loop.run_in_executor(None, mt5.symbol_info_tick, pos.symbol)
    
    # Reverse the side to close
    close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    close_price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": close_type,
        "position": ticket,
        "price": close_price,
        "deviation": 20,
        "magic": 0,
        "comment": "hedge_close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    result = await loop.run_in_executor(None, mt5.order_send, request)
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return OrderResult(success=False, error_code=result.retcode, error_msg=result.comment)
    
    return OrderResult(
        success=True,
        broker_order_id=str(result.order),
        fill_price=result.price,
        commission=getattr(result, "commission", 0.0) or 0.0,
    )
```

### 7.5 Get account info

```python
async def get_account_info(self) -> dict:
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, mt5.account_info)
    return {
        "balance": info.balance,
        "equity": info.equity,
        "margin": info.margin,
        "free_margin": info.margin_free,
        "currency": info.currency,
    }
```

## 8. Position monitor (unsolicited close detection)

MT5 lib không có push event khi position bị đóng ngoài chỉ định (vd margin call, manual close trên MT5 UI, stop-out). Phải poll.

```python
async def position_monitor_loop(redis_client, adapter, publisher, account_id):
    """Poll MT5 positions every 2s, detect closures, publish events."""
    last_known: set[str] = set()
    
    while True:
        try:
            loop = asyncio.get_running_loop()
            positions = await loop.run_in_executor(None, mt5.positions_get)
            current = {str(p.ticket) for p in (positions or [])}
            
            closed = last_known - current
            for ticket in closed:
                # This position was closed externally (margin call, manual MT5 UI, etc.)
                publisher.publish_event("position_closed_external", {
                    "broker_order_id": ticket,
                    "detected_at": int(time.time() * 1000),
                })
                log.warning("position closed externally: ticket=%s", ticket)
            
            last_known = current
        except Exception:
            log.exception("position monitor error")
        
        await asyncio.sleep(2)
```

→ Server `response_handler` xử lý `event_stream:exness:{account_id}` event này → cascade close primary FTMO.

> **Đây là điểm BỔ SUNG so với v1**. V1 không có flow này, leg primary có thể bị mồ côi nếu MT5 position bị đóng ngoài.

## 9. `CommandDispatcher` (asyncio version)

```python
class CommandDispatcher:
    def __init__(self, redis_client, adapter, publisher, account_id):
        self.redis = redis_client
        self.adapter = adapter
        self.publisher = publisher
        self.account_id = account_id
        self.stream_key = f"cmd_stream:exness:{account_id}"
        self.consumer_group = f"exness-{account_id}"
        self.consumer_name = "client"
    
    async def run_loop(self):
        while True:
            try:
                entries = await self.redis.xreadgroup(
                    self.consumer_group, self.consumer_name,
                    {self.stream_key: ">"}, count=1, block=1000,
                )
                if entries:
                    for stream, msgs in entries:
                        for msg_id, fields in msgs:
                            await self._dispatch(msg_id, fields)
            except Exception:
                log.exception("dispatcher error")
                await asyncio.sleep(0.5)
    
    async def _dispatch(self, msg_id, fields):
        action = fields["action"]
        try:
            if action == "open":
                result = await self.adapter.place_market_order(
                    symbol=fields["symbol"],
                    side=fields["side"],
                    volume_lots=float(fields["volume_lots"]),
                )
            elif action == "close":
                result = await self.adapter.close_position(fields["broker_order_id"])
            else:
                log.warning("unknown action: %s", action)
                return
            
            await self.publisher.publish_response(
                fields["request_id"], fields["order_id"], action, result,
            )
        except Exception as e:
            log.exception("dispatch error")
            await self.publisher.publish_error(
                fields["request_id"], fields["order_id"], action, str(e),
            )
        finally:
            await self.redis.xack(self.stream_key, self.consumer_group, msg_id)
```

## 10. Heartbeat + AccountSync

Tương tự FTMO client, format key: `client:exness:{account_id}` TTL 30s + `account:exness:{account_id}` HSET.

## 11. Windows service setup

```bat
:: install_service.bat
nssm install exness-client-001 "C:\Python311\python.exe" -m client.main
nssm set exness-client-001 AppDirectory C:\path\to\client-exness
nssm set exness-client-001 AppStdout C:\path\to\logs\exness-001.log
nssm start exness-client-001
```

> **MT5 terminal phải đang chạy + đã login** trước khi service start. Recommend: bật MT5 auto-login + auto-start với Windows.

## 12. Lessons learned từ v1

- ❌ MT5 `ORDER_FILLING_FOK` → retcode 10030 trên Exness. **V2: dùng IOC.**
- ❌ MT5 lib không lock → race condition crash multi-thread. **V2: chỉ executor thread, no concurrent access.**
- ❌ Không có position monitor → MT5 close ngoài → primary mồ côi. **V2: position_monitor_loop poll 2s.**
- ❌ Symbol có suffix khác (`EURUSDm` vs `EURUSD`) → confused. **V2: client trust `symbol` field từ command (server đã map qua whitelist).**

## 13. Test strategy

- **Unit test**: mock MT5 lib, test normalize_volume, mock order_send retcode logic.
- **Integration test**: MT5 demo account, place + close + monitor.
- **Smoke test**: server push command qua redis-cli → verify resp_stream + position appears in MT5 terminal.
