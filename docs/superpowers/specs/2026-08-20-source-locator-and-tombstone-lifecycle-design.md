# Source Locator and Tombstone Lifecycle Design

**Status:** Approved in brainstorming on 2026-08-20

**Phase:** Phase 2 — Obsidian Sync, child 5 of 9

**Depends on:** 2026-08-18-plugin-journal-and-small-file-sync-design.md

**Next child:** device-cursor-and-manifest-reconciliation-design.md

## 1. Purpose

This child makes a canonical source retain its identity while its Obsidian file is renamed, moved, deleted, or restored. It owns canonical locator history, tombstones, lifecycle sync events, lifecycle-safe plugin path rebinding, and the corresponding projection intents.

source_id is durable identity, never a path-derived identifier. Rename, move, delete, and restore retain the same source_id; none creates a source or a content version. Delete retains the current canonical version under a tombstone. Restore returns that same source to active only at an explicitly requested, available target locator.

This is not a replica or reconciliation engine. Cursor pull, remote apply, manifest repair, and first registration after SQLite recovery remain child 6. Conflict candidate capture and resolution remain child 8.

## 2. Canonical context

The following inherited rules are non-negotiable:

- PostgreSQL and verified immutable R2 bytes form the canonical boundary; SQLite and projections are rebuildable.
- Phase 1 source publication owns content-version creation, idempotency replay, audit, and the durable projection outbox. This child extends it and does not replace it.
- Child 4 owns the portable sql.js journal, plugin-local local_file_id, small-file create/update, and the mapping from local identity to backend-issued source_id.
- Child 4 treats delete, rename, move, and restore notifications as deferred_lifecycle; it does not rebind a path or send a lifecycle mutation.
- Policy is separate from sources.sync_state. An active source can be effectively denied, and a deleted source retains canonical content, audit lineage, and its current version.
- Device, user, and workspace scope derive only from the approved obsidian_sync device credential. A public lifecycle body never chooses a workspace or user.
- Raw paths, content, full digests, tokens, object keys, and provider details never enter logs, metrics, traces, safe errors, JUnit, or sanitized device evidence.

## 3. Scope

### 3.1 Included

- PostgreSQL locator history and open-tombstone schema.
- Framework-neutral rename, move, delete, and restore commands.
- Authenticated lifecycle API, generated client, and closed error contract.
- Atomic lifecycle event, audit, policy outcome, and projection-intent writes.
- Initial canonical locator creation for new small-file create commits.
- Plugin lifecycle journal records, ordered foreground dispatch, and safe path rebinding only after a canonical result.
- Deterministic same-device restore of a locally deleted unchanged file, with an explicit target locator.
- Unit, contract, migration, race, integration, Desktop, and Mobile evidence for this behavior.

### 3.2 Excluded

- New content upload, multipart upload, or content-version changes as part of a lifecycle command.
- Cursor pull, remote file apply, echo prevention, manifest reconciliation, initial registration after journal loss, and repair of a legacy source without a canonical locator (child 6).
- Candidate retention, text merge, binary choice, Conflict Inbox, and user resolution of lifecycle conflicts (child 8).
- A Web editor, Web lifecycle UI, physical canonical-object GC, or automatic rename-collision resolution.
- New projection-worker behavior. Intents remain durable dispatch requirements; Phase 3 owns ingestion and projection implementation.

## 4. Approved decisions

1. Restore always carries an explicit target_locator. The server never silently restores the prior locator.
2. A locator is canonical relational state, not JSON metadata and not inferred from object bytes or title.
3. Rename and move carry the expected current locator and a target locator. The expected locator fences stale path mutations even if content has not changed.
4. Every lifecycle command carries the version observed by the device. A changed current version is a durable conflict, never a last-write-wins path update.
5. Rename changes the final filename in the same parent locator; move changes the parent locator. The server validates that classification.
6. Delete closes the current locator and frees the path for another active source. The tombstone keeps historical evidence but does not reserve the path.
7. Restore requires the one open tombstone, its retained current version, and unchanged local bytes. Changed bytes after delete are a child-8 conflict.
8. No lifecycle operation is coalesced across a delete or restore boundary. Per-source journal order is preserved.
9. A denied or indeterminate policy result at the new locator commits canonical locator state and creates projection delete intents, not a false lifecycle state.
10. Existing child-4 sources without a canonical locator are never backfilled from a guess. They are protected from lifecycle mutation until child 6 proves their locator through a manifest.

