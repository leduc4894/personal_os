# Deployment, Backup and Recovery

## 1. Primary topology and fallback storage

### Host A — Personal OS

```text
6 vCPU
16 GB RAM
200 GB NVMe
No GPU required
```

Chạy reverse proxy, API, MCP, worker, Web App, PostgreSQL, Qdrant, Neo4j, Temporal, Redis và Alloy agent. Original files/attachments không lưu lâu dài trên disk; chúng nằm trong active private S3-compatible object store.

### Host B — Observability

```text
4 vCPU
8 GB RAM
120 GB NVMe
No GPU
```

Chạy Prometheus, Grafana, Loki, Tempo, Alertmanager, Alloy gateway và exporters. Sentry dùng cloud errors-only để tránh full self-hosted bundle quá nặng cho một người dùng.

### Optional MinIO fallback storage

Cloudflare R2 là production default. Nếu dùng MinIO Community làm production fallback, MinIO phải chạy ngoài Host A trên persistent storage/failure domain riêng, chỉ reachable qua private network và có backup riêng. MinIO Community repository đã archive; bản `RELEASE.2025-10-15T17-29-55Z` chỉ được activate sau explicit risk acceptance. Đây không phải host thứ ba bắt buộc khi R2 đang active.

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

- Cloudflare R2 là backend mặc định; MinIO Community là controlled fallback, không phải automatic failover target.
- Bucket versioning nếu active provider hỗ trợ.
- Lifecycle/retention và delete protection cho canonical objects.
- Inventory manifest gồm object key, hash, size và last modified.
- Backup nằm trên failure domain khác active backend nếu dữ liệu quan trọng.

### PostgreSQL

- Encrypted daily logical backup lên backup object store; R2 là mặc định, còn khi MinIO active thì backup target phải tách khỏi MinIO storage đó.
- WAL/PITR nếu chấp nhận vận hành thêm; nếu không, ghi rõ RPO 24 giờ.
- Backup manifest bind database schema revision và active object-store inventory checkpoint.

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
3. Verify/restore active canonical object-store objects against manifest.
4. Start Temporal/Redis and application in maintenance mode.
5. Rebuild Qdrant and Neo4j from canonical checkpoint.
6. Run integrity, policy and golden retrieval gates.
7. Enable read traffic, then writes/sync.
8. Record restore evidence and unresolved gaps.

Projection snapshots không được activate nếu không match restored PostgreSQL checkpoint và contract hash.

## 7. Controlled object-store cutover

1. Resolve và audit exact source backend, target backend và canonical checkpoint.
2. Đưa canonical writes vào maintenance/read-only; reads tiếp tục từ source nếu integrity còn pass.
3. Replicate exact object keys từ source sang target bằng scoped credentials.
4. Verify inventory count, object key, SHA-256 và byte size; missing/mismatch là terminal failure.
5. Backup active-backend configuration và deploy target endpoint/credentials; application không giữ đồng thời hai write credentials.
6. Chạy canonical read/write smoke, idempotency test và missing/corrupt fail-closed test trên target.
7. Mở lại writes và ghi audit evidence khi toàn bộ gate pass.
8. Giữ source read-only theo retention; rollback chỉ khi source vẫn match pre-cutover checkpoint và chưa có target-only committed write.

Không có request-time automatic failover. Nếu target validation fail, hệ thống giữ maintenance/read-only và không tự ghi sang backend khác.

## 8. Deployment rules

- Pin image digests/versions.
- PostgreSQL application state và Temporal persistence có thể dùng cùng server instance nhưng phải dùng database, user, migration và backup scope riêng.
- Migration job chạy một lần trước app rollout.
- Health checks phân biệt liveness, readiness và dependency status.
- App processes stateless; rolling restart không mất canonical writes.
- Destructive operation chỉ exact target, có preview/confirmation/audit.
- Rollback application chỉ khi database schema tương thích; projection rollback chỉ tới verified generation.

## 9. Backup verification

Ít nhất hàng tháng restore PostgreSQL + sampled/all objects vào disposable environment và rebuild projections. Backup không được xem là tốt chỉ vì upload thành công. Nếu MinIO là production fallback candidate, drill phải kiểm tra S3 compatibility và controlled cutover trước khi coi candidate sẵn sàng.
