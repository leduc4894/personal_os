# Automatic Vault Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically converge eligible existing and newly created Obsidian notes through the durable journal and expose current per-note sync state locally.

**Architecture:** Preserve `JournalCapture`, `JournalRepository`, `JournalQueueDriver`, policy verification, lifecycle ordering, frozen fingerprints and idempotency boundaries. Add a coalescing snapshot coordinator in plugin composition; project local note state from the latest relevant journal row; replace manual-sync WDIO with automatic convergence evidence.

**Tech Stack:** TypeScript strict, Obsidian API, sql.js, Vitest, WebdriverIO, Python bootstrap and PostgreSQL canonical evidence.

**Spec:** `docs/superpowers/specs/2026-08-22-automatic-vault-convergence-design.md`

## Global Constraints

- PostgreSQL/R2 remain canonical; Qdrant/Neo4j remain rebuildable projections.
- Preserve terminal audit rows and idempotency identities. Never rewrite journal history.
- Verified policy is mandatory for admission; absent or invalid policy fails closed.
- Paths may render only in local Obsidian note-status UI, never logs, telemetry, HTTP payloads or WDIO artifacts.
- Never silently resolve conflicts, delete Vault bytes, bypass lifecycle guards or queue limits.
- Live evidence must use a clean `knowledge-ci-*` stack and pass Desktop WDIO.

## File structure

| File | Responsibility |
|---|---|
| `src/journal/automatic-snapshot.ts` | Coalesces snapshot triggers; no HTTP/UI logic. |
| `src/journal/capture.ts` | Deterministic snapshot admission and queued-event count. |
| `src/journal/note-status.ts` | Pure latest-note status projection. |
| `src/journal/repository.ts` | Local current-note status query. |
| `src/plugin.ts` | Composition, trigger lifecycle, command removal. |
| `src/device-authentication-setting-tab.ts` | Local-only note status list. |
| `test/specs/device-login-sync.e2e.ts` | Existing/new/policy-recovery live proof. |

## Task 1: Coalesce automatic snapshot triggers

**Files:**

- Create: `apps/obsidian-plugin/src/journal/automatic-snapshot.ts`
- Test: `apps/obsidian-plugin/src/journal/automatic-snapshot.test.ts`

**Interfaces:**

```ts
export type AutomaticSnapshotReason = "startup" | "policy_accepted" | "policy_revision_advanced";
export interface AutomaticSnapshotResult {
  readonly outcome: "completed" | "skipped" | "stopped";
  readonly queuedEventCount: number;
}
export interface AutomaticSnapshotRunner {
  runSnapshot(): Promise<AutomaticSnapshotResult>;
  requestQueuePass(): Promise<void>;
}
export class AutomaticSnapshotCoordinator {
  request(reason: AutomaticSnapshotReason): void;
  stop(): void;
}
```

- [ ] **Step 1: Write the failing test**

```ts
it("runs exactly one follow-up snapshot for triggers received during a running snapshot", async () => {
  const harness = createCoordinatorHarness();
  harness.runner.blockFirstSnapshot();
  harness.coordinator.request("startup");
  await harness.waitUntilFirstSnapshotStarted();
  harness.coordinator.request("policy_accepted");
  harness.coordinator.request("policy_revision_advanced");
  harness.runner.releaseFirstSnapshot({ outcome: "completed", queuedEventCount: 1 });
  await harness.waitForIdle();
  expect(harness.runner.snapshotCallCount).toBe(2);
  expect(harness.runner.queuePassCallCount).toBe(1);
});
```

- [ ] **Step 2: Verify RED**

Run: `pnpm exec vitest run src/journal/automatic-snapshot.test.ts`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the coordinator**

```ts
request(_reason: AutomaticSnapshotReason): void {
  if (this.#isStopped) return;
  this.#hasFollowUpSnapshot = true;
  if (!this.#isRunning) void this.#drain();
}
async #drain(): Promise<void> {
  this.#isRunning = true;
  try {
    while (!this.#isStopped && this.#hasFollowUpSnapshot) {
      this.#hasFollowUpSnapshot = false;
      const result = await this.#runner.runSnapshot();
      if (result.outcome === "completed" && result.queuedEventCount > 0) {
        await this.#runner.requestQueuePass();
      }
    }
  } finally { this.#isRunning = false; }
}
```

`stop()` must prevent a pending follow-up and queue request after unload.

- [ ] **Step 4: Verify GREEN**

Run: `pnpm exec vitest run src/journal/automatic-snapshot.test.ts && pnpm exec tsc --noEmit`

- [ ] **Step 5: Commit**

