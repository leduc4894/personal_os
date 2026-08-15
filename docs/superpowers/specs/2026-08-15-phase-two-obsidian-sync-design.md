# Phase 2 Obsidian Sync Design

**Date:** 2026-08-15

**Status:** Approved architecture umbrella; not directly implementation-plannable

**Owning phase:** Phase 2 — Obsidian sync

## 1. Objective

Build a durable, bidirectional synchronization boundary between one canonical
workspace and Obsidian Desktop/Mobile. The backend and protocol support multiple
devices; primary acceptance uses one Vault plus a second device for conflict and
reconciliation cases.

Phase 2 is complete when Markdown, text and binary bytes, source identity,
locator history, tombstones, device cursors, policy outcomes and conflicts remain
correct through offline use, retry, restart and concurrent edits. Searchability
is not a Phase 2 requirement.

This document fixes cross-cutting architecture, boundaries, invariants and the
dependency order for Phase 2. Its scope is intentionally too broad for one
implementation plan, worktree or pull request. Implementation proceeds only
through the child specs in section 17; no phase-wide implementation plan may be
derived directly from this umbrella.

## 2. Canonical context

Phase 1 already provides and has green live gates for:

- repository workspaces, strict typing, lint/test/build gates and diagnostics;
- authenticated local PostgreSQL, Qdrant, Neo4j, Redis and Temporal services;
- the nine-table PostgreSQL canonical baseline;
- the Cloudflare R2 content-addressable store with exact SHA-256/size verification;
- source create, update and no-change publication with optimistic concurrency;
- exact idempotency replay and ambiguous-commit evidence lookup;
- canonical current-source reads;
- durable projection intents and the Temporal dispatcher;
- identity bootstrap, backup, verification and empty-target restore.

Phase 1 deliberately leaves these surfaces as shells:

- `apps/obsidian-plugin/` has no Vault, network, settings or UI behavior;
- `apps/api/` has runtime diagnostics but no public sync/source API;
- `apps/web/` has no login, device or exclusion management product flow;
- source publication implements create/update/no-change, not locator lifecycle,
  tombstones or conflict capture;
- `SourceIngestionWorkflow` starts may be queued, but Phase 3 owns the worker
  implementation.

Phase 2 composes public authenticated services around the existing canonical
core. It does not replace Phase 1 storage, publication, reading, dispatch or
recovery paths.

## 3. Approved decisions

1. Implement dependency-ordered end-to-end vertical slices.
2. Use a server-only application architecture; no Cloudflare Worker or hybrid
   edge route is introduced.
3. Sync Markdown, text and binary bytes. Parsing and indexing remain Phase 3.
4. Support multi-device protocol semantics from the start.
5. Treat Obsidian Mobile as an acceptance target without assuming long-running
   background execution.
6. Use a minimal Web Admin for login, device approval/revocation and exclusions.
7. Use browser-based device authorization with plugin polling; tokens never
   travel in callback URLs.
8. Resolve conflicts in a plugin Conflict Inbox. The Web Admin does not gain an
   editor or conflict UI in this phase.
9. Use API-mediated upload through 16 MiB and resumable multipart staging above
   16 MiB, with a Phase 2 maximum of 100 MiB.
10. Keep PostgreSQL authoritative and SQLite rebuildable.

## 4. Scope

### 4.1 Included

- Password login using Argon2id, secure web sessions and optional TOTP.
- Device authorization, rotating credentials, SecretStorage integration and
  device revoke.
- Stable backend-issued source IDs independent of path.
- Local SQLite file mapping, event journal, cursor and upload session state.
- Authenticated upload, download, sync-event and manifest APIs.
- Create, update, rename, move, delete and restore.
- Locator history and tombstones.
- Server-owned exclusion policies and signed plugin snapshots.
- Cursor polling, gap detection and manifest reconciliation.
- API-mediated single-part and R2 staging multipart upload.
- Atomic remote apply and echo-loop prevention.
- Verified conflict candidate capture and plugin resolution.
- OpenAPI, generated TypeScript client, migrations, security tests, runbooks and
  Desktop/Mobile acceptance evidence.

### 4.2 Excluded

- Markdown parsing, metadata registry, chunking and indexing execution: Phase 3.
- Search/context/MCP read endpoints: Phase 5.
- Web editor and Web conflict UI: Phase 7.
- AI proposals and approved writes: Phase 8.
- Cloudflare Worker, `server_only`/`hybrid_edge` routing revisions and an 8 MiB
  edge threshold.
