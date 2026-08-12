# Neo4j Knowledge Graph

## 1. Vai trò

Neo4j là rebuildable graph projection để traversal, related-context discovery, multi-hop reasoning và explainability. PostgreSQL vẫn sở hữu source/version/policy; Qdrant vẫn sở hữu semantic/sparse candidate retrieval.

## 2. Node model

| Label | Identity | Nội dung |
|---|---|---|
| `Source` | `source_id` | Note, PDF, web page, audio, video |
| `Section` | `source_version_id + block_id` | Heading/structural section |
| `Chunk` | `chunk_id` | Evidence anchor; không giữ full body |
| `Tag` | canonical tag path | Tag hierarchy |
| `Domain` | canonical domain path | Domain hierarchy |
| `Entity` | resolved entity ID | Person, concept, project, tool, place... |
| `Claim` | claim ID | Atomic statement có evidence |
| `Decision` | decision ID | Decision lifecycle |
| `Task` | task ID | Extracted/explicit action item |

Mọi node có `workspace_id`, projection contract và source provenance. Không tạo node cho mọi token hoặc mọi noun phrase.

## 3. Explicit relationships from Obsidian

```text
(Source)-[:HAS_SECTION]->(Section)
(Section)-[:HAS_CHILD]->(Section)
(Section)-[:HAS_CHUNK]->(Chunk)
(Source)-[:LINKS_TO]->(Source)
(Source)-[:EMBEDS]->(Source)
(Source)-[:TAGGED_WITH]->(Tag)
(Source)-[:IN_DOMAIN]->(Domain)
(Tag)-[:CHILD_OF]->(Tag)
(Domain)-[:CHILD_OF]->(Domain)
(Source)-[:HAS_ALIAS]->(Entity)
```

Wikilink resolver hỗ trợ title, alias, relative path, heading fragment và block reference. Unresolved link được giữ dưới dạng pending reference để resolve lại khi source mới xuất hiện hoặc rename.

## 4. Obsidian-specific optimization

- `[[Note]]`, `[[Note#Heading]]`, `[[Note#^block]]` giữ target granularity.
- Embeds dùng relation riêng với normal link để context assembler biết có thể inline content.
- Heading tree giữ thứ tự bằng `ordinal`.
- Backlink không lưu hai chiều; query reverse `LINKS_TO`.
- Inline/frontmatter tags normalize vào cùng Tag nodes.
- Alias resolution lưu matched alias và resolution method.
- Daily note dates được normalize nhưng chỉ tạo temporal edges khi hữu ích.
- Canvas/unsupported plugin metadata không được suy diễn nếu chưa có parser contract.

## 5. Inferred graph

AI extraction tạo entity, claim và relation riêng biệt với explicit graph. Mọi inferred relation bắt buộc có:

```text
confidence
extraction_method
extractor_provider
extractor_model
extraction_contract
evidence_chunk_ids
verified
valid_from
valid_to
```

Không promote inferred edge thành explicit fact. Người dùng có thể verify, reject hoặc merge entity; quyết định được lưu canonical trong PostgreSQL rồi project sang graph.

## 6. Relationship types

Controlled vocabulary ban đầu:

```text
RELATED_TO
SUPPORTS
CONTRADICTS
DEPENDS_ON
IMPLEMENTS
USES
PART_OF
CAUSES
PRECEDES
SUPERSEDES
MENTIONS
ABOUT
```

Generic `RELATED_TO` chỉ dùng khi không đủ bằng chứng cho type cụ thể.

## 7. Entity resolution

Pipeline dùng normalized name, aliases, exact identifiers, workspace context và semantic similarity. Merge không phá identity:

- Canonical entity giữ stable ID.
- Duplicate entity trỏ `MERGED_INTO` trong audit state PostgreSQL.
- Graph rebuild materialize canonical result.
- Ambiguous match giữ candidates; không auto-merge dưới confidence gate.

## 8. Query patterns

- Backlinks và outgoing links của source/section.
- Related sources qua shared entity/domain/tag.
- Shortest bounded path giữa hai source/entity.
- Claims support/contradict một decision.
- Decision supersession chain.
- Neighborhood expansion từ Qdrant seeds.
- Orphan source, unresolved wikilink và disconnected subgraph.

Mọi traversal có depth, node và time budget. Graph không tự trả full content; kết quả là IDs để policy check và canonical hydration.

## 9. Projection and rebuild

Graph generation được build từ canonical checkpoint, catch up event stream, verify constraints/counts/golden queries rồi activate route trong PostgreSQL. Incremental writes mang generation fencing token. Delete/exclusion tạo tombstone và remove graph elements của source version tương ứng.

## 10. Constraints and indexes

- Unique constraints cho stable IDs trong workspace.
- Index label + workspace + stable lookup key.
- Index unresolved reference key và entity normalized name nếu query chứng minh cần.
- Không index mọi relation property.
- Không lưu embeddings trong Neo4j khi Qdrant đã phục vụ vector search.

## 11. Graph quality gates

- Explicit wikilink precision phải deterministic.
- Mọi inferred relation có evidence chunk còn tồn tại.
- Denied/deleted source không còn active node/edge.
- Bounded traversal không vượt budget.
- Rebuild cùng checkpoint tạo graph manifest tương đương.
- Golden graph cases bao phủ aliases, heading links, unresolved links, contradiction và supersession.
