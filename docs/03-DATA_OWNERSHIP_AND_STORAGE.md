# Data Ownership and Storage

## 1. Canonical contract

Một content version hợp lệ là phép nối giữa:

```text
PostgreSQL version record
  + immutable S3/R2 object
  + verified SHA-256 and byte size
```

Obsidian không phải backend source of truth. Nó là client có working copy mà người dùng kiểm soát. Web App là client thứ hai của cùng canonical backend.

## 2. Content-addressable storage

Object key không dựa vào path hoặc title:

```text
objects/sha256/{first_2}/{next_2}/{sha256}
```

- Object immutable; không overwrite cùng key với bytes khác.
- SHA-256 tính trên exact uploaded bytes.
- Byte size và media type được verify trước khi publish version.
- Trùng bytes giữa nhiều source/version được deduplicate tự nhiên.
- Encryption, retention và lifecycle do bucket policy quản lý.

## 3. Stable identities

```text
user_id             UUID
workspace_id        UUID
device_id           UUID
source_id           UUID ổn định qua rename/move
source_version_id   UUID cho immutable version
event_id            UUID cho sync/write event
chunk_id            deterministic ID trong source version lineage
```

Path, URL, title và filename là mutable attributes.

## 4. Source model

```text
source_id
workspace_id
source_type
title
locator
media_type
current_version_id
sync_state
policy_id
created_at
updated_at
deleted_at
```

`source_type` ban đầu gồm `markdown`, `text`, `pdf`, `image`, `audio`, `web`, `youtube`.

## 5. Version model

```text
source_version_id
source_id
content_hash
object_key
byte_size
media_type
content_version
parent_version_id
author_kind
author_id
client_timestamp
committed_at
```

`content_version` tăng đơn điệu trong phạm vi source. Optimistic concurrency dùng `base_version_id`, không dùng timestamp để quyết định thắng thua.

## 6. Derived artifacts

OCR text, transcript, parsed document, thumbnail và extracted metadata có thể lưu trong R2 nhưng luôn là derived artifacts:

```text
artifacts/{source_version_id}/{artifact_kind}/{pipeline_hash}
```

Mỗi artifact tham chiếu source version, provider/model/version và input hash. Xóa artifact không làm mất canonical source.

## 7. Write paths

### Obsidian human edit

```text
local save → plugin event → upload/verify bytes
→ PostgreSQL version commit → projection workflow
```

### Web App human edit

```text
load base version → edit → explicit save
→ optimistic concurrency check → object commit
→ new canonical version → plugin pull/apply
```

Explicit save của người dùng là approval cho chính nội dung họ đang sửa.

### AI-proposed edit

```text
proposal → policy check → diff → user approval
→ canonical commit → plugin pull/apply → sync confirmation
```

AI không ghi trực tiếp vào Vault filesystem.

## 8. Deletion

Delete là tombstone trước, physical GC sau:

1. Ghi source tombstone và audit event.
2. Projection workflow loại source khỏi active retrieval/graph.
3. Giữ version/object theo retention.
4. GC chỉ xóa object không còn reference, không có hold và đã qua grace period.

## 9. Recovery rule

Qdrant, Neo4j, Redis, logs hoặc Temporal history không bao giờ được dùng để phát minh lại canonical content. Khi canonical object thiếu, hệ thống đánh dấu integrity failure và yêu cầu resync/restore từ canonical backup.
