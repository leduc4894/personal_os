# Implementation Plan

## 1. Repository shape

```text
apps/
  api/                 FastAPI composition and HTTP routes
  mcp/                 MCP transport adapter
  worker/              Temporal worker bootstrap
  web/                 Next.js Workspace and Admin
  obsidian-plugin/     Obsidian client
src/personal_os/
  identity/
  sources/
  sync/
  metadata/
  ingestion/
  retrieval/
  graph/
  context/
  actions/
  administration/
  evaluation/
  observability/
  shared/
migrations/            Alembic baseline and forward migrations
infra/                 Compose, proxy, Alloy, Prometheus, Grafana
tests/
  unit/
  contract/
  integration/
  end_to_end/
  golden/
  performance/
docs/                  This canonical documentation set
```

Mỗi domain package chứa contracts, service, repository ports và errors của chính nó. Infrastructure adapters phụ thuộc domain ports; domain không import FastAPI, Qdrant client, Neo4j driver hoặc provider SDK.

## 2. Global implementation rules

- Python type hints đầy đủ; mypy strict cho application packages.
- TypeScript strict cho Web App và plugin.
- TDD cho domain behavior và bug fixes.
- Mỗi schema change có Alembic migration + upgrade/downgrade test.
- Mỗi workflow idempotent và có retry/failure tests.
- Mỗi API contract change cập nhật OpenAPI, generated client, tests và docs.
- Mỗi domain/phase plan mới đi kèm một task diagnostics surface: mọi closed error path của domain đó surface reason token (trail/settings/log đóng) ngay khi code land, không trì hoãn đến Phase 10 (pattern chuẩn: `docs/15-OBSERVABILITY_AND_ALERTING.md` §Device diagnostics).
- External call có timeout, bounded retry, error mapping và metrics.
- Không log raw content/query/vector/secret.
- Không thêm đường chạy ngoài target contracts hoặc model giả làm sai lệch acceptance.

## 3. Build sequence

### Phase 1 — Bootstrap and canonical core

Design specs được viết, review và triển khai lần lượt theo dependency order:

1. `phase-one-workspace-bootstrap-design.md`
2. `runtime-configuration-and-diagnostics-design.md`
3. `local-service-stack-design.md`
4. `canonical-postgresql-baseline-design.md`
5. `content-addressable-object-storage-design.md`
6. `source-version-commit-and-idempotency-design.md`
7. `canonical-core-acceptance-and-recovery-design.md`

Infrastructure baseline được kiểm tra ngày 2026-08-12 và ưu tiên patched/LTS stability:

| Component | Pinned version |
|---|---|
| PostgreSQL | `18.4` |
| Qdrant | `1.18.2` |
| Neo4j Community | `5.26.28` LTS |
| Redis Open Source | `8.6.4` |
| Temporal Server | `1.31.2` |
| Temporal UI | `2.53.0` |
| Temporal CLI | `1.8.0` |

Compose và deployment manifests pin exact image tag cùng digest, không dùng floating tag. Upgrade đi qua pull request riêng với release-note review, compatibility/migration tests và rollback evidence. Cloudflare R2 là managed dependency ngoài Compose; production và test/CI dùng bucket cùng credentials tách biệt.

Deliverables:

1. Python/TypeScript workspaces, lint/type/test CI.
2. Settings + secret-file loading, structured errors/logging.
3. PostgreSQL empty baseline for identity, source/version/object, events và audit.
4. Cloudflare R2 CAS adapter with streaming SHA-256 verification, offline contract tests và live test-bucket pipeline.
5. Source version transaction, pre-upload idempotency replay, optimistic concurrency/no-change, idempotent event service và fenced projection-intent dispatcher.
6. Local Compose for PostgreSQL, Qdrant, Neo4j Community single-instance, Redis và Temporal Server/UI/admin tools.
7. Internal CLI cho bootstrap và canonical smoke; Phase 1 không tạo public HTTP API tạm thời.

Deployment constraints:

