# PostgreSQL Data Model

## 1. Vai trò

PostgreSQL là canonical application state và correctness authority cho source identity, versions, policy, projection route, workflow intent, audit và approvals. Content bytes lớn nằm trong private Cloudflare R2 production bucket.

## 2. Core tables

| Nhóm | Tables |
|---|---|
| Identity | `users`, `workspaces`, `devices`, `device_tokens`, `web_sessions` |
| Sources | `sources`, `source_versions`, `content_objects`, `source_locators`, `source_tombstones`, `source_conflicts` |
| Sync | `sync_events`, `device_cursors`, `manifest_runs`, `manifest_pages`, `manifest_entry_resolutions`, `manifest_actions`, `multipart_uploads`, `multipart_parts` |
| Metadata | `source_metadata`, `metadata_values`, `property_definitions`, `schema_revisions` |
| Policy | `source_policies`, `policy_rules`, `policy_evaluations` |
| Projection | `projection_deployments`, `projection_routes`, `projection_checkpoints`, `projection_manifests`, `projection_failures`, `embedding_cache` |
| Safety/product | `action_proposals`, `action_approvals`, `audit_events`, `query_logs`, `query_feedback`, `provider_usage` |

`sources.current_version_id` là authoritative pointer. `content_objects.content_hash` unique và reference-counted. Flexible values dùng typed columns với constraint chỉ một representation được set.

## 3. Key constraints

- Mọi business table có `workspace_id` và foreign key.
- Current version thuộc đúng source.
- Content hash là lowercase SHA-256, byte size không âm.
- Event ID và idempotency key unique.
- Version number tăng đơn điệu theo source.
- Chỉ một active projection route cho mỗi projection kind.
- Approved action bind exact proposal hash và base version.
- Tombstone không làm mất audit lineage.

## 4. JSONB policy

JSONB dùng cho raw source metadata, provider-neutral structured output và bounded diagnostic detail. Không dùng JSONB thay stable foreign key, current pointer, query-critical lifecycle state, typed range value hoặc authorization scope. Mọi JSONB có Pydantic contract và schema revision.

Exclusion policy dùng immutable relational revisions và normalized typed rule operands; active policy pointer cùng `policy_evaluations` là trục riêng với `sources.sync_state`. Một source `active`, `stored_not_indexed` hoặc `deleted` vẫn có effective decision `allowed` hay `denied`; không thêm `excluded` vào lifecycle enum. Signed snapshot canonical bytes/signature được persist cùng revision để read không regenerate hoặc resign.

## 5. Transaction boundaries

### Publish source version

1. Preflight idempotency key/event ID để trả operation đã commit trước khi đọc lại object bytes.
2. Trong transaction, lock idempotency identity rồi lock source identity/row theo thứ tự cố định.
3. Tra lại idempotency và verify expected base version.
4. Reuse/insert content-object metadata chỉ từ verified receipt.
5. Nếu bytes thay đổi, insert immutable version và update current pointer; nếu bytes không đổi thì giữ version/pointer.
6. Insert sync event, audit và hai projection intent cho changed content; no-change không tạo intent.
7. Commit một lần.

Hai lớp khóa là transaction-level advisory lock theo thứ tự cố định: `(workspace_id, idempotency_key)` trước (namespace `0x53564349`), rồi source identity (namespace `0x53564353`) và `SELECT ... FOR UPDATE` source row hiện có. Session advisory lock bị cấm; lock luôn được giải phóng bởi commit, rollback, cancellation hoặc mất kết nối. Transaction không thực hiện bất kỳ call R2 hay Temporal nào.

Event update có `base_version_id = committed_version_id` là persisted marker cho `no_change`. Exact replay hydrate kết quả từ event/version/object hiện có; không tạo thêm audit hay canonical row.

