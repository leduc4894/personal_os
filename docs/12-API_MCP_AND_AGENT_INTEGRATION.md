# API, MCP and Agent Integration

## 1. Adapter rule

FastAPI và MCP là adapters gọi cùng domain services. Không adapter nào query PostgreSQL, Qdrant, Neo4j hoặc Cloudflare R2 trực tiếp nếu đã có service tương ứng.

## 2. API groups

```text
/auth
/workspace
/sources
/sync
/search
/context
/graph
/actions
/admin
/events
```

HTTP API được version qua OpenAPI contract/release, không encode architecture generation vào domain names.

Trạng thái hiện tại (2026-08-15): Phase 2 child 1
(`api-runtime-and-contract-foundation-design.md`) đã triển khai contract spine
— runnable FastAPI composition root tại `apps/api`, envelope/error/correlation
contract, `/api/health/live` và `/api/health/ready`, local-only
`/api/openapi.json`, deterministic OpenAPI snapshot và shared generated
client. Các group ở trên thuộc các child spec sau theo sequence trong
`2026-08-15-phase-two-obsidian-sync-design.md` (section 17). Operator
runbook: `docs/operations/api-runtime-contract.md`.

### Device sync routes (Child 6, spec 7.1-7.4)

Tám route đã đăng ký, mỗi route một semantic operation id, đều yêu cầu active
`obsidian_sync` device Bearer credential (workspace/device derive từ
credential; request field không bao giờ chọn scope) và trả canonical envelope
kèm `Cache-Control: no-store`:

| Route | Operation id |
|---|---|
| `GET /api/sync/events` | `pullDeviceSyncEvents` |
| `POST /api/sync/cursor-acknowledgements` | `acknowledgeDeviceSyncCursor` |
| `POST /api/sync/manifests` | `startDeviceManifest` |
| `PUT /api/sync/manifests/{manifest_run_id}/pages/{page_number}` | `appendDeviceManifestPage` |
| `POST /api/sync/manifests/{manifest_run_id}/finalize` | `finalizeDeviceManifest` |
| `GET /api/sync/manifests/{manifest_run_id}/actions` | `listDeviceManifestActions` |
| `POST /api/sync/manifests/{manifest_run_id}/complete` | `completeDeviceManifest` |
| `GET /api/sources/{source_id}/versions/{source_version_id}/content` | `downloadDeviceSourceVersion` |

Binary download là ngoại lệ envelope được tài liệu hóa duy nhất của group
này: success stream exact verified bytes với `Content-Length`, `Content-Type`
và `X-Content-SHA256`; lỗi trước khi stream vẫn là JSON envelope canonical.

Closed error registry của domain `device_sync` (spec 13; mỗi code map đúng
một HTTP status đã test, không nhận safe detail — lý do đọc được đi qua
structured diagnostics và plugin trail):

| Code | HTTP status |
|---|---|
| `device_cursor_gap` | 409 |
| `device_cursor_regression` | 409 |
| `device_cursor_ack_ahead` | 409 |
| `device_event_unavailable` | 404 |
| `device_event_integrity_failed` | 409 |
| `device_manifest_not_found` | 404 |
| `device_manifest_expired` | 410 |
| `device_manifest_state_invalid` | 409 |
| `device_manifest_page_invalid` | 422 |
| `device_manifest_page_replay_mismatch` | 409 |
| `device_manifest_digest_mismatch` | 422 |
| `device_manifest_policy_advanced` | 409 |
| `device_download_integrity_failed` | 422 |
| `device_sync_dependency_unavailable` | 503 |

Mọi code đều terminal cho request kích hoạt ngoại lệ duy nhất:
`device_sync_dependency_unavailable` (503) là retryable — plugin backoff
bounded rồi retry với cùng identity. Operator runbook của toàn bộ mặt
đồng bộ thiết bị: `docs/operations/device-cursor-manifest-reconciliation.md`.

