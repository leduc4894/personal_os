# Deployment, Backup and Recovery

## 1. Primary topology

### Host A — Personal OS

```text
6 vCPU
16 GB RAM
200 GB NVMe
No GPU required
```

Chạy reverse proxy, API, MCP, worker, Web App, PostgreSQL, Qdrant, Neo4j, Temporal, Redis và Alloy agent. Original files/attachments không lưu lâu dài trên disk; chúng nằm trong private Cloudflare R2 production bucket.

### Host B — Observability

```text
4 vCPU
8 GB RAM
120 GB NVMe
No GPU
```

Chạy Prometheus, Grafana, Loki, Tempo, Alertmanager, Alloy gateway và exporters. Sentry dùng cloud errors-only để tránh full self-hosted bundle quá nặng cho một người dùng.

Cloudflare R2 là canonical object store duy nhất. Production và test/CI dùng bucket cùng credentials tách biệt. R2 outage là dependency incident: application dùng bounded retry rồi fail closed và chờ provider phục hồi; không có fallback host, backend cutover, dual-write hoặc request-time failover.

## 2. Capacity allocation

Host A rough guardrails:

```text
Qdrant                  60–80 GB disk, 4–6 GB RAM
PostgreSQL              20–30 GB disk, 2–3 GB RAM
Neo4j                   10–20 GB disk, 2–3 GB RAM
Temporal + Redis         5–10 GB disk, 1–2 GB RAM
Apps/Docker/temp/cache  phần còn lại với 20% free-space reserve
```

Host B storage được bound bằng retention: Prometheus 20–25 GB, Loki 25–30 GB, Tempo 15–20 GB; còn lại cho OS/images/headroom.

Không hard-allocate toàn bộ disk. Alerts ở 70%, warning 80%, critical 90%; mọi database phải giữ headroom cho compaction/migration.

## 3. Scaling triggers

Nâng Host A RAM từ 16 lên 32 GB trước khi thêm CPU nếu Qdrant resident set, Neo4j page cache và PostgreSQL cùng tạo sustained memory pressure. Nâng disk khi forecast còn dưới 60 ngày. Chỉ thêm GPU khi local OCR/STT throughput thực sự là bottleneck.

Host B tăng disk hoặc giảm retention khi growth forecast vượt 30 ngày; tăng RAM khi Alloy tail-sampling buffer/Tempo/Loki gây sustained pressure, không theo cảm giác.

## 4. Backup hierarchy

### Canonical object store

- Cloudflare R2 production bucket là canonical bytes authority duy nhất.
- Lifecycle/retention và delete protection cho canonical objects.
- Inventory manifest gồm object key, hash, size và last modified.
- Backup/restore evidence phải xử lý rõ vendor/account concentration risk; một bản copy quan trọng chỉ nằm trong cùng account R2 không được xem là failure-domain độc lập.

### PostgreSQL

- Encrypted daily logical backup lên backup object store đã phê duyệt; credential và retention tách khỏi application runtime.
- WAL/PITR nếu chấp nhận vận hành thêm; nếu không, ghi rõ RPO 24 giờ.
- Backup manifest bind database schema revision và Cloudflare R2 inventory checkpoint.

### Qdrant and Neo4j

Correctness recovery là rebuild từ canonical state. Snapshot projection chỉ tối ưu RTO, không thay rebuild drill. Snapshot upload lên backup object store rồi xóa local sau checksum verification.

### Configuration

Backup encrypted secrets/config inventory, Docker manifests, dashboards, alert rules và registry export. Không lưu plaintext secrets trong Git.

## 5. Recovery objectives

Initial personal-use targets:

```text
Canonical data RPO    24 hours without PITR; lower when PITR enabled
Canonical service RTO 4 hours
Projection RPO        zero relative to restored canonical checkpoint
Projection RTO        dependent on corpus/provider rate; benchmark required
```

## 6. Recovery order

1. Provision clean network/hosts and exact pinned software.
2. Restore PostgreSQL and verify schema.
3. Verify/restore Cloudflare R2 canonical objects against manifest.
4. Start Temporal/Redis and application in maintenance mode.
5. Rebuild Qdrant and Neo4j from canonical checkpoint.
6. Run integrity, policy and golden retrieval gates.
7. Enable read traffic, then writes/sync.
8. Record restore evidence and unresolved gaps.

Projection snapshots không được activate nếu không match restored PostgreSQL checkpoint và contract hash.

## 7. Cloudflare R2 outage and recovery

1. Xác định incident là network, credential, bucket policy, account hoặc provider outage mà không log secret/path.
2. Giữ mọi write chưa verify ở trạng thái chưa publish; PostgreSQL không trỏ current tới object thiếu.
3. Temporal thực hiện bounded retry cho lỗi retryable, sau đó giữ workflow ở trạng thái resumable/degraded.
4. Các operation không cần object bytes có thể tiếp tục; operation đọc/ghi bytes trả structured dependency unavailable.
5. Khôi phục đúng R2 endpoint, bucket và bucket-scoped credential; không tự tạo bucket hoặc nới quyền.
6. Chạy authenticated read/write/integrity smoke trên exact environment.
7. Resume workflow backlog và reconcile PostgreSQL references với R2 inventory.
8. Ghi incident/recovery evidence và unresolved integrity gaps.

Không có request-time automatic failover hoặc provider khác để thử. Nếu R2 validation fail, các operation cần object bytes tiếp tục fail closed.

## 8. Deployment rules

- Pin image digests/versions.
- PostgreSQL application state và Temporal persistence có thể dùng cùng server instance nhưng phải dùng database, user, migration và backup scope riêng.
- Migration job chạy một lần trước app rollout.
- Health checks phân biệt liveness, readiness và dependency status.
- App processes stateless; rolling restart không mất canonical writes.
- Destructive operation chỉ exact target, có preview/confirmation/audit.
- Rollback application chỉ khi database schema tương thích; projection rollback chỉ tới verified generation.

## 9. Backup verification

Ít nhất hàng tháng restore PostgreSQL + sampled/all R2 objects vào disposable environment và rebuild projections. Backup không được xem là tốt chỉ vì upload thành công. Drill phải kiểm tra exact inventory/hash/size, bucket/credential isolation và khả năng resume workflow sau R2 outage.
