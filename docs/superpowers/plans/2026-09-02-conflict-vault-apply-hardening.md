# Conflict Vault-Apply Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Share safe vault stage/verify/replace mechanics and clean exact conflict staging siblings left by a failed apply.

**Architecture:** Extract an internal seam-agnostic byte-mutation primitive. Device-sync continues to own durable remote-apply transitions; conflict composition continues to own echo markers and `CanonicalApplyError` mapping. The primitive may clean only exact opaque-token sibling locators and never performs a vault scan.

**Tech Stack:** TypeScript strict, Vitest, Obsidian structural vault adapter seam, journal persistence.

**Spec:** `docs/superpowers/specs/2026-09-02-conflict-vault-apply-hardening-design.md`

## Global Constraints

- Do not broad-scan/delete by staging prefix. Cleanup receives the target locator and the owning opaque token.
- Preserve `AtomicVaultWriter`, `CanonicalOutcomeApplier`, existing closed reason vocabularies, marker ordering, and visible-file trash behavior.
- Ambiguous bytes remain preserved and blocked; only hidden internal siblings may use adapter removal.
- Do not change API/schema/Web UI or run Desktop/Mobile journeys. Retire only the two source-conflicts maintenance rows.

---

## File structure

- Create: `apps/obsidian-plugin/src/device-sync/atomic-vault-mutation.ts`: shared stage/verify/replace and exact-cleanup primitive.
- Create: `apps/obsidian-plugin/src/device-sync/atomic-vault-mutation.test.ts`: primitive behavior.
- Modify: `apps/obsidian-plugin/src/device-sync/atomic-vault-writer.ts`: device durable adapter.
- Modify: `apps/obsidian-plugin/src/conflicts/composition.ts`: conflict adapter and cleanup.
- Modify tests: `atomic-vault-writer.test.ts`, `conflicts/composition.test.ts`.
- Modify: `docs/handoff/BACKLOG.md`.

### Task 1: Establish the internal mutation primitive

**Files:**

- Create: `apps/obsidian-plugin/src/device-sync/atomic-vault-mutation.ts`
- Create: `apps/obsidian-plugin/src/device-sync/atomic-vault-mutation.test.ts`

**Interfaces:**

- Consumes: `VaultMutationSeam`, `FrozenFingerprint`, `buildTempSiblingLocator`, `buildRollbackSiblingLocator`.
- Produces: `stageVerifyAndReplaceVaultContent(input)` and `cleanupExactVaultSiblings(seam, input)`.

- [ ] **Step 1: Write failing direct seam tests.**

```ts
const result = await stageVerifyAndReplaceVaultContent({
  seam,
  targetLocator: "notes/a.md",
  tempToken: "owned-token",
  bytes: new TextEncoder().encode("remote bytes"),
  expectedFinalFingerprint: fingerprintOf("remote bytes"),
  expectedBaseFingerprint: fingerprintOf("local bytes"),
});
expect(result.rollbackLocator).toBeNull();
await cleanupExactVaultSiblings(seam, {
  targetLocator: "notes/a.md",
  tempToken: "owned-token",
});
expect(await seam.locatorExists(buildTempSiblingLocator("notes/a.md", "owned-token"))).toBe(false);
expect(await seam.locatorExists("notes/.a.md.device-sync-tmp-other-token")).toBe(true);
```

Include final-verification failure that restores verified old bytes.

- [ ] **Step 2: Run the test file.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/atomic-vault-mutation.test.ts`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the smallest internal types and behavior.**

```ts
export interface AtomicVaultMutationInput {
  readonly seam: VaultMutationSeam;
  readonly targetLocator: string;
  readonly tempToken: string;
  readonly bytes: Uint8Array;
  readonly expectedFinalFingerprint: FrozenFingerprint;
  readonly expectedBaseFingerprint: FrozenFingerprint | null;
}
export interface AtomicVaultMutationResult {
  readonly rollbackLocator: string | null;
  readonly restoredToBase: boolean;
}
```

Stage and verify exact bytes, prove the optional base, retain/verify rollback bytes, verify the final bytes, and expose only a private typed failure for caller mapping.

- [ ] **Step 4: Run primitive tests and commit.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/atomic-vault-mutation.test.ts`

Expected: PASS.

```bash
git add apps/obsidian-plugin/src/device-sync/atomic-vault-mutation.ts apps/obsidian-plugin/src/device-sync/atomic-vault-mutation.test.ts
git commit -m "refactor: extract atomic vault mutation primitive"
```

### Task 2: Adapt device-sync without changing durability semantics

