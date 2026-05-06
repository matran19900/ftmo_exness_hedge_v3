# 12 — Business Rules & Invariants

Đây là contract giữa engineering và business. Mọi rule có **mã** (R1, G1...) để truy ngược trong code comments + commit messages.

## 1. Hedging logic (R1–R10)

### R1 — Primary always FTMO
Phía FTMO là **primary leg**, có SL/TP gắn vào. Đây là vế "thật" để pass challenge.

### R2 — Secondary always Exness
Phía Exness là **secondary leg**, đối ứng. Là vế "bảo hiểm" — nếu FTMO fail challenge, Exness lãi tương đương.

### R3 — Secondary KHÔNG có SL/TP
Lệnh Exness mở **market only**, KHÔNG SL/TP. Đóng theo cascade khi primary đóng. Lý do:
- Tránh trường hợp 1 leg đóng trước (slippage không đồng nhất).
- Cascade close server-driven đảm bảo cả 2 đóng "gần như đồng thời".

### R4 — Secondary side ngược primary
- Primary BUY → Secondary SELL.
- Primary SELL → Secondary BUY.

### R5 — Primary fill TRƯỚC, secondary fill SAU
**Sequence**: primary order qua FTMO trước → khi fill → secondary qua Exness. KHÔNG đặt song song.

Lý do: nếu đặt song song và 1 leg fail, leg kia mồ côi. Đặt tuần tự + phòng vệ:
- Primary fail → KHÔNG đặt secondary, order kết thúc với status `cancelled`.
- Primary fill, secondary fail → status `secondary_failed`. Server retry 0.5s/1s/2s. Sau 3 lần fail → toast cảnh báo CEO, primary giữ nguyên (CEO quyết định đóng hay giữ).

### R6 — Volume primary tính từ risk_amount
```
sl_pips = abs(entry - sl) / pip_size
pip_value_quote = pip_size * ftmo_contract_size           # quote currency / pip / lot
pip_value_usd = pip_value_quote * quote_to_usd_rate
sl_usd_per_lot = sl_pips * pip_value_usd
volume_p = risk_amount / sl_usd_per_lot
volume_p = clamp(volume_p, min_lot, max_lot) → round to volume_step
```

### R7 — Volume secondary scale theo ratio + contract_size
```
volume_s_raw = volume_p × secondary_ratio × (ftmo_contract_size / exness_contract_size)
volume_s = clamp_round(volume_s_raw, exness_min_lot, exness_max_lot, exness_volume_step)
```

`secondary_ratio` mặc định 1.0, có thể đổi per-pair hoặc per-order.

### R8 — Final P&L = sum(p_realized_pnl, s_realized_pnl)
USD. Hiển thị dưới history với break-down 2 leg.

### R9 — Cascade on primary close
Bất kỳ event nào đóng primary (TP, SL, manual cTrader, manual app, externally) → server gửi cascade close cho secondary.

### R10 — Cascade on secondary close (NEW v2)
Nếu secondary bị đóng externally (margin call MT5, manual MT5 UI) → `position_monitor_loop` detect → server cascade close primary.

## 2. SL/TP rules (R11–R20)

### R11 — SL bắt buộc cho primary
Mỗi hedge order phải có SL > 0 ở primary.

### R12 — TP optional
TP = 0 → server skip TP, chỉ set SL.

### R13 — SL distance ≥ 5 pips (cấu hình được)
Reject nếu `sl_pips < MIN_SL_PIPS`. Lý do: tránh micro-stop bị slippage hit ngay khi mở.

### R14 — SL/TP gắn ở broker side (FTMO server-side)
Không phải server local check. cTrader broker tự fire khi giá hit. Nếu broker reject SL invalid → response error → status cancelled.

### R15 — Pip size theo asset
- Forex 5-digit (vd EURUSD): `pip_size = 0.0001`.
- Forex JPY: `pip_size = 0.01`.
- Indices (US30, NAS100): `pip_size = 1`.
- Gold (XAUUSD): `pip_size = 0.01`.
- Crypto (BTCUSD): `pip_size = 1`.

