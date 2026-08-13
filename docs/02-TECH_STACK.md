# Technology Stack

## 1. Core stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI, Pydantic |
| Domain persistence | SQLAlchemy, Alembic |
| Canonical state | PostgreSQL |
| Canonical bytes | Private Cloudflare R2 buckets, tách production và test/CI |
| Search projection | Qdrant |
| Graph projection | Neo4j |
| Durable workflows | Temporal Python SDK |
| Cache and rate limit | Redis |
| MCP | Official MCP Python SDK |
| Obsidian plugin | TypeScript strict mode, Obsidian API |
| Package/runtime | `uv` cho Python, `pnpm` cho TypeScript |
| Containers | Docker Compose; reverse proxy bằng Traefik hoặc Caddy |

Không dùng floating `latest` tag trong production. Exact versions được pin trong lockfile và deployment manifest.

### Phase 1 infrastructure baseline

Baseline được kiểm tra ngày 2026-08-12, ưu tiên current patched release hoặc LTS khi current feature release chưa tích lũy đủ patch:

| Component | Version | Rationale |
|---|---|---|
| PostgreSQL | `18.4` | Current supported minor của PostgreSQL 18 |
| Qdrant | `1.18.2` | Current patched release |
| Neo4j Community | `5.26.28` LTS | Single-instance Community Edition, LTS thay cho monthly current release |
| Redis Open Source | `8.6.4` | Patched GA release; Phase 1 không cần tính năng mới chỉ có trong 8.8 |
| Temporal Server | `1.31.2` | Current patched server release |
| Temporal UI | `2.53.0` | Current UI release tương thích Temporal Server 1.31.2 |
| Temporal CLI | `1.8.0` | Current administrative CLI release |

Compose và deployment manifest pin exact tag cùng image digest. Nâng version đi qua pull request riêng, review release notes, compatibility/migration tests và rollback evidence; không tự động nâng production. Cloudflare R2 là managed dependency ngoài Compose và được kiểm soát bằng bucket-scoped credentials cùng live compatibility tests.

## 2. Web App

| Nhu cầu | Technology |
|---|---|
| Framework | Next.js App Router + React + TypeScript strict |
| Styling | Tailwind CSS |
| Accessible components | shadcn/ui + Radix UI |
| Markdown editor | CodeMirror 6 |
| Server state | TanStack Query |
| Local UI state | Zustand tối thiểu |
| Forms | React Hook Form + Zod |
| Offline cache/drafts | IndexedDB qua Dexie |
| Generated API client | OpenAPI TypeScript generator |
| Progress/events | Server-Sent Events |
| Graph visualization | Cytoscape.js |
| Unit/component tests | Vitest + Testing Library + MSW |
| End-to-end tests | Playwright |

## 3. AI and retrieval providers

### Dense embedding

```text
Provider: OpenAI
Model: text-embedding-3-small
Dimension: 1536
```

Dense calls chỉ được gửi khi source policy cho phép cloud AI.

### Sparse encoding

```text
Runtime: FastEmbed
Model: Qdrant/bm25
Execution: local CPU
```

### Reranking

- Mặc định: Cohere `rerank-multilingual-v3.0`.
- Local fallback và evaluation arm: Jina multilingual base reranker chạy ONNX qua FastEmbed.
- Nếu reranker không dùng được: giữ fused order, ghi degraded reason; không gọi provider khác trái policy.

### OCR

- Text extraction native trước đối với PDF có text layer.
- OCR fallback: PaddleOCR, server text detector và multilingual Latin recognizer hỗ trợ tiếng Việt.
- Lazy-load trong worker; không giữ model thường trực nếu không có job.

### Speech-to-text

- Provider interface cho cloud transcription và local transcription.
- Local fallback: `faster-whisper`, model `large-v3-turbo`, Silero VAD, word timestamps.
- YouTube ưu tiên caption hợp lệ; chỉ transcription khi caption thiếu hoặc không đạt quality gate.

### LLM

Mọi LLM call qua `LLMProvider`; prompt, model, temperature và output schema được version hóa. Provider client không xuất hiện trực tiếp trong domain modules.

## 4. Provider interfaces

```text
EmbeddingProvider
SparseEncoderProvider
RerankerProvider
OCRProvider
SpeechToTextProvider
LLMProvider
EntityExtractor
RelationExtractor
```

Mỗi result có provider/model/version, latency, usage, retry count và policy decision; không ghi raw content vào telemetry.

## 5. Observability

| Signal | Technology |
|---|---|
| Exceptions/crashes | Sentry Cloud, errors-only |
| Metrics/alerts | Prometheus + Alertmanager |
| Dashboards | Grafana |
| Logs | Grafana Loki |
| Traces | OpenTelemetry + Grafana Tempo |
| Collection/gateway | Grafana Alloy |
| Host/container metrics | node exporter + cAdvisor hoặc tương đương |
| Notifications | Telegram chính, email cho critical |

## 6. Testing

| Scope | Technology |
|---|---|
| Backend | pytest, pytest-asyncio |
| Static quality | Ruff, mypy strict |
| Integration | Testcontainers hoặc disposable Docker stack |
| Temporal | Temporal testing environment + live namespace tests |
| API contracts | OpenAPI snapshot + generated-client compile |
| Load | k6 |
| Frontend | Vitest, MSW, Playwright |
| Plugin | TypeScript test runner + Obsidian contract fixtures |

## 7. Deliberate exclusions

- Không dùng PostgreSQL full-text làm retrieval engine chính.
- Không dùng Neo4j làm canonical application database.
- Không dùng Redis queue thay Temporal cho durable workflows.
- Không chạy model server riêng khi API provider đáp ứng policy và chất lượng.
- Không tách microservices trước khi benchmark chứng minh cần scale/isolation riêng.
