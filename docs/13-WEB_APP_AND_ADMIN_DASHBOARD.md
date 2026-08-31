# Web App and Admin Dashboard

## 1. Product shape

Một Next.js application chứa hai khu vực dùng chung auth, API client và design system:

```text
Workspace       notes, sources, search, graph, editor, proposals
Administration sync, policies, schema, workflows, providers, health
```

Không xây hai frontend riêng trong giai đoạn đầu.

## 2. Workspace features

- Global hybrid search với filters/facets.
- Source library cho Markdown, text, PDF, image, audio, web và YouTube.
- Markdown editor bằng CodeMirror với frontmatter support.
- Read-only viewers cho binary sources và transcripts.
- Graph explorer bằng Cytoscape.js.
- Citation viewer tới lines/pages/timestamps.
- Version history, diff và conflict resolution.
- AI context preview và proposal review.
- Sync/projection status rõ ràng trên từng source.

## 3. Editing contract

- Editor load exact `base_version_id`.
- Draft autosave vào IndexedDB, không tự commit canonical version.
- User bấm Save để commit bằng optimistic concurrency.
- Stale base mở conflict UI; không overwrite.
- Remote Obsidian edit được invalidate qua SSE hint bằng local effect-based fetch trên generated API client.
- Binary source không chỉnh bytes trực tiếp trong phase đầu; metadata và transcript correction có workflow riêng.

## 4. Admin features

### Policy and exclusions

Quản lý folder/path glob, source type, size, metadata predicate, AI access và retention. Có preview “rule này ảnh hưởng source nào” trước publish.

### Metadata schema

Quản lý fixed mappings, flexible property definitions, type, filter mode, sensitivity và registry revisions. Publish có diff và impact analysis.

### Projection operations

Xem active deployment, generation health, rebuild/verify/activate/rollback giữa verified generations và exact cleanup. UI không cho nhập arbitrary collection name.

### Workflow operations

Xem progress, failures, retry classification, cancel và repair action. UI gọi application API, không gọi Temporal trực tiếp.

### Provider and cost

Chọn cloud/local provider theo policy, test connectivity bằng redacted probe, xem usage/latency/error budget. Secrets nhập qua secret management flow và không được đọc lại plaintext.

### Observability links

Deep-link theo request ID/trace ID tới Grafana hoặc Sentry, không nhúng admin credentials vào URL.

## 5. Frontend architecture

```text
app/                 route groups and layouts
features/            source, search, graph, policy, schema, workflow
components/ui/       shared accessible primitives
lib/api/             generated client and adapters
lib/auth/            session helpers
lib/events/          SSE client
lib/offline/         Dexie drafts/cache
```

Feature modules không import database/Qdrant/Temporal concepts ngoài public API DTOs.

## 6. State strategy

- Generated API client + local effect-based fetch: server state, invalidation, retry — no data library mandated (ratified 2026-08-31, spec 17 amendment).
- URL: shareable search filters, selected source/tab.
- React local state: component interaction.
- Zustand: chỉ cross-feature ephemeral UI state thực sự cần.
- Dexie: drafts và bounded offline cache.
- Không duplicate server records lâu dài trong Zustand.

## 7. Security

- HTTP-only, Secure, SameSite session cookie.
- CSRF protection cho writes.
- Strict Content Security Policy.
- Markdown rendered bằng sanitizer; HTML/script disabled mặc định.
- Signed short-lived download URLs.
- Admin destructive action yêu cầu typed confirmation và recent re-auth khi phù hợp.
- Không đưa note content vào frontend analytics hoặc Sentry breadcrumbs.

## 8. Accessibility and UX

- Keyboard navigation, focus states và screen-reader labels.
- Search usable hoàn toàn bằng bàn phím.
- Progress phân biệt queued/running/retrying/failed/completed.
- Error message có action khắc phục và request ID.
- Mobile layout hỗ trợ đọc, tìm và approve; graph/editor nâng cao ưu tiên desktop.

## 9. Tests

- Vitest cho state/formatting/validation.
- Testing Library + MSW cho feature flows.
- Playwright cho login, search, edit conflict, policy publish, proposal approve và rebuild monitor.
- Generated client compile chống API drift.
- Accessibility smoke checks trên critical routes.
