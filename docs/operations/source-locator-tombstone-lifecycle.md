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
- One lifecycle **command**: `Restore selected tombstone` — the only
  remaining explicit lifecycle command. The former `Sync now` and
  `Sync existing files` commands were removed: convergence is automatic
  (see `docs/operations/plugin-journal-small-file-sync.md`).
- Two diagnostics **commands** (`Run sync self-check`,
  `Copy sync diagnostics`) are owned by the sync-error-tracing runbook
  (`docs/operations/sync-error-tracing.md`); they never touch lifecycle
  state.

Every other surface (logs, telemetry, error messages, command labels)
is restricted to the closed enum vocabulary of spec 11 and Child 5.
No path, locator, source ID, token, fingerprint or remote URL is ever
rendered, with one sanctioned exception: the local-only
`Sync status by note` settings list (see "Redacted diagnostics" below).

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
| `restore_pending`    | `Offline — queued (N)`  | A restore is reserved (target reservation) or its event is queued but not yet acknowledged by the server. |
| `tombstoned`         | `Offline — queued (N)`  | Server confirmed the delete; the local mapping is retained for explicit restore. |
| `restored`           | no extra banner         | Server confirmed the restore; the source is live again.                          |
| `reconcile_required` | `Reconcile required`    | Hard stop; child 6 owns repair before any further sync runs.                     |

A pending lifecycle state blocks other writes to the same file but does NOT
block the device's foreground pass: the bounded queue interleaves the
content lane and the lifecycle lane so a rename / move / delete /
restore commits before the next content event for the same file. Vault
rename and delete events request a bounded pass directly, and the
lifecycle lane drains through the same trigger-driven passes as the
content lane — no command is needed to ship pending lifecycle work.

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

### Explicit restore via command (reservation-first protocol)

The **Restore selected tombstone** command (`apps/obsidian-plugin`'s
command palette) is the only safe path to revive a tombstoned file. The
restore target locator is **reserved durably before any bytes are
staged**, so the automatic convergence lane can never ship the staged
restore bytes as a fresh source at the target (the convergence/lifecycle
lane race fixed 2026-08-25):

1. The picker lists every retained restorable tombstone — `tombstoned`
   rows plus `restore_pending` rows that still hold an open tombstone
   (a durable reservation or an in-flight restore the operator can
   resume) — by its safe plugin-local id (`Tombstone #abcd1234`); paths
   are never shown.
2. The user supplies a target Vault path. Accepting the prompt records
   the durable reservation: the row rebinds to the target path and
   enters `restore_pending` (the journal schema v6 keeps the prior path
   for an explicit cancel). A refused reservation surfaces one closed
   token (see the table below) through a path-free Notice and the
   diagnostics trail, and the journal stays healthy.
3. The user stages the restored bytes at the reserved target path —
   between the prompt and the confirmation (or before opening the
   command, provided the target has not already converged). While the
   reservation holds, the settle admission and the automatic snapshot
   both defer the target path: the staged bytes never converge as a new
   source, and deleting or renaming them is treated as staging action,
   not a tracked lifecycle transition.
4. On confirmation the bytes must hash to the file's last-committed
   fingerprint (the bytes the server acknowledged, not the mutable
   observed fingerprint); the lifecycle capture records a `restore`
   event with the predecessor delete event id and requests one bounded
   queue pass. The tombstone closes only when the server commits; the
   committed receipt rebinds the local mapping to the target path.
5. The explicit **Cancel** button releases the reservation (the row
   returns to its prior path, `tombstoned`). A passive dismissal — the
   close button, escape, or the app losing focus — keeps the
   reservation durable and resumable through the picker.

A hash mismatch at confirmation, a missing retained mapping, a missing
open tombstone or a missing delete predecessor is rejected with the
closed `journal_mutation_failed` `JournalStoreErrorReason`; the
reservation stays resumable. The Sync status refresh is the single
source of truth for what landed and what did not.

