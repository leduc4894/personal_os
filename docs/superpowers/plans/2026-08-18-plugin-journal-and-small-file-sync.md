# Plugin Journal and Small-File Sync Implementation Plan

> **For agentic workers:** Implement one task at a time. For every behavior,
> first add the named failing test, run and read its failure, then make the
> smallest change that passes it. Do not start child 5/6/7 work while executing
> this plan.

**Goal:** Add a portable Obsidian SQLite journal, bounded offline create/update
queue, authenticated API-mediated small-file upload through 16 MiB, exact
replay and minimal local controls, while keeping Vault content and canonical
publication safe.

**Architecture:** The plugin owns a `sql.js` WebAssembly journal persisted as
verified `DataAdapter` generations; Vault remains the byte authority. A
framework-neutral small-file sync domain owns preflight operation identity and
publication orchestration. PostgreSQL owns durable operation/replay state; the
existing source-publication service remains the only path that turns verified
bytes into canonical source versions. FastAPI owns device authentication,
strict API schemas and OpenAPI. No direct-to-R2 client route is added.

**Normative spec:**
`docs/superpowers/specs/2026-08-18-plugin-journal-and-small-file-sync-design.md`.
The auth/device and exclusion-policy plans must be complete before this work.

## Global constraints

- Implement only child 4. Do not implement locator/tombstone lifecycle,
  rename/move/delete/restore mutations, pull/cursor/reconciliation, remote
  apply, multipart, Worker routing, conflict resolution or client-side
  encryption.
- The supported local operation is create/update of a regular,
  policy-allowed file with `size_bytes <= 16 * 1024 * 1024`. The backend owns
  the threshold check; the plugin never treats its local check as authority.
- Use `sql.js` WebAssembly with Obsidian `DataAdapter.readBinary` and
  `DataAdapter.writeBinary`. Do not import Node built-ins, Electron,
  `FileSystemAdapter`, native SQLite or an ORM into the plugin.
- Journal records contain no raw file bytes, access/refresh credentials, raw
  policy rules, provider identifiers or secrets. Paths/digests may stay in
  local SQLite but are never emitted to diagnostics.
- The existing `SourceVersionPublicationService` and server-only
  `VerifiedObjectReceipt` boundary are reused. The plugin never receives an
  R2 key, presigned URL, receipt or canonical object-store detail.
- API requests derive device, user and workspace from the opaque bearer token;
  no request body chooses a workspace. Reuse the canonical envelopes, closed
  error vocabulary, deterministic OpenAPI and generated API client workflow.
- A server preflight may reserve an internal UUID for a future create operation
  but must not insert a `sources` row until bytes have been fully verified and
  publication commits. The client never mints a canonical `source_id`.
- `excluded` and `indeterminate` local policy outcomes fail closed.
  `excluded_policy`, `blocked_size`, `blocked_conflict`,
  `deferred_lifecycle` and `integrity_failed` never automatically retry.
- Queue work is foreground and bounded. It starts only on plugin load after
  recovery, a Vault event or `Sync now`; it must end on unload/suspend and may
  have one active content request.
- Preserve existing user changes. Every database change has Alembic upgrade and
  downgrade tests. Every API change updates OpenAPI, generated client and
  contract tests. Python is mypy-strict and TypeScript strict.

## Deliverable structure

```text
apps/obsidian-plugin/src/
├── journal/
│   ├── contracts.ts             Local closed states, records and safe outcomes
│   ├── fingerprint.ts           Browser-compatible SHA-256 and file metadata
│   ├── sqlite-database.ts       sql.js adapter; one-writer transaction boundary
│   ├── persistence.ts           Generation/manifest verification and recovery
│   ├── repository.ts            Journal mutation/query implementation
│   ├── capture.ts               Settled create/modify capture and explicit scan
│   ├── queue-driver.ts          Bounded foreground preflight/upload/retry loop
│   ├── sync-api.ts              Authenticated preflight/raw-content transport
│   └── status.ts                Redacted status projection and command contracts
└── plugin.ts                    Composition only: adapters, listeners, commands

src/personal_os/small_file_sync/
├── contracts.py                 Typed preflight, operation and replay values
├── errors.py                    Closed sync API/domain errors
├── ports.py                     Operation store, clock and publication ports
├── service.py                   Preflight and receive orchestration
└── metrics.py                   Closed redacted metric labels

packages/postgresql-source-store/src/postgresql_source_store/
└── small_file_sync_operations.py Durable operation/replay transaction adapter

apps/api/src/api_runtime/
├── small_file_sync_composition.py
├── small_file_sync_models.py
└── small_file_sync_routes.py
```

