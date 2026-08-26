# Device Cursor and Manifest Reconciliation Design

**Status:** Approved in brainstorming on 2026-08-26

**Phase:** Phase 2 — Obsidian Sync, child 6 of 9

**Depends on:**

- `2026-08-18-plugin-journal-and-small-file-sync-design.md`
- `2026-08-20-source-locator-and-tombstone-lifecycle-design.md`
- `2026-08-25-explicit-restore-target-reservation-design.md`

**Next child:** `resumable-multipart-mobile-upload-design.md`

## 1. Purpose

This child makes server-to-device synchronization and device repair correct
through retry, restart, missed watcher observations, cursor discontinuity and
loss of the plugin SQLite journal. It owns server event pull, monotonic device
cursors, crash-safe remote apply, exact echo suppression, checkpoint-bound
manifest reconciliation, offline registration and repair of the Child 5
`reconcile_required` state.

PostgreSQL remains the authority for canonical identity, event order, current
version, locator and tombstone state. The Vault remains the user-controlled
working copy. Plugin SQLite is a rebuildable durable journal: losing it starts
reconciliation and never authorizes the client to invent a source identity or
assume that local bytes may overwrite canonical bytes.

This child is not the conflict-resolution system. It detects and durably
surfaces conflicts but does not retain verified conflict candidates, merge
text, choose binary winners or add a Conflict Inbox. Those behaviors remain
Child 8.

## 2. Canonical context

The following inherited rules are non-negotiable:

- PostgreSQL plus verified immutable Cloudflare R2 bytes form the canonical
  boundary. SQLite, Qdrant and Neo4j are rebuildable.
- Child 4 owns the portable sql.js journal, small-file create/update pipeline,
  bounded foreground queue and server-issued source identity on first commit.
- Child 5 owns locator/tombstone lifecycle and the durable plugin mapping
  through rename, move, delete and restore. Child 6 consumes those canonical
  events; it does not redefine their transaction semantics.
- The explicit-restore target-reservation amendment is part of the inherited
  plugin journal contract. Reconciliation must preserve `restore_pending`
  reservations and must not admit their target paths as new sources.
- Policy is a separate axis from lifecycle. Missing, stale or indeterminate
  policy evidence fails closed, but policy does not falsify canonical locator
  or tombstone state.
- A watcher reduces latency only. Cursor continuity and manifest
  reconciliation establish correctness.
- Raw path, locator, content, full digest, credential, object key, temporary
  file name, provider detail and exception text never enter logs, metrics,
  traces, JUnit, diagnostics exports or handoffs.

Child 5 is closed: the mandatory Desktop WDIO journey and physical Mobile
matrix passed after the explicit-restore target-reservation remediation. The
stale Child 5 status in `docs/20-IMPLEMENTATION_PLAN.md` must be corrected when
this child's implementation updates canonical documentation.

## 3. Scope

### 3.1 Included

- A dedicated framework-neutral `device_sync` domain with contracts, services,
  repository ports, errors and low-cardinality metrics.
- PostgreSQL device cursor, manifest run/page/resolution/action state and
  forward Alembic migration.
- Authenticated event pull, cursor acknowledge, manifest and exact-version
  binary download APIs.
- A plugin `SyncCoordinator` that serializes outbound, inbound and repair
  phases without freezing Vault editing.
- Plugin SQLite cursor, observation-generation, manifest-resume,
  remote-apply and echo-marker state.
- Self-origin proof, remote create/update/rename/move/delete/restore apply,
  local-trash delete semantics and crash recovery.
- Checkpoint-bound manifest planning for `upload`, `download`,
  `apply_tombstone`, `conflict`, `no_change` and `excluded`.
- Conservative source identity recovery after SQLite loss, including proven
  historical-locator and open-tombstone matches.
- Cursor polling and periodic foreground reconciliation suitable for Obsidian
  Desktop and Mobile.
- Mandatory diagnostics surfaces for every new closed failure path.
- The triggered API request-correlation and plugin diagnostics-trail backlog
  items listed in section 15.
- Unit, contract, migration, race, integration, Desktop WDIO and physical
  Mobile evidence.

### 3.2 Excluded

- Multipart/resumable uploads and files above the existing single-part limit:
  Child 7.
- Verified conflict candidate retention, three-way merge, binary choice and
  user conflict resolution: Child 8.
- Temporal orchestration of interactive device manifest runs. PostgreSQL owns
  their durable state.
- Source-lifecycle write-metric changes, including the deferred missing
  `record_commit(COMMITTED)` call.
- Changes to existing small-file-sync metrics, including the deferred
  `_validate_epoch_ms` exception-class cleanup.
- Hard deletion of Vault files, automatic collision renaming or silent
  last-write-wins behavior.
- Projection worker behavior, parsing, indexing or physical canonical-object
  garbage collection.

## 4. Approved decisions

1. Child 6 is a separate `device_sync` domain. It does not grow the upload or
   source-lifecycle domains into a replica engine.
