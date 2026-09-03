# Manifest repair quiet-checkpoint deadlock — handoff (PARTIAL)

**Date:** 2026-09-03
**Plan:** `docs/superpowers/plans/2026-09-03-manifest-repair-quiet-checkpoint-deadlock-plan.md`
**Spec:** `docs/superpowers/specs/2026-09-03-manifest-repair-quiet-checkpoint-deadlock-design.md`
**Branch:** `master`
**Status: PARTIAL — prong 1 (the capture-side root cause) landed and
verified; prong 2 (the reconciler repair deadlock) precisely scoped but
NOT implemented.** The BACKLOG row stays open until prong 2's gates
pass. SDD reports:
`.superpowers/sdd/2026-09-03-manifest-repair-quiet-checkpoint-deadlock/`
(task-1 diagnosis, task-2/3 prong-1 evidence + prong-2 scoping).

## Gate status (with evidence)

| Gate | Result | Evidence |
|---|---|---|
| Task 1 diagnosis | DONE | task-1 report: the full live causal chain — root cause is the rename/delete settle racing an in-flight create (identity not yet landed, not covered by the 2026-08-25 transit heal) flagging the GLOBAL `is_reconcile_required`; the reconciler deadlock is the second layer |
| Prong 1 RED | DONE | 4 new tests in `lifecycle-capture.test.ts` ("settle deferral vs an in-flight create"); 3 failed on the immediate flag; 2 old immediate-flag tests replaced with their intent preserved |
| Prong 1 GREEN | DONE | commit `29f65f5` — `SETTLE_DEFERRAL_ATTEMPTS` + bounded settle re-arm (rename) + bounded delete retry; the flag-write-failure contract test moved onto the exhausted-budget path |
| Full plugin suite | PASS | `pnpm --dir apps/obsidian-plugin exec vitest run` → **1465/1465 (65 files)** exit 0, after the fix |
| Prong 2 RED/GREEN | NOT DONE | ScriptedServer does not model the real server's resume-same-generation / expire-on-new-generation / `max(canonical, ack)` checkpoint semantics — extending it faithfully is the mandatory first step (the 2026-09-02 round's own lesson about pinning unreachable states) |
| Task 4 gates | NOT DONE | deferred with prong 2 |

## What landed (commit `29f65f5`)

`apps/obsidian-plugin/src/journal/lifecycle-capture.ts`: a rename or
delete observation settling against a row whose create upload is still
in flight (no source identity yet, but a live pending event — exactly
the operator's "create note → rename seconds later" on the real stack)
no longer flags the global `is_reconcile_required` and hard-stops the
whole journal. The settle re-arms itself bounded
(`SETTLE_DEFERRAL_ATTEMPTS = 40` × the settle delay): identity lands →
the rename/delete records normally; the create terminalizes failed →
the existing uncommitted-transit heal owns the row; the budget
exhausts → the fail-closed flag keeps its meaning for genuine
pathology. The delete variant retries bounded the same way. The
rejected-flag-write contract (reject + `lifecycle_reconcile_persist_failed`
token) is preserved on the exhausted path.

## Interpretive decisions

1. **The deferral never drops the row** — the replaced tests' "the
   upload may still commit server-side, so the row must never be
   silently dropped" rationale is strengthened, not weakened: deferral
   waits out the upload instead of flagging.
2. **The fail-closed flag survives** as the bounded fallback — the
   2026-08-25 transit heal and the flag both keep their original
   domains; only the undecided (pending) window changes verdict.
3. **Prong 2 implementation deferred rather than rushed**: the live
   choreography (run 1 `expired` → run 2 re-mint `base_ack 1 <
   checkpoint 2` → manual repair mints run 3 then refuses at the
   binding check) could not be fully reconciled with the server code
   (`device_manifest_store.py` resumes same-generation starts, which
   should have returned run 2, not minted run 3) — implementing
   against an unreconstructed choreography risks the wrong pin. The
   server semantics ARE now read and documented in the task-2/3
   report; the next session extends ScriptedServer with them first.
4. **Server rows torn down after copying**: the live runs expired
   server-side at 07:27Z/07:45Z anyway; the unredacted evidence
   (runs/actions/pages with ids, journal fixture g22 + manifest +
   trail) is preserved machine-local under
   `.local/live-round-evidence/lifecycle-readback-20260903/` and the
   CI stack was torn down (`serve-live-ci.sh down`).

## Deferred items (verdicts)

- **Prong 2 (reconciler)** — in scope of this plan, NOT done; the plan
  document's Tasks 2/3/4 remain the authoritative next steps (extend
  ScriptedServer → RED journey → the two client changes: shed the
  locally-bound stale run via the generation bump before minting, and
  terminalize the never-fitting pending action so the run completes).
  Not a BACKLOG row — this plan owns it and stays open.
- **Plugin redeploy to the operator's vaults** — after prong 2 (one
  rebuild); vault A's stuck journal stays preserved (fixture copied
  out) until prong 2 decides reset-vs-repair.
- **The closed-reason L1 re-fire** — separate plan; fires after this
  row retires (its own handoff carries the proven recipe).

## Next actions

1. Extend `ScriptedServer` with the real start/resume/expire +
   `max(canonical, acknowledged)` checkpoint semantics; reproduce the
   live stuck choreography as a RED journey (the open micro-questions
   are listed in the task-2/3 report).
2. Implement the two client changes (a) shed-before-mint at the
   `runOne` binding-mismatch branch, (b) terminal-settle of the
   never-fitting pending action at the re-minted attempt's gap edge.
3. Run the plan's Task 4 gates (plugin suite, `poe device-sync-test`
   on a fresh CI project, `poe verify`), retire the BACKLOG row, and
   update this handoff's status in its commit.
4. Rebuild + redeploy the plugin; re-fire the L1 round.
