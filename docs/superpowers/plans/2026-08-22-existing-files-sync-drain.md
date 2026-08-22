# Existing-files Sync Drain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one confirmed `Sync existing files` operation scan and commit newly admitted notes without a separate Sync now command.

**Architecture:** Keep capture and queue-driver boundaries intact. The plugin composition layer schedules a bounded post-scan drain and retries only the driver's closed `pass_already_running` outcome. WDIO is launched once through the guarded bootstrap and leaves a closed phase result for verification.

**Tech Stack:** TypeScript, Vitest, WebdriverIO/Obsidian, Python bootstrap, PostgreSQL live evidence.

**Spec:** `docs/superpowers/specs/2026-08-22-existing-files-sync-drain-design.md`

## Global Constraints

- Use only `knowledge-ci-*` disposable live stacks.
- Do not print or persist secrets, TOTP values, paths, content, cookies or tokens.
- Preserve journal terminal policy events as audit evidence.
- A WDIO pass is required before completion.

### Task 1: Protect post-scan queue scheduling

**Files:**
- Modify: `apps/obsidian-plugin/src/plugin.ts`
- Test: `apps/obsidian-plugin/src/plugin.test.ts`

- [ ] Write a failing source-level test proving completed scans route to the dedicated drain helper and that only the drain helper awaits a pass result.
- [ ] Run `pnpm --dir apps/obsidian-plugin exec vitest run src/plugin.test.ts` and observe failure.
- [ ] Implement `#drainExistingFilesScanQueue()` with 60 maximum 250 ms retries for `pass_already_running`; return after one real pass.
- [ ] Make `#runBoundedQueuePass()` return `QueuePassSummary` without changing status refresh semantics.
- [ ] Re-run the focused test, type-check and build.

### Task 2: Make live verdict durable

**Files:**
- Modify: `tools/obsidian_live_acceptance_bootstrap.py`
- Test: `tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py`

- [ ] Add a failing contract test for selecting the device-login WDIO spec and preserving its closed phase artifact when requested.
- [ ] Implement bounded `--wdio-spec` choices and `--keep-wdio-phase-status`.
- [ ] Run `uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q`.

### Task 3: Prove the policy-recovery journey live

**Files:**
- Modify: `apps/obsidian-plugin/test/specs/device-login-sync.e2e.ts`

- [ ] Start a clean `knowledge-ci-existing-files-sync` stack using `.local/RESTART.md` order.
- [ ] Run bootstrap once with `--wdio-spec test/specs/device-login-sync.e2e.ts --keep-wdio-phase-status`.
- [ ] Read the retained phase artifact and require `policy_recovery_journey_completed`.
- [ ] Record sanitized journal/server/status evidence in the handoff; if any earlier phase remains, fix that seam and repeat from a clean disposable stack.
