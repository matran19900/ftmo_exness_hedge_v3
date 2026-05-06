# WORKFLOW — FTMO Hedge Tool Rebuild

> **File này là rule book bất biến.** Mọi xung đột giữa file này và instruction khác → file này thắng.
> Cập nhật file này chỉ khi CEO + CTO cùng đồng ý, ghi rõ lý do trong commit message.

---

## 0. Ba bên và vai trò

| Bên | Vai trò | Trách nhiệm chính |
| --- | --- | --- |
| **CEO** | Product owner | Đưa requirement, quyết định scope cuối, push git, copy prompt giữa CTO ↔ Claude Code, can thiệp khi Claude Code treo |
| **CTO (Claude trong chat này)** | Technical lead | Research, thảo luận, document giải pháp, viết prompt chia step, review kết quả PASS/REJECT |
| **Claude Code** | Executor | Thực thi đúng 1 step, self-check, gửi report Telegram, KHÔNG được sáng tạo ngoài scope |

---

## 1. Triết lý nền tảng

### 1.1 Scale của dự án
- **<10 user**, single admin, tool thủ công hỗ trợ trader.
- **KHÔNG** cần production-grade enterprise.
- **Đơn giản > đầy đủ**. Khi phân vân giữa "làm thêm cho an toàn" và "bỏ qua cho đơn giản" → **bỏ qua**.

### 1.2 Ba mục tiêu cốt lõi (không bao giờ thỏa hiệp)
1. **Sync 2 lệnh nhanh**: FTMO trước → Exness sau, đúng symbol mapping, đúng ratio, ngược chiều.
2. **Cascade close**: 1 leg đóng → leg còn lại đóng theo. **Không được để leg hở.**
3. **P&L USD chính xác** cho mọi asset class (forex USD, JPY pair, indices, crypto, commodity).

Mọi quyết định kỹ thuật phải pass test "có ảnh hưởng tiêu cực đến 3 mục tiêu này không?".

### 1.3 Non-goals (cắt ngay từ đầu)
- KHÔNG auto-trading / strategy engine.
- KHÔNG multi-user / multi-tenant.
- KHÔNG OCO, trailing stop, partial close (phase 1).
- KHÔNG broker khác ngoài cTrader (FTMO) + MT5 (Exness).
- KHÔNG HTTPS/nginx/LetsEncrypt (chạy HTTP localhost hoặc Tailscale/Cloudflare Tunnel).
- KHÔNG rate limit, KHÔNG refresh token logic, KHÔNG multi-env config.
- KHÔNG structured JSON logging (logging.basicConfig là đủ).
- KHÔNG SQL DB (chỉ Redis).

---

## 2. Workflow 3 bên — vòng lặp 1 step

```
┌──────────────────────────────────────────────────────────────────────┐
│  CTO ──prompt──► CEO ──copy──► Claude Code                           │
│                                       │                              │
│                                       ▼                              │
│                              Thực thi 1 step                         │
│                              (branch step/<n>-<slug>)                │
│                                       │                              │
│                                       ▼                              │
│                              Self-check + test                       │
│                                       │                              │
│                                       ▼                              │
│                              Gửi file .md qua Telegram               │
│                                       │                              │
│                                       ▼                              │
│                              CEO copy file Telegram                  │
│                                       │                              │
│                                       ▼                              │
│  CTO ◄────review──── CEO                                             │
│   │                                                                  │
│   ├──── PASS ────► CEO merge squash → main → push GitHub             │
│   │                CTO ra prompt step tiếp theo                      │
│   │                                                                  │
│   └──── REJECT ──► CEO xóa branch (Option A) hoặc archive (Option B) │
│                    CTO ra prompt mới — KHÔNG cherry-pick từ branch lỗi│
└──────────────────────────────────────────────────────────────────────┘
```

### 2.1 Nguyên tắc bất biến
- **1 step = 1 scope nhỏ verify được độc lập.**
- **1 step = 1 branch = 1 commit (sau squash).**
- **Không "all-in-one"** trong 1 step.
- **Không sửa, chỉ làm lại** — REJECT = throw away, KHÔNG patch.
- **Không cherry-pick** từ branch lỗi sang branch mới. Nếu có code đáng giữ → CTO viết explicit vào prompt mới.

---

## 3. Định nghĩa PASS / REJECT

