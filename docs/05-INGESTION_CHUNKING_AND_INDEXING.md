# Ingestion, Chunking and Indexing

## 1. Pipeline

```text
Canonical version committed
→ policy evaluation
→ source-specific extraction
→ normalized document
→ structural chunk plan
→ metadata projection
→ dense/sparse encoding
→ fenced Qdrant upsert
→ explicit/inferred graph projection
→ verification and checkpoint
```

Mỗi stage nhận input reference nhỏ và đọc bytes qua canonical reader. Không truyền raw document qua Temporal history.

## 2. Normalized document contract

Parser trả source-neutral structure gồm document metadata và các block có `block_id`, `block_type`, `text`, `source_location`, `parent_block_id`, `attributes`. Location có thể là line range, page/bounding box, timestamp range hoặc DOM selector. Shared layers không giả định mọi nguồn có heading hoặc path.

## 3. Markdown parser

Parser tối ưu cho Obsidian và giữ YAML frontmatter, heading tree, paragraph, list, task, quote, callout, table, code fence, block IDs, footnotes, embeds, wikilinks, links, tags và source line range. Frontmatter không được lặp nguyên khối vào mọi chunk.

## 4. Chunking contract

```text
target_tokens       384
hard_max_tokens     640
min_tokens           80
overlap_tokens       64 only for forced textual splits
```

Rules:

1. Heading breadcrumb đi kèm embedding input, không tính là chunk body.
2. Không tách list item ngắn, callout nhỏ hoặc table row nếu còn trong hard limit.
3. Code fence được giữ nguyên khi có thể; fence lớn chia theo logical line ranges.
4. Section dài chia theo paragraph trước, sentence sau, token window cuối cùng.
5. Overlap không áp dụng giữa các block tự nhiên để tránh duplicate candidates.
6. Transcript chunks theo sentence và timestamp.
7. PDF chunks giữ page span; OCR block giữ bounding boxes.

## 5. Parent context

Retrieval point là leaf chunk. Context assembler có thể mở rộng về parent heading section, chunk liền kề, transcript window hoặc block đầy đủ. Parent content lấy từ canonical parsed artifact/source bytes, không nhân đôi toàn bộ vào mọi point.

## 6. Deterministic identity

```text
logical_block_id = parser-stable structural identity
chunk_digest     = SHA-256(normalized chunk body)
chunk_id         = UUIDv5(source_id, logical_block_id + split_index + chunk_digest + chunking_contract_hash)
point_id         = UUIDv5(workspace_id, source_id + chunk_id)
```

Cùng input và contract tạo cùng plan. Thay parser/chunking contract tạo generation mới; không trộn output khác contract trong active deployment.

## 7. Embedding input

```text
Title: {title}
Path: {heading_breadcrumb}
Type: {note_type} / {knowledge_type}
Domains: {domains}
Tags: {tags}

{chunk_body}
```

Chỉ thêm field có giá trị. Không thêm raw JSON, private field bị policy từ chối hoặc boilerplate rỗng. Exact input format có contract hash và được lưu trong provenance.

## 8. Incremental indexing

- Unchanged digest + compatible model contract: reuse vector và point.
- Added/changed chunk: encode và upsert.
- Removed chunk: tombstone/delete sau khi new points đã visible.
- Metadata-only change: payload update nếu embedding input không đổi.

Thứ tự `upsert new → verify → remove orphan` tránh zero-result window.

## 9. Full rebuild

1. Chụp canonical checkpoint trong PostgreSQL.
2. Provision collection/graph generation mới theo contract hash.
3. Enumerate sources được policy cho phép.
4. Project bằng idempotent batches.
5. Catch up events sau checkpoint.
6. Verify counts, hashes, indexes, exclusions và golden queries.
7. Activate PostgreSQL route nguyên tử.
8. Giữ generation trước trong bounded rollback window rồi cleanup exact target.

Không rebuild từ projection cũ.

## 10. Provider behavior

- Dense OpenAI chỉ cho content được phép ra cloud.
- Sparse BM25 chạy local cho mọi content được index.
- Embedding cache key gồm input digest, provider, model, dimension và contract hash.
- External calls có timeout, bounded retry và typed failure.
- Rate limit quản lý trong worker; Temporal là retry owner.

## 11. Verification

- Mọi expected chunk có đúng một point.
- Không point nào thuộc deleted/denied source.
- Point payload khớp canonical version và contract hash.
- Replay không đổi point set.
- Rebuild cùng checkpoint tạo manifest tương đương.
- Citation location đọc lại được canonical text tương ứng.