2. One plugin `SyncCoordinator` owns every mutating foreground network phase.
   Triggers coalesce; no outbound, inbound or reconciliation mutation runs in
   parallel with another.
3. A server cursor records the highest event sequence the device has durably
   acknowledged. A local cursor records the highest sequence whose terminal
   handling is durable in the plugin journal. Neither may advance on intent
   alone.
4. Remote content apply is a durable state machine. A crash can leave verified
   old or new bytes recoverable, but never a partially trusted file or a cursor
   that claims an unverified apply.
5. Echo suppression requires an exact operation marker, locator operands and
   expected fingerprint. No time-window wildcard suppresses watcher events.
6. Remote delete moves a proven unchanged file to Obsidian local trash through
   `Vault.trash(file, false)`. There is no hard-delete fallback.
7. Reconciliation persists a local generation barrier. Watchers continue to
   journal edits after the barrier, while outbound dispatch waits for the run
   to reach a terminal-safe checkpoint.
8. After SQLite loss, an unknown local entry may bind automatically only by
   one of the proof rules in section 12. Hash-only matching is forbidden.
9. Manifest planning is deterministic at one server event checkpoint and one
   exact active policy revision. A policy advance invalidates the unfinished
   run rather than serving stale allowed actions.
10. PostgreSQL, not Temporal, stores interactive manifest progress and frozen
    actions. Replays return the original run outcome.
11. Cursor pull runs on foreground triggers and every 30 seconds while active.
    Full reconciliation runs on correctness triggers and every six hours of
    foreground active time. Neither assumes mobile background execution.
12. A manifest `upload` action is terminal-safe once it has durably created or
    reauthorized an existing outbound journal event. It need not commit to the
    server before the manifest checkpoint completes.

## 5. Architecture and ownership

```text
Obsidian Vault events                         Canonical sync events
        |                                             |
        v                                             v
portable SQLite journal <--- SyncCoordinator ---> authenticated FastAPI
        |                    one mutating phase              |
        |                                                    v
        +-- outbound queue                         device_sync domain
        +-- local cursor                                    |
        +-- manifest resume                                 v
        +-- remote apply marker                    PostgreSQL + R2 reads
        +-- exact echo marker
```

### 5.1 Backend

`src/personal_os/device_sync/` owns provider-neutral value objects, command and
result contracts, planning rules, typed errors, metrics and repository ports.
It imports no FastAPI, SQLAlchemy, database driver, R2 SDK or Obsidian type.

The PostgreSQL adapter owns event hydration, cursor locking, manifest
persistence, identity proof and deterministic action materialization. The R2
adapter remains behind the existing canonical-read boundary; device sync asks
for an exact verified source version and never receives an object key or
verified receipt.

FastAPI owns bearer extraction, strict wire validation, response mapping,
binary streaming and request correlation. Request bodies never select a user,
workspace or device.

### 5.2 Plugin

The coordinator depends on narrow ports:

- `DeviceSyncRepository`: local cursor, manifest, barrier, action and apply
  state;
- `DeviceSyncApi`: pull, acknowledge, manifest and download requests;
- `RemoteEventApplier`: self-origin proof and event/action classification;
- `AtomicVaultWriter`: same-directory staging, verified replacement, rename,
  move and local trash;
- existing outbound queue/lifecycle drivers and policy session.

Vault, SQLite and HTTP details stay in adapters. Mobile-loadable modules do not
import Node.js, Electron or `FileSystemAdapter` at module load time.

## 6. PostgreSQL evolution

One forward Alembic migration creates the child-owned tables and constraints.
It has empty upgrade, fixture upgrade, application smoke and downgrade tests.
Manifest rows are bounded temporary protocol state and may cascade from an
exact expired-run cleanup; source, version, event, locator, tombstone and audit
lineage never cascade through this child.

### 6.1 `device_cursors`

```text
device_cursor_id                  UUID primary key
workspace_id, device_id           credential-derived ownership
acknowledged_sequence             bigint, non-negative
delivered_through_sequence        bigint, >= acknowledged_sequence
created_at, updated_at            database times
```

There is one row per workspace/device. The device row and cursor row use
restricting foreign keys. A fresh device starts at sequence zero. Pull updates
only the delivered watermark. Acknowledge locks the row, requires the expected
prior sequence, rejects regression and rejects a sequence above the delivered
watermark unless the exact manifest-completion transaction authorizes its
checkpoint.
Exact acknowledge replay is a no-op returning the frozen cursor.

The manifest exception is not circular: the same transaction that validates
and changes one exact `applying` run to `completed` may advance its device
cursor to that run's checkpoint without a prior delivered watermark. No
already-completed, failed, expired or foreign run grants a new cursor advance.

Sync-event compaction must not pass the minimum acknowledged cursor of any
active device. If retained history nevertheless cannot satisfy a cursor, pull
returns the closed gap outcome and never fabricates missing events.