Server đọc từ `symbol_config:{sym}.pip_size`, sync từ market-data cTrader.

### R16 — SL direction theo side
- BUY: SL phải < entry, TP phải > entry.
- SELL: SL phải > entry, TP phải < entry.
Reject 400 nếu sai.

### R17 — Min SL pips configurable
`MIN_SL_PIPS=5` mặc định. Đổi qua `app:settings`. Áp dụng cho mọi symbol bất kể asset class.

### R18 — TP=0 nghĩa là không có TP
Frontend cho phép user clear TP. Server gửi `tp=0` trong command → client hiểu là không set TP.

### R19 — SL/TP modify chỉ áp dụng primary
PATCH `/orders/{id}/sl-tp` → command chỉ tới FTMO client. Secondary KHÔNG có SL/TP (R3).

### R20 — SL/TP modify chỉ khi status=open
Status khác → 409 conflict. Lý do: nếu đang `closing`, modify race với close → broker reject hoặc gây inconsistency.

## 3. Account & pair rules (R21–R23)

### R21 — Mỗi pair = đúng 1 FTMO + đúng 1 Exness
1-1 mapping. KHÔNG hỗ trợ 1 FTMO ↔ N Exness hoặc N-N.

### R22 — Account_id unique global
`ftmo_acc_001` và `exness_acc_001` là 2 namespace riêng. Cùng account_id 2 broker khác nhau OK.

### R23 — Không xóa account đang dùng
Reject DELETE nếu:
- Account còn pair link → DELETE pair trước.
- Pair còn open orders → close orders trước.

## 4. Connectivity rules (R24–R26)

### R24 — Reject lệnh mới nếu pair offline
Trước khi push command, server check heartbeat 2 client của pair. Bất kỳ 1 cái offline → 503.

### R25 — WS event chỉ carry order_id
Lifecycle events qua WS chỉ chứa `{type, order_id}`. Frontend refetch `/positions` để lấy state mới.

Lý do: tránh bug stale state khi multiple events arrive out-of-order.

### R26 — WS subscribe diff
Khi đổi symbol/timeframe → unsubscribe old + subscribe new (không full re-subscribe). Tránh server resend full state.

## 5. P&L rules (R27–R30)

### R27 — P&L USD chính xác mọi asset class
Server convert quote currency → USD bằng tick conversion pair (vd USDJPY for JPY pairs).

### R28 — P&L tracker interval 1s
Quá nhanh → load Redis + WS broadcast lớn. Quá chậm → user thấy lag. 1s là sweet spot.

### R29 — Tick stale = 5s threshold
`tick:{sym}` TTL 5s. Tick > 5s coi như stale. Frontend hiển thị icon cảnh báo.

### R30 — Snapshot mỗi 30s vào ZSET cho mini-chart
`order:{id}:snaps` lưu pnl history mỗi 30s. Đủ chi tiết cho mini-chart trong order detail modal, không phình bộ nhớ.

## 6. Symbol whitelist rules (R31–R34)

### R31 — `symbol_mapping_ftmo_exness.json` là single source of truth
Symbol KHÔNG có trong file → KHÔNG hiển thị frontend, KHÔNG trade được. Server reject 404.

### R32 — Server filter khi sync symbols
Market-data cTrader trả nhiều symbol. Server iterate, **chỉ giữ** symbols match whitelist key (`ftmo_symbol`).

### R33 — Volume conversion FTMO → Exness theo file
Field `ftmo_units_per_lot / exness_trade_contract_size` từ file. Mọi tính toán volume secondary dùng ratio này.

### R34 — Symbol map immutable runtime
File load 1 lần lúc startup. Đổi file → restart server. Không hot-reload.

> Lý do đơn giản hóa: tránh race condition giữa orders đang process với map mới.

## 7. Edge cases (G1–G15)

Đánh dấu G để phân biệt với rule R (rule = invariant phải đảm bảo, edge case = tình huống cần xử lý đúng).

### G1 — Primary fill nhưng secondary fail
- Status: `secondary_failed`.
- Server retry 3 lần (0.5s, 1s, 2s).
- Sau 3 fail → toast cảnh báo CEO. Primary giữ nguyên.
- CEO quyết định: đóng manual primary, hoặc giữ chấp nhận rủi ro tạm thời.

