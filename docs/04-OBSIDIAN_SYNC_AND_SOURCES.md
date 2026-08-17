# Obsidian Sync and Source Ingestion

## 1. Vai trò của plugin

Plugin là client đồng bộ chính cho working copy trong Obsidian. Nó onboard device, duy trì local manifest/cursor, upload/download source versions, áp remote edit sau conflict check và hiển thị trạng thái. Plugin không sở hữu canonical version order và không giữ secret trong frontmatter.

## 2. Supported local files

| Loại | Hành vi |
|---|---|
| Markdown | Parse Obsidian structure và index text |
| Plain text/code | Parse theo media type, index text |
| PDF | Upload bytes; native extraction hoặc OCR ở backend |
| Image | Upload bytes; OCR/caption theo policy |
| Audio | Upload bytes; transcription theo policy |
| Khác | Lưu canonical bytes; chỉ index khi có parser được hỗ trợ |

Unsupported parser không làm sync thất bại. Source ở trạng thái `stored_not_indexed` với lý do rõ ràng.

## 3. Event envelope

```text
workspace_id
source_id
device_id
event_id
idempotency_key
event_type
base_version_id
content_hash
byte_size
client_timestamp
```

Event types gồm `create`, `update`, `rename`, `move`, `delete`, `restore`. Rename/move không tạo source identity mới.

## 4. Manifest reconciliation

Plugin gửi manifest phân trang gồm stable ID, path, hash, size và local version. Backend trả action plan: `upload`, `download`, `apply_tombstone`, `conflict`, `no_change`, `excluded`.

Reconciliation chạy sau onboarding, cursor gap, theo lịch định kỳ và khi người dùng repair. Filesystem watcher chỉ giảm latency; manifest reconciliation mới là correctness mechanism.

## 5. Exclusion policy

Exclusion được cấu hình trong Admin Dashboard, không lấy plugin settings làm authority. Phase 2 dùng deny-only rules với default allow cho exact source ID, folder prefix, bounded glob path, extension, media type, maximum size và source type. Property predicate cùng `local_only`/`cloud_ok` thuộc policy mở rộng của Phase 3/4, không được giả lập sớm trong exclusion evaluator.

Backend là policy authority. Plugin nhận signed policy snapshot Ed25519 để tránh upload không cần thiết; snapshot không có TTL nhưng chống rollback theo monotonic revision và authenticated cross-signed keyset. Backend vẫn kiểm tra lại mọi event. Missing evidence hoặc evaluation failure là `indeterminate` và được enforce như deny.

Khi allow chuyển thành deny: dừng ingest, ghi transition, tạo projection tombstones, xóa cache và giữ/GC canonical bytes theo retention.

## 6. Conflict handling

- Conflict xảy ra khi `base_version_id` không còn current.
- Text/Markdown: three-way diff khi có common ancestor.
- Binary: không auto-merge; giữ hai candidate versions.
- Người dùng chọn local, remote hoặc merged result.
- Resolution tạo version mới tham chiếu conflict parents.
- Không dùng last-write-wins âm thầm.

## 7. Web App synchronization

Khi Web App commit version, plugin nhận change qua cursor polling/push hint. Plugin chỉ apply nếu local hash vẫn bằng base hash. Apply dùng temporary file + atomic rename. Operation ID ngăn echo loop nhưng final hash luôn được xác minh.

## 8. Upload strategy

- File nhỏ: backend-mediated hoặc presigned single-part upload.
- File lớn: PostgreSQL multipart session, presigned parts, complete rồi full-object SHA-256 verification.
- Capability scope theo workspace, source, version, part, size và expiry.
- Partial upload có timeout và cleanup workflow.

## 9. Mobile constraints

- Không giả định background task chạy lâu.
- Batch nhỏ, resumable cursor và multipart.
- Không block editor khi backend offline.
- Queue local có bounded size và hiển thị degraded state.
- Local content luôn được bảo toàn khi sync lỗi.

## 10. Acceptance criteria

- Replay event không tạo duplicate version.
- Rename giữ nguyên source ID.
- Missed watcher event được reconciliation phát hiện.
- Excluded folder không được upload/index ngoài policy.
- Concurrent edit không silent overwrite.
- Remote web edit được plugin apply nguyên tử.
- Binary upload corrupt không được publish làm current.