- Temporal persistence dùng cùng PostgreSQL server trong local/single-host deployment nhưng có database, user, migration và backup scope riêng với canonical application database.
- Cloudflare R2 là canonical object store duy nhất; production và test/CI dùng private bucket cùng bucket-scoped credentials tách biệt.
- R2 dependency failure dùng bounded retry rồi fail closed; không có backend fallback, dual-write hoặc controlled cutover.
- PostgreSQL không publish version/current pointer tới object chưa được ghi và verify exact key, SHA-256 cùng byte size.
- Source publication dùng transaction advisory lock theo thứ tự idempotency-then-source, receipt nội bộ tối đa 5 phút tuổi, outbox intent với lease 60 giây và Temporal workflow identity deterministic `source-ingestion/{workspace_id}/{event_id}`; operator runbook tại `docs/operations/source-publication.md`.
- Phase 1 queue các `SourceIngestionWorkflow` start trên task queue `source-ingestion` nhưng không register workflow implementation; worker ingestion đến với deliverable Phase 3, và các start đã queue chờ trên task queue cho đến lúc đó.

Acceptance:

- Create empty stack, bootstrap one user/workspace/device.
- Commit/read an immutable source version.
- Duplicate idempotency key returns same committed result.
- Corrupt/missing object cannot become current.
- Offline object-storage contract suite và live Cloudflare R2 test-bucket pipeline pass trước production activation.
- Production/test credentials không có quyền chéo; mỗi live test run chỉ cleanup exact allowlist các canonical object key do chính run tạo.
- Migration upgrade/downgrade and backup smoke pass.

### Phase 2 — Obsidian sync

Phase 2 được triển khai qua child-spec program (sequence trong
`docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md`,
section 17). Trạng thái từng child:

- Child 1 `api-runtime-and-contract-foundation-design.md` — **hoàn thành
  (2026-08-15)**: runnable API composition root, envelope/error/correlation
  contract, liveness/readiness, deterministic OpenAPI snapshot và shared
  generated client. Plan:
  `docs/superpowers/plans/2026-08-15-api-runtime-and-contract-foundation.md`;
  handoff: `docs/handoff/2026-08-15-api-runtime-contract-foundation.md`;
  runbook: `docs/operations/api-runtime-contract.md`.
- Child 2 `web-auth-and-device-authorization-design.md` — **hoàn thành
  (2026-08-16)**: protected enrollment CLI, password login với throttling,
  web session/CSRF/re-auth contract, TOTP và recovery codes, browser device
  authorization với exact-replay exchange/refresh, device revoke
  (admin/self), Web Admin (login/security/devices/approval), Obsidian plugin
  SecretStorage onboarding, leak/integration/E2E acceptance gates và
  operations runbook. Plan:
  `docs/superpowers/plans/2026-08-16-web-auth-and-device-authorization.md`;
  handoff:
  `docs/handoff/2026-08-16-web-authentication-and-device-authorization.md`;
  runbook:
  `docs/operations/web-authentication-and-device-authorization.md`.
  Deliverable 1 của phase (plugin auth/onboarding và secure token store) đã
  đóng; implementation deliverables 2-7 thuộc các child sau và chưa hoàn tất.
- Child 3 `exclusion-policy-publication-design.md` —
  **hoàn thành (2026-08-17)**: deny-only rule model với Python/TypeScript
  golden parity, async preview, immutable/idempotent publication
  (advisory-lock ordering, ambiguous-commit evidence lookup), Ed25519 signed
  snapshot với cross-signed key rotation qua offline `personal-api policy-key`
  CLI, backend fail-closed enforcement tại mọi canonical boundary, durable
  reconciliation với pending
  projection intents, Admin API + `/admin/policy` + plugin acquisition/
  verification, và acceptance gates (feature, browser E2E, performance,
  device-verification). Alembic head `20260817_01` (single head); feature
  gates `poe exclusion-policy-test` (1383 passed), `pnpm run
  test:e2e:exclusion-policy`, performance budgets pass; implementation commit
  range `7d9a470..94a8a06` (design `1e7f270`). Spec:
  `docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md`;
  runbook:
  `docs/operations/exclusion-policy-publication.md`; handoff:
  `docs/handoff/2026-08-17-exclusion-policy-publication.md`. Reference-device
  verification records (Desktop + Mobile) còn blocking — xem handoff. Deliverable
  6 đóng; các deliverable Vault-sync (2–5, 7) và projection consumers của
  policy-transition intents thuộc child sau.
