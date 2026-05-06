# 11 — Deployment

## 1. Topology

Tối thiểu cho 1 pair (1 FTMO + 1 Exness) đã cần **3 máy**:

| Máy | Vai trò | OS yêu cầu | Spec đề xuất |
| --- | --- | --- | --- |
| **#1 Server VPS** | FastAPI + Redis + Frontend static + market-data cTrader connection | Linux (Ubuntu 22.04) hoặc Windows | 2 vCPU, 2GB RAM, 20GB SSD |
| **#2 FTMO client máy** | 1 process FTMO trading client | Linux hoặc Windows VPS | 1 vCPU, 1GB RAM |
| **#3 Exness client máy** | 1 process Exness trading client + MT5 terminal | **Windows only** (MT5 lib) | 2 vCPU, 2GB RAM (MT5 ăn RAM) |

Mỗi pair thêm = thêm 2 máy (1 FTMO + 1 Exness).

> **Tại sao tách?** CEO yêu cầu rõ Q5: "mỗi ftmo client phải chạy trên 1 máy khác nhau". Lý do: cTrader limit ngầm số session per IP, isolation khi 1 client crash, dễ debug.

## 2. Server VPS setup

### 2.1 OS prep (Ubuntu 22.04)

```bash
# Update
sudo apt update && sudo apt upgrade -y

# Python 3.11
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt install -y python3.11 python3.11-venv python3.11-dev

# Redis 7
sudo add-apt-repository ppa:redislabs/redis -y
sudo apt install -y redis-server
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Verify
redis-cli ping  # → PONG
```

### 2.2 Server deploy

```bash
# Clone repo
git clone https://github.com/matran19900/ftmo_exness_hedge_v3.git
cd ftmo_exness_hedge_v3/apps/server

# Virtual env
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# .env
cp .env.example .env
nano .env  # fill values
```

`.env` server:
```
LOG_LEVEL=INFO

ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=$2b$12$...           # bcrypt hash
JWT_SECRET=<openssl rand -hex 32>
JWT_EXPIRE_MINUTES=1440

REDIS_URL=redis://localhost:6379/0

CTRADER_CLIENT_ID=<from cTrader OpenAPI portal>
CTRADER_CLIENT_SECRET=<from cTrader OpenAPI portal>
CTRADER_REDIRECT_URI=http://server-ip:8000/auth/ctrader/callback
CTRADER_HOST=demo.ctraderapi.com
CTRADER_PORT=5035

SYMBOL_MAP_PATH=docs/symbol_mapping_ftmo_exness.json

POSITION_TRACKER_INTERVAL=1.0
TIMEOUT_CHECKER_INTERVAL=60.0
PRIMARY_FILL_TIMEOUT=30
```

Generate `ADMIN_PASSWORD_HASH`:
```bash
python -c "import bcrypt; print(bcrypt.hashpw(b'mypassword', bcrypt.gensalt()).decode())"
```

### 2.3 Frontend build & deploy

```bash
cd ../web
npm install
npm run build
# Output: dist/
```

Server `main.py` mount static:
```python
app.mount("/", StaticFiles(directory="../web/dist", html=True), name="static")
```

### 2.4 Run server

Dev:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Production (systemd unit):
```ini
# /etc/systemd/system/ftmo-server.service
[Unit]
Description=FTMO Hedge Server
After=network.target redis-server.service
Requires=redis-server.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ftmo_exness_hedge_v3/apps/server
Environment="PATH=/home/ubuntu/ftmo_exness_hedge_v3/apps/server/.venv/bin"
ExecStart=/home/ubuntu/ftmo_exness_hedge_v3/apps/server/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable ftmo-server
sudo systemctl start ftmo-server
sudo journalctl -u ftmo-server -f
```

### 2.5 Network access (đơn giản hóa <10 user)

3 lựa chọn (CEO tự chọn, KHÔNG có HTTPS/nginx ở phase 1):

1. **Tailscale**: cài Tailscale trên server + máy CEO → access `http://<tailnet-ip>:8000`. **Đơn giản nhất, đủ secure.**
2. **Cloudflare Tunnel**: free, giấu IP server. `cloudflared tunnel ...`.
3. **Public HTTP** (RỦI RO): expose port 8000, login JWT. KHÔNG khuyến khích vì không có HTTPS → password lộ.