```bash
git add apps/obsidian-plugin/src/journal/automatic-snapshot.ts apps/obsidian-plugin/src/journal/automatic-snapshot.test.ts
git commit -m "feat: coordinate automatic vault snapshots"
```

## Task 2: Admit existing, changed and re-authorized files automatically

**Files:**

- Modify: `apps/obsidian-plugin/src/journal/capture.ts`
- Modify: `apps/obsidian-plugin/src/journal/contracts.ts`
- Test: `apps/obsidian-plugin/src/journal/capture.test.ts`

**Interfaces:**

```ts
export interface ExistingFilesScanSummary {
  readonly outcome: "completed" | "cancelled" | "stopped";
  readonly processedFileCount: number;
  readonly skippedFileCount: number;
  readonly queuedEventCount: number;
  readonly isTruncated: boolean;
}
async runAutomaticSnapshot(): Promise<ExistingFilesScanSummary>;
```

- [ ] **Step 1: Write failing admission tests**

```ts
it("creates an allowed queued successor after a prior policy block", async () => {
  await harness.captureUnderPolicy("notes/recovered.md", "excluded_policy");
  harness.allowMarkdown();
  const result = await harness.capture.runAutomaticSnapshot();
  expect(result.queuedEventCount).toBe(1);
  expect(harness.eventsFor("notes/recovered.md").map((event) => event.state)).toEqual([
    "excluded_policy", "queued",
  ]);
});
```

Also cover: new allowed note queues one create; changed committed note queues one update; unchanged committed note queues zero; currently excluded note creates terminal audit only; lifecycle-deferred path creates no content event.

- [ ] **Step 2: Verify RED**

Run: `pnpm exec vitest run src/journal/capture.test.ts`

Expected: FAIL because `runAutomaticSnapshot` and `queuedEventCount` do not exist.

- [ ] **Step 3: Implement shared snapshot capture**

Refactor the existing snapshot enumeration into one deterministic private method. `runAutomaticSnapshot()` invokes it without confirmation. Preserve sorting, current batch/file limits, path normalization, byte reads, lifecycle guards and fail-closed policy decisions. Count only recorded/coalesced `queued` rows as `queuedEventCount`; terminal policy/size rows remain audit evidence but do not dispatch.

- [ ] **Step 4: Verify GREEN**

Run: `pnpm exec vitest run src/journal/capture.test.ts && pnpm exec tsc --noEmit`

- [ ] **Step 5: Commit**

```bash
git add apps/obsidian-plugin/src/journal/capture.ts apps/obsidian-plugin/src/journal/contracts.ts apps/obsidian-plugin/src/journal/capture.test.ts
git commit -m "feat: admit eligible vault files automatically"
```

## Task 3: Add local current-note sync status

**Files:**

- Create: `apps/obsidian-plugin/src/journal/note-status.ts`
- Test: `apps/obsidian-plugin/src/journal/note-status.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/repository.ts`
- Test: `apps/obsidian-plugin/src/journal/repository.test.ts`
- Modify: `apps/obsidian-plugin/src/journal/status.ts`
- Test: `apps/obsidian-plugin/src/journal/status.test.ts`

**Interfaces:**

```ts
export type LocalNoteSyncState =
  | "synced" | "queued" | "syncing" | "retrying"
  | "policy_blocked" | "conflict" | "reconcile_required";
export interface LocalNoteSyncStatus {
  readonly normalizedPath: string;
  readonly state: LocalNoteSyncState;
  readonly policyRevisionNumber: number | null;
  readonly retryAtEpochMs: number | null;
  readonly reason: JournalSafeErrorLabel | null;
}
readLocalNoteSyncStatuses(): readonly LocalNoteSyncStatus[];
```

- [ ] **Step 1: Write failing projection/privacy tests**

```ts
it("shows synced rather than an older policy block after successor commit", async () => {
  await captureExcludedThenAllowedAndCommit(repository, "notes/current.md");
  expect(repository.readLocalNoteSyncStatuses()).toContainEqual(
    expect.objectContaining({ normalizedPath: "notes/current.md", state: "synced" }),
  );
  expect(projectJournalSyncStatus(projectInput({
    eventStateErrorCounts: repository.readEventStateErrorCounts(),
  })).kind).toBe("ready");
});
```

Add precedence tests for queued, preflight/uploading, waiting_retry, latest excluded event, conflict, and reconcile-required. Serialize every aggregate/telemetry value and assert it contains no local path.

- [ ] **Step 2: Verify RED**

Run: `pnpm exec vitest run src/journal/note-status.test.ts src/journal/repository.test.ts src/journal/status.test.ts`

Expected: FAIL because the local projection does not exist.

- [ ] **Step 3: Implement query and pure projection**

