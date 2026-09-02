# Conflict Vault-Apply Hardening Handoff

Plan: `docs/superpowers/plans/2026-09-02-conflict-vault-apply-hardening.md`
Spec: `docs/superpowers/specs/2026-09-02-conflict-vault-apply-hardening-design.md`
Branch: `conflict-vault-apply-hardening` (from `master` `a30daab`; no worktree, per operator instruction)

## Status: COMPLETE

Final plan commit: `3b9c5af` (Task 4 docs, BACKLOG retirement; the docs-only fix wave
containing this handoff follows it). Last code commit: `2246971`. All four plan tasks
done, every per-task review clean (Task 2 after one fix round); final whole-branch review
verdict: **ready to merge** — its two Important findings (missing handoff + BACKLOG rows;
writer-primitive implicit protocol design debt) are closed by this fix wave and the rows
below.

| Gate | Result | Evidence |
|---|---|---|
| `vitest run` (plugin) | PASS | 65 test files / 1463 tests passed — re-run and read by the final reviewer, not just implementer-reported |
| `tsc --noEmit` | PASS | clean, exit 0 (final reviewer re-verified) |
| `eslint . --max-warnings=0` | PASS | clean, exit 0 (final reviewer re-verified) |
| `pnpm run build` | PASS | exit 0, `node scripts/build-plugin.mjs` (implementer-run, Task 4 gate table) |
| BACKLOG retirement | DONE | exactly two source-conflicts maintenance rows removed (`3b9c5af`); Desktop Conflict Inbox journey row untouched |

Commits: `9ea6f06` plan+spec docs · `3197892` Task 1 extract primitive · `af33f58` Task 2
device-sync rewire · `34da82d` Task 2 fix round (TOCTOU refusal) · `2246971` Task 3
conflict sweep · `3b9c5af` Task 4 BACKLOG retirement.

## What changed

- `apps/obsidian-plugin/src/device-sync/atomic-vault-mutation.ts` (new, + direct seam
  tests): plugin-internal, Obsidian-agnostic stage/verify/replace primitive
  (`stageVerifyAndReplaceVaultContent`) plus exact-token sibling cleanup
  (`cleanupExactVaultSiblings`); private typed failure with closed stages, sibling-name
  builders moved here as their canonical home.
- `apps/obsidian-plugin/src/device-sync/atomic-vault-writer.ts`: `stageAndReplace` rewired
  onto the primitive through a sequencing seam wrapper (below); caller-owned target-shape
  checks retained; primitive failures mapped onto the existing closed
  `AtomicVaultWriterError` vocabulary (no new tokens); the duplicated inline
  stage/verify/replace body and `#trashQuietly` deleted.
- `apps/obsidian-plugin/src/conflicts/composition.ts`: the canonical-outcome applier
  delegates to the primitive and adds the stage-guarded exact-token failed-apply sweep
  (below); the duplicated local closure and `hashesTo` deleted.
- `docs/handoff/BACKLOG.md`: the cleanup-sweep and shared-core source-conflicts rows
  retired; two new trigger-based rows added (below).

No API, schema, Web UI, or canonical-contract change — no canonical doc update required.

## Spec-interpretation adjudications

1. **Task 2 durable-point placement (controller ruling).** The plan's Task 2 Step 2
   sketch places the durable `temp_verified` transition **before** the single primitive
   call. Because the primitive bundles staging atomically with the replace, that placement
   lands the transition before the hidden staging write — verified empirically to break 4
   `remote-event-applier.test.ts` crash tests (the row already reads `temp_verified` at
   the first staging write; both crash-after-staging tests resume+complete instead of
   abandoning behind the repair barrier; the `verify_final` test's corrupt-read ordinal
   shifts onto `prove_base`) — i.e. a durable semantics change. Binding sections govern
   over the illustrative sketch: the task title ("without changing durability
   semantics"), the spec's behavior pinning (Required behavior #5; acceptance criteria —
   the atomic-writer suites pin the same success/rollback/ambiguity/trash behavior after
   extraction), and the plan's Global Constraints. Implemented as a delegating
   sequencing-seam wrapper whose `renameLocator` fires the transition exactly once,
   immediately before the primitive's first rename — always the first visible mutation,
   the same durable point the previous inline code used (between staged-bytes
   verification and the replace). All 47 applier tests pass unmodified. The clean
   successor (a formal pre-replace hook on the primitive) is backlogged (Row A).
2. **Task 3 sweep scope.** The plan's Task 3 Step 3 sketch shows an unconditional
   `cleanupExactVaultSiblings` in the catch; spec Required behavior #3 scopes the sweep
   guarantee to failures "between staging and replace" and #4 forbids data loss — at
   `replace` the rollback sibling may hold the only copy of the old bytes (target renamed
   away, temp rename-in failed). Implemented as a stage guard on the caught
   `AtomicVaultMutationFailure.stage`: pre-replace stages (`stage`, `verify_staged`,
   `prove_base`) sweep both exact-token siblings (nothing else can exist yet — this also
   reclaims the primitive's `prove_base` read-throw leftover, which retains the staged
   temp sibling); `replace` trashes only the exact-token temp sibling (winner bytes are
   re-derivable; the parked repair row keeps the apply owed); `verify_final` sweeps
   nothing (the replace consumed the temp; the rollback was consumed by a successful
   restore or must stay preserved when `restoredToBase: false`); foreign throws sweep
   nothing (they prove nothing about the disk). No scan/prefix/glob anywhere; every
   locator is derived from target + the command's own opaque token.
