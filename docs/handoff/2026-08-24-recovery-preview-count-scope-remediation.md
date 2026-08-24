# Recovery Preview Count Scope Remediation Planning Handoff

Date: 2026-08-24

## Status gate

Planning is complete; implementation has not started. The planning baseline is
commit `d6af511` (`docs: record ci action admission evidence`). No production,
test, schema, workflow, or configuration file was changed while creating this
plan.

## Decision

The remediation changes only the protected live test oracle. Its seeded
workspace already creates a signed policy preview, so the existing restore
drill can prove that preview state appears in the complete source-store map
but is deliberately absent from the current recovery count contract. The
plan adds no manifest, lock-order, migration, or recovery-service change.

## Planned gates

| Gate | Status | Evidence required during execution |
| --- | --- | --- |
| TDD RED | Not run | Existing all-table comparison fails when the seeded `policy_previews` row is present. |
| Focused live restore GREEN | Not run | The edited drill passes against a disposable `knowledge-ci-*` stack and dedicated R2 test bucket. |
| Offline recovery compatibility | Not run | `tests/integration/canonical_core/test_recovery_integration.py` and `tests/unit/recovery` pass. |
| Static/diff checks | Not run | Ruff format/check, mypy, and `git diff --check` pass. |
| Protected canonical-core acceptance | Not run | `.github/workflows/canonical-core-acceptance.yml` is green on the implementation SHA. |

## Deferred items

None. No `BACKLOG.md` row was added.

## Next actions

Execute `docs/superpowers/plans/2026-08-24-recovery-preview-count-scope-remediation.md` with the mandatory live-test prerequisite sequence in `.local/RESTART.md`.
