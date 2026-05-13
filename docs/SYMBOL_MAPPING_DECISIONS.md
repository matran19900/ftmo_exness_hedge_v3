# Symbol Mapping Architecture — Decisions Handoff to Phase 4 CTO

**Created**: 2026-05-13
**Source**: CEO ↔ CTO (discussion-only chat instance) — quyết định nguyên tắc, chưa viết solution code/spec
**Target**: CTO instance đang chạy Phase 4 — dùng tài liệu này làm input để thiết kế solution chi tiết + step breakdown
**Scope**: Refactor symbol mapping khỏi file global hiện tại (`symbol_mapping_ftmo_exness.json`) sang kiến trúc per-account/cache-based phù hợp với multi-Exness-broker-type setup.

---

## 1. Vấn đề gốc

File `symbol_mapping_ftmo_exness.json` hiện tại đóng 3 vai trò trộn lẫn:
- **Vai trò A**: Whitelist symbol được phép trade phía FTMO (R31-R34).
- **Vai trò B**: Symbol name mapping FTMO ↔ Exness.
- **Vai trò C**: Numerical specs (contract_size, pip_size, pip_value, units_per_lot).

File chỉ phù hợp khi: **1 FTMO type + 1 Exness type duy nhất**. Vấn đề thực tế:

- Exness có nhiều account type (Standard, Cent, Pro, Raw, Zero) với symbol suffix khác nhau (`EURUSDm` vs `EURUSDc` vs ...) và **contract_size khác nhau** (Standard: 100000 units/lot vs Cent: 1000 units/lot — lệch 100x).
- 1 file global áp dụng cho mọi Exness client → volume hedge sai catastrophic khi CEO cắm account khác type.
- Specs trong file là static, không sync với broker → broker đổi spec → app sai mà không ai biết.

---

## 2. Decisions đã chốt

### D-SM-01: Symbol mapping là per-Exness-account property, KHÔNG global

**Decision**: Mỗi Exness account có 1 symbol mapping riêng, được resolve runtime dựa trên `pair_id → exness_account_id`. Không có file mapping global cho mọi Exness clients.

**Trade-off**: Tăng độ phức tạp lookup ở `order_service`, `position_tracker`, `response_handler` (phải truyền/lookup `account_id` để resolve symbol + specs). Đổi lại: hỗ trợ đúng multi-broker-type.

---

### D-SM-02: Mapping creation qua Web UI Wizard, KHÔNG qua file trong folder client

**Decision**: Refactor toàn bộ mapping flow sang **UI-driven**. Không có file `symbol_mapping.json` trong folder của client. Toàn bộ setup qua frontend.

**Flow chi tiết**:
1. CEO add Exness client mới qua Settings UI (account creation flow).
2. Client connect MT5, gọi `mt5.symbols_get()` + `mt5.symbol_info()` cho từng symbol → publish raw symbols snapshot lên server (Redis ephemeral).
3. Server compute signature (sha256 hash) từ snapshot.
4. Server check `mapping_cache:{sig}`:
   - **HIT** → auto-link cache cho account, status → `active`. Không cần CEO làm gì.
   - **MISS** → flag `mapping_status=pending_mapping`, frontend hiện wizard.
5. Wizard auto-match (logic giống `build_symbol_mapping.py`): `exact` → `suffix_strip` → `manual_hint`.
6. CEO review/confirm/skip từng row → click "Save Mapping".
7. Server atomic write file cache → populate Redis → link account → status `active`.

**Rationale**:
- CEO không cần SSH/copy file vào folder client.
- Specs sync trực tiếp từ MT5 → không có rủi ro CEO gõ nhầm `pip_size`/`contract_size`.
- Setup tập trung tại 1 chỗ (web UI), không phân tán.

**Trade-off**:
- Setup mới mất 5-10 phút click thay vì copy 1 file (chỉ 1 lần/profile).
- Phụ thuộc client online lúc add — MT5 phải connect được mới mở được wizard. Mitigation: nút "Re-sync symbols" để retry khi MT5 reconnect.

---

### D-SM-03: Cache-based mapping reuse với Signature = Sig-1 (sha256 sorted symbol names)

**Decision**: Server cache mapping theo signature. Khi client mới publish raw symbols, server compute signature và check cache trước. Nếu HIT → reuse mapping, không cần CEO làm wizard lại.

**Signature method**: **Sig-1**
```python
signature = sha256(sorted([s.name for s in raw_symbols])).hexdigest()
```
Chỉ hash **tên symbols**, KHÔNG hash specs.