Query the newest event per `local_file_id`, joined to the current local path. Map only closed event states/reasons. Keep the method local to the plugin repository: do not add it to HTTP contracts. Reuse successor filtering in aggregate status so old terminal audit rows are never current blockers.

- [ ] **Step 4: Verify GREEN**

Run: `pnpm exec vitest run src/journal/note-status.test.ts src/journal/repository.test.ts src/journal/status.test.ts && pnpm exec tsc --noEmit`

- [ ] **Step 5: Commit**

```bash
git add apps/obsidian-plugin/src/journal/note-status.ts apps/obsidian-plugin/src/journal/note-status.test.ts apps/obsidian-plugin/src/journal/repository.ts apps/obsidian-plugin/src/journal/repository.test.ts apps/obsidian-plugin/src/journal/status.ts apps/obsidian-plugin/src/journal/status.test.ts
git commit -m "feat: project current local note sync status"
```

## Task 4: Compose convergence and remove manual sync commands

**Files:**

- Modify: `apps/obsidian-plugin/src/plugin.ts`
- Test: `apps/obsidian-plugin/src/plugin.test.ts`

- [ ] **Step 1: Write failing composition tests**

```ts
it("removes manual sync commands and requests startup convergence", () => {
  expect(pluginSource).not.toContain('id: "sync-existing-files"');
  expect(pluginSource).not.toContain('id: "sync-now"');
  expect(pluginSource).toContain('automaticSnapshotCoordinator.request("startup")');
  expect(pluginSource).toContain('automaticSnapshotCoordinator.stop()');
});
```

Add an assertion that authenticated policy acceptance requests `policy_accepted` only after `adoptOnboardingTrust()` returns.

- [ ] **Step 2: Verify RED**

Run: `pnpm exec vitest run src/plugin.test.ts`

Expected: FAIL because commands remain and the coordinator is absent.

- [ ] **Step 3: Implement composition**

Create the coordinator after journal recovery and capture/queue construction. Its runner calls `capture.runAutomaticSnapshot()` only when policy can capture and journal is safe; its dispatcher awaits the existing bounded queue wrapper. Request `startup` after listeners are installed and `policy_accepted` after successful onboarding trust. Remove `sync-existing-files`, confirmation modal and `sync-now`; retain `Restore selected tombstone`. Stop the coordinator before releasing journal resources.

- [ ] **Step 4: Verify GREEN**

Run: `pnpm exec vitest run src/plugin.test.ts src/journal/automatic-snapshot.test.ts && pnpm exec tsc --noEmit && pnpm run build`

- [ ] **Step 5: Commit**

```bash
git add apps/obsidian-plugin/src/plugin.ts apps/obsidian-plugin/src/plugin.test.ts apps/obsidian-plugin/src/journal/automatic-snapshot.ts apps/obsidian-plugin/src/journal/automatic-snapshot.test.ts
git commit -m "feat: converge vault notes automatically"
```

## Task 5: Render the local note-status list

**Files:**

- Modify: `apps/obsidian-plugin/src/device-authentication-setting-tab.ts`
- Test: `apps/obsidian-plugin/src/device-authentication-setting-tab.test.ts`
- Modify: `apps/obsidian-plugin/src/plugin.ts`

- [ ] **Step 1: Write failing rendering tests**

```ts
it("renders a path only in the local settings list", () => {
  const rendered = renderSettings({ localNoteSyncStatuses: [{
    normalizedPath: "notes/local-only.md", state: "policy_blocked",
    policyRevisionNumber: 12, retryAtEpochMs: null, reason: "excluded_policy",
  }] });
  expect(rendered).toContain("notes/local-only.md");
  expect(serializeTelemetry(rendered.snapshot)).not.toContain("notes/local-only.md");
});
```

Add tests for all seven states, empty list, deterministic sort, and policy block showing only revision plus closed reason.

- [ ] **Step 2: Verify RED**

Run: `pnpm exec vitest run src/device-authentication-setting-tab.test.ts`

Expected: FAIL because the settings snapshot/list does not exist.

- [ ] **Step 3: Implement local-only UI**

Add `localNoteSyncStatuses` to the settings snapshot and source it from `readLocalNoteSyncStatuses()`. Render `Sync status by note` after existing status cards using fixed labels. Do not change redacted status-bar output and do not introduce network calls.

- [ ] **Step 4: Verify GREEN**

Run: `pnpm exec vitest run src/device-authentication-setting-tab.test.ts src/plugin.test.ts && pnpm exec tsc --noEmit && pnpm run build`

- [ ] **Step 5: Commit**

```bash
git add apps/obsidian-plugin/src/device-authentication-setting-tab.ts apps/obsidian-plugin/src/device-authentication-setting-tab.test.ts apps/obsidian-plugin/src/plugin.ts
git commit -m "feat: show local note sync status"
```

