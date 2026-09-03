# Manifest repair quiet-checkpoint deadlock — design spec

**Date:** 2026-09-03
**Domain:** device-sync
**Plan:** `docs/superpowers/plans/2026-09-03-manifest-repair-quiet-checkpoint-deadlock-plan.md`

## Problem

A device whose local apply lattice has consumed remote events up to
sequence `A` can enter manifest reconciliation whose run checkpoint `C`
satisfies `C < A` on a quiet workspace (canonical sequence not
advancing). Every synthetic apply the run plans needs
`appliedSequence + 1 <= C`; with `A >= C` the run can never fit, the
recovery bound exhausts, and the journal rests hard-stopped
(`Reconcile required`). The explicit `Repair sync` command then cannot
even start a corrective run: the start-stage binding check refuses
while the journal still binds the exhausted open run
(`device_manifest_state_invalid`), minting an abandoned server run.

Live evidence: 2026-09-03, vault A, plugin 0.2.0 — see the source
handoff. Trigger: an ordinary create→move→rename note transit (empty
default note moved and renamed before any commit), which additionally
produced an unprovable manifest entry (`conflict` action,
`device_manifest_identity_ambiguous`).

## Required behavior

1. **Repair converges on a quiet workspace.** After the deadlock
   preconditions exist, an explicit `Repair sync` (possibly after a
   bounded number of attempts the command itself drives — at most the
   existing two-run bound) must converge the device: barrier cleared,
   no open run binding, cursors equal, status leaves
   `Reconcile required`. No requirement that any other device write or
   that canonical advance.
2. **Convergence stays canonical.** Any outcome still lands through
   the existing fences (`completeManifest` →
   `journal.completeDeviceSyncRepair`); no local cursor mutation
   outside them.
3. **No abandoned runs.** The repair path must not leave server-side
   runs it will never finish (the observed `collecting` run 3); a run
   the client abandons must be closed or never minted.
4. **Idempotence.** Repeated `Repair sync` on the converged state
   stays converged (the 2026-09-02 journey's rule): neither replays an
   event nor creates a new conflict/repair row, completions grow only
   by the documented clean-run shape.
5. **The ambiguous entry stays honest.** The unprovable transit entry
   keeps its durable closed conflict verdict (or the documented
   terminal-safe outcome); the fix must not "prove" unprovable
   identity to force convergence.

## Acceptance criteria

1. A RED harness journey pins the live stuck state (Task 2): the
   intermediate barrier genuinely exists, the exhausted run binding is
   real, the manual repair's start refusal reproduces; GREEN after the
   fix with the convergence + idempotence + no-abandoned-run
   assertions above.
2. The 2026-09-02 regression journey and the existing recovery
   invariants stay green.
3. Full plugin gates + `poe device-sync-test` (CI disposable project)
   + `poe verify` green, per the plan's Task 4.
4. The live re-fire of the closed-reason L1 round (separate plan) is
   NOT claimed by this spec; only the defect row
   `2026-09-03 | device-sync` retires here.

## Error cases

- The shed/closed run's server state is already expired (idle
  deadline passed mid-repair): the repair still converges; expiry is
  not an error path.
- The pending action's target diverges while the repair re-plans: the
  existing stale-action verdicts (`device_manifest_action_stale`,
  `device_manifest_local_diverged`) settle it; no clobbering.
- Policy advance mid-repair: the existing restart-run path applies;
  convergence still bounded.
- A foreign/stale process race is out of scope (the launcher
  contracts own it).

## Out of scope

- The Conflict Inbox / capture lane (the `identity_ambiguous` verdict
  itself is input, not work).
- Server checkpoint contract changes UNLESS Task 1's verdict requires
  them — if it does, the plan extends with the contract-update
  obligations before any implementation.
- The closed-reason L1 journey (re-fires separately after this fix).
