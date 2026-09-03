# Canonical PostgreSQL Baseline Repin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repin the canonical PostgreSQL lifecycle oracle to Alembic head `20260902_02` without changing schema behavior.

**Architecture:** Existing migrations are the only catalog source of truth. Update the integration test's fixed head/catalog oracle for migrations through `20260902_02`, then run its complete upgrade/downgrade lifecycle against a disposable guarded project.

**Tech Stack:** Python strict, pytest, Psycopg, Alembic, PostgreSQL local stack.

**Spec:** `docs/superpowers/specs/2026-09-02-canonical-postgresql-baseline-repin-design.md`

## Global Constraints

- Do not create/edit migrations, production schema code, backup manifests, local secrets, or `knowledge-local` data.
- Derive exact expectations from migrations through `20260902_02`, including the reduced `ck_user_credentials__timestamps` expression.
- Use a disposable `knowledge-ci-*` project only through `CI=true bash .local/serve-live-ci.sh up <project>`; clean it with the matching `down`.
- Do not print secret values, DSNs, raw database exceptions, source content or tokens.

---

## File structure

- `tests/integration/test_canonical_postgresql_baseline.py`: fixed lifecycle catalog, row-count and fingerprint oracle.
- `migrations/versions/20260826_01_add_device_sync_reconciliation.py` through `20260902_02_drop_totp_prompt_dismissal.py`: read-only catalog sources.
- `docs/handoff/BACKLOG.md`: retirement record.

### Task 1: Establish stale-oracle evidence

**Files:**

- Modify only if extra coverage is necessary: `tests/integration/test_canonical_postgresql_baseline.py`

**Interfaces:**

- Consumes: `baseline_stack`, `run_alembic`, `_current_revision`, and `_assert_exact_object_set`.
- Produces: red evidence that existing fixed constants describe `20260818_01`, not the actual head.

- [ ] **Step 1: Run the existing lifecycle suite unchanged on a guarded disposable project.**

Run: `CI=true bash .local/serve-live-ci.sh up knowledge-ci-baseline-repin-20260902`

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-baseline-repin-20260902 uv run pytest tests/integration/test_canonical_postgresql_baseline.py -q`

Expected: FAIL with the stale head/catalog assertion; record only safe error/count evidence.

- [ ] **Step 2: Add this focused test only if existing output does not directly show the mismatched head/catalog.**

```python
def test_upgrade_head_matches_declared_baseline_head(baseline_stack: BaselineStack) -> None:
    result = run_alembic(["upgrade", "head"], baseline_stack.alembic_env)
    assert result.returncode == 0, _alembic_failure("upgrade head", result)
    assert _current_revision(baseline_stack.connection) == _HEAD_REVISION
    _assert_exact_object_set(baseline_stack.connection)
```

- [ ] **Step 3: Commit any new red test.**

```bash
git add tests/integration/test_canonical_postgresql_baseline.py
git commit -m "test: pin canonical baseline head expectation"
```

### Task 2: Repin the exact catalog oracle

**Files:**

- Modify: `tests/integration/test_canonical_postgresql_baseline.py`

**Interfaces:**

- Consumes: migration IDs `20260826_01`, `20260826_02`, `20260828_01`–`20260828_04`, `20260901_01`–`20260901_03`, `20260902_01`, `20260902_02`.
- Produces: `_HEAD_REVISION == "20260902_02"`, exact current 40-table catalog, exact index/trigger/constraint corpus, and matching fixed row-count vectors.

- [ ] **Step 1: Change the fixed head and append migration-defined groups in deterministic order.**

```python
_HEAD_REVISION: str = "20260902_02"
_TABLES_IN_COUNT_ORDER: tuple[str, ...] = (
    *_PHASE1_TABLES_IN_COUNT_ORDER,
    *_AUTHENTICATION_TABLES_IN_COUNT_ORDER,
    *_POLICY_TABLES_IN_COUNT_ORDER,
    *_SMALL_FILE_TABLES_IN_COUNT_ORDER,
    *_DEVICE_SYNC_TABLES_IN_COUNT_ORDER,
    *_MULTIPART_UPLOAD_TABLES_IN_COUNT_ORDER,
    *_SOURCE_CONFLICT_TABLES_IN_COUNT_ORDER,
)
assert len(_TABLES_IN_COUNT_ORDER) == 40
```

Populate each named group with the exact tables created by its migration; include `source_conflicts`.

- [ ] **Step 2: Repin exact indexes, triggers and constraints from migration DDL.**

Add source-conflict indexes and every post-head catalog object to the existing frozensets. Assert the reduced credentials constraint through the suite's normalized catalog representation:

```python
assert _constraint_definition(conn, "ck_user_credentials__timestamps") == (
    "CHECK (((updated_at >= created_at) AND (password_changed_at >= created_at)))"
)
```

Use the actual normalization form emitted by the test's existing fingerprint helper; do not weaken to a substring assertion.

- [ ] **Step 3: Update every fixed table count and seed vector.**

Every newly introduced table has an explicit count entry (zero unless the fixture deliberately seeds it). Update lifecycle assertions from 30 to 40 and preserve the exact fingerprint/no-op/downgrade guarantees.

- [ ] **Step 4: Run the full baseline suite.**

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-baseline-repin-20260902 uv run pytest tests/integration/test_canonical_postgresql_baseline.py -q`

Expected: PASS for fresh upgrade, no-op, advisory lock, concurrent first upgrade, gated downgrade, re-upgrade and interruption behavior.

- [ ] **Step 5: Commit the repin.**

```bash
git add tests/integration/test_canonical_postgresql_baseline.py
git commit -m "test: repin canonical PostgreSQL baseline catalog"
```

### Task 3: Fresh-project verification and backlog retirement

**Files:**

- Modify: `docs/handoff/BACKLOG.md`

- [ ] **Step 1: Re-run on a fresh disposable project.**

Run: `CI=true bash .local/serve-live-ci.sh down`

Run: `CI=true bash .local/serve-live-ci.sh up knowledge-ci-baseline-repin-verify-20260902`

Run: `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-baseline-repin-verify-20260902 uv run pytest tests/integration/test_canonical_postgresql_baseline.py -q`

Expected: PASS without secret-bearing output.

- [ ] **Step 2: Shut down the disposable project.**

Run: `CI=true bash .local/serve-live-ci.sh down`

Expected: CI project down; `knowledge-local` remains down.

- [ ] **Step 3: Retire only the canonical-postgresql-baseline row.**

- [ ] **Step 4: Run static/diff checks and commit.**

Run: `uv run ruff format --check tests/integration/test_canonical_postgresql_baseline.py; uv run ruff check tests/integration/test_canonical_postgresql_baseline.py; uv run mypy tests/integration/test_canonical_postgresql_baseline.py; git diff --check`

Expected: every command exits 0.

```bash
git add docs/handoff/BACKLOG.md
git commit -m "docs: retire canonical baseline backlog item"
```