## 5. Ownership and boundaries

    Obsidian Vault events + local bytes       Plugin sql.js journal
    rename/move/delete/explicit restore  ->  ordered lifecycle intent
                                                    |
                                          authenticated lifecycle API
                                                    |
                               lifecycle domain + policy enforcement boundary
                                                    |
                  PostgreSQL: source / locator / tombstone / event / audit / intents
                                                    |
                                  existing Temporal projection dispatcher

The plugin owns local observation, local identity mapping, foreground retries, and the locally chosen restore target. It does not decide canonical identity, locator ownership, policy authorization, or the current version.

FastAPI owns bearer extraction, strict wire validation, canonical envelopes, and HTTP mapping. The lifecycle domain owns state-machine validation, fingerprints, and result hydration. PostgreSQL owns locking, exact replay lookup, locator uniqueness, atomic mutation, and outbox insertion. API and plugin code do not import database, R2, Temporal, or projection implementation details into their domain contracts.

## 6. Lifecycle vocabulary and invariants

### 6.1 Source and locator state

sources.sync_state remains active, stored_not_indexed, pending, or deleted. This child transitions active to deleted and deleted to active. stored_not_indexed is a later parser outcome and is rejected for lifecycle mutation unless its owner explicitly extends this contract.

An active source affected by this child has exactly one open canonical locator. A deleted source has no open locator and exactly one open tombstone. The current source version is non-null and belongs to the source. Policy is never encoded as a source lifecycle state.

### 6.2 Locator normalization and title

normalized_locator uses the existing canonical NFC, slash-separated locator normalizer. It rejects empty segments, dot traversal, backslashes, control characters, non-canonical Unicode, and overlong values. display_locator is retained only for authenticated source views; it is never telemetry-safe.

For rename, the server derives the mutable title from the target filename using one versioned helper: remove only the final extension, preserve normalized Unicode, and reject an empty or more-than-500-character title. Move does not change title. This child does not introduce a parser-derived title contract.

### 6.3 Operation shapes

| Operation | Required state | Required evidence | Canonical mutation |
|---|---|---|---|
| rename | active | expected version, expected locator, target in same parent | close old locator, open target, update title |
| move | active | expected version, expected locator, target with different parent | close old locator, open target |
| delete | active | expected version and expected locator | close locator, create tombstone, set deleted |
| restore | deleted | open tombstone ID, retained version, explicit available target | close tombstone, open target, set active |

Expected and target locators must differ. A new event requesting no locator change is invalid; only exact replay may return a previous success. One source has at most one open locator, and one locator belongs to at most one active source within a workspace.

## 7. PostgreSQL evolution

One forward Alembic migration creates lifecycle schema, extends the closed event and intent constraints, and updates typed SQLAlchemy metadata. It requires empty upgrade, fixture upgrade, application smoke, and downgrade tests. If locator or tombstone records exist, downgrade is destructive and requires the repository's explicit destructive Alembic gate.

### 7.1 source_locators

source_locators contains:

    source_locator_id                 UUID primary key
    workspace_id, source_id           canonical ownership pair
    normalized_locator                normalized bounded path
    display_locator                   authenticated display value
    opened_event_id, opened_sequence  opening create or lifecycle event
    closed_event_id, closed_sequence  nullable closing lifecycle event
    opened_at, closed_at              database times

The table has foreign keys to workspace, source, and opening/closing event; a workspace/source history index; a partial unique index on workspace_id plus normalized_locator where closed_event_id is null; and a partial unique index on source_id where closed_event_id is null. Event sequence fields preserve deterministic history order without copying paths into diagnostics.

