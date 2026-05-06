# 03 — FTMO Trading Client

## 1. Tổng quan

FTMO trading client là **process Python độc lập** chạy trên 1 máy riêng. Trách nhiệm:
- Kết nối cTrader Open API tới 1 FTMO account.
- Subscribe Redis Stream `cmd_stream:ftmo:{account_id}` để nhận command từ server.
- Thực thi command (open / close / modify SL-TP) qua cTrader.
- Đẩy response về `resp_stream:ftmo:{account_id}`.
- Đẩy event không-yêu-cầu (vd position closed do SL hit) về `event_stream:ftmo:{account_id}`.
- Heartbeat → `client:ftmo:{account_id}` TTL 30s.
- Account sync → HSET `account:ftmo:{account_id}`.

**KHÔNG** lo về:
- Cascade close (server làm).
- Volume conversion (server tính, client chỉ nhận `volume_lots` cuối cùng).
- Symbol whitelist (server đã filter trước khi gửi command).
- P&L USD (server tính từ tick).

## 2. Tại sao Twisted thuần (không asyncio)?

cTrader Open API chỉ có lib Twisted. Trong v1, server dùng asyncio nên phải bridge → phức tạp + bug.

V2: client là process riêng, **không cần** asyncio. Chỉ cần:
- Twisted reactor cho cTrader.
- Redis blocking client (`redis-py` không async) gọi `XREADGROUP` blocking 1s timeout trong 1 callable Twisted.

→ Code thẳng, không bridge, dễ maintain.

## 3. Cấu trúc thư mục

```
apps/client-ftmo/
├── client/
│   ├── __init__.py
│   ├── main.py                    ← entry point
│   ├── config.py                  ← env loader
│   ├── ctrader_adapter.py         ← cTrader connection + place/close/modify
│   ├── command_dispatcher.py      ← XREADGROUP loop, dispatch command
│   ├── response_publisher.py      ← XADD resp/event streams
│   ├── heartbeat.py               ← reactor.callLater 10s heartbeat
│   ├── account_sync.py            ← reactor.callLater 30s account info → Redis
│   └── types.py                   ← OrderResult, Command, Response dataclasses
├── pyproject.toml
└── .env.example
```

## 4. Entry point (`main.py`)

```python
def main():
    config = Config.from_env()
    redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    
    adapter = CTraderAdapter(
        client_id=config.CTRADER_CLIENT_ID,
        client_secret=config.CTRADER_CLIENT_SECRET,
        access_token=config.CTRADER_ACCESS_TOKEN,
        host=config.CTRADER_HOST,
        port=config.CTRADER_PORT,
    )
    
    publisher = ResponsePublisher(redis_client, account_id=config.ACCOUNT_ID)
    dispatcher = CommandDispatcher(redis_client, adapter, publisher, config.ACCOUNT_ID)
    heartbeat = Heartbeat(redis_client, config.ACCOUNT_ID, interval=10)
    account_sync = AccountSync(redis_client, adapter, config.ACCOUNT_ID, interval=30)
    
    # Connect cTrader (async via Twisted)
    adapter.connect_and_auth()  # uses reactor.callLater internally
    
    # Schedule loops on reactor
    reactor.callLater(0, dispatcher.run_loop)
    reactor.callLater(0, heartbeat.run_loop)
    reactor.callLater(0, account_sync.run_loop)
    
    reactor.run()  # block forever
```

## 5. Config (`.env`)

```
ACCOUNT_ID=ftmo_acc_001          # user-defined ID, KHÔNG phải ctidTraderAccountId
REDIS_URL=redis://server-host:6379/0
CTRADER_CLIENT_ID=...
CTRADER_CLIENT_SECRET=...
CTRADER_ACCESS_TOKEN=...         # OAuth-granted token cho FTMO live account này
CTRADER_HOST=live.ctraderapi.com  # live cho trading account
CTRADER_PORT=5035
LOG_LEVEL=INFO
```

> **OAuth token cho FTMO trading account**: client phải có `access_token` được pre-granted (qua OAuth flow ngoài hệ thống — user click consent ở browser, lấy token, paste vào `.env`).
> Server **KHÔNG** dùng token này — token này chỉ ở client process.

## 6. `CTraderAdapter`

### 6.1 Connect + auth

