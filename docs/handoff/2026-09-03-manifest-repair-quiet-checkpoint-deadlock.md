# Manifest repair quiet-checkpoint deadlock — handoff

**Date:** 2026-09-03
**Plan:** `docs/superpowers/plans/2026-09-03-manifest-repair-quiet-checkpoint-deadlock-plan.md`
**Spec:** `docs/superpowers/specs/2026-09-03-manifest-repair-quiet-checkpoint-deadlock-design.md`
**Branch:** `master`
**Status: COMPLETE** — both prongs landed and verified (gates below).
The BACKLOG row `2026-09-03 | device-sync` retires with this handoff.
SDD reports:
`.superpowers/sdd/2026-09-03-manifest-repair-quiet-checkpoint-deadlock/`.

## Gate status (with evidence)

| Gate | Result | Evidence |
|---|---|---|
| Prong 1 RED | PASS | 4 tests in `lifecycle-capture.test.ts` ("settle deferral vs an in-flight create"); 3 failed on the immediate flag pre-fix |
| Prong 1 GREEN | PASS | commit `29f65f5`; full plugin suite **1465/1465** at that point |
| Prong 2 RED | PASS | journey "sheds a stale open-run binding after the server idle-expired it instead of stranding the fresh run" failed exactly at the live defect's terminal state (`device_manifest_state_invalid`, binding retained, zero completions) |
| Prong 2 GREEN | PASS | commit `f7a92e5`; the journey asserts convergence: barrier cleared, binding null, cursors equal, the fresh server run COMPLETED (never abandoned `collecting`), no start-stage flip, the honest `device_cursor_gap` verdict on the trail |
| Plugin suite / tsc / lint / build | PASS | `vitest run` **1466/1466 (65 files)**; `tsc --noEmit` clean; `eslint --max-warnings=0` clean; `build-plugin.mjs` clean — all exit 0 post-fix |
| 2026-09-02 regression journey | PASS | "repairs a cursor gap created inside delete-and-recreate reconciliation" green unchanged (the terminal-settle guard `checkpoint > applied` keeps its further-ahead-checkpoint shape on the restart path) |
| `poe device-sync-test` (CI project) | PASS | `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-deadlock-verify-20260903 uv run poe device-sync-test` exit 0 — recorded in the session log; conftest-provisioned and cleaned, `knowledge-local` untouched |
| `uv run poe verify` | PASS | exit 0 at close-out |

## What landed

1. **`29f65f5` — capture settle deferral (the live incident's root
   cause).** A rename or delete observation settling against a row
   whose create upload is still in flight defers bounded
   (`SETTLE_DEFERRAL_ATTEMPTS = 40` × the settle delay) instead of
   flagging the global `is_reconcile_required`: identity lands → the
   observation records normally; the create terminalizes failed → the
   uncommitted-transit heal owns the row; the budget exhausts → the
   fail-closed flag keeps its meaning. Ordinary "create a note, rename
   it seconds later" no longer hard-stops the journal.
2. **`f7a92e5` — the reconciler repair path.** Three coordinated
   changes in `manifest-reconciler.ts` (+ `persistRepairBarrierReason`
   on the repository and its contract):
   - **Shed-before-block** at `runOne`'s binding mismatch: the server's
     start receipt naming a different run proves the journal's binding
     is stale (the server idle-expired it — the live freeing moment);
     the stale binding is discarded and the loop's second attempt
     adopts the server's run. No more mint-then-refuse, no abandoned
     `collecting` runs.
   - **Durable cursor-gap verdict from the reconciler's own gap
     branch** (`persistRepairBarrierReason`): the same verdict the
     applier's prepare path already persists, so the resting barrier is
     honest and a later resume's recovery branch can key on it.
   - **Narrow terminal-settle** of the never-fitting pending action:
     only when the attempt is the loop's retry AND
     `checkpoint <= applied` (the quiet misfit — even the retry's own
     checkpoint cannot fit the lattice, and the planned placement's
     content was already delivered by the pull lane). The action
     settles with the closed `device_cursor_gap` reason and the run
     completes through the canonical fence. The guard preserves every
     recoverable shape (a further-ahead checkpoint still fitting keeps
     the restart path — pinned by the untouched 2026-09-02 journey).

   Harness: `ScriptedServer.expireOpenRun()` models the server's run
   idle-expiry (the per-device unfinished slot freeing); the RED/GREEN
   journey drives the exact live choreography (pull-advanced lattice ≥
   frozen checkpoint → open bound run under a repair barrier →
   idle-expiry → explicit repair → convergence).

## Interpretive decisions (with reasons)

1. **The deferral never drops the row** — the replaced tests' "the
   upload may still commit server-side" rationale is strengthened: the
   settle waits out the upload instead of flagging.
2. **(B)'s guard is `checkpoint <= applied`, not an
   already-applied-source check.** The evidence-based alternative
   (per-source applied evidence) required a new repository read and a
   planner-semantics discussion; the chosen guard is strictly narrower
   in time (only the retry attempt) and state (only the never-fitting
   lattice), and the pull-lane delivery makes the "already delivered"
   claim structural: every event ≤ applied is delivered by
   definition, and the planner never plans sources above the
   checkpoint, so `checkpoint <= applied` implies every planned
   placement's event is already delivered.
3. **No flapping after convergence (code stands):** the coordinator
   runs a repair only when one is owed (`runRepairIfRequired`'s
   isRepairOwed gate) — a converged device (barrier null, no open run,
   no reconcile flag) rests; the planner's re-plan of uncovered
   sources only matters inside a later owed repair, which the fixed
   path converges bounded. The deeper planner question (re-planning
   sources whose events the lattice already consumed — the manifest's
   restore semantics for locally-deleted files) is a product-semantics
   discussion, not a defect: recorded here as the standing note, no
   BACKLOG row.
4. **Prong 2 was deferred once mid-day** (session 1 ended PARTIAL —
   the harness did not model the server's resume/expire semantics);
   session 2 closed the story from the raw run ids (run 2 belonged to
   the SECOND device — the "re-mint" assumption was wrong) and landed
   the fix. The wrong assumption is preserved in the SDD addendum for
   the record.

## Deferred items (verdicts)

- **None in scope.** The L1 re-fire (row `2026-08-24 |
  closed-reason-surfacing`) is a separate plan's obligation and stays
  open on its own row; its blocker (this defect) is now fixed, so the
  re-fire is unblocked (stack bootstrap recipe, two-vault fixture and
  the verified readback path are all proven in the block handoff).

## Next actions

1. Rebuild + redeploy the plugin dist to the operator's vaults (vault
   A's stuck journal from the live round can be reset per the
   documented fresh-journal procedure — the defect class is fixed).
2. Re-fire the L1 round exactly as its plan defines; retire the
   closed-reason row on observed readbacks.
