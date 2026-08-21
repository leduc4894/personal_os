# Source-locator and tombstone lifecycle operations

This guide covers the operator playbook for the Child 5 source-locator
and tombstone lifecycle across the plugin and canonical backend. It describes
the closed state machine, the safe operator actions, the handling of
`reconcile_required`, the exact replay semantics, the deletion
semantics and the redacted diagnostics the plugin surfaces.

The operator surface is small and deliberately redacted:

- One **status-bar item** with one of the six spec-11 closed values plus
  the pending count.
- One **settings tab** with the same closed status, the closed blocker
  guidance and the sync settings.
- Three **commands**: `Sync now`, `Sync existing files` and
  `Restore selected tombstone`.

Every other surface (logs, telemetry, error messages, command labels)
is restricted to the closed enum vocabulary of spec 11 and Child 5.
No path, locator, source ID, token, fingerprint or remote URL is ever
rendered.

Live setup details (launcher, stack secrets, restart sequence) live at
[`.local/RESTART.md`](../../.local/RESTART.md) — never copy them here.

## State machine and visual cues

The closed `LifecycleLocalFileState` enum has exactly eight values:

| State                | Visual cue (status-bar) | Where it appears                                                                 |
| -------------------- | ----------------------- | -------------------------------------------------------------------------------- |
| `active`             | no extra banner         | The default; content surface is the source of truth.                             |
| `rename_pending`     | `Offline — queued (N)`  | A rename event is queued but not yet acknowledged by the server.                 |
| `move_pending`       | `Offline — queued (N)`  | A move event is queued but not yet acknowledged by the server.                   |
| `delete_pending`     | `Offline — queued (N)`  | A delete event is queued but not yet acknowledged by the server.                 |
| `restore_pending`    | `Offline — queued (N)`  | A restore event is queued but not yet acknowledged by the server.                |
| `tombstoned`         | `Offline — queued (N)`  | Server confirmed the delete; the local mapping is retained for explicit restore. |
| `restored`           | no extra banner         | Server confirmed the restore; the source is live again.                          |
| `reconcile_required` | `Reconcile required`    | Hard stop; child 6 owns repair before any further sync runs.                     |

A pending lifecycle state blocks other writes to the same file but does NOT
block the device's foreground pass: the bounded queue interleaves the
content lane and the lifecycle lane so a rename / move / delete /
restore commits before the next content event for the same file.

The closed blocked reason codes surface the **why** a lifecycle event
is stuck:

| Code                     | Meaning                                                                  |
| ------------------------ | ------------------------------------------------------------------------ |
| `idempotency_conflict`   | The server rejected the replay because the captured idempotency identity drifted. |
| `version_conflict`       | The server has a newer base version than the event expected.              |
| `locator_conflict`       | The locator the event claimed no longer matches the server's mapping.    |
| `tombstone_not_found`    | The captured tombstone id has no row on the server.                      |
| `tombstone_closed`       | The captured tombstone id was already consumed by an earlier restore.     |
| `commit_outcome_unknown` | The server could not determine the outcome; durable retry is unsafe.     |
| `integrity_failed`       | The closed-enum integrity failure: the event closes terminal.             |

## Safe operator actions

The brief freezes three explicit operator actions; everything else is
an automatic flow the lifecycle capture handles.

### Rename via the Vault

Use the Obsidian Vault rename command (or the file explorer) to rename
or move a tracked file. The lifecycle capture listens to the Vault's
`rename` event and records the rename / move as one durable event in
the same transaction as the `local_files` path rebind. **Do not** edit
the journal directly; the `local_files.normalized_path` rebind and the
captured fingerprint are atomic and the content lane defers any
still-pending content event for the same file.

A rename or move that does not match the observed fingerprint is
rejected as `journal_mutation_failed`. Reissue the rename through the
Vault; the durable event lands in the queue and the next bounded pass
ships it.

### Delete via the Vault

Use the Obsidian Vault delete command. The lifecycle capture listens to
the `delete` event and records a delete event together with the issued
tombstone id; the local mapping is marked `tombstoned` (not removed)
and the durable mapping stays for restore eligibility.

The tombstone is **retained locally** (see "Deletion semantics" below).
Do NOT delete the row directly; the lifecycle capture owns the only safe
path to release it (a successful restore successor).

### Explicit restore via command

