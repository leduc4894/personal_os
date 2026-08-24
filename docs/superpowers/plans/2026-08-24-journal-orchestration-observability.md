# Journal Orchestration Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface a bounded closed reason token for every remaining swallowed
journal-orchestration failure without changing sync behavior.

**Architecture:** Add one typed, plugin-local reporter that appends only closed
`journal_failure` entries to the existing Sync diagnostics trail. Inject it into
the coordinators and capture components; each existing catch reports a static
site token, then keeps its current fail-closed result, scheduling, and cleanup.

**Tech Stack:** TypeScript strict, Obsidian plugin, Vitest, ESLint, existing
Sync diagnostics trail and Copy diagnostics export.

**Spec:** `docs/superpowers/specs/2026-08-24-journal-orchestration-observability-design.md`

## Global Constraints

- Use only the seven exact closed snake-case tokens in the spec; never append
  exception text, paths, ids, hashes, bytes, credentials, or provider data.
- Preserve queue ordering, retry timing, scan summaries, lifecycle outcomes,
  cancellation and fail-closed behavior.
- Reporter calls are fire-and-forget and must not make an existing safe path
  reject or block.
- Use TDD: create the focused failing assertion before its implementation.
- No production dependency or new diagnostics surface type.

---

### Task 1: Typed journal-failure reporter and closed vocabulary

**Files:**
- Create: `apps/obsidian-plugin/src/journal/diagnostic-reporter.ts`
- Modify: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts`
- Test: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.test.ts`
- Test: `apps/obsidian-plugin/src/journal/diagnostic-reporter.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export interface JournalFailureReporter {
    reportJournalFailure(token: SyncDiagnosticClosedToken): void;
  }
  export function createJournalFailureReporter(
    trail: SyncDiagnosticsTrail | null,
  ): JournalFailureReporter;
  ```
- Consumes: `SyncDiagnosticsTrail.append({ kind: "journal_failure", tokens })`.

- [ ] **Step 1: Write failing vocabulary and reporter tests**

  ```ts
  expect(SYNC_COMPOSITION_READ_FAILURE_TOKENS).toContain("retry_schedule_read_failed");
  reporter.reportJournalFailure("snapshot_drain_failed");
  expect(trail.readEntries()[0]).toMatchObject({
    kind: "journal_failure",
    tokens: ["snapshot_drain_failed"],
  });
  ```

- [ ] **Step 2: Run RED tests**

  Run: `pnpm --filter @workspace/obsidian-plugin test -- sync-diagnostics-trail diagnostic-reporter`

  Expected: FAIL because the new tokens and reporter module do not exist.

- [ ] **Step 3: Add the seven tokens and reporter**

  ```ts
  export const SYNC_COMPOSITION_READ_FAILURE_TOKENS = [
    "status_read_failed", "note_status_read_failed",
    "retry_schedule_read_failed", "sync_status_read_failed",
    "queue_drain_failed", "snapshot_drain_failed",
    "settled_admission_failed", "automatic_snapshot_admission_failed",
    "lifecycle_reconcile_persist_failed",
  ] as const;
  ```

  The reporter calls `void trail?.append({ kind: "journal_failure", tokens: [token] })`.

- [ ] **Step 4: Run GREEN tests and static checks**

  Run: `pnpm --filter @workspace/obsidian-plugin test -- sync-diagnostics-trail diagnostic-reporter`

  Expected: PASS; arbitrary strings fail TypeScript type checking.

- [ ] **Step 5: Commit**

  ```bash
  git add apps/obsidian-plugin/src/journal/diagnostic-reporter.ts apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts apps/obsidian-plugin/src/journal/*.test.ts
  git commit -m "feat: add typed journal failure reporter"
  ```

### Task 2: Surface composition-root read failures

**Files:**
- Modify: `apps/obsidian-plugin/src/plugin.ts:1210-1216,1276-1296`
- Test: `apps/obsidian-plugin/src/plugin.test.ts`

**Interfaces:**
- Consumes: `JournalFailureReporter.reportJournalFailure(token)` from Task 1.
- Produces: once-per-session reporting for retry scheduling and sync-status
  reads while retaining `return` and `return null` outcomes.

- [ ] **Step 1: Write failing source-contract tests**

  ```ts
  expect(retryReadCatchBody).toContain('"retry_schedule_read_failed"');
  expect(statusReadCatchBody).toContain('"sync_status_read_failed"');
  ```

- [ ] **Step 2: Run RED test**

  Run: `pnpm --filter @workspace/obsidian-plugin test -- plugin`

  Expected: FAIL because neither token is emitted.

- [ ] **Step 3: Implement two once-only reporters**

  Add private boolean guards mirroring `#recordStatusReadFailureOnce`; each
  guard calls the Task 1 reporter with its exact token. Invoke them in the
  catches at lines 1213 and 1292 before preserving the existing fallback.

- [ ] **Step 4: Run GREEN tests**

  Run: `pnpm --filter @workspace/obsidian-plugin test -- plugin`

  Expected: PASS; no retry timer or partial status is introduced.

- [ ] **Step 5: Commit**

  ```bash
  git add apps/obsidian-plugin/src/plugin.ts apps/obsidian-plugin/src/plugin.test.ts
  git commit -m "feat: surface plugin composition read failures"
  ```

### Task 3: Surface queue and snapshot drain failures

**Files:**
- Modify: `apps/obsidian-plugin/src/journal/automatic-snapshot.ts`
- Modify: `apps/obsidian-plugin/src/plugin.ts:681-703`
- Test: `apps/obsidian-plugin/src/journal/automatic-snapshot.test.ts`
- Test: `apps/obsidian-plugin/src/plugin.test.ts`

