# R2 Zero-Byte Live Diagnostics Handoff

Date: 2026-08-24

## Status gate

Implementation is complete on branch `codex/r2-zero-byte-live-diagnostics`.
The final implementation commit is `37d1def`
(`fix: harden R2 zero-byte JUnit diagnostics`). It adds a closed diagnostic for
the protected zero-byte live case without changing R2 adapter or cleanup
semantics. The hosted protected R2 workflow remains undispatched because it
requires protected GitHub credentials and is an external side effect.

## Verification evidence

| Gate | Status | Evidence |
| --- | --- | --- |
| TDD RED | Passed | Classifier/stage/emission, artifact-retention, nested XML, workflow-option, real-pytest-JUnit-envelope, and invalid-reason regressions each failed before their minimal implementation. |
| Focused diagnostics + artifact contracts | Passed | `57 passed` for `test_live_junit_sanitization.py`, `test_zero_byte_live_diagnostics.py`, and `test_ci_security.py`. |
| Offline R2 adapter contract | Passed | `45 passed` for `tests/contract/object_storage/test_r2_adapter_contract.py`. |
| Full repository gate | Passed | Fresh sequential `uv run poe verify` completed on `37d1def`: formatting, lint, mypy strict, import/API contracts, Python tests, TypeScript tests, and all builds. The only prior failed attempt was diagnosed as two concurrent Next.js builds contending for its build lock; the sequential rerun passed the previously affected authentication-leakage test. |
| Diff check | Passed | `git diff --check` returned no output before the handoff-only commit. |
| Protected R2 zero-byte workflow | Pending external gate | The workflow now writes failed-case captured stdout into raw JUnit and sanitizes it to the exact closed record, but no hosted run has supplied the provider stage/reason evidence yet. |

## Decisions and rulings

- Worked on the requested branch without a linked worktree. This follows the
  explicit user direction; the cost if wrong is checkout contention, mitigated
  by the dedicated branch and the serial final verification.
- The sanitizer accepts exactly one line-delimited JSON record only inside the
  pytest captured-output envelope for the failed `test_zero_byte_round_trip`
  case. It canonicalizes that record and removes every other artifact stream
  or failure detail. The cost if pytest changes this envelope is fail-closed
  diagnostic removal until the contract test is updated.
- The hosted protected workflow was not dispatched locally. It needs protected
  credentials and remains the evidence source for the actual provider boundary
  classification; dispatching it without explicit authorization would be an
  external CI side effect.

## Deferred item

No new backlog row was added. The existing `object-storage` hosted-R2 row in
[`BACKLOG.md`](BACKLOG.md) is the single index entry for this same outstanding
protected-run evidence; its delivery gate remains **Before Child 7 and
production activation**.

## Next action

Dispatch `.github/workflows/object-storage-live.yml` from a protected GitHub
context, then inspect the sanitized JUnit artifact for exactly one
`r2_live_zero_byte_failed` record and use its `stage`/`reason` to decide
whether a separate adapter/provider remediation spec is needed.