### G2 — Primary fail
- Status: `cancelled`.
- KHÔNG mở secondary.
- Toast info user.

### G3 — Cả 2 fail
- KHÔNG xảy ra trong sequence (R5: secondary chỉ mở sau primary fill). G2 đã handle.

### G4 — Mất kết nối client giữa flow
- FTMO disconnect sau primary fill: secondary push fail → status `secondary_failed`.
- Exness disconnect sau secondary fill: P&L tracker không bị ảnh hưởng (đọc tick từ market-data).
- Khi client reconnect: heartbeat resume, server tiếp tục.

### G5 — Mất kết nối giữa cascade close
- Primary closed (event), server push cascade close cho secondary, nhưng Exness offline.
- Server retry 3 lần. Vẫn fail → status `closing` mãi.
- Toast cảnh báo. Khi Exness back online → user click "retry close" trong UI.

### G6 — User drag SL khi primary đã đóng (race)
- Frontend drag → PATCH `/orders/{id}/sl-tp` → server check status != open → 409.
- Frontend revert SL line + toast warning.

### G7 — User click × 2 lần liên tiếp
- DELETE 1 lần → status=`closing`.
- DELETE lần 2 → server check status=`closing` → 409 idempotent ignore.
- Frontend disable button khi đã click.

### G8 — Tick missing khi đặt market
- POST /orders/hedge khi tick không có (subscribe chưa kịp): 503 "no tick available".
- Frontend toast retry sau 1s.

### G9 — JPY pair: USDJPY tick chưa có
- Position tracker iterate JPY order → cần USDJPY tick → tick chưa có → server `subscribe_spots(['USDJPY'])` + skip 1 round (return rate=0 → skip update).
- Round sau có tick → P&L bình thường.

### G10 — Server restart giữa primary fill và secondary push
- Order ở status `primary_filled`, s_status `waiting_primary` hoặc chưa set.
- Restart: lifespan resume background tasks.
- KHÔNG auto-resume push secondary (tránh duplicate sau crash).
- CEO check qua frontend → click "retry secondary" hoặc đóng primary manual.

### G11 — User xóa pair khi có open order
- DELETE /pairs/{id} → server check `orders:by_status:open` filter `pair_id` → reject 409.

### G12 — Primary đóng do TP, secondary đóng độc lập do MT5 stopout cùng lúc
- 2 events arrive gần như đồng thời.
- handle_event idempotency: status guard. Sự kiện đầu update status `closing`, sự kiện thứ 2 thấy `closing` (legs cùng đóng) → final close OK.
- Final P&L tính bình thường.

### G13 — Symbol đổi name ở broker (vd `EURUSD` → `EURUSD.r`)
- Khi sync symbols, ktra whitelist không match → drop symbol đó.
- Frontend không thấy symbol đó nữa → user không trade được.
- CEO update file `symbol_mapping_ftmo_exness.json` → restart server.

### G14 — Volume tính ra 0.001 nhưng exness min = 0.01
- `clamp_round_exness` round → 0.01.
- volume_secondary KHÁC volume_primary tính theo ratio → toast cảnh báo "secondary volume rounded up".

### G15 — Disconnect cTrader market-data connection
- Server log error, retry connect mỗi 30s.
- Trong khoảng disconnect: tick stale, P&L update ngừng, mới đặt lệnh sẽ fail (no tick).
- Frontend agents channel show market-data status offline.

## 8. Security rules (R35–R38)

### R35 — JWT bắt buộc cho mọi REST + WS
Trừ `/auth/login`, `/auth/ctrader*` (OAuth flow phải accessible browser), `/health` (nếu có).

### R36 — Password bcrypt
Stored: `$2b$12$...` hash. Verify qua `bcrypt.checkpw`. Plain password không bao giờ log.

### R37 — JWT secret từ env, never commit
`.env` trong `.gitignore`. CEO sync qua channel an toàn.