Khuyến nghị: **Tailscale**.

### 2.6 cTrader OAuth setup (1 lần)

1. Đăng ký app ở https://openapi.ctrader.com/
2. Tạo demo account (cho market-data)
3. Sau khi server start, mở browser:
   ```
   http://server-ip:8000/auth/ctrader
   ```
4. Login cTrader demo → consent → redirect callback → server lưu credentials Redis.
5. Verify:
   ```bash
   curl http://server-ip:8000/auth/ctrader/status
   ```

> Token expire ~30 ngày demo. Khi expire → server logs warning + AccountStatus bar bật cảnh báo → CEO truy cập lại `/auth/ctrader`.

## 3. FTMO client máy setup

### 3.1 OS prep (Linux hoặc Windows)

Linux:
```bash
sudo apt install python3.11 python3.11-venv git
```

Windows:
- Cài Python 3.11 từ python.org (check "Add to PATH")
- Cài Git for Windows

### 3.2 Deploy

```bash
git clone https://github.com/matran19900/ftmo_exness_hedge_v3.git
cd ftmo_exness_hedge_v3/apps/client-ftmo

python -m venv .venv
.venv\Scripts\activate     # Windows
# source .venv/bin/activate # Linux
pip install -e .

cp .env.example .env
notepad .env               # Windows
# nano .env                # Linux
```

`.env` FTMO client:
```
ACCOUNT_ID=ftmo_acc_001                     # PHẢI khớp với account_id đã add ở server settings
REDIS_URL=redis://<tailnet-server-ip>:6379/0

CTRADER_CLIENT_ID=<same as server>
CTRADER_CLIENT_SECRET=<same as server>
CTRADER_ACCESS_TOKEN=<token of THIS FTMO live account>
CTRADER_HOST=live.ctraderapi.com
CTRADER_PORT=5035

LOG_LEVEL=INFO
```

> **Lưu ý quan trọng**: `CTRADER_ACCESS_TOKEN` là access_token của **chính FTMO account live** mà client này sẽ trade. KHÔNG phải token của market-data demo account ở server.

### 3.3 Lấy access token cho FTMO live account

Manual OAuth flow (1 lần per account):

1. Tạo URL:
   ```
   https://openapi.ctrader.com/apps/auth?client_id=<CLIENT_ID>&redirect_uri=<REDIRECT>&scope=trading
   ```
2. Mở browser → login FTMO account → consent.
3. Browser redirect đến `<REDIRECT>?code=XYZ`.
4. Exchange code → token bằng curl:
   ```bash
   curl -X POST https://openapi.ctrader.com/apps/token \
     -d "grant_type=authorization_code" \
     -d "code=XYZ" \
     -d "redirect_uri=<REDIRECT>" \
     -d "client_id=<CLIENT_ID>" \
     -d "client_secret=<CLIENT_SECRET>"
   ```
5. Response chứa `access_token` + `refresh_token` → paste vào `.env`.

> Đơn giản hóa v2: KHÔNG có refresh logic. Token expire (~30 ngày live) → CEO redo manual.

### 3.4 Run

Dev:
```bash
python -m client.main
```

