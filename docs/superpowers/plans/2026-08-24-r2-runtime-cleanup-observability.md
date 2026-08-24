# R2 Runtime Cleanup Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface R2 runtime-check spool-cleanup and client-teardown failures through closed, privacy-safe reason tokens.

**Architecture:** `SpoolManager` returns counts and one closed cleanup reason. The runtime-check maps that summary—or an injected janitor failure—to a reasoned cleanup event, and emits a separate teardown event after the HeadBucket outcome without changing that outcome or exit code.

**Tech Stack:** Python 3.14, asyncio, dataclasses, project diagnostics, pytest, Ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-08-24-r2-runtime-cleanup-observability-design.md`

## Global Constraints

- Use only Python 3.14 through `uv`; `except A, B:` is valid syntax.
- No new dependency, provider fallback/retry, R2 list/delete call, public route, or core `ErrorCode`.
- Reasons are exact `SafeToken` constants; never derive them from exceptions, errno, paths, spool names, endpoints, buckets, digests, or bytes.
- Janitor/close degradation never skips HeadBucket or changes its documented exit code; `CancelledError` propagates.
- Write RED tests first and prove captured diagnostics omit sensitive sentinels.

## File Structure

- `packages/r2-object-storage/src/r2_object_storage/spool.py`: cleanup vocabulary and summary.
- `packages/r2-object-storage/src/r2_object_storage/runtime_check.py`: event routing and close handling.
- `src/personal_os/diagnostics/events.py`: closed event schemas.
- `tests/unit/object_storage/`, `tests/contract/object_storage/`: behavior, ordering, cancellation and leakage.
- `docs/operations/object-storage.md`: operator contract.

---

### Task 1: Preserve spool-cleanup failure evidence

**Files:**

- Modify: `packages/r2-object-storage/src/r2_object_storage/spool.py:89-95,348-376`
- Test: `tests/unit/object_storage/test_spool_manager.py`

**Interfaces:** Produces `SPOOL_CLEANUP_DEFERRED`, `SPOOL_CLEANUP_SCAN_FAILED`, and `SPOOL_CLEANUP_ENTRY_FAILED` (`SafeToken`); extends `SpoolCleanupSummary` with `failed_count: int` and `reason: SafeToken | None`.

- [ ] **Step 1: Write failing summary tests.** Patch `os.scandir` to raise `OSError`, then assert `summary.reason == SPOOL_CLEANUP_SCAN_FAILED`, zero inventory counts, and zero failed count. Create a grammar-matching stale entry, make `lstat` and then `unlink` raise `OSError`, and assert `SPOOL_CLEANUP_ENTRY_FAILED` plus `failed_count == 1`.

- [ ] **Step 2: Run RED tests.** Run `uv run pytest tests/unit/object_storage/test_spool_manager.py -q`; expect failure because current summaries have neither field and convert scan/entry errors into clean or skipped-only results.

- [ ] **Step 3: Implement the minimal immutable contract.** Add the two fields and validate non-negative counts and allowed combinations. Return scan failure directly; count entry failures separately; choose `entry_failed` over `deferred` if both occur; leave non-stale/non-regular skips as non-failures.

```python
@dataclass(frozen=True, slots=True)
class SpoolCleanupSummary:
    examined_count: int
    removed_count: int
    skipped_count: int
    deferred_count: int
    failed_count: int = 0
    reason: SafeToken | None = None