### 6.2 `manifest_runs`

```text
manifest_run_id                   UUID primary key
workspace_id, device_id           credential-derived ownership
base_acknowledged_sequence        cursor at start
checkpoint_sequence              greatest visible event sequence at start
policy_revision_number            exact active revision at start
client_observation_generation     non-negative local barrier generation
state                             collecting | planned | applying |
                                  completed | expired | failed
next_page_number                  zero-based expected page
entry_count                       0..100,000 cumulative entries
final_digest                      nullable lowercase SHA-256
safe_error_code                   nullable closed manifest error
created_at, expires_at             one-hour lifetime
planned_at, completed_at           nullable database times
```

Only one unfinished run exists per device. Start returns that run on exact
resume; it does not create a competing checkpoint. Expiry is based on database
time. An expired or policy-stale run cannot accept another page or complete.
The 100,000-entry run cap is independent of the 10,000 pending-event queue cap:
reconciliation may prove a large healthy Vault without turning every entry
into outbound work, while still bounding temporary PostgreSQL state.

### 6.3 `manifest_pages`

```text
manifest_run_id, page_number      primary key
entry_count                       0..500
page_digest                       lowercase SHA-256
received_at                       database time
```

Pages are contiguous from zero. Exact page-number/digest/count replay is a
no-op. Reuse with different evidence fails the run with a closed replay
mismatch. Page and final digests use one versioned canonical-JSON grammar.

### 6.4 `manifest_entry_resolutions`

Each accepted entry stores the run/page/order, opaque plugin-local entry ID,
optional client source/version evidence, submitted hash/size/media identity,
optional proven canonical source/version/locator/tombstone IDs and one closed
match kind. It does not store raw bytes, display text or provider values.

The authenticated request may carry the normalized locator needed for policy
and identity resolution. The adapter resolves it against canonical locator
history at the run checkpoint and retains only canonical IDs plus an internal
locator-evidence digest. Locator text is not copied into diagnostics or action
telemetry.

### 6.5 `manifest_actions`

Finalization inserts an ordered immutable row per action:

```text
manifest_run_id, action_index      primary key
action_kind                        upload | download | apply_tombstone |
                                   conflict | no_change | excluded
local_entry_id                     nullable for canonical-only downloads
source_id, source_version_id       nullable by action shape
source_locator_id                  nullable exact checkpoint locator
source_tombstone_id                nullable exact open tombstone
safe_reason_code                   nullable closed conflict/exclusion reason
```

Database constraints enforce the required and forbidden columns of every
action kind. Pagination is stable by `action_index`; a later canonical event
cannot rewrite a planned action.

## 7. Public API contracts

All routes require an active `obsidian_sync` device bearer credential and
derive workspace/device scope from it. JSON responses use the canonical
envelope and `Cache-Control: no-store`.

### 7.1 Pull events

`GET /api/sync/events` returns at most 200 immutable events after the server's
acknowledged cursor. The client does not choose an arbitrary workspace or
starting sequence.

The response contains:

```text
acknowledged_sequence
page_checkpoint_sequence
delivered_through_sequence
events[]
has_more
```

Each event contains the immutable event ID/sequence/type, nullable origin
device ID, source ID, base/current version IDs, base/current fingerprint
evidence, committed time and the operation-shaped locator/tombstone operands.
Create, rename, move, restore and update include the resulting locator; an
update's resulting locator is the locator the source held open at the event's
own sequence (updates change no locator, so they never include a prior
locator, and an update whose active locator cannot be resolved at its
sequence is the closed integrity error). Rename, move
and delete include the prior locator. Delete/restore include the exact
tombstone ID. The payload is hydrated from canonical event, version, object,
locator and tombstone rows; it is not stored as a second event body.

The first page read binds a database statement checkpoint. Events committed
after that checkpoint wait for a later pull. A missing retained predecessor,
unhydratable lifecycle operand or impossible event shape returns a closed gap
or integrity error; the server does not skip the row.

### 7.2 Acknowledge cursor

`POST /api/sync/cursor-acknowledgements` carries the expected prior sequence
and applied-through sequence. It advances only after plugin SQLite has
durably terminalized every visible workspace event through that watermark.
The endpoint is monotonic, idempotent and transactionally fenced as described
in section 6.1.

### 7.3 Manifest runs

The route set supports:

1. start/resume a run with the local observation generation;
2. put the exact next ordered page of at most 500 entries;
3. finalize with total count and final digest;
4. read deterministic action pages;
5. complete after every action is terminal-safe locally.

Completion requires the exact planned run and final digest. It atomically
marks the run completed and authorizes/advances the server cursor to the run
checkpoint. A lost completion response is resolved by exact replay. The plugin
does not clear its local barrier until the completion result and local cursor
are durably recorded.

