# 02 — Server Overview

## 1. Cấu trúc thư mục

```
apps/server/
├── app/
│   ├── main.py                    ← FastAPI + lifespan + WS endpoint + static
│   ├── config.py                  ← pydantic-settings (.env loader)
│   ├── auth.py                    ← JWT + bcrypt + ws-auth + rest-auth
│   ├── dependencies.py            ← singletons (redis_service, market_data_service)
│   ├── redis_client.py            ← aioredis ConnectionPool
│   ├── api/
│   │   ├── auth.py                ← POST /auth/login
│   │   ├── auth_ctrader.py        ← OAuth flow cho market-data account
│   │   ├── symbols.py             ← GET /symbols, /symbols/{}/tick, /symbols/{}/calculate-volume
│   │   ├── orders.py              ← POST /orders/hedge, DELETE, PATCH /sl-tp
│   │   ├── positions.py           ← GET /positions
│   │   ├── accounts.py            ← GET /accounts, POST/DELETE /accounts (manage pairs)
│   │   ├── pairs.py               ← GET/POST/DELETE /pairs
│   │   ├── settings.py            ← GET/PATCH /settings
│   │   └── charts.py              ← GET /charts/{sym}/ohlc
│   ├── services/
│   │   ├── redis_service.py       ← Redis access layer (CRUD, streams, etc.)
│   │   ├── order_service.py       ← Validation, volume calc, routing, cascade
│   │   ├── response_handler.py    ← Process resp/event streams from clients
│   │   ├── position_tracker.py    ← P&L USD loop (1s)
│   │   ├── timeout_checker.py     ← Pending order timeout (60s)
│   │   ├── broadcast.py           ← WebSocket manager
│   │   ├── symbol_whitelist.py    ← Load symbol_mapping_ftmo_exness.json + filter
│   │   └── market_data.py         ← cTrader market-data wrapper (Twisted bridge)
│   └── schemas/                   ← Pydantic DTOs
├── pyproject.toml
└── .env.example
```

## 2. `main.py` — entry point

### 2.1 Lifespan

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    await redis_client.ping()
    await redis_service.setup_consumer_groups()  
    # consumer groups cho mọi account trong settings
    
    # Load symbol whitelist
    symbol_whitelist.load_from_file("docs/symbol_mapping_ftmo_exness.json")
    
    # Start market-data cTrader connection (1 instance)
    market_data_service = MarketDataService(redis_service, broadcast_manager)
    set_market_data_service(market_data_service)
    await market_data_service.start()  # spawns Twisted reactor
    
    # Background tasks
    bg_tasks = [
        asyncio.create_task(response_reader_loop(...)),       # đọc tất cả resp_stream:*
        asyncio.create_task(position_tracker_loop(...)),
        asyncio.create_task(timeout_checker_loop(...)),
    ]
    yield
    # SHUTDOWN
    for t in bg_tasks: t.cancel()
    await market_data_service.stop()
    await redis_client.aclose()
```

### 2.2 Routers

```python
app.include_router(auth.router)            # /auth/login
app.include_router(auth_ctrader.router)    # /auth/ctrader/* (market-data OAuth only)
app.include_router(symbols.router)         # /symbols
app.include_router(orders.router)          # /orders
app.include_router(positions.router)       # /positions
app.include_router(accounts.router)        # /accounts (manage FTMO/Exness accounts)
app.include_router(pairs.router)           # /pairs (manage pair definitions)
app.include_router(settings.router)        # /settings
app.include_router(charts.router)          # /charts
```

### 2.3 WebSocket `/ws`

```
GET /ws?token=<JWT>
```

Auth bằng `get_current_user_ws(token)`.

Channels:
- `positions` — P&L update + lifecycle
- `ticks:{symbol}`
- `candles:{symbol}:{tf}`
- `agents` — heartbeat status của tất cả clients

## 3. `config.py` — settings

```python
class Settings(BaseSettings):
    # App
    LOG_LEVEL: str = "INFO"
    
    # Auth
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD_HASH: str          # bcrypt hash
    JWT_SECRET: str
    JWT_EXPIRE_MINUTES: int = 1440    # 24h
    
    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # Market-data cTrader
    CTRADER_CLIENT_ID: str
    CTRADER_CLIENT_SECRET: str
    CTRADER_REDIRECT_URI: str
    CTRADER_HOST: str = "demo.ctraderapi.com"  # market-data dùng demo
    CTRADER_PORT: int = 5035
    
    # Symbol whitelist
    SYMBOL_MAP_PATH: str = "docs/symbol_mapping_ftmo_exness.json"
    
    # Tracker
    POSITION_TRACKER_INTERVAL: float = 1.0
    TIMEOUT_CHECKER_INTERVAL: float = 60.0
    PRIMARY_FILL_TIMEOUT: int = 30   # giây — primary chưa fill → timeout
    
    class Config:
        env_file = ".env"