## Task 1: Establish the portable journal dependency and plugin boundaries

**Files:**

- Modify: `apps/obsidian-plugin/package.json`, `pnpm-lock.yaml`,
  `apps/obsidian-plugin/README.md`
- Create: `apps/obsidian-plugin/src/journal/contracts.ts`,
  `apps/obsidian-plugin/src/journal/contracts.test.ts`
- Modify: mobile/bundle-boundary contract tests that currently guard plugin
  imports.

**Tests first:** Add tests proving the closed event states and safe error labels
reject unknown values, and a static/bundle contract that permits the selected
WASM package but rejects Node, Electron, `FileSystemAdapter`, native SQLite and
credential/path/hash sentinels.

**Implementation:** Add one pinned, reviewed `sql.js` production dependency
after recording its exact version, license, WASM asset/bundling behavior and
zero native runtime requirement in the README. Add only immutable TypeScript
types for `JournalEvent`, `JournalEventState`, `LocalFile`, `FrozenFingerprint`,
`JournalMeta`, `QueueOutcome` and safe errors. Freeze the 16 MiB, 10,000-row,
64 MiB and 250 ms constants in this module. Do not instantiate SQLite yet.

**Verify:**

```powershell
pnpm --filter @workspace/obsidian-plugin test -- contracts
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
uv run pytest tests/contract/api/test_plugin_authentication_bundle.py -q
```

Commit: `feat: define portable sync journal contracts`

## Task 2: Implement verified SQLite generations and recovery

**Files:**

- Create: `apps/obsidian-plugin/src/journal/sqlite-database.ts`,
  `persistence.ts`, `persistence.test.ts`, `sqlite-database.test.ts`
- Modify: `apps/obsidian-plugin/src/plugin.ts` only to inject a narrow
  `DataAdapter`/plugin-data directory adapter; do not add behavior there.

**Tests first:** Test an empty first open, schema migration, serialized writer,
valid manifest load, torn generation, digest/size mismatch, missing newest
generation, fallback to the prior verified generation and no valid generation.
The final case must set `reconcile_required` and leave a supplied Vault fake
untouched.

**Implementation:** Use the Vault's configured plugin directory, never a
hard-coded `.obsidian` string. Persist each committed SQLite image as
`journal.sqlite.g<generation>`, read back and verify SHA-256/size, then publish
a verified manifest. Retain current plus one prior verified generation only
after the new manifest is valid. Expose one serialized async writer; buffer a
bounded number of path notifications during recovery and set
`reconcile_required` on overflow.

**Verify:**

```powershell
pnpm --filter @workspace/obsidian-plugin test -- persistence sqlite-database
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
```

Commit: `feat: persist portable journal generations`

## Task 3: Add journal repository, immutable fingerprints and coalescing

**Files:**

- Create: `apps/obsidian-plugin/src/journal/repository.ts`,
  `fingerprint.ts`, `repository.test.ts`, `fingerprint.test.ts`
- Modify: `sqlite-database.ts`, `contracts.ts`

**Tests first:** Cover SHA-256/byte-size/media-type derivation, a create with no
source ID, an update with source/base IDs, same-file replacement while queued,
replacement while waiting retry before preflight, successor creation after
preflight freeze, terminal-state retention, queue count/size limits and
redacted attempted-event history.

**Implementation:** Implement the schema from spec section 6: `journal_meta`,
`local_files`, `journal_events` and bounded `journal_attempts`. Generate random
plugin-local file IDs and stable event/idempotency UUIDs. Allow coalescing only
before an event enters `preflight`; persist a successor for any later edit.
On either soft limit, persist `reconcile_required`, retain in-flight evidence
and refuse only new per-change rows—not the user's edit.

**Verify:**

```powershell
pnpm --filter @workspace/obsidian-plugin test -- repository fingerprint
pnpm --filter @workspace/obsidian-plugin type-check
```

Commit: `feat: add durable local sync journal`

## Task 4: Add capture, policy gating and explicit existing-file scan

**Files:**