- Child 5 `source-locator-and-tombstone-lifecycle-design.md` — **hoàn thành (2026-08-25)**:
  schema/domain/PostgreSQL/API/plugin lifecycle implementation
  và automated gates delivered; hai mandatory live gate đã PASS qua
  explicit-restore target-reservation remediation — guarded Desktop WDIO
  journey (`obsidian_live_acceptance_passed`) và physical Mobile matrix 8/8
  trên thiết bị thật, evidence tại handoff
  `docs/handoff/2026-08-25-explicit-restore-target-reservation.md`. Plan:
  `docs/superpowers/plans/2026-08-20-source-locator-and-tombstone-lifecycle.md`;
  runbook: `docs/operations/source-locator-tombstone-lifecycle.md`; handoff:
  `docs/handoff/2026-08-20-source-locator-and-tombstone-lifecycle.md`.
- Child 6 `device-cursor-and-manifest-reconciliation-design.md` —
  **triển khai hoàn tất (2026-08-26); Desktop WDIO live gate PASSED
  (2026-08-27, verdict `obsidian_live_acceptance_passed`, đủ 4 kịch bản);
  còn ma trận Mobile vật lý**: domain
  `device_sync` (cursor/manifest/verified-download PostgreSQL schema
  `20260826_01`+`20260826_02`, tám route `/api/sync` + binary download,
  closed `device_*` error registry), plugin coordinator (journal v7, remote
  apply crash-safe, echo suppression, manifest reconciliation, `Repair
  sync`, trail v2) và release candidate plugin 0.2.0 đã đóng với offline
  gates xanh (`poe verify`, `poe api-contract-check`, `poe
  device-sync-test`, plugin vitest/tsc/lint/build). Child 6 CHƯA đóng: hai
  Desktop WDIO journey đã chạy và PASS; ma trận Mobile vật lý còn chờ
  operator ghi records (một dòng BACKLOG giữ chỗ, blocks Child 7);
  mock/unit/Desktop evidence không thay được Mobile vật lý, nên chưa có
  completion claim. Spec:
  `docs/superpowers/specs/2026-08-26-device-cursor-and-manifest-reconciliation-design.md`;
  plan:
  `docs/superpowers/plans/2026-08-26-device-cursor-and-manifest-reconciliation.md`;
  runbook:
  `docs/operations/device-cursor-manifest-reconciliation.md`; handoff:
  `docs/handoff/2026-08-26-device-cursor-and-manifest-reconciliation.md`.

Deliverables:

1. Plugin auth/onboarding and secure token store.
2. Stable ID persistence independent of path.
3. Local durable cursor/manifest and bounded event queue.
4. Upload/download APIs including multipart.
5. Manifest reconcile planner.
6. Server-owned exclusions and plugin policy snapshot.
7. Conflict detection/diff/resolution.

Acceptance:

- Create/update/rename/move/delete/restore pass end-to-end.
- Offline replay/out-of-order events do not duplicate or overwrite.
- Remote version applies atomically.
- Policy-denied source is neither uploaded outside contract nor indexed.

### Phase 3 — Metadata and Markdown ingestion

Deliverables:

1. Metadata registry and admin service API.
2. Obsidian Markdown parser and normalized document contract.
3. Structural token-bounded chunker.
4. Deterministic chunk/point identities and projection manifest.
5. Temporal source ingestion workflow.

Acceptance:

- Parser fixtures cover frontmatter, wikilinks, headings, callouts, tables, code, embeds và block references.
- Same input/contracts create identical plan.
- Flexible property validation and hierarchy expansion pass.
- Workflow crash/retry produces one final manifest.

### Phase 4 — Qdrant hybrid retrieval

Deliverables:

1. Collection contract with named dense/sparse vectors and exact indexes.
2. OpenAI embedding adapter and local BM25 sparse adapter.
3. Policy-gated batching/cache.
4. Semantic filter AST + schema-aware compiler.
5. Dense/sparse search, RRF fusion, reranker adapters and diversity.
6. Deployment provision/verify/activate/repair/rebuild services.

Acceptance:

- `domains`, `tags`, time và dynamic typed filters pass against real Qdrant.
- Local-only fixture never reaches cloud mocks/live audit.
- Golden retrieval meets pinned threshold.
- Wipe/rebuild returns equivalent manifest and acceptable golden metrics.

### Phase 5 — Context, API and MCP

Deliverables:

1. Canonical hydrator and citation service.
2. Context budget/adjacent/parent assembly.
3. Retrieval explain trace with redaction.
4. FastAPI search/source/context endpoints.
5. MCP search/read/graph-ready tool contracts.
6. Codex and Claude connection guides/tests.

Acceptance:

- Citation resolves exact source version/location.
- Prompt-injection fixtures cannot change tool/write policy.
- API and MCP return equivalent results for same request.
- Generated TypeScript client compiles.

### Phase 6 — Neo4j graph

Deliverables:

1. Graph schema/constraints and projection adapter.
2. Deterministic Obsidian links, headings, tags, domains.
3. Entity/claim extraction ports and provenance storage.
4. Bounded graph query service and retrieval expansion.
5. Graph rebuild/repair workflow.

Acceptance:

- Graph golden suite passes explicit and inferred cases.
- Every inferred edge has valid evidence.
- Policy-denied/deleted source leaves no active graph residue.
- Wipe/rebuild manifest equivalent.

### Phase 7 — Web App and Admin

Deliverables:

1. Auth shell, generated API client, TanStack Query and SSE.
2. Search/library/source viewers.
3. CodeMirror editor, IndexedDB drafts, version/conflict UI.
4. Graph explorer.
5. Policy/schema/provider/workflow/projection admin pages.
6. Proposal review/approval UI shell.

Acceptance:

- Playwright critical flows pass desktop and mobile read/approval layouts.
- Web edit syncs to Obsidian without echo loop.
- Admin publish operations show impact/diff and write audit.
- No frontend route calls infrastructure databases directly.

### Phase 8 — Safe actions

Deliverables:

1. Proposal contracts/repository/service.
2. Approval Temporal workflow and expiry.
3. MCP proposal tools.
4. Plugin apply/confirmation path.
5. Audit and stale-base recovery UX.

Acceptance:

- Static scanner finds no direct AI write route.
- Proposal hash/base/user binding enforced.
- Changed base invalidates approval.
- Approved edit completes canonical commit and normal client sync.

### Phase 9 — Additional source adapters

Implement one vertical slice at a time: PDF native text, image/scanned PDF OCR, audio STT, web capture, YouTube caption/STT. Each slice adds provider adapter, artifact contract, parser/chunker, citation viewer, policy tests, golden cases and cost metrics.

### Phase 10 — Production operations

Deliverables:

1. Two-host Compose manifests and private networking.
2. Alloy/Prometheus/Grafana/Loki/Tempo/Alertmanager configs theo `docs/15-OBSERVABILITY_AND_ALERTING.md` (stack authority; Phase 2 device diagnostics đã có từ child 6).
3. Sentry errors-only integration and scrubber tests.
4. Backup manifests, restore automation and runbooks.
5. Capacity/retrieval/indexing benchmarks.
6. Final service resource limits and trace sampling based on measurements.

Acceptance:

- Alerts reach Telegram/email and resolved notification works.
- Monthly-style restore drill succeeds in disposable environment.
- No content appears in logs/traces/Sentry test events.
- Sustained personal workload fits agreed host capacity with headroom.

## 4. Commit and review strategy

Mỗi deliverable nhỏ đi theo cycle:

```text
failing test → minimal implementation → focused tests
→ integration/contract gate → docs update → review → commit
```

Không gom nhiều domains độc lập vào một pull request. Database boundary, provider boundary và public contract được review riêng khi có thay đổi đáng kể.

## 5. Definition of done

- Acceptance của phase chạy trên cùng final commit.
- Không còn mục để trống hoặc cấu hình không được tài liệu hóa.
- Lint/type/unit/contract/integration liên quan pass.
- Migration và rollback/recovery path được chứng minh.
- Security/policy/idempotency tests pass.
- Canonical docs và runbooks cập nhật.
- Không claim live/provider/deployment success nếu chưa có command evidence.
