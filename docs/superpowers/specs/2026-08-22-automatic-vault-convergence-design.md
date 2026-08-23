# Automatic Vault Convergence and Note Sync Status Design

## 1. Purpose

The Obsidian plugin must converge every eligible Vault note to the canonical
sync service without asking the user to identify or manually rescan old files.
It must also make the local state of each note intelligible: a user can tell
which note is synced, pending, retrying, excluded by policy, conflicting, or
requires reconciliation.

This replaces the explicit `Sync existing files` operator workflow. The
durable SQLite journal, frozen fingerprints, idempotency keys, policy
verification, lifecycle ordering, and server-side preflight remain in force.

## 2. Scope and non-goals

In scope:

- automatic bounded Vault snapshots after journal recovery and after a newly
  accepted policy snapshot;
- automatic re-admission of previously policy-excluded notes when the active
  verified policy allows them;
- one local note-status list and aggregate status surface in Obsidian;
- Desktop WDIO proof that both an existing note and a newly created note
  converge to canonical server state exactly once.

Out of scope:

- changing PostgreSQL/R2 canonical ownership, server policy enforcement, or
  Qdrant/Neo4j projection contracts;
- silently resolving conflicts, deleting user bytes, or uploading notes that
  are excluded by the verified current policy;
- sending note paths, content, hashes, tokens, cookies, or policy operands in
  logs, telemetry, or server status APIs.

## 3. Convergence contract

### 3.1 Triggers

The plugin schedules one bounded automatic snapshot when all of these hold:

1. journal recovery has completed without `reconcile_required`;
2. a verified policy snapshot is available (`policy_ready` or a verified
   offline cache); and
3. either plugin startup completed, an authenticated onboarding accepted a
   policy snapshot, or the accepted policy revision advanced.

Multiple triggers while a snapshot is running coalesce into one follow-up
snapshot. A snapshot never runs concurrently with another snapshot. It is not
a polling daemon and performs no network request itself.

### 3.2 Per-file admission

The snapshot enumerates regular Vault files in deterministic path order and
uses the same `JournalCapture` admission path as a settled create/modify
event. For each file:

- an untracked allowed file records a queued `create` event;
- a tracked file whose observed fingerprint differs from the committed
  fingerprint records/coalesces a queued `update` event;
- an unchanged committed file records no event;
- an excluded file records terminal `excluded_policy` evidence;
- a file with prior terminal `excluded_policy` evidence and a newly allowed
  current decision records an allowed successor event; the old event remains
  immutable audit evidence;
- lifecycle-deferred paths preserve their lifecycle guard and are not treated
  as creates or updates.

The snapshot respects existing maximum-file and batch bounds. Reaching queue
or journal limits sets the existing `reconcile_required` stop; it never drops
or rewrites an existing journal row.

### 3.3 Dispatch

After a snapshot writes any queued event, it requests the existing bounded
queue driver. If a pass is active, the trigger is retained as one coalesced
follow-up request and begins only after the active pass exits. Exactly one
content request may be in flight. The driver keeps current retry, login,
idempotency, frozen-fingerprint and lifecycle-predecessor rules.

There is no `Sync existing files` command and no confirmation modal. `Sync
now` is also removed from the user-facing command surface; convergence is
automatic. A future explicit repair action, if needed, must be a separate
reconcile contract and never bypass this journal.

## 4. Note-status UI

The plugin adds a local-only Sync status view reachable from the settings tab.
It contains an aggregate summary and a deterministic note list. The list may
show a Vault-local normalized path because it is rendered only on the user's
device; paths must not enter telemetry, logs, HTTP status payloads, or test
artifacts.

Each note has one current state, selected from the latest relevant journal
event and local-file mapping:

| UI state | Source condition | User guidance |
|---|---|---|
| Synced | latest event committed/no-change and current fingerprint matches | No action required |
| Queued | eligible queued work exists | Upload starts automatically |
| Syncing | preflight or upload is active | Wait; bytes are frozen safely |
| Retrying | retryable network/server failure is pending | Retry time and closed reason shown |
| Policy blocked | latest relevant event is `excluded_policy` | Show policy revision and closed rule kind; changes re-evaluate automatically after verified policy acceptance |
| Conflict | latest relevant event is `blocked_conflict` | Preserve local bytes and direct user to conflict resolution |
| Reconciliation required | journal or lifecycle mapping requires repair | Automatic sync is stopped; show closed repair guidance |

The aggregate status is derived from the same rows and cannot report `Policy
blocked` for an older excluded event once a later successor for that note is
queued, syncing, or committed. Audit history remains queryable locally but is
not presented as a current blocker.

## 5. Failure behavior

- Missing or invalid policy trust fails closed: no snapshot admission is made;
  existing journal evidence and Vault bytes remain unchanged.
- Offline or retryable server failure leaves queued work durable and shows
  `Retrying`; next allowed foreground trigger resumes it.
- Plugin unload/suspension stops new work and preserves every durable event.
- Snapshot read failure skips only the affected file, records no fabricated
  success, and leaves the next valid trigger able to retry it.
- `reconcile_required`, conflict, size block, and lifecycle guards remain
  distinct, visible states and never become automatic upload attempts.

## 6. Acceptance criteria

1. On clean plugin startup, a pre-existing allowed Markdown note is committed
   without any sync command.
2. A Markdown note created after startup is committed through the same capture
   and queue pipeline.
3. A note first excluded by policy, then allowed by a later authenticated
   policy acceptance, automatically gains one committed successor while its
   original `excluded_policy` event remains audit evidence.
4. Each controlled note produces exactly one canonical source, version, sync
   event and committed upload operation for its declared content identity.
5. The status list presents the controlled note as `Synced` after completion;
   aggregate status contains no stale `Policy blocked` verdict.
6. WDIO proves all three cases against a clean `knowledge-ci-*` stack and
   retains a closed, sanitized final verdict artifact.
7. Unit tests cover trigger coalescing, successor admission, current-status
   selection, list privacy boundaries, unload behavior, and queue limits.

## 7. Affected contracts

- Supersedes the explicit snapshot portions of
  `2026-08-18-plugin-journal-and-small-file-sync-design.md` section 7 and
  the `2026-08-22-existing-files-sync-drain-design.md` workflow.
- Requires updates to `docs/operations/plugin-journal-small-file-sync.md` and
  `docs/operations/source-locator-tombstone-lifecycle.md` command references.
- Does not alter the canonical hierarchy in `docs/01-CANONICAL_ARCHITECTURE.md`
  or the policy fail-closed boundary in `docs/14-SECURITY_PRIVACY_AND_POLICY.md`.