**Files:**

- Modify: `apps/obsidian-plugin/src/device-sync/atomic-vault-writer.ts`
- Modify: `apps/obsidian-plugin/src/device-sync/atomic-vault-writer.test.ts`

**Interfaces:**

- Consumes: Task 1 primitive and `DeviceSyncRepository.transitionRemoteApply`.
- Produces: unchanged `AtomicVaultWriterImpl.stageAndReplace(input): Promise<VerifiedVaultMutation>`.

- [ ] **Step 1: Add a red ordering assertion.**

```ts
await writer.stageAndReplace(contentInput(base, next));
expect(repository.transitions).toContainEqual({
  eventSequence: EVENT_SEQUENCE,
  state: "temp_verified",
  tempToken: TEMP_TOKEN,
});
```

The test must assert that the durable `temp_verified` transition occurs before the seam's first visible mutation.

- [ ] **Step 2: Move only byte-level work to the shared primitive.**

```ts
await this.#repository.transitionRemoteApply({
  eventSequence: input.eventSequence,
  state: "temp_verified",
  tempToken: input.tempToken,
});
const result = await stageVerifyAndReplaceVaultContent(normalizedInput);
```

Retain the writer's existing target-shape checks and map the primitive failure to the current `AtomicVaultWriterError` stage/reason.

- [ ] **Step 3: Run and commit focused device tests.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/device-sync/atomic-vault-writer.test.ts src/device-sync/remote-event-applier.test.ts`

Expected: PASS.

```bash
git add apps/obsidian-plugin/src/device-sync/atomic-vault-writer.ts apps/obsidian-plugin/src/device-sync/atomic-vault-writer.test.ts
git commit -m "refactor: reuse vault mutation primitive in device sync"
```

### Task 3: Adapt and harden conflict canonical apply

**Files:**

- Modify: `apps/obsidian-plugin/src/conflicts/composition.ts`
- Modify: `apps/obsidian-plugin/src/conflicts/composition.test.ts`

**Interfaces:**

- Consumes: Task 1 primitive, `createConflictCanonicalOutcomeApplier`, `CanonicalApplyError`, existing echo-marker methods.
- Produces: unchanged `CanonicalOutcomeApplier.applyCanonicalOutcome(command): Promise<void>`.

- [ ] **Step 1: Add a red failed-apply cleanup test.**

Force the existing seam to fail after temporary staging; retry the same command and assert its recorded temp locator is gone while a separately created hidden sibling with a different token remains. Assert the error remains `{ stage: "vault_apply" }`.

- [ ] **Step 2: Run the named regression.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/conflicts/composition.test.ts -t "cleans only its staging sibling after failed apply"`

Expected: FAIL because current composition has no exact failed-apply sweep.

- [ ] **Step 3: Replace duplicated local mutation with the primitive and clean exact siblings in the failed branch.**

```ts
try {
  await stageVerifyAndReplaceVaultContent(mutationInput);
} catch {
  await cleanupExactVaultSiblings(seam, { targetLocator: locator, tempToken });
  await consumeMarkerQuietly(marker);
  throw new CanonicalApplyError("vault_apply");
}
```

Keep marker recording before mutation, consumption best-effort, and every mutation failure mapped to `vault_apply`.

- [ ] **Step 4: Run conflict tests and commit.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run src/conflicts/composition.test.ts src/conflicts/controller.test.ts`

Expected: PASS.

```bash
git add apps/obsidian-plugin/src/conflicts/composition.ts apps/obsidian-plugin/src/conflicts/composition.test.ts
git commit -m "fix: clean conflict apply staging siblings"
```

### Task 4: Gate and retire the two maintenance rows

**Files:**

- Modify: `docs/handoff/BACKLOG.md`

- [ ] **Step 1: Run plugin gates.**

Run: `pnpm --dir apps/obsidian-plugin exec vitest run; pnpm --dir apps/obsidian-plugin exec tsc --noEmit; pnpm --dir apps/obsidian-plugin run lint; pnpm --dir apps/obsidian-plugin run build`

Expected: every command exits 0.

- [ ] **Step 2: Remove only the cleanup-sweep and shared-core rows.**

Do not alter the Desktop Conflict Inbox journey row.

- [ ] **Step 3: Check diff and commit.**

Run: `git diff --check; git diff -- docs/handoff/BACKLOG.md`

Expected: clean diff check; no live row changes.

```bash
git add docs/handoff/BACKLOG.md
git commit -m "docs: retire conflict vault apply maintenance rows"
```