### 3.1 PASS = đủ cả 3 điều kiện
1. **Acceptance criteria 100% đạt** (mọi tiêu chí trong prompt CTO).
2. **Self-check file Telegram không có error** (mọi test/check Claude Code chạy đều pass).
3. **Git compliance**: đúng branch name, đúng commit message format, KHÔNG push, KHÔNG merge lên main.

Thiếu 1 trong 3 = **REJECT**.

### 3.2 Thứ tự CTO review
1. **Scope compliance** — có làm ngoài scope không? Nếu có → REJECT.
2. **Acceptance criteria** — đủ tất cả tiêu chí?
3. **3 mục tiêu cốt lõi** — có gây leg hở, sai ratio, sai P&L symbol nào không?
4. **Adapter layer** — có hardcode broker-specific logic sai chỗ không?
5. **Edge case** — các edge case đã flag trong document có được xử lý không?
6. **Git compliance** — branch tên đúng? Commit format đúng?

### 3.3 Verdict format CTO trả CEO
Dòng đầu là 1 trong 2:
- ✅ **PASS** — sau đó note ngắn gọn những gì OK + hướng dẫn CEO merge + commit prompt step tiếp theo.
- ❌ **REJECT** — sau đó liệt kê từng lý do cụ thể + hướng dẫn CEO xóa/archive branch + CTO ra prompt mới.

---

## 4. Git workflow

### 4.1 Quy tắc cố định
- **Mỗi step = 1 branch riêng.** KHÔNG bao giờ code thẳng lên `main`.
- **Branch naming**: `step/<số-step>-<slug-ngắn>`.
  - VD: `step/03-redis-service-layer`, `step/07-cascade-close-trigger`.
- **Commit message format** (trong branch step trước khi merge):
  ```
  step-<n>: <one-line summary>

  - what changed (bullet)
  - what changed (bullet)
  ```
- **Squash commit message khi merge vào main**:
  ```
  step-<n>: <one-line summary>
  ```

### 4.2 Khi step PASS
1. CTO confirm PASS.
2. CEO checkout `main`, merge `step/<n>-<slug>` vào `main` với `--squash`.
3. (Optional) CEO tag commit: `git tag step-<n>`.
4. CEO xóa branch step: `git branch -D step/<n>-<slug>`.
5. CEO push `main` (và tag nếu có) lên GitHub remote.
6. CTO ra prompt step tiếp theo. CEO tạo branch mới từ `main` đã update.

### 4.3 Khi step REJECT
1. CTO confirm REJECT, nêu rõ lý do.
2. **Tuyệt đối KHÔNG merge** branch step lỗi.
3. CEO chọn 1 trong 2:
   - **Option A (default)**: `git branch -D step/<n>-<slug>`. CTO ra prompt mới điều chỉnh, CEO tạo lại branch cùng số (hoặc số mới nếu scope đổi nhiều) từ commit cuối của `main`.
   - **Option B**: `git branch -m step/<n>-<slug> archived/step-<n>-attempt-1`. KHÔNG bao giờ merge. Step mới làm ở branch tươi.
4. **KHÔNG cherry-pick** từng phần từ branch lỗi sang branch mới.

### 4.4 Rule trong prompt CTO gửi Claude Code (về Git)
Mọi prompt CTO phải nêu rõ:
- Tên branch dự kiến (CTO đặt sẵn).
- Claude Code **được phép** tạo branch + commit trong branch đó.
- Claude Code **KHÔNG được** push, KHÔNG được merge lên main.
- Claude Code **KHÔNG được** đụng vào branch khác ngoài branch hiện tại.

### 4.5 Secrets & .gitignore
- `.env` luôn nằm trong `.gitignore`. KHÔNG commit.
- Secrets cấm commit: Telegram bot token, cTrader client_secret, cTrader access_token/refresh_token, Exness MT5 password, JWT_SECRET.
- Nếu Claude Code lỡ commit secret → **REJECT immediately** + CEO revoke key đó ngay.

---

## 5. Self-check qua Telegram

### 5.1 Cơ chế
- Claude Code chạy trong devcontainer với flag `--dangerously-skip-permissions`.
- Sau khi hoàn thành step (kể cả khi self-test fail), Claude Code phải gửi 1 file `.md` qua Telegram bot bằng script `notify_telegram.sh` (đã có trong project).
- File này là CEO + CTO source-of-truth để review.