Linux production (systemd):
```ini
# /etc/systemd/system/ftmo-client.service
[Unit]
Description=FTMO Trading Client
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ftmo_exness_hedge_v3/apps/client-ftmo
Environment="PATH=/home/ubuntu/ftmo_exness_hedge_v3/apps/client-ftmo/.venv/bin"
ExecStart=/home/ubuntu/ftmo_exness_hedge_v3/apps/client-ftmo/.venv/bin/python -m client.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Windows production (NSSM):
```bat
:: Cài NSSM: https://nssm.cc/
:: Run as admin
nssm install ftmo-client-001 "C:\Users\Admin\ftmo_exness_hedge_v3\apps\client-ftmo\.venv\Scripts\python.exe" -m client.main
nssm set ftmo-client-001 AppDirectory "C:\Users\Admin\ftmo_exness_hedge_v3\apps\client-ftmo"
nssm set ftmo-client-001 AppStdout "C:\Users\Admin\logs\ftmo-001.log"
nssm set ftmo-client-001 AppStderr "C:\Users\Admin\logs\ftmo-001.err.log"
nssm set ftmo-client-001 AppStdoutCreationDisposition 4
nssm set ftmo-client-001 AppStderrCreationDisposition 4
nssm set ftmo-client-001 AppRotateFiles 1
nssm set ftmo-client-001 AppRotateOnline 0
nssm set ftmo-client-001 AppRotateBytes 10485760
nssm start ftmo-client-001
```

### 3.5 Verify connection

Trên server:
```bash
redis-cli HGETALL client:ftmo:ftmo_acc_001
# → status=online, last_seen=<recent timestamp>
```

Frontend AccountStatus bar → ●xanh.

## 4. Exness client máy setup (Windows only)

### 4.1 OS prep (Windows VPS)

- Windows 10/11 hoặc Windows Server 2019+
- Python 3.11 (64-bit, **PHẢI khớp với MT5 terminal architecture**)
- MT5 terminal từ Exness website
- Cấu hình Windows: tắt sleep, disable Windows updates auto-restart trong giờ trade

### 4.2 MT5 terminal setup

1. Cài MT5 từ Exness portal.
2. Login với `Exness-MT5Real` server + login + password.
3. **Bật auto-login** (Tools → Options → Server → Save account information).
4. **Bật algo trading** (toolbar).
5. Verify: thấy giá ticks trong Market Watch.

### 4.3 Deploy

```bat
git clone https://github.com/matran19900/ftmo_exness_hedge_v3.git
cd ftmo_exness_hedge_v3\apps\client-exness

python -m venv .venv
.venv\Scripts\activate
pip install -e .

copy .env.example .env
notepad .env
```

`.env` Exness client:
```
ACCOUNT_ID=exness_acc_001                   # khớp với server settings
REDIS_URL=redis://<tailnet-server-ip>:6379/0

MT5_LOGIN=12345678
MT5_PASSWORD=...
MT5_SERVER=Exness-MT5Real
MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe

LOG_LEVEL=INFO
```

### 4.4 Run

Dev:
```bat
python -m client.main
```

Production (NSSM):
```bat
nssm install exness-client-001 "C:\path\to\.venv\Scripts\python.exe" -m client.main
nssm set exness-client-001 AppDirectory "C:\path\to\client-exness"
nssm set exness-client-001 AppStdout "C:\logs\exness-001.log"
nssm set exness-client-001 AppStderr "C:\logs\exness-001.err.log"
nssm set exness-client-001 AppRotateFiles 1
nssm set exness-client-001 AppRotateBytes 10485760
nssm start exness-client-001
```

> **Quan trọng**: NSSM service phải chạy trong cùng user session với MT5 terminal đã login. Nếu MT5 chạy trong session khác → MT5 lib không nhìn thấy.
> Khuyến nghị: bật **auto-logon Windows** + để MT5 chạy luôn ở user "Admin".

### 4.5 Verify

Server:
```bash
redis-cli HGETALL client:exness:exness_acc_001
# → status=online
```

## 5. Redis configuration

### 5.1 `redis.conf` adjustments

`/etc/redis/redis.conf`:
```
# Bind to localhost + Tailscale IP
bind 127.0.0.1 100.x.y.z

# Security: require password
requirepass <strong-password>

# Persistence: save snapshot every 60s if 100+ writes
save 60 100

# Append-only log for durability
appendonly yes
appendfsync everysec

# Max memory (set ~70% RAM)
maxmemory 1gb
maxmemory-policy noeviction
```

Restart:
```bash
sudo systemctl restart redis-server
```

Update `REDIS_URL` ở mọi `.env`:
```
REDIS_URL=redis://:<password>@<tailnet-ip>:6379/0
```

### 5.2 Backup script

```bash
#!/bin/bash
# /home/ubuntu/backup-redis.sh
DATE=$(date +%Y%m%d-%H%M)
redis-cli -a <password> BGSAVE
sleep 5
cp /var/lib/redis/dump.rdb /backup/dump-$DATE.rdb
find /backup -name "dump-*.rdb" -mtime +30 -delete
```

Cron daily:
```
0 3 * * * /home/ubuntu/backup-redis.sh
```

## 6. Logging & monitoring

### 6.1 Log locations

| Process | Path |
| --- | --- |
| Server | `journalctl -u ftmo-server` (Linux) hoặc `C:\logs\server.log` (Windows) |
| FTMO client | `C:\logs\ftmo-001.log` |
| Exness client | `C:\logs\exness-001.log` |
| Redis | `/var/log/redis/redis-server.log` |

### 6.2 Health checks (manual)

```bash
# Server
curl http://server:8000/auth/ctrader/status