Finalization moves `collecting` to `planned`. The first successful action-page
read moves `planned` to `applying`; later reads are exact state-preserving
replays. Every action read and completion rechecks the active policy revision.
An advance after planning invalidates the unfinished run with
`device_manifest_policy_advanced` instead of serving or completing stale
actions.

### 7.4 Binary download

`GET /api/sources/{source_id}/versions/{source_version_id}/content` performs
device scope, source/version membership, current policy and canonical object
integrity checks before streaming exact bytes.

A successful response is the documented binary-success exception to the JSON
envelope. It carries `Content-Length`, `Content-Type`, exact
`X-Content-SHA256` and the normal request/trace correlation headers. Errors
detected before streaming remain canonical JSON envelopes. A failure after
headers/body streaming starts terminates the stream and is a retryable
transport failure; it cannot be rewritten into a second JSON body. The plugin
therefore trusts no partial response and verifies declared digest and size
while staging and again after final placement. The route never returns an R2
key, presigned URL, receipt or provider header.

OpenAPI snapshot, generated TypeScript client and hand-mirrored mobile plugin
wire types change together. Contract tests prove their agreement.

## 8. Plugin SQLite evolution

Journal schema v7 upgrades the existing v6 image losslessly. It adds:

- local applied cursor and last server-acknowledged cursor;
- monotonic Vault observation generation;
- active repair barrier generation and reason;
- resumable manifest run, page and action progress;
- `remote_apply_operations`;
- exact echo markers.

The v6-to-v7 migration sets both cursor values to zero and leaves no active
manifest/apply marker. It preserves every file mapping, pending event,
lifecycle row, tombstone, restore reservation and attempt. If the recovered
journal was already `reconcile_required`, migration does not clear it.

### 8.1 Remote apply operation

One operation holds only local correctness evidence:

```text
server_event_sequence, event_id
source_id, operation
prior/target normalized locator       local-only SQLite data
expected base/final fingerprint
opaque temporary and rollback token
state                                prepared | temp_verified | vault_mutated |
                                     locally_applied | server_acknowledged
safe_error_code                       nullable closed reason
```

No bytes, credential, object key, URL or provider response is stored. The
temporary token derives exact sibling names locally but those names never
reach a diagnostic surface. `temp_verified` is used only by a content-bearing
apply. `vault_mutated` means the operation-shaped Vault effect completed:
replace/create, rename/move or local trash. Delete has no final fingerprint;
its final proof is the absent prior locator plus the retained tombstone mapping.

### 8.2 Echo marker

An echo marker binds the event sequence, source, operation, locator operands
and expected final fingerprint. A watcher observation is suppressed only when
all applicable members match. A marker is retired after either the exact
watcher observation or a recovery snapshot proves the final state. Elapsed
time alone never consumes a marker.

## 9. `SyncCoordinator`

The coordinator coalesces startup, app-resume, local-commit, periodic and
explicit-repair triggers. One bounded cycle runs:

```text
recover unfinished remote apply
-> if repair is required: reconcile
-> drain eligible lifecycle/content outbound work
-> pull and apply one inbound page
-> acknowledge the locally durable contiguous cursor
-> request one bounded follow-up when work remains
```

The coordinator never starts another mutating phase while one is active.
Watcher capture, automatic snapshot observation and editor activity continue
throughout. Plugin unload aborts new work, waits only for bounded local
persistence and leaves every network intent resumable.

### 9.1 Cadence

Cursor pull is requested:

- after safe startup/recovery;
- after authenticated onboarding or app resume;
- after a canonical local commit;
- every 30 seconds while the plugin is foreground-active.

Full reconciliation is requested:

- after onboarding when no trusted local mapping exists;
- after SQLite rebuild/fallback, cursor gap, compacted history, unknown event
  shape or local invariant mismatch;
- by the explicit `Repair sync` command;
- every six hours of accumulated foreground-active time.

Triggers coalesce. Offline/timeout/429/temporary outage uses cancellable
jittered exponential backoff from one second to five minutes. Mobile suspend
persists state and is not a terminal failure. A manifest run expires after one
hour of wall time even if the app was suspended; resume then starts a new run.

## 10. Incremental event handling

### 10.1 Self-origin

A self-origin event is a no-op only when the local immutable event identity,
canonical source/version result and committed fingerprint all match. The
plugin durably closes/reconciles the corresponding journal evidence before
advancing the local cursor. A same-device event with missing or different
evidence requires repair; origin device ID alone never suppresses it.

### 10.2 Remote create and update

A remote create may write only when the target is absent and unreserved. A
remote update may replace only when the current local fingerprint equals the
event's recorded base fingerprint and the mapping names the same source. A
pending local successor, changed base or occupied foreign target becomes a
visible conflict. No remote event overwrites it.

The writer creates a same-directory temporary sibling, streams the exact
version, verifies size/hash, persists `temp_verified`, performs the narrow
atomic-replace operation and verifies the final hash. Where the Obsidian data
adapter cannot replace an existing path in one rename, the adapter first
retains an exact rollback sibling and uses the durable state machine so a crash
always recovers verified old or new bytes. It never falls back to an in-place
unverified write.