Closed reservation refusal tokens (Notice + one `journal_failure` trail
entry each; `restore_reservation_persist_failed` — a failed reservation
persistence — rides the sync-error-tracing runbook's token table):

| Token | Meaning | Operator action |
| --- | --- | --- |
| `restore_target_occupied` | The target path already belongs to another tracked source (a converged duplicate or a genuine other note). | Remove the occupying duplicate or choose another target path. |
| `restore_target_busy` | An upload for the target path is in flight right now. | Retry the command after the current pass settles. |
| `restore_already_pending` | A restore event for this tombstone is already recorded and not yet committed. | Wait for the queue to drain; re-run the command afterwards. |

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
2. No user action can bypass the stop. The Vault listeners, the
   automatic snapshot coordinator and the scheduled retry trigger all
   funnel through the same dispatcher; the driver
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
  histogram (counts only) and the closed blocked reason codes list. The
  per-note `Sync status by note` list renders local Vault paths on-device
  only; that surface is owned by
  `docs/operations/plugin-journal-small-file-sync.md`.
- **Command names** — `Restore selected tombstone`, plus the two
  diagnostics commands listed in the operator surface above.
- **Picker labels** — `Tombstone #abcd1234` (last 8 chars of the
  plugin-local file id). The underlying path is never shown.
- **Notice and confirmation modals** — "No retained tombstones", "Pick
  a tombstone to restore", "Restore Tombstone #abcd1234 to the chosen
  Vault path?", etc.

What the user **does not** see — never, anywhere on the surface:

- Vault paths, including the file name and any parent directories. The
  single sanctioned exception is the local-only `Sync status by note`
  settings list (see above), which renders the user's own paths on-device
  and never lets them leave the device.
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
  `apps/obsidian-plugin/README.md`) — composition wiring, the automatic
  snapshot coordinator and the command surface.
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
the plan. The Desktop journey is a mandatory gate. Execute the full matrix on
a physical Mobile device when available; if it cannot be run, retain an
explicit `DEFERRED` record linked to the one matching BACKLOG row and source
handoff. Never represent deferred Mobile work as observed PASS evidence.
Evidence references must identify a sanitized operator record or closed
deferral; they must never contain paths, locator values, content, digests,
credentials or tokens.

After the services are ready, use the single guarded Desktop entrypoint:

```powershell
$env:CI = "true"
uv run python tools/obsidian_live_acceptance_bootstrap.py --project-name knowledge-ci-<bounded-token>
```

The entrypoint applies the current migration, creates or replays the canonical
identity and Web credential, initializes the policy key, and runs
`.local/e2e-totp-code.py` as a mandatory preflight. The helper produces a code
only after activation. If it reports that no active credential exists, the
entrypoint completes TOTP enrollment and activation through the real Web HTTP
routes, reruns the helper, publishes policy through the existing local helper,
and only then launches the focused WDIO journey. Do not label the missing
credential BLOCKED or deferred unless this bootstrap branch itself fails with
its closed result code. The entrypoint emits status only; do not redirect or
copy child output into acceptance evidence.

## Desktop live acceptance record

- Device: WDIO Obsidian Desktop on Windows
- App version: 1.13.7
- Plugin version: 0.1.0
- Recorded at UTC: 2026-08-21T18:29:13Z
- Operator: Codex automated WDIO operator
- Latest guarded result: PASS (`obsidian_live_acceptance_passed`,
  2026-08-25, disposable project `knowledge-ci-restore-reservation`) after
  the explicit-restore target reservation fix: the journey stages the
  restored bytes on the reserved target between the prompt and the
  confirm, and the previously failing explicit-restore phase commits with
  stable source and version identity, four lifecycle events, four locator
  rows, zero pending and zero blocked. Earlier guarded PASS 2026-08-22
  after the modal settlement fix. Child diagnostics remained outside the
  evidence boundary.

| Scenario | Outcome | Evidence |
| --- | --- | --- |
| Tracked rename | PASS | operator-record:desktop-live-20260825 |
| Tracked move | PASS | operator-record:desktop-live-20260825 |
| Delete | PASS | operator-record:desktop-live-20260825 |
| Explicit restore | PASS | operator-record:desktop-live-20260825 |
| Stable source and version identity | PASS | operator-record:desktop-live-20260825 |
| Pending lifecycle drain | PASS | operator-record:desktop-live-20260825 |

## Mobile live acceptance record

