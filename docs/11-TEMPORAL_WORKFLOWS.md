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

## 9. Tests

- Deterministic replay.
- Time-skipping retry/timeout tests.
- Activity idempotency và crash-after-commit.
- Cancellation/continue-as-new.
- Fencing rejection.
- Live Temporal + PostgreSQL + Cloudflare R2 test bucket + Qdrant + Neo4j workflow.
