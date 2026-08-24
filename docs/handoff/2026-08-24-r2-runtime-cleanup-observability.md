# R2 Runtime Cleanup Observability Handoff

Date: 2026-08-24

## Status gate

Task 3 documentation closure is implemented. The evidence commit for the
documentation and contract test is
[`be89fd7`](../../commit/be89fd7fdf2f6076e89da737a36a485a77c46692) (`docs:
explain r2 cleanup degradation diagnostics`). This handoff is committed
separately after that implementation commit.

## Changes

- `docs/operations/object-storage.md` names the closed cleanup-scan reason
  `spool_cleanup_scan_failed`, the entry reason `spool_cleanup_entry_failed`,
  and the separate client-close degradation event and reason.
- The guide records safe cleanup counts, unchanged HeadBucket outcome/exit
  semantics, and the precedence rule: the probe outcome determines command
  status; cleanup and client-close degradation are additional safe events.
- `tests/contract/object_storage/test_r2_runtime_contract.py` asserts the two
  required guide diagnostic tokens.

## Gate evidence

| Gate | Result | Evidence |
| --- | --- | --- |
| TDD RED | Passed (expected failure) | The new contract assertion failed because `spool_cleanup_scan_failed` was absent. |
| TDD GREEN | Passed | The focused assertion passed: `1 passed in 0.50s`. |
| Focused object-storage tests | Passed | `uv run pytest tests/unit/object_storage tests/contract/object_storage -q`: `266 passed, 3 skipped in 49.78s`. |
| Format check | Blocked by pre-existing scope | `uv run poe format-check` reports three unrelated unformatted files: `packages/r2-object-storage/src/r2_object_storage/spool.py`, `tests/unit/object_storage/test_spool_manager.py`, and (before formatting) the touched contract test. The touched test was formatted; the two unrelated files were not changed. |
| Lint | Passed | `uv run poe lint`: Ruff and all pnpm lint workspaces passed. |
| Python type check | Passed | `uv run poe python-type-check`: mypy success on 182 source files. |
| Boundary check | Passed | `uv run poe boundary-check`: 5 import contracts kept, 10 architecture tests passed, API artifacts current, generated client check completed. |
| Diff check | Passed | `git diff --check` exited 0 before the implementation commit. |
| Full verify | Blocked by same format prerequisite | `uv run poe verify` stopped at `format-check` for the same two unrelated files. |

## Deferred items

None. No `BACKLOG.md` row was added.

## Next actions

Reformat the two pre-existing files in their owning implementation change,
then rerun `uv run poe format-check` and `uv run poe verify`.

