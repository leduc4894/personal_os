# Product Roadmap

Mỗi milestone tạo phần mềm chạy được, có acceptance, migration, rollback và documentation. Không triển khai feature của milestone sau để “chuẩn bị” nếu chưa cần.

## Milestone 0 — Foundation

Scope: repository skeleton, contracts, PostgreSQL baseline, S3/R2 CAS, auth, config, CI và local Docker dependencies.

Exit:

- Empty environment bootstrap được.
- Canonical object/version commit có hash verification và idempotency.
- Unit, migration và integration skeleton pass.

## Milestone 1 — Obsidian sync

Scope: plugin onboarding, stable IDs, event upload, cursor, manifest reconciliation, exclusions và conflict detection.

Exit:

- Markdown/text/binary files sync bền vững.
- Rename giữ identity; offline replay không duplicate.
- Admin policy deny được thực thi server-side.

## Milestone 2 — Markdown ingestion and search

Scope: Obsidian parser, structural chunker, metadata registry, Qdrant collection, dense/sparse providers và basic hybrid search.

Exit:

- Search Việt/Anh có citation.
- `domains`, `tags` và flexible properties filter đúng.
- Qdrant wipe-and-rebuild pass.

## Milestone 3 — MCP and context

Scope: retrieval/context services, citations, MCP read tools, Codex/Claude integration, explain retrieval và golden evaluation.

Exit:

- Hai AI client dùng cùng contracts.
- Context budget, policy và prompt-injection tests pass.
- Retrieval baseline được pin.

## Milestone 4 — Knowledge graph

Scope: Neo4j projection, wikilinks/backlinks/heading references, entities/claims, graph queries và graph-assisted retrieval.

Exit:

- Explicit graph deterministic.
- Inferred edges có evidence/provenance.
- Neo4j wipe-and-rebuild và graph golden pass.

## Milestone 5 — Web workspace and administration

Scope: search/library/editor, version diff/conflict UI, policy/schema admin, workflow/projection cockpit và provider settings.

Exit:

- Web edit sync ngược Obsidian an toàn.
- Policy/schema publish có preview/diff/audit.
- Projection operations không nhận arbitrary target.

## Milestone 6 — AI write proposals

Scope: proposal tools, approval workflow, diff, stale-base validation và plugin apply.

Exit:

- Không có direct AI write path.
- Approval bind exact content/base version.
- End-to-end proposal → approval → sync pass.

## Milestone 7 — Additional source types

Triển khai tuần tự, không cùng lúc:

1. PDF native text extraction.
2. Image/scanned PDF OCR.
3. Audio transcription.
4. Web capture.
5. YouTube metadata/caption/transcription.

Mỗi source adapter phải có canonical artifact, citation location, policy, parser/chunker tests và golden retrieval trước khi chuyển loại tiếp theo.

## Milestone 8 — Operations and resilience

Scope: full observability stack, alerts, backup manifests, restore automation, projection rebuild/repair, capacity benchmark và runbooks.

Exit:

- Two-host deployment ổn định trong observation window.
- Alert delivery, restore drill, trace sampling và capacity gates pass.
- RPO/RTO đo được thay vì ước lượng.

## Milestone 9 — Personal OS intelligence

Chỉ sau khi retrieval/graph/action safety ổn định: daily/weekly review, decision intelligence, contradiction/staleness detection, project context packs và learning from feedback. Mỗi feature phải chứng minh giá trị qua evaluation trước khi tự động hóa sâu hơn.
