# Explicit restore target reservation — design spec

**Date:** 2026-08-25
**Domain:** `apps/obsidian-plugin` journal lifecycle (Child 5 remediation, pre-Child 6)
**Source demand:** `docs/handoff/BACKLOG.md` row 2026-08-25 `small-file-sync`
"Convergence/lifecycle lane race" (`Implement by: Before next plugin release`),
detailed in `docs/handoff/2026-08-24-child-six-deferred-remediation.md` §3 and §7.
**Upstream spec:** `2026-08-20-source-locator-and-tombstone-lifecycle-design.md`
(the Child 5 contract; this document amends the plugin-side restore procedure only —
no canonical/server semantic changes).

## 1. Problem

The explicit-restore command requires the operator to stage the restored bytes
at a target Vault path **before** the command records the restore lifecycle
event (the Child 5 contract verifies the staged bytes against the
last-committed fingerprint at request time). Whenever the target path is not
the still-tracked tombstoned path, that staging window lets the automatic
convergence lane ship the staged bytes as a **fresh source** at the target
locator, before and independently of the restore event. The server then
correctly rejects the restore with the closed conflict family
(`source_locator_conflict`, per the upstream contract "restore returns the
source to active only at an explicitly requested, **available** target
locator") and the journal hard-stops.

Observed evidence:

- Mandatory Desktop WDIO journey `obsidian_wdio_failed_after_delete`
  (2026-08-24/25): 3 journeys → 0 committed restore events, 3 fresh sources
  at restore paths, 6 uploads terminalized `failed/source_locator_conflict`;
  the restored-bytes create committed ~200 ms before the rejected restore
  POST.
- Physical mobile matrix 2026-08-25 scenario 5 (explicit restore): FAIL with
  the same closed tokens on device and server.

## 2. Root cause (plugin side; the server behaves per spec)

Four defects in the explicit-restore flow, all plugin-side:

1. **Staging-window convergence.** A file staged at an untracked target path
   `T` is admitted by the content capture (`#admitNormalizedPath`) as a fresh
   `create` — through the settle admission (Vault create event) or the
   automatic startup snapshot (plugin reload) — and a queue pass ships it as a
   new source at `T`. The per-file lane discipline (lifecycle lane drained
   before the content lane per pass; per-`local_file_id` freeze) cannot see
   this race: the staged create belongs to a **different** `local_files` row
   than the tombstoned one. It is cross-file locator contention.
2. **No durable target reservation.** Nothing marks `T` as spoken-for while a
   restore is pending, so convergence has no way to defer it.
3. **Record-time state lies and double-restore.** Recording a restore event
   sets `lifecycle_state = 'restored'` immediately (`initialStateFor`) before
   the event ever ships; `detectAutomaticRestore` additionally consumes the
   tombstone at record time. Meanwhile the content admission's
   automatic-restore branch (tracked row + open tombstone) can fire on the
   same row and record a **second** restore event → server `tombstone_closed`
   → blocked_conflict hard stop. The closed enum value `restore_pending` is
   currently written by nobody.
4. **No post-commit path rebind.** On a committed restore,
   `recordLifecycleCommittedReceipt` sets the state to `restored` but never
   rebinds `local_files.normalized_path` to the event's target locator. Even a
   perfectly ordered restore to `T ≠ prior path` leaves the local row at the
   old path while the canonical locator `T` is owned by the restored source;
   the next convergence of the staged bytes at `T` then conflicts with the
   canonical locator and hard-stops the journal.

## 3. Fix design — reservation-first explicit restore

Plugin-side only. No server, wire, OpenAPI, or generated-client changes. The
server's `source_locator_conflict` 409 remains the correct guard for genuine
cross-device contention.

### 3.1 Durable reservation (new `restore_pending` meaning)

Schema v6 adds one nullable column `local_files.restore_prior_path` (the
plugin journal's established versioned-migration pattern; upgrade from v5 is
lossless, downgrade drops the column).

`LifecycleRepository.reserveRestoreTarget(localFileId, targetPath)` runs one
transaction that:

- validates the row: exists, has `source_id` + `base_version_id` +
  last-committed fingerprint, an open tombstone, and a stored predecessor
  delete event; rejects (closed `journal_mutation_failed`, as today) when the
  row is not restorable;
- rejects with the closed refusal token `restore_already_pending` when a
  non-terminal `restore` event for the row already exists;
- enforces target availability for the normalized `targetPath`:
  - another row **with a source id** at the target (a converged fresh source
    or a genuine other note) → refused `restore_target_occupied`;
  - a phantom row (no `source_id`) whose content events are all in
    `queued`/`waiting_retry` → those events are frozen terminal
    `deferred_lifecycle` and the phantom row is released (the D7-style
    delete of row + events + attempts) **inside the same transaction**, then
    the reservation proceeds;
  - a phantom row with any event in `preflight`/`uploading` (an in-flight
    upload) → refused `restore_target_busy` (the operator retries after the
    pass settles);
- rebinds the row: `normalized_path = targetPath`,
  `lifecycle_state = 'restore_pending'`, and sets
  `restore_prior_path` to the pre-reservation path when transitioning from
  `tombstoned` (an existing `restore_prior_path` is preserved across
  re-reservation, never chained).

`LifecycleRepository.releaseRestoreTarget(localFileId)` (explicit cancel)
restores `normalized_path = restore_prior_path`,
`lifecycle_state = 'tombstoned'`, clears `restore_prior_path`, in one
transaction. Release happens **only** on an explicit Cancel button; modal
dismissal and crashes leave the durable reservation in place (resumable —
see 3.4).

### 3.2 Confirm-on-reserved `requestRestore`

`requestRestore(localFileId, targetPath)` becomes strict: the row must
already be `restore_pending` and bound to `targetPath` (otherwise closed
`journal_mutation_failed`). Byte verification against the last-committed
fingerprint is unchanged. Recording the restore event now advances the row to
`restore_pending` at record time (`initialStateFor("restore")`), never
`restored`, and never consumes the tombstone eagerly. The tombstone is
consumed and the state advances to `restored` only by the committed receipt
(the existing driver path). `detectAutomaticRestore` drops its eager
`consumeRestoreSuccessor` call for the same reason and refuses (closed
`journal_mutation_failed`) when the row is `restore_pending`.

After `requestRestore` records, the command requests one bounded queue pass
(the same discipline the Vault rename/delete listeners already follow).

### 3.3 Convergence deferral of reserved locators

- `#admitNormalizedPath` (both the settle path and the snapshot path) defers
  a tracked row whose `lifecycle_state` is `restore_pending`: no create, no
  update, no automatic-restore detection. `#isLifecycleDeferredPath` treats
  such rows as deferred.
- `captureDelete` and `captureRename` on a `restore_pending` row are quiet
  no-ops (return `null`): the reservation owns the row; deleting or renaming
  the staged bytes mid-flow is operator staging action, not a tracked
  lifecycle transition. If the staged bytes are absent or altered at confirm
  time, the unchanged fingerprint check refuses the restore and the
  reservation remains resumable.

### 3.4 Picker and resumption

`readTombstonedLocalFileIds` becomes `readRestorableLocalFileIds`: rows with
an open tombstone in states `tombstoned` or `restore_pending`. A crash or
dismissal leaves a resumable reservation listed by the picker; re-running the
command re-reserves (rebinding the target when a new path is supplied) or is
refused `restore_already_pending` while the recorded event is still in
flight.

### 3.5 Post-commit rebind

`recordLifecycleCommittedReceipt` case `restore` additionally sets
`normalized_path` to the event operands' `target_locator` (read in the same
transaction) before the state advances. After a committed restore the local
row sits at the target path, state `restored`, tombstone consumed; the next
admission of the staged bytes matches the last-committed fingerprint and is a
no-op. `restore_prior_path` is cleared on commit.

