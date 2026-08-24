# Recovery Preview Count Scope Remediation Implementation Handoff

Date: 2026-08-24

## Status gate

Implementation is complete through `3e5fa12` (`test: correct canonical core
fixture typing`). The protected R2 live acceptance passed on this code tree in
GitHub Actions run [32734042167](https://github.com/leduc4894/personal_os/actions/runs/32734042167):
the live suite, exact-key cleanup, and sanitized-JUnit upload all completed
successfully. No production, schema, workflow, or dependency change was made.

## Decision

The remediation changes only the protected live test oracle. Its seeded
workspace already creates a signed policy preview, so the existing restore
drill can prove that preview state appears in the complete source-store map
but is deliberately absent from the current recovery count contract. The
plan adds no manifest, lock-order, migration, or recovery-service change.

## Verification gates

| Gate | Status | Evidence |
| --- | --- | --- |
| TDD RED | Historical root-cause evidence | The original all-table oracle failure is recorded by the remediation design; the local red rerun stopped at missing local CI-injected R2 configuration, so no intentional failing shared-branch workflow was dispatched. |
| Focused live restore GREEN | Passed in protected suite | GitHub Actions run 32733357145 injected the dedicated configuration and completed the canonical-core live suite. |
| Offline recovery compatibility | Passed | 105 passed, 4 skipped, 8 deselected. |
| Ruff format/check | Passed | Fresh format check and lint both exit 0. |
| Mypy | Passed | `uv run mypy tests/integration/canonical_core/test_live_r2_acceptance.py` reports no issues after the narrow fixture type corrections. |
| Diff check | Passed | `git diff --check` exits 0. |
| Protected canonical-core acceptance | Passed | GitHub Actions run 32734042167, `Ubuntu phase-one acceptance live gate`, completed in 2m40s, including exact-key cleanup and sanitized JUnit upload. |

## Deferred items

None. No `BACKLOG.md` row was added.

## Next actions

No further action is required for this remediation. The local workstation
stack was restored to `ready` after the disposable `knowledge-ci-*` project
was reset.
