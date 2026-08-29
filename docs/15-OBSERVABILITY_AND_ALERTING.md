# Observability and Alerting

## 1. Stack

```text
Exceptions/crashes   Sentry Cloud, errors-only
Metrics              Prometheus
Dashboards           Grafana
Logs                  Loki
Traces                OpenTelemetry → Alloy → Tempo
Alerts                Prometheus rules → Alertmanager
Notifications         Telegram + email
```

Sentry không bật performance tracing, profiles hoặc session replay; Tempo là tracing authority.

## 2. Structured logging

JSON log fields chuẩn:

```text
timestamp
level
service
environment
request_id
trace_id
workflow_id
activity
workspace_id_hash
source_id
operation
result_code
duration_ms
```

Không log raw body/query/excerpt. Error stack được gửi Sentry sau scrubber.

HTTP access observation dùng closed event set
`api_request_completed` / `api_request_rejected` / `api_request_failed`
(INFO/WARNING/ERROR theo status <400/<500/else) với đúng các field
`http_method`, `route` (route template hoặc hằng `unmatched`), `status_code`,
`duration_ms` cùng correlation fields. Response bắt đầu dưới 400 nhưng không
bao giờ gửi body chunk cuối (ví dụ download chết giữa stream hoặc client ngắt
giữa body) được phân loại `api_request_failed` kèm reason token đóng (tùy
chọn) `response_body_incomplete` — giữ nguyên status đã gửi. Raw path, query,
headers, cookies, body, response data và exception text không bao giờ vào
access observation; correlation value không hợp lệ chỉ được ghi bằng
rejection event với reason token, không echo giá trị bị từ chối.

Multipart upload (child 7) thêm structured event đóng
`multipart_upload_rejected` (stage + `error_code` thuộc khối `multipart_*`,
survive restart qua rotating log) cho mọi rejection path kể cả cleanup failure
của session đã committed — lý do đóng luôn đọc được, không nuốt im lặng.

## 3. Metrics

### Sync

Event rate, pending events, manifest drift, upload bytes, conflict count, cursor lag và integrity failures.

### Ingestion

Workflow duration, stage failures, chunks/source, OCR/STT duration, provider calls, token usage, retry count và projection lag.

### Retrieval

Request rate, p50/p95 latency theo stage, candidate counts, degraded modes, zero-result rate, cache hit, reranker latency và policy-denied count.

### Storage

PostgreSQL size/connections, Qdrant points/index bytes/RAM, Neo4j store/heap, Redis memory, disk usage và object-store request/bytes/cost theo low-cardinality backend kind.

### Multipart upload

`multipart_session_total`/`_duration_seconds` (outcome), `multipart_completion_total`/`_duration_seconds` (outcome), `multipart_cleanup_total` (outcome) và `multipart_rejection_total` (stage, error_code) — label universe đóng đúng năm label `outcome`/`state`/`platform_class`/`stage`/`error_code`, validate lúc import; không session ID, staging key, ETag, request ID hay path nào làm label.

Không dùng source ID, path, query hoặc tag làm Prometheus label có cardinality cao.

## 4. Tracing and sampling

Service gửi OTLP tới Alloy. Alloy batch, redact attributes, tạo span metrics và tail-sample trước Tempo.

Sau benchmark, policy mặc định:

```text
errors and failed workflows               100%
slow traces                               100%
security/admin/write/rebuild operations   100%
normal application traces                  10%
health/readiness/scrape traces               0%
```

Ngưỡng ban đầu: API/MCP 2 giây, retrieval 3 giây, Temporal activity 10 giây. Trong benchmark/canary ngắn hạn giữ 100% để đo; sau đó áp policy trên và điều chỉnh theo traces/second, Alloy memory và Tempo disk growth.

Tail sampling yêu cầu mọi span cùng trace tới cùng Alloy instance. Trace context được truyền qua HTTP, MCP request boundary và Temporal headers.

## 5. Alertmanager routing

| Severity | Channel | Timing |
|---|---|---|
| `critical` | Telegram + email | group wait 30s, repeat 30m |
| `warning` | Telegram | group wait 5m, repeat 4h |
| `info` | Grafana only | không push |

Gửi resolved notification. Bot token, chat ID và SMTP password đọc từ secret files. Alertmanager config được validate bằng `amtool` trong CI.

Host A giám sát heartbeat của Host B và Host B giám sát Host A. Tuy vậy cả hai host không thể tự báo khi cùng mất mạng/điện; nếu cần bảo đảm này phải thêm một external dead-man check.

## 6. Critical alerts

- Canonical object hash mismatch/missing current object.
- PostgreSQL unavailable hoặc backup stale.
- Disk > 85% hoặc predicted exhaustion.
- Projection policy leakage/drift vượt zero tolerance.
- Active route target missing/contract mismatch.
- Workflow backlog/lag vượt SLO.
- Authentication anomaly hoặc repeated denied admin action.
- No telemetry heartbeat từ một host.