### 3.6 Command flow (plugin.ts)

1. Picker (restorable rows, unchanged safe labels).
2. Target-path prompt — accepting the prompt **first reserves** the target
   (3.1). Refusals surface immediately (3.7) and the flow ends.
3. The operator stages the restored bytes at the reserved target path (the
   confirm modal no longer blocks this contractually; staging between the
   prompt and the confirm is the documented procedure, and staging before
   the command is still correct whenever the target has not yet converged).
4. Confirm → `requestRestore` (verify + record + bounded pass). A
   fingerprint mismatch refuses with the unchanged closed
   `journal_mutation_failed` and the reservation stays.
5. Explicit Cancel at the prompt or the confirm → `releaseRestoreTarget`.
   Modal dismissal never releases.

The WDIO journey stages the bytes between the prompt and the confirm via
`browser.execute` and no longer disables/re-enables the plugin.

### 3.7 Diagnostics surface (mandatory task per AGENTS.md)

New closed refusal tokens, declared once and mirrored into the trail token
union:

- `restore_target_occupied`, `restore_target_busy`,
  `restore_already_pending` — surfaced as (a) a closed user Notice with no
  path/locator text, and (b) one `journal_failure` diagnostics-trail entry
  via the failure reporter;
- `restore_reservation_persist_failed` — joins
  `SYNC_COMPOSITION_READ_FAILURE_TOKENS` (mirrors
  `lifecycle_reconcile_persist_failed`) for a failed reservation
  persistence.