- MinIO, multiple canonical providers, fallback, dual-write or cutover; ADR-018
  prohibits them.
- Full canonical object garbage collection. Phase 2 cleans exact noncanonical
  multipart staging objects only.
- Client-side encryption, SSE-C and customer-managed keys.
- Mutation testing as a milestone gate.

## 5. Architecture and boundaries

```text
Obsidian plugin                    Minimal Web Admin
  Vault API                         login/device/policy UI
  SQLite journal                    secure browser session
  SecretStorage
         \                           /
          authenticated FastAPI adapters
                         |
       auth / policy / sync / upload / conflict services
                         |
    PostgreSQL + Phase 1 publication/read + Cloudflare R2
                         |
              durable projection intents
                         |
       Phase 3 SourceIngestionWorkflow (not registered yet)
```

FastAPI and Web UI contain no source lifecycle rules. Domain packages define
closed contracts, errors, services and repository ports. PostgreSQL, R2,
cryptography and HTTP libraries remain infrastructure dependencies outside the
domain.

The plugin uses Obsidian `Vault`, `FileManager`, `requestUrl`, `Platform` and
`SecretStorage` APIs through narrow adapters. Mobile-compatible code must not
import Node.js, Electron or `FileSystemAdapter` at module load time.

## 6. PostgreSQL evolution

Phase 2 uses forward Alembic migrations, split by owning vertical slice rather
than one large migration. Every migration has empty upgrade, fixture upgrade
and downgrade tests.

### 6.1 Authentication

- `user_credentials`: user, Argon2id password hash and credential revision.
- `web_sessions`: hashed opaque session ID, idle/absolute expiry, CSRF state and
  revocation.
- `totp_credentials`: encrypted TOTP secret, enrollment state and revision.
- `totp_recovery_codes`: individually hashed, one-use recovery codes.
- `device_authorization_grants`: hashed device code, requested device metadata,
  approval state and ten-minute expiry.
- `device_tokens`: hashed access/refresh material, token family, rotation link,
  inactivity/absolute expiry and revocation reason.

The initial web password is enrolled by a protected internal CLI that reads the
secret interactively or from an exact secret file. It never accepts a password
in an argument, environment variable or loggable configuration value.

### 6.2 Sync and lifecycle

- `source_locators`: source, normalized locator, display locator, validity event
  sequence and current/history state. One current locator is unique per active
  workspace scope.
- `source_tombstones`: source, delete event, retained version/locator, actor and
  restore linkage.
- `device_cursors`: one contiguous applied server event sequence per device.
- `manifest_runs`: device, server checkpoint, policy revision, page/final
  digests, state and bounded expiry.
- `manifest_pages`: run, page number, entry count and digest. Raw paths/content
  are not copied into diagnostics.

Rename and move retain `source_id` and current source version. Rename may update
the mutable source title; move updates only locator state. Both emit immutable
sync events and projection upsert intents referencing the current version so a
future Phase 3 projection can refresh path/title-derived fields.

Delete retains the current version, creates a tombstone, marks the source
`deleted` and emits delete intents. Restore requires an available target
locator, closes the tombstone, returns the source to `active` and emits upsert
intents.

`active` means the canonical source is current and allowed. It does not assert
that a projection is searchable. Phase 3 may later set `stored_not_indexed` for
an unsupported parser or terminal indexing reason.

### 6.3 Upload and conflicts

- `multipart_uploads`: expected digest/size/media type, exact staging key,
  provider upload ID, state, expiry and owning event/device.
- `multipart_parts`: session, part number, expected range, provider ETag and
  completion state.
- `source_conflicts`: originating event, base version, observed remote version,
  verified candidate content object, device, locator state, resolution kind,
  resolution event/version and timestamps.

A captured conflict also inserts a `sync_events` row in the same transaction.
The existing `(workspace_id, idempotency_key)` and event-ID uniqueness therefore
remain the global accepted-operation identity. The conflict row references that
event. Replay lookup checks conflict membership before applying the Phase 1
published/no-change classifier.

Conflict candidates are verified content objects but are not source versions
and never become current implicitly. Phase 2 retains their conflict references;
future full garbage collection owns physical deletion after retention, hold and
reference checks.

## 7. Device-local SQLite

SQLite is a journal, not canonical authority. It stores:

- a local file key and the backend-issued `source_id` mapping;
- normalized path, last observed hash/size and base version;
- stable pending `event_id` and idempotency key;
- event state, attempt count, retry time and safe error code;
- multipart session/part progress without presigned URLs;
- last contiguous applied server event sequence;
- manifest run resume state and policy revision;
- remote-apply operation marker and expected final hash.

Raw file bytes remain in the Vault. SQLite loss or integrity failure clears no
remote state and mints no source identity; it requires a full manifest
reconciliation.

An unsent queued modify may be superseded by a newer local state for the same
source. Delete/restore boundaries are never coalesced. Once preflight or upload
starts, event identity and fingerprint are frozen; a later file change creates a
new event.

The queue accepts at most 10,000 pending rows and uses a 64 MiB SQLite soft cap.
Reaching either bound durably sets `reconcile_required`, preserves in-flight
rows, stops creating per-change rows and retains a dirty Vault generation. After
connectivity returns, the plugin finishes in-flight work and reconciles current
state. It never blocks editing, expires unsynced work or deletes local content.

## 8. Authentication and minimal Web Admin

### 8.1 Web login

- Passwords use Argon2id through a pinned, reviewed library.
- Web sessions use opaque random identifiers; PostgreSQL stores only hashes.
- Web-session idle expiry is 12 hours and absolute expiry is 7 days.
- Cookies are Secure, HttpOnly and SameSite=Lax with bounded idle and absolute
  expiry.
- State-changing forms require origin validation and a per-session CSRF token.
- Login permits at most five failed attempts per username/source bucket in 15
  minutes, with a generic response and bounded lockout.
- TOTP is optional. Its server-managed encryption key comes from an exact secret
  file; recovery codes are individually hashed.

Passkeys and external OIDC remain deferred.

### 8.2 Plugin authorization

1. Plugin requests a device authorization grant and receives a public user code,
   opaque polling secret, verification URL and expiry.
2. Plugin opens the verification URL in the browser.
3. User logs in, reviews device name/platform/scope and approves or denies.
4. Plugin polls no faster than every five seconds and exchanges an approved
   one-use grant.
5. Backend registers the device and returns a 15-minute opaque access token plus
   rotating refresh credential.
6. Plugin stores the refresh credential through Obsidian SecretStorage; plugin
   settings store only the secret name.

Refresh has 30-day inactivity and 90-day absolute expiry. Reuse of a rotated
refresh credential revokes the device token family. Admin revoke immediately
disables the device, its tokens and pending grants while preserving its local
files and queue.

### 8.3 Admin surface

Phase 2 Web Admin contains only:

- login/logout and optional TOTP enrollment/recovery;
- device authorization approval/denial;
- device list, registered/last-seen state and revoke;
- exclusion draft, impact preview, publish and audit.

## 9. Exclusion policy

The server is authoritative. Initial rule kinds are exact source ID, normalized
folder prefix, bounded glob, extension/media type, maximum size and source type.
Property predicates remain inactive until Phase 3 provides canonical metadata.

Publishing creates an immutable policy revision. The plugin receives a bounded
canonical-JSON snapshot with an Ed25519 detached signature to avoid unnecessary
upload, but every preflight and manifest action is reevaluated by the backend.
The signing key comes from an exact server secret file; the public key and key ID
are delivered during authenticated onboarding and rotated by an authenticated
keyset contract. Adding the pinned Ed25519/Argon2 implementation libraries
requires an explicit production-dependency review in the implementation plan.

Unknown revision, invalid signature or evaluation failure defaults to deny.
Changing allow to deny stops upload/ingestion, creates projection delete intents
for an existing source and preserves canonical bytes according to retention.
The plugin keeps the local file.

## 10. Public API groups

Phase 2 adds authenticated endpoints under existing canonical groups:

- `/auth`: login/session/TOTP and device authorization/refresh/revoke;
- `/sync`: preflight, events after cursor, cursor acknowledge and manifest runs;
- `/sources`: lifecycle commands and verified current/candidate download;
- `/uploads`: single-part receive, multipart create/parts/status/complete/abort;
- `/admin`: devices, policy draft/preview/publish and safe status.

Pydantic schemas are the backend source, OpenAPI is snapshot-tested and the
generated TypeScript client compiles in CI. Credentials derive user/workspace/
device scope server-side; request bodies do not select an arbitrary workspace.

All responses use the canonical request envelope and stable safe error codes.
No public response contains `VerifiedObjectReceipt`, R2 credentials, canonical
object key, raw provider exception or database identity not required by the
client contract.

## 11. Upload and download

### 11.1 Preflight

