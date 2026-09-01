# Source Conflict Capture and Resolution Design

**Date:** 2026-09-02
**Status:** Approved design; ready for implementation planning
**Owning phase:** Phase 2, Child 8 — Conflict Inbox and resolution

## 1. Objective

Child 8 makes concurrent or stale edits a durable, visible, user-resolved
outcome. A user must never lose verified local work or silently overwrite a
canonical current version. The feature covers candidate retention, bounded
three-way text merge, binary choice, lifecycle conflicts, and conflict races
across Obsidian devices.

The canonical state remains PostgreSQL plus immutable Cloudflare R2 bytes.
Obsidian is an editable client, not an authority. A conflict candidate is a
verified immutable object, but never a source version and never current merely
because it was captured.

## 2. Scope and non-goals

In scope:

- one server-authoritative `source_conflicts` aggregate for stale content,
  edit/delete, delete/edit, and concurrent locator races;
- capture of verified local candidate bytes or a no-byte deletion intent;
- device-authenticated Conflict Inbox APIs and an Obsidian plugin Conflict
  Inbox;
- bounded editable three-way merge for text and Markdown;
- `keep_remote` / `keep_local` binary resolution without automatic merge;
- resolution current-version and policy rechecks, idempotency, audit, and
  safe repair after a local Vault apply failure;
- unit, migration, contract, integration, two-device, and plugin coverage.

Out of scope:

- a Web Admin conflict editor or resolver;
- parser/indexer-assisted merge, semantic merge, or automatic resolution;
- candidate-object garbage collection, retention expiry, or physical deletion;
- repair of the indexed delete-and-recreate cursor-gap defect. That work stays
  in `device-sync` and is due before its next live round;
- the Child 9 physical Mobile/operations acceptance matrix.

## 3. Fixed decisions and invariants

1. The server owns one conflict aggregate. `small_file_sync` and
   `source_lifecycle` delegate conflict capture through a domain port rather
   than creating independent conflict state machines.
2. Every conflict binds immutable evidence: origin event/device, base version,
   remote version observed during capture, candidate kind, candidate object
   when present, and a locator snapshot. It is not rewritten to describe later
   remote state.
3. Candidate bytes are admitted only after the existing verified-object flow
   proves their digest, size, and media type. Capture changes no source current
   pointer.
4. Exact replay of a capture event returns the original conflict. A resolution
   is a new accepted operation with a new event identity.
5. A resolution binds the reviewed remote version and rechecks both current
   version and policy in its canonical transaction. If the remote advanced, the
   attempted resolution is recorded as stale and a successor conflict binds
   the newer remote version; the predecessor evidence remains immutable.
6. `keep_remote` creates no redundant source version. `keep_local` and
   `save_merged` each create exactly one immutable source version against the
   reviewed remote version, then link it to the resolution.
7. Server canonical commit precedes any plugin Vault write. A failed local
   apply is a durable local repair state, never a reason to roll back the
   canonical resolution.
8. Text merge is user-mediated. Binary has no automatic merge or
   last-write-wins path. All winning choices require an explicit user action.
9. Raw candidate bytes, paths, diff text, merged drafts, tokens, object keys,
   and presigned URLs never enter logs, diagnostics, metrics, traces, or
   Temporal history.

## 4. Domain model

### 4.1 Conflict variants

`conflict_kind` is a closed enum:

| Kind | Meaning | Candidate requirement |
|---|---|---|
| `stale_content` | Local create/update is based on a non-current version. | Verified content object required. |
| `edit_remote_delete` | Local content edit races a remote delete/tombstone. | Verified content object required. |
| `delete_remote_edit` | Local deletion intent races a remote content update. | No content object; deletion intent required. |
| `locator_collision` | Rename/move/restoration conflicts with a canonical locator state. | Locator snapshot required; content object required only when local bytes changed. |

The database constrains these shapes; a conflict cannot be partly a deletion
and partly a content candidate. A source ID may be null only while a locator
collision has not identified a canonical source; all other conflict kinds bind
one source.

### 4.2 Persistent record

`source_conflicts` contains at least:

- conflict UUID, workspace ID, nullable source ID, and `conflict_kind`;
- originating sync event ID, originating device ID, and idempotency identity;
- nullable base version ID, observed remote version ID, and an immutable
  normalized-locator snapshot represented only in canonical state;