### 7.2 source_tombstones

source_tombstones contains:

    source_tombstone_id               UUID primary key
    workspace_id, source_id           canonical ownership pair
    delete_event_id                   unique delete event
    retained_version_id               source current version at delete
    retained_locator                  final normalized locator before delete
    actor_kind, actor_id              canonical delete actor
    deleted_at                        database time
    restore_event_id, restored_at     nullable closing linkage

A partial unique index on source_id where restore_event_id is null permits one open tombstone. retained_locator is immutable evidence, not an active locator or a reservation. Tombstone expiry, physical object deletion, holds, and retention remain later work.

### 7.3 Existing tables and legacy state

The migration extends sync_events.event_type from create and update to also permit rename, move, delete, and restore. It extends source-event projection intent constraints so delete accepts a null source_version_id and upsert requires the current source version. Source-event intent identity remains event_id plus projection_kind.

The small-file create path receives an internal initial_locator input. In the same successful canonical create transaction, it inserts the source's first locator alongside the existing source, version, create event, audit entry, and normal upsert intents. The child-4 preflight and raw-content wire format does not change.

The migration never fabricates a locator for a source committed before this behavior. An active legacy source remains readable and updatable through child 4, but lifecycle mutation returns source_locator_missing. The plugin preserves files and mappings, marks reconcile_required, and waits for child 6 to establish a proven locator through manifest reconciliation.

## 8. Domain contracts and API

### 8.1 Command

SourceLifecycleCommand is framework-neutral:

    event_id                 stable UUIDv7 journal identity
    idempotency_key          opaque stable key
    operation                rename | move | delete | restore
    source_id                existing backend-issued UUID
    expected_version_id      required for every operation
    expected_locator         rename/move/delete only
    target_locator           rename/move/restore only
    tombstone_id             restore only
    policy_revision          accepted local snapshot revision
    client_timestamp         optional UTC timestamp

The API derives LifecycleDeviceContext from the bearer credential and rejects inactive, revoked, wrongly scoped, or cross-workspace devices before domain execution. The command has no bytes, hash, R2 receipt, provider detail, user ID, or workspace ID.

The request fingerprint uses existing canonical JSON rules across operation, source, exact version/locator/tombstone operands, policy revision, event, and idempotency identity. Only its SHA-256 is stored. Locator text is absent from audit diffs, diagnostics, and metrics.

### 8.2 Public endpoint

POST /api/sources/lifecycle-events accepts the command above under the existing canonical envelope. It returns committed or committed_replay with opaque source, version, event, and tombstone IDs; event sequence; lifecycle state; and the resulting locator only to the authenticated submitting plugin.

The response never contains a verified receipt, object key, raw policy rule, provider value, or database internals. OpenAPI is snapshot-tested; the shared generated TypeScript client remains the only API contract imported by the plugin.

### 8.3 Exact replay

Before a source lock, the service checks workspace_id, event_id, and idempotency_key. An exact stored fingerprint returns the original serialized outcome. A mismatch in source, operation, version, locator, tombstone, or policy revision rejects with the closed idempotency code and creates one rejection audit after a trusted actor boundary.

For ambiguous PostgreSQL acknowledgement, the adapter discards its connection and does a bounded fresh evidence lookup. It returns committed replay if found; otherwise it reports retryable source_lifecycle_commit_outcome_unknown. It never assumes rollback or inserts a second event.

## 9. Transaction, policy, and projection intents

### 9.1 Lock and validation order

Each lifecycle command uses one PostgreSQL transaction and the Phase 1 bounded retry rules. It acquires transaction-scoped locks in this fixed order:

1. idempotency identity;
2. source_id advisory lock and sources row FOR UPDATE;
3. advisory locks for supplied old/target normalized locators in canonical text order, then relevant locator rows FOR UPDATE;
4. the open tombstone row FOR UPDATE for restore.

