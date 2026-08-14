# CLAUDE.md

## Mục tiêu

- Xây dựng hệ thống quản lý tri thức cá nhân theo tài liệu canonical trong `docs/`.
- Ưu tiên tính đúng, provenance, citation, privacy, idempotency và khả năng rebuild.
- Chỉ triển khai trong phạm vi contract đã được tài liệu hóa.

## Nguồn sự thật

Đọc theo thứ tự: `docs/00-PRODUCT_VISION_AND_PRD.md`, `01-CANONICAL_ARCHITECTURE.md`,
`02-TECH_STACK.md`, tài liệu domain liên quan, `19-ARCHITECTURE_DECISIONS.md`,
rồi `20-IMPLEMENTATION_PLAN.md`. Khi xung đột, tài liệu có phạm vi cụ thể hơn được ưu tiên;
không tự suy diễn thay đổi kiến trúc.

## Workflow bắt buộc

Với thay đổi không tầm thường, thực hiện theo thứ tự:

1. `plan`: xác định mục tiêu, phạm vi, file ảnh hưởng, rủi ro và cách xác minh.
2. `spec`: mô tả behavior, contract, acceptance criteria và trường hợp lỗi.
3. `task`: chia thành deliverable nhỏ, độc lập, có kiểm thử và điều kiện hoàn thành.
4. Viết test thất bại cho behavior hoặc bug trước khi viết implementation.
5. Viết thay đổi nhỏ nhất để test pass; chạy lint, type check và test liên quan.
6. Cập nhật tài liệu canonical khi contract hoặc quyết định thay đổi.

Lưu tài liệu mới tại `docs/superpowers/plans/`, `docs/superpowers/specs/`, `docs/superpowers/tasks/` khi người dùng yêu cầu
hoặc khi cần artifact lâu dài. Không tạo tài liệu quy trình cho chỉnh sửa hiển nhiên, nhỏ.

## Handoff

- Mỗi plan hoàn thành hoặc bị gián đoạn phải có đúng MỘT file handoff tại
  `docs/handoff/YYYY-MM-DD-<domain-slug>.md`; không chia nhỏ thành nhiều file.
  Chia section bên trong (trạng thái gate, quyết định, item trì hoãn, next actions) thay vì chia file.
- Viết handoff khi: plan hoàn tất (trước khi dọn workspace làm việc), khi BLOCKED,
  hoặc khi session kết thúc mà còn việc dở. Nội dung tối thiểu: commit SHA cuối,
  trạng thái các gate kèm bằng chứng, các quyết định diễn giải spec kèm lý do,
  danh sách item trì hoãn kèm phán quyết, và next actions.
- Handoff là bản chụp một thời điểm: link tới tài liệu living (`docs/operations/`, README)
  thay vì sao chép nội dung của chúng.
- Nếu handoff vượt khoảng 400 dòng, phần vượt thuộc về tài liệu canonical —
  cập nhật tài liệu đó và rút gọn handoff.
- Mọi item trì hoãn phải có đúng MỘT dòng index trong `docs/handoff/BACKLOG.md`
  (ngày, domain, mô tả một dòng, trỏ tới handoff nguồn chứa chi tiết và phán quyết).
  Xóa dòng khi hoàn thành. BACKLOG là index sống; handoff vẫn giữ đầy đủ ngữ cảnh.

## Quy tắc đặt tên

- Tên phải mô tả domain + vai trò, hành vi hoặc kết quả; dùng tiếng Anh nhất quán.
- Không dùng tên dự án làm tiền tố/hậu tố: `personal_os_*`, `personal-os-*`, `*PersonalOS`.
- Ngoại lệ duy nhất là package root `src/personal_os/` đã được kiến trúc canonical quy định.
- Cấm tên thuần thứ tự: `task3`, `phase2`, `wave6`, `step1`, `module4`, `item5`.
- Cấm tên mơ hồ: `temp`, `misc`, `stuff`, `common`, `utils`, `helper`, `new`, `final`, `v2`.
- Số chỉ được làm tiền tố sắp xếp khi có semantic slug: `03-hybrid-retrieval-filtering`.
- Tên plan/spec/task phải tự giải thích được nội dung mà không cần mở file.