The **Restore selected tombstone** command (`apps/obsidian-plugin`'s
command palette) is the only safe path to revive a tombstoned file:

1. The picker lists every retained tombstone by its safe
   plugin-local id (`Tombstone #abcd1234`); paths are never shown.
2. The user supplies a target Vault path; the bytes at that path must
   hash to the file's last-committed fingerprint (the bytes the server
   acknowledged, not the mutable observed fingerprint).
3. The user confirms; the lifecycle capture records a `restore` event
   with the predecessor delete event id and consumes the tombstone.

A hash mismatch, a missing retained mapping, a missing open tombstone
or a missing delete predecessor is rejected with the closed
`journal_mutation_failed` `JournalStoreErrorReason`. The Sync status
refresh is the single source of truth for what landed and what did not.

Automatic restore is permitted ONLY when the capture detects a
tombstoned path re-appearing with bytes that hash to the last-committed
fingerprint. **Path reuse alone is never sufficient** — a Vault create
on a tombstoned path with different bytes flips the row to
`reconcile_required` and refuses the create.

## `reconcile_required` handling

The `reconcile_required` state is a hard stop. It appears when:

- A tombstoned path re-appeared with bytes that do NOT match the
  last-committed fingerprint (the row is flipped to
  `reconcile_required`, the open tombstone is cleared so the file is
  not eligible for automatic restore and the global
  `journal_meta.is_reconcile_required` flag is set).
- A lifecycle event references a missing predecessor event id (a row
  in `journal_events` is gone but `lifecycle_event_operands.
  predecessor_event_id` still references it).
- A lifecycle event has no matching `lifecycle_event_operands` row
  (the keyed-extension write was lost).

When `reconcile_required` appears:

1. The status refresh stops the queue driver. The foreground pass
   never starts.
2. No user action can bypass the stop. The `Sync now` command and the
   Vault listeners both funnel through the same wrapper; the driver
   returns `stopped` until child 6 repairs the journal.
3. The settings tab repeats the closed `reconcile_required` guidance
   line — "Sync stopped: journal reconciliation is required before
   syncing can continue. Repair and reconciliation are owned by child
   6." — and never logs the offending row, path, source ID or locator.

The operator action is: leave the journal untouched until child 6 ships.
Manual SQL edits are unsafe and will not undo the durable
`reconcile_required` flag.

## Exact replay semantics

Every queued event carries a stable `(event_id, idempotency_key)` pair
the lifecycle capture minted at capture time. The bounded queue pass
selects the same `event_id` on replay and ships the same wire body,
including the same operands (expected / target locator, source ID,
expected version, tombstone ID, policy revision, predecessor event
id).

The server's exact-replay contract (spec 10.3) returns either:

- the **original receipt** when the server already owns the event, so
  the durable `journal_events.state` flips to `committed` and the
  `lifecycle_event_operands.server_receipt_tombstone_id` is set when
  the server-returned tombstone id differs from the operands-derived
  one; or
- a **reopened flow** when the server cannot determine the outcome —
  the durable `journal_attempts` row carries the closed safe-error
  label, the next eligible retry time is persisted and the next
  bounded pass retries with the SAME identity.

A restore successor sources the tombstone id exclusively from the
predecessor delete event's `server_receipt_tombstone_id` column (the
server is the only authority over the tombstone domain). The
operands-derived tombstone id is a fallback used only when the
predecessor's server receipt has not landed yet.

## Deletion semantics (tombstone retained locally)

When the server commits a delete, the local mapping is marked
`tombstoned` and the tombstone row is retained in the durable journal
under `lifecycle_event_operands`. The row is pruned only by a
successful restore successor (`consumeRestoreSuccessor`) or by a
future child that owns explicit tombstone pruning. Until then:

- The picker continues to list the tombstone in
  `Restore selected tombstone`.
- The lifecycle state stays `tombstoned`.
- The `local_files` row stays reachable by the restore surface.

Operators should NOT delete a tombstone row directly: the durable
restore eligibility depends on it, and removing the row without a
committed restore successor would lose the only authority over the
tombstone id the server mailed.

## Redacted diagnostics (what the user sees vs what they don't)

The status surface, the settings surface and the command labels carry
ONLY the closed enum vocabulary. Specifically:

- **Status bar text** — one of the six spec-11 values plus the pending
  count. Nothing else.
