# Device Cursor-Gap Repair Handoff

Plan: `docs/superpowers/plans/2026-09-02-device-cursor-gap-repair.md`
Spec: `docs/superpowers/specs/2026-09-02-device-cursor-gap-repair-design.md`
Branch: `device-cursor-gap-repair` (from `master` `61e61c0`; no worktree, per operator instruction)

## Status: COMPLETE

Final code commit: `72ba543` (docs commits follow this handoff). All plan tasks done,
final whole-branch review verdict: **ready to merge** (one fix wave applied and
re-reviewed clean).

| Gate | Result | Evidence |
|---|---|---|
| Journey regression (red → green) | PASS | `device-sync-journey.test.ts` "repairs a cursor gap created inside delete-and-recreate reconciliation" — red at second assertion pre-fix, green post-fix incl. convergence + idempotency + no-new-conflict-row assertions |
| `poe device-sync-test` (CI-env form) | PASS | 1863 passed / 2 skipped / 1 deselected, exit 0 (disposable `knowledge-ci-cursor-gap-verify-20260902`, conftest-provisioned and cleaned; `knowledge-local` untouched) |
| `tsc --noEmit` | PASS | exit 0 (implementer, Task 3, controller, fix wave) |
| `pnpm run lint` | PASS | exit 0 (implementer + controller re-ran) |
| `pnpm run build` | PASS | exit 0 |
| BACKLOG retirement | DONE | exactly one row removed (2026-09-01 device-sync cursor-gap row, commit `47cff5a`); closed-reason smoke row retained |

Commits: `ac7d857` plan+spec docs · `81bf57b` red journey (plan-verbatim) ·
`3d33272` amended fault injection (real re-block reproduction) · `5b5b0bb` production
fix · `47cff5a` BACKLOG retirement · `72ba543` assertion strengthening.

## What changed

- `apps/obsidian-plugin/src/device-sync/manifest-reconciler.ts`: at the
  synthetic-event-fit edge (`applied+1 > checkpoint`), when the durable barrier
  already reads `device_cursor_gap`, the stale checkpoint-frozen run is closed via
  `discardActiveManifestRun` + the same `api.completeManifest` wire call at its
  persisted checkpoint (no local cursor moves — `applied >= checkpoint` holds
  there), then one fresh checkpoint-bound run is re-minted through the existing
  restart loop under the same barrier; convergence still lands solely through the
  canonical fence (`completeManifest` → `journal.completeDeviceSyncRepair`).
  Bounded at two run attempts; crash-safe ordering verified in review.
- `apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts`: the
  delete-and-recreate regression journey + convergence/idempotency/no-new-row
  assertions.
- `docs/handoff/BACKLOG.md`: defect row retired; one test-hygiene row added (below).

No API route, PostgreSQL schema, server retention, or canonical-contract change —
no canonical doc update required (final-review verdict; recovery behavior documented
in the module docstring).

## Interpretive decisions (spec over plan text)

1. **Task 1's verbatim test snippet pinned an unreachable state.** As written, the
   harness converges on the first repair (conflict-only plan terminalizes without a
   synthetic event; the completion fence absorbs the gap), so the intermediate
   `device_cursor_gap` barrier never exists. Ruling: the spec's Required behavior #2
   ("The deferred sequence produces the existing device_cursor_gap result") governs;
   round 2 amended the journey's fault injection (peer standing divergence + own
   echo + tombstone across the deferred sequence, all via pre-existing harness
   hooks) to genuinely reproduce the live re-block. Controller asked the operator
   (no answer); the relax-and-retire alternative was rejected because it would
   close a live defect row on a harness that never reproduced it.
2. **Task 2's Step-2 idempotency snippet is unsatisfiable.** `expect(server.completions).toHaveLength(completions)`
   contradicts pinned journey 1 (clean-state explicit repair completes exactly one
   run). Ruling: spec #5's actual clause governs ("neither replays the same event
   nor creates a new conflict/repair row"); adapted assertions pin barrier-null,
   run-null, cursors-equal-and-unmoved, completions +1, and (fix wave) the exact
   `["conflict"]` plan — any action beyond the fixture's standing peer conflict
   fails.
3. **The plan's hint method `repository.completeManifestRun` does not exist.** The
   plan itself said to use actual names; the real fence is
   `journal.completeDeviceSyncRepair({manifestRunId, checkpointSequence, barrierGeneration})`.

## Deferred items (verdicts)

- **Journey-test `console.log` leftovers** (GAP/ED PLAN debug lines, ~1243-1245 and
  ~1412, from earlier round commit `8cc9515`) — out of scope (pre-existing, another
  round's journeys); BACKLOG row added 2026-09-02, implement before the next
  device-sync live round.
- **Stale cross-reference in the retained closed-reason row** (BACKLOG L27 still
  says "see the 2026-09-01 device-sync rows below") — out of scope: that row belongs
  to the closed-reason-surfacing domain; its own trigger ("after the
  device-manifest recovery fixes land") is now satisfied, so its owner should
  refresh the row text (and the live parity note below) on the next touch. The
  existing row is that item's BACKLOG index — no duplicate row added.
- **Live parity of the "frozen open run" premise** — the reproduction's
  frozen-open-server-run deadlock is harness-verified and code-traced, but the live
  2026-09-01 shape is still inference. Next actions carry the re-verification; not
  a merge gate (plan retires on automated gates).
- **Decorative `2` suffix in a journey version id; `asOutcome` stale-checkpoint
  branch is defensively unreachable** — code stands (final-review triage:
  acceptable, matches file style / fail-closed by design); no BACKLOG row.

## Next actions

1. On the next device-sync live round (the closed-reason row's trigger — now
   satisfied): confirm a delete-and-recreate Repair sync converges on the real
   stack, and let the closed-reason-surfacing owner refresh their BACKLOG row.
2. Delete the journey debug `console.log` lines in the next journey-file edit
   (BACKLOG row 2026-09-02 device-sync).
3. Merge `device-cursor-gap-repair` per the finishing-branch decision.
