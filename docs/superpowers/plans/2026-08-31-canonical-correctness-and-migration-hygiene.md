# Canonical Correctness and Migration Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire seven small-defect BACKLOG rows — including the two committed-RED privacy/migration tests (`raw locator clear`, `gated-downgrade partial commit`) — and add the two device-sync scale indexes.

**Architecture:** Each row is an independent failing-test-first fix in its owning module: no cross-domain abstraction, no public-contract change, exactly one new Alembic revision (the two indexes). The downgrade fix hoists the small-file evidence gate into `20260820_01`'s downgrade via a shared `allow_destructive` helper in `migrations/database_migration_runtime.py`, so a refused downgrade never commits `20260820_01`'s drops first.

**Tech Stack:** Python 3.14 (mypy strict, ruff), SQLAlchemy Core, Alembic, pytest (unit + `local_stack` integration).

**Spec:** `docs/superpowers/specs/backlog/2026-08-31-canonical-correctness-and-migration-hygiene-design.md`

## Global Constraints

- No public contract, wire behavior or query-semantics change; the only schema change is Task 7's two indexes (one revision + upgrade/downgrade tests + `CANONICAL_POSTGRESQL_SCHEMA_REVISION` bump).
- C5 is the privacy fix itself: raw note paths must not outlive a terminal transition.
- Each fix lands in its owning module (repetition-over-abstraction precedents stand).
- Each BACKLOG row is removed in the diff that closes it.
- The two RED tests at `tests/integration/source_publication/test_small_file_operations.py` (`test_terminal_transition_clears_raw_locator_and_keeps_digest` L599; `test_gated_downgrade_drops_the_operation_table_and_reapplies_head` L1056) must be green in the same run that lands Tasks 5 and 6 — the 2026-08-30 documented-failure expectation dies with this plan.

---

### Task 1: Restore directory-scoped collection for `test_secret_files.py`

**Files:**
- Modify: `src/personal_os/diagnostics/logging.py:55` (move the `RuntimeSettings`/`ServiceName` import under `TYPE_CHECKING`) — primary fix
- Modify (fallback only): `tests/unit/runtime_configuration/test_secret_files.py` (priming import) — only if the import is NOT type-only
- Test: the collection run itself

**Interfaces:**
- Consumes: the verified cycle `secret_files.py:12` → `diagnostics/__init__.py:23` → `diagnostics/logging.py:54-55` → `runtime_configuration.models` → `runtime_configuration/__init__` (re-enters the partially initialized `secret_files`).

- [ ] **Step 1: Reproduce the collection failure**

Run: `uv run pytest tests/unit/runtime_configuration -q`
Expected: FAIL with an ImportError/AttributeError from the circular chain above.

- [ ] **Step 2: Classify the back-edge import**