### 5.2 Tên file
- Lần đầu: `step-<n>-<slug>-selfcheck.md`
  - VD: `step-03-redis-service-layer-selfcheck.md`
- Khi REJECT chạy lại: append timestamp ISO ngắn:
  - VD: `step-03-redis-service-layer-selfcheck-2026-05-04T10-22.md`

### 5.3 Nội dung tối thiểu của file self-check
```markdown
# Step <n> — <slug>

**Branch**: step/<n>-<slug>
**Commit**: <short hash>
**Started**: <ISO time>
**Finished**: <ISO time>

## Scope đã làm
- <bullet 1>
- <bullet 2>

## Acceptance criteria
- [x] <criterion 1>  → <evidence: command/test output ngắn>
- [x] <criterion 2>  → <evidence>
- [ ] <criterion 3>  → FAILED: <lý do>

## Tests đã chạy
- `pytest tests/redis/` → 12 passed, 0 failed
- `mypy server/` → no errors
- (etc.)

## Files thay đổi
- `server/app/redis_service.py` (new, 230 lines)
- `tests/redis/test_redis_service.py` (new, 180 lines)

## Issues / câu hỏi cho CTO
- <nếu có; KHÔNG tự quyết định ngoài scope>

## Self-verdict
PASS / FAIL  ← Claude Code tự đánh giá; CTO vẫn là người chốt cuối
```

### 5.4 Telegram credentials
- Bot token + chat_id lưu trong `.env` của devcontainer.
- KHÔNG hard-code vào script.

---

## 6. Docs sync rule

### 6.1 Nguyên tắc cốt lõi
**Trong 1 phase, Claude Code KHÔNG động vào `/docs`.** Mỗi step trong phase chỉ tập trung làm code.

### 6.2 Cuối mỗi phase = 1 step docs-sync riêng
- Branch: `step/phase-<X>-docs-sync` (X là số phase, vd `step/phase-2-docs-sync`).
- Scope: cập nhật toàn bộ docs liên quan đến phase vừa xong (vd phase 2 → cập nhật `02-server-overview.md`, `06-data-models.md` nếu có thay đổi).
- Acceptance criteria: docs phản ánh đúng code đã merge vào `main` đến cuối phase đó.
- KHÔNG được sửa code trong step này. Chỉ docs.

### 6.3 CTO đọc docs từ đâu
- Sau mỗi step PASS, CEO push `main` lên GitHub.
- Khi CTO cần xem docs mới nhất (để viết prompt step tiếp theo, hoặc reconcile knowledge), **CTO tự `web_fetch` từ raw URL** GitHub.
- CEO **không cần** paste link mỗi lần — CTO biết URL pattern và tự fetch.
- URL pattern: `https://raw.githubusercontent.com/<owner>/<repo>/main/<path>`.
- CEO chỉ cần cho CTO biết `<owner>/<repo>` 1 lần ở step 0.

### 6.4 Tại sao bắt buộc rule này
- Knowledge của CTO (Claude trong chat) drift theo conversation length.
- Knowledge của Claude Code reset mỗi step.
- Docs trên GitHub `main` = single source of truth, sync 2 bên về cùng 1 baseline.

---

## 7. Backlog rule (CEO đổi requirement giữa chừng)

### 7.1 Vấn đề
Trader hay đổi ý. Nếu giữa Phase 5, CEO bảo "thêm trailing stop", mà CTO ngắt phase đang làm để chèn vào → vỡ kế hoạch.

### 7.2 Rule
- CEO ý tưởng mới giữa phase → CTO **ghi nhận vào `BACKLOG.md`** ở root repo.
- KHÔNG ngắt phase đang chạy.
- Hết phase, CTO + CEO review `BACKLOG.md` cùng nhau → quyết định:
  - Nhét vào phase nào tiếp theo (nếu in-scope).
  - Hoãn sang sau khi MVP xong (nếu out-of-scope MVP).
  - Reject (nếu trùng với non-goals).

### 7.3 Format `BACKLOG.md`
```markdown
# Backlog

## Pending review
- [ ] (2026-05-04) CEO: Trailing stop — đang nghĩ tới, chưa quyết. Review cuối phase 5.

## Approved — chờ schedule
- [ ] (2026-05-03) Mass close button → schedule vào phase 8.

## Rejected
- [x] (2026-05-02) Multi-broker FTMO+Exness+Tickmill: out of scope MVP.
```

