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

## 3. Metrics

### Sync

Event rate, pending events, manifest drift, upload bytes, conflict count, cursor lag và integrity failures.

### Ingestion

Workflow duration, stage failures, chunks/source, OCR/STT duration, provider calls, token usage, retry count và projection lag.

### Retrieval

Request rate, p50/p95 latency theo stage, candidate counts, degraded modes, zero-result rate, cache hit, reranker latency và policy-denied count.

### Storage

PostgreSQL size/connections, Qdrant points/index bytes/RAM, Neo4j store/heap, Redis memory, disk usage và object-store request/bytes/cost theo low-cardinality backend kind.

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

## 7. Retention

```text
Prometheus   30 days
Loki         14 days
Tempo         7 days
Sentry       30 days or provider plan limit
Docker logs   3 days, size-rotated
```

Cardinality, ingestion rate và disk watermark có dashboards riêng. Retention được giảm trước khi tăng disk nếu dữ liệu cũ không còn operational value.

## 8. Dashboards

- System overview và host capacity.
- Sync health và canonical integrity.
- Ingestion/workflow pipeline.
- Retrieval quality/latency/provider cost.
- Qdrant/Neo4j projection health.
- Backup and recovery freshness.
- Alert delivery health.

Mỗi panel có owner, unit, source metric và link runbook.
