# Canonical Core Readiness Remediation Handoff

## Status

The requested canonical-core remediation plan is complete on branch
`codex/canonical-core-readiness-remediation`. The final implementation commit
is `a4b79c7` (`style: format recovery subprocess changes`); the functional
commit range is `f1919eb..f4b88f6`.

The four completed `Before Child 5` canonical-core rows have been removed from
the living [backlog](BACKLOG.md): identity input validation, canonical-read
telemetry, recovery-value/manifest boundaries, and dump-process adapter
hardening. The broader Child 5 rows outside this plan remain indexed.

## Gate evidence

| Gate | Status | Evidence |
| --- | --- | --- |
| Focused canonical-core tests | Green | Task gates: 53 identity/source tests, 42 recovery tests, 32 dump-process tests; final fix gate: `uv run poe canonical-core-test` → 1,024 passed, 11 skipped. |
| Full Python suite | Green | `uv run pytest -q` → 3,088 passed, 21 skipped, 331 deselected (one pre-existing Starlette/httpx deprecation warning). |
| Python lint | Green | `uv run poe python-lint` → all checks passed. |
| Strict types | Green | `uv run poe python-type-check` → no issues in 161 source files. |
| Formatting | Green | `uv run poe format-check` → 408 Python files formatted; all recursive JavaScript format checks passed. |
| Architecture/API boundaries | Green | `uv run poe boundary-check` → five contracts kept, ten architecture tests passed, API artifact check current. |
| Review | Green | Per-task reviews approved Tasks 1–3; whole-branch review findings were fixed and the scoped final re-review approved all five findings. |

## Delivered behavior

- Identity bootstrap rejects non-string typed inputs with the existing typed
  error and field-specific closed reason; canonical-read FAILED telemetry is
  limited to reader-side failures, not consumer-body application errors.
- Snapshot tokens and captured child stdout are excluded from representations;
  non-object decoded manifests reject as `json_noncanonical` through the
  existing recovery bundle-invalid contract.
- Dump/restore drains child pipes concurrently, completes cleanup on caller
  cancellation, redacts captured output, rejects CR/LF passfile fields before
  spawn, preserves valid colon/backslash escaping, and retains existing error
  mappings.
- Regression tests use opaque assertions so protected test values do not reach
  pytest assertion introspection or JUnit output.

## Decisions

- The user explicitly requested a new branch without a worktree; execution
  stayed in the clean current checkout. Risk: concurrent local edits could
  contaminate the branch; task dispatches were serialized and the checkout was
  rechecked at every gate.
- The binding remediation design includes broader Child 5 work, but this plan
  explicitly covers only the canonical-core tasks. Those three task scopes
  were implemented; unrelated API-auth and small-file-sync backlog rows remain
  untouched. Risk: Child 5 is not fully unblocked until their separate work
  lands.
- CR/LF cannot be represented in libpq password-file fields without changing
  the credential. The adapter now refuses them before spawning the child,
  rather than using an incorrect escape sequence. Risk: callers with malformed
  line-break credentials now receive the existing fail-closed error path.

## Deferred work and next actions

No new deferred item was introduced by this plan. Continue Child 5 only after
the still-indexed API-auth and small-file-sync `Before Child 5` rows are
resolved; see [BACKLOG.md](BACKLOG.md) for their delivery gates.