## 3. Response envelope

```json
{
  "request_id": "uuid",
  "data": {},
  "warnings": [],
  "error": null
}
```

Envelope là strict model (`extra="forbid"`): đúng một trong `data`/`error`
khác null. `request_id` do server sinh (UUIDv7), trả về trong cả body và header
`X-Request-ID`; client không thể chọn request id. Response luôn kèm
`traceparent` (W3C version `00`). Warning dùng `{code, message, details}` với
cùng safe-detail grammar; mọi public warning vocabulary phải được đăng ký
trước khi dùng.

Errors có stable code, retryable flag và safe detail. Không trả provider
exception nguyên văn. HTTP mapping là closed per-code table (400/422/404/405
cho bốn API transport codes; 503 cho database dependency codes; 500 cho
`internal_error`); mọi code mà route expose phải có đúng một mapping được test.
Local/test `/api/openapi.json` là ngoại lệ tài liệu hóa duy nhất không dùng
envelope.

## 4. MCP tool catalog

### Read tools

```text
search_knowledge
get_source
get_source_section
get_context
explain_retrieval
get_backlinks
find_related
find_path
get_decision_context
```

### Proposal tools

```text
propose_source_edit
propose_new_note
propose_metadata_change
propose_link
list_action_proposals
```

Không có tool “write directly”. Approval và apply là user-visible workflow.

## 5. MCP resources

Resources chỉ expose bounded, policy-checked views như workspace summary, source metadata, context pack và proposal diff. Resource URI chứa stable ID, không chứa raw filesystem path làm authority.

## 6. Tool response contract

```text
request_id
items
citations
warnings
degraded_components
next_cursor
```

Search/context item luôn có source ID, version ID, score/rank summary và citation. Agent không cần biết Qdrant point ID hoặc graph internal ID.

## 7. Authentication and scope

- Obsidian dùng per-device rotating token, workspace scoped.
- Web App dùng secure HTTP-only session.
- Remote MCP dùng personal access token với tool scopes và expiry.
- Local stdio MCP nhận token qua process environment/secret store.
- Mọi request resolve user/workspace từ credential; không tin `workspace_id` tùy ý trong body.

## 8. Codex and Claude integration

Hai client dùng cùng MCP server và tools. Recommended agent flow:

1. `search_knowledge` để lấy candidates.
2. `get_context` cho bounded context có citations.
3. Khi cần thay đổi, gọi proposal tool.
4. Người dùng approve trong Web App/Obsidian.
5. Agent đọc lại current version nếu tiếp tục làm việc.

Context pack có thể được dùng để tạo session bootstrap nhưng không thay system instructions của AI client.

## 9. Pagination and limits

- Cursor opaque, bind query/filter/deployment revision.
- Server cap `top_k`, context tokens, graph hops, source bytes và proposal diff size.
- Streaming progress dùng SSE; không stream raw workflow histories.
- Rate limits riêng cho search, canonical reads và costly provider operations.

## 10. Contract governance

- Pydantic schemas là backend source; OpenAPI được snapshot test.
- TypeScript client generated và compile trong CI.
- Breaking change cần explicit API release note và bounded contract transition window.
- MCP schemas có contract tests với Codex/Claude-compatible clients.
- Raw SQL/Cypher/Qdrant filters không thuộc public contract.
- Pipeline hiện tại: `personal-api export-openapi` render deterministic
  document (key-sorted, không `servers`/machine values) vào committed snapshot
  `packages/api-client/openapi.json`; `poe api-contract-check` so sánh
  byte-for-byte với fresh render và verify generated TypeScript
  (`packages/api-client/src/generated/schema.ts`) không stale — gate này nằm
  trong `poe boundary-check` và `poe verify`. Một contract change phải cập nhật
  routes/models, snapshot, generated client và contract tests trong cùng một
  change. Generator input luôn là local snapshot, không bao giờ remote URL.
