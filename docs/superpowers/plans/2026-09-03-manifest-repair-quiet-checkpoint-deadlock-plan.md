# Manifest repair quiet-checkpoint deadlock — plan

**Date:** 2026-09-03
**Domain:** device-sync (manifest reconciliation + repair path)
**Defect source:** live observation during the 2026-09-03 lifecycle
readback round —
[`docs/handoff/2026-09-03-lifecycle-rejection-ring-live-readback.md`](../../handoff/2026-09-03-lifecycle-rejection-ring-live-readback.md)
(BACKLOG row `2026-09-03 | device-sync`).

## The defect (observed live, not yet mechanism-traced)

Ordinary vault actions on a fresh, connected device — create an empty
default note, move it into a folder, rename it — hard-stop the journal
(`Reconcile required`), and the repair machinery cannot restart it:

1. First `Repair sync`: blocked at the `actions` stage with
   `device_cursor_gap` after one `conflict` action
   (`device_manifest_identity_ambiguous`, the unprovable transit entry).
2. The 2026-09-02 automatic recovery fired (verified server-side: run 1
   closed, run 2 re-minted 24s later, no operator action) — but the
   re-minted run still cannot fit: `base_acknowledged_sequence=1 <
   checkpoint_sequence=2` while the local apply lattice already sits at
   `Applied: 2`. Its pending `download` action cannot become a fitting
   synthetic event; the run rests `applying`, polled for 23+ minutes.
3. A manual second `Repair sync` is refused at the start-stage binding
   check (`device_manifest_state_invalid`) — the journal still binds the
   open run 2 — and mints an abandoned `collecting` run 3.

Escape hatch observed in design comments ("the one-hour expiry later
starts a fresh, further-ahead checkpoint that fits") requires the
canonical sequence to ADVANCE. On a quiet workspace with the journal
hard-stopped (no outbound lane, no other writer), canonical never
advances — the deadlock is stable. **Working theory (to be proven by
Task 1, not assumed): the checkpoint must not be chosen below what the
device's local lattice already consumed on a quiet workspace, and/or
the manual repair must be able to shed a locally-bound open run that
the recovery bound already exhausted.**

## Preserved inputs

- The stuck journal is INTACT in vault A (operator keeps Obsidian
  closed until Task 1 copies it — never move journal files under a
  live app).
- Unredacted server rows (runs/actions/pages) + run ids:
  `.local/live-round-evidence/lifecycle-readback-20260903/` (machine
  local, never committed).
- Two `Copy sync diagnostics` exports (trail timestamps above) and API
  log `manifest_start` timestamps, in the source handoff.
- Live server rows expire idle at 07:27Z (run 2) / 07:45Z (run 3) on
  2026-09-03; the CI stack stays up until this plan no longer needs
  them (teardown: `CI=true bash .local/serve-live-ci.sh down`).

## Task 1 — Diagnosis: pin the exact mechanism

**Files:** read-only analysis; deliverable is the task report note
under `.superpowers/sdd/2026-09-03-manifest-repair-quiet-checkpoint-deadlock/`.

- [ ] Copy the stuck journal fixture out of vault A (operator closes
  Obsidian first): `journal.sqlite.g*`, `journal.manifest.json`,
  `sync-diagnostics-trail.json` → the evidence directory above.
- [ ] From the fixture, read the durable state the reconciler sees:
  `activeManifestRunId`, `manifestCheckpointSequence`,
  `appliedSequence`/`acknowledgedSequence`, barrier reason/generation,
  the retained action progress rows, and the `local_files`/event rows
  behind the ambiguous transit entry (which sequence the two applied
  downloads consumed, and their origin).
- [ ] Code-trace the exact branches: which `blocked("start",
  device_manifest_state_invalid)` line fires on the manual repair
  (`manifest-reconciler.ts:682` server-echo generation mismatch vs
  `:696` local binding mismatch); where `base_acknowledged_sequence`
  and `checkpoint_sequence` are chosen server-side for a re-minted
  run; why the pending `download` cannot settle.
- [ ] Verdict in the report: the deadlock's minimal causal chain, the
  layer(s) that must change (client reconciler, server checkpoint
  selection, or both), and the harness hooks needed to reproduce it
  faithfully.

## Task 2 — RED: the failing harness journey

**Files:** `apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts`
(new journey beside the 2026-09-02 regression).

- [ ] Reproduce the LIVE shape end-to-end against the ScriptedServer:
  an unprovable transit entry (conflict action), a local apply lattice
  that runs ahead of a quiet checkpoint (the two downloads), the
  re-mint whose checkpoint still cannot fit (`base_ack` at 1,
  checkpoint at 2), then an explicit repair refused at the start
  binding check — pin the stuck state (barrier
  `device_manifest_state_invalid` or the surviving `device_cursor_gap`,
  an `applying` run binding, pending action retained).
- [ ] The 2026-09-02 lesson applies verbatim: the harness must reach
  the REAL stuck state — amend fault injection until the intermediate
  barrier genuinely exists — no relax-and-retire, no pinning an
  unreachable state. The live fixture from Task 1 is the oracle for
  what "stuck" looks like.

## Task 3 — GREEN: the minimal fix

Mechanism is decided by Task 1's verdict; the candidate directions
(hypotheses, not decisions):

- the re-mint/checkpoint floor must account for what the device's
  lattice already consumed on a quiet canonical timeline (server-side
  checkpoint selection, client-supplied evidence, or both);
- the explicit repair path must shed a locally-bound open run whose
  recovery bound is exhausted instead of refusing at the start check
  (and must not mint runs it will abandon server-side);
- the pending action that can never fit must settle as a durable
  closed verdict rather than resting `applying` forever.

- [ ] Smallest change that turns Task 2's journey green; convergence
  must land through the canonical fence as before (no local cursor
  shortcuts), stay idempotent under repeated repairs, and leave no
  abandoned server runs.
- [ ] No regression to the 2026-09-02 journey ("repairs a cursor gap
  created inside delete-and-recreate reconciliation") or the existing
  recovery invariants.

## Task 4 — Gates, retirement, handoff

- [ ] `pnpm --dir apps/obsidian-plugin exec vitest run` + `tsc
  --noEmit` + `lint` + `build` all exit 0.
- [ ] `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-<bounded-token>
  uv run poe device-sync-test` exit 0 on a fresh disposable project.
- [ ] `uv run poe verify` exit 0 (or the documented pre-existing
  master failures re-verified at the branch base, per the 2026-09-02
  round's pattern).
- [ ] Retire the BACKLOG row `2026-09-03 | device-sync` (this plan
  owns it); the closed-reason L1 row stays (it re-fires its own
  journey).
- [ ] ONE handoff at `docs/handoff/2026-09-03-manifest-repair-quiet-checkpoint-deadlock.md`;
  teardown the CI stack if this round was its last user.

## Optional live probe (not a gate)

If the operator is available past 07:27Z 2026-09-03 (run-2 idle
expiry): have vault B create one note (canonical advances), then one
`Repair sync` on vault A. Convergence would confirm the
quiet-checkpoint theory live; non-convergence is additional Task-1
input. Never represented as the acceptance gate — the harness journey
is.

## Risks

- The harness may reproduce only part of the live chain — the Task 1
  fixture comparison is the guard against pinning a wrong state.
- A server-side checkpoint change touches the manifest run contract —
  if Task 1 lands there, extend this plan with the contract-update
  obligations (OpenAPI/client/contract tests) before implementing.
- Live evidence expires (07:27Z/07:45Z rows); everything durable is
  already copied out.