No R2, Temporal, provider, or network call occurs inside the transaction. Deadlock, serialization, and bounded lock contention reuse the existing at-most-three cancellable retry policy. Business conflicts and integrity errors never retry.

After replay recheck, the transaction validates source/tombstone state, current version, expected locator, operation classification, locator availability, and policy. It evaluates policy for the resulting locator with source ID, source type, existing media type, size, and target locator. A policy failure is fail-closed for projection planning but does not erase or misrepresent a real locator transition.

### 9.2 Atomic effects

| Operation | Source and lifecycle effects | Intent per projection kind |
|---|---|---|
| rename or move, policy allowed | close prior locator, open target, write event/audit | upsert with current version |
| rename or move, denied/indeterminate | same canonical locator effects | delete with null version |
| delete | close locator, insert tombstone, set deleted state/time, write event/audit | delete with null version |
| restore, policy allowed | close tombstone, open target, clear deleted state/time, write event/audit | upsert with retained current version |
| restore, denied/indeterminate | same canonical restore effects | delete with null version |

Every committed lifecycle event creates exactly one Qdrant and one Neo4j source-event intent. Rejection and exact replay create none. The existing dispatcher, deterministic workflow identity, lease fencing, retry, and Temporal payload privacy contracts are unchanged.

### 9.3 Audit, errors, and telemetry

Successful audit actions are source.locator_renamed, source.locator_moved, source.deleted, and source.restored. Rejections use the same action with rejected result. The safe diff digest includes only canonical IDs, operation, state transition, version ID, and an internal digest of locator operands; it never exposes locator text.

Add closed errors:

    source_lifecycle_input_invalid
    source_locator_missing
    source_locator_conflict
    source_tombstone_not_found
    source_tombstone_closed
    source_lifecycle_version_conflict
    source_lifecycle_commit_outcome_unknown

Diagnostics and metrics may use only registered error/operation/state tokens, counts, durations, attempt counts, and opaque IDs already allowed by the registry. Lifecycle metrics are labeled only by operation and outcome. No source, tombstone, path, or digest becomes a metric label.

## 10. Plugin lifecycle journal

### 10.1 Durable records

The child-4 journal operation vocabulary adds rename, move, delete, and restore. A lifecycle record holds stable event and idempotency IDs, source_id, observed version, expected/target locator operands as local data, optional tombstone ID, state, attempts, retry time, and a closed safe error. It stores no bytes, credential, object key, URL, provider data, or verified receipt.

local_files retains a deleted mapping instead of dropping it:

    local_file_id -> source_id, last committed version and hash, last locator,
                     open tombstone ID, lifecycle state

This persistence allows a later restore to retain canonical identity. A path is rebound only after the lifecycle result is durable.

### 10.2 Capture and dispatch

The existing 250 ms per-path settle runs before fingerprinting. A Vault rename supplies old and new paths; the plugin classifies same-parent changes as rename and other changes as move. Delete queues an ordered delete event and retains its mapping. Any lifecycle observation affecting a pending create/update freezes that pending work; a target path is never inferred as a new create.

Lifecycle events run in bounded foreground passes with the existing one-second-to-five-minute jittered retry behavior. Events for one source_id dispatch in journal order. Restore waits for its delete predecessor to commit and uses the resulting tombstone ID. Offline, timeout, 429, and 5xx retain the event. Collision, stale locator/version, missing/closed tombstone, policy-required conflict, and integrity mismatch are non-retryable and preserve Vault content plus journal evidence.

The plugin automatically restores only when it proves the same retained mapping and last hash. Otherwise it offers a local Restore deleted source action, where the user selects a retained local tombstone and target file/path. Changed target bytes never make a restore request; they are blocked for child 8 conflict handling.

No lifecycle record is coalesced. This keeps delete/restore and path history auditable and prevents a burst of visible moves from being silently collapsed.

### 10.3 Recovery boundary

On source_locator_missing, the plugin does not guess an old path or mint a source ID. It preserves all local data, marks reconcile_required, and guides the user to repair sync when child 6 exists.

