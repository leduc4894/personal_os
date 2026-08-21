# Obsidian Live Acceptance Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one fail-closed bootstrap entrypoint that guarantees active TOTP before policy publication and the live Obsidian WDIO journey.

**Architecture:** A typed Python orchestrator composes existing protected CLIs, the existing local helper scripts, and the real TOTP HTTP routes. External process output is suppressed, while an injected subprocess boundary and `httpx` transport keep ordering, fresh-state recovery, and non-leakage contract-testable.

**Tech Stack:** Python 3.14, httpx, pytest, existing repository CLIs and WDIO.

**Spec:** `docs/superpowers/specs/2026-08-21-obsidian-live-acceptance-bootstrap.md`

## Global Constraints

- Operate only with `CI=true` and an exact disposable `knowledge-ci-*` project.
- Never read, print, log, copy, or commit a secret, TOTP code, recovery code, cookie, or token.
- TOTP activation must use the approved HTTP enrollment and verification routes, never direct database mutation.
- Run `.local/e2e-totp-code.py` before policy publication and WDIO; bootstrap an absent active credential and rerun the helper.
- Use `.local/publish-policy-revision.py` and the existing focused WDIO command.
- Preserve the service-start order and launcher contracts in `.local/RESTART.md`.

---

### Task 1: Specify the bootstrap contract

**Files:**
- Create: `docs/superpowers/specs/2026-08-21-obsidian-live-acceptance-bootstrap.md`
- Create: `docs/superpowers/plans/2026-08-21-obsidian-live-acceptance-bootstrap.md`
- Create: `docs/superpowers/tasks/2026-08-21-obsidian-live-acceptance-bootstrap.md`

**Interfaces:**
- Consumes: the approved four guardrails and Task 12 live acceptance contract.
- Produces: closed behavior, failure, privacy, and verification requirements.

- [ ] Write and self-review the plan/spec/task artifacts for placeholders, ambiguity, and scope drift.
- [ ] Commit the artifacts with `docs: specify live acceptance bootstrap`.

### Task 2: Prove fresh TOTP activation precedes WDIO

**Files:**
- Create: `tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py`
- Create: `tools/obsidian_live_acceptance_bootstrap.py`

**Interfaces:**
- Consumes: protected identity/Web credential/policy CLIs, TOTP HTTP routes, local TOTP and policy helpers, focused WDIO command.
- Produces: `run_live_acceptance(config, executor, client_factory, output) -> int` and a CLI accepting `--project-name`.

- [ ] Write a contract test whose fresh helper preflight fails, whose HTTP verification activates the credential, and whose observed WDIO launch occurs only after the second helper succeeds.
- [ ] Run `uv run pytest tests/contract/tools/test_obsidian_live_acceptance_bootstrap.py -q` and confirm the expected import failure.
- [ ] Implement the smallest typed orchestration and closed status output that passes the fresh-state test.
- [ ] Add rerun, invalid-project, failed-postflight, and sensitive-output cases one failing behavior at a time.
- [ ] Run the focused contract tests after each implementation step.

### Task 3: Make the preflight rule durable

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/operations/source-locator-tombstone-lifecycle.md`

**Interfaces:**
- Consumes: the executable bootstrap contract from Task 2.
- Produces: unambiguous agent and operator instructions.

- [ ] Document that `.local/e2e-totp-code.py` produces codes only after activation and that absence selects bootstrap, not BLOCKED/deferred status.
- [ ] Document the single bootstrap entrypoint without copying secret or launcher values.
- [ ] Run focused tests, Ruff, mypy strict, inspect `git diff`, and verify AGENTS.md and the operations guide line counts before committing.
- [ ] Commit with `feat: guard obsidian live acceptance bootstrap`.