Warnings gồm elevated latency/error rate, provider throttling, cache degradation, reconcile drift và backup verification age.

## 7. Device diagnostics (plugin)

Tầng thiết bị (Obsidian plugin) là nơi người dùng gặp lỗi trước tiên và không
có service nào chạy thường trực; observability của nó là pattern closed-token
tại chỗ, không phụ thuộc stack Phần 1:

- **Durable sync diagnostics trail (v2)**: sidecar ghi contract
  `obsidian_sync_diagnostics_trail/v2` — ring 128 entry `{kind, atEpochMs, tokens}`
  chỉ chứa nhãn đóng (`QueuePassOutcome`, `JournalSafeErrorLabel`,
  `JournalStoreErrorReason`, sync failure kinds, lifecycle outcomes, server
  envelope error codes, opaque `request_id`), persist qua restart qua sidecar
  `sync-diagnostics-trail.json` qua vault adapter; hỏng thì reset + ghi
  `trail_reset`. Loader chấp nhận sidecar v1 và losslessly rewrite các entry
  đã biết lên v2; foreign token vẫn reset qua `trail_reset`. Child 6 thêm các
  kind đóng `credential_failure` (`access_missing` / `refresh_failed`),
  `cursor_failure` (pull / acknowledge), `apply_failure` (prepare / download /
  verify_temp / vault_mutation / verify_final / local_commit / recovery /
  trash), `reconcile_failure` (start / page / finalize / actions / complete)
  và `composition_read_failure` (status_read / note_status_read /
  retry_schedule_read / sync_status_read — bị loại khỏi derived stop reasons
  vì các read một-lần-mỗi-session này không dừng sync). `wire_failure` giờ
  chỉ nghĩa là một HTTP attempt thực sự chạm transport và thất bại; thiếu
  credential hay refresh fail trước contact ghi `credential_failure`. Child 7
  thêm kind đóng `multipart_failure`: đúng một stage token
  (`multipart_resume`/`multipart_verify`/`multipart_cleanup`) cộng reason
  token `multipart_*` của thất bại, cho mọi catch của multipart runner
  (best-effort abort, best-effort progress clear và mọi thrown failure).
  Trail
  observe-only: không đổi semantics sync, append fire-and-forget, không bao
  giờ chặn pass. Hành vi cadence đã ghi nhận (Task 14): sau một thời gian
  suspend dài, catch-up burst ghi nợ từng stale tick 30 giây vào accumulator
  reconcile sáu giờ — tệ nhất chỉ tạo một cơ hội periodic-reconcile giả rồi
  no-op khi không có gì nợ (xem runbook device-sync).
- **Wire correlation**: mọi wire failure mang `request_id` từ response envelope
  (UUID-gated) để join với access observation của API; nâng cấp tự nhiên là đọc
  header `traceparent` (W3C) API đã trả.
- **Self-check command**: probe tuần tự (trail persist, credential presence,
  origin reachability qua `/api/health/live`) với verdict đóng; không đụng
  sync state.
- **Export command**: khối sanitized (status, blockers, counts, trail tail)
  chỉ gồm nhãn đóng + ISO timestamp — clipboard/modal, không path/hostname/secret.
- **Admin sync-rejection route**: `GET /api/admin/sync/rejections` trả counters
  + ring 50 rejection gần nhất (`error_code`, `at_epoch_ms`, `operation`) sau
  admin auth.
- **Luật nền**: mọi closed error path mới phải surface reason token tới
  trail/settings — không nuốt im lặng (khởi nguồn: bug park 2 ngày ẩn sau
  `journal_mutation_failed` bị catch bỏ qua).

Runbook vận hành: `docs/operations/sync-error-tracing.md`. Toàn bộ mặt
cursor/apply/reconcile của Child 6 (cách đọc các kind mới, cursor lag,
repair state, reason tokens) có runbook riêng tại
`docs/operations/device-cursor-manifest-reconciliation.md`. API structured
diagnostics (`api_request_failed` gồm cả 5xx exception) đã ghi kèm
server-generated `request_id` của request correlation middleware.

## 8. Retention

```text
Prometheus   30 days
Loki         14 days
Tempo         7 days
Sentry       30 days or provider plan limit
Docker logs   3 days, size-rotated
```

Cardinality, ingestion rate và disk watermark có dashboards riêng. Retention được giảm trước khi tăng disk nếu dữ liệu cũ không còn operational value.

## 9. Dashboards

- System overview và host capacity.
- Sync health và canonical integrity.
- Ingestion/workflow pipeline.
- Retrieval quality/latency/provider cost.
- Qdrant/Neo4j projection health.
- Backup and recovery freshness.
- Alert delivery health.

Mỗi panel có owner, unit, source metric và link runbook.
