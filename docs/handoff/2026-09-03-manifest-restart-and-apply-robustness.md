# Manifest restart and apply robustness — handoff

**Date:** 2026-09-03
**Plan:** `docs/superpowers/plans/2026-09-03-manifest-restart-and-apply-robustness-plan.md`
**Branch:** `master`
**Status: COMPLETE for findings 2 and 3 — finding 2 (restart asymmetry) fixed
(`186286d`), finding 3 (apply wedge) fixed at BOTH layers (defensive bound
`7e82701` + the deep settle-and-continue fix landed 2026-09-03 evening);
finding 1 (burst loss) remains design-open with its own row.**
SDD evidence rides in this handoff (no separate workspace this round).

## Gate status (with evidence)

| Gate | Result | Evidence |
|---|---|---|
| Harness fidelity upgrades | DONE | ScriptedServer RETAINS page digests, verifies the finalize digest against them, expires an unfinished run on a different-generation start; `failActionsRead`, `InMemoryVault.failWritesAtLocator` and `failWritesForBasenames` (a locked basename refuses its own hidden staging siblings — the live `verify_temp` shape) |
| Finding 2 RED | PASS | journey "invalidates a server run whose retained pages the fresh capture contradicts…" failed pre-fix exactly at the retained barrier (the wedge) |
| Finding 2 GREEN | PASS | commit `186286d` — `RUN_EVIDENCE_INVALIDATION_REASONS` restarts carry a new observation generation (`advanceRepairBarrierGeneration`), the server expires the contradicted run, a truly fresh run converges |
| Finding 3 defensive bound | PASS | commit `7e82701` — `DEVICE_SYNC_REPAIR_RETRY_BOUND = 3` consecutive same-reason repair retries surface as the readable blocked verdict |
| Finding 3 deep fix RED | PASS | journey "completes a manifest run past a repeatedly refused vault write instead of holding every other placement hostage" failed pre-fix exactly at `barrierGeneration` non-null (the wedge) |
| Finding 3 deep fix GREEN | PASS | same journey green: run completes (barrier null, binding null, applied==ack), the hostage placement delivers, the locked file stays absent with `device_apply_vault_failed` readable on the trail; after the lock clears + one deferred sequence + explicit repair the locked file delivers |
| Honest-verdict polish | PASS | `applySyntheticEvent` records the applier-returned conflict's closed reason on the action row (was `terminal(null)`) |
| Plugin suite / tsc / lint / build | PASS | `vitest run` **1474/1474 (65 files)**; tsc clean; eslint clean; build clean — all exit 0 |
| `poe device-sync-test` (CI project) | PASS | `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-applywedge-20260903` — **1863 passed, 2 skipped, exit 0** on a fresh stack (see the flaky note below); project torn down afterwards |
| `poe verify` | PASS | exit 0 at close-out |

## The finding-3 deep fix (what the missing piece actually was)

Two layers wedged, not one:

1. **The cycle-start recovery died before the repair could resume.** The
   leftover `prepared` row's clean verdict abandons the intent and then
   mints `device_apply_recovery_abandoned` — but `startRepairBarrier` is
   REFUSED under an active manifest run binding, so the whole recovery
   phase threw `journal_mutation_failed` every cycle and the repair lane
   never ran again. Fix: the clean branch skips the barrier mint when
   `activeManifestRunId !== null` — a bound run already IS the manifest
   reconciliation the barrier exists to force (its action re-attempt or
   its canonical-only download re-converges the abandoned event).