```python
class CTraderAdapter:
    def __init__(self, client_id, client_secret, access_token, host, port):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.client = Client(host, port, TcpProtocol)
        
        # Pending requests: clientMsgId → callback
        self._pending: dict[str, Callable] = {}
        
        # Lookups built after auth
        self._symbol_id_by_name: dict[str, int] = {}
        self._ctid_trader_account_id: int | None = None
    
    def connect_and_auth(self):
        d = self.client.startService()
        d.addCallback(lambda _: self._app_auth())
        d.addCallback(lambda _: self._get_account_list())
        d.addCallback(lambda _: self._account_auth())
        d.addCallback(lambda _: self._load_symbol_map())
        d.addErrback(self._on_connect_error)
```

### 6.2 Place order

```python
def place_market_order(self, symbol: str, side: str, volume_lots: float, sl: float|None, tp: float|None) -> Deferred:
    """Returns Deferred that fires with OrderResult."""
    symbol_id = self._symbol_id_by_name[symbol]
    contract_size = self._symbol_info[symbol].contract_size
    volume_int = int(round(volume_lots * contract_size * 100))  # cTrader cents
    
    client_msg_id = uuid4().hex
    req = ProtoOANewOrderReq(
        ctidTraderAccountId=self._ctid_trader_account_id,
        symbolId=symbol_id,
        orderType=ProtoOAOrderType.MARKET,
        tradeSide=ProtoOATradeSide.BUY if side == "buy" else ProtoOATradeSide.SELL,
        volume=volume_int,
        stopLoss=sl if sl else None,
        takeProfit=tp if tp else None,
        clientMsgId=client_msg_id,
    )
    
    d = Deferred()
    self._pending[client_msg_id] = d.callback
    self.client.send(req)
    return d  # fires when ProtoOAExecutionEvent received
```

### 6.3 Close order

```python
def close_position(self, broker_order_id: str) -> Deferred:
    """broker_order_id is positionId from cTrader."""
    req = ProtoOAClosePositionReq(
        ctidTraderAccountId=self._ctid_trader_account_id,
        positionId=int(broker_order_id),
        volume=...,  # full close
        clientMsgId=uuid4().hex,
    )
    ...
```

### 6.4 Modify SL/TP

```python
def modify_sl_tp(self, broker_order_id, sl_price, tp_price) -> Deferred: ...
```

### 6.5 Callbacks (cTrader → adapter)

```python
def on_message_received(self, client, message):
    if message.payloadType == ProtoOAExecutionEvent:
        execution_event = ProtoOAExecutionEvent()
        execution_event.ParseFromString(message.payload)
        client_msg_id = message.clientMsgId
        
        if client_msg_id and client_msg_id in self._pending:
            callback = self._pending.pop(client_msg_id)
            callback(self._build_order_result(execution_event))
        else:
            # Unsolicited: position closed (SL hit, manual close on cTrader UI, etc.)
            self.on_unsolicited_event(execution_event)
```

### 6.6 Unsolicited events

Khi position bị đóng do SL/TP hit hoặc user đóng tay trên cTrader UI → cTrader gửi `ProtoOAExecutionEvent(POSITION_CLOSED)` không kèm `clientMsgId` của command nào.

Adapter gọi `on_unsolicited_event(event)` → publisher đẩy vào `event_stream:ftmo:{account_id}` để server xử lý cascade close cho secondary.

## 7. `CommandDispatcher`

```python
class CommandDispatcher:
    def __init__(self, redis_client, adapter, publisher, account_id):
        self.redis = redis_client
        self.adapter = adapter
        self.publisher = publisher
        self.account_id = account_id
        self.stream_key = f"cmd_stream:ftmo:{account_id}"
        self.consumer_group = f"ftmo-{account_id}"
        self.consumer_name = "client"
    
    def run_loop(self):
        """Schedules itself via reactor.callLater for non-blocking polling."""
        try:
            entries = self.redis.xreadgroup(
                self.consumer_group, self.consumer_name,
                {self.stream_key: ">"}, count=1, block=1000,  # 1s blocking
            )
            if entries:
                for stream, msgs in entries:
                    for msg_id, fields in msgs:
                        self._dispatch(msg_id, fields)
        except Exception as e:
            log.exception("dispatcher loop error: %s", e)
        
        # Re-schedule
        reactor.callLater(0, self.run_loop)
    
    def _dispatch(self, msg_id, fields):
        action = fields["action"]
        request_id = fields["request_id"]
        order_id = fields["order_id"]
        
        if action == "open":
            d = self.adapter.place_market_order(
                symbol=fields["symbol"],
                side=fields["side"],
                volume_lots=float(fields["volume_lots"]),
                sl=float(fields.get("sl") or 0) or None,
                tp=float(fields.get("tp") or 0) or None,
            )
            d.addCallback(lambda result: self.publisher.publish_response(
                request_id, order_id, "open", result,
            ))
            d.addErrback(lambda err: self.publisher.publish_error(
                request_id, order_id, "open", str(err),
            ))
        elif action == "close":
            d = self.adapter.close_position(broker_order_id=fields["broker_order_id"])
            d.addCallback(lambda result: self.publisher.publish_response(
                request_id, order_id, "close", result,
            ))
        elif action == "modify_sl_tp":
            ...
        else:
            log.warning("unknown action: %s", action)
        
        # Always XACK so message không re-delivered
        self.redis.xack(self.stream_key, self.consumer_group, msg_id)
```

