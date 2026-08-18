# Plugin Journal and Small-File Sync Design

**Status:** Approved in brainstorming on 2026-08-18

**Phase:** Phase 2 — Obsidian Sync, child 4 of 9

**Depends on:**
`2026-08-16-web-auth-and-device-authorization-design.md` and
`2026-08-17-exclusion-policy-publication-design.md`

**Next child:** `source-locator-and-tombstone-lifecycle-design.md`

## 1. Purpose

This child makes an authenticated Obsidian plugin durably capture local
create/update intent and upload regular, policy-allowed files of at most
16 MiB through the authenticated API. It owns the portable SQLite journal,
foreground offline queue, API preflight/stream/replay contract and minimal
plugin status surface.

It deliberately does not make the plugin a replica engine. Rename, move,
delete and restore are deferred to child 5. Remote cursor/pull, manifest
reconciliation and repair are deferred to child 6. Multipart and files larger
than 16 MiB are deferred to child 7. Conflict UI and resolution are deferred to
child 8.

## 2. Canonical context

The following inherited rules are non-negotiable:

- PostgreSQL is canonical state and R2/S3 holds immutable canonical bytes.
- Only the server turns verified bytes into an internal receipt and publishes a
  source version in its PostgreSQL transaction.
- The plugin has an approved device credential with fixed `obsidian_sync`
  scope. It never chooses a user or workspace from request data.
- The plugin evaluates the signed exclusion-policy snapshot locally, but the
  backend remains authoritative and re-evaluates policy at preflight.
- Plugin code must work on Obsidian Desktop and Mobile. It cannot rely on
  Node, Electron, a native SQLite driver or a long-running background process.
- Vault bytes remain in the Vault; they are never copied into the journal.
- No logs, metrics, errors or telemetry may expose raw content, path, full
  digest, token, policy rule or provider detail.
- A local failure must not delete Vault content, block editing or manufacture a
  canonical source identity.

This design follows the platform rules and reference-device evidence in
`docs/operations/exclusion-policy-device-verification.md`.

## 3. Scope

### 3.1 Included

- `sql.js` WebAssembly SQLite journal persisted through Obsidian `DataAdapter`
  binary I/O on Desktop and Mobile.
- Durable local create/update intent, source mapping, file fingerprint, bounded
  retry state and safe diagnostics.
- Create/update of any regular policy-allowed Vault file of at most
  `16 * 1024 * 1024` bytes, including Markdown and binary assets.
- Explicit `Sync existing files` snapshot scan and foreground `Sync now` queue
  pass.
- Capture of create/modify events, 250 ms per-file settle and coalescing before
  a preflight begins.
- Authenticated preflight and raw single-part stream endpoints, including
  exact replay after a lost commit response.
- Full byte-size and SHA-256 verification before canonical publication.
- Minimal status, commands, blocker messages and redacted instrumentation.
- Unit, contract, persistence-recovery, server integration and reference-device
  acceptance tests for the behavior defined here.

### 3.2 Excluded

- Rename, move, delete, restore, source locators, tombstones and their API
  mutations (child 5).
- Cursor pull, remote apply, manifest reconciliation and repair (child 6).
- Presigned URL, Cloudflare Worker, hybrid-edge routing, multipart/resumable
  upload and files above 16 MiB (child 7).
- Conflict candidate capture UI, merge and user resolution (child 8).
- Multi-provider, MinIO, fallback, dual-write, client-side encryption,
  customer-managed keys, parsing/indexing, MCP writes and mutation testing.
- Automatic initial full-Vault upload, periodic recursive scans and a background
  sync daemon.

## 4. Approved decisions

1. Use a journal-first, portable SQLite design. Do not upload directly from a
   watcher callback.
2. Use `sql.js` plus `DataAdapter.readBinary`/`writeBinary`; no Node/Electron
   database API or native SQLite dependency is permitted.
3. Persist SQLite as verified immutable generations and a small manifest, rather
   than claiming a mobile filesystem rename is universally atomic.
4. A same-file unsent create/update may coalesce only before preflight starts.
   A frozen preflight/upload fingerprint is immutable; a later save is a
   successor event.
5. Do not scan an existing Vault automatically. The user explicitly invokes
   `Sync existing files`.
