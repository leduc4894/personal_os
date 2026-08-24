# R2 Runtime Cleanup Observability Handoff

Date: 2026-08-24

## Status gate

Task 3 documentation closure is implemented. The evidence commit for the
documentation and contract test is
[`be89fd7`](../../commit/be89fd7fdf2f6076e89da737a36a485a77c46692) (`docs:
explain r2 cleanup degradation diagnostics`). The verified implementation/gate
tree SHA is
[`5d4de6a`](../../commit/5d4de6ad7babceac14ecf58b501af6a916b46a3b) (`docs:
correct r2 closure gate evidence`). That handoff-correction commit is
metadata-only: it changes only this handoff and does not alter the
implementation or verified gate outcomes. This record makes no claim about a
subsequent, unverified SHA.

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
| Format check | Passed after closure fix | `uv run poe format-check` passed after formatting the branch-owned `packages/r2-object-storage/src/r2_object_storage/spool.py` and `tests/unit/object_storage/test_spool_manager.py` (and the touched contract test). |
| Lint | Passed | `uv run poe lint`: Ruff and all pnpm lint workspaces passed. |
| Python type check | Passed | `uv run poe python-type-check`: mypy success on 182 source files. |
| Boundary check | Passed | `uv run poe boundary-check`: 5 import contracts kept, 10 architecture tests passed, API artifacts current, generated client check completed. |
| Diff check | Passed after closure fix | `git diff --check` passed on the final working tree after removing the handoff EOF blank line. The historical `730ed2a..adba69a` range correctly exposed the defect in the prior handoff commit. |
| Full verify | Passed after closure fix | `uv run poe verify` passed after the branch-owned formatting fixes. |
| Final-review TDD RED | Passed (expected failure) | `uv run pytest tests/unit/object_storage/test_spool_manager.py -q` produced 10 intended failures for fractional and boolean counts before the type guard. |
| Final-review TDD GREEN | Passed | The same focused file passed with `44 passed, 3 skipped in 2.12s` after the exact-integer guard. |
| Final-review object-storage unit suite | Passed | `uv run pytest tests/unit/object_storage -q`: `194 passed, 3 skipped in 2.20s`. |
| Final-review targeted static checks | Passed | `uv run ruff format --check ...`: 2 files already formatted; `uv run ruff check ...`: all checks passed; `uv run mypy packages/r2-object-storage/src`: success on 8 source files. |

## Deferred items

None. No `BACKLOG.md` row was added.

## Next actions

No further actions are required for this closure.
