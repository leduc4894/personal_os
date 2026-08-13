# Personal Knowledge OS — Documentation Root

Đây là bộ tài liệu độc lập để xây dựng một Personal Knowledge OS từ đầu. Nội dung trong thư mục này tự đủ nghĩa, không phụ thuộc code, migration, test hay quyết định lịch sử của repository chứa nó.

## Mục tiêu

Hệ thống hợp nhất ghi chú Obsidian, trình soạn thảo web và các nguồn như web, PDF, hình ảnh, audio, video thành một knowledge workspace duy nhất để:

```text
Nhớ → Tìm → Kết nối → Đánh giá → Phản biện
→ Quyết định → Hành động → Học từ kết quả
```

Codex, Claude và các AI client khác truy cập tri thức qua MCP hoặc API có kiểm soát, có citation và không được truy cập trực tiếp database.

## Quy tắc đọc

1. [Product vision](00-PRODUCT_VISION_AND_PRD.md)
2. [Canonical architecture](01-CANONICAL_ARCHITECTURE.md)
3. [Technology stack](02-TECH_STACK.md)
4. [Data ownership](03-DATA_OWNERSHIP_AND_STORAGE.md)
5. Tài liệu domain liên quan
6. [Architecture decisions](19-ARCHITECTURE_DECISIONS.md)
7. [Implementation plan](20-IMPLEMENTATION_PLAN.md)

## Mục lục

| Tài liệu | Nội dung |
|---|---|
| [00](00-PRODUCT_VISION_AND_PRD.md) | Tầm nhìn, phạm vi và tiêu chí thành công |
| [01](01-CANONICAL_ARCHITECTURE.md) | Kiến trúc tổng thể và boundary |
| [02](02-TECH_STACK.md) | Tech stack đã chọn |
| [03](03-DATA_OWNERSHIP_AND_STORAGE.md) | Nguồn chân lý, CAS và lifecycle dữ liệu |
| [04](04-OBSIDIAN_SYNC_AND_SOURCES.md) | Obsidian sync và source ingestion |
| [05](05-INGESTION_CHUNKING_AND_INDEXING.md) | Parsing, chunking và indexing |
| [06](06-METADATA_AND_SCHEMA_REGISTRY.md) | Metadata cố định, linh hoạt và schema registry |
| [07](07-POSTGRESQL_DATA_MODEL.md) | PostgreSQL data model |
| [08](08-QDRANT_RETRIEVAL.md) | Qdrant collection, payload, index và query |
| [09](09-NEO4J_KNOWLEDGE_GRAPH.md) | Neo4j knowledge graph |
| [10](10-RETRIEVAL_CONTEXT_AND_RERANKING.md) | Retrieval, reranking, context và citation |
| [11](11-TEMPORAL_WORKFLOWS.md) | Temporal workflows |
| [12](12-API_MCP_AND_AGENT_INTEGRATION.md) | API, MCP và tích hợp AI agent |
| [13](13-WEB_APP_AND_ADMIN_DASHBOARD.md) | Web App và Admin Dashboard |
| [14](14-SECURITY_PRIVACY_AND_POLICY.md) | Security, privacy và policy |
| [15](15-OBSERVABILITY_AND_ALERTING.md) | Observability và alerting |
| [16](16-TESTING_AND_EVALUATION.md) | Testing và evaluation |
| [17](17-DEPLOYMENT_BACKUP_AND_RECOVERY.md) | Deployment, backup và recovery |
| [18](18-PRODUCT_ROADMAP.md) | Product roadmap |
| [19](19-ARCHITECTURE_DECISIONS.md) | Architecture decision records |
| [20](20-IMPLEMENTATION_PLAN.md) | Implementation plan |
| [21](21-RISKS_AND_CAPACITY_TRIGGERS.md) | Risks và capacity triggers |
| [22](22-GLOSSARY.md) | Glossary |

## Bất biến kiến trúc

- Private Cloudflare R2 giữ canonical content bytes bất biến; production và test/CI dùng bucket cùng credentials tách biệt.
- PostgreSQL giữ canonical identity, version, policy và application state.
- Obsidian và Web App là hai client chỉnh sửa cùng một logical workspace.
- Qdrant và Neo4j là projection có thể xóa và rebuild.
- Temporal điều phối workflow bền vững; Redis chỉ là cache và coordination phụ trợ.
- Mọi source có stable ID; path, URL hoặc title không phải identity duy nhất.
- Mọi external write do AI đề xuất đều đi qua policy, diff và approval.
- Một người dùng có một knowledge workspace; mọi loại source cùng nằm trong không gian truy xuất và liên kết chung.
- Không đưa raw source content vào log, metric, trace hoặc error report.
- Chỉ triển khai các contracts được mô tả trong bộ tài liệu này.

## Trạng thái tài liệu

Tài liệu mô tả target architecture. “Đã chọn” nghĩa là quyết định thiết kế phải được implementation và acceptance tests tuân thủ.