**Interfaces:**
- Consumes: Task 1 `JournalFailureReporter`.
- Produces: constructor options that accept the reporter and emit
  `queue_drain_failed` or `snapshot_drain_failed` once per rejected drain.

- [ ] **Step 1: Write failing rejection tests**

  ```ts
  runPass: async () => { throw new Error("sentinel"); }
  expect(tokens).toEqual(["queue_drain_failed"]);
  ```

  Repeat with `runSnapshot` rejecting and expect `snapshot_drain_failed`.

- [ ] **Step 2: Run RED test**

  Run: `pnpm --filter @workspace/obsidian-plugin test -- automatic-snapshot`

  Expected: FAIL because the drain rejection resolves without a token.

- [ ] **Step 3: Replace anonymous drain catches with typed reporting**

  Keep the settled promise contract. Each `catch` reports its static token and
  returns `undefined`; inject the composition-root reporter into both
  constructors.

- [ ] **Step 4: Run GREEN tests**

  Run: `pnpm --filter @workspace/obsidian-plugin test -- automatic-snapshot plugin`

  Expected: PASS; coalescing and `stop()` behavior are unchanged.

- [ ] **Step 5: Commit**

  ```bash
  git add apps/obsidian-plugin/src/journal/automatic-snapshot.ts apps/obsidian-plugin/src/journal/automatic-snapshot.test.ts apps/obsidian-plugin/src/plugin.ts apps/obsidian-plugin/src/plugin.test.ts
  git commit -m "feat: report journal coordinator drain failures"
  ```

### Task 4: Surface capture and reconcile persistence failures

**Files:**
- Modify: `apps/obsidian-plugin/src/journal/capture.ts:235-238,355-383`
- Modify: `apps/obsidian-plugin/src/journal/lifecycle-capture.ts:336,585,592`
- Modify: `apps/obsidian-plugin/src/plugin.ts` composition wiring
- Test: `apps/obsidian-plugin/src/journal/capture.test.ts`
- Test: `apps/obsidian-plugin/src/journal/lifecycle-capture.test.ts`

**Interfaces:**
- Consumes: Task 1 reporter.
- Produces: `settled_admission_failed`, one coalesced
  `automatic_snapshot_admission_failed` per scan, and
  `lifecycle_reconcile_persist_failed` on each failed reconcile persistence.

- [ ] **Step 1: Write failing behavioral tests**

  ```ts
  await capture.runAutomaticSnapshot();
  expect(summary.skippedFileCount).toBe(2);
  expect(tokens).toEqual(["automatic_snapshot_admission_failed"]);
  ```

  Add separate tests for a rejected settle admission and a rejected
  reconciliation write, asserting the original wait/reject outcomes and their
  exact tokens.

- [ ] **Step 2: Run RED tests**

  Run: `pnpm --filter @workspace/obsidian-plugin test -- capture lifecycle-capture`

  Expected: FAIL because errors are currently erased.

- [ ] **Step 3: Implement bounded reporters**

  Report before replacing a settled rejection. Track one boolean inside each
  automatic scan invocation so multiple item failures append one token. In
  lifecycle capture, report then preserve the current null/fail-closed result;
  do not claim reconciliation persisted.

- [ ] **Step 4: Run GREEN tests**

  Run: `pnpm --filter @workspace/obsidian-plugin test -- capture lifecycle-capture automatic-vault-convergence`

  Expected: PASS; skipped counts, waiters, lifecycle guards and rejection
  behavior are unchanged.

- [ ] **Step 5: Commit**

  ```bash
  git add apps/obsidian-plugin/src/journal/capture.ts apps/obsidian-plugin/src/journal/lifecycle-capture.ts apps/obsidian-plugin/src/plugin.ts apps/obsidian-plugin/src/journal/capture.test.ts apps/obsidian-plugin/src/journal/lifecycle-capture.test.ts
  git commit -m "feat: surface journal capture failure reasons"
  ```

### Task 5: Document and verify the complete surface

**Files:**
- Modify: `docs/operations/sync-error-tracing.md`
- Test: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.test.ts`
- Test: `apps/obsidian-plugin/src/journal/sync-diagnostics-export.test.ts`
- Create: `docs/handoff/2026-08-24-journal-orchestration-observability.md`

**Interfaces:**
- Consumes: all exact tokens from Tasks 1-4.
- Produces: operator guidance and a handoff with final commit SHA, gate
  evidence, decisions, deferred-item ruling and next actions.

- [ ] **Step 1: Write failing documentation/token-contract assertions**

  ```ts
  expect(runbookText).toContain("lifecycle_reconcile_persist_failed");
  expect(exportedTokens).toContain("automatic_snapshot_admission_failed");
  ```

- [ ] **Step 2: Run RED tests**

  Run: `pnpm --filter @workspace/obsidian-plugin test -- sync-diagnostics-trail sync-diagnostics-export`

  Expected: FAIL until the complete vocabulary is documented/exported.

- [ ] **Step 3: Document safe meanings and write handoff**

  State each token's safe operator meaning and the bounded emission behavior;
  record no deferred item unless one has a concrete contract trigger for
  `docs/handoff/BACKLOG.md`.

- [ ] **Step 4: Run final verification**

  Run: `uv run poe verify`

  Expected: PASS, including plugin tests, TypeScript checks, Python tests,
  artifact checks and builds.

- [ ] **Step 5: Commit**

  ```bash
  git add docs/operations/sync-error-tracing.md docs/handoff/2026-08-24-journal-orchestration-observability.md apps/obsidian-plugin/src/journal/sync-diagnostics-trail.test.ts apps/obsidian-plugin/src/journal/sync-diagnostics-export.test.ts
  git commit -m "docs: hand off journal diagnostics closure"
  ```