> **Idempotency**: server gắn `request_id` (uuid) khi push. Client luôn XACK sau dispatch (kể cả error). Nếu network die giữa chừng → server timeout sau 30s, treat as failed, KHÔNG retry tự động (tránh duplicate orders).

## 8. `ResponsePublisher`

```python
class ResponsePublisher:
    def publish_response(self, request_id, order_id, action, result: OrderResult):
        self.redis.xadd(f"resp_stream:ftmo:{self.account_id}", {
            "request_id": request_id,
            "order_id": order_id,
            "action": action,
            "status": "filled" if result.success else "error",
            "broker_order_id": result.broker_order_id or "",
            "fill_price": str(result.fill_price or 0),
            "fill_time": str(int(time.time() * 1000)),
            "error_code": str(result.error_code or 0),
            "error_msg": result.error_msg or "",
            "commission": str(result.commission or 0),
        })
    
    def publish_event(self, event_type: str, data: dict):
        """Unsolicited events (position closed by SL/TP/manual)."""
        self.redis.xadd(f"event_stream:ftmo:{self.account_id}", {
            "event_type": event_type,
            **data,
        })
```

## 9. Heartbeat

```python
class Heartbeat:
    def run_loop(self):
        try:
            self.redis.hset(f"client:ftmo:{self.account_id}", mapping={
                "status": "online",
                "last_seen": int(time.time() * 1000),
                "version": "v2.0.0",
            })
            self.redis.expire(f"client:ftmo:{self.account_id}", 30)
        except Exception:
            log.exception("heartbeat error")
        
        reactor.callLater(self.interval, self.run_loop)
```

## 10. AccountSync

```python
class AccountSync:
    def run_loop(self):
        d = self.adapter.get_account_info()
        d.addCallback(self._save)
        d.addErrback(lambda err: log.warning("account sync error: %s", err))
        reactor.callLater(self.interval, self.run_loop)
    
    def _save(self, info):
        self.redis.hset(f"account:ftmo:{self.account_id}", mapping={
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "free_margin": info.free_margin,
            "currency": info.currency,
            "updated_at": int(time.time() * 1000),
        })
```

## 11. Run as Windows service

```bat
:: install_service.bat
nssm install ftmo-client-001 python.exe -m client.main
nssm set ftmo-client-001 AppDirectory C:\path\to\client-ftmo
nssm set ftmo-client-001 AppStdout C:\path\to\logs\ftmo-001.log
nssm set ftmo-client-001 AppStderr C:\path\to\logs\ftmo-001.err.log
nssm start ftmo-client-001
```

> Mỗi máy chạy đúng **1 instance** với 1 `ACCOUNT_ID` riêng.

## 12. Lessons learned từ v1 — không lặp lại

- ❌ Bridge Twisted ↔ asyncio trong cùng process. **V2: client process Twisted thuần, không asyncio.**
- ❌ `_pending: dict` không cleanup → memory leak. **V2: timeout cleanup mỗi 60s, drop pending > 5 phút.**
- ❌ Lưu `access_token` trong env không có flow refresh. **V2: chấp nhận expire, log warning, user re-grant.**
- ❌ Không log clientMsgId → khó debug. **V2: mọi request log INFO với client_msg_id.**

## 13. Test strategy

- **Unit test**: mock cTrader Client, test command dispatcher với fake stream entries.
- **Integration test**: connect cTrader demo account, place + close 1 order, verify resp_stream content.
- **Smoke test**: start client, server push fake command bằng `redis-cli XADD cmd_stream:ftmo:test_acc * action open ...` → verify execution.
