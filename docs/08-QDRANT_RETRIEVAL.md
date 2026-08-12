# Qdrant Payload and Retrieval Projection

## 1. Collection strategy

Một knowledge workspace dùng một active chunk collection để mọi source — Obsidian, web, PDF, image, audio và YouTube — được tìm và liên kết cùng nhau.

```text
knowledge_chunks_g{generation}_{contract_hash_prefix}
```

PostgreSQL route là authority. Alias chỉ là operational convenience và được reconcile theo route. Collection contract immutable; incremental content được ghi bằng deployment capability có fencing token.

## 2. Point and vector contract

Một point tương ứng một retrieval leaf chunk.

```text
Point ID: deterministic UUID from workspace_id + source_id + chunk_id
Named dense vector: dense, dimension 1536, cosine
Named sparse vector: sparse, Qdrant/bm25
```

Không tạo point cho note header rỗng. Parent section được hydrate khi context assembly cần.

## 3. Payload

```json
{
  "workspace_id": "uuid",
  "source_id": "uuid",
  "source_version_id": "uuid",
  "chunk_id": "uuid",
  "source_type": "markdown",
  "note_type": "decision",
  "knowledge_type": "procedural",
  "title": "Example",
  "locator": "Projects/Example.md",
  "heading": "Decision",
  "heading_path": ["Project", "Decision"],
  "chunk_index": 3,
  "chunk_text": "...",
  "source_location": {"kind": "lines", "start": 42, "end": 57},
  "domains": ["core_skills", "core_skills/soft_skills"],
  "tags": ["learning", "learning/note_taking"],
  "aliases": ["PKM decision"],
  "created": "2026-01-01T00:00:00Z",
  "updated": "2026-01-02T00:00:00Z",
  "archived": false,
  "ai_access": "cloud_ok",
  "attrs_keyword": [{"k": "status", "v": "accepted"}],
  "attrs_integer": [{"k": "rating", "v": 5}],
  "attrs_float": [],
  "attrs_boolean": [],
  "attrs_datetime": [],
  "attrs_text": [],
  "attr_keys": ["status", "rating"],
  "provenance": {
    "content_hash": "sha256",
    "parser_contract": "hash",
    "chunking_contract": "hash",
    "embedding_provider": "openai",
    "embedding_model": "text-embedding-3-small",
    "embedding_dimension": 1536,
    "embedding_contract": "hash",
    "sparse_model": "Qdrant/bm25",
    "projection_contract": "hash"
  }
}
```

`chunk_text` nằm trong Qdrant để rerank/context nhanh nhưng không canonical; citation cuối có thể hydrate từ active canonical object store để bảo đảm exact version.

## 4. Hierarchical filtering

`domains` và `tags` chứa assignment cùng mọi ancestor. Query `under core_skills` compile thành exact match `domains = core_skills`, không dùng prefix scan. Hai field này thường xuyên filter nên index trong RAM.

## 5. Flexible property encoding

Mỗi type dùng nested `{k,v}` array. Nested condition bảo đảm key và value thuộc cùng phần tử:

```text
attrs_keyword[].k = "status"
AND
attrs_keyword[].v = "accepted"
```

Schema registry quyết định property nào được encode, type và operator hợp lệ. Số property tăng không làm tăng số physical indexes.

## 6. Payload indexes

### RAM-resident hot indexes

| Field | Type | Vì sao |
|---|---|---|
| `source_type` | keyword | Lọc loại nguồn thường xuyên |
| `note_type` | keyword | Chọn semantics của note |
| `knowledge_type` | keyword | Lọc loại tri thức |
| `domains` | keyword | Điều hướng domain phân cấp |
| `tags` | keyword | Filter/facet thường xuyên |
| `archived` | boolean | Mandatory active-content filter |
| `ai_access` | keyword | Mandatory privacy/provider filter |

Collection dành cho đúng một workspace nên `workspace_id` vẫn có trong payload để kiểm tra integrity nhưng không cần RAM index. Nếu sau này một collection chứa nhiều workspace, phải thêm keyword index trước ingest.

### On-disk fixed indexes

| Field | Type | Use case |
|---|---|---|
| `aliases` | keyword | Exact alias lookup/filter |
| `created` | datetime | Time range |
| `updated` | datetime | Recency/time range |

### On-disk shared dynamic indexes

```text
attr_keys
attrs_keyword[].k
attrs_keyword[].v
attrs_integer[].k
attrs_integer[].v
attrs_float[].k
attrs_float[].v
attrs_boolean[].k
attrs_boolean[].v
attrs_datetime[].k
attrs_datetime[].v
attrs_text[].k
attrs_text[].v
```

Key paths dùng keyword indexes. Value paths dùng type tương ứng; text value dùng full-text index. Tất cả `on_disk=true` vì flexible filters ít nóng hơn domains/tags. Chỉ promote một field thành RAM index sau benchmark.

## 7. Fields deliberately not indexed

Identity/citation/provenance như `source_id`, `source_version_id`, `chunk_id`, `heading`, `locator`, content hash và contract hashes không index mặc định. Chúng dùng để hydrate, verify và diagnose sau candidate selection.

`title`, `heading`, `aliases`, domains và tags đã nằm trong sparse input; không thêm full-text indexes cho title/heading ở giai đoạn đầu.

## 8. Query contract

Public query chỉ gửi semantic filter AST. Compiler validate registry, inject `archived=false` và policy filters, compile hierarchical/dynamic nested filters, reject raw physical field names và bind query vào active deployment route.

## 9. Hybrid retrieval

Dense và sparse chạy song song. Candidate lists fuse bằng Reciprocal Rank Fusion. Reranker xử lý top candidates; diversity cap tránh một source chiếm toàn bộ kết quả. Exact IDs/aliases được boost có giới hạn nhưng không vượt mandatory policy.

## 10. Rebuild and verification

- Create collection với exact vector/index contract trước ingest.
- Introspect và hash contract.
- Upsert deterministic batches với fencing token.
- Verify manifest, policy exclusions và sample payloads.
- Catch up canonical events rồi mới activate route.
- Mọi collection có deployment row; orphan collection chỉ cleanup bằng exact name.
