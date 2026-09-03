# Lifecycle rejection ring live readback — plan

**Date:** 2026-09-03
**Domain:** closed-reason-surfacing (L1 lifecycle rejection ring)
**Governing acceptance:** spec criterion 4 of
`docs/superpowers/specs/2026-08-24-closed-reason-surfacing-remediation-design.md`
(one live smoke round reading each failure class back from the real
surfaces). This plan closes the round's ONE remaining readback: the
lifecycle rejection ring (BACKLOG row `2026-08-24 | closed-reason-surfacing`).

Secondary objective (same live round, same trigger): the 2026-09-02
device-cursor-gap handoff's next action #1 — confirm a
delete-and-recreate `Repair sync` converges on the real stack (live
parity of the cursor-gap repair, whose blockers are all landed).

## Goal

Produce, on the operator's real test vault(s) against one disposable
`knowledge-ci-*` project, the missing L1 evidence pair:

1. **Server half** — one typed lifecycle 4xx recorded in the lifecycle
   rejection ring: `GET /api/admin/source-lifecycle/rejections` (Web
   Admin `/admin/lifecycle` page renders the same) carries a
   `recent_rejections` row `{error_code: source_locator_conflict,
   operation: restore}` plus a `commit_counters` row
   `{operation: restore, outcome: rejected}`.
2. **Plugin half** — the same exchange parked on the device: the
   restore event terminalizes `blocked_conflict`, and the Child 8
   conflict lane captures the losing side server-side as a byteless
   `locator_collision` listed in the device's Conflict Inbox
   (Keep remote only). `Copy sync diagnostics` is recorded verbatim.

Then retire the BACKLOG row, record sanitized evidence, and hand off.
No production code change is expected; deliverables are docs-only.

## Trigger design (grounding)

