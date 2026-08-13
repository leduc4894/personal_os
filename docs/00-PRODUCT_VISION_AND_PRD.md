# Product Vision and PRD

## 1. Tầm nhìn

Xây dựng một hệ điều hành tri thức cá nhân hợp nhất ghi chú, nguồn tham khảo, lịch sử quyết định và ngữ cảnh công việc. Hệ thống giúp người dùng tìm lại bằng từ khóa hoặc ý nghĩa, khám phá liên kết, xây context chính xác cho AI và tạo hành động có kiểm soát.

## 2. Người dùng mục tiêu

Một người dùng duy nhất, sử dụng:

- Một Obsidian Vault làm working copy trên máy cá nhân.
- Một Web App để đọc, tìm kiếm, chỉnh sửa và quản trị từ xa.
- Codex, Claude hoặc AI client khác qua MCP/API.
- Nguồn bổ sung: web page, PDF, image, plain text, audio và YouTube.

Multi-user collaboration, billing và public knowledge sharing không thuộc phạm vi đầu tiên.

## 3. Giá trị cốt lõi

- **Nhớ:** đồng bộ nội dung và lịch sử phiên bản bền vững, không phụ thuộc đường dẫn file.
- **Tìm:** hybrid retrieval kết hợp dense, sparse, metadata filters và graph expansion.
- **Kết nối:** biến wikilink, backlink, heading, tag, domain, entity và claim thành graph có provenance.
- **Suy luận có bằng chứng:** context trả cho AI có citation tới source/version/chunk và thể hiện mức tin cậy.
- **Hành động an toàn:** AI tạo proposal; người dùng xem diff và phê duyệt trước khi thay đổi.

## 4. Use cases ưu tiên

1. Tìm lại một ý hoặc đoạn code dù không nhớ đúng từ khóa.
2. Hỏi AI dựa riêng trên knowledge workspace và nhận citation.
3. Tạo context pack cho phiên Codex/Claude mới.
4. Tìm các note, source, claim và decision có liên quan.
5. Theo dõi quyết định nào thay thế quyết định nào và bằng chứng đi kèm.
6. Chỉnh sửa note từ Obsidian hoặc Web App mà không mất thay đổi.
7. Ingest PDF, ảnh và audio thành nội dung có thể tìm kiếm.
8. Quản trị exclusion, privacy, provider và indexing policy từ Admin Dashboard.
9. Xóa toàn bộ Qdrant/Neo4j rồi rebuild từ canonical state.

## 5. Functional requirements

- Stable identity cho workspace, source, version, chunk và event.
- Bidirectional sync giữa canonical backend, Obsidian và Web App.
- Immutable content objects lưu theo SHA-256.
- Metadata cố định và flexible properties có typed filtering.
- Structural chunking tối ưu cho Obsidian Markdown.
- Dense, sparse, hybrid và graph-assisted retrieval.
- Explainable ranking và citation tới source location.
- Knowledge graph gồm explicit facts và AI-inferred relations tách biệt.
- Durable ingestion/rebuild/reconcile workflows.
- MCP tools dùng chung domain services với FastAPI.
- Human approval cho AI write.
- Audit, metrics, logs, traces và error tracking không lộ nội dung.

## 6. Non-functional requirements

- Idempotent: replay cùng event không tạo duplicate version, chunk, point hoặc edge.
- Rebuildable: Qdrant và Neo4j có thể phục hồi từ PostgreSQL + canonical bytes trong Cloudflare R2.
- Fail closed: policy hoặc canonical bytes không rõ ràng thì không index và không trả nội dung.
- Single-user efficient: không đưa vào kiến trúc các thành phần chỉ phục vụ hyperscale.
- Provider-portable: embedding, reranking, OCR, speech-to-text và LLM qua interface.
- Observable: mỗi request có request ID và trace ID; mỗi workflow có progress/error code.
- Testable: deterministic core không cần gọi provider thật; live provider tests được tách riêng.

## 7. Ngoài phạm vi ban đầu

- Realtime multi-user collaborative editing.
- Backend tự ý sửa Vault mà không có user action/approval.
- Fine-tune model riêng.
- Video understanding ngoài caption/transcript và metadata.
- Microservice hóa theo từng database hoặc provider.
- Dùng Qdrant hay Neo4j làm nơi duy nhất giữ dữ liệu không thể tái tạo.

## 8. Definition of success

- Một note mới xuất hiện trong retrieval sau khi sync hoàn tất.
- Edit đồng thời được phát hiện và không silent overwrite.
- Search trả kết quả đúng bằng tiếng Việt và tiếng Anh với citation hợp lệ.
- Excluded/private source không xuất hiện trong projection không được phép.
- Xóa collection Qdrant và graph Neo4j rồi rebuild cho kết quả tương đương.
- Codex và Claude dùng cùng MCP contracts, cùng policy và cùng context service.
- Hệ thống vận hành trên hai host nhỏ, không cần GPU thường trực.