2. **The in-run settle had no way to free the lattice sequence.** The
   failed apply's row (`prepared`/`temp_verified`) holds the sequence; the
   next action's synthetic prepare at the same sequence with a different
   deterministic eventId would collide as `device_apply_recovery_ambiguous`.
   Fix: new `applier.settleVaultFailedApply(event, reason)` — the caller
   (reconciler `applySyntheticEvent`) proves a PRIOR durable attempt of
   the same action failed with the closed `device_apply_vault_failed`
   (the durable `received` progress row at run start IS the attempt
   evidence — no schema v10 needed), then the settle runs the writer's
   crash-safe recovery on the exact row (`repository.readRemoteApply`,
   new by-sequence read — `readUnfinishedApply` can name an earlier
   still-unacknowledged row instead):
   - `clean` → `abandonRemoteApply` (row + echo marker deleted) — the
     sequence stays REUSABLE, the cursor does not burn it, no barrier;
   - `mutated` → transition + terminalize `applied` (the refusal's write
     actually completed — never a lossy verdict);
   - `restored` / a recovery that itself meets the refusal →
     `terminalizeEvent(conflict, reason)` — the repository's own contract
     ("any other terminal outcome closes a dangling prepared row with its
     closed reason") frees the sequence the only legal way for an
     unprovable `temp_verified` row; the digest-verified staged sibling
     survives untouched;
   - `blocked` → surface + non-retryable throw (never free ambiguous
     bytes; the `7e82701` bound keeps the verdict readable).

The action then terminalizes with its closed reason and the run CONTINUES
(`terminal("device_apply_vault_failed")` — one `reconcile_failure`
observation on the trail because the completion discards progress rows).

### Interpreted decisions (with rationale)

- **Settle scope is exactly `device_apply_vault_failed`** — the closed
  vault-failure family. Retryable transport failures keep today's retry
  semantics; `device_apply_trash_failed` and conflict-family reasons
  already settle durably through their own paths.
- **Attempt evidence is `outcome='received'` from a PRIOR pass of the same
  run** (snapshot at `runOne` start). A crash between the receipt and the
  apply makes the next pass's first real refusal settle after one attempt
  — accepted: the class is a persistent vault condition, the settle leaves
  durable readable evidence, and the placement re-converges later.
- **Post-unlock re-delivery is NOT unconditional.** After a completion,
  the fence sets applied=ack=checkpoint, so a fresh run's synthetic apply
  needs a checkpoint strictly above the cursor — on a perfectly quiet
  canonical timeline a parked canonical-only download waits for timeline
  growth (any peer commit, or any deferrable sequence). This is the
  STANDING fence property the "sheds a stale open-run binding" journey
  already pins, not a defect this fix introduces; the new journey's second
  phase models it with `server.deferSequences(1)` and cites it. If
  unconditional quiet-timeline re-delivery is wanted, that is a
  fence-semantics product decision (adjacent to the burst-loss row's
  manifest-restore question), out of this row's scope.

### Flaky CI note (not a regression)

The first `device-sync-test` invocation hit a stack cold-start readiness
race (29 errors, all "local-stack step 'up' failed with code 75" — the
stack became `ready` moments later and every subsequent module reused it),
and the second (reusing the populated project without a reset) hit
planner-index drift and a content-hash unique violation from accumulated
data while a parallel plugin-suite run restarted postgres mid-run. A clean
teardown + fresh-stack rerun with nothing else running: **1863 passed,
exit 0**. Lesson for this machine: run `device-sync-test` alone and on a
fresh (or explicitly reset) project.

## What remains (rows)

- **Finding 1 (burst loss)** — unchanged, design-open (the plan's Task 2
  research item): BACKLOG row stays; next step is its own plan.
- The L1 re-fire (row `2026-08-24 | closed-reason-surfacing`) re-fires
  after the remaining rows close or the operator accepts the current
  bounded behavior.

## Next actions

1. Write the burst-loss plan (chain-composed rename capture) — its own
   plan document; the RED journey must pin the reservation and
   delete-deferral behaviors staying intact.
2. Rebuild + redeploy the plugin dist to both vault test fixtures — the
   current `apps/obsidian-plugin/dist` (built 2026-09-03 evening) already
   contains all FIVE fixes of today, newer than the vault B fixture's
   copy (four fixes).
3. Re-fire the L1 round after the burst-loss row closes or the operator
   accepts the bounded behavior.
