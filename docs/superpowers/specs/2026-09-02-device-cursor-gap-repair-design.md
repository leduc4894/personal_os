# Device Cursor-Gap Repair Design

## Goal

Make the plugin's explicit **Repair sync** action converge when a retained-event
cursor gap arises inside a reconciliation created by delete-and-recreate of a
device-committed file.

## Scope

- Extend the existing plugin-level device-sync journey that already proves a
  STARTUP cursor-gap repair.
- Preserve the existing server `device_cursor_gap` contract and all closed
  diagnostic tokens.
- Change only the client-side reconciliation/repair state transitions needed
  for the delete-and-recreate shape to finish its manifest repair and release
  the repair barrier.

## Required behavior

1. The test fixture starts from a device-committed file, deletes it locally,
   recreates the same locator with different bytes, and defers the server
   sequence during the resulting reconciliation.
2. The deferred sequence produces the existing `device_cursor_gap` result.
3. After the reconciliation's manifest repair settles with either existing
   closed local-conflict action outcome (`device_manifest_local_diverged` or
   `identity_ambiguous`), invoking **Repair sync** must run the canonical
   gap-repair path rather than returning to the same barrier.
4. Repair must converge the persisted journal state: no active repair run,
   no `device_cursor_gap` repair barrier, and the cursor/reconciliation
   completion fence advances exactly through the repaired server state.
5. Retrying the action after successful convergence is idempotent; it neither
   replays the same event nor creates a new conflict/repair row.

## Non-goals

- No change to device-sync HTTP routes, PostgreSQL schema, error registry, or
  server event-retention policy.
- No queue-pass watchdog work unless investigation proves it is necessary for
  this reproduction; that is a separate defect and is not a current BACKLOG
  row.
- No Desktop or Mobile live journey, including the downstream lifecycle-ring
  smoke readback.

## Design constraints

- The journal remains the durable client authority; all recovery state changes
  must use its serialized writer and remain replay-safe across plugin reload.
- The existing `device_cursor_gap` reason is retained and must remain visible
  through the sync diagnostics/status path; no raw locators, bytes or IDs may
  reach diagnostics.
- Reuse the current manifest repair and completion-fence mechanisms instead of
  adding a competing reconciliation path.

## Acceptance criteria

- A new failing journey test for the exact delete-and-recreate cursor-gap
  shape passes after the smallest repair-state transition change.
- Existing STARTUP cursor-gap repair, manifest reconciliation, remote apply,
  status, diagnostics, plugin type-check, lint and build suites remain green.
- The BACKLOG device-sync row is removed only after automated gates pass. The
  closed-reason live-smoke row stays pending because its operator readback is
  explicitly outside this scope.