Read `src/personal_os/diagnostics/logging.py` around L54-55. If `RuntimeSettings`/`ServiceName` appear only in annotations, apply the primary fix:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_os.runtime_configuration.models import RuntimeSettings, ServiceName
```

(and add `from __future__ import annotations` if absent). If the import is used at runtime, apply the fallback instead — a priming import at the top of `test_secret_files.py`, above the `runtime_configuration` import:

```python
from personal_os.error_contracts.exceptions import SecretFileError  # noqa: F401  (primes the diagnostics package; BREAKS the import cycle documented in BACKLOG 2026-08-14 §15)
```

- [ ] **Step 3: Verify**

Run: `uv run pytest tests/unit/runtime_configuration -q && uv run pytest tests/unit/error_contracts -q`
Expected: both exit 0 (directory-scoped collection restored; no regression).

- [ ] **Step 4: Commit + retire the row**

Remove `| 2026-08-14 | infra (pre-existing) | Circular import in tests/unit/runtime_configuration/test_secret_files.py...` from `docs/handoff/BACKLOG.md`.

```bash
git add src/personal_os/diagnostics/logging.py docs/handoff/BACKLOG.md
git commit -m "fix: break the diagnostics runtime-configuration import cycle"
```

---

### Task 2: `_validate_epoch_ms` distinguishes non-integer input

**Files:**
- Modify: `src/personal_os/small_file_sync/metrics.py:207-209`
- Test: `tests/unit/small_file_sync/test_contracts.py` (beside `test_rejection_ring_timestamps_reject_a_broken_epoch_clock` L783-791)

**Interfaces:**
- Produces: two distinguishable `ValueError` raises — non-integer input (closed token `epoch_ms_clock_non_integer`) vs negative integer (existing token). Scope: the small-file-sync copy only (the row's owner); the three sibling copies in other domains stay as-is per repetition-over-abstraction.

- [ ] **Step 1: Write the failing test**

```python
def test_rejection_ring_timestamps_distinguish_a_non_integer_clock() -> None:
    """A non-int clock value is its own closed reason, not the generic
    negative-integer message (BACKLOG 2026-08-23 §5)."""
    recorder = InMemorySmallFileSyncMetrics(epoch_ms_clock=lambda: "1_000")  # type: ignore[return-value]
    with pytest.raises(ValueError, match="epoch_ms_clock_non_integer"):
        recorder.record_rejection(...)  # the same call shape as the L783 test
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/small_file_sync/test_contracts.py -k non_integer_clock -q`
Expected: FAIL — the current single raise matches "non-negative integer" for both shapes.

- [ ] **Step 3: Implement**

```python
def _validate_epoch_ms(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("epoch_ms_clock_non_integer: the clock must return an int")
    if value < 0:
        raise ValueError("epoch_ms_clock must return a non-negative integer")
```

- [ ] **Step 4: Run + commit + retire the row**

Run: `uv run pytest tests/unit/small_file_sync -q` — PASS.

```bash
git add src/personal_os/small_file_sync/metrics.py tests/unit/small_file_sync/test_contracts.py docs/handoff/BACKLOG.md
git commit -m "fix: give the non-integer epoch clock its own closed reason"
```
Remove the `| 2026-08-23 | sync-error-tracing | _validate_epoch_ms masks a non-int clock value...` row.

---

### Task 3: Fresh commits record `COMMITTED`

**Files:**
- Modify: `src/personal_os/source_lifecycle/service.py:94-125` (`commit()`), plus a `_record_commit` helper beside `_record_replay` (L127-141)
- Test: `tests/unit/source_lifecycle/` (beside the existing metrics tests, e.g. `test_metrics.py` / the service test module that pins `replayed`)

**Interfaces:**
- Consumes: `SourceLifecycleMetrics.record_commit(*, operation, outcome, duration_seconds)` (metrics.py:67-73) — unchanged; `LifecycleMetricOutcome.COMMITTED` already exists.
- Produces: fresh successful commits record `COMMITTED`; exact replays keep recording `REPLAYED` only.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_fresh_commit_records_the_committed_counter_row(service, ...) -> None:
    """The write side finally records COMMITTED, so the admin route's
    commit_counters can show a committed row (BACKLOG 2026-08-24 §5.4)."""
    result = await service.commit(command, device_context)   # the harness of the existing replay test
    assert result is not None                                # fresh commit, not a replay
    counters = service.metrics.lifecycle_diagnostics().commit_counters
    assert counters.get((command.operation, LifecycleMetricOutcome.COMMITTED)) == 1
    assert counters.get((command.operation, LifecycleMetricOutcome.REPLAYED)) is None
```

And the replay invariant (extend the existing replay test): an exact replay adds `REPLAYED == 1` and does NOT add `COMMITTED`.

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/unit/source_lifecycle -k committed_counter -q` → FAIL (no committed row).

- [ ] **Step 3: Implement**

In `commit()`, before `return result` (L125):

```python
        self._record_commit(command=command, started_at=started_at)
        return result
```

Beside `_record_replay`:

```python
    def _record_commit(self, *, command: LifecycleCommand, started_at: datetime) -> None:
        self.metrics.record_commit(
            operation=command.operation,
            outcome=LifecycleMetricOutcome.COMMITTED,
            duration_seconds=(self.clock() - started_at).total_seconds(),
        )
```

(Match `_record_replay`'s exact duration convention — read it first and mirror.)

- [ ] **Step 4: Run + commit + retire the row**

Run: `uv run pytest tests/unit/source_lifecycle -q` — PASS.

```bash
git add src/personal_os/source_lifecycle/service.py tests/unit/source_lifecycle docs/handoff/BACKLOG.md
git commit -m "fix: record the committed outcome on fresh lifecycle commits"
```
Remove the `| 2026-08-24 | source-lifecycle | Write side records only replayed outcomes...` row.

---

### Task 4: Spool shielded cleanup stops masking cancellation

**Files:**
- Modify: `packages/r2-object-storage/src/r2_object_storage/spool.py:135-153` (`_run_shielded_cleanup`)
- Test: `tests/contract/object_storage/test_spool_shielded_cleanup.py` (new)

**Interfaces:**
- Consumes: the fixed adapter pattern `adapter.py:109-144` (`_run_shielded`, commit `f9c27df`) and its tests at `tests/contract/object_storage/test_r2_adapter_resource_contract.py:887,921`.
- Produces: `_run_shielded_cleanup(cleanup, *, on_cleanup_failure=None)` — `CancelledError` always wins; a cleanup failure routes to the callback (or stays unobservable-by-design when `None`, per the adapter docstring convention).

- [ ] **Step 1: Write the failing tests (both orderings)**

```python
@pytest.mark.asyncio
async def test_raising_cleanup_does_not_mask_caller_cancellation() -> None:
    async def cleanup() -> None:
        raise RuntimeError("cleanup exploded")

    task = asyncio.ensure_future(_run_shielded_cleanup(cleanup()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cleanup_failure_alone_reaches_the_callback() -> None:
    seen: list[BaseException] = []
    async def cleanup() -> None:
        raise RuntimeError("cleanup exploded")
    with pytest.raises(RuntimeError):
        await _run_shielded_cleanup(cleanup(), on_cleanup_failure=seen.append)
    assert len(seen) == 0  # no cancellation: the error propagates unchanged
```

(Import `_run_shielded_cleanup` from `r2_object_storage.spool`; follow the file-family's existing async test setup.)

- [ ] **Step 2: Run to verify failure** — the first test FAILS (RuntimeError masks CancelledError today).

- [ ] **Step 3: Implement (mirror the adapter)**

```python
async def _run_shielded_cleanup(
    cleanup: Coroutine[object, object, None],
    *,
    on_cleanup_failure: Callable[[BaseException], None] | None = None,
) -> None:
    """Drive ``cleanup`` to completion even when the caller is cancelled.

    A cleanup that itself fails while the caller is cancelled must never
    mask that cancellation (same invariant as the adapter's
    ``_run_shielded``); ``on_cleanup_failure`` is the observation seam and
    ``None`` means unobservable by design.
    """
    task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BaseException as cleanup_error:
            if on_cleanup_failure is not None:
                on_cleanup_failure(cleanup_error)
        raise
```

Survey the five call sites (spool.py:224, 225, 339, 342, 344): where a diagnostics sink is in scope, pass an `on_cleanup_failure` that records the closed `internal_error` token event (mirror the adapter call sites); otherwise leave `None`.

- [ ] **Step 4: Run + commit + retire the row**

Run: `uv run pytest tests/contract/object_storage -q` — PASS.

```bash
git add packages/r2-object-storage/src/r2_object_storage/spool.py tests/contract/object_storage/test_spool_shielded_cleanup.py docs/handoff/BACKLOG.md
git commit -m "fix: preserve cancellation when the spool shielded cleanup raises"
```
Remove the `| 2026-08-30 | object-storage | spool.py:148-149 _run_shielded_cleanup retains the cleanup-raises-masks-cancellation pattern...` row.

---

### Task 5: Terminal transitions clear the raw locator (RED→GREEN)

**Files:**
- Modify: `packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py` (`_apply_terminal_transition` L1372-1384 and the bound variant L1188-1198)
- Test: `tests/integration/source_publication/test_small_file_operations.py:599` (existing RED) + a bound-side companion

**Interfaces:**
- Consumes: `terminal_result_update_statement` (L504-528, guard `state == STATE_PENDING`), `terminal_locator_clear_statement` (L395-415, same guard), bound twins (`bound_terminal_result_update_statement` requires `STATE_RECEIVING`, L418-435 clear).
- Produces: clear-before-terminal ordering inside the same transaction; concurrent terminal winners still surface as `_state_invalid` via the terminal update's zero-row guard.

- [ ] **Step 1: Run the RED test**

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan3-t5-* uv run pytest tests/integration/source_publication/test_small_file_operations.py::test_terminal_transition_clears_raw_locator_and_keeps_digest -m local_stack -q`
Expected: FAIL — `row["normalized_locator"] is not None` (the clear is a guaranteed zero-row update today).

- [ ] **Step 2: Reorder both variants**

In `_apply_terminal_transition`, move the clear BEFORE the guarded terminal update (same transaction):

```python
        # Clear the transient raw locator while the row is still in the
        # pre-terminal state the clear's own guard admits; the retained
        # digest stays so an exact replay can still confirm locator
        # identity. A concurrent terminal winner between the two statements
        # surfaces as a zero-row terminal update -> _state_invalid, rolling
        # the whole transaction back.
        if row.normalized_locator is not None:
            cleared = await connection.execute(
                terminal_locator_clear_statement(operation_id=row.operation_id)
            )
            if cleared.rowcount != 1:
                raise _state_invalid()
        guarded = await connection.execute(
            terminal_result_update_statement(operation_id=row.operation_id, result=result)
        )
        if guarded.rowcount != 1:
            raise _state_invalid()
```

Apply the identical reorder to the bound variant (L1188-1198: `bound_terminal_locator_clear_statement` while `STATE_RECEIVING`, then `bound_terminal_result_update_statement`). Verify `_state_invalid()` is the module's actual raise helper name first (`rg -n "_state_invalid" packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py`).

- [ ] **Step 3: Add the bound-side companion test**

Mirror the L599 test against the receiving-bound path (the harness used by the bound-transition tests in the same file), asserting `normalized_locator is None` + fingerprint retained after the bound terminal transition.

- [ ] **Step 4: Run + commit + retire the row**

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan3-t5-* uv run pytest tests/integration/source_publication/test_small_file_operations.py -m local_stack -q` — the formerly-RED test PASSES.

```bash
git add packages/postgresql-source-store/src/postgresql_source_store/small_file_sync_operations.py tests/integration/source_publication/test_small_file_operations.py docs/handoff/BACKLOG.md
git commit -m "fix: clear the raw locator on terminal transitions for real"
```
Remove the `| 2026-08-30 | small-file | Raw locator never cleared on terminal transition...` row.

---

### Task 6: Gated downgrades leave no half-applied schema (RED→GREEN)

**Files:**
- Modify: `migrations/database_migration_runtime.py` (shared `allow_destructive_requested`)
- Modify: `migrations/versions/20260818_01_add_small_file_sync_operations.py` (delegate its `_downgrade_gate_open` to the shared helper)
- Modify: `migrations/versions/20260820_01_add_source_locator_lifecycle.py:365-427` (downgrade preflights the small-file evidence gate before ANY drop)
- Test: `tests/integration/source_publication/test_small_file_operations.py:1039` (`test_downgrade_refuses_to_discard_operation_evidence`) + `:1056` (existing RED) — extend the refusing test with schema-intact assertions

**Interfaces:**
- Produces: `allow_destructive_requested(config) -> bool` in `migrations/database_migration_runtime.py` (reads the `allow_destructive=true` `x` flag exactly as `_downgrade_gate_open` does today).

- [ ] **Step 1: Extend the refusing test first (RED assertions)**

In `test_downgrade_refuses_to_discard_operation_evidence` (L1039), after the expected `CommandError`:

```python
    # A refused downgrade must leave NO half-applied schema behind: still at
    # head, columns intact (BACKLOG 2026-08-30, migrations/small-file).
    assert await _schema_head(small_file_harness) == _SMALL_FILE_HEAD
    async with small_file_harness.engine.connect() as connection:
        columns = (
            await connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'knowledge'"
                    " AND table_name = 'small_file_upload_operations'"
                    " AND column_name IN ('normalized_locator', 'locator_fingerprint')"
                )
            )
        ).scalars().all()
    assert set(columns) == {"normalized_locator", "locator_fingerprint"}
```

(`_SMALL_FILE_HEAD` = the file's existing head constant, e.g. `"20260829_01"` — reuse whatever `_schema_head` comparisons already use.)

- [ ] **Step 2: Run to verify the new assertions fail**

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan3-t6-* uv run pytest tests/integration/source_publication/test_small_file_operations.py -k downgrade -m local_stack -q`
Expected: FAIL — head/columns show the half-applied state; the L1056 test also fails.

- [ ] **Step 3: Implement the hoisted gate**

In `migrations/database_migration_runtime.py` add (mirroring `_downgrade_gate_open`'s cmd_opts read from 20260818_01 — copy its body verbatim):

```python
def allow_destructive_requested(config: Config) -> bool:
    """True when the operator passed ``-x allow_destructive=true``.

    Shared by every revision whose downgrade discards canonical evidence
    so a refusal can preflight BEFORE any earlier-revision drop commits
    (``transaction_per_migration`` commits each revision independently).
    """
```

In `20260818_01`, `_downgrade_gate_open()` becomes a thin delegate. In `20260820_01`'s `downgrade()`, FIRST (before its own protected-row gate and any drop):

```python
    bind = op.get_bind()
    operation_row_count = int(bind.execute(sa.text(_DOWNGRADE_GATE_COUNT_SQL)).scalar_one())
    if operation_row_count > 0 and not allow_destructive_requested(...):
        raise RuntimeError("small_file_sync_downgrade_requires_explicit_gate")
```

Import the count SQL/gate semantics from 20260818_01's module-level constants — if the module-name-with-digits blocks the import, duplicate the one-line count SQL into 20260820_01 with a provenance comment (migrations are allowed local repetition; the shared helper is only the flag read).

- [ ] **Step 4: Run both downgrade tests + round-trip gates**

```bash
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan3-t6-* uv run pytest tests/integration/source_publication/test_small_file_operations.py -k downgrade -m local_stack -q
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan3-t6-* uv run pytest tests/contract/test_canonical_postgresql_migration_contract.py -q
```
Expected: PASS (both formerly-RED tests green; contract graph gates intact).

- [ ] **Step 5: Commit + retire the row**

```bash
git add migrations/ docs/handoff/BACKLOG.md tests/integration/source_publication/test_small_file_operations.py
git commit -m "fix: preflight the small-file evidence gate before any downgrade drop"
```
Remove the `| 2026-08-30 | migrations/small-file | In-process downgrade partial-commit gap...` row.

---

### Task 7: Device-sync scale indexes

**Files:**
- Create: `migrations/versions/20260901_02_add_device_sync_workspace_scoped_indexes.py`
- Modify: `src/personal_os/database_schema.py` (`CANONICAL_POSTGRESQL_SCHEMA_REVISION = "20260901_02"`)
- Modify: `tests/integration/device_sync/test_device_sync_query_plans.py` (docstring + new pins)
- Test: `tests/unit/migrations/` (new revision test following Task 2 of the web-auth plan's structure)

**Interfaces:**
- Consumes: `sync_events` (existing global `uq_sync_events__event_sequence` only), `source_tombstones` (no `restore_event_id` index today), the query-plan harness of `test_device_sync_query_plans.py` (10,000-event fixture, `EXPLAIN (FORMAT JSON)`).
- Produces: `ix_sync_events__workspace_event_sequence` on `(workspace_id, event_sequence)`; `ix_source_tombstones__restore_event_id` on `(restore_event_id)` partial `WHERE restore_event_id IS NOT NULL`; revision `20260901_02` (down_revision `20260901_01` if the web-auth plan landed first, else `20260829_01` — set to the actual current head when this task starts).

- [ ] **Step 1: Write the failing query-plan pins**

In `test_device_sync_query_plans.py` — update the module docstring (it currently states the schema authority owns NO `restore_event_id` index) and add:

```python
@pytest.mark.asyncio
async def test_multi_workspace_pull_page_uses_the_composite_index(harness) -> None:
    """A second workspace makes the workspace-scoped pull index load-bearing
    (BACKLOG 2026-08-26 device-sync)."""
    # seed the fixture's second workspace (the harness's existing seed call,
    # workspace_id=2) then:
    plan = await harness.explain(device_pull_page_statement(workspace_id=_SECOND_WORKSPACE_ID, ...))
    assert _index_names(plan) & {"ix_sync_events__workspace_event_sequence"}


@pytest.mark.asyncio
async def test_tombstone_restore_lookup_is_indexed(harness) -> None:
    plan = await harness.explain(
        sa.select(source_tombstones).where(source_tombstones.c.restore_event_id == sa.bindparam("rid"))
    )
    assert _index_names(plan) & {"ix_source_tombstones__restore_event_id"}
```

(Reuse the file's existing EXPLAIN helpers/assertion style — read `_index_names`' actual equivalent in the file first and mirror it.)

- [ ] **Step 2: Run to verify failure** — both new tests FAIL (indexes absent).

- [ ] **Step 3: Implement migration + bump**

```python
revision: str = "20260901_02"
down_revision: str | None = "<current head>"

def upgrade() -> None:
    op.create_index(
        "ix_sync_events__workspace_event_sequence", "sync_events",
        ["workspace_id", "event_sequence"], schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_source_tombstones__restore_event_id", "source_tombstones",
        ["restore_event_id"], schema=SCHEMA_NAME,
        postgresql_where=sa.text("restore_event_id IS NOT NULL"),
    )

def downgrade() -> None:
    op.drop_index("ix_source_tombstones__restore_event_id", table_name="source_tombstones", schema=SCHEMA_NAME)
    op.drop_index("ix_sync_events__workspace_event_sequence", table_name="sync_events", schema=SCHEMA_NAME)
```

(Copy `SCHEMA_NAME`/header conventions from `20260820_01`.) Bump `CANONICAL_POSTGRESQL_SCHEMA_REVISION`. Update `test_downgrade_drops_known_objects_in_exact_reverse_without_cascade` (`tests/contract/test_canonical_postgresql_migration_contract.py:1031`) if it enumerates objects.

- [ ] **Step 4: Run all migration + plan gates**

```bash
CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan3-t7-*
uv run alembic upgrade head && uv run alembic -x allow_destructive=true downgrade <predecessor> && uv run alembic upgrade head
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan3-t7-* uv run pytest tests/integration/device_sync/test_device_sync_query_plans.py tests/integration/device_sync/test_device_sync_migration.py -m local_stack -q
bash .local/serve-live-ci.sh down
```
Expected: exit 0 throughout; existing single-workspace plan gates keep passing.

- [ ] **Step 5: Commit + retire the row**

```bash
git add migrations/ src/personal_os/database_schema.py tests/integration/device_sync/ tests/unit/migrations/ docs/handoff/BACKLOG.md
git commit -m "feat: workspace-scoped device-sync pull and tombstone-restore indexes"
```
Remove the `| 2026-08-26 | device-sync | Per-workspace pull index (workspace_id, event_sequence)...` row.

---

### Task 8: Final verification

- [ ] **Step 1: The 2026-08-30 documented failures are gone**

```bash
CI=true bash .local/serve-live-ci.sh up knowledge-ci-plan3-final-*
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-plan3-final-* uv run pytest tests/integration/source_publication tests/integration/canonical_core -m "local_stack and not r2_live" -q
bash .local/serve-live-ci.sh down
```
Expected: **0 failed** (the 2026-08-30 run had exactly these two failures; nothing else regressed).

- [ ] **Step 2: Full offline gates**

```bash
uv run poe verify
uv run poe api-contract-check
```
Expected: exit 0; OpenAPI snapshot unchanged.

- [ ] **Step 3: BACKLOG check**

Run: `rg -n "2026-08-14 \| infra|2026-08-23 \| sync-error-tracing|2026-08-24 \| source-lifecycle|2026-08-30 \| object-storage|2026-08-30 \| small-file|2026-08-30 \| migrations/small-file|2026-08-26 \| device-sync" docs/handoff/BACKLOG.md`
Expected: no hits — all seven rows retired.

## Self-review notes

Spec coverage: C1→Task 1, C2→Task 2, C3→Task 3, C4→Task 4, C5→Task 5, C6→Task 6, C7→Task 7; acceptance criteria 1–6 map to Tasks 5/6/8, 1, 3, 7, 8 and per-task retirements. Consistency check: `_state_invalid` and the harness helper names carry explicit verify-first instructions where the plan could not confirm them from source; the two new revision ids (`20260901_01` web-auth, `20260901_02` this plan) are ordered and the down_revision instruction says "set to actual head at task start".
