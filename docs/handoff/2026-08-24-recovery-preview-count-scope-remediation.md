# Recovery Preview Count Scope Remediation Implementation Handoff

Date: 2026-08-24

## Status gate

Code/task review is complete at implementation commit `e197dbd` (`test:
scope live restore counts to canonical tables`). The protected R2 live gate is
**BLOCKED**, not deferred or passed: the dedicated endpoint, bucket, and
secret-root configuration required by the live harness is unavailable in this
workspace. No production, schema, workflow, or dependency change was made.

## Decision

The remediation changes only the protected live test oracle. Its seeded
workspace already creates a signed policy preview, so the existing restore
drill can prove that preview state appears in the complete source-store map
but is deliberately absent from the current recovery count contract. The
plan adds no manifest, lock-order, migration, or recovery-service change.

## Verification gates

| Gate | Status | Evidence |
| --- | --- | --- |
| TDD RED | Blocked before assertion | Focused live setup failed closed because `R2_TEST_ENDPOINT`, `R2_TEST_BUCKET_NAME`, and `R2_TEST_SECRET_ROOT` are unavailable. |
| Focused live restore GREEN | Blocked | Same dedicated R2 configuration gate; no mock/substitute was used. |
| Offline recovery compatibility | Passed | 105 passed, 4 skipped, 8 deselected. |
| Ruff format/check | Passed | Fresh format check and lint both exit 0. |
| Mypy | Failed baseline | 13 existing integration fixture/test typing errors remain; no unrelated fixes made. |
| Diff check | Passed | `git diff --check` exits 0. |
| Protected canonical-core acceptance | Blocked | Cannot dispatch/claim green without dedicated R2 configuration. |

## Deferred items

None. No `BACKLOG.md` row was added.

## Next actions

Next actions: provision/load the approved dedicated R2 endpoint, bucket, and
secret-root through the repository's live-test contract without exposing
values; rerun the focused live restore test and the protected
`canonical-core-acceptance.yml` workflow on `e197dbd`; retain sanitized
evidence and update this handoff only after the live gate is green.