### 10.3 Remote rename and move

Rename/move requires the proven source mapping, matching expected locator and
base fingerprint, and an available target. The plugin persists the apply and
echo marker before calling the Vault adapter. A target occupied by another
tracked or untracked file is a conflict; the plugin does not rename either
file automatically. The committed event's operation classification remains
the Child 5 classification.

### 10.4 Remote delete

Delete requires the same source, expected locator and retained-version
fingerprint. The plugin calls `Vault.trash(file, false)` so Desktop and Mobile
use Obsidian local trash. Only after the trash operation is durable does it
mark the local mapping tombstoned and advance the cursor. A changed file,
missing proof or trash failure preserves bytes and surfaces a closed apply
reason. There is no permanent-delete fallback.

### 10.5 Remote restore

Restore requires the exact tombstone/source lineage and an available target.
If unchanged retained bytes already exist under a proven local tombstone, the
plugin may rebind/move them. Otherwise it downloads the retained current
version through the verified apply path. Restore cannot consume a different or
closed tombstone and cannot bypass an existing `restore_pending` reservation.

### 10.6 Policy

Every inbound action is rechecked against the currently verified policy before
bytes are requested or placed. Excluded/indeterminate content is not
downloaded; existing local bytes remain. The plugin records a closed excluded
state that permits cursor progress. Canonical lifecycle truth may still be
applied when its locator/version proof is safe, because policy does not rewrite
source lifecycle history.

## 11. Crash recovery and cursor advancement

Recovery runs before any network phase:

| Durable evidence | Required recovery |
|---|---|
| `prepared`, no trusted temp | Remove only the exact recorded temp if present; retry |
| verified temp, target still at expected base | Resume the exact replacement |
| Vault mutation happened and its operation-shaped final proof matches | Mark locally applied; do not apply twice |
| rollback sibling exists and target is absent/corrupt | Restore the verified rollback; require repair |
| target hash differs from base and final | Preserve all recoverable bytes; require repair |
| locally applied but server ack missing | Retry idempotent cursor acknowledgement |

The local cursor advances in the same SQLite generation commit that records a
terminal-safe outcome: applied, proven self-origin no-op, durable conflict,
handled tombstone or excluded. It never advances for a retryable failure,
prepared operation or swallowed exception.

The server cursor advances only after that local generation is verified. If
the acknowledgement response is lost, local state retains the owed ack and the
next cycle retries it before pulling another page.

## 12. Manifest reconciliation

### 12.1 Local barrier and page capture

Starting a run persists barrier generation `G` and pauses outbound dispatch.
Watcher observations continue and receive generations greater than `G`.
Manifest entries are enumerated in normalized-locator order and contain:

```text
opaque local entry ID
optional known source ID
normalized locator
SHA-256, byte size, media type
optional last committed version ID
local observation generation
```

Each entry is fingerprinted from settled current bytes. A file that changes
during enumeration is not frozen: the later watcher observation stays in the
journal, and every action rechecks current path/fingerprint/pending evidence
before mutation.

### 12.2 Identity proof after SQLite loss

For an entry without a source ID, the backend evaluates these rules in order at
the run checkpoint:

1. An exact current active locator proves source identity.
2. If no current locator matches, one unique historical locator plus the exact
   current canonical fingerprint proves a remotely renamed/moved source.
3. If neither matches, one open tombstone whose retained locator and retained
   version fingerprint both match proves the deleted source.
4. Hash-only matching, multiple candidates, historical locator without exact
   fingerprint, closed tombstone or missing evidence proves nothing.

Rule 1 can bind identity when bytes differ, but without a trusted local base a
different fingerprint becomes `conflict`; it is never automatically uploaded
or downloaded. Rules 2 and 3 require exact fingerprints because their locators
are no longer active authorities.

An entry that supplies a source ID must prove that source belongs to the
credential workspace. Invalid or cross-workspace evidence fails closed and is
not retried through locator fallback.

### 12.3 Deterministic planning

At finalization, the planner compares every entry resolution with canonical
source/version/locator/tombstone state at checkpoint `C`:

- `upload`: a new allowed unowned locator, or a known source whose trusted
  local base is current and whose bytes changed;
- `download`: an allowed canonical active source missing locally, or a known
  source whose local bytes still equal a stale trusted base;
- `apply_tombstone`: a proven local entry for a canonically deleted source;
- `conflict`: both sides advanced, untrusted divergent bytes, ambiguous
  identity, occupied target or inconsistent lifecycle evidence;
- `no_change`: current bytes match; it may also bind recovered identity and
  align a proven historical locator when the target is free;
- `excluded`: current policy forbids transfer; local bytes are preserved.

Hash equality across unrelated locators never deduplicates source identity.
Canonical sources absent from the manifest receive deterministic download or
excluded actions according to checkpoint lifecycle/policy state. A deleted
canonical source absent locally needs no file action.