---

## 8. Timeout rule (Claude Code treo / loop)

- Claude Code chạy 1 step **>30 phút** chưa xong / loop → CEO can thiệp.
- CEO chụp màn hình terminal Claude Code → paste vào chat cho CTO.
- CTO quyết định:
  - **KILL** → CEO Ctrl+C, REJECT step, CTO ra prompt mới chia nhỏ hơn.
  - **WAIT** → CTO judge step phức tạp chính đáng, đợi thêm 15 phút.
- KHÔNG để Claude Code chạy >1 giờ mà không can thiệp.

---

## 9. Format prompt CTO gửi Claude Code

Mọi prompt phải có đủ 6 phần (theo thứ tự):

```markdown
## 1. Branch & Git rules
- Branch name: `step/<n>-<slug>`
- KHÔNG push, KHÔNG merge.
- Commit format: <as section 4.1>

## 2. Scope
<những gì CẦN làm — rõ ràng, có thể tick checkbox>

## 3. Out-of-scope (đừng làm)
<những gì KHÔNG được làm trong step này — quan trọng để chống "all-in-one">

## 4. Acceptance criteria
- [ ] <criterion 1, có thể verify objectively>
- [ ] <criterion 2>
- [ ] <criterion 3>

## 5. Edge cases cần xử lý
<nếu có>

## 6. Self-check & report
- Chạy các test/command sau: <list>
- Sau khi xong, gửi file `step-<n>-<slug>-selfcheck.md` qua Telegram bằng `notify_telegram.sh`.
- File phải theo format ở WORKFLOW.md section 5.3.
```

---

## 10. Cuts vs keeps — bảng quyết định cho scale <10 user

| Hạng mục | Quyết định | Ghi chú |
| --- | --- | --- |
| HTTPS + nginx + LetsEncrypt | ❌ Cắt | HTTP localhost / Tailscale / Cloudflare Tunnel |
| NSSM Windows service | ✅ Giữ MT5 agent | Server chạy uvicorn tay được |
| Rate limit `/auth/login` | ❌ Cắt | Single admin, không public |
| JWT 24h | ✅ Giữ | Login lại mỗi ngày OK |
| JWT refresh logic | ❌ Cắt | Đơn giản hóa |
| bcrypt password hash | ✅ Giữ | Effort thấp, security cơ bản |
| Backup Redis BGSAVE auto | ⚠️ Đơn giản hóa | Cron `redis-cli BGSAVE` + copy `dump.rdb` thủ công sang Google Drive |
| Structured JSON logging | ❌ Cắt | `logging.basicConfig` đủ |
| Health endpoint chi tiết | ⚠️ Đơn giản hóa | `/health` → `{"ok": true}` |
| Multi-environment config | ❌ Cắt | 1 `.env` duy nhất |
| OAuth state CSRF (HMAC) | ✅ Giữ | cTrader OAuth bắt buộc |
| Unit test coverage cao | ⚠️ Có chọn lọc | Chỉ test critical: volume calc, P&L conversion, cascade close trigger. UI/API endpoint không cần unit test. |
| SQL DB | ❌ Cắt | Redis-only |
| Multi-user / multi-tenant | ❌ Cắt | Single admin |
| Auto-trading / strategy | ❌ Cắt | Tool thủ công |
| OCO / trailing / partial close | ❌ Cắt phase 1 | Có thể thêm sau |
| Broker khác cTrader/MT5 | ❌ Cắt | Adapter layer mở sẵn để extend sau |

---

## 11. Compliance & legal disclaimer

- Tool chỉ hỗ trợ kỹ thuật. KHÔNG tư vấn legality của hedge FTMO.
- CEO chịu trách nhiệm đọc FTMO ToS và quyết định có dùng hay không.
- CTO + Claude Code KHÔNG phán xét chiến lược, chỉ build đúng yêu cầu kỹ thuật.

---

## 12. Document version

- **v1.0** — 2026-05-04 — CEO + CTO chốt khởi đầu rebuild.
- Mọi update file này sau đây phải có entry mới ở mục này, ghi rõ lý do.
