# Testing and Evaluation

## 1. Test layers

- Unit: normalization, parser, chunker, filter compiler, ranking và policy.
- Contract: API, MCP, provider adapters, payload/graph contracts.
- Integration: PostgreSQL, Qdrant, Neo4j, Redis và Temporal trong disposable Docker stack; Cloudflare R2 chạy trong live object-storage pipeline riêng.
- End-to-end: Obsidian sync/Web edit đến retrieval/citation.
- Live provider: OpenAI, Cohere, OCR/STT cloud nếu bật.
- Performance: indexing throughput, retrieval latency, memory/disk growth.

## 2. Golden retrieval corpus

Corpus có Vietnamese, English, cross-language, exact keyword, code, hierarchical filter, flexible property filter, time range, multi-hop, contradiction, negative memory, distractors và malicious prompt injection.

Mỗi case định nghĩa query, allowed sources, relevant chunks, forbidden sources, expected filters/citations và quality thresholds. Query secrets hoặc private personal content nằm ngoài Git.

## 3. Retrieval metrics

- Recall@K, nDCG@K, MRR, success@K.
- Citation precision và source-version correctness.
- Filter precision/recall.
- Duplicate rate và source diversity.
- Answer groundedness khi có generation step.
- Latency và provider cost.

Baseline pin theo provider/model/contracts. Update baseline là explicit reviewed action, không tự động chấp nhận regression.

## 4. Graph tests

- Wikilink/alias/heading/block resolution.
- Backlink, embeds và hierarchy.
- Entity merge ambiguity.
- Evidence integrity cho inferred edge.
- Contradiction/supersession/path queries.
- Depth/node/time budgets.
- Rebuild equivalence.

## 5. Sync tests

- Create/update/rename/move/delete/restore.
- Duplicate/out-of-order events.
- Cursor gap và full manifest reconcile.
- Offline queue/reconnect.
- Text and binary conflicts.
- Multipart interruption/checksum mismatch.
- Remote Web App edit applied atomically by plugin.
- Exclusion policy changes.

## 6. Workflow tests

Mỗi workflow có success, retryable failure, terminal failure, crash-after-commit, cancellation, timeout, continue-as-new, idempotent replay và fencing cases. Live registry liệt kê exact test nào cần dependency thật.

## 7. Security tests

- Cross-scope token denial.
- Revoked/expired device and MCP tokens.
- Prompt injection corpus.
- Local-only content không xuất hiện trong cloud provider mocks/live audit.
- Excluded content không có point/edge.
- Proposal approval stale-base rejection.
- Telemetry scrubber không leak content/secret.

## 8. Migration tests

Mỗi schema change chạy empty upgrade, application smoke, data fixture upgrade và downgrade trên disposable PostgreSQL. Baseline phải tự bootstrap từ database rỗng.

## 9. Wipe-and-rebuild drills

- Xóa disposable Qdrant collection rồi rebuild từ PostgreSQL + Cloudflare R2.
- Xóa disposable Neo4j database rồi rebuild.
- So sánh contract hash, manifest IDs, counts và golden results.
- Không seed từ snapshot projection trong correctness drill.

Object-storage unit/contract suite dùng scripted test double và không cần network. Phase 1 live Cloudflare R2 pipeline chạy behavior cases trên bucket test: single-part streaming put/get, head, exact key, conditional create, SHA-256/size verification, deduplication và missing/corrupt fail-closed. Multipart và presigned behavior chỉ được thêm sau khi owning contract tương ứng được duyệt.

Live R2 tests dùng credentials chỉ truy cập bucket test. Mỗi CI run ghi lại exact allowlist các canonical object key do chính run tạo; cleanup chỉ được xóa các key trong allowlist đó, không list/xóa wildcard hoặc prefix. Job chỉ chạy trên trusted branch, schedule hoặc manual dispatch; pull request từ fork không nhận secrets. Acceptance bắt buộc thiếu credentials phải báo blocked/fail rõ ràng, không âm thầm skip. Production bucket và credentials không xuất hiện trong test pipeline.

## 10. CI gates

```text
format/lint
type check
unit tests
contract tests
frontend/plugin tests
OpenAPI client compile
migration tests
container config validation
secret/content leak scanners
```

Integration/live/performance gates chạy theo pipeline riêng với dependency manifest rõ ràng. Không đánh dấu milestone hoàn thành khi acceptance bắt buộc chưa chạy.

Exclusion-policy child (2026-08-17) đóng bộ feature gates riêng: `uv run poe
exclusion-policy-test` (unit/contract/API/disposable-stack integration),
`pnpm run test:e2e:exclusion-policy` (browser journey),
`uv run pytest tests/performance/test_exclusion_policy_performance.py -m
local_stack -q` (evaluator/verify/preview/reconciliation budgets) và
`uv run poe exclusion-policy-device-verification` (recorded Desktop/Mobile
reference-device evidence; fail khi records còn thiếu). CI workflow
`.github/workflows/exclusion-policy-acceptance.yml` chạy cùng các gates này
trên disposable stack.