# Redis
redis-cli ping

# Client heartbeats
redis-cli HGETALL client:ftmo:ftmo_acc_001
redis-cli HGETALL client:exness:exness_acc_001

# Pending commands stuck
redis-cli ZRANGE pending_cmds:ftmo:ftmo_acc_001 0 -1 WITHSCORES
```

> Phase 1 KHÔNG có Prometheus/Grafana. Chỉ logs + manual checks. Đủ cho <10 user.

## 7. Adding a new pair (vận hành)

CEO muốn add account FTMO mới + Exness mới + pair link 2 cái:

1. Mua/setup VPS Linux cho FTMO client mới (vd `vps-ftmo-002`).
2. Mua/setup VPS Windows + MT5 cho Exness client mới (vd `vps-exness-002`).
3. SSH/RDP vào — clone repo, deploy giống section 3 / 4 với `ACCOUNT_ID=ftmo_acc_002` / `ACCOUNT_ID=exness_acc_002`.
4. Trên frontend Server (qua Tailscale):
   - Settings → Accounts → Add `ftmo_acc_002`
   - Settings → Accounts → Add `exness_acc_002`
   - Settings → Pairs → Create `pair_002` linking 2 cái mới
5. Verify AccountStatus bar có 2 dot xanh mới.
6. Đặt thử lệnh nhỏ với pair_002.

## 8. Disaster recovery

### 8.1 Server crash + restart

- systemd auto-restart sau 5s.
- Lifespan setup_consumer_groups() idempotent (không lỗi nếu groups đã tồn tại).
- Pending commands trong cmd_stream chưa XACK → client process tiếp khi reconnect.
- Position tracker resume từ Redis state (no in-memory).

**Risk**: server crash giữa primary fill và secondary push (~1s window). Order ở status `primary_filled`. CTO viết script recovery:

```bash
# /home/ubuntu/recover-secondaries.py
# Iterate orders với status=primary_filled, age > 30s
# → push secondary command nếu chưa có response
```

CEO chạy thủ công khi nghi ngờ.

### 8.2 Client crash

- NSSM/systemd auto-restart.
- Heartbeat key expire sau 30s → Server reject lệnh mới với pair này.
- Khi client back → heartbeat resume → server lại nhận lệnh.

### 8.3 Redis crash

**Worst case**: rare nhưng có thể.
- AOF replay khi Redis restart → recovery state.
- Nếu AOF corrupt → restore từ `/backup/dump-*.rdb` mới nhất → mất tối đa 1 ngày data.
- CEO check broker UI để biết open positions thực tế → có thể cần adjust orders manually.

## 9. Update / upgrade procedure

```bash
# Server
cd ftmo_exness_hedge_v3
git pull
cd apps/server
source .venv/bin/activate
pip install -e . --upgrade
sudo systemctl restart ftmo-server

# Frontend
cd ../web
npm install
npm run build
# Server StaticFiles auto-serve new dist/

# Each client (FTMO/Exness)
git pull
.venv\Scripts\activate
pip install -e . --upgrade
nssm restart ftmo-client-001    # or exness-client-001
```

## 10. Cost estimate (vận hành 1 pair)

- Server VPS: $5-10/tháng (Hetzner CX21 hoặc DO 2GB)
- FTMO client VPS: $4-5/tháng (Linux nhỏ)
- Exness client VPS Windows: $10-15/tháng (Windows VPS 2GB)
- Tailscale free tier: $0
- **Tổng: ~$20/tháng cho 1 pair.**

Mỗi pair thêm: +$15/tháng (1 FTMO Linux + 1 Exness Windows).