The cheap documented trigger is a **tombstone-restore locator
conflict**. With the reservation-first protocol a converged
single-device journal can no longer produce it — the local reservation
check (`restore_target_occupied`,
`apps/obsidian-plugin/src/journal/lifecycle-repository.ts:577-616`)
refuses any target held by another source-identified `local_files`
row. The server-side check (`_lock_target_locator_row`,
`packages/postgresql-source-store/src/postgresql_source_store/lifecycle_store.py:1220-1261`)
refuses when ANY other active canonical source holds the target
locator. The deterministic blind spot between the two: **a target
path occupied server-side by a second writer whose note the journey
vault has not bound to a source-identified local row** — exactly the
cross-device race the Child 8 lane was built to capture, created the
same way the Conflict Inbox journey runbook sanctions ("edit the same
note remotely (web/second session)").

Journey (two real Obsidian vaults on one machine, both devices of the
same CI project; vault A runs the journey, vault B is the second
writer):

1. Vault A: create note P1 with unique content A → committed.
2. Vault A: delete P1 → delete committed, open tombstone T1, local row
   `tombstoned`.
3. Vault A: `Restore selected tombstone` → pick T1 → target path P3 (a
   fresh path) → accept the prompt (durable reservation; row
   `restore_pending`, rebound to P3). Passively dismiss the confirm
   modal — the reservation stays resumable.
4. Vault B: create note at the SAME path P3 (any content) → committed
   server-side as an active source at P3. (Vault A's journal defers P3
   while the reservation holds; whether the manifest later lands those
   bytes on disk is irrelevant to the server check.)
5. Vault A: stage the restore bytes at P3 — create/edit the note at P3
   to exactly content A (byte-exact with P1's last-committed bytes;
   staging at a reserved target is a staging action, never a fresh
   source).
6. Vault A: re-run `Restore selected tombstone` → pick T1 → Confirm →
   the restore event ships and the server answers the typed 409
   `source_locator_conflict` (operation `restore`); the ring records
   it and the byteless `locator_collision` conflict is captured
   (`SourceLifecycleService._capture_race_conflict`).

Expected plugin state after step 6: the restore event is terminal
`blocked_conflict` (the tombstone stays restorable — the picker keeps
listing it); the Conflict Inbox lists one byteless `locator_collision`
(Keep remote only); no journal hard stop.

**Interpretive decision (recorded for the handoff):** the original
acceptance wording says "match it against the plugin trail's parked
outcome". Today's lifecycle lane parks the 409 on the event row
(`blocked_conflict` safe error) and writes NO trail entry — the trail
naming (`wire_failure · blocked_conflict · source_locator_conflict`)
is the content-lane upload mapping that predates the reservation-first
protocol. The honest plugin-side readback is therefore the parked
event outcome + the Conflict Inbox capture; `Copy sync diagnostics`
is recorded and its (expected) silence on the lifecycle exchange is
part of the evidence, not a gap in it.

## Operator steps (sanitized evidence only)

Journey A — delete-and-recreate repair convergence (run first; uses
its own note):

- A1. Create note with unique content → committed (Ready).
- A2. Delete it; recreate the SAME path with DIFFERENT content → the
  journal flags `Reconcile required` (queue stopped) — expected.
- A3. Run `Repair sync` → expected: the repair completes, the barrier
  clears, status returns Ready (no re-block). The recreated note may
  remain an unsynced local file (a durable conflict blocker with no
  mutation) — record what is observed.

Journey B — the L1 trigger above, with per-step expected states.
After the readbacks, optional tidy-up (all sanctioned actions):
resolve the Inbox conflict Keep remote; delete the staged note at P3
(staging action while the reservation holds); re-run the restore
command and Cancel to release the reservation.

Evidence rules (unchanged from the round's plan): closed tokens,
counts, ISO-8601 UTC timestamps only — no paths, hostnames,
credentials, content, note names, ids beyond the sanctioned opaque
ones.

## Codex-side duties

1. `CI=true bash .local/serve-live-ci.sh up
   knowledge-ci-lifecycle-readback-20260903` (the one-command path;
   it stands the whole service set and the tunnel).
2. `CI=true uv run python tools/obsidian_live_acceptance_bootstrap.py
   --project-name knowledge-ci-lifecycle-readback-20260903
   --bootstrap-only` (journey-ready: TOTP active + policy published).
3. Build the current plugin (`pnpm --dir apps/obsidian-plugin run
   build`) and prepare vault B (fresh vault folder + the built plugin
   folder under `.obsidian/plugins/knowledge-workspace/`); instruct
   the operator to update vault A's plugin copy, start both vaults on
   the public origin, fresh-start vault A's journal for the new
   project (move `journal.sqlite.g*`, `journal.manifest.json` and the
   `sync-diagnostics-trail.json` sidecar out — documented procedure),
   and log both vaults in through the real browser flow.
4. Server-side verification at each checkpoint through an
   authenticated Web Admin session (reuse the 2026-08-31 round's
   parameterized scratch helper:
   `…/task-5-admin-helper.py read api/admin/source-lifecycle/rejections`).
5. Tear down with `bash .local/serve-live-ci.sh down` only after the
   operator evidence and checkpoint verification complete.

## Files

- Modify: `docs/operations/sync-error-tracing.md` (the Class 3
  evidence section of the live-smoke record).
- Modify: `docs/handoff/BACKLOG.md` (remove exactly the
  `2026-08-24 | closed-reason-surfacing` row — only after every
  required readback is observed).
- Create: `docs/handoff/2026-09-03-lifecycle-rejection-ring-live-readback.md`
  (the round's single handoff).
- No code, schema, or contract changes expected.

## Risks and honesty rules

- If any step cannot be produced, the row STAYS with the blocking
  gate and evidence recorded; no partial completion claim (the
  2026-08-31 round's rule).
- A staging hash mismatch at Confirm is a local refusal
  (`journal_mutation_failed`), not the L1 trigger — the operator
  restages byte-exact content A and retries.
- If the journey reveals a defect (as the 2026-09-01 round did), the
  defect is routed to its owning domain as a BACKLOG row with a
  concrete `Implement by`; the L1 row stays open if its readback did
  not land.
- Vault B must be a test vault — never the daily personal vault.