## Task 6: Prove automatic convergence live

**Files:**

- Modify: `apps/obsidian-plugin/test/specs/device-login-sync.e2e.ts`
- Modify: `apps/obsidian-plugin/test/support/live-device-onboarding.ts`
- Modify: `apps/obsidian-plugin/test/support/live-acceptance-phase-status.ts`
- Modify: `tools/obsidian_live_acceptance_bootstrap.py`
- Test: `tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py`

- [ ] **Step 1: Write failing no-command/phase tests**

```ts
it("does not execute removed sync commands", () => {
  expect(specSource).not.toContain("sync-existing-files");
  expect(specSource).not.toContain("sync-now");
});
```

Add bootstrap contract coverage for retained phases `automatic_existing_note_committed`, `automatic_new_note_committed`, `automatic_policy_successor_committed`, and `automatic_convergence_journey_completed`.

- [ ] **Step 2: Verify RED**

Run: `pnpm exec vitest run src/authentication/live-device-onboarding-reuse.test.ts && uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q`

Expected: FAIL until the automatic journey and phase codes exist.

- [ ] **Step 3: Implement live journey**

Place one allowed fixture before plugin enablement and wait for automatic canonical commit. Create a second allowed fixture after startup and require its independent exact publication. Publish a Markdown block, create a third fixture and verify terminal audit evidence; publish allowing policy, reauthorize until that revision is cached, and require a committed successor without a command. Assert the list reports `synced` and aggregate text has no stale policy block. Artifacts contain only phase codes and numeric sanitized counts.

- [ ] **Step 4: Run offline gates**

Run: `pnpm exec vitest run && pnpm exec tsc --noEmit && pnpm run build && uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q`

Expected: PASS.

- [ ] **Step 5: Run clean Desktop gate**

```powershell
$env:CI = 'true'
uv run python tools/obsidian_live_acceptance_bootstrap.py --project-name knowledge-ci-automatic-convergence --wdio-spec test/specs/device-login-sync.e2e.ts --keep-wdio-phase-status
```

Expected: retained verdict `passed`, final phase `automatic_convergence_journey_completed`, and exactly one canonical publication per controlled content identity.

- [ ] **Step 6: Commit**

```bash
git add apps/obsidian-plugin/test/specs/device-login-sync.e2e.ts apps/obsidian-plugin/test/support/live-device-onboarding.ts apps/obsidian-plugin/test/support/live-acceptance-phase-status.ts tools/obsidian_live_acceptance_bootstrap.py tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py
git commit -m "test: prove automatic vault convergence live"
```

## Task 7: Update operational documentation

**Files:**

- Modify: `docs/operations/plugin-journal-small-file-sync.md`
- Modify: `docs/operations/source-locator-tombstone-lifecycle.md`
- Modify: `docs/handoff/2026-08-22-plugin-sync-status-recovery.md`

- [ ] **Step 1: Locate obsolete instructions**

Run: `rg -n "Sync existing files|Sync now" docs/operations/plugin-journal-small-file-sync.md docs/operations/source-locator-tombstone-lifecycle.md`

Expected: obsolete command references are found before edit.

- [ ] **Step 2: Update contracts**

Document automatic startup/policy convergence, seven note states, local-only path visibility, automatic retry guidance, and that Restore selected tombstone is the only remaining explicit lifecycle command. Record the actual WDIO phase and sanitized evidence in the existing handoff; do not create another handoff for this domain.

- [ ] **Step 3: Verify documentation**

Run: `rg -n "TO(DO)|TB(D)|PLACE(HOLDER)" docs/superpowers/specs/2026-08-22-automatic-vault-convergence-design.md docs/superpowers/plans/2026-08-22-automatic-vault-convergence.md; git diff --check; git status --short`

Expected: no placeholders, no whitespace errors, and only intended changes.

- [ ] **Step 4: Commit**

```bash
git add docs/operations/plugin-journal-small-file-sync.md docs/operations/source-locator-tombstone-lifecycle.md docs/handoff/2026-08-22-plugin-sync-status-recovery.md docs/superpowers/specs/2026-08-22-automatic-vault-convergence-design.md docs/superpowers/plans/2026-08-22-automatic-vault-convergence.md
git commit -m "docs: describe automatic vault convergence"
```

## Final verification

- [ ] `pnpm --dir apps/obsidian-plugin exec vitest run`
- [ ] `pnpm --dir apps/obsidian-plugin exec tsc --noEmit`
- [ ] `pnpm --dir apps/obsidian-plugin run build`
- [ ] `uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q`
- [ ] Clean Desktop WDIO finishes at `automatic_convergence_journey_completed` with a retained `passed` artifact.
