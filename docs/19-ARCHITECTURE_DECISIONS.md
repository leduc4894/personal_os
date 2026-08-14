# Architecture Decisions

## ADR-001 — Canonical state is split by responsibility

**Decision:** Private Cloudflare R2 giữ immutable bytes; PostgreSQL giữ identity/version/current pointer/policy. Hai thành phần tạo thành canonical boundary.

**Reason:** Object storage tối ưu durable bytes; relational database tối ưu transactional state. Obsidian và Web App là clients, không phải backend authority.

## ADR-002 — One user, one knowledge workspace

**Decision:** Product đầu tiên chỉ phục vụ một user và một logical workspace chứa Obsidian cùng external sources.

**Reason:** Đơn giản hóa auth, collection strategy và product UX nhưng vẫn giữ stable UUID boundaries.

## ADR-003 — Modular monolith before microservices

**Decision:** Một backend codebase với API, MCP và worker processes.

**Reason:** Giảm operational complexity; chỉ tách service khi benchmark/isolation yêu cầu.

## ADR-004 — Qdrant and Neo4j are rebuildable projections

**Decision:** Không dữ liệu duy nhất nằm trong Qdrant/Neo4j. PostgreSQL route trỏ active generation.

**Reason:** Cho phép wipe/rebuild, contract evolution và fail-safe recovery.

## ADR-005 — Unified Qdrant collection per workspace

**Decision:** Markdown, web, PDF, image, audio và YouTube cùng collection.

**Reason:** Giữ cross-source retrieval và liên kết. Tách collection chỉ khi isolation/retention/benchmark chứng minh cần.

## ADR-006 — Sparse typed dynamic metadata

**Decision:** Fixed hot fields có indexes riêng; flexible properties dùng shared typed nested arrays controlled by PostgreSQL registry.

**Reason:** Filter linh hoạt mà RAM index count không tăng theo số property.

## ADR-007 — Domains and tags are hot RAM indexes

**Decision:** Hierarchical `domains` và `tags` mở rộng ancestors và index keyword trong RAM.

**Reason:** Đây là facets/filter thường xuyên; latency tương tác đáng giá hơn lượng RAM nhỏ của personal corpus.

## ADR-008 — Structural, source-aware parsing with source-neutral downstream contracts

**Decision:** Parser hiểu Markdown/Obsidian; normalized document, chunking output, projection và retrieval dùng generic contracts.

**Reason:** Tối ưu note hiện tại nhưng không khóa shared system vào Markdown.

## ADR-009 — Temporal owns durable orchestration and retries

**Decision:** Workflow deterministic; activities làm I/O; Temporal là retry owner.

**Reason:** Idempotent resume, visibility và bounded failure handling tốt hơn ad-hoc queues.

## ADR-010 — MCP and API are adapters

**Decision:** Cả hai gọi shared domain services; không query databases trực tiếp.

**Reason:** Policy, ranking, citation và audit phải nhất quán cho mọi client.

## ADR-011 — Human approval for AI writes

**Decision:** AI chỉ tạo proposal; commit cần exact diff approval và base-version recheck.

**Reason:** Ngăn silent destructive edits và giữ Obsidian/Web state nhất quán.

## ADR-012 — Admin Dashboard owns exclusions and schema configuration

**Decision:** Plugin nhận policy snapshot nhưng backend/Admin là authority.

**Reason:** Một nơi quản lý policy, audit và projection cleanup; client không thể bypass.

## ADR-013 — Cloud-first AI with policy-gated local fallback

**Decision:** Dense embedding và reranking mặc định dùng APIs đã chọn; sparse local luôn có. OCR/STT có provider abstraction và local fallback lazy-loaded.

**Reason:** Personal workload không đáng duy trì GPU/model servers, nhưng local-only policy vẫn có đường xử lý phù hợp.

## ADR-014 — Two small hosts and managed object/error storage