The settings lifecycle histogram already renders `restore_pending`
("Restore pending"); the status-bar pending line and the runbook state table
wording are updated to "a restore is reserved or queued but not yet
acknowledged by the server". No raw path, locator, source id, tombstone id,
fingerprint or token ever enters any surface.

## 4. Acceptance criteria

1. Explicit restore to a target path different from the tombstoned path
   commits exactly once with stable source and version identity; the local
   row ends bound to the target path, `restored`, tombstone consumed, and no
   fresh source exists at the target locator.
2. Staged bytes at a reserved target never converge as a new source: both
   the settle admission and the automatic snapshot defer a `restore_pending`
   locator (pinned by tests for the queued-phantom, in-pass, and
   post-commit windows).
3. Every new closed error path surfaces its reason token readably (Notice +
   trail for the refusal tokens; trail for the persist token); nothing is
   swallowed silently and no surface carries path/locator/digest content.
4. A committed restore rebinds `normalized_path` in the same transaction as
   the committed receipt; a released reservation restores the prior path
   atomically; a dangling reservation is resumable through the picker.
5. The server contract is untouched: the `content_source_locator_conflict`
   wire-corpus entry, the closed 409 semantics and the exact-replay contract
   are unchanged.
6. The mandatory Desktop WDIO journey (restaged protocol) returns
   `obsidian_live_acceptance_passed`, and the physical mobile matrix
   explicit-restore scenario passes under the documented staging procedure.
7. Journal schema v5 databases upgrade to v6 losslessly (and v6 rows read
   back with `restore_prior_path = null` before any reservation); the
   full plugin suite, type check, lint and build are green; `poe verify`
   stays green.
8. No regression: same-path automatic restore still restores and consumes
   in one step; rename/move/delete capture and drain semantics are
   unchanged; the offline capture/reconnect and unload/reload behaviours are
   unchanged.

## 5. Error and edge cases

| Case | Behaviour |
| --- | --- |
| Target occupied by a tracked source row | Refused `restore_target_occupied`; tombstone stays open; journal healthy; Notice + trail. |
| Target phantom row with in-flight upload (`preflight`/`uploading`) | Refused `restore_target_busy`; operator retries after the pass settles. |
| Target phantom row with only `queued`/`waiting_retry` creates | Frozen `deferred_lifecycle` + released inside the reservation transaction; reservation proceeds. |
| Restore event already recorded and non-terminal | Refused `restore_already_pending`. |
| Bytes at confirm mismatch / absent | Unchanged closed `journal_mutation_failed`; reservation retained and resumable. |
| Reservation persistence failure | Closed `journal_mutation_failed` thrown + `restore_reservation_persist_failed` trail token. |
| Explicit Cancel | Atomic release to the prior path, state `tombstoned`. |
| Modal dismissal / crash / reload | Durable reservation retained; resumable via the picker; target stays deferred from convergence. |
| Delete/rename notification on a reserved row | Quiet no-op (staging action, not a lifecycle transition). |
| Genuine cross-device locator contention at ship time | Existing server 409 → `blocked_conflict` terminal handling (correct per spec; unchanged). |
| Staged bytes still present after commit | Next admission matches the last-committed fingerprint → no-op (no duplicate source). |

## 6. Non-goals

- Any server, wire-corpus, OpenAPI or generated-client change.
- Repair of an already-converged duplicate (the plugin refuses the restore
  readably instead; removing the duplicate stays an operator action until
  Child 6 reconciliation lands).
- The untitled-name transit race and the one-vault-two-devices double-admit
  (documented operational findings; separate families).
- Mobile acceptance by mock or inference — the physical matrix is the only
  accepted evidence (AGENTS.md live-test gate).