### 12.4 Applying actions

Before each action the plugin rechecks target availability, current fingerprint
and whether a local event newer than the entry was captured. A mismatch does
not invalidate other safe actions, but that action becomes a durable conflict
or repair blocker and performs no overwrite.

An `upload` action is applied by durably recording or reauthorizing an outbound
journal event under the repair barrier. A `download` or `apply_tombstone`
action uses the remote-apply state machine. `no_change`, `conflict` and
`excluded` persist their mapping/blocker evidence without network mutation.

After every action is terminal-safe, the plugin completes the server run,
durably records local cursor `C`, clears `reconcile_required` and the barrier,
then releases all outbound rows whose observation generation is greater than
`G` plus any upload rows created by the planner. The next pull begins after
`C`. No Vault edit is discarded or rewritten to fit the manifest snapshot.

## 13. Error contract

The domain adds closed errors grouped by responsibility:

```text
device_cursor_gap
device_cursor_regression
device_cursor_ack_ahead
device_event_unavailable
device_event_integrity_failed
device_manifest_not_found
device_manifest_expired
device_manifest_state_invalid
device_manifest_page_invalid
device_manifest_page_replay_mismatch
device_manifest_digest_mismatch
device_manifest_policy_advanced
device_download_integrity_failed
device_sync_dependency_unavailable
```

Exact public names are registered once in the central error registry and map
to one tested HTTP status/retryable pair. Input/state/identity errors are
non-retryable. Offline, timeout, 429 and temporary dependency outage are
retryable with bounded backoff. Integrity, gap, stale action and unknown event
shape stop mutation and require repair or a compatible plugin release.

Unknown exceptions map to the existing safe internal error. Raw database,
Vault, R2 or network messages never cross the boundary.

Normal planner/apply blockers are not route exceptions. They use a separate
closed action-reason vocabulary including
`device_manifest_identity_ambiguous`, `device_manifest_local_diverged`,
`device_manifest_target_occupied`, `device_manifest_action_stale` and
`device_manifest_policy_excluded`. These tokens may appear only in a conflict,
excluded or local repair action/trail surface; they never turn one ambiguous
entry into a failed whole-manifest request.

## 14. Diagnostics and metrics

### 14.1 Plugin trail v2

The diagnostics sidecar writes
`obsidian_sync_diagnostics_trail/v2`. The loader accepts v1 and losslessly
rewrites known entries to v2; a foreign token still resets through the existing
closed `trail_reset` behavior.

New trail kinds are:

```text
credential_failure
cursor_failure
apply_failure
reconcile_failure
composition_read_failure
```

Their stage vocabularies are also closed:

```text
cursor_failure       pull | acknowledge
apply_failure        prepare | download | verify_temp | vault_mutation |
                     verify_final | local_commit | recovery | trash
reconcile_failure    start | page | finalize | actions | complete
credential_failure   access_missing | refresh_failed
composition_read_failure
                     status_read | note_status_read | retry_schedule_read |
                     sync_status_read
```

`wire_failure` now means an HTTP attempt actually reached the transport and
failed. A missing credential or refresh failure before contact records
`credential_failure`. `status_read_failed` and `note_status_read_failed`
record `composition_read_failure`; that kind is deliberately excluded from
derived stop reasons because these once-per-session settings reads do not stop
sync.

Every cursor/apply/reconcile closed path appends a stage token and closed
reason. A parsed server envelope also appends its UUID-gated `request_id` and
registered server error code. Settings and Copy diagnostics expose only closed
status/reason tokens, counts, cursor lag, repair state and the five newest
trail entries in newest-first order.

### 14.2 API structured diagnostics

`api_request_failed`, including exceptional 5xx handling, records the bound
server-generated `request_id` in its structured diagnostics line exactly as
completed/rejected observations already do. It also carries the closed result
code. No raw request path, body, locator, response content or exception text is
added.

### 14.3 Metrics

`device_sync` owns low-cardinality metrics for:

- pull pages/events and pull outcomes;
- acknowledged cursor advances and aggregate cursor lag;
- manifest run/action outcomes, duration and drift count;
- remote apply outcomes by closed event/action kind;
- integrity and repair failures by closed reason.

Metric labels never contain workspace/device/source/run IDs, locator, digest,
media title or request ID. This child does not modify
`personal_os.small_file_sync.metrics` or source-lifecycle metrics.

### 14.4 No swallowed reasons

Every newly introduced catch must do at least one of the following before
closing the path:

- return a typed public/domain error;
- append a closed plugin trail reason;
- persist a closed settings/status blocker;
- write a structured closed API event.

Fire-and-forget diagnostics remain observe-only and cannot change the sync
outcome, but the primary failure reason must still reach a readable surface.

## 15. Triggered backlog adjudication