3. **Task 2 Step 1 "red ordering assertion" unachievable.** The invariant (transition
   before the first visible mutation) already held in the pre-task code — the transition
   sat between verification and the replace — so no honest test could go red; the only
   way to force red is the sketch-literal placement of adjudication 1, i.e. a durability
   regression. The two ordering tests stand as green pins on both sides of the rewire
   (documented baseline run in the task report): they assert the durable row's state at
   the exact moment of the first visible mutation plus the plan-verbatim transition
   record. The final reviewer judged the resulting pins stronger than the sketch's
   containment assertion.

## Review-found fix (Task 2 fix round, commit `34da82d`)

TOCTOU bypass in the initial rewire: an **updated** apply whose target vanished between
the occupied-target shape check and the primitive's `prove_base` read (a window spanning
the entire staging write) silently took the created shape with the pinned-base proof
skipped. Fixed by refusing in the sequencing seam's `readBytes` (null target read, no
durable proof yet, update, target locator) → mapped to the closed divergence refusal
(`vault_mutation` / `device_manifest_local_diverged`, non-retryable — the apply settles
as a conflict). Red→green test pins it; a second new test pins the transition-refusal
closure channel (`verify_temp` + carried store reason, no mutation).

## Deferred items (verdicts)

### Closed by verdict — code stands, no BACKLOG rows

- **`failureOf` test helper's structural guard also matches `AtomicVaultWriterError`**
  (final-review finding 1): the fake seams throw plain `Error`s, so the structural path
  never sees a wrapped error and the false positive cannot fire. An `instanceof`
  refinement would add coupling without preventing any reachable failure.
- **Raw `locatorExists` throw on the writer's updated path** (finding 5; adapter IO
  rejection unwrapped at the shape check): the applier's catch fallthrough maps any
  foreign throw to a closed token — nothing raw escapes to callers or logs.
- **One extra null read at `prove_base` for created/restored applies** (finding 6): no
  test pins a read ordinal for those operations; zero behavioral effect.
- **The primitive's `prove_base` read-throw branch retains the staged temp sibling**
  (finding 8; only the fingerprint-mismatch branch trashes it): self-healing — the
  composition failure sweep removes it on the next failed/retried apply, and the writer's
  prepared-state recovery reclaims it.

### Backlogged out-of-scope improvements — see BACKLOG rows below

- **Residual `hashesTo`/`#restoreRollback` duplication** between writer and primitive
  (finding 2): not dead code — the writer's rename/move/trash/recover paths still use
  them; dedup requires exporting from the primitive module. Row A.
- **Composition ignores `cleanupExactVaultSiblings`'s boolean** (finding 3): a
  persistently refused rollback-sibling trash is silently data-preserving (spec req 4
  compliant); a `conflict_sibling_cleanup_failed` closed token would make it observable.
  Row A.
- **`prove_base` read-throws map to the non-retryable divergence token** (finding 4): a
  transient read error durably settles the event as a conflict instead of retrying —
  fail-safe (bytes preserved), self-heals via reconciliation, pinned by test. Clean fix
  is a cause-bearing primitive failure. Row A.
- **Writer↔primitive implicit protocol** (final-review Important #2): the sequencing
  wrapper assumes the first `renameLocator` is the first visible mutation, and the TOCTOU
  guard identifies the prove-base read positionally. Design debt needing a follow-up
  plan. Row A.
- **Replace-stage rollback-sibling residue**: a conflict apply `replace`-stage failure
  leaves one exact-token rollback sibling un-reclaimed by retries (the retry takes the
  created shape over the absent target). Data-preserving, spec req 3 compliant; reclaiming
  requires a recovery sweep with the naming-contract proof. Row B.

BACKLOG rows added (2026-09-03, section "Trigger-based deferred work"): Row A — domain
`vault-mutation`, the follow-up primitive hardening plan, `Before the next
atomic-vault-mutation contract extension`; Row B — domain `source-conflicts`, the
replace-stage rollback-sibling recovery sweep, `When a conflict vault-apply recovery
sweep is designed`. Both point here.

## Next actions

1. Follow-up vault-mutation hardening plan when Row A's trigger fires — scope: a
   cause-bearing `AtomicVaultMutationFailure` (read-throw vs fingerprint mismatch;
   target-absent distinction or a `requireOccupiedTarget` flag — removes the writer
   wrapper's positional null-read heuristic and the `prove_base` read-throw →
   divergence-token nuance), a formal pre-first-visible-mutation hook retiring the
   sequencing wrapper's implicit protocol, the `conflict_sibling_cleanup_failed` closed
   token, and dedup of the writer's residual `hashesTo`/`#restoreRollback`.
2. Merge `conflict-vault-apply-hardening` via the usual branch-finishing flow.