- **Settings status** — same closed value, plus the closed blocker
  guidance table for the active blockers (16 MiB boundary, authorized
  policy refresh, no-overwrite conflict / lifecycle deferrals,
  queue-preserving browser login, child-6 repair of a
  `reconcile_required` journal).
- **Settings sync status tab** — the closed redacted lifecycle-state
  histogram (counts only) and the closed blocked reason codes list.
- **Command names** — `Sync now`, `Sync existing files`, `Restore
  selected tombstone`.
- **Picker labels** — `Tombstone #abcd1234` (last 8 chars of the
  plugin-local file id). The underlying path is never shown.
- **Notice and confirmation modals** — "No retained tombstones", "Pick
  a tombstone to restore", "Restore Tombstone #abcd1234 to the chosen
  Vault path?", etc.

What the user **does not** see — never, anywhere on the surface:

- Vault paths, including the file name and any parent directories.
- Locator text (`expected_locator`, `target_locator`,
  `last_locator`).
- Source IDs, base version IDs, predecessor event IDs.
- Tombstone IDs (the safe picker label uses the plugin-local file id
  suffix, never the tombstone id itself).
- Fingerprints (SHA-256, byte size, media type).
- Access tokens, refresh tokens, authorization headers, session identifiers.
- Server URLs, request correlation IDs, response bodies or status codes
  (the bounded retry translates everything onto closed safe-error
  labels).
- The provider's error code, registry code, exception class or message.

Failures are surfaced as the closed `JournalStoreError` reason
(`journal_mutation_failed`, `journal_query_failed`, etc.); the raw
exception never reaches a thrown error, a console call, the settings
tab or any telemetry.

## Linked references

- Plugin composition (`apps/obsidian-plugin/src/plugin.ts` and
  `apps/obsidian-plugin/README.md`) — composition wiring and the three
  command surface.
- Lifecycle contracts (`apps/obsidian-plugin/src/journal/lifecycle-contracts.ts`)
  — the closed `LifecycleLocalFileState` enum and the operand record.
- Status projection (`apps/obsidian-plugin/src/journal/status.ts`) —
  the closed surface that folds the lifecycle histogram and the
  blocked reason codes onto the spec-11 sync status.
- Live launcher / secrets — [`.local/RESTART.md`](../../.local/RESTART.md)
  (NEVER copy launcher details or secrets into this guide).

## Live acceptance procedure

Follow [`.local/RESTART.md`](../../.local/RESTART.md) exactly. Use a disposable
`knowledge-ci-*` project, the repository launchers, both policy workers, Web
Admin on port 38000 and the existing tunnel. Run the focused WDIO command from
the plan, then execute the full matrix on a physical Mobile device. Evidence
references must identify a sanitized operator record; they must never contain
paths, locator values, content, digests, credentials or tokens. These records
are mandatory gates, not deferred backlog work.

## Desktop live acceptance record

- Device: WDIO Obsidian Desktop on Windows
- App version: 1.13.7
- Plugin version: 0.1.0
- Recorded at UTC: 2026-08-21T14:55:32Z
- Operator: Codex automated WDIO operator

| Scenario | Outcome | Evidence |
| --- | --- | --- |
| Tracked rename | FAIL | Task 12 handoff Desktop gate |
| Tracked move | NOT REACHED | Task 12 handoff Desktop gate |
| Delete | NOT REACHED | Task 12 handoff Desktop gate |
| Explicit restore | NOT REACHED | Task 12 handoff Desktop gate |
| Stable source and version identity | NOT REACHED | Task 12 handoff Desktop gate |
| Pending lifecycle drain | NOT REACHED | Task 12 handoff Desktop gate |

## Mobile live acceptance record

- Device: PENDING
- App version: PENDING
- Plugin version: PENDING
- Recorded at UTC: PENDING
- Operator: PENDING

| Scenario | Outcome | Evidence |
| --- | --- | --- |
| Tracked rename | NOT RUN | PENDING |
| Tracked move | NOT RUN | PENDING |
| Delete | NOT RUN | PENDING |
| Proven automatic restore | NOT RUN | PENDING |
| Explicit restore | NOT RUN | PENDING |
| Offline capture and reconnect | NOT RUN | PENDING |
| Unload and reload | NOT RUN | PENDING |
| Policy-denied transition | NOT RUN | PENDING |