Ví dụ tốt:

- `source-version-commit-plan.md`
- `obsidian-conflict-resolution-spec.md`
- `approval-stale-base-check-task.md`
- `compile_metadata_filter()`, `resolve_source_version()`, `citation_repository`

Ví dụ không hợp lệ:

- `task3.md`, `phase-2-spec.md`, `wave6/`, `personal-os-retrieval.ts`
- `processData()`, `handleThing()`, `utils.py`, `final-service-v2.ts`

Quy ước theo ngôn ngữ và artifact:

- Python module, function, variable: `snake_case`; class/protocol: `PascalCase`;
  constant: `UPPER_SNAKE_CASE`.
- TypeScript variable/function: `camelCase`; type/class/component: `PascalCase`;
  constant bất biến dùng chung: `UPPER_SNAKE_CASE`.
- Python package/test folder dùng `snake_case`; web/route/infra folder dùng `kebab-case`.
- File React component dùng `PascalCase.tsx`; module/hook TypeScript dùng `kebab-case.ts`.
- Boolean bắt đầu bằng `is`, `has`, `can`, `should`; collection dùng danh từ số nhiều.
- Đại lượng phải có đơn vị: `timeout_seconds`, `size_bytes`, `latency_ms`.
- ID phải nói rõ thực thể: `source_id`, `version_id`, `workspace_id`; không dùng `id` mơ hồ.
- Function dùng động từ + đối tượng; class/type dùng danh từ; event dùng thì quá khứ.
- Database dùng `snake_case`; migration nêu hành vi, ví dụ `add_source_version_checksum`.
- Test đặt theo behavior, ví dụ `test_rejects_stale_approval_base`.

## Biên kiến trúc

- PostgreSQL giữ canonical state; S3/R2 giữ canonical bytes bất biến.
- Qdrant và Neo4j chỉ là projection có thể xóa và rebuild.
- Temporal điều phối durable workflow; Redis không thay thế durable workflow.
- Domain không import FastAPI, database driver hoặc provider SDK.
- Mọi AI write bên ngoài phải qua proposal, policy, diff và approval.
- Không log raw content, query, vector, token, secret hoặc dữ liệu nhạy cảm.
- External call phải có timeout, bounded retry, error mapping và metrics.

## Chất lượng và thay đổi

- Python phải type đầy đủ, tương thích mypy strict; TypeScript phải strict.
- Thay đổi schema cần Alembic migration và test upgrade/downgrade.
- Thay đổi API cần cập nhật OpenAPI, generated client, contract tests và docs.
- Workflow phải idempotent và có test retry/failure.
- Không thêm dependency production hoặc đổi kiến trúc nếu chưa nêu lý do và tác động.
- Không sửa, xóa hoặc format lại thay đổi không liên quan của người dùng.
- Không tuyên bố hoàn thành nếu chưa chạy command xác minh và đọc kết quả.

## Riêng cho Claude Code

- Khi bắt đầu session, đọc file này và tài liệu canonical liên quan trước khi đề xuất code.
- Duy trì danh sách công việc ngắn cho thay đổi nhiều bước; chỉ có một bước đang thực hiện.
- Tìm kiếm trước khi chỉnh sửa; ưu tiên sửa file hiện có và giữ diff nhỏ, có chủ đích.
- Chỉ hỏi khi thiếu thông tin làm thay đổi đáng kể kết quả; nếu không, chọn giả định an toàn.
- Không dùng chế độ bỏ qua permission trừ khi người dùng yêu cầu rõ ràng.
- Trước khi bàn giao, kiểm tra diff, số dòng hai file hướng dẫn và command liên quan.