### R38 — Sensitive fields không log
`access_token`, `password_hash`, `JWT_SECRET`, `MT5_PASSWORD`, `client_secret` mask trong logs (`***`).

## 9. Data rules (R39–R42)

### R39 — Mọi timestamp epoch ms (int as string)
Redis lưu, JSON wire format. ISO chỉ ở display.

### R40 — Order_id format `ord_<8-char>`
Generated server-side, sortable lỏng theo time.

### R41 — Account_id user-defined
`[a-zA-Z0-9_-]{1,32}`. CEO tự đặt khi add account.

### R42 — Pair_id user-defined
Same constraint.

## 10. Operational rules (R43–R45)

### R43 — Mỗi máy 1 client process
KHÔNG chạy 2 FTMO clients trên cùng máy. Nếu cần → tách máy.

### R44 — MT5 terminal phải pre-login + auto-start
Service NSSM start trước khi MT5 ready → init fail → restart loop. Để MT5 luôn chạy + auto-login.

### R45 — Backup Redis trước major upgrade
Trước khi `git pull` + restart server → `BGSAVE` + copy dump.rdb.

---

## 11. Chart overlay rules (R46–R48)

### R46 — Filter order overlay theo symbol + pair
Frontend chỉ vẽ horizontal lines (Entry/SL/TP) cho order thỏa CẢ 2 điều kiện:
- `order.symbol === store.selectedSymbol`
- `order.pair_id === store.selectedPairId`

Lý do: chart data từ market-data cTrader chỉ chính xác cho 1 symbol đang xem. Vẽ order khác symbol gây sai vị trí.

### R47 — Chỉ vẽ leg primary (FTMO), KHÔNG vẽ secondary (Exness)
- Symbol FTMO ≠ symbol Exness (vd `XAUUSD` ↔ `GOLD`).
- Giá quote 2 broker khác nhau, slippage khác nhau.
- Secondary KHÔNG có SL/TP ở broker side (R3).
→ Vẽ secondary lên chart cTrader sẽ sai và gây hiểu lầm.

### R48 — Ba trạng thái line phân biệt bằng style
| Trạng thái | Điều kiện | Style | Drag |
|---|---|---|---|
| **Setup** | Form đang fill, chưa submit | Dashed, light color | ✅ (update form) |
| **Pending** | `order.status === 'pending'` | Dashed, medium color | ❌ |
| **Open** | `order.status === 'open'` | Solid, dark color | ✅ (PATCH SL/TP) |

Color convention: Entry = blue, SL = red, TP = green.

Quản lý price line keyed by `order_id` để tránh full redraw → flicker (lesson learned từ v1).

---

## 12. Test matrix

| Rule | Test scenario |
| --- | --- |
| R3 | Đặt hedge → verify command tới Exness KHÔNG có sl/tp fields |
| R5 | Mock primary fail → verify secondary KHÔNG được push |
| R6 | Risk $100 EURUSD SL 20 pips → volume_p ≈ 0.5 lots |
| R9 | Manual close cTrader → secondary đóng < 1s sau |
| R10 | Manual close MT5 UI → primary đóng < 3s sau (poll 2s) |
| R13 | SL distance 3 pips → 400 reject |
| R16 | BUY với SL > entry → 400 reject |
| R24 | Stop FTMO client → POST /orders/hedge → 503 |
| R27 | USDJPY P&L → assert match cTrader/MT5 ± 1% |
| R31 | GET /symbols → chỉ symbols trong whitelist |
| R31 | GET /symbols/EXOTIC → 404 |
| R46 | Mở chart EURUSD pair_main → có 1 lệnh USDJPY pair_main + 1 lệnh EURUSD pair_alt → chỉ thấy line của lệnh EURUSD pair_main |
| R47 | Open hedge order → chart chỉ vẽ leg primary (FTMO), không vẽ secondary (Exness) |
| R48 | Form draft → dashed light. Status pending → dashed medium. Status open → solid dark |
| G1 | Mock secondary fail × 3 → status `secondary_failed`, toast error |
| G6 | Drag SL khi status=closing → 409 + revert |
| G10 | Kill server giữa primary fill + secondary push → restart → status `primary_filled` persist |
