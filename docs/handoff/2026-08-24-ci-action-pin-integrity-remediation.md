# CI Action Pin Integrity Remediation Handoff

Date: 2026-08-24  
Domain: CI workflow security and admission  
Final implementation commit: `25dac2e` (`fix: correct setup-node action pins`)  
Task 3 documentation commit: recorded by the commit containing this handoff

## Gate status

- Task 1 RED evidence is preserved in `.superpowers/sdd/2026-08-24-ci-action-pin-integrity-remediation/task-1-report.md`: the focused contract was `1 failed, 1 passed` against the three historical typo references.
- Focused contract: `uv run pytest tests/contract/test_setup_node_pin_integrity.py -q` — **2 passed**.
- Adjacent contract: `uv run pytest tests/contract/test_setup_node_pin_integrity.py tests/contract/test_ci_security.py -q` — **46 passed** (Task 2 evidence).
- Lint: `uv run poe lint` — **exit 0**; Ruff and all workspace ESLint checks passed.
- Type check: `uv run poe type-check` — **exit 0**; mypy reported no issues in 182 files and all workspace TypeScript checks passed.
- Boundary check: `uv run poe boundary-check` — **exit 0**; 5 import contracts kept, architecture boundary tests passed, API artifacts current, and generated client check passed.
- Full tests: `uv run poe test` — **exit 0**; Python `3453 passed, 21 skipped, 398 deselected, 1 warning`; frontend `1 + 138 + 707` tests passed.
- Build: `uv run poe build` — **exit 0**; all Python distributions and workspace builds completed.
- Complete gate: `uv run poe verify` — **exit 1** at the first `format-check` subtask because Ruff reports `tests/contract/test_setup_node_pin_integrity.py` would be reformatted. No implementation file was changed in Task 3; this remains an open local hygiene concern for the implementation branch.
- Diff hygiene: `git diff --check` — **exit 0**; `git status --short` was clean before Task 3 documentation; the restricted workflow/test diff was empty relative to `25dac2e`.
- Assertion rendering review: the historical typo assertion renders only the relative path, line number, and literal action reference; no environment, credential, secret, or token values are included.

## Exact implementation edits

Task 2 changed exactly one SHA character in each named workflow, from `actions/setup-node@820762786026740c76336085b0efc47a31fe5020` to `actions/setup-node@820762786026740c76f36085b0efc47a31fe5020`:

1. `.github/workflows/quality.yml` (two references).
2. `.github/workflows/authentication-acceptance.yml` (one reference).
3. `.github/workflows/exclusion-policy-acceptance.yml` (one reference).

Comments, installation behavior, toolchain settings, permissions, triggers, caches, and surrounding workflow content were retained.

## GitHub admission observation

**OPEN EXTERNAL GATE — not observed.** This branch was not pushed and no pull request or protected-master run was submitted, per task authority. Therefore the required admission criterion remains unverified: the `Setup Node` step must start in `quality.yml`, `authentication-acceptance.yml`, and `exclusion-policy-acceptance.yml`, with no pre-checkout/action-resolution failure for the historical typo reference. This handoff makes no GitHub-side pass claim.

## Deferred item and next action

The normal review path must observe all three protected workflow runs before production activation. Remove the matching row from `docs/handoff/BACKLOG.md` only after each run shows `Setup Node` admitted and started.

