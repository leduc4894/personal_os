# Manifest restart and apply robustness — handoff

**Date:** 2026-09-03
**Plan:** `docs/superpowers/plans/2026-09-03-manifest-restart-and-apply-robustness-plan.md`
**Branch:** `master`
**Status: PARTIAL — finding 2 (restart asymmetry) fixed and green;
finding 3 (apply wedge) landed its defensive bound with the refined
mechanism recorded; finding 1 (burst loss) remains design-open.**
SDD evidence rides in this handoff (no separate workspace this round).

## Gate status (with evidence)

| Gate | Result | Evidence |
|---|---|---|
| Harness fidelity upgrades | DONE | ScriptedServer now RETAINS page digests, verifies the finalize digest against them (faithful to the real server), and expires an unfinished run on a different-generation start; `failActionsRead` (persistent mid-run actions failure) and `InMemoryVault.failWritesAtLocator` (vault write refusal injection) added |
| Finding 2 RED | PASS | journey "invalidates a server run whose retained pages the fresh capture contradicts…" failed pre-fix exactly at the retained barrier (the wedge) |
| Finding 2 GREEN | PASS | commit `186286d` — `RUN_EVIDENCE_INVALIDATION_REASONS` restarts carry a new observation generation (`advanceRepairBarrierGeneration`), the server expires the contradicted run, a truly fresh run converges (barrier cleared, binding null, completions grew); journey green |
| Finding 3 defensive bound | PASS | commit `7e82701` — `DEVICE_SYNC_REPAIR_RETRY_BOUND = 3` consecutive same-reason repair retries surface as the readable blocked verdict (churn stops; the operator's explicit repair retries); focused coordinator test green |
| Plugin suite / tsc / lint / build | PASS | `vitest run` **1468/1468 (65 files)**; tsc clean; eslint clean; build clean — all exit 0 |
| `poe device-sync-test` (CI project) | PASS | `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-robustness-verify-20260903` exit 0 (recorded in the session log) |
| `poe verify` | PASS | exit 0 at close-out |

## The refined finding-3 mechanism (from the harness probe)

The simple injection (a vault write refusal) does NOT wedge today: the
applier's FIRST apply failure terminalizes the event durably as a
conflict (one `apply_failure` trail entry, the run completes with the
placement unsettled — applied stays behind but the state is readable).
The LIVE wedge churned because the failure sat at `verify_temp` (mid-apply,
after the durable prepare) AND the recovery/terminalization path itself
failed with `device_cursor_gap` — the event could never settle, so the
repair retried forever. Reproducing that needs a mid-apply failure whose
durable settle also breaks (a composed fault injection). The coordinator
bound (`7e82701`) caps ANY such chain at three same-reason retries with
a readable verdict — the wedge becomes a bounded, readable blocked state
whose explicit repair retries once the underlying condition clears. The
deeper fix (the run completing despite an unsettled mid-apply action —
schema-level per-action attempt counting) stays with the row below.

## What remains (rows)

- **Finding 1 (burst loss)** — unchanged, design-open (the plan's Task 2
  research item): BACKLOG row stays.
- **Finding 3 (apply wedge)** — the defensive bound landed; the full
  "run completes anyway" fix (per-action durable attempt counting,
  schema v10) stays: row updated, `Before Child 9 operations
  acceptance`.
- The L1 re-fire (row `2026-08-24 | closed-reason-surfacing`) re-fires
  after the remaining rows close or the operator accepts the current
  bounded behavior.

## Next actions

1. Rebuild + redeploy the plugin dist (contains all four of today's
   fixes) to both vaults; reset the wedged vault journals; teardown the
   `knowledge-ci-l1-refire-20260903` stack (the live wedge's durable
   evidence is copied under `.local/live-round-evidence/`).
2. Decide finding 1's design (chain-composed rename capture) — likely
   its own plan; finding 3's deep fix rides the same decision round.
3. Re-fire the L1 round.