- Status: DEFERRED
- Reason: The convergence/lifecycle lane race that failed the 2026-08-25
  explicit-restore scenario is fixed (the explicit-restore target
  reservation protocol; the mandatory Desktop journey passed guarded the
  same day). The physical matrix must be re-run under the new staging
  procedure — the operator stages the restored bytes on the reserved
  target between the target-path prompt and the confirm — before this
  record may flip to PASS. Retained as DEFERRED until that physical
  evidence exists.
- Source handoff: handoff:source-lifecycle-mobile-deferral
- Backlog key: source-lifecycle-mobile-acceptance
- Implement by: Before Child 6 acceptance closure

| Scenario | Outcome | Evidence |
| --- | --- | --- |
| Tracked rename | DEFERRED | handoff:source-lifecycle-mobile-deferral |
| Tracked move | DEFERRED | handoff:source-lifecycle-mobile-deferral |
| Delete | DEFERRED | handoff:source-lifecycle-mobile-deferral |
| Proven automatic restore | DEFERRED | handoff:source-lifecycle-mobile-deferral |
| Explicit restore | DEFERRED | handoff:source-lifecycle-mobile-deferral |
| Offline capture and reconnect | DEFERRED | handoff:source-lifecycle-mobile-deferral |
| Unload and reload | DEFERRED | handoff:source-lifecycle-mobile-deferral |
| Policy-denied transition | DEFERRED | handoff:source-lifecycle-mobile-deferral |

### Physical observation 2026-08-25 (sanitized)

A physical iPhone executed the full eight-scenario matrix against a
disposable `knowledge-ci-*` project. Sanitized record at closed-token,
counts and timestamp level only (evidence `operator-record:mobile-live-20260825`);
per the ruling the Mobile record above stays DEFERRED and no scenario is
claimed PASS in the contract table.

Passed on the physical device (7 of 8; canonical server commits plus
device diagnostics):

- **Tracked rename** — rename lifecycle event committed server-side at
  23:50:00Z with the source active again; device status settled Ready.
- **Tracked move** — move lifecycle event committed at 23:52:13Z with the
  source active.
- **Delete** — delete lifecycle event committed, source state deleted,
  exactly one new open tombstone, listed by the restore picker.
- **Proven automatic restore** — the first restore event ever committed
  in this environment landed at 23:57:03Z, the source returned to active
  and the device open-tombstone count closed from 4 to 3; the device
  diagnostics export reported Status Ready with two completed pass
  outcomes and zero append failures.
- **Offline capture and reconnect** — a rename captured in airplane mode
  surfaced `Offline — queued (1)` and committed as the next lifecycle
  event at 11:41:36Z immediately after WiFi reconnect.
- **Unload and reload** — the queued event survived an app swipe-kill
  while offline and drained after reconnect on the reloaded app; a clean
  post-reset startup snapshot committed at 11:37:08Z.
- **Policy-denied transition** — device status `Policy blocked` with the
  closed policy blocker line; enforcement was plugin-side (no upload
  attempted, no new failed operation, policy snapshot served from a 304
  cache) and the queue was unaffected.

Failed on the physical device (1 of 8):

- **Explicit restore** — the convergence/lifecycle lane race; the content
  lane ships restored bytes before the restore lifecycle event lands, so
  the server answered the restore-command upload with the closed 409
  conflict family (after one 401 from an expired credential that
  auto-refreshed) and the operation terminalized failed with
  `source_locator_conflict`; the device trail recorded
  `wire_failure · blocked_conflict · source_locator_conflict` at
  00:24:35Z, then the documented `Reconcile required (3)` hard stop; no
  delete commit and no tombstone were involved. The same pre-existing
  race currently fails the mandatory Desktop WDIO journey after delete;
  it is indexed in `docs/handoff/BACKLOG.md` and Mobile stays DEFERRED
  until it is fixed.

Operational findings (sanitized; no note names, paths or content):

- Obsidian Mobile creates new notes under a locale-default untitled name
  before the user names them; the convergence lane ships that default
  name and, when the workspace already holds it, the server correctly
  rejects with the closed conflict token and the journal hard-stops —
  name new notes immediately.
- One vault must live on exactly one device; a vault replicated across
  two devices (for example through iCloud) is double-admitted by both
  journals and the second device always conflicts with the closed
  conflict token.
- iOS onboarding requires returning to the app right after the browser
  approval step; background polling is suspended otherwise.
- A newly created note stays queued until it has content; an empty note
  does not upload.