**Rationale**:
- 2 client cùng loại Exness account (cùng Standard, cùng Cent, ...) sẽ có symbols giống nhau → cùng signature → reuse mapping → CEO không phải lặp wizard cho từng client.
- Đơn giản, deterministic, không phụ thuộc CEO label manually.

**Trade-off**:
- Nếu broker thêm/bớt 1 symbol → signature đổi → cache miss. Xử lý bằng diff-aware wizard (D-SM-04).
- Nếu broker đổi spec nhưng giữ tên (vd contract_size 100000 → 99999.99) → signature vẫn match. Xử lý bằng spec divergence validation (D-SM-05).

**Không dùng**:
- Manual account profile label (CEO loại trừ vì khó khăn khi chuyển broker khác).
- Sig-2 (hash + specs) — quá nhạy cảm.
- Sig-3 (subset matching) — không dùng làm signature, nhưng dùng làm logic diff (D-SM-04).

---

### D-SM-04: Diff-aware wizard khi raw_symbols gần giống cache cũ

**Decision**: Khi signature MISS, server fuzzy-match với các cache existing. Nếu intersect ≥95% → mở wizard ở mode "diff": pre-fill toàn bộ mapping từ cache gần nhất, chỉ highlight rows mới/thay đổi để CEO confirm.

**Logic match**:
```python
threshold = 0.95
for existing_sig, existing_cache in cache_index:
    intersect = len(set(new_symbols) & set(existing_cache.symbols))
    union_size = max(len(new_symbols), len(existing_cache.symbols))
    if intersect / union_size >= threshold:
        return existing_cache  # candidate cho diff mode
```

**Kết quả tạo cache MỚI** với signature mới (KHÔNG sửa cache cũ — cache cũ vẫn nguyên cho các account đang dùng).

**Rationale**:
- Broker thêm/bớt vài symbol là thường (ví dụ Exness thêm crypto mới). CEO không phải làm lại 100+ symbols cho 1 symbol thêm.
- Cache cũ vẫn intact → account dùng cache cũ không bị ảnh hưởng.

---

### D-SM-05: Spec divergence khi sig match — contract_size phải GIỐNG TUYỆT ĐỐI

**Decision**: Khi signature match nhưng spec lệch:
- **contract_size**: phải giống tuyệt đối (exact match). Lệch dù 0.01 → BLOCK link, force CEO re-create mapping.
- Spec khác (digits, pip_size, ...): warning nếu lệch, không block (CTO Phase 4 quyết định threshold cụ thể nếu cần).

**Rationale từ CEO**: Volume conversion formula:
```
Volume_Exness = (1 unit tài sản / contract_size)
```
→ contract_size sai = volume hedge sai = mục tiêu cốt lõi #1 (sync) bị phá vỡ. Không có chuyện "tolerance" cho contract_size.

**Implementation hint**: Validation chạy sau khi sig match, trước khi link account vào cache. Nếu fail → flag `mapping_status=spec_mismatch`, frontend hiện wizard với cảnh báo + CEO chọn "Re-create mapping for this account" (tạo cache mới với spec mới).

---

### D-SM-06: Raw symbols snapshot lifecycle — Ephemeral (Option B)

**Decision**: Raw symbols snapshot trong Redis là **ephemeral**.

**Lifecycle**:
1. Client publish → `exness_raw_symbols:{account_id}` (Redis, no TTL nhưng lifecycle quản lý chủ động).
2. Khi `mapping_status=pending_mapping`: snapshot tồn tại trong Redis (để wizard load).
3. Sau khi CEO save mapping qua wizard:
   - Server lưu snapshot vào field `raw_symbols_snapshot` trong file cache `{sig}.json`.
   - DELETE `exness_raw_symbols:{account_id}` khỏi Redis.
4. Re-sync (client publish lại):
   - Server diff với snapshot trong file cache.
   - Giống → không làm gì, không lưu Redis.
   - Khác → giữ Redis snapshot, mở wizard diff-aware mode.

**Rationale**:
- Redis sạch khi mọi account đã map xong.
- Snapshot lúc tạo mapping vẫn lưu vĩnh viễn (trong file cache, cùng chỗ với mapping nó tạo ra) → vẫn diff được khi broker thay đổi.
- File = single source of truth, Redis chỉ là working cache + transient state.

---

### D-SM-07: File = Source of Truth, Redis = Working Cache

**Decision**: File JSON trên disk là source of truth. Redis là copy để query nhanh runtime.

**Startup behavior**:
1. Server startup load tất cả files trong `server/data/symbol_mapping_cache/*.json`.
2. Populate Redis từ files (overwrite nếu Redis đã có).
3. Nếu Redis có entry mà file không có → log WARN + xóa Redis (file là truth).
4. Nếu file có entry mà Redis không có → load vào Redis.

