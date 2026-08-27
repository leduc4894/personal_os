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

- Remote edit no-echo: pass — guarded Desktop WDIO verdict `obsidian_live_acceptance_passed` (2026-08-27): remote edit applied with exact byte-identical no-echo; final device state Applied/Acknowledged, cursor lag 0
- Cursor gap to manifest repair: pass — guarded Desktop WDIO verdict `obsidian_live_acceptance_passed` (2026-08-27): cursor gap repaired through a completed manifest run; the transient `Repair: Running (device_manifest_state_invalid)` entry cleared with the blocker gone
- Lost-SQLite recovery without duplicate sources: pass — guarded Desktop WDIO verdict `obsidian_live_acceptance_passed` (2026-08-27): journal rebuilt through manifest reconciliation with no duplicate canonical source
- Remote tombstone to local trash: pass — guarded Desktop WDIO verdict `obsidian_live_acceptance_passed` (2026-08-27): remote tombstone moved the proven-unchanged file into Obsidian local trash

Recorded by Duc with the guarded Desktop WDIO journey on 2026-08-27.

## Mobile reference device

- Manifest suspend/resume: pass — suspension mid-run paused the apply; on resume the run continued and the device reached applied/acknowledged convergence through the interruption; two runs expired at their one-hour database deadline and were replaced by a fresh run that completed in under a minute (final cursor lag 0, no active runs left). Finding recorded: a suspension exactly at the finalize transition blocks resume until the deadline (supersede fix staged).
- Remote apply no-echo: pass — remote-origin content applied byte-exact through pull and manifest download actions with zero device-origin uploads across the whole session (no echo); one post-rebuild remote update settled as a durable conflict (`device_manifest_local_diverged`) preserving local bytes by design.
- Lost-SQLite repair: pass — the journal was deleted on device; every re-admitted duplicate create was refused (locator-occupied 409 family); the manifest rebuild re-verified identity with zero duplicate canonical sources (43 active sources, no duplicated active locator) and the cursor reconverged. Finding recorded: the rebuild did not trigger reconcile-first on mobile, so outbound create attempts poisoned local claims (later local edits ride durable conflicts until the Child 8 flow).
- Tombstone to local trash: pass — a remote tombstone (server delete event 2026-08-27 12:23 UTC) moved the proven-unchanged file into Obsidian local trash on the physical device (operator-confirmed trash presence; no hard delete); a second tombstone on a conflict-claimed file preserved local bytes by design.
- Edit-during-reconciliation preservation: pass — the edit survived the reconciliation run byte-exact (never discarded or overwritten); its replay attempt settled as the documented durable conflict owned by the later conflict flow.

Recorded by Duc with Codex on 2026-08-27.
