# Device-Sync Reference Device Verification Records

Recorded evidence of the mandatory Child 6 live gates (design 18.1): the
Desktop WDIO journey and the physical Mobile matrix of the device cursor and
manifest reconciliation feature. The recorded-evidence gate
(`uv run poe device-sync-device-verification`) reads this file and fails —
by design, never skips — while any record is missing, partial or a
placeholder. Mock, unit inference or Desktop evidence can never substitute
for the physical Mobile rows.

Recording rules (sanitized evidence only):

- one `- <Label>: <observed outcome>` row per scenario below, using the
  observed closed status/state tokens (for example `Repair: Ready`,
  `cursor_failure:pull:device_cursor_gap` then a completed manifest run,
  local trash path observed), never file names, paths, content, digests,
  tokens, credentials or request ids;
- one `Recorded by <operator> on YYYY-MM-DD.` line per section naming the
  human who observed the device;
- Desktop rows come from the guarded Desktop WDIO run
  (`tools/obsidian_live_acceptance_bootstrap.py --wdio-spec
  test/specs/device-sync-reconciliation.e2e.ts`, closed verdict
  `obsidian_live_acceptance_passed` plus the sanitized
  `SANITIZED_DEVICE_RECONCILIATION_EVIDENCE` block) observed by the operator;
- Mobile rows come from the physical reference device against the same
  disposable stack (see the living runbook
  [`device-cursor-manifest-reconciliation.md`](device-cursor-manifest-reconciliation.md)).

## Desktop reference device

- Remote edit no-echo: pending
- Cursor gap to manifest repair: pending
- Lost-SQLite recovery without duplicate sources: pending
- Remote tombstone to local trash: pending

Recorded by (operator, date) — pending.

## Mobile reference device

- Manifest suspend/resume: pending
- Remote apply no-echo: pending
- Lost-SQLite repair: pending
- Tombstone to local trash: pending
- Edit-during-reconciliation preservation: pending

Recorded by (operator, date) — pending.