6. Files over 16 MiB become `blocked_size`; they do not retry or lose local
   content while multipart is absent.
7. Queue processing runs only in bounded foreground passes started by plugin
   load, a Vault event or `Sync now`; no work continues after unload/suspend.
8. Local policy `excluded` and `indeterminate` both fail closed to
   `excluded_policy`; re-evaluation requires a valid newer policy snapshot.
9. Use a two-step API: JSON preflight followed by an authenticated raw-byte
   stream to a server-owned operation URL.
10. The server, never the client, creates a source/source version after verified
    content. The plugin only stores the returned canonical `source_id`.
11. A missing file, rename, move, delete or restore is `deferred_lifecycle` in
    this child. It must not produce a network lifecycle mutation.
12. A stale base is `blocked_conflict`; no retry, overwrite or automatic copy is
    allowed.
13. A lost server response is replayed with the same event/idempotency identity
    and returns the original result, never a duplicate publication.

## 5. Architecture and ownership

```text
Vault bytes + file events             Plugin portable local state
file is edited by Obsidian            sql.js journal generations + manifest
          |                                      |
          | settle, policy, fingerprint           | bounded foreground driver
          +---------------> durable intent <------+ 
                                      |
                           HTTPS authenticated API
                                      |
          preflight / exact replay / raw stream verification
                                      |
          PostgreSQL canonical transaction + immutable object bytes
```

The Vault owns file bytes and user editing. The journal owns no raw file bytes,
credentials, secret material or remote provider identifiers. It records only
the metadata required to recover a local intent.

The API adapter owns authentication extraction, strict wire validation,
canonical envelopes and HTTP error mapping. A framework-neutral sync service
owns idempotency, policy/base checks, operation transitions, verification and
publication. PostgreSQL adapters own locks and storage statements. Object-store
adapters receive a server-internal verified flow only; they are not exposed to
the plugin.

## 6. Portable journal persistence

### 6.1 Storage location and writer

The plugin creates journal files below its own Vault-local plugin directory,
using `Vault.configDir` and path normalization rather than assuming a literal
`.obsidian` directory. It opens SQLite only through `sql.js` and persists the
database bytes through `DataAdapter` binary methods.

One journal writer serializes all mutations. It may batch a bounded burst of
already-decided mutations into one SQLite transaction, but event semantics are
not weakened: a batch never coalesces an event whose preflight has started.
During recovery, a bounded in-memory path buffer holds watcher notifications.
If that buffer overflows, the journal sets `reconcile_required` instead of
silently losing a mutation.

### 6.2 Generation protocol

Each successful SQLite transaction produces the next generation:

1. export the database bytes;
2. calculate byte size and SHA-256;
3. write `journal.sqlite.g<generation>`;
4. read it back and verify size/digest; and
5. write and verify a small manifest naming that generation, its digest and
   schema version.

On startup, the plugin accepts only a manifest whose named generation verifies.
If the newest write is torn, missing or invalid, it selects the newest prior
verified generation. If no valid generation exists, it preserves every Vault
file, starts an empty journal marked `reconcile_required`, and exposes safe
recovery status. Retention keeps the current verified generation and one prior
verified generation; cleanup is best effort and never precedes manifest
verification.

### 6.3 Logical schema

The implementation may add indexes and migration bookkeeping, but the durable
meaning of these records is fixed:

| Record | Required fields and meaning |
|---|---|
| `journal_meta` | schema version, dirty generation, last verified persistence generation, `reconcile_required`, safe recovery state |
| `local_files` | random `local_file_id`, normalized current local path, nullable server `source_id`, observed SHA-256/size/media type, last committed base version, policy revision |
| `journal_events` | stable UUID `event_id`, idempotency key, `create`/`update`, frozen fingerprint, state, attempt count, next eligible retry, safe error code and nullable operation ID |
| `journal_attempts` | bounded timestamp, closed outcome/error label and request correlation ID only |

`local_file_id` is a plugin-local random identity, not a canonical source ID or
a persistent backend locator. For a create event, `source_id` is null until the
server returns a committed receipt. A journal row never contains file bytes,
access token, refresh token, raw policy rule or server/provider secret.

### 6.4 Queue limits

