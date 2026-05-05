# CTO Chat Handoff Template

Khi CEO mở phiên CTO chat mới (vì phiên cũ đã chạm giới hạn độ dài hoặc context bị suy giảm), paste template dưới đây vào tin nhắn đầu tiên:

---

[Bàn giao context]
Project: FTMO Hedge Tool v3. Bạn là CTO. Tuân thủ project instructions đã có sẵn trong project knowledge.

Thứ tự đọc bắt buộc:
1. `docs/MASTER_PLAN_v2.md` — kế hoạch tổng thể, 5 phase, breakdown từng step.
2. `docs/PROJECT_STATE.md` — snapshot trạng thái hiện tại. Đọc kỹ — nó nói chính xác chúng ta đang ở đâu.
3. `docs/DECISIONS.md` — sổ tích lũy các quyết định kiến trúc và vận hành.
4. `docs/PHASE_<N>_REPORT.md` — báo cáo hoàn thành phase gần nhất (nếu có).

Trọng tâm hiện tại: <CEO điền 1 dòng — vd "review verdict step 2.3" hoặc "viết prompt cho step 2.5">

Hành động cuối của CTO trước: <CEO điền 1 dòng — vd "approve step 2.4 PASS, đưa hướng dẫn merge">

---

CEO chỉ điền 2 dòng (trọng tâm hiện tại + hành động cuối). Mọi thứ còn lại CTO tự derive từ project files.

## Khi `PROJECT_STATE.md` là đủ
Cho các tiến trình routine (viết prompt step kế tiếp, review verdict), `PROJECT_STATE.md` một mình đã đủ context.

## Khi nào đọc thêm file khác
- Bắt đầu phase mới: đọc Section của phase đó trong `MASTER_PLAN_v2.md` + `PHASE_N_REPORT.md` của phase trước.
- Có debate về kiến trúc: đọc `DECISIONS.md` để tìm reasoning đã có.
- Implement feature cụ thể: đọc các doc chuyên đề (vd `09-frontend.md`, `12-business-rules.md`) khi đã được tạo.

## Lưu ý cho CEO
- Không cần upload toàn bộ docs vào project knowledge mỗi lần — chỉ cần `MASTER_PLAN_v2.md`, `PROJECT_STATE.md`, `DECISIONS.md`, và `PHASE_N_REPORT.md` mới nhất.
- Sau mỗi step PASS hoặc REJECT, CTO phải cập nhật `PROJECT_STATE.md` (Vị trí hiện tại, Active context, Pending items) và push để CEO upload lại bản mới vào project knowledge.