Transaction retry bị chặn ở tối đa 3 attempts, chỉ áp dụng cho deadlock, serialization failure và bounded lock contention, với cancellable jitter 50–250 ms; business conflict, identity misuse, receipt failure, metadata conflict và invariant failure không retry. Nếu acknowledgment của commit không chắc chắn, adapter bỏ connection hiện tại, mở connection bounded mới và tra cứu key/event/fingerprint; chỉ retry sau khi tra cứu chứng minh vắng mặt. Khi PostgreSQL không khả dụng, lỗi là retryable `source_commit_outcome_unknown`, không bao giờ là rollback giả định.

Object upload xảy ra trước transaction; orphan object được GC sau grace period nếu transaction không commit.

### Commit source lifecycle event

Lifecycle commit khóa idempotency identity, source, các locator theo thứ tự
canonical và tombstone liên quan trong một transaction. `source_locators` là
lịch sử locator có khoảng mở/đóng; tại một thời điểm mỗi source chỉ có một
locator mở và mỗi workspace chỉ có một owner cho locator mở.
`source_tombstones` giữ retained version cùng delete/restore lineage. Rename,
move, delete và restore không insert `source_versions` và không đổi
`sources.current_version_id`. Transaction ghi lifecycle event, audit và
projection intents cùng canonical mutation; policy deny/indeterminate vẫn ghi
trạng thái thật nhưng chọn projection delete. Exact replay trả receipt cũ và
không tạo thêm row.

### Device cursor and manifest reconciliation (Child 6)

Migration heads `20260826_01` + `20260826_02` tạo năm bảng:
`device_cursors` (một row watermark mỗi workspace/device; delivered ≥
acknowledged, acknowledge monotonic, fence regression/ack-ahead), và nhóm
manifest tạm thời `manifest_runs` / `manifest_pages` /
`manifest_entry_resolutions` / `manifest_actions` (collecting → planned →
applying → completed | expired | failed; tối đa 100.000 entry/run, page 500
entry, lifetime đúng one hour — một giờ — theo database time). Bản sửa `20260826_02`
nới shape constraint của `manifest_actions`: chỉ canonical-only download mới
được thiếu `local_entry_id` — per-entry catch-up download vẫn echo entry của
nó; mọi action kind khác bắt buộc entry.

Manifest rows là bounded temporary protocol state: cleanup của exact expired
run cascade qua pages/resolutions/actions, nhưng source, version, event,
locator, tombstone và audit lineage không bao giờ cascade qua child này.
Cursor chỉ advance qua exact acknowledgement sau khi local generation của
device durable; transaction của manifest completion là sole exception — cùng
transaction chuyển một exact `applying` run thành `completed` mới được đưa
cursor tới checkpoint của run mà không cần delivered watermark trước đó.
Sync-event compaction không được vượt quá minimum acknowledged cursor của
device còn active; nếu retained history không đáp ứng, pull trả closed gap
outcome chứ không fabricate event. Trên wire, action download mang checkpoint
locator được hydrated at read time từ locator row canonical (workspace-scoped;
hydrate thất bại fail-closed `device_manifest_state_invalid`) — no locator text persists trong bất kỳ bảng manifest nào.

### Activate projection

Trong một transaction: verify deployment state/checkpoint/contract, compare route revision, swap target, increment revision và ghi audit event.

## 6. Indexing strategy

Index theo current source/version lookup, device cursor/event sequence, pending projection intent, active policy, proposal status và audit time range. Không tạo GIN index cho mọi JSONB trước benchmark.

## 7. Retention

- Sync events compact sau khi device cursor và backup checkpoint an toàn.
- Query logs chỉ giữ redacted metadata trong bounded retention.
- Source versions/objects theo retention, tombstone grace và holds.
- Projection manifests/cache có thể xóa và rebuild.
- Audit events quan trọng dùng append-only retention dài hơn.

## 8. Migration discipline

- Alembic baseline upgrade từ empty database.
- Mỗi migration có downgrade hoặc explicit irreversible gate.
- Test upgrade, application smoke và downgrade trên disposable PostgreSQL.
- Baseline chỉ chứa schema cần thiết cho target architecture.
