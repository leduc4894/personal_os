# PostgreSQL Data Model

## 1. Vai trò

PostgreSQL là canonical application state và correctness authority cho source identity, versions, policy, projection route, workflow intent, audit và approvals. Content bytes lớn nằm trong active private S3-compatible object store.

## 2. Core tables

| Nhóm | Tables |
|---|---|
| Identity | `users`, `workspaces`, `devices`, `device_tokens`, `web_sessions` |
| Sources | `sources`, `source_versions`, `content_objects`, `source_locators`, `source_tombstones`, `source_conflicts` |
| Sync | `sync_events`, `device_cursors`, `manifest_runs`, `multipart_uploads`, `multipart_parts` |
| Metadata | `source_metadata`, `metadata_values`, `property_definitions`, `schema_revisions` |
| Policy | `source_policies`, `policy_rules`, `policy_evaluations` |
| Projection | `projection_deployments`, `projection_routes`, `projection_checkpoints`, `projection_manifests`, `projection_failures`, `embedding_cache` |
| Safety/product | `action_proposals`, `action_approvals`, `audit_events`, `query_logs`, `query_feedback`, `provider_usage` |

`sources.current_version_id` là authoritative pointer. `content_objects.content_hash` unique và reference-counted. Flexible values dùng typed columns với constraint chỉ một representation được set.

## 3. Key constraints

- Mọi business table có `workspace_id` và foreign key.
- Current version thuộc đúng source.
- Content hash là lowercase SHA-256, byte size không âm.
- Event ID và idempotency key unique.
- Version number tăng đơn điệu theo source.
- Chỉ một active projection route cho mỗi projection kind.
- Approved action bind exact proposal hash và base version.
- Tombstone không làm mất audit lineage.

## 4. JSONB policy

JSONB dùng cho raw source metadata, provider-neutral structured output và bounded diagnostic detail. Không dùng JSONB thay stable foreign key, current pointer, query-critical lifecycle state, typed range value hoặc authorization scope. Mọi JSONB có Pydantic contract và schema revision.

## 5. Transaction boundaries

### Publish source version

1. Lock source row.
2. Verify expected base version.
3. Insert immutable version referencing verified object.
4. Update current pointer.
5. Insert sync/change event và projection intent.
6. Commit.

Object upload xảy ra trước transaction; orphan object được GC sau grace period nếu transaction không commit.

### Activate projection

Trong một transaction: verify deployment state/checkpoint/contract, compare route revision, swap target, increment revision và ghi audit event.

## 6. Indexing strategy

Index theo current source/version lookup, device cursor/event sequence, pending projection intent, active policy, proposal status và audit time range. Không tạo GIN index cho mọi JSONB trước benchmark.

## 7. Retention

- Sync events compact sau khi device cursor và backup checkpoint an toàn.
- Query logs chỉ giữ redacted metadata trong bounded retention.
- Source versions/objects theo retention, tombstone grace và holds.
- Projection manifests/cache có thể xóa và rebuild.
- Audit events quan trọng dùng append-only retention dài hơn.

## 8. Migration discipline

- Alembic baseline upgrade từ empty database.
- Mỗi migration có downgrade hoặc explicit irreversible gate.
- Test upgrade, application smoke và downgrade trên disposable PostgreSQL.
- Baseline chỉ chứa schema cần thiết cho target architecture.
