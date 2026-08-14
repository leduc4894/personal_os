# Temporal Workflows

## 1. Nguyên tắc

Temporal dùng cho công việc nhiều bước cần retry, resume và audit. Workflow code deterministic; network/database/provider calls chỉ nằm trong activities. Raw content và vectors không đi qua workflow history.

## 2. Workflow catalog

### SourceIngestionWorkflow

```text
load canonical references
→ evaluate policy
→ extract/parse artifact
→ normalize metadata
→ build chunks
→ encode dense/sparse
→ update Qdrant
→ update Neo4j
→ verify and checkpoint
```

### WorkspaceReconcileWorkflow

So sánh plugin manifest, PostgreSQL current versions, Cloudflare R2 inventory và projection checkpoints; tạo repair actions có giới hạn.

### ProjectionRebuildWorkflow

Provision generation, snapshot checkpoint, project batches, catch up, verify và đưa deployment về `ready`. Activation là command riêng có authorization.

### ProjectionRepairWorkflow

Re-derive expected points/edges từ canonical state và sửa drift trong exact active generation với fencing.

### ActionApprovalWorkflow

Tạo proposal, chờ signal approve/reject/expire, revalidate policy/base version rồi commit approved action.

### GarbageCollectionWorkflow

Đánh dấu candidates, chờ grace period, recheck references/holds rồi xóa exact objects/deployments.

### BackupVerificationWorkflow

Tạo manifest, verify checksums và định kỳ restore vào disposable environment.

## 3. Idempotency

- Workflow ID derive từ business identity và operation generation.
- Activity write dùng idempotency key/unique constraint.
- Qdrant point ID và Neo4j node ID deterministic.
- Repeated upload completion không tạo version mới.
- Replay activity sau timeout phải trả lại committed outcome khi đã thành công.

Source publication ghi một Qdrant và một Neo4j projection intent, nhưng cả hai derive cùng workflow ID `source-ingestion/{workspace_id}/{event_id}`. Dispatcher dùng lease token làm fence, `USE_EXISTING` cho workflow đang chạy và reject duplicate run cho workflow đã đóng; retry sau lost acknowledgement không tạo execution thứ hai.

## 4. Retry ownership

Temporal là retry owner cho external calls. Provider SDK retries bị tắt hoặc giới hạn một attempt để tránh nhân retry. Activity policies phân loại:

| Class | Ví dụ | Retry |
|---|---|---|
| Transient | timeout, 429, temporary unavailable | exponential, bounded |
| Conflict | stale base, fencing mismatch | non-retryable; cần replan |
| Policy | denied content/provider | terminal |
| Invalid | schema/hash mismatch | terminal + alert |
| Dependency outage | Qdrant/object store/Neo4j down | bounded retry rồi pending repair; không tự chuyển object-store backend |

Mỗi call có timeout riêng; workflow có overall deadline và cancellation behavior.

## 5. Large workload handling

- Batch references, không batch raw bodies trong history.
- Continue-as-new theo source/batch count để bound history.
- Heartbeat cho OCR, transcription và large indexing activity.
- Cancellation cleanup chỉ xóa unreferenced staging resources.
- Concurrency/rate limit riêng theo provider và projection target.

## 6. Fencing

Projection activity nhận capability do server tạo:

```text
workspace_id
deployment_id
target_name
generation
contract_hash
fencing_token
expires_at
allowed_operations
```

Stale workflow không được ghi vào target mới hoặc activate route.

## 7. Signals and queries

- Signals: approve, reject, cancel, pause admission.
- Queries: stage, counts, last checkpoint, retry summary và sanitized failure code.
- Progress được Web App nhận qua SSE từ application service; UI không kết nối Temporal trực tiếp.

## 8. Recovery

Worker restart không mất workflow. Temporal outage không làm mất canonical intent trong PostgreSQL. Dispatcher định kỳ quét undispatched intents bằng locked batches. Không dựa vào Temporal history làm business database dài hạn.

Dispatcher claim bằng `FOR UPDATE SKIP LOCKED`, commit lease trước khi gọi Temporal và chỉ acknowledge bằng exact lease token. Các boundary cố định của một dispatch cycle:

```text
claim batch                          50 intents
concurrent Temporal starts            8
lease                                 60 seconds
Temporal start/describe timeout       10 seconds
retry backoff                         min(300, 2 ** prior_attempt_count) seconds
```

Claim chọn pending rows theo `(available_at, created_at, projection_intent_id)`, gán status `leased`, lease token UUIDv7 và database-time expiry. Attempt count chỉ tăng khi outcome đã biết hoặc lease hết hạn. Lease hết hạn trả intent về pending, tăng attempt, ghi `projection_dispatch_lease_expired` và áp dụng backoff; stale token không bao giờ ghi đè transition của dispatcher khác. Temporal outage giữ intent ở pending với backoff có cap, không phụ thuộc attempt count. Workflow input là closed contract `source_ingestion_reference/v1` chỉ chứa contract tag và bốn UUID (workspace, event, source, source version); raw content, title, object key, hash và vector không bao giờ vào Temporal history.

Start có thể được Temporal chấp nhận trước khi ingestion worker được triển khai; workflow task chờ trên task queue `source-ingestion` cho đến Phase 3. Phase 1 chỉ queue các workflow start này — worker Phase 1 chưa register implementation của `SourceIngestionWorkflow`; registration đến với deliverable Phase 3.

Readiness của API/MCP kiểm tra PostgreSQL connectivity và schema head; readiness của worker thêm kiểm tra Temporal namespace. Backlog age chỉ degrade readiness, không degrade liveness, và liveness không thực hiện network call.

## 9. Tests

- Deterministic replay.
- Time-skipping retry/timeout tests.
- Activity idempotency và crash-after-commit.
- Cancellation/continue-as-new.
- Fencing rejection.
- Live Temporal + PostgreSQL + Cloudflare R2 test bucket + Qdrant + Neo4j workflow.