- Create: `apps/obsidian-plugin/src/journal/capture.ts`, `capture.test.ts`
- Modify: `apps/obsidian-plugin/src/exclusion-policy/policy-session.ts` only
  through a narrow existing public query or a new tested safe adapter method;
  `apps/obsidian-plugin/src/plugin.ts`

**Tests first:** With fake Vault files/events and fake policy outcomes, prove
250 ms per-path settling, regular-file filtering, allowed ≤16 MiB enqueue,
one-byte-over limit to `blocked_size`, excluded/indeterminate to
`excluded_policy`, no bytes leave the device when denied, startup does not scan, and
confirmed `Sync existing files` processes a bounded snapshot.

**Implementation:** Register create/modify listeners after journal recovery.
Capture always re-reads bytes before fingerprinting and persists the accepted
policy revision. Add a command that confirms before a batched snapshot scan;
do not add periodic scanning. Observe lifecycle notifications only to mark the
matching entry `deferred_lifecycle`, stop its network work and prevent a path
rebind or inferred lifecycle mutation. Do not implement lifecycle HTTP calls.

**Verify:**

```powershell
pnpm --filter @workspace/obsidian-plugin test -- capture exclusion-policy
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
```

Commit: `feat: capture policy-allowed small-file changes`

## Task 5: Define framework-neutral server sync contracts and errors

**Files:**

- Create: `src/personal_os/small_file_sync/__init__.py`, `contracts.py`,
  `errors.py`, `ports.py`, `metrics.py`,
  `tests/unit/small_file_sync/test_contracts.py`, `test_errors.py`
- Modify: `src/personal_os/error_contracts/codes.py`, package exports and
  registry tests.

**Tests first:** Prove strict UUID/operation shape, create versus update field
requirements, 16 MiB equality/overage, printable idempotency rules, opaque
operation token grammar, terminal outcome mapping and exceptions that never
echo locator, digest, token or payload sentinels.

**Implementation:** Define typed `SmallFilePreflight`,
`SmallFileUploadOperation`, `SmallFilePreflightOutcome` and committed replay
result values. Add only closed errors needed for malformed preflight,
operation-not-found/expired, operation identity mismatch, size limit,
content-integrity failure and upload state invalid. Keep FastAPI, SQLAlchemy,
R2 and request types out of the domain package.

**Verify:**

```powershell
uv run pytest tests/unit/small_file_sync tests/unit/error_contracts -q
uv run ruff check src/personal_os/small_file_sync tests/unit/small_file_sync
uv run mypy src/personal_os/small_file_sync
```

Commit: `feat: define small-file sync domain contracts`

## Task 6: Add durable upload-operation schema and PostgreSQL adapter

**Files:**

