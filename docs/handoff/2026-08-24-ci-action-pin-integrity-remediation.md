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
- Complete gate (pre-fix): `uv run poe verify` — **exit 1** at the first `format-check` subtask because Ruff reported `tests/contract/test_setup_node_pin_integrity.py` would be reformatted.
- Complete gate (final): after running `uv run ruff format tests/contract/test_setup_node_pin_integrity.py`, `uv run poe verify` — **exit 0**; all format, lint, type-check, boundary, test, and build subtasks passed.
- Diff hygiene: `git diff --check` — **exit 0**; `git status --short` was clean before Task 3 documentation; the restricted workflow/test diff was empty relative to `25dac2e`.
- Assertion rendering review: the historical typo assertion renders only the relative path, line number, and literal action reference; no environment, credential, secret, or token values are included.

## Exact implementation edits

Task 2 changed exactly one SHA character in each named workflow, from `actions/setup-node@820762786026740c76336085b0efc47a31fe5020` to `actions/setup-node@820762786026740c76f36085b0efc47a31fe5020`:

1. `.github/workflows/quality.yml:92` (one reference).
2. `.github/workflows/authentication-acceptance.yml:45` (one reference).
3. `.github/workflows/exclusion-policy-acceptance.yml:54` (one reference).

Comments, installation behavior, toolchain settings, permissions, triggers, caches, and surrounding workflow content were retained.

## GitHub admission observation

**OBSERVED — admission gate satisfied.** `master` commit `4db17bbce6b71900868e3ab768bb87598705a719` was pushed on 2026-08-24. GitHub recorded successful `Setup Node` steps after checkout in each affected workflow, with no action-resolution failure for the historical typo reference:

1. [quality run 32729704466](https://github.com/leduc4894/personal_os/actions/runs/32729704466): Ubuntu quality and Windows portability both completed `Setup Node` successfully.
2. [authentication acceptance run 32729704340](https://github.com/leduc4894/personal_os/actions/runs/32729704340): both browser-journey and stack jobs completed `Setup Node` successfully.
3. [exclusion policy acceptance run 32729704448](https://github.com/leduc4894/personal_os/actions/runs/32729704448): the acceptance-stack job completed `Setup Node` successfully.

The protected-run admission criterion is closed. This evidence covers action admission only; the longer acceptance jobs continue under their own workflow conclusions.