**Save behavior** (CEO confirm mapping qua wizard):
1. Atomic write file (`tempfile + rename` trên cùng filesystem).
2. Sau file write thành công → write Redis.
3. Nếu Redis write fail nhưng file thành công → server log ERROR, sẽ load lại khi startup. CEO có thể manually trigger reload.

**Rationale**:
- File dễ Git track (cho audit trail) — KHÔNG commit vào main repo nhưng có thể backup riêng.
- File dễ backup (rsync, tar).
- Redis flush nhầm không mất mapping.

---

### D-SM-08: Folder layout

**Decision**:
```
server/
├── data/
│   ├── ftmo_whitelist.json                              ← FTMO canonical (D-SM-09)
│   └── symbol_mapping_cache/
│       ├── exness_acc_001_<full_sha256>.json
│       ├── exness_acc_003_<full_sha256>.json
│       └── ...
```

**Permissions**:
- `server/data/ftmo_whitelist.json`: read-only cho server runtime. CEO maintain bằng tay, commit qua Git.
- `server/data/symbol_mapping_cache/*.json`: read-write cho server runtime (atomic write tempfile + rename). KHÔNG commit Git (gitignore folder này trừ ftmo_whitelist.json).

**Backup**: Folder `server/data/symbol_mapping_cache/` cần được include trong deployment backup procedure (rsync, tar). Phase 4 CTO document trong RUNBOOK.

---

### D-SM-09: FTMO whitelist là static, global file

**Decision**: Phía FTMO KHÔNG có per-account mapping/wizard. Chỉ có 1 file global `server/data/ftmo_whitelist.json` chứa canonical symbols FTMO. Tất cả FTMO clients reference cùng file này.

**Rationale từ CEO**: "Các FTMO client là giống nhau về symbols" — FTMO cTrader trả symbols giống nhau cho mọi FTMO account. Server cũng có FTMO account riêng cho market data, mọi FTMO client tham chiếu theo dữ liệu server.

**Nội dung file** (sau refactor):
```json
{
  "version": 1,
  "symbols": [
    {
      "name": "EURUSD",
      "asset_class": "forex",
      "quote_ccy": "USD",
      "ftmo_units_per_lot": 100000,
      "ftmo_pip_size": 0.0001,
      "ftmo_pip_value": 10.0
    },
    {
      "name": "XAUUSD",
      "asset_class": "metals",
      "quote_ccy": "USD",
      "ftmo_units_per_lot": 100,
      "ftmo_pip_size": 0.1,
      "ftmo_pip_value": 10.0
    }
  ]
}
```

**KHÔNG còn**: bất kỳ field nào liên quan Exness (exness_symbol, exness_contract_size, exness_pip_size, exness_pip_value).

**Maintenance**:
- CEO maintain bằng tay (CEO build từ TSV xuất từ cTrader, giống approach `build_symbol_mapping.py` nhưng chỉ phần FTMO).
- Commit vào Git như source code config.
- Đổi file → restart server (immutable runtime, giống R34 hiện tại).

---

### D-SM-10: File naming convention cho cache files

**Decision**: `{clientName}_{full_sha256_hash}.json`

**Quy tắc**:
- `clientName` = `account_id` của Exness client **đầu tiên** trigger tạo cache này (field `created_by_account` trong nội dung file).
- `full_sha256_hash` = sha256 đầy đủ (64 hex chars), KHÔNG shortened.
- Phân cách bằng `_`.

**Ví dụ**:
```
exness_acc_001_a3f5b9c2d4e6f8a1...{rest of sha256}.json
exness_acc_003_b7c8d9e0f1a2b3c4...{rest of sha256}.json
```

**Logic uniqueness**:
- Cache **lookup key** = signature thuần (hash), không phụ thuộc clientName.
- ClientName chỉ ở tên file để CEO đọc bằng mắt → biết "profile này được acc_001 tạo lần đầu".
- Khi account thứ 2+ join cùng signature → KHÔNG đổi tên file, chỉ append vào `used_by_accounts` array trong nội dung file.

**Edge case — account đầu tiên bị xóa**:
- CEO xóa `exness_acc_001` khỏi system, nhưng acc_002, acc_003 vẫn dùng cache.
- File tên vẫn là `exness_acc_001_{hash}.json` (lịch sử) — **KHÔNG rename**.
- Đơn giản, không có race condition rename, CEO biết "đây là profile lịch sử từ acc_001 dù acc đó không còn".