```

- [ ] **Step 4: Run GREEN tests.** Run `uv run pytest tests/unit/object_storage/test_spool_manager.py -q`; expect all existing cleanup-bound and Windows-safe cases to pass.

- [ ] **Step 5: Commit.** Commit only the spool implementation and its test with message `feat: retain closed spool cleanup failure reasons`.

### Task 2: Emit reasoned janitor and client-close diagnostics

**Files:**

- Modify: `src/personal_os/diagnostics/events.py:289-300`
- Modify: `packages/r2-object-storage/src/r2_object_storage/runtime_check.py:85-107,276-341`
- Modify: `tests/unit/object_storage/test_error_diagnostics_contract.py`
- Modify: `tests/unit/object_storage/test_runtime_check.py`
- Modify: `tests/contract/object_storage/test_r2_runtime_contract.py`

**Interfaces:** Consumes Task 1's summary. Requires cleanup event fields `{operation, count, reason}`. Produces `object_storage_client_close_degraded` with fixed `operation=object_storage_client_close`, `reason=object_storage_client_close_failed`, `error_code=internal_error`, `error_category=internal`, and `is_retryable=false`.

- [ ] **Step 1: Write failing registry and runtime tests.** Assert the cleanup event requires `reason`; assert a close failure after a successful probe returns `0` and emits success then close-degraded events; assert the close reason is fixed and the injected exception sentinel is absent. Add equivalent cases for summary scan/entry/deferred reasons, injected janitor failure (`spool_cleanup_janitor_failed`), unavailable probe plus close failure, and cancellation propagation.

```python
assert [event["event"] for event in events] == [
    "object_storage_operation_succeeded",
    "object_storage_client_close_degraded",
]
assert events[-1]["reason"] == "object_storage_client_close_failed"
assert "sentinel-close" not in captured_output
```

- [ ] **Step 2: Run RED tests.** Run `uv run pytest tests/unit/object_storage/test_runtime_check.py tests/contract/object_storage/test_r2_runtime_contract.py tests/unit/object_storage/test_error_diagnostics_contract.py -q`; expect failure because cleanup has no reason and `close()` is suppressed.

- [ ] **Step 3: Register and route only closed fields.** Add `reason` to the cleanup event schema and the new close event. Emit the summary's reason or the fixed janitor-failed token. Replace `contextlib.suppress(Exception)` around close with a `try/except`: re-raise cancellation; otherwise emit the fixed close event and retain the already-determined exit code. Never use `str(error)` or its class name.

```python
logger.emit(
    EventName.OBJECT_STORAGE_SPOOL_CLEANUP_DEGRADED,
    {"operation": _JANITOR_OPERATION_TOKEN, "count": deferred_count, "reason": reason},
)
```

- [ ] **Step 4: Run GREEN tests and static checks.** Run `uv run pytest tests/unit/object_storage/test_runtime_check.py tests/contract/object_storage/test_r2_runtime_contract.py tests/unit/object_storage/test_error_diagnostics_contract.py -q`, `uv run ruff check packages/r2-object-storage/src/r2_object_storage src/personal_os/diagnostics tests/unit/object_storage tests/contract/object_storage`, and `uv run mypy packages/r2-object-storage/src/r2_object_storage src/personal_os/diagnostics`; expect all to exit 0.

- [ ] **Step 5: Commit.** Commit implementation, registry, and tests with message `feat: surface r2 runtime cleanup degradation reasons`.

### Task 3: Document and close the implementation

**Files:**

- Modify: `docs/operations/object-storage.md:38-60`
- Test: closest existing object-storage documentation/contract test

**Interfaces:** Documents the closed events produced by Tasks 1–2; creates no code interface.

- [ ] **Step 1: Write a failing documentation assertion.** Assert the operations guide contains `spool_cleanup_scan_failed` and `object_storage_client_close_degraded`, using a close existing contract test.

- [ ] **Step 2: Run RED test.** Run `uv run pytest tests/contract/object_storage -q`; expect failure until the guide names those diagnostics.

- [ ] **Step 3: Update safe operator guidance.** Explain safe cleanup counts plus closed reason, then separate client-close degradation and unchanged HeadBucket result/exit semantics. Do not add paths, endpoint/bucket names, exception text, or credentials.

- [ ] **Step 4: Run complete verification.** Run `uv run pytest tests/unit/object_storage tests/contract/object_storage -q`, `uv run poe format-check`, `uv run poe lint`, `uv run poe python-type-check`, `uv run poe boundary-check`, and `git diff --check`; expect exit 0. Run `uv run poe verify` before final handoff if Node/pnpm prerequisites are available; otherwise report the external prerequisite exactly.

- [ ] **Step 5: Commit and hand off.** Commit docs and test with message `docs: explain r2 cleanup degradation diagnostics`. Create exactly one `docs/handoff/YYYY-MM-DD-r2-runtime-cleanup-observability.md`, including final SHA, each gate/evidence, the one-event precedence rule, and only concrete deferred `BACKLOG.md` items.