At most 10,000 pending events and 64 MiB of SQLite data are allowed as soft
limits. Reaching either limit durably sets `reconcile_required`, preserves
in-flight rows and stops creating per-change journal rows. It does not block
editing, delete local content, expire unsynced work or create a retry storm.
Child 6 will reconcile a dirty Vault generation; this child only records the
need safely.

## 7. Capture and event state machine

### 7.1 Capture

On plugin load, register only create/modify capture required for this scope.
For a path event, settle 250 ms, then re-read the file and calculate SHA-256,
byte size and media type before policy evaluation. A successful current policy
decision for a regular file at most 16 MiB appends a create/update intent.

An existing Vault is never scanned merely because the plugin is enabled or
restarted. The explicit `Sync existing files` command takes a snapshot and
processes it in bounded batches through the same capture path. The command asks
for confirmation before queuing work.

Lifecycle notifications are observed only as a correctness guard. If a tracked
file disappears or an affected rename/move/delete/restore is detected, the
matching item becomes `deferred_lifecycle`, network mutation stops, and child 4
does not rebind its path or infer a new source. The scanner excludes such
affected paths until child 5 owns the transition.

### 7.2 States and transitions

```text
queued -> preflight -> uploading -> committed | no_change
   |           |              |
   +--------> waiting_retry <-+

terminal/non-retry states:
excluded_policy, blocked_size, blocked_conflict,
deferred_lifecycle, integrity_failed
```

`queued` is eligible only when its retry time has passed. `preflight` freezes
the exact event fingerprint. `uploading` streams only bytes that still match
the frozen digest and size. `committed` records the returned source/version;
`no_change` records a safe no-op receipt.

An unsent event for the same `local_file_id` may be replaced by a later current
fingerprint while it remains `queued` or `waiting_retry` and preflight has not
started. Once `preflight` begins, the original event and fingerprint never
change. A later save creates a successor event. If the plugin detects that the
file changed before or during a stream, it must not commit stale local state;
the attempt returns to safe retry/failure handling and the successor represents
the newer bytes.

`blocked_size` means the observed file is larger than 16 MiB. `excluded_policy`
means either local or server policy denied the operation. `blocked_conflict`
means the server found a stale base. `integrity_failed` means verification of
the local or received bytes failed. Those states never receive automatic retry.

## 8. Foreground queue and retry

A queue pass starts only on plugin load after safe recovery, a Vault event, or
the `Sync now` command. It selects the oldest eligible event and has one active
content stream at a time. The pass has a deadline and uses `AbortController`;
it ends before plugin unload/mobile suspension rather than becoming a daemon.

Offline, timeout, 429 and 5xx preserve the event and use exponential backoff
from one second to five minutes with jitter. Each new foreground trigger may
run one bounded pass; it is not a periodic background recursive scan.

An expired access credential may cause at most one refresh attempt per queue
pass. A revoked/reused family, exhausted refresh or explicit logout preserves
the journal and presents `Login required`. The plugin may resume only after a
successful new login. Cancellation and mobile suspension are resumable,
non-terminal states.

## 9. Policy, privacy and safety

Local capture and each preflight use the current accepted signed policy
snapshot. A raw `excluded` or `indeterminate` policy outcome is enforced as
`excluded_policy`; it uploads no bytes and does not retry. A new accepted
snapshot may re-evaluate blocked work. The request includes the accepted policy
revision so the server can diagnose a stale client view without trusting it.

The server independently authorizes the device, workspace and policy before
issuing an upload operation. A server `excluded` result is equally terminal and
leaves local files untouched. No local policy state can grant an upload that the
server denies.

User-visible local UI may identify the file the user selected. Logs, error
reports and metrics may use opaque IDs, counts, durations and closed state/error
labels only. They never include content, locator/path, full digest, token,
policy expression, object key, provider exception or operation URL.

## 10. Small-file API contract

All endpoints are under `/api`, require the existing Obsidian bearer device
credential and fixed `obsidian_sync` scope, use the canonical response envelope
and expose closed safe errors. Request bodies never choose a workspace or user.
Routes below are the child 4 proposed contract; child 1's generated OpenAPI
client remains the only shared Web/plugin contract dependency.

### 10.1 Preflight

`POST /api/sync/journal-events/preflight` accepts a strict JSON object:

```text
event_id             UUID; stable journal event
idempotency_key      opaque stable key for this event
operation            create | update
local_file_id        plugin-local random UUID
source_id            required for update; absent for create
base_version_id      required for update; absent for create
normalized_locator   current normalized path for policy/display context
sha256               exact lowercase content digest
size_bytes           exact non-negative byte size
media_type           validated media type or application/octet-stream
policy_revision      accepted signed local policy revision
```

The server validates schema, credential/device state, policy, idempotency,
operation shape, update base and the server-owned single-part size threshold.
For `create`, it records a pending operation keyed to the device/event identity
but does not create a canonical source merely by accepting preflight. The
canonical source and first version are committed only after verified bytes.

Preflight returns exactly one typed outcome:

| Outcome | Required plugin behavior |
|---|---|
| `committed_replay` | persist the original source/version receipt and finish without uploading |
| `no_change` | persist the no-op result and finish |
| `excluded` | set `excluded_policy`; retain local file and stop retry |
| `conflict` | set `blocked_conflict`; retain local/base metadata and stop retry |
| `single_part_upload` | persist opaque `operation_id` and stream to the supplied same-origin API URL |

The operation record binds the device, workspace derived by credential, event
identity, idempotency key, declared fingerprint, policy decision and expiry.
It permits no payload substitution. Terminal results retain the original safe
receipt for exact replay. The operation is implementation state, not a public
provider receipt or source locator.

### 10.2 Content stream

`PUT /api/uploads/{operation_id}/content` accepts the authenticated raw byte
stream for only the preflight-bound operation. The client checks the currently
read bytes against the frozen size/digest before sending. It sends no R2 key,
presigned URL, verified receipt or provider metadata.

The server enforces the threshold itself, applies explicit deadline and bounded
retry/error mapping, spools through the Phase 1 server-side bounded
verification/CAS path, and verifies full byte size and SHA-256 before the
publication service runs. Only then may one PostgreSQL transaction create or
update the source version and record the terminal operation receipt.

A broken stream, size mismatch or digest mismatch cannot publish and becomes a
safe integrity failure. An object-store implementation detail is never returned
to the client. The server owns object cleanup under its Phase 1 ownership rules.

### 10.3 Exact replay

If the server commits but the client loses the response, repeating preflight
with the same `event_id` and idempotency key returns `committed_replay` with the
original canonical result. It does not allocate another upload, source or
version. Retrying with a different payload under the same identity is rejected.

## 11. Minimal plugin UX and operations

The plugin displays a small sync status with counts and one of:

```text
Ready | Syncing | Offline — queued | Login required |
Policy blocked | Reconcile required
```

It offers only these commands:

- `Sync now`: one bounded foreground pass of currently eligible events.
- `Sync existing files`: confirmed snapshot scan in bounded batches.

Required blocker guidance is:

| Condition | Message/action |
|---|---|
| `blocked_size` | Explain the 16 MiB limit and that multipart is a later child. |
| `excluded_policy` | Show only a safe policy-blocked explanation; refresh policy only through authorized flow. |
| `blocked_conflict` or `deferred_lifecycle` | Explain that no overwrite occurred and the later lifecycle/conflict flow owns resolution. |
| Login required | Open existing browser login; keep queue unchanged. |
| `reconcile_required` | Stop the driver and explain that child 6 repair/reconciliation is required. |

Instrumentation is limited to redacted counters for enqueue, coalesce,
preflight/upload outcomes, retry, blocker and persistence recovery. UI or
telemetry must never become an automatic upload control.

## 12. Errors and retry matrix

| Condition | Journal outcome |
|---|---|
| Offline, timeout, 429, 5xx | keep event; bounded jittered foreground retry |
| Access expiry | refresh once in this pass; otherwise retain queue and require login |
| Device/token family revoked | retain queue and require login |
| Local/server policy deny | `excluded_policy`, no upload/retry |
| File larger than 16 MiB | `blocked_size`, no network retry |
| Stale base | `blocked_conflict`, no overwrite/retry |
| File disappears/lifecycle event | `deferred_lifecycle`, no network mutation |
| Local/server digest or size mismatch | `integrity_failed`, never publish |
| Lost committed response | same identity preflight; `committed_replay` |
| Watcher loss, buffer overflow, SQLite recovery failure, queue cap | set `reconcile_required`; preserve Vault and queue evidence |