---

### D-SM-11: Nội dung file cache

**Decision**: Mỗi file cache có schema sau:

```json
{
  "signature": "a3f5b9c2d4e6f8a1...{full sha256}",
  "created_at": "2026-05-13T10:00:00Z",
  "updated_at": "2026-05-13T10:00:00Z",
  "created_by_account": "exness_acc_001",
  "used_by_accounts": ["exness_acc_001", "exness_acc_003"],
  "raw_symbols_snapshot": [
    {
      "name": "EURUSDm",
      "contract_size": 100000,
      "digits": 5,
      "pip_size": 0.0001,
      "volume_min": 0.01,
      "volume_step": 0.01,
      "volume_max": 200,
      "currency_profit": "USD"
    }
  ],
  "mappings": [
    {
      "ftmo": "EURUSD",
      "exness": "EURUSDm",
      "match_type": "suffix_strip",
      "contract_size": 100000,
      "pip_size": 0.0001,
      "pip_value": 10.0,
      "quote_ccy": "USD"
    }
  ]
}
```

**Schema design notes**:
- `raw_symbols_snapshot`: snapshot lúc tạo mapping, để diff khi broker thay đổi sau này.
- `mappings`: kết quả CEO confirm qua wizard. Bao gồm cả specs (copied từ raw snapshot) để lookup nhanh.
- `used_by_accounts`: reverse index để biết cache này đang được account nào dùng. Khi account bị xóa → cập nhật array (không rename file).
- CTO Phase 4 quyết định schema cuối cùng + validate qua Pydantic v2 strict.

---

### D-SM-12: Manual hint rules vị trí

**Decision**: Manual hint rules (vd `NATGAS.cash → XNGUSD`, `GER40.cash → DE30`) lưu trong **config file** ở `server/config/symbol_match_hints.json`, load lúc startup.

**Rationale**:
- CEO sửa được không cần deploy code, chỉ cần restart server.
- Hardcode vào code → cập nhật rules = thay đổi source code → deployment cycle.
- Tách config khỏi code = clean separation.

**Format**:
```json
{
  "version": 1,
  "hints": [
    {"ftmo": "NATGAS.cash", "exness_candidates": ["XNGUSD"], "note": "Natural gas hedge proxy"},
    {"ftmo": "GER40.cash", "exness_candidates": ["DE30", "DAX40"], "note": "DAX index"},
    {"ftmo": "US30.cash", "exness_candidates": ["US30"], "note": ""},
    {"ftmo": "US500.cash", "exness_candidates": ["US500"], "note": ""},
    {"ftmo": "US100.cash", "exness_candidates": ["USTEC", "NAS100"], "note": ""}
  ]
}
```

**Usage trong wizard auto-match**: sau khi thử `exact` + `suffix_strip` không match → lookup hints, nếu có candidate trùng với raw symbols broker → suggest. CEO vẫn confirm/override.

---

## 3. Open items — CTO Phase 4 quyết định

Các điểm dưới đây CEO chưa quyết — CTO Phase 4 propose hướng giải quyết khi thiết kế solution doc:

1. **Wizard UX details**:
   - Bulk actions: "Accept all auto-matched" / "Skip all unmapped" / Filter by asset class.
   - Hiển thị specs preview trong wizard (contract_size, pip_size từ raw snapshot).
   - Validation feedback realtime khi CEO override (vd cảnh báo contract_size mismatch).

2. **API endpoints**:
   - `GET /accounts/exness/{acc_id}/raw-symbols` — load raw snapshot cho wizard.
   - `POST /accounts/exness/{acc_id}/symbol-mapping/auto-match` — server compute auto-match, return proposal.
   - `POST /accounts/exness/{acc_id}/symbol-mapping/save` — CEO submit final mapping.
   - `GET /symbol-mapping-cache` — list all cache entries (admin/debug).

3. **Atomic write strategy**:
   - Tempfile + rename trên cùng filesystem (POSIX atomic).
   - Lock file để tránh concurrent writes (2 wizards mở cùng lúc cho 2 accounts cùng signature).
   - Backup file cũ trước khi overwrite (vd `.bak`).

4. **Migration từ file cũ**:
   - File `symbol_mapping_ftmo_exness.json` hiện tại được dùng như input để bootstrap:
     - Phần FTMO → trích ra `server/data/ftmo_whitelist.json`.
     - Phần Exness → archive (không tự động convert thành cache file vì specs có thể outdated).
   - Hoặc CTO Phase 4 quyết định cách khác.