This child necessarily creates a plugin release and changes the diagnostics
trail vocabulary. Its implementation therefore includes and retires, after
green evidence, these `docs/handoff/BACKLOG.md` rows:

1. Record `request_id` for failed API requests in the structured diagnostics
   line (`Before next plugin release`).
2. Prevent `status_read_failed`/`note_status_read_failed` from occupying the
   derived settings stop-reason line (`At next plugin diagnostics-trail
   vocabulary change`).
3. The residual diagnostics-trail hygiene group: dead test bind,
   `login_required` without wire contact and trail-tail element order (`At next
   plugin diagnostics-trail change`).

The implementation removes the dead bind, distinguishes credential failures
from real wire failures and pins newest-first tail rendering.

These conditional rows remain exactly once because their trigger is not
reached:

- `_validate_epoch_ms` masking in small-file-sync metrics: Child 6 adds a new
  device-sync metrics domain and does not change small-file-sync metrics.
- Missing fresh `record_commit(COMMITTED)` on the source-lifecycle write side:
  Child 6 consumes lifecycle events and does not change lifecycle write
  metrics.

## 16. Failure matrix

| Condition | Required behavior |
|---|---|
| Exact cursor/page/run replay | Return frozen outcome; no duplicate row/action |
| Cursor regression or ack ahead | Reject; preserve both cursors and local state |
| Compacted/missing event history | Stop pull; set readable repair reason |
| Unknown event shape | Preserve bytes; require repair/compatible client |
| Self-origin evidence mismatch | Do not suppress; require repair |
| Remote update with changed local base | Conflict; no overwrite |
| Remote target occupied | Conflict; no automatic rename |
| Download hash/size mismatch | Keep old/rollback bytes; integrity blocker |
| Delete trash failure | Keep file and cursor; surface apply reason |
| Crash before replacement | Remove only exact temp or resume verified temp |
| Crash after replacement | Verify final hash and finalize without reapply |
| Echo marker mismatch | Treat watcher event as real; require repair if invariant fails |
| Manifest page mismatch | Fail run; never reuse a partial alternate manifest |
| Policy advances during run | Invalidate and start a new checkpoint-bound run |
| Local edit after barrier | Preserve newer journal row; stale action cannot overwrite |
| Lost completion acknowledgement | Exact run replay; no second action plan |
| SQLite loss | Full manifest proof; no client-minted/guessed source ID |
| Mobile suspend | Persist and resume; expiry starts a new run, not fake success |

## 17. Test strategy

Implementation starts with failing tests. Required layers are:

### 17.1 Domain and property tests

- Monotonic cursor and exact acknowledge replay.
- Event ordering, page bounds and terminal-safe contiguity.
- Manifest canonical JSON digests, page replay and action determinism.
- Identity-proof priority, ambiguity and hash-only refusal.
- Current/stale/divergent base planner matrix.
- Policy-revision and canonical-event races.
- Low-cardinality metric label rejection.

### 17.2 Migration and PostgreSQL tests

- Alembic empty upgrade, fixture upgrade, smoke and downgrade.
- SQLite v6-to-v7 migration preserving every Child 4/5 row and reservation.
- Query plans for cursor/event and manifest action pagination.
- Cursor lock races, duplicate page/finalize/complete and lost acknowledgement.
- Manifest expiry and exact cleanup.
- Event hydration for all six event types.

### 17.3 API and plugin tests

- Strict auth-scoped request/response models, error mappings, OpenAPI snapshot
  and generated-client compile.
- Binary download success, policy denial, missing/corrupt object and leakage.
- Coordinator coalescing and one-active-phase invariant.
- Fake-clock 30-second pull and six-hour foreground reconciliation cadence.
- Crash injection at every remote-apply transition.
- Exact echo match, mismatch and restart recovery.
- Rename/move target collision and local-trash delete.
- Generation barrier with watcher edits during enumeration/action apply.
- Diagnostics v1-to-v2 migration, request-ID correlation, P5 kind separation,
  credential taxonomy and newest-first tail.
- Static Mobile boundary tests prohibiting Node/Electron imports.

### 17.4 Integration and privacy

- Disposable PostgreSQL stack for concurrent event commits, cursor gaps,
  manifest replay and lost database acknowledgements.
- Cloudflare R2 test-bucket path for verified download and corrupt/missing
  fail-closed behavior; cleanup uses only the run's exact key allowlist.
- Two-device remote edit/lifecycle and SQLite-loss reconciliation.
- Leak scans covering logs, trails, settings exports, JUnit and retained live
  verdict artifacts.

## 18. Acceptance criteria

The final implementation proves on one final commit:

1. A remote edit applies exactly once, with verified old/new bytes recoverable
   at every crash point, and creates no outbound echo event.
2. Remote rename and move retain source/version identity and never overwrite an
   occupied target.
3. Remote delete sends an unchanged proven file to Obsidian local trash; remote
   restore retains source/version/tombstone lineage.
