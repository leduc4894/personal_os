# Device Cursor-Gap Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make explicit Repair sync converge a cursor gap created inside a delete-and-recreate reconciliation.

**Architecture:** Add one red regression to the existing plugin journey. Trace the persisted barrier and completion-fence state that differs from the already-passing STARTUP cursor-gap recovery, then reuse that exact completion path; do not create a second recovery mechanism.

**Tech Stack:** TypeScript strict, Vitest, Obsidian-plugin journal and reconciliation modules.

**Spec:** `docs/superpowers/specs/2026-09-02-device-cursor-gap-repair-design.md`

## Global Constraints

- Preserve the closed `device_cursor_gap`, `device_manifest_local_diverged`, and `identity_ambiguous` tokens; diagnostics never contain locators, bytes, digests or IDs.
- Use serialized journal transitions and preserve reload/idempotency behavior.
- Do not change API routes, PostgreSQL schema, server retention, or run any Desktop/Mobile live journey.
- Retire only the device-sync defect row after all automated gates pass. The closed-reason smoke row remains.

---

## File structure

- `apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts`: in-memory end-to-end regression.
- `apps/obsidian-plugin/src/device-sync/manifest-reconciler.ts`: manifest-run completion and repair-barrier release.
- `apps/obsidian-plugin/src/device-sync/sync-coordinator.ts`: explicit repair dispatch, changed only if the red trace proves it is the skip point.
- `apps/obsidian-plugin/src/device-sync/repository.ts`: durable transition, changed only if its validation rejects the valid completion fence.
- `docs/handoff/BACKLOG.md`: retirement record.

### Task 1: Pin the delete-and-recreate regression

**Files:**

- Modify: `apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts`

**Interfaces:**

- Consumes: `buildPluginStack(server)`, `ScriptedServer.deferSequences`, `coordinator.request("explicit_repair")`, and `deviceSyncRepository.readState()`.
- Produces: a journey whose successful repair has null barrier/active run and equal applied/acknowledged cursors at the run checkpoint.

- [ ] **Step 1: Add the failing journey beside the existing cursor-gap repair journey.**

```ts
it("repairs a cursor gap created inside delete-and-recreate reconciliation", async () => {
  const server = new ScriptedServer();
  const stack = buildPluginStack(server);
  await server.commitCreate(LOCATOR, bytesOf("committed bytes"));
  stack.coordinator.request("startup");
  await flushCycles();

  stack.vault.deleteFile(LOCATOR);
  stack.vault.setFileBytes(LOCATOR, bytesOf("recreated bytes"));
  server.deferSequences(1);
  stack.coordinator.request("explicit_repair");
  await flushCycles();
  expect(stack.deviceSyncRepository.readState().barrierReason).toBe("device_cursor_gap");

  stack.coordinator.request("explicit_repair");
  await flushCycles();
  expect(stack.deviceSyncRepository.readState().barrierGeneration).toBeNull();
});
```

- [ ] **Step 2: Run the isolated test and preserve the re-blocking failure as red evidence.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/device-sync-journey.test.ts -t "repairs a cursor gap created inside delete-and-recreate reconciliation"`

Expected: FAIL because the second explicit repair leaves `device_cursor_gap`.

- [ ] **Step 3: Inspect only test-harness state to identify the skipped transition.**

Compare the STARTUP gap journey and this run's manifest action outcomes, active run ID, completion record and cursor values. Locate whether `manifest-reconciler.ts`, `sync-coordinator.ts`, or `repository.ts` blocks the established completion fence. Do not add production logging.

- [ ] **Step 4: Commit the red regression.**

```bash
git add apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts
git commit -m "test: cover recreated-file cursor gap"
```

### Task 2: Reuse the canonical repair completion fence

**Files:**

- Modify: `apps/obsidian-plugin/src/device-sync/manifest-reconciler.ts`
- Modify if required by Task 1 trace: `apps/obsidian-plugin/src/device-sync/sync-coordinator.ts` or `repository.ts`
- Test: `apps/obsidian-plugin/src/device-sync/device-sync-journey.test.ts`
- Test if repository changes: `apps/obsidian-plugin/src/device-sync/repository.test.ts`

**Interfaces:**

- Consumes: existing explicit-repair reason, active manifest run, closed action settlement and repository completion-fence transition.
- Produces: the existing completion fence using the run's persisted checkpoint; no new public method or reason.

- [ ] **Step 1: Make the smallest state-edge change revealed by Task 1.**

The completed reconcile must invoke the existing completion transition with its persisted checkpoint after a closed local-divergence action:

```ts
await repository.completeManifestRun({
  manifestRunId: run.manifestRunId,
  appliedThrough: run.checkpointSequence,
  acknowledgedThrough: run.checkpointSequence,
});
```

Use the selected module's actual current method and parameter names. Do not synthesize cursor advancement, clear only in-memory state, or bypass repository validation.

- [ ] **Step 2: Strengthen the journey with convergence and idempotency assertions.**

```ts
const state = stack.deviceSyncRepository.readState();
expect(state.activeManifestRunId).toBeNull();
expect(state.appliedSequence).toBe(state.acknowledgedSequence);
const completions = server.completions.length;
stack.coordinator.request("explicit_repair");
await flushCycles();
expect(server.completions).toHaveLength(completions);
```

- [ ] **Step 3: Run focused tests.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/device-sync-journey.test.ts src/device-sync/manifest-reconciler.test.ts src/device-sync/sync-coordinator.test.ts src/device-sync/repository.test.ts`

Expected: PASS.

- [ ] **Step 4: Commit the fix.**

```bash
git add apps/obsidian-plugin/src/device-sync
git commit -m "fix: complete repair after recreated-file cursor gap"
```

### Task 3: Verify and retire the resolved defect

**Files:**

- Modify: `docs/handoff/BACKLOG.md`

- [ ] **Step 1: Run the complete device-sync gate.**

Run: `uv run poe device-sync-test`

Expected: PASS.

- [ ] **Step 2: Run plugin static/build gates.**

Run: `pnpm --dir apps/obsidian-plugin exec tsc --noEmit; pnpm --dir apps/obsidian-plugin run lint; pnpm --dir apps/obsidian-plugin run build`

Expected: every command exits 0.

- [ ] **Step 3: Remove only the 2026-09-01 device-sync cursor-gap row.**

Do not alter the closed-reason live-smoke or any Desktop/Mobile evidence row.

- [ ] **Step 4: Check documentation diff and commit.**

Run: `git diff --check; git diff -- docs/handoff/BACKLOG.md`

Expected: clean diff check; exactly one device-sync row retired.

```bash
git add docs/handoff/BACKLOG.md
git commit -m "docs: retire cursor gap repair backlog item"
```

