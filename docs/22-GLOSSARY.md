# Glossary

## Canonical bytes

Immutable source content stored in the private Cloudflare R2 production bucket and addressed by content hash.

## Canonical object store

The private Cloudflare R2 bucket authorized for canonical reads and writes. Production and test/CI use separate buckets and credentials.

## R2 test key allowlist

The exact set of canonical object keys created by one trusted integration run in the dedicated R2 test bucket. Cleanup may delete only keys recorded in that run's allowlist; it never lists or deletes a wildcard/prefix.

## Canonical application state

Identity, version order, current pointer, policy, audit and workflow intent stored transactionally in PostgreSQL.

## Content-addressable storage (CAS)

Storage pattern where object identity/key derives from a cryptographic digest of exact bytes.

## Source

Một logical knowledge item như Obsidian note, PDF, image, audio, web page hoặc YouTube video; có stable ID và nhiều immutable versions.

## Projection

Derived, rebuildable representation optimized for a query workload, như Qdrant vectors hoặc Neo4j graph.

## Deployment generation

Một concrete projection target được build theo canonical checkpoint và contract hash, verify trước khi active.

## Fencing token

Capability/version marker ngăn stale workflow ghi vào projection target không còn hợp lệ.

## Dense retrieval

Semantic similarity search bằng continuous embedding vectors.

## Sparse retrieval

Lexical retrieval bằng sparse vectors, mạnh với keyword, identifier và rare terms.

## Hybrid retrieval

Kết hợp dense, sparse, exact lookup, metadata filters và tùy chọn graph expansion.

## Reciprocal Rank Fusion (RRF)

Phương pháp hợp nhất nhiều ranked lists dựa trên rank thay vì raw score scale.

## Reranker

Model đánh giá lại query–candidate pairs sâu hơn sau first-stage retrieval.

## Semantic filter AST

Provider-neutral typed expression do client gửi và server compile sang physical database filters sau policy/registry validation.

## Schema registry

PostgreSQL authority mô tả flexible property keys, types, filter behavior, sensitivity và revisions.

## Chunk

Bounded retrieval unit có stable provenance và source location; không phải canonical source độc lập.

## Parent context

Larger section/window được hydrate quanh relevant leaf chunk để AI hiểu đủ ngữ cảnh.

## Provenance

Thông tin về source version, evidence, provider/model, parser/chunker và contract đã tạo ra derived data.

## Citation

Stable reference tới exact source version và location như lines, page/bounding box hoặc timestamp.

## Knowledge graph

Graph của sources, sections, entities, claims, decisions và typed relationships có provenance.

## Explicit relation

Quan hệ được viết trực tiếp trong source, ví dụ Obsidian wikilink hoặc tag.

## Inferred relation

Quan hệ do algorithm/AI suy ra; phải có confidence, evidence và verification state.

## Idempotency

Khả năng thực hiện lại cùng logical operation mà không tạo thêm side effects.

## Manifest reconciliation

So sánh inventory/cursors giữa client, canonical state và projections để phát hiện missed change hoặc drift.

## Tombstone

Logical deletion marker dùng trước physical garbage collection.

## MCP

Protocol để Codex, Claude và AI clients gọi typed tools/resources của Personal Knowledge OS.

## Context pack

Bounded set of relevant canonical excerpts, graph facts và citations được chuẩn bị cho một AI task/session.

## Tail sampling

Trace sampling quyết định sau khi collector đã thấy đủ spans để giữ error/slow/important traces.