4. Local and server cursors advance only through terminal-safe contiguous
   outcomes; retryable failure advances neither.
5. Cursor gap, compacted history and unknown event shape stop incremental pull
   and surface a closed repair reason.
6. Manifest page replay and final action pagination are deterministic; digest
   or policy mismatch fails closed.
7. A missed watcher observation is found and converted to the correct upload,
   download, conflict, no-change, tombstone or excluded action.
8. Losing SQLite rebuilds source mappings and cursor state without creating a
   duplicate canonical source.
9. Current locator, unique historical-locator/fingerprint and open-tombstone
   proofs work in their approved priority; hash-only or ambiguous evidence does
   not bind.
10. An edit observed during reconciliation remains in generation greater than
    the barrier and replays after checkpoint completion.
11. Policy-excluded/indeterminate content is neither downloaded nor uploaded
    outside the policy contract, and local bytes are preserved.
12. Every new closed failure path has a readable reason token and the three
    triggered diagnostics backlog rows are retired with evidence.
13. Raw content, locator, path, full digest, credential, temporary name and
    provider exception sentinels are absent from every forbidden surface.

### 18.1 Mandatory live gates

Desktop WDIO runs against a disposable `knowledge-ci-*` project through the
documented local bootstrap/tunnel contract and proves:

- remote edit plus exact no-echo;
- cursor gap to manifest repair;
- lost-SQLite identity/cursor recovery without duplicate sources;
- remote tombstone to local trash.

The physical Mobile matrix proves:

- manifest suspend/resume;
- remote apply plus no-echo;
- lost-SQLite repair;
- tombstone to local trash;
- edit-during-reconciliation preservation.

Mock, unit inference or Desktop evidence cannot replace the physical Mobile
matrix. A missing or non-PASS live gate leaves Child 6 BLOCKED; no completion
claim is permitted.

## 19. Operations and documentation

Implementation adds one living runbook for:

- cursor/repair status and closed diagnostics;
- `Repair sync` behavior;
- safe recovery from expired manifest, apply marker and SQLite loss;
- Desktop and Mobile acceptance procedure;
- operator interpretation of cursor lag, action counts and reason tokens.

It updates canonical documents `04`, `07`, `12`, `15`, `16` and `20` in the
same contract-changing implementation. API changes update OpenAPI, the
generated client, contract tests and release notes together.

At plan completion or interruption, write exactly one handoff at
`docs/handoff/YYYY-MM-DD-device-cursor-and-manifest-reconciliation.md` with
final commit SHA, gate evidence, interpretation decisions, deferred verdicts
and next actions. Any remaining deferred item receives exactly one indexed row
in `docs/handoff/BACKLOG.md` with a verifiable `Implement by` milestone.

## 20. Deferred boundaries

| Owner | Deferred responsibility |
|---|---|
| Child 7 | Multipart/resumable upload and large-file Mobile behavior |
| Child 8 | Conflict candidates, merge, binary choice and user resolution |
| Child 9 | Cross-slice Phase 2 acceptance/operations closure |
| Phase 3 | Parsing, indexing and projection-intent consumption |
| Production operations | Prometheus exporter/sink and fleet dashboards |
| Later retention work | Sync-event compaction execution and canonical GC |

## 21. References

- `docs/00-PRODUCT_VISION_AND_PRD.md`
- `docs/01-CANONICAL_ARCHITECTURE.md`
- `docs/02-TECH_STACK.md`
- `docs/03-DATA_OWNERSHIP_AND_STORAGE.md`
- `docs/04-OBSIDIAN_SYNC_AND_SOURCES.md`
- `docs/07-POSTGRESQL_DATA_MODEL.md`
- `docs/11-TEMPORAL_WORKFLOWS.md`
- `docs/12-API_MCP_AND_AGENT_INTEGRATION.md`
- `docs/14-SECURITY_PRIVACY_AND_POLICY.md`
- `docs/15-OBSERVABILITY_AND_ALERTING.md`
- `docs/16-TESTING_AND_EVALUATION.md`
- `docs/17-DEPLOYMENT_BACKUP_AND_RECOVERY.md`
- `docs/19-ARCHITECTURE_DECISIONS.md`
- `docs/20-IMPLEMENTATION_PLAN.md`
- `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md`
- `docs/superpowers/specs/2026-08-18-plugin-journal-and-small-file-sync-design.md`
- `docs/superpowers/specs/2026-08-20-source-locator-and-tombstone-lifecycle-design.md`
- `docs/superpowers/specs/2026-08-25-explicit-restore-target-reservation-design.md`
- `docs/handoff/2026-08-23-sync-error-tracing-observability.md`
- `docs/handoff/2026-08-24-closed-reason-surfacing-remediation.md`
- `docs/handoff/2026-08-24-child-six-deferred-remediation.md`
- `docs/handoff/2026-08-25-explicit-restore-target-reservation.md`
- `docs/handoff/BACKLOG.md`