The plugin sends event identity, operation, source/local identity, base version,
locator, digest, byte size, media type and policy revision before bytes.

The server returns exactly one outcome:

- committed replay;
- no-change;
- excluded;
- conflict with candidate-upload/no-upload instructions;
- already-stored verification path;
- API-mediated single-part plan;
- resumable multipart plan.

Preflight checks authentication, device state, policy, idempotency, locator and
base evidence. The transaction rechecks every correctness condition under the
Phase 1 lock order.

### 11.2 Single-part

Objects from 0 through 16 MiB stream to the authenticated API. The API reuses
the Phase 1 bounded spool, conditional CAS write and full verification. Failure
restarts this small upload.

### 11.3 Multipart

Objects above 16 MiB through the existing 100 MiB product maximum use:

- 8 MiB equal parts except the final part;
- at most three concurrent parts on Desktop and two on Mobile;
- short-lived presigned part URLs for one exact noncanonical staging key;
- persisted provider upload ID and part ETags;
- full staging-object SHA-256/size/media verification;
- promotion by streaming verified staging bytes through the Phase 1 conditional
  CAS writer;
- exact staging-key abort/delete after commit or bounded expiry.

Part URLs expire after ten minutes and a multipart session expires after 24
hours. The client never receives a presigned URL targeting an existing canonical key.
Presigned URLs are bearer credentials and never enter SQLite, logs, traces,
errors or reports. The 16 MiB threshold is an initial server-owned routing
constant, not a client invariant; the response plan remains stable if telemetry
later changes the threshold.

### 11.4 Publication

Only the server converts verified bytes into an internal receipt. Publication
then reuses the Phase 1 service and PostgreSQL transaction. No HTTP, Worker,
plugin or Web schema can deserialize a receipt.

### 11.5 Download and apply

Downloads go through a policy-checked canonical read API. The plugin writes a
temporary sibling file, verifies expected digest and size, and atomically
renames only if the local file still matches the recorded base. The final hash
is verified again and an operation marker prevents the watcher from producing
an echo event. A mismatched local base becomes a conflict instead of overwrite.

## 12. Cursor and reconciliation

Each device pulls at most 200 immutable sync events per page after its last
contiguous sequence.
The cursor advances only after an event has been:

- atomically applied;
- proven to be a matching self-origin no-op;
- recorded locally as a conflict; or
- safely handled as tombstone/excluded.

A sequence gap, compacted history, unknown event shape or local invariant
mismatch stops incremental pull and requires reconciliation.

Reconciliation runs after onboarding/SQLite recovery, on cursor gaps, on a
bounded active-app schedule and through an explicit Repair Sync command.

Each manifest run expires after one hour, binds a server checkpoint and policy
revision, and accepts at most 500 entries per ordered page. Pages and the final
manifest carry digests. Finalization returns paginated, deterministic `upload`,
`download`, `apply_tombstone`, `conflict`, `no_change` or `excluded` actions.
Vault events observed during the run remain journaled and replay afterward; the
editor is never frozen.

An offline new file has a SQLite-only local key. During registration/reconcile,
the backend serializes normalized locator identity, resolves simultaneous first
registration and issues the canonical `source_id`. The plugin never invents a
canonical source ID from a path.

## 13. Conflict behavior

A stale base is a durable conflict outcome, not a retryable failure.

1. Preflight observes the stale base and returns a candidate upload plan if the
   candidate object is not already verified.
2. The server verifies the local candidate.
3. One transaction inserts the accepted sync event, conflict record, candidate
   content-object reference and audit event without changing current pointer.
4. Exact retry returns the same conflict.
5. The plugin shows base, observed remote and local candidate in Conflict Inbox.

Markdown/text uses a bounded byte/text three-way diff and editable merge result;
it does not depend on the Phase 3 parser. Choices are keep remote, keep local or
save merged. Binary offers keep remote or keep local only.

Resolution binds conflict ID and the remote version the user reviewed. The
server rechecks current version and policy. If current advanced, resolution is
stale and requires review again. Keep remote marks resolved without a redundant
version. Keep local or save merged publishes one new immutable version against
the reviewed remote and links the result to the conflict.

The same discipline covers local edit versus remote delete, local delete versus
remote edit and concurrent locator collision. There is no automatic binary
merge or silent last-write-wins.

## 14. Error handling