5. **Spec divergence threshold cho fields khác contract_size**:
   - digits, pip_size, pip_value: threshold bao nhiêu thì warning?
   - Hoặc chỉ block contract_size, các field khác luôn dùng từ raw snapshot mới nhất?

6. **Edit mapping flow**:
   - Account row có nút "Edit Mapping" → mở wizard với mapping hiện tại + diff so với raw symbols mới nhất.
   - Phase 4 chỉ có create, hay create + edit + delete?
   - Tôi (CTO discussion-only) nghiêng **Phase 4 = create + edit**, delete defer Phase 5.

7. **Order_service lookup pattern refactor**:
   - Hiện tại: `whitelist.volume_conversion_ratio(symbol)` — global lookup.
   - Sau refactor: cần lookup `pair → ftmo_acc + exness_acc → symbol_mapping_cache → ratio`.
   - Impact lớn lên `order_service`, `position_tracker`, `response_handler`. CTO Phase 4 design careful.

8. **Frontend store changes**:
   - Whitelist symbol list trong Zustand store hiện global → thay đổi thành per-pair (chọn pair mới biết symbols nào available trên cả 2 leg).
   - UX impact lên HedgeOrderForm — chọn pair TRƯỚC, sau đó symbol dropdown filter theo intersect(ftmo_whitelist, exness_mapping[pair.exness_acc]).

9. **Timing trong Phase 4**:
   - Refactor này nên insert TRƯỚC step 4.1 (MT5 client skeleton) vì 4.1 sẽ implement symbol sync — cần biết structure trước.
   - Hoặc tạo sub-phase 4.0.x riêng.
   - CTO Phase 4 quyết định step breakdown.

---

## 4. Reference materials CEO đã cung cấp

Các file CEO sẽ gửi kèm cho CTO Phase 4:

1. **`build_symbol_mapping.py`** — script Python hiện tại CEO dùng build file `symbol_mapping_ftmo_exness.json`. Chứa logic:
   - `MANUAL_EXNESS` dict (sẽ chuyển thành `server/config/symbol_match_hints.json` — D-SM-12).
   - `_strip_ftmo_suffix()` (logic suffix_strip).
   - `pip_value()` formula.
   - Đây là **reference cho logic auto-match** trong server.

2. **`ftmo_symbols.tsv`** — export symbols từ FTMO cTrader account của CEO. Format:
   - Columns: `symbol`, `units_per_lot`, `pip_size`, `digits`, ...
   - Đây là **reference cho format raw export** mà CEO sẽ dùng tạo `ftmo_whitelist.json`.

3. **`exness_symbols.tsv`** — export symbols từ Exness account của CEO. Format tương tự.
   - Trong kiến trúc mới, raw export này sẽ được **client publish trực tiếp lên server**, không cần export TSV ra ngoài.
   - Nhưng format các field trong TSV (`symbol`, `trade_contract_size`, `digits`, `pip_size`) là **reference cho schema raw_symbols_snapshot** trong file cache (D-SM-11).

4. **`symbol_mapping_ftmo_exness.json`** — file mapping hiện tại đang dùng production. Dùng cho:
   - Migration input (D-SM-13 trong open items).
   - Reference cho `MANUAL_EXNESS` mappings đã được CEO verify bằng kinh nghiệm trader.

---

## 5. Acceptance khi CTO Phase 4 finalize solution

CTO Phase 4 sẽ:
1. Đọc file này.
2. Đọc reference materials (4 files trên).
3. Viết solution doc đầy đủ (kiến trúc, flow, data structure, edge cases, acceptance criteria) — có thể là `docs/phase-4-symbol-mapping-design.md` hoặc append vào `docs/phase-4-design.md` đang viết.
4. Resolve các open items ở §3 bằng decisions cụ thể (assign mã D-NNN khi commit vào DECISIONS.md ở step docs-sync).
5. Viết step breakdown — cách insert vào Phase 4 plan hiện tại.
6. Viết prompt cho Claude Code (1 step đầu tiên).
7. CEO review prompt → execute → check → tiếp tục.

---

## 6. Lưu ý cho CTO Phase 4

- Các decisions trong tài liệu này là **principles**, không phải spec. CTO Phase 4 có quyền propose điều chỉnh principle nếu phát hiện trade-off CEO chưa thấy.
- Tài liệu này thuộc chat instance riêng (tránh context bloat của chat Phase 4 chính). Sau khi CTO Phase 4 reconcile decisions vào DECISIONS.md, file này có thể archive.
- Nếu CTO Phase 4 cần clarification thêm về **principles** (không phải spec details), CEO sẽ quay lại chat discussion-only này để confirm với CTO discussion-only.
