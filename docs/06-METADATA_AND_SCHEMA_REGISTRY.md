# Metadata and Schema Registry

## 1. Fixed properties

```text
note_type
source_type
knowledge_type
domains
tags
aliases
created
updated
```

`domains` dùng số nhiều vì một source có thể thuộc nhiều domain. Importer chấp nhận alias `domain` nhưng canonical output luôn là `domains`.

## 2. Hierarchical domains and tags

Giá trị canonical dùng slash, lowercase slug và không có slash đầu/cuối:

```text
core_skills/soft_skills/note_taking
```

Projection mở rộng ancestor để filter cha tìm được con:

```json
[
  "core_skills",
  "core_skills/soft_skills",
  "core_skills/soft_skills/note_taking"
]
```

Raw leaf assignment vẫn nằm trong PostgreSQL để UI phân biệt giá trị người dùng nhập và ancestor suy ra.

## 3. Flexible properties

Flexible properties phụ thuộc `note_type`, ví dụ `book.author`, `project.status`, `decision.decided_at` hoặc `trading_journal.risk_reward`. Chúng không trở thành column hoặc Qdrant index riêng cho từng tên property.

## 4. Schema registry

Registry trong PostgreSQL định nghĩa:

```text
property_key
applies_to_note_types
value_type
cardinality
filter_mode
return_mode
normalizer
validation_constraints
sensitivity
registry_revision
```

Value types: `keyword`, `text`, `integer`, `float`, `boolean`, `datetime`, `uuid`, `keyword_list`.

Filter modes: `none`, `exact`, `range`, `text`. Return modes: `omit`, `summary`, `detail`.

## 5. Normalization

Thứ tự ưu tiên:

1. Explicit property của người dùng.
2. Deterministic mapping/alias.
3. Source-derived metadata.
4. AI suggestion có provenance; không overwrite explicit value.

Mỗi normalized value lưu raw value, normalized value, origin, normalizer version và confidence nếu suy ra.

## 6. Unknown properties

Property chưa có registry vẫn được giữ trong PostgreSQL JSONB để không mất dữ liệu, nhưng mặc định không filter trong Qdrant, không gửi vào embedding, không gửi ra AI nếu sensitivity chưa được phân loại và xuất hiện trong Admin Dashboard để người dùng approve schema.

## 7. Semantic filter AST

```json
{
  "and": [
    {"field": "note_type", "op": "eq", "value": "book"},
    {"field": "rating", "op": "gte", "value": 4},
    {"field": "domains", "op": "under", "value": "core_skills"}
  ]
}
```

Server tra registry, validate type/operator rồi compile sang Qdrant. Client không biết physical payload paths.

## 8. Schema changes

- Registry revisions immutable.
- Publishing revision mới có diff và validation.
- Thay physical encoding/index contract yêu cầu collection generation mới.
- Thêm flexible property dùng encoding đã có không tạo index mới.
- Delete property là deprecate trước; canonical raw metadata không tự động bị xóa.