## 13. Acceptance criteria and verification

Implementation begins with failing tests for each behavior. The relevant test
and verification gates must prove all of the following:

1. Desktop and mobile use the same WASM SQLite/DataAdapter path; torn latest
   persistence falls back to the newest verified generation without queue loss.
2. Save bursts before preflight coalesce; a save after freeze creates a
   successor; no event fingerprint mutates after preflight.
3. The 10,000 event and 64 MiB limits set `reconcile_required` without blocking
   Vault editing or deleting local content.
4. A regular allowed file at or below exactly 16 MiB reaches single-part
   preflight; one byte more reaches `blocked_size` and makes no upload call.
5. Server integration proves no source/version commits until full digest and
   size verification; stale base is a durable conflict and changed bytes cannot
   commit the frozen fingerprint.
6. Lost response replay returns the original receipt without a second source or
   source version.
7. Offline, timeout, 429 and 5xx retain work and do not create a background
   retry loop; credential refresh happens at most once in a queue pass.
8. Excluded/indeterminate content sends no bytes; server deny retains the local
   file; log/telemetry fixtures prove sensitive data is absent.
9. Rename/move/delete/restore produce no network mutation in this child and
   become `deferred_lifecycle`.
10. Reference-device tests follow the current exclusion-policy device
    verification runbook and record only sanitized evidence.

Required implementation gates are focused unit tests, plugin contract tests,
server API/integration tests, OpenAPI snapshot and generated-client compile,
TypeScript strict/lint, Python type/lint/test gates touched by the server,
mobile-boundary static tests, and a real test-Vault check on Desktop and Mobile.

## 14. Deferred boundaries

| Owner | Deferred responsibility |
|---|---|
| Child 5 | Canonical source locator, rename/move/delete/restore, tombstone lifecycle and lifecycle-safe path rebinding. |
| Child 6 | Cursor pull, remote apply, offline registration/recovery, manifest reconciliation and repair of `reconcile_required`. |
| Child 7 | Multipart/resumable mobile upload, R2 Worker/hybrid routing and files above 16 MiB. |
| Child 8 | Candidate preservation, Conflict Inbox, merge and visible resolution. |
| Later work | Multi-provider/fallback/dual-write, client-side encryption/CMK, parsing/indexing, MCP writes and mutation testing. |

## 15. Visual companions

- [System boundary](html/4.%20plugin-journal-and-small-file-sync-design/2026-08-18-plugin-journal-and-small-file-sync-system-boundary.html)
- [Journal state machine](html/4.%20plugin-journal-and-small-file-sync-design/2026-08-18-plugin-journal-and-small-file-sync-journal-state-machine.html)
- [API replay flow](html/4.%20plugin-journal-and-small-file-sync-design/2026-08-18-plugin-journal-and-small-file-sync-api-replay-flow.html)
- [Watcher and offline retry](html/4.%20plugin-journal-and-small-file-sync-design/2026-08-18-plugin-journal-and-small-file-sync-watcher-offline-retry.html)
- [Policy and lifecycle boundary](html/4.%20plugin-journal-and-small-file-sync-design/2026-08-18-plugin-journal-and-small-file-sync-policy-lifecycle-boundary.html)
- [Minimal UX and operations](html/4.%20plugin-journal-and-small-file-sync-design/2026-08-18-plugin-journal-and-small-file-sync-minimal-ux-operations.html)
- [Acceptance criteria](html/4.%20plugin-journal-and-small-file-sync-design/2026-08-18-plugin-journal-and-small-file-sync-acceptance-criteria.html)

## 16. References

- `docs/04-OBSIDIAN_SYNC_AND_SOURCES.md`
- `docs/07-POSTGRESQL_DATA_MODEL.md`
- `docs/12-API_MCP_AND_AGENT_INTEGRATION.md`
- `docs/14-SECURITY_PRIVACY_AND_POLICY.md`
- `docs/16-TESTING_AND_EVALUATION.md`
- `docs/19-ARCHITECTURE_DECISIONS.md`
- `docs/20-IMPLEMENTATION_PLAN.md`
- `docs/operations/exclusion-policy-device-verification.md`
- `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md`
- `docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md`
- `docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md`