```

## 4. `auth.py`

- `verify_password(plain, hashed)` — bcrypt
- `create_access_token(payload, expires_minutes)` — JWT
- `decode_access_token(token)` — JWT verify, raise HTTPException(401) on fail
- `get_current_user_rest(authorization: str = Header)` — REST dependency
- `get_current_user_ws(token: str = Query)` — WS dependency

## 5. `dependencies.py` — singletons

```python
_redis_service: RedisService | None
_market_data_service: MarketDataService | None
_symbol_whitelist: SymbolWhitelist | None

def get_redis_service() -> RedisService
def set_market_data_service(svc): ...
def get_market_data_service() -> MarketDataService
def get_symbol_whitelist() -> SymbolWhitelist
```

## 6. `redis_client.py` — async pool

```python
pool = aioredis.ConnectionPool.from_url(
    settings.REDIS_URL, decode_responses=True, max_connections=50,
)
redis_client = aioredis.Redis(connection_pool=pool)
```

## 7. Market-data cTrader connection

Đây là điểm duy nhất trong server cần Twisted-asyncio bridge.

### 7.1 Trách nhiệm
- Connect 1 cTrader connection (account demo) lúc startup.
- Auth: app_auth + account_auth với access_token của account demo.
- Sync symbols list → filter qua whitelist → HSET `symbol_config:{sym}` + SADD `symbols:active`.
- Subscribe spot ticks cho symbol active → SETEX `tick:{sym}` TTL 5s + WS broadcast.
- Subscribe live trendbar cho timeframe đang xem → WS broadcast.
- Get historical OHLC khi REST `/charts/{sym}/ohlc` được gọi.
- **KHÔNG** đặt order qua connection này.

### 7.2 OAuth flow (chỉ 1 lần lúc setup)

```
GET /auth/ctrader              ← redirect cTrader consent
GET /auth/ctrader/callback     ← exchange code, save Redis hash ctrader:market_data_creds
GET /auth/ctrader/status       ← { has_credentials, expires_at, expires_in_seconds }
```

Token expire ~30 ngày (live) / 30 phút (demo). Nếu expire → user click `/auth/ctrader` re-grant.

> **Đơn giản hóa v2**: KHÔNG có refresh token loop. CEO yêu cầu rõ "đơn giản, scale <10 user". Khi token expire → market data tạm dừng → CEO re-grant manually. Server log warning + alert qua agents WS channel.

### 7.3 Implementation

`app/services/market_data.py`:
```python
class MarketDataService:
    def __init__(self, redis_service, broadcast_manager): ...
    
    async def start(self): 
        # Spawn Twisted thread, app_auth, account_auth, load_symbol_map
        ...
    
    async def stop(self): ...
    
    async def get_trendbars(self, symbol, timeframe, count) -> list[Candle]: ...
    
    async def subscribe_spots(self, symbols: list[str]): ...
    
    async def get_symbol_info(self, symbol) -> SymbolInfo: ...
    
    # Background: tick_feed_loop reads from cTrader callbacks → SETEX tick:{sym} + broadcast
```

## 8. Logging

- `logging.basicConfig` set ở `main.py` startup.
- Format: `%(asctime)s %(name)-25s %(levelname)s %(message)s`.
- Level từ `LOG_LEVEL` env var.
- Mask sensitive: `access_token`, `password_hash`, `JWT_SECRET` không bao giờ log full.

## 9. Error handling

- Global FastAPI exception handlers cho `HTTPException`, `ValidationError`, `Exception`.
- Format error: `{ "detail": "human readable message" }`.
- Validation fail (user error) → log INFO/WARN.
- Adapter exception → log ERROR + stack trace.