**Decision:** Host A chạy Personal OS, Host B chạy observability; Cloudflare R2 mặc định giữ bytes và Sentry Cloud giữ errors-only.

**Reason:** Tách failure domain vừa đủ, tránh disk/GPU/Sentry self-host overhead quá mức.

## ADR-015 — Tail-based trace sampling

**Decision:** Alloy giữ toàn bộ error/slow/security/write traces và 10% normal traces sau benchmark.

**Reason:** Bảo toàn signal quan trọng với Tempo retention nhỏ.

## ADR-016 — Baseline-first architecture

**Decision:** Baseline, contracts và tests chỉ mô tả target system có thể bootstrap từ empty environment.

**Reason:** Giữ implementation surface nhỏ, nhất quán và có thể kiểm chứng từ đầu.

## ADR-017 — One active S3-compatible canonical object store (superseded)

**Status:** Superseded by ADR-018. Giữ record này để giải thích quyết định cũ; không được dùng làm implementation contract.

**Decision:** Cloudflare R2 là production default. MinIO Community phục vụ local/test và có thể là production fallback trên failure domain riêng sau explicit risk acceptance. Tại một thời điểm chỉ một backend được application cấp quyền ghi; chuyển backend là maintenance operation có replication, inventory/hash/size verification, configuration activation, smoke test và audit. Không automatic failover theo request.

**Reason:** Một S3-compatible contract giữ domain portable nhưng dual-write hoặc opportunistic failover có thể tạo split-brain giữa PostgreSQL pointer và object bytes. MinIO Community repository đã archive nên fallback này cần pin bản cuối, kiểm soát exposure, backup và replacement trigger.

## ADR-018 — Cloudflare R2 is the only canonical object store

**Decision:** Cloudflare R2 là canonical object store duy nhất cho local, test/CI và production. Production và test/CI dùng private bucket cùng bucket-scoped credentials tách biệt. Không triển khai MinIO, provider fallback, dual-write hoặc object-store cutover. R2 dependency failure dùng bounded retry rồi fail closed; PostgreSQL không publish pointer tới object chưa verify.

**Reason:** Hệ thống cá nhân không cần gánh source build, patching, backup và vận hành một MinIO Community dependency đã archive. Một managed backend giảm đáng kể operational surface và loại split-brain/fallback state machine; đổi lại hệ thống chấp nhận Cloudflare/account concentration risk và cần live R2 tests cùng restore evidence.

## ADR-019 — Transaction-scoped advisory locks and a leased outbox publish source versions

**Decision:** Source version publication chạy trong một PostgreSQL `READ COMMITTED` transaction với hai transaction advisory lock theo thứ tự cố định — idempotency identity `(workspace_id, idempotency_key)` trước, source identity sau — thay vì `SERIALIZABLE` hay một idempotency ledger table riêng. Changed publication ghi cặp projection intent như durable outbox; một dispatcher riêng claim intent theo leased batch (50 intents, lease 60 giây, backoff `min(300, 2 ** prior_attempt_count)` giây) và start đúng một Temporal workflow deterministic `source-ingestion/{workspace_id}/{event_id}` với conflict policy `USE_EXISTING` và input đóng chỉ gồm bốn UUID. Commit không chắc chắn được giải quyết bằng evidence lookup trên connection mới hoặc trả về retryable `source_commit_outcome_unknown`; database failure không bao giờ kích hoạt compensating R2 deletion.

**Reason:** Contention thực tế nằm ở replay identity và source pointer, nên source-local locking giữ retry thấp trong initial import mà vẫn tạo deterministic outcomes cho mọi race. Outbox tách transaction PostgreSQL khỏi Temporal start — không giữ lock qua network call và không đòi cross-system atomicity — trong khi workflow ID deterministic khiến retry sau lost acknowledgement hội tụ về một execution. Tham khảo spec `docs/superpowers/specs/source-version-commit-and-idempotency-design.md` và runbook `docs/operations/source-publication.md`.