This child does not consume remote lifecycle events, apply a remote delete, rename local files from server state, or prevent watcher echo after remote apply. Child 6 owns those behaviors.

## 11. Failure matrix

| Condition | Required behavior |
|---|---|
| Exact replay | Return frozen prior result; no canonical duplicate |
| Same event/key with different request | Reject safely and retain journal evidence |
| Target held by active source | Durable locator conflict; no overwrite or local rebind |
| Current locator or version changed | Durable lifecycle conflict; no automatic retry or merge |
| Delete/restore race | One transaction wins; the other returns safe state conflict |
| Restore target unavailable | Reject; tombstone remains open |
| Restore bytes differ from retained version | Block before request for child 8; do not publish content |
| Target policy deny/indeterminate | Commit canonical state; create projection deletes |
| Legacy source without locator | Mark reconcile_required; do not invent history |
| Dispatcher outage | Commit durable intent with canonical transaction; dispatcher recovers independently |
| Ambiguous commit acknowledgement | Bounded evidence lookup, then retryable unknown if absent |

## 12. Test strategy and acceptance criteria

Implementation starts with failing tests. Required gates are focused Python and TypeScript unit tests, domain/API contracts, generated OpenAPI client compile, migration empty/fixture/downgrade tests, disposable PostgreSQL integration, plugin persistence/restart tests, mobile-boundary static checks, and sanitized Desktop/Mobile test-Vault evidence.

The final implementation proves:

1. A small-file create under this child atomically creates its first canonical locator alongside source, version, event, audit, and intents.
2. Rename and move preserve source and current-version IDs, close one old locator, open one target locator, and classify parent changes correctly.
3. Rename updates title deterministically; move does not.
4. Delete retains the current version, leaves no open locator, creates one tombstone, marks deleted, and emits exactly two delete intents.
5. Restore requires the open tombstone, retained current version, and explicit free target; it retains the original source/version identity and closes the tombstone.
6. Allowed transitions create two upserts; denied or indeterminate move/restore creates two deletes without falsifying lifecycle state.
7. Exact replay is equivalent; mismatched reuse, collisions, stale version/locator, and delete/restore races create no duplicate rows and no silent overwrite.
8. Fault injection proves no partial locator/tombstone/event/audit/intent graph, and uncertain commit handling never guesses.
9. The plugin preserves local_file_id to source_id across delete and restart, orders delete before restore, and never rebinds before commit.
10. Changed local bytes after delete and unresolved lifecycle collisions remain visible, non-destructive blockers rather than uploads or automatic merges.
11. Legacy no-locator sources require reconciliation rather than fabricated history.
12. Locator, content, token, digest, and provider sentinels are absent from logs, traces, errors, JUnit, and device evidence.

## 13. Deferred boundaries

| Owner | Deferred responsibility |
|---|---|
| Child 6 | Cursor delivery, remote lifecycle apply, echo suppression, manifest proof, legacy locator binding, and repair reconciliation |
| Child 7 | Multipart/resumable upload and large-file behavior |
| Child 8 | Lifecycle conflict candidates, merge, user resolution, and edits after delete |
| Phase 3 | Parsing, indexing execution, and consumption of lifecycle projection intents |
| Later retention work | Tombstone grace expiry, holds, and physical canonical-object garbage collection |

## 14. References

- docs/04-OBSIDIAN_SYNC_AND_SOURCES.md
- docs/07-POSTGRESQL_DATA_MODEL.md
- docs/14-SECURITY_PRIVACY_AND_POLICY.md
- docs/16-TESTING_AND_EVALUATION.md
- docs/19-ARCHITECTURE_DECISIONS.md
- docs/20-IMPLEMENTATION_PLAN.md
- docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md
- docs/superpowers/specs/2026-08-17-exclusion-policy-publication-design.md
- docs/superpowers/specs/2026-08-18-plugin-journal-and-small-file-sync-design.md
