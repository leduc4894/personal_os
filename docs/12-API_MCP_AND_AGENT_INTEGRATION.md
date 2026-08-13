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

## 3. Response envelope

```json
{
  "request_id": "uuid",
  "data": {},
  "warnings": [],
  "error": null
}
```

Errors có stable code, retryable flag và safe detail. Không trả provider exception nguyên văn.

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
