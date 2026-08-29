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

Lifecycle event giữ nguyên `source_id` và `current_version_id`: rename/move chỉ
đóng locator cũ rồi mở locator mới, delete mở tombstone giữ version hiện tại,
và restore đóng đúng tombstone rồi mở locator do người dùng chọn. Plugin lưu
event lifecycle bất biến trong journal, gửi theo thứ tự của từng source và chỉ
xóa trạng thái pending sau receipt canonical. Automatic restore chỉ hợp lệ khi
bytes tái xuất hiện khớp fingerprint đã commit; explicit restore là command có
xác nhận. Repair của `reconcile_required` đã thuộc Child 6: lệnh `Repair sync`
và manifest reconciliation dọn cờ này qua `markReconcileComplete`, không bao giờ
tự clear sau một pass thường.

## 4. Manifest reconciliation

Plugin gửi manifest phân trang gồm stable ID, path, hash, size và local version. Backend trả action plan: `upload`, `download`, `apply_tombstone`, `conflict`, `no_change`, `excluded`.

Reconciliation chạy sau onboarding, cursor gap, theo lịch định kỳ và khi người dùng repair. Filesystem watcher chỉ giảm latency; manifest reconciliation mới là correctness mechanism.

Child 6 hoàn thiện mechanism này thành manifest run checkpoint-bound: mỗi run
đóng băng một event checkpoint và một active policy revision tại PostgreSQL
(`device_cursors`, `manifest_runs`, `manifest_pages`,
`manifest_entry_resolutions`, `manifest_actions`; migration heads
`20260826_01` + `20260826_02`), page/digest replay là no-op khi khớp chính xác,
policy advance giữa chừng invalidates run bằng `device_manifest_policy_advanced`
và một run mới thay thế. Action `download` carry checkpoint locator trên wire
actions (hydrate lúc đọc; không có locator text nào persist trong các bảng
manifest), và chỉ download canonical-only mới thiếu `local_entry_id` — download
catch-up theo entry vẫn echo entry của nó. Cursor pull chạy mỗi 30 giây
foreground, full reconciliation mỗi 6 giờ active tích lũy; cursor local chỉ
tiến khi kết quả terminal-safe đã durable trong generation commit, và transaction
manifest-completion là ngoại lệ duy nhất cho cursor server. Runbook vận hành:
`docs/operations/device-cursor-manifest-reconciliation.md`.

Remote apply là state machine crash-safe (temp sibling verified, atomic replace,
verify final hash); remote delete đưa file proven-unchanged vào Obsidian local
trash qua `Vault.trash(file, false)` — không có hard-delete fallback. Sau khi
mất SQLite journal, identity chỉ được chứng minh theo thứ tự: current locator
khớp, historical locator duy nhất cộng exact fingerprint, hoặc open tombstone
cộng retained fingerprint; hash-only không bao giờ bind identity, và plugin
không bao giờ tự mint source ID.

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

- File nhỏ (≤16 MiB): backend-mediated hoặc presigned single-part upload.
- File lớn (>16 MiB đến 100 MiB): preflight trả `multipart_upload`; plugin
  mở một server-owned multipart session (part 8 MiB, tối đa 13 part, part
  URL 10 phút, session 24 giờ), resume qua status-first, và completion luôn
  full-object SHA-256/size/media-type verification trước khi publication.
- Quá 100 MiB: capture chặn born-terminal `blocked_size` ở plugin.
- Capability scope theo workspace, device, event, part, size và expiry; URL
  chỉ trúng staging phi-canonical của session, không trúng canonical object.
- Partial upload có expiry cleanup workflow; mọi cleanup dùng exact staging
  key của session, không có list/wildcard/prefix-based deletion.
- Policy được recheck ở tạo session, issue part URL, completion và
  publication; part URL đã cấp trước khi policy đổi chỉ ghi staging tạm,
  không thể publish denied content.
- Operator runbook: `docs/operations/resumable-multipart-upload.md`.

## 9. Mobile constraints

- Không giả định background task chạy lâu.
- Batch nhỏ, resumable cursor và multipart.
- Không block editor khi backend offline.
- Queue local có bounded size và hiển thị degraded state.
- Local content luôn được bảo toàn khi sync lỗi.

## 10. Acceptance criteria

- Replay event không tạo duplicate version.
- Rename giữ nguyên source ID.
- Move/delete/restore giữ nguyên current version; delete/restore giữ tombstone lineage.
- Desktop WDIO và ma trận thiết bị Mobile thật đều phải PASS trước khi đóng Child 5.
- Missed watcher event được reconciliation phát hiện.
- Excluded folder không được upload/index ngoài policy.
- Concurrent edit không silent overwrite.
- Remote web edit được plugin apply nguyên tử.
- Binary upload corrupt không được publish làm current.