- Create: `migrations/versions/20260818_01_add_small_file_sync_operations.py`,
  `packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py`
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/tables.py`,
  `backup_snapshot.py`, package exports
- Create/modify: migration metadata, backup and store unit/integration tests.

**Tests first:** Verify upgrade/downgrade, exact metadata match, operation
uniqueness by workspace/device/event/idempotency, payload fingerprint mismatch,
expired non-terminal operation, concurrent preflight, replay after commit and
no `sources` row created merely by create preflight.

**Implementation:** Add one schema-qualified `small_file_upload_operations`
table. It stores derived workspace/device, event/idempotency, operation kind,
declared fingerprint/size/media type, policy revision, nullable reserved
server-generated source ID for a create, nullable update source/base IDs,
state, expiry and terminal canonical result fields. It never stores bytes,
raw path, token, receipt or provider key. Add constraints/indexes and a
transaction adapter that locks operation identity, returns an exact terminal
result, reserves a create UUID without inserting `sources`, and persists the
publication result atomically with operation terminal state.

**Verify:**

```powershell
uv run pytest tests/unit/migrations tests/contract/source_publication/test_table_metadata.py tests/unit/postgresql_source_store -q
uv run pytest tests/integration/source_publication -q
uv run poe python-lint
uv run poe python-type-check
```

Commit: `feat: persist small-file sync upload operations`

## Task 7: Orchestrate preflight, receive and canonical publication

**Files:**

- Create: `src/personal_os/small_file_sync/service.py`,
  `tests/unit/small_file_sync/test_service.py`, fakes
- Modify: `src/personal_os/sources/publication.py` only through a narrow
  adapter/port if required; `packages/r2-object-storage` only if its existing
  bounded spool/verification port cannot already serve the service.

**Tests first:** Prove local/server policy denial before object-store access,
exact preflight replay, pending same-identity return, different payload
rejection, create reservation without source insert, stale base conflict,
no-change, content mismatch non-publication, response-loss replay and one
publication under concurrent receive calls.

**Implementation:** Build `SmallFileSyncService` over operation store, policy
guard, existing publication service and aware clock. Preflight authenticates
through a typed device context supplied by the API adapter, rechecks policy and
base, and either returns a terminal replay/outcome or a short-lived opaque
operation. Receive binds to that exact operation, checks expiry/state and uses
the existing server-side bounded spool/CAS/full verification path. Only a
verified receipt flows into `SourceVersionPublicationService`. Its PostgreSQL
transaction result is then written as the operation's replayable terminal
receipt. Do not serialize an internal receipt to HTTP.

**Verify:**

```powershell
uv run pytest tests/unit/small_file_sync tests/unit/sources/test_publication_service.py -q
uv run pytest tests/contract/source_publication/test_no_public_api.py tests/contract/source_publication/test_telemetry_leakage.py -q
uv run mypy src/personal_os/small_file_sync
```

Commit: `feat: orchestrate verified small-file publication`

## Task 8: Compose authenticated API routes and regenerate the client

**Files:**

- Create: `apps/api/src/api_runtime/small_file_sync_composition.py`,
  `small_file_sync_models.py`, `small_file_sync_routes.py`
- Modify: `application.py`, `server.py`, `openapi_export.py` if composition
  needs an explicit export seam
- Modify generated artifacts: `packages/api-client/openapi.json`,
  `packages/api-client/src/generated/schema.ts`
- Create/modify: API runtime and API contract tests.

**Tests first:** Test that both routes demand an active `obsidian_sync` device,
derive workspace/device server-side, reject malformed/unexpected JSON, map each
closed outcome to the canonical envelope/status, reject body larger than 16 MiB
without publishing, and never expose receipt/object/provider fields. Snapshot
the semantic operation IDs.

**Implementation:** Register only:

```text
POST /api/sync/journal-events/preflight
PUT  /api/uploads/{operation_id}/content
```

Use strict Pydantic models and the existing device bearer dependency pattern
from policy plugin routes. The content route consumes raw bytes, applies an
explicit request deadline/size limiter, and returns a canonical terminal data
envelope. Reuse typed error handlers; do not add CORS, callback URLs,
presigned URLs or a route that accepts a verified receipt. Export OpenAPI then
regenerate—not hand-edit—the TypeScript schema.

**Verify:**

```powershell
uv run pytest tests/unit/api_runtime tests/contract/api -q
uv run --package api-runtime personal-api export-openapi --output packages/api-client/openapi.json
pnpm --filter @workspace/api-client run generate
uv run poe api-contract-check
pnpm --filter @workspace/api-client type-check
```

Commit: `feat: expose authenticated small-file sync API`

## Task 9: Implement plugin preflight/content transport and queue driver

**Files:**

- Create: `apps/obsidian-plugin/src/journal/sync-api.ts`, `queue-driver.ts`,
  `sync-api.test.ts`, `queue-driver.test.ts`
- Modify: `apps/obsidian-plugin/src/api/request-url-transport.ts` only if a
  tested raw `ArrayBuffer` request path is required; `plugin.ts` composition.

**Tests first:** Prove correct bearer/header/body construction, no auto retry in
the transport, oldest eligible first, one active request, preflight freezes the
event, client re-fingerprint before send, lost response preflight replay,
offline/timeout/429/5xx backoff (1 second through 5 minutes with jitter), one
refresh maximum per pass, logout/revoke queue preservation and no run after
unload/suspend.

**Implementation:** Hand-mirror only these sync wire shapes in the plugin,
because the generated client is intentionally not bundled into Obsidian. Use
the existing token session for access/one refresh and `requestUrl` through a
pure injected adapter. Read at most the supported 16 MiB file into the
platform-safe request body; enforce one active operation and discard late
`requestUrl` results after the driver deadline because it is not abortable.
Persist every state transition before the next network action. Map server
outcomes to journal terminal states without interpreting provider details.

**Verify:**

```powershell
pnpm --filter @workspace/obsidian-plugin test -- sync-api queue-driver
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
```

Commit: `feat: run bounded small-file sync queue`

## Task 10: Wire commands, status and safe unload behavior

**Files:**

- Create: `apps/obsidian-plugin/src/journal/status.ts`, `status.test.ts`
- Modify: `apps/obsidian-plugin/src/plugin.ts`,
  `apps/obsidian-plugin/src/authentication/settings-tab.ts`, related tests and
  `apps/obsidian-plugin/README.md`.

**Tests first:** Test the closed status projection, `Sync now`, confirmation
before `Sync existing files`, Login required action, blocker text, no raw
path/hash/token in status telemetry fixture, listener disposal, journal flush
attempt on unload and a suspended pass that remains resumable.

**Implementation:** Keep the composition root thin. Register the two commands,
create/modify/lifecycle guard listeners and a small status surface. Existing
authentication settings remain authoritative for login; do not add a second
credential store. On unload, stop listeners/driver, persist a final journal
generation when possible and clear memory-only access state. Never show a
button that implies automatic full-Vault upload.

**Verify:**

```powershell
pnpm --filter @workspace/obsidian-plugin test
pnpm --filter @workspace/obsidian-plugin type-check
pnpm --filter @workspace/obsidian-plugin lint
uv run pytest tests/contract/api/test_plugin_authentication_bundle.py -q
```

Commit: `feat: add small-file sync plugin controls`

## Task 11: Cross-boundary integration, device evidence and operations

**Files:**

- Create: `tests/integration/small_file_sync/`,
  `tests/contract/api/test_small_file_sync_openapi.py`,
  `tests/contract/small_file_sync/`
- Modify: relevant source-publication and API leakage tests
- Create: `docs/operations/plugin-journal-small-file-sync.md`
- Modify: `docs/operations/exclusion-policy-device-verification.md` only to
  add sanitized child-4 reference-device evidence after the tests exist.

**Tests first:** Add end-to-end fixture coverage for offline create/update then
reconnect, exact replay after dropped response, 16 MiB boundary, denied policy,
server policy change during upload, stale update base, changed local bytes,
generation recovery, queue-cap flag, lifecycle deferral and revoked device.
Use disposable, guarded test infrastructure only; never a personal stack.

**Implementation:** Add no new product behavior. Document startup, queue state,
safe diagnostics, recovery generation selection, `reconcile_required`, size
block, policy block and operator evidence procedure. Perform Desktop and Mobile
test-Vault checks following the existing runbook, storing only sanitized
outcomes and no file names/content/digests/tokens.

**Verify:**

```powershell
uv run pytest tests/integration/small_file_sync tests/contract/small_file_sync tests/contract/api/test_small_file_sync_openapi.py -q
pnpm --filter @workspace/obsidian-plugin test
pnpm --filter @workspace/obsidian-plugin build
uv run poe python-lint
uv run poe python-type-check
pnpm --recursive run lint
pnpm --recursive run type-check
uv run poe api-contract-check
```

Commit: `test: verify small-file sync boundaries`

## Task 12: Final acceptance, documentation and handoff

**Files:**

- Modify: `docs/20-IMPLEMENTATION_PLAN.md` only if completion changes its
  canonical status; `docs/operations/plugin-journal-small-file-sync.md`
- Create: exactly one
  `docs/handoff/2026-08-18-plugin-journal-small-file-sync.md`
- Modify: `docs/handoff/BACKLOG.md` only for a genuinely deferred item that
  lacks its existing child-spec owner.

**Acceptance checklist:**

- [ ] Journal writes and recovers verified SQLite generations on Desktop and
  Mobile without native APIs.
- [ ] No automatic initial scan or background retry exists.
- [ ] Queued changes coalesce only before preflight; frozen content gets a
  successor event.
- [ ] An allowed file up to 16 MiB publishes exactly once through the API.
- [ ] Server verifies bytes before canonical publication and response loss
  exactly replays the original outcome.
- [ ] Offline/auth/policy/size/conflict/lifecycle outcomes preserve Vault data
  and follow their closed journal state.
- [ ] No sensitive value appears in generated output, logs, test reports or
  sanitized device evidence.
- [ ] All focused, migration, integration, API snapshot/generated-client,
  plugin build/lint/type and reference-device gates pass from one commit.

Record the final commit SHA, every gate and its evidence, decisions needed to
interpret the spec, deferred child-owned work and next action in the single
handoff. Do not add a second handoff for this plan.
