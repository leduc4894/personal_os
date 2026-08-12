# Risks and Capacity Triggers

## 1. Data integrity risks

### PostgreSQL pointer and object diverge

Mitigation: publish protocol verify object trước transaction, periodic inventory reconcile, restore-tested manifests và fail-closed reads.

### Concurrent Obsidian/Web edits

Mitigation: exact base version, three-way conflict, atomic client apply và no silent last-write-wins.

### Projection drift

Mitigation: deterministic manifests, canonical expected side, periodic reconcile, fencing và wipe/rebuild drills.

## 2. Retrieval risks

### Multilingual quality insufficient

Mitigation: Vietnamese/English/cross-language golden corpus; evaluate dense/reranker changes before switching.

### Flexible filters become slow

Mitigation: typed on-disk shared indexes, per-property usage metrics without high-cardinality labels, benchmark then selectively promote hot physical fields.

### One collection grows too large

Do not split by source type. Consider sharding/collection split only when measured latency, maintenance time hoặc storage isolation fails targets. Preserve cross-source query through retrieval orchestration if split becomes necessary.

## 3. Provider risks

### Cloud outage/rate limit

Mitigation: Temporal bounded retry, sparse-only degraded retrieval, provider usage dashboards và background ingestion backlog.

### Privacy leak

Mitigation: policy before payload construction, local-only tests, content scrubbers, minimum context and auditable provider call metadata.

### Cost growth

Mitigation: content-hash cache, incremental indexing, batching, transcript/OCR only when necessary and monthly budget alerts.

## 4. Operational risks

### Too many stateful systems for one person

Mitigation: modular monolith, Compose, projections rebuildable, automated backup/restore and avoid optional services until their milestone.

### Host B failure hides alerts

Mitigation: Host A heartbeat check plus Telegram path; use external dead-man service only if outage notification guarantee becomes necessary.

### Disk exhaustion

Mitigation: R2 for bytes/backups, bounded telemetry retention, disk forecast alerts, compaction headroom and exact cleanup workflows.

## 5. Capacity triggers

| Signal | Trigger | Action |
|---|---|---|
| Host A memory | >80% sustained 15m, swapping hoặc OOM | Tune caches, then raise 16→32 GB |
| Host A disk | >70% or <60 days forecast | Inspect growth/GC, then expand disk |
| Qdrant p95 | Above search SLO with CPU idle and hot-filter pressure | Benchmark index/on-disk settings |
| Index backlog | Cannot catch up within agreed window | Increase worker concurrency within provider limits |
| Local OCR/STT | Queue age unacceptable | Prefer cloud API or add temporary/dedicated GPU worker |
| Host B disk | >70% or retention forecast short | Reduce low-value retention, then expand |
| Alloy memory | Tail buffer pressure/dropped traces | Tune decision window/rate, then add RAM |
| Provider cost | Monthly budget threshold exceeded | Review cache, batch, model and ingestion policy |

## 6. Decision gates

- Do not self-host full Sentry unless cloud policy/cost requires it and Host B sizing is re-evaluated.
- Do not add local dense model until local-only semantic retrieval has a measured need.
- Do not add GPU until OCR/STT benchmark shows API or CPU path violates latency/cost target.
- Do not add Qdrant payload index without real filter frequency/selectivity evidence.
- Do not introduce multi-user tenancy before product scope changes explicitly.
