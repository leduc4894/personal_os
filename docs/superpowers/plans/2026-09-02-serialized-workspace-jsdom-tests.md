# Serialized Workspace jsdom Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `poe verify` deterministic on busy machines by serializing only the pnpm workspace test fan-out that runs the Web and Obsidian jsdom suites.

**Architecture:** The root script is the sole orchestration seam. It invokes the unchanged package-level Vitest commands with `--workspace-concurrency=1`, so package suites do not contend with each other while their internal test-worker behavior remains untouched. CI inherits this through the existing `uv run poe verify` command.

**Tech Stack:** pnpm 10.34.0 workspaces, Vitest 4.1.10, jsdom 30.0.1, Poe, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-02-serialized-workspace-jsdom-tests-spec.md`

## Global Constraints

- Do not increase Vitest/jsdom test timeouts or weaken assertions.
- Do not alter package-local `test` scripts, Vitest configs, production code,
  dependencies, pnpm lockfile, or CI workflow commands.
- The root test command must work with no `npm_config_workspace_concurrency`
  environment setting.
- Delete the one BACKLOG row only after repeat evidence shows the repository
  command is stable.

---

### Task 1: Pin sequential workspace test orchestration

**Files:**
- Modify: `package.json:11-18`
- Modify: `tests/contract/test_quality_orchestration.py`

**Interfaces:**
- Consumes: root `scripts.test` and package `test` scripts.
- Produces: root test command
  `pnpm --workspace-concurrency=1 --recursive --if-present run test`.

- [ ] **Step 1: Write the failing orchestration assertion**

  Add an assertion to the root-quality contract that parses `package.json` and
  pins the exact command:

  ```python
  assert package_json["scripts"]["test"] == (
      "pnpm --workspace-concurrency=1 --recursive --if-present run test"
  )
  ```

  Keep existing assertions that `poe verify` reaches `typescript-test` through
  the root script rather than duplicating package commands in Poe.

- [ ] **Step 2: Run the contract test and verify RED**

  Run:

  ```powershell
  uv run pytest tests/contract/test_quality_orchestration.py -q
  ```

  Expected: FAIL because the current root script lacks
  `--workspace-concurrency=1`.

- [ ] **Step 3: Make the one-line orchestration change**

  Replace only `scripts.test` in root `package.json` with the exact command
  produced by the contract. Do not add an environment-variable prefix,
  `--parallel`, timeout flag, or package filter.

- [ ] **Step 4: Verify the command routes each suite once**

  Run:

  ```powershell
  uv run pytest tests/contract/test_quality_orchestration.py -q
  pnpm run test
  ```

  Expected: contract passes; output shows one completed Web suite and one
  completed Obsidian-plugin suite, with no ambient concurrency override.

- [ ] **Step 5: Commit the orchestration fix**

  ```powershell
  git add package.json tests/contract/test_quality_orchestration.py
  git commit -m "test: serialize workspace jsdom suites"
  ```

### Task 2: Prove repeat stability through the public quality gate

**Files:**
- Modify: `docs/handoff/BACKLOG.md`
- Create: `docs/handoff/2026-09-02-serialized-workspace-jsdom-tests.md`

**Interfaces:**
- Consumes: the Task 1 root command and existing Poe graph
  (`typescript-test` → `pnpm run test`).
- Produces: sanitized command evidence that no external concurrency setting is
  required and the jsdom deferred item is closed.

- [ ] **Step 1: Prove no ambient override is needed**

  In a fresh shell, ensure `npm_config_workspace_concurrency` is unset, then
  run:

  ```powershell
  Remove-Item Env:npm_config_workspace_concurrency -ErrorAction SilentlyContinue
  pnpm run test
  pnpm run test
  ```

  Expected: both full workspace test runs pass. Record only command outcome,
  package-suite counts and duration; do not copy test output containing
  fixture content.

- [ ] **Step 2: Run full repository verification**

  Run:

  ```powershell
  uv run poe verify
  git diff --check
  git status --short
  ```

  Expected: all checks pass. The TypeScript test step succeeds through the
  committed root command without an environment workaround.

- [ ] **Step 3: Close the deferred finding**

  Delete only the `2026-09-01 | web-infra (pre-existing) | poe verify's
  typescript-test step flakes ...` row from `docs/handoff/BACKLOG.md`. Write
  one handoff with final SHA, the two repeat runs, full-gate outcome, the
  scheduling-only decision, and any remaining unrelated warnings.

- [ ] **Step 4: Commit closure artifacts**

  ```powershell
  git add docs/handoff/BACKLOG.md docs/handoff/2026-09-02-serialized-workspace-jsdom-tests.md
  git commit -m "docs: close jsdom test flake backlog"
  ```
