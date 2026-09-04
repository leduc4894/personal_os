# Two-Vault Synchronization Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove two independent Obsidian Vault plugin clients converge through one canonical stack.

**Architecture:** A/B have separate Vault roots, credentials and journals. A redacted oracle checks canonical, A, B, cursor and diagnostic state. The real Obsidian two-Vault journey is a guarded operator gate.

**Tech Stack:** TypeScript strict, Vitest, WDIO, Python, pytest, PostgreSQL, R2.

**Spec:** `docs/superpowers/specs/2026-09-04-two-vault-synchronization-acceptance-design.md`

## Global Constraints

- No fake HTTP actor or direct API writer substitutes for either plugin actor.
- Live work uses `CI=true bash .local/serve-live-ci.sh up knowledge-ci-*` and approved bootstrap helpers.
- Evidence never has paths, content, digests, credentials, URLs, object keys, or request IDs.
- Bulk remains 100 small files/20 edits; existing capacity and 100 MiB refusals stay unchanged.

---

### Task 1: Isolated actors and convergence oracle

**Files:** Create `apps/obsidian-plugin/test/support/two-vault-live-actors.ts`, `apps/obsidian-plugin/test/support/two-vault-evidence.ts`; test `apps/obsidian-plugin/test/support/two-vault-live-actors.test.ts`.

- [ ] Write failing tests that A/B device/journal identities differ and oracle errors expose only safe aggregates.
- [ ] Run focused Vitest; expect RED.
- [ ] Implement actor methods through capture, lifecycle capture, queue driver, coordinator, remote applier, and reconciler.
- [ ] Run focused Vitest, type-check, lint; expect PASS.
- [ ] Commit `test: add isolated two-vault actor harness`.

### Task 2: Create/update propagation (TV-01–TV-02)

**Files:** Create `apps/obsidian-plugin/test/specs/two-vault-synchronization.e2e.ts`.

- [ ] Write failing A-create→B and converged A/B update cases with source-count/cursor/no-echo assertions.
- [ ] Run selected disposable E2E cases; expect RED.
- [ ] Implement bounded wait/phase reporting only; never bypass plugin capture.
- [ ] Run device-sync focused suite, type-check, lint, build, selected E2E; expect PASS.
- [ ] Commit `test: cover two-vault create update`.

### Task 3: Lifecycle propagation (TV-03–TV-07)

**Files:** Modify `apps/obsidian-plugin/test/specs/two-vault-synchronization.e2e.ts`; test `apps/obsidian-plugin/src/journal/lifecycle-capture.test.ts`.

- [ ] Write failing rename, move, delete, restore, and create-delete-before-pull cases.
- [ ] Run selected lifecycle cases; expect RED.
- [ ] Implement only lifecycle orchestration; defect fixes start with a focused RED regression.
- [ ] Run lifecycle/device-sync/type/lint/E2E gates; expect PASS.
- [ ] Commit `test: cover two-vault lifecycle propagation`.

### Task 4: Ordering/offline/journal recovery (TV-08–TV-13)

**Files:** Modify `apps/obsidian-plugin/test/specs/two-vault-synchronization.e2e.ts`.

- [ ] Write failing ordered lifecycle/content, duplicate replay, offline restart, missed watcher, and lost-journal cases.
- [ ] Run selected cases; expect RED.
- [ ] Add deterministic delivery/restart gates without changing queue semantics.
- [ ] Run journal/coordinator suites and E2E; expect PASS.
- [ ] Commit `test: cover two-vault replay recovery`.

### Task 5: Content conflicts (TV-14–TV-17)

**Files:** Modify `apps/obsidian-plugin/test/specs/two-vault-synchronization.e2e.ts`; test conflict capture/controller suites.

- [ ] Write failing Markdown/binary concurrent-edit and edit/delete/delete/edit cases.
- [ ] Run cases; expect RED and reject silent overwrite.
- [ ] Use existing conflict capture/Inbox paths; never resolve directly.
- [ ] Run conflict/API/plugin/E2E gates; expect PASS.
- [ ] Commit `test: cover two-vault content conflicts`.

### Task 6: Lifecycle conflicts/stale resolution (TV-18–TV-20)

**Files:** Modify `apps/obsidian-plugin/test/specs/two-vault-synchronization.e2e.ts`.

- [ ] Write failing rename-edit, competing-locator, and remote-advance-during-resolution cases.
- [ ] Run selected cases; expect RED.
- [ ] Assert no duplicate locator/source and successor conflict on stale resolution.
- [ ] Run lifecycle/conflict/E2E gates; expect PASS.
- [ ] Commit `test: cover two-vault lifecycle conflicts`.

### Task 7: Crash/suspension/cursor repair (TV-21–TV-25)

**Files:** Modify `apps/obsidian-plugin/test/specs/two-vault-synchronization.e2e.ts`.

- [ ] Write failing crash-after-commit/apply, suspension, cursor-gap, policy-advance cases.
- [ ] Run selected cases; expect RED.
- [ ] Add deterministic failure gates; preserve retry/repair contracts.
- [ ] Run device-sync/E2E gates; expect PASS.
- [ ] Commit `test: cover two-vault crash recovery`.

### Task 8: Bounded bulk/burst behavior (TV-26–TV-30)

**Files:** Create `apps/obsidian-plugin/src/device-sync/two-vault-synchronization.test.ts`; modify two-Vault E2E spec.

- [ ] Write failing 100-file copy/edit/delete/folder-move, 20-save burst, capacity/size refusal cases.
- [ ] Run focused suite; expect RED.
- [ ] Use normal watcher/capture paths; do not raise limits or parallelize production drain.
- [ ] Run focused/type/lint/integration gates; expect PASS.
- [ ] Commit `test: cover bounded two-vault bulk`.

### Task 9: Guarded acceptance, docs, handoff

**Files:** Modify `tools/obsidian_live_acceptance_bootstrap.py`, phase-status support, `docs/16-TESTING_AND_EVALUATION.md`, `docs/20-IMPLEMENTATION_PLAN.md`; create `docs/operations/two-vault-synchronization.md`.

- [ ] Write failing WDIO-allowlist and closed-token tests.
- [ ] Run focused tests; expect RED.
- [ ] Register exact spec; document CI bootstrap, two empty Vaults, safe evidence, cadence, recovery, teardown.
- [ ] Run diff check, device-sync, plugin gate and guarded Desktop journey; require operator evidence before pass claim.
- [ ] Commit docs; write one two-vault handoff and one BACKLOG row only for unavailable physical Mobile evidence.

## Plan self-review

- Tasks 1–8 map one-to-one to actor setup and TV-01–TV-30; Task 9 owns gates/docs/handoff.
- Every task has an independent RED/PASS/commit cycle.