| Class | Required behavior |
|---|---|
| Offline, timeout, 429, temporary outage | keep queue/parts, bounded backoff, no cursor advance |
| Expired access token | refresh once, then require login while preserving queue |
| Revoked device/token reuse | revoke family, preserve local data |
| Stale base | capture conflict; do not network-retry |
| Policy denied | stop upload, mark excluded, preserve local file |
| Cursor/invariant gap | stop incremental pull and reconcile |
| Hash/size/part mismatch | terminal integrity error; never commit/apply |
| Ambiguous commit acknowledgement | Phase 1 evidence lookup/exact replay |
| Unknown failure | fail closed with safe internal code |

Cancellation preserves the journal and completed part records. Mobile suspend
does not count as terminal failure. Retry backoff starts at one second, uses
jitter and caps at five minutes. Retries are bounded and cancellable; provider
SDK retries remain disabled or limited so the owning service controls retry.

## 15. Privacy, security and observability

Never log or emit raw content, path, locator, diff, query, token, cookie, device
code, signed URL, object key, full digest, multipart provider ID/ETag or provider
exception message.

Allowed diagnostics are opaque IDs, shortened digests, counts, durations,
closed operation/state/error labels and platform class. Authentication, device
approval/revoke, policy publish, delete/restore and conflict resolution write
append-only audit events.

Every external call has an explicit deadline and typed mapping. R2 failure
remains fail closed; no provider switch occurs. Public databases and provider
admin surfaces remain prohibited.

## 16. Testing and acceptance

### 16.1 Required gates

- Unit tests for normalization, state machines, queue transitions, policy,
  authentication, token rotation and conflict resolution.
- Property/race tests for idempotency, ordering, locator uniqueness, cursor
  continuity and multipart completion.
- Migration empty upgrade, fixture upgrade and downgrade.
- OpenAPI snapshot and generated TypeScript client compile.
- Plugin contract tests for SQLite crash/restart, missed watcher, queue bounds,
  atomic apply and echo suppression.
- Static mobile boundary tests prohibiting Node/Electron imports.
- PostgreSQL/R2/Temporal integration for all six lifecycle operations.
- Two-device end-to-end duplicate, out-of-order, conflict and reconcile cases.
- Playwright login, TOTP, device approval/revoke and exclusion publication.
- Leak scanners for tokens, URLs, paths, content and provider errors.
- Real test-Vault acceptance on Obsidian Desktop and Mobile, storing only a
  sanitized outcome manifest.

Mutation testing remains deferred. Deterministic tests, property/race coverage
and live dependency gates are the Phase 2 requirements.

### 16.2 Phase acceptance

All cases run against one final commit:

1. Browser login approves Desktop and Mobile devices; refresh/revoke works.
2. Markdown, text and binary create/update sync exactly once after offline use.
3. Rename and move preserve `source_id`.
4. Delete creates a tombstone; restore returns the same source identity.
5. Duplicate and out-of-order requests never duplicate or overwrite.
6. Missed watcher and deleted SQLite recover through reconciliation.
7. Remote content applies atomically without echo.
8. Interrupted multipart upload resumes and corrupt completion cannot publish.
9. Text and binary conflicts retain candidates and require visible resolution.
10. Policy-denied content is neither uploaded outside contract nor projected.
11. No sensitive value appears in logs, traces, errors, JUnit or manifests.
12. Upgrade/downgrade, backup and restore include the Phase 2 schema/state.

## 17. Child-spec program and delivery sequence

| Order | Child spec | Owns | Depends on |
|---|---|---|---|
| 1 | `sync-contract-and-schema-design.md` | shared API envelope, error vocabulary, migration sequencing and generated-client boundary | Phase 1 canonical core |
| 2 | `web-auth-and-device-authorization-design.md` | password/TOTP, web sessions, browser device approval, token rotation/revoke and device Admin | child 1 |
| 3 | `exclusion-policy-publication-design.md` | rule model, preview/publish, signed snapshot and backend enforcement | children 1–2 |
| 4 | `plugin-journal-and-small-file-sync-design.md` | SQLite journal, offline queue and create/update through 16 MiB | children 1–3 |
| 5 | `source-locator-and-tombstone-lifecycle-design.md` | rename, move, delete, restore, stable identity and lifecycle intents | child 4 |
| 6 | `device-cursor-and-manifest-reconciliation-design.md` | pull cursor, atomic apply, echo prevention, offline registration and repair | children 4–5 |
| 7 | `resumable-multipart-mobile-upload-design.md` | staging multipart, resume, verify/promote and exact staging cleanup | children 1, 3–4 |
| 8 | `source-conflict-capture-and-resolution-design.md` | verified candidates, text merge, binary choice and conflict races | children 4–7 |
| 9 | `obsidian-sync-acceptance-and-operations-design.md` | cross-slice E2E, real devices, recovery, runbooks and final Phase 2 handoff | children 1–8 |

