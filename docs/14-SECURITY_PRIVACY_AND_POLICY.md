# Security, Privacy and Policy

## 1. Threat model

Protect against stolen device/token, exposed admin service, malicious source content, prompt injection, accidental cloud disclosure, cross-source policy bypass, poisoned projection, leaked telemetry và unsafe AI write.

## 2. Authentication

- Single user không có nghĩa bỏ authentication.
- Web login dùng password hash mạnh, optional passkey/TOTP và secure session.
- Mỗi Obsidian device có token riêng, rotation và revoke.
- MCP token có tool scopes, expiry và explicit workspace binding.
- Service credentials tách theo least privilege.
- Secret nằm trong secret files/manager, không commit hoặc lưu frontmatter.

## 3. Authorization

Authorization chạy tại domain service, không chỉ UI. Mỗi request derive identity/scope từ credential. Admin, sync, read, provider-use và action-approval là scopes riêng.

## 4. Content policy

Mỗi source được đánh giá thành:

```text
excluded       không ingest/index
local_only     chỉ local sparse/local providers
cloud_ok       được dùng approved cloud providers
```

Unknown hoặc evaluation failure mặc định deny. Policy revision gắn vào every projection/query decision.

## 5. Exclusions

Admin Dashboard là authority cho exclusion rules. Backend re-evaluate mọi upload, projection và query. Plugin-side filter chỉ tối ưu bandwidth. Rule changes tạo auditable transition và removal workflow.

## 6. Prompt injection defense

- Source text luôn được đóng khung là untrusted data.
- Không cho retrieved text thay tool policy/system instruction.
- Context assembly tách citation/content khỏi instructions.
- Strip hoặc annotate active HTML/script.
- Tool choice và write permission do server/agent policy quyết định, không do note content.
- Tests chứa malicious notes, indirect injection và poisoned metadata.

## 7. AI provider boundary

- Policy check xảy ra trước khi request body được tạo.
- Chỉ gửi minimum chunk/context cần thiết.
- Không gửi excluded/local-only content tới cloud embedding, reranking, OCR, STT hoặc LLM.
- Provider request logs chỉ giữ digest/usage, không body.
- Provider fallback cũng phải thỏa cùng policy; không tự chuyển cloud khi local lỗi.

## 8. Write safety

AI write flow bắt buộc:

```text
proposal → policy check → immutable diff
→ user approval → base-version recheck
→ canonical commit → client apply → normal sync
```

Approval bind proposal hash, source ID, base version, user và expiry. Nếu base version đổi, approval vô hiệu và cần regenerate diff.

## 9. Network

- Chỉ reverse proxy public HTTPS endpoint cần thiết.
- PostgreSQL, Qdrant, Neo4j, Redis và Temporal không public Internet.
- R2 hoặc MinIO bucket private và dùng scoped credentials/presigned URLs; credential của backend inactive không cấp quyền ghi cho application runtime.
- Object-store cutover chạy trong maintenance/read-only mode, verify exact inventory/hash/size và ghi audit; dependency failure không được kích hoạt automatic failover.
- Admin operational UIs đi qua VPN/SSH tunnel hoặc authenticated proxy.
- Host-to-host observability traffic dùng private network và authenticated OTLP/scrape boundary.

## 10. Telemetry redaction

Không log/metric/trace/error-report:

- Full query text hoặc source content.
- Excerpt/citation text.
- Token, signed URL hoặc credential.
- Vector/embedding.
- Raw provider exception có thể echo input.
- Arbitrary path nếu path được xem là sensitive.

Được phép: stable opaque IDs, hashes rút gọn, counts, durations, safe error codes và provider/model identifiers.

## 11. Audit

Audit append-only cho login, token lifecycle, policy/schema publish, source delete/restore, projection activation/cleanup, provider setting, proposal approval và backup restore. Audit có actor, target, action, request ID, result, safe diff digest và timestamp.

## 12. Destructive actions

- Resolve exact target từ PostgreSQL trước.
- Không dùng wildcard collection/object deletion.
- Preview impact và require typed confirmation.
- Recheck holds, active route và backup gate.
- Ghi audit trước/sau action.
- Ưu tiên tombstone/grace period hơn immediate physical delete.
