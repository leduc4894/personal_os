# Two-Vault Synchronization Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove two independent Obsidian Vault plugin clients converge through the canonical stack for normal, concurrent, recovery, and bounded-bulk operations.

**Architecture:** Two isolated plugin actors use separate journals, Vault roots and credentials but share one disposable canonical stack. A redacted oracle compares canonical, A and B state after each case; real Obsidian two-Vault behavior remains a guarded operator gate.

**Tech Stack:** TypeScript strict, Vitest, WDIO, Python, pytest, PostgreSQL, R2.

**Spec:** `docs/superpowers/specs/2026-09-04-two-vault-synchronization-acceptance-design.md`

## Global Constraints

- No fake HTTP server or direct API writer substitutes for either plugin actor.
- Live work uses `CI=true bash .local/serve-live-ci.sh up knowledge-ci-*` and approved bootstrap helpers.
- Diagnostics/evidence never carry paths, content, digests, credentials, URLs, object keys, or request IDs.
- Bulk stays at 100 small files/20 edits; above 10,000 pending events or 100 MiB asserts the documented refusal.

---

## Approved task decomposition

This section supersedes the earlier four-workstream draft below. Each task has
its own RED/PASS/commit cycle; the detailed file and command instructions in
the following sections apply to the matching task.

1. **Actor isolation and oracle:** separate A/B device, journal and Vault state; redacted convergence assertions.
2. **Create/update propagation:** TV-01–TV-02.
3. **Lifecycle propagation:** TV-03–TV-07 (rename, move, delete, restore, create-delete-before-pull).
4. **Ordering and offline replay:** TV-08–TV-13.
5. **Content conflicts:** TV-14–TV-17 (Markdown, binary, edit/delete, delete/edit).
6. **Lifecycle conflicts and stale resolution:** TV-18–TV-20.
7. **Crash, suspension and cursor repair:** TV-21–TV-25.
8. **Bounded bulk and burst:** TV-26–TV-30 plus capacity/size refusal.
9. **Guarded acceptance and operations:** bootstrap allowlist, closed phase tokens, runbook, canonical docs, operator evidence, handoff.

No task is accepted solely because a later task passes. A defect found in any
task first receives a focused regression test, then the smallest repair, then
the owning two-Vault case is rerun.

---

### Task 1: Build isolated actors and redacted convergence oracle

**Files:**
- Create: `apps/obsidian-plugin/test/support/two-vault-live-actors.ts`
- Create: `apps/obsidian-plugin/test/support/two-vault-evidence.ts`
- Test: `apps/obsidian-plugin/test/support/two-vault-live-actors.test.ts`

**Interfaces:** `createTwoVaultLiveActors(options): Promise<TwoVaultLiveActors>` returns `actorA`/`actorB`; each `VaultActor` supplies `createFile`, `updateFile`, `renameFile`, `moveFile`, `deleteFile`, `restoreFile`, `requestCycle`, `requestRepair`, `goOffline`, `goOnline`, `restart`, and `readSafeState`. `assertConverged(input)` throws only safe aggregate state/count fields.

- [ ] **Step 1: Write failing isolation tests**

```ts
it("uses distinct device and journal identities", async () => {
  const actors = await createTwoVaultLiveActors({ projectName: "knowledge-ci-two-vault-unit" });
  expect(actors.actorA.deviceId).not.toBe(actors.actorB.deviceId);
});
```

- [ ] **Step 2: Verify RED** — run `pnpm --dir apps/obsidian-plugin exec vitest run test/support/two-vault-live-actors.test.ts`; expect missing factory/oracle.
- [ ] **Step 3: Implement the smallest actor boundary** — drive every local operation through capture, lifecycle-capture, queue-driver, coordinator, remote-event-applier, and manifest-reconciler; keep raw roots/bytes private.
- [ ] **Step 4: Verify PASS** — run focused Vitest, `pnpm --dir apps/obsidian-plugin run type-check`, and `pnpm --dir apps/obsidian-plugin run lint`.
- [ ] **Step 5: Commit** — stage only the three Task-1 support/test files with message `test: add isolated two-vault actor harness`.

### Task 2: Add baseline, ordering, and conflict matrix

**Files:**
- Create: `apps/obsidian-plugin/test/specs/two-vault-synchronization.e2e.ts`
- Modify: `apps/obsidian-plugin/test/support/two-vault-live-actors.ts`
- Test: `apps/obsidian-plugin/test/specs/two-vault-synchronization.e2e.ts`

**Interfaces:** Consumes Task 1 and emits closed phase tokens for TV-01–TV-20.

- [ ] **Step 1: Write failing TV-01–TV-20 cases**