The filenames above describe stable domain boundaries; their written artifacts
use the required `YYYY-MM-DD-<name>` prefix when created.

Each child goes through its own brainstorming approval, written design, user
review, implementation plan, test-first implementation, review and exactly one
plan handoff. `writing-plans` may be invoked only for the currently approved
child spec. No child may silently widen another child's public contract; a
cross-boundary change first updates this umbrella and every affected child spec.

The implementation order inside the program remains:

1. Contract and migration foundation.
2. Login, device authorization and minimal Admin.
3. Server-owned exclusions.
4. Small-file create/update.
5. Locator lifecycle: rename/move/delete/restore.
6. Cursor, pull, atomic apply, manifest reconciliation and offline registration.
7. Multipart and mobile resilience.
8. Conflict Inbox and resolution.
9. Cross-platform acceptance, operations docs and final handoff.

Every deliverable follows failing test, minimal implementation, focused gates,
integration/contract evidence, docs, review and commit. A deliverable is not
complete with only backend or plugin work.

## 18. Phase 1 deferred-work adjudication

### 18.1 Integrated in Phase 2

- Multipart/resumable upload and exact staging cleanup.
- Authenticated upload/download/sync HTTP APIs.
- SQLite device queue, cursor and manifest state.
- Source locators, tombstones and conflict records.
- Rename, move, delete and restore transactions.
- Minimal Web Admin and plugin conflict UI.
- Required public-exposure hardening from the Phase 1 backlog, including secret
  repr protection and canonical-read/error diagnostics touched by Phase 2.

### 18.2 Reused rather than rebuilt

- R2 CAS, verified receipts and content deduplication.
- PostgreSQL create/update/no-change publication.
- Idempotency locks, replay and ambiguous-commit recovery.
- Canonical read and durable projection dispatcher.
- Identity, recovery and live infrastructure gates.

### 18.3 Not integrated

- Worker/hybrid routing and the earlier 8 MiB proposal.
- Multi-provider, MinIO, fallback and dual-write.
- Full orphan/canonical object GC beyond exact multipart staging cleanup.
- Client-side encryption/customer-managed keys.
- Parsing/indexing and temporary MCP endpoints.
- AI-approved writes and mutation testing.

## 19. Visual companions

- [System boundary](html/2026-08-15-phase-two-obsidian-sync-system-boundary.html)
- [Data lifecycle](html/2026-08-15-phase-two-obsidian-sync-data-lifecycle.html)
- [Upload contract](html/2026-08-15-phase-two-obsidian-sync-upload-contract.html)
- [Authentication and device flow](html/2026-08-15-phase-two-obsidian-sync-auth-and-device-flow.html)
- [Reconcile protocol](html/2026-08-15-phase-two-obsidian-sync-reconcile-protocol.html)
- [Conflict resolution](html/2026-08-15-phase-two-obsidian-sync-conflict-resolution.html)
- [Error and test strategy](html/2026-08-15-phase-two-obsidian-sync-error-and-test-strategy.html)
- [Delivery sequence](html/2026-08-15-phase-two-obsidian-sync-delivery-sequence.html)

## 20. References

- `docs/00-PRODUCT_VISION_AND_PRD.md`
- `docs/01-CANONICAL_ARCHITECTURE.md`
- `docs/03-DATA_OWNERSHIP_AND_STORAGE.md`
- `docs/04-OBSIDIAN_SYNC_AND_SOURCES.md`
- `docs/07-POSTGRESQL_DATA_MODEL.md`
- `docs/11-TEMPORAL_WORKFLOWS.md`
- `docs/14-SECURITY_PRIVACY_AND_POLICY.md`
- `docs/16-TESTING_AND_EVALUATION.md`
- `docs/19-ARCHITECTURE_DECISIONS.md`
- `docs/20-IMPLEMENTATION_PLAN.md`
- [Cloudflare R2 upload objects](https://developers.cloudflare.com/r2/objects/upload-objects/)
- [Cloudflare R2 presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)
- [Obsidian SecretStorage](https://docs.obsidian.md/plugins/guides/secret-storage)
- [Obsidian plugin mobile guidance](https://docs.obsidian.md/oo/plugin)
