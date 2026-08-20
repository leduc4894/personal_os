# Source Publication Readiness Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all five source-publication gates required before Child 5.

**Architecture:** Bind existing diagnostics at the service composition boundary, retain retryable database failures in the dispatcher loop, and make stale-lease events reflect only committed fenced state. Adapter checks remain static and provider-free.

**Tech Stack:** Python 3.13, asyncio, SQLAlchemy async, Temporal adapter, pytest, mypy strict.

**Spec:** `docs/superpowers/specs/2026-08-20-child-five-readiness-remediation-design.md`

## Global Constraints

- No public API, error token, metric label, or migration change.
- Events and metrics contain only opaque IDs and registered safe tokens.
- Database and Temporal errors retain bounded retry and fail-closed behavior.

---

### Task 1: Bind source-publication telemetry and classify outcomes

**Files:**
- Modify: `src/personal_os/sources/publication.py`, `src/personal_os/sources/metrics.py`, source-publication composition root found with `rg -n "SourceVersionPublicationService" apps src tools`
- Test: `tests/unit/sources/test_publication_service.py`, `tests/unit/sources/test_source_metrics.py`, `tests/contract/source_publication/test_telemetry_leakage.py`

**Interfaces:**
- Consumes: `SourceVersionPublicationService.publish_create()` and `.publish_update()`.
- Produces: the registered `source_version_publish_*` diagnostics and non-rejection recording for retryable outcomes.

- [ ] **Step 1: Write the failing tests**

```python
with pytest.raises(SourcePublicationError):
    await service.publish_create(command=busy_command, stream=stream, diagnostic_context=context)
assert PublicationMetricOutcome.REJECTED not in [record.outcome for record in metrics.records]
assert emitted_event_names == [EventName.SOURCE_VERSION_PUBLISH_FAILED]
```

- [ ] **Step 2: Run the focused tests**

Run: `uv run pytest tests/unit/sources/test_publication_service.py tests/unit/sources/test_source_metrics.py tests/contract/source_publication/test_telemetry_leakage.py -q`

Expected: FAIL because retryable mapped errors are counted as `rejected` or events are not composed.

- [ ] **Step 3: Implement the minimal safe behavior**

```python
if error.code in REJECTION_REASON_BY_ERROR_CODE:
    self.metrics.record_rejection(operation=operation, reason=REJECTION_REASON_BY_ERROR_CODE[error.code])
else:
    self._emit_retryable_failure(operation=operation, error=error, duration_seconds=duration_seconds)
```

Bind the existing diagnostic sink and emit only the registered operation/outcome/error-code safe fields. Do not record a retryable failure under a successful or rejected publication metric outcome.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/sources/test_publication_service.py tests/unit/sources/test_source_metrics.py tests/contract/source_publication/test_telemetry_leakage.py -q; uv run mypy src/personal_os/sources`

Expected: PASS.

```powershell
git add src/personal_os/sources apps tests
git commit -m "fix: bind source publication telemetry"
```

### Task 2: Recover the dispatcher and commit diagnostics before emission

**Files:**
- Modify: `apps/worker/src/workflow_worker/projection_dispatch_runtime.py`, `packages/postgresql-source-store/src/postgresql_source_store/projection_intents.py`
- Test: `tests/unit/workflow_worker/test_projection_dispatch_runtime.py`, `tests/unit/sources/test_projection_dispatch.py`, `tests/integration/projection_dispatch/test_projection_intent_leases.py`

**Interfaces:**
- Consumes: `ProjectionDispatchRuntime.run_until_shutdown()` and fenced store transitions.
- Produces: bounded retryable DB loop recovery and post-commit stale-lease diagnostics.

- [ ] **Step 1: Write failing loop and transaction-order tests**

```python
await runtime.run_until_shutdown(shutdown)
assert store.dispatch_calls == 2
assert diagnostics.events[-1].name is EventName.PROJECTION_INTENT_DISPATCH_FAILED
assert transaction_committed_before(diagnostics.events[-1])
```

- [ ] **Step 2: Run focused tests**

Run: `uv run pytest tests/unit/workflow_worker/test_projection_dispatch_runtime.py tests/unit/sources/test_projection_dispatch.py tests/integration/projection_dispatch/test_projection_intent_leases.py -q`

Expected: FAIL because an unavailable database failure escapes or emits before commit.

- [ ] **Step 3: Implement the one retry branch**

```python
try:
    await self.dispatch_pending_intents_once()
except ProjectionDispatchError as error:
    if error.code is not ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE:
        raise
    await _wait_for_shutdown_or_delay(shutdown, _RETRY_DELAY_SECONDS)
```

Move stale-lease `emit()` after transaction-context exit and derive its reason from the guarded result.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/workflow_worker/test_projection_dispatch_runtime.py tests/unit/sources/test_projection_dispatch.py tests/integration/projection_dispatch/test_projection_intent_leases.py -q`

Expected: PASS.

```powershell
git add apps/worker packages/postgresql-source-store tests
git commit -m "fix: harden projection dispatch recovery"
```

### Task 3: Tighten source adapter structural contracts

**Files:**
- Modify: source adapter boundary test located with `rg -n "fastapi|aiohttp|boto3" tests/contract`; `tests/contract/source_publication/test_table_metadata.py`
- Test: same files

- [ ] **Step 1: Add failing exact scanner/value tests**

```python
assert {"fastapi", "aiohttp", "boto3"}.isdisjoint(scanned_imports)
assert expected_field_map == actual_field_map
```

- [ ] **Step 2: Run the contract suite**

Run: `uv run pytest tests/contract/source_publication -q`

Expected: FAIL until the scanner covers the complete forbidden families and mapping equality is exact.

- [ ] **Step 3: Make the minimal contract-only correction**

```python
assert expected_field_map.items() == actual_field_map.items()
```

Do not introduce a framework/provider import or alter adapter public imports.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/contract/source_publication -q; uv run mypy src/personal_os/sources packages/postgresql-source-store/src/postgresql_source_store`

Expected: PASS.

```powershell
git add packages/postgresql-source-store tests
git commit -m "test: strengthen source adapter contracts"
```