```ts
it("TV-03 propagates an A rename without a new source", async () => {
  await actors.actorA.createFile(fixture.bytes);
  await actors.actorA.renameFile();
  await actors.actorB.requestCycle();
  await assertConverged({ actors, expected: { sourceCount: 1, lifecycle: "renamed" } });
});
```

- [ ] **Step 2: Verify RED** — start `knowledge-ci-two-vault-matrix` with the mandatory stack script, then run WDIO against the new spec; expect missing orchestration/phase wiring.
- [ ] **Step 3: Implement orchestration only** — cover TV-01–07 baseline, TV-08–13 offline/replay/reconcile, TV-14–20 text/binary/edit-delete/locator/stale-resolution races. Use bounded safe waits and canonical aggregates; when a defect appears, first add a focused `src/**/*.test.ts` RED regression, then minimally fix production code.
- [ ] **Step 4: Verify PASS** — run `uv run poe device-sync-test`, plugin Vitest, type-check, lint, build, then the guarded two-Vault spec.
- [ ] **Step 5: Commit** — stage Task-2 E2E/support/regression files with message `test: cover two-vault propagation and conflicts`.

### Task 3: Add recovery and bounded bulk coverage

**Files:**
- Create: `apps/obsidian-plugin/src/device-sync/two-vault-synchronization.test.ts`
- Modify: `apps/obsidian-plugin/test/specs/two-vault-synchronization.e2e.ts`

**Interfaces:** Produces TV-21–TV-30, parameterized by `fileCount` and `editCount`.

- [ ] **Step 1: Write failing recovery/bulk tests**

```ts
it("TV-24 repairs a cursor gap while A commits post-checkpoint work", async () => {
  await actors.induceCursorGap(actors.actorB);
  await actors.actorA.updateFile(nextBytes);
  await actors.actorB.requestRepair();
  await assertConverged({ actors, expected: { repair: "completed" } });
});
```

- [ ] **Step 2: Verify RED** — run `pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/two-vault-synchronization.test.ts`.
- [ ] **Step 3: Implement safe fixtures** — cover crash-after-commit/apply, suspension, cursor gap, policy advance, 100-file copy/edit/delete/folder move, and 20-save rename/edit/rename. Do not parallelize production drain or raise limits; assert closed capacity/size status.
- [ ] **Step 4: Verify PASS** — run focused Vitest/type-check/lint and TV-21–TV-30 on the disposable stack.
- [ ] **Step 5: Commit** — stage Task-3 files with message `test: add two-vault recovery and bulk coverage`.

### Task 4: Register guarded acceptance, docs, and handoff

**Files:**
- Modify: `tools/obsidian_live_acceptance_bootstrap.py`
- Modify: `apps/obsidian-plugin/test/support/live-acceptance-phase-status.ts`
- Create: `docs/operations/two-vault-synchronization.md`
- Modify: `docs/16-TESTING_AND_EVALUATION.md`
- Modify: `docs/20-IMPLEMENTATION_PLAN.md`
- Test: `tools/test_obsidian_live_acceptance_bootstrap.py`
- Test: `apps/obsidian-plugin/test/support/live-acceptance-phase-status.test.ts`

**Interfaces:** Registers exactly `test/specs/two-vault-synchronization.e2e.ts` and only closed `two_vault_*` phase tokens.

- [ ] **Step 1: Write failing registration tests** — parser must accept the exact new WDIO spec; TypeScript test must accept `two_vault_baseline_completed` and reject an unregistered token.
- [ ] **Step 2: Verify RED** — run the focused Python and TypeScript registration suites; expect allowlist/token absence.
- [ ] **Step 3: Implement minimum registration and runbook** — document CI stack, bootstrap, two empty test Vaults, safe operator actions/outcomes, polling cadence, recovery checkpoints, and teardown. Only unavailable physical Mobile evidence can defer, once, in BACKLOG.
- [ ] **Step 4: Final verification** — run `git diff --check`, `uv run poe device-sync-test`, plugin Vitest/type-check/lint/build, guarded two-Vault Desktop journey, and inspect the sanitized result.
- [ ] **Step 5: Commit and handoff** — commit registration/docs; write exactly one `docs/handoff/YYYY-MM-DD-two-vault-synchronization.md` with final SHA, evidence, decisions, and mobile gate state.

## Plan self-review

- **Spec coverage:** Task 1 covers actor isolation/oracle; Task 2 TV-01–TV-20; Task 3 TV-21–TV-30 and bounds; Task 4 live gate, privacy/diagnostics, docs, and handoff.
- **Completeness scan:** no unresolved implementation markers; a discovered product defect always starts with a focused failing regression.
- **Type consistency:** Task 1 defines the actor/oracle consumed by Tasks 2–3; Task 4 registers the exact E2E filename from Task 2.