- candidate kind plus nullable verified candidate content-object reference;
- `open`, `resolving`, `resolved`, or `superseded` status and timestamps;
- nullable resolution kind, resolution event ID, resulting version ID, and
  successor conflict ID;
- audit correlation data made only from opaque identifiers and closed labels.

Foreign keys preserve all evidence needed by an open conflict. Candidate
references prevent a future canonical GC from deleting bytes while any conflict
or successor requires them. Child 8 defines no deletion job.

### 4.3 State machine

```text
capture:                 open
begin resolve:           open -> resolving
winner accepted:         resolving -> resolved
remote/policy stale:     resolving -> superseded; create successor(open)
exact replay:            terminal state is returned, never duplicated
```

`resolved` and `superseded` are terminal. A transient transport failure before
transaction commit leaves the conflict open; a lost acknowledgement is resolved
by the new resolution event identity's exact replay. No client may alter a
captured evidence row.

## 5. Capture and resolution flow

### 5.1 Capture

1. An existing sync or lifecycle service detects a stale base or a lifecycle
   collision. It returns a conflict instruction rather than retrying or
   publishing current state.
2. For a content candidate, the plugin completes the existing single-part or
   multipart verified-object flow. Transient upload/verification failure stays
   in the durable journal retry path and does not create a conflict yet.
3. The capture service rechecks authorization and policy, acquires the same
   source/locator consistency boundary as the competing mutation, and in one
   PostgreSQL transaction inserts the accepted sync event, conflict record,
   candidate reference or deletion intent, and audit row. It does not change
   the source current pointer.
4. A replay finds conflict membership before the normal published/no-change
   classifier and returns the stored conflict outcome.
5. The plugin persists a `blocked_conflict` reference and surfaces it in the
   Conflict Inbox; it never retries conflict capture as a normal network
   failure.

### 5.2 Resolution

1. The Inbox reads safe conflict metadata and downloads base, observed remote,
   and candidate bytes only through the verified-read boundary.
2. Text/Markdown creates a bounded three-way merge proposal locally. The user
   selects `keep_remote`, `keep_local`, or edits then selects `save_merged`.
   Binary shows safe name/media type/size/hash information and optional safe
   previews, then permits only `keep_remote` or `keep_local`.
3. A changed local or merged result first becomes a verified candidate object
   through the existing upload flow. The resolve command carries only its
   verified reference, never raw content.
4. In one canonical transaction, the resolver validates ownership, conflict
   state, reviewed remote version, current source/locator state, and active
   policy. It atomically accepts remote or publishes the local/merged object,
   records the resolution event and audit, and closes the conflict.
5. If the reviewed remote version is no longer current, the resolver records a
   stale resolution attempt, creates a successor with the new observed remote,
   retains the original candidate and evidence, and requires another explicit
   review. It does not overwrite the original record.
6. On success, the plugin applies the returned current bytes/tombstone/locator
   outcome atomically with echo suppression. If that fails, it stores
   `local_apply_pending` in its durable journal/trail and retries safe local
   application without issuing another resolution.

## 6. API and plugin boundary

All routes are under `/api`, use the canonical envelope and existing Obsidian
bearer device credentials with `obsidian_sync` scope. Workspace/user identity
is derived from the credential. API adapters validate wire data and map closed
domain errors; they contain no conflict business logic.

| Operation | Contract intent |
|---|---|
| `GET /api/sync/conflicts` | Paginated safe metadata for the authenticated device workspace. |
| `GET /api/sync/conflicts/{conflict_id}` | One conflict's safe metadata, choices allowed by kind/media type, and immutable evidence identifiers. |
| `GET /api/sync/conflicts/{conflict_id}/evidence/{role}` | Verified streaming read of exact `base`, `remote`, or `candidate` bytes after policy recheck. |
| `POST /api/sync/conflicts/{conflict_id}/resolve` | `conflict_id`, reviewed remote version, resolution kind, new event/idempotency identity, and optional verified candidate reference. |

All four route shapes must add OpenAPI, generated client, route/auth/error,
and contract coverage in the same change. No endpoint exposes a raw R2 key,
provider receipt, secret, or a cross-workspace conflict.

The plugin owns:

- durable conflict references and `local_apply_pending` repair work in the
  journal schema;
- Conflict Inbox list/detail UI, explicit user choices and local merge draft;
- bounded merge execution, verified downloads, and atomic Vault apply with
  echo suppression;
- closed-token diagnostics and a user-visible repair/status surface.

The plugin must not store raw candidate bytes or merged drafts in the journal.
The Vault is the user-visible working copy; drafts can be held only in bounded
ephemeral memory until explicitly uploaded or discarded.

## 7. Failure, privacy, and observability contract

| Condition | Required outcome |
|---|---|
| Stale base or lifecycle race | Capture/open conflict; no silent overwrite and no normal retry. |
| Upload/download/object verification transient failure | Bounded retry at existing boundary; no conflict until candidate is verified. |
| Policy denial at capture or resolution | Fail closed, preserve local work, no unauthorized bytes published. |
| Conflict already resolved/superseded | Return typed terminal/replay result; no second winner. |
| Reviewed remote changed | Immutable stale predecessor plus open successor. |
| Canonical resolution acknowledgement lost | Replay the resolution event identity; no duplicate version. |
| Plugin Vault apply failure | `local_apply_pending`, closed reason token, bounded local repair; canonical result stands. |
| Candidate/evidence unavailable or hash invalid | Fail closed with typed reason; never replace local bytes. |

Child 8 adds closed safe error codes and diagnostics events for every new
closed error path. Metrics use a closed, low-cardinality conflict kind/status/
resolution/error-code universe; IDs, locators and digest values are not metric
labels. Audit records authentication, capture and resolution without raw
content. The operation docs name the Inbox repair and reason-token readback.

## 8. Verification and acceptance

Implementation starts with failing tests. The completed slice must prove:

1. Migration upgrade/downgrade, indexes, foreign keys, and conflict-kind
   constraints work from empty and existing Child 7 schemas.
2. A content candidate is verified and referenced before capture; capture
   leaves the current pointer unchanged and exact replay returns one conflict.
3. Delete/edit and locator collision records preserve valid, non-ambiguous
   evidence without sending or inventing bytes.
4. `keep_remote` closes the conflict without creating a source version;
   `keep_local` and `save_merged` create exactly one version and bind the
   resolution event/audit record.
5. Concurrent captures, two resolutions, capture versus resolution, current
   pointer advancement, and policy revision changes converge without deadlock,
   duplicate versions, or silent overwrites.
6. A stale resolution produces an immutable predecessor and correctly bound
   successor; replay remains deterministic for both records.
7. API/OpenAPI/generated-client tests prove device authorization, workspace
   isolation, strict wire validation, error mapping, and no sensitive leakage.
8. Plugin tests cover inbox rendering, bounded text merge, binary choices,
   user-discarded drafts, verified reads, atomic apply/echo suppression, and
   restart-safe `local_apply_pending` recovery.
9. Two-device integration/E2E covers concurrent Markdown, text, binary,
   edit/delete, delete/edit, and locator races; each ends in visible user
   resolution with no silent overwrite.
10. Diagnostics/telemetry fixtures prove no raw paths, bytes, diff/merge text,
    tokens, object keys, URLs, or full digests are emitted.

Relevant final gates include focused Python and plugin suites, ruff, mypy
strict, eslint, TypeScript strict, OpenAPI snapshot/generated-client compile,
migration integration tests, device-sync integration tests, and the required
Desktop live Conflict Inbox journey. The physical Mobile matrix remains a
Child 9 gate and is recorded only through the existing mobile backlog item.

## 9. References

- `docs/00-PRODUCT_VISION_AND_PRD.md`
- `docs/01-CANONICAL_ARCHITECTURE.md`
- `docs/15-OBSERVABILITY_AND_ALERTING.md`
- `docs/19-ARCHITECTURE_DECISIONS.md`
- `docs/20-IMPLEMENTATION_PLAN.md`
- `docs/superpowers/specs/2026-08-15-phase-two-obsidian-sync-design.md`
- `docs/superpowers/specs/2026-08-18-plugin-journal-and-small-file-sync-design.md`
- `docs/superpowers/specs/2026-08-20-source-locator-and-tombstone-lifecycle-design.md`
- `docs/superpowers/specs/2026-08-28-resumable-multipart-mobile-upload-design.md`
