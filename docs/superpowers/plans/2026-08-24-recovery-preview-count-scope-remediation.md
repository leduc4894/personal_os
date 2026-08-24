# Recovery Preview Count Scope Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the protected live restore drill compare only the current manifest's closed canonical count set while proving that seeded policy-preview state is excluded.

**Architecture:** Keep recovery services, manifest parsing, snapshot locking, and PostgreSQL schema unchanged. Extend the existing end-to-end live-R2 restore test: read the complete harness map once, prove its seeded preview row exists, derive the expected map with `CANONICAL_COUNT_TABLES`, and assert that the expected map, restore receipt, and restore target each have exactly that key set.

**Tech Stack:** Python 3.14, pytest/pytest-asyncio, SQLAlchemy, recovery contracts, disposable PostgreSQL 18.4 stack, live Cloudflare R2 test harness.

**Spec:** `docs/superpowers/specs/2026-08-24-recovery-preview-count-scope-remediation-design.md`

## Global Constraints

- Change only `tests/integration/canonical_core/test_live_r2_acceptance.py`; no production recovery code, database-state fixture, migration, manifest contract, snapshot lock order, `pg_dump` behavior, or policy-preview lifecycle change is authorized.
- Import `CANONICAL_COUNT_TABLES` from `personal_os.recovery.contracts`; do not copy a table-name list into the test.
- `policy_previews` and `policy_preview_results` remain reconstructible and excluded from the recovery manifest and restore witness.
- Preserve the exact-key R2 cleanup, object-restoration, restored-byte, and canonical-read assertions in the live drill.
- The protected test must fail without live credentials or a disposable `knowledge-ci-*` stack; do not replace it with a mocked assertion or mark it skipped.
- Use RED then minimal GREEN changes; run focused tests, static checks, the offline compatibility suite, and the protected canonical-core live workflow on the same implementation commit.

## File Structure

- `tests/integration/canonical_core/test_live_r2_acceptance.py`: imports the closed recovery count set and makes the live restore oracle explicit; no new test module is needed because the existing drill owns the real backup, restore receipt, restore target, and seeded preview state.
- `tests/integration/canonical_core/test_recovery_integration.py`: read-only comparison reference; its existing filtered count-map pattern is the established offline convention and must remain unchanged.
- `src/personal_os/recovery/contracts.py`: read-only source of `CANONICAL_COUNT_TABLES`; this task must consume it without changing the current v3 contract.

---

### Task 1: Scope the protected live restore count oracle to canonical tables

**Files:**

- Modify: `tests/integration/canonical_core/test_live_r2_acceptance.py:50-70,614-666`
- Test: `tests/integration/canonical_core/test_live_r2_acceptance.py::test_restore_matches_source_bundle_and_post_restore_read`
- Reference only: `tests/integration/canonical_core/test_recovery_integration.py:389-394`
- Reference only: `src/personal_os/recovery/contracts.py:116-125`

**Interfaces:** Consumes `CANONICAL_COUNT_TABLES: tuple[str, ...]` and `CanonicalCoreHarness.table_counts() -> dict[str, int]`. Produces the regression witness `counts_at_backup: dict[str, int]`, whose keys are exactly `set(CANONICAL_COUNT_TABLES)` and which is compared with `result.table_counts` and `PostgresqlRestoreTarget.read_canonical_counts() -> Mapping[str, int]`.

- [ ] **Step 1: Write the failing live regression assertions.** Import `CANONICAL_COUNT_TABLES` beside the existing recovery-contract import. In `test_restore_matches_source_bundle_and_post_restore_read`, retain the full count map after `create_backup`, prove the fixture-seeded preview is observable there, prove previews are outside the contract, then filter the expected map through the closed set.

```python
from personal_os.recovery.contracts import CANONICAL_COUNT_TABLES, InMemoryCanonicalBackupMetrics

full_counts_at_backup = await harness.table_counts()
assert full_counts_at_backup["policy_previews"] == 1
assert "policy_previews" not in CANONICAL_COUNT_TABLES
assert "policy_preview_results" not in CANONICAL_COUNT_TABLES
counts_at_backup = {
    table_name: full_counts_at_backup[table_name]
    for table_name in CANONICAL_COUNT_TABLES
}
assert set(counts_at_backup) == set(CANONICAL_COUNT_TABLES)
```

- [ ] **Step 2: Run the focused live test to prove RED.** Following `.local/RESTART.md` and the repository live-test contract, first validate configuration through the standard loader, stop `knowledge-local`, then bring up only the disposable `knowledge-ci-<nonce>` stack and use the existing R2 test credentials without printing them. Run:

```powershell
$env:CI = "true"
$env:LOCAL_STACK_TEST_PROJECT = "knowledge-ci-recovery-preview-count"
uv run pytest tests/integration/canonical_core/test_live_r2_acceptance.py::test_restore_matches_source_bundle_and_post_restore_read -m "local_stack and r2_live" -q
```

Expected: RED on the pre-remediation all-table assertion because `full_counts_at_backup` contains `policy_previews` while the restore receipt and target expose only `CANONICAL_COUNT_TABLES`.

- [ ] **Step 3: Make the smallest GREEN oracle change.** Replace the former direct assignment `counts_at_backup = await harness.table_counts()` with the filtered map from Step 1. Before value equality assertions, make all three count witnesses explicitly closed-set assertions; leave object count, conditional object recreation, acceptance probe, and restored-byte read unchanged.

```python
assert set(counts_at_backup) == set(CANONICAL_COUNT_TABLES)
assert set(result.table_counts) == set(CANONICAL_COUNT_TABLES)
assert dict(result.table_counts) == counts_at_backup

restored_counts = dict(await live_restore_target_context.restore_target.read_canonical_counts())
assert set(restored_counts) == set(CANONICAL_COUNT_TABLES)
assert restored_counts == counts_at_backup
```

- [ ] **Step 4: Run GREEN and regression gates.** Re-run the focused live command from Step 2; expect PASS while the full source map still reports one preview row. Then run the offline compatibility coverage and static checks:

```powershell
uv run pytest tests/integration/canonical_core/test_recovery_integration.py tests/unit/recovery -q
uv run ruff format --check tests/integration/canonical_core/test_live_r2_acceptance.py
uv run ruff check tests/integration/canonical_core/test_live_r2_acceptance.py
uv run mypy tests/integration/canonical_core/test_live_r2_acceptance.py
git diff --check
```

Expected: every command exits 0; current and historical recovery-manifest/restore tests stay green.

- [ ] **Step 5: Run the protected acceptance workflow on the implementation commit.** Push the committed change to protected `master` through the repository's approved integration flow or manually dispatch `.github/workflows/canonical-core-acceptance.yml`; wait for its `ubuntu-live` job. Its fixed command is:

```bash
uv run pytest tests/integration/canonical_core -m "local_stack and r2_live" -q --junitxml=.local/test-results/canonical-core-acceptance.xml
```

Expected: PASS, including the edited restore drill, with only the sanitized JUnit report retained. Treat a missing credential, unavailable disposable stack, cleanup failure, or non-green workflow as a blocking acceptance gate rather than a deferred/mock substitute.

- [ ] **Step 6: Commit and hand off.** Commit only the test change with message `test: scope live restore counts to canonical tables`. After the protected workflow passes on that SHA, write exactly one `docs/handoff/2026-08-24-recovery-preview-count-scope-remediation.md` with the final SHA, RED/GREEN and all gate evidence, the assertion that preview tables remain excluded, and only concrete deferred `BACKLOG.md` rows if any external gate cannot be completed.

## Self-Review

- Spec coverage: Task 1 filters only the live baseline, imports the contract-owned closed set, proves each of the three count maps has exactly the canonical keys, leaves object/read assertions in place, and executes offline plus protected live acceptance gates. No task widens backup/restore, locks, manifests, schema, or policy-preview lifecycle.
- Completeness: every task step supplies a concrete change or verification command.
- Type consistency: `table_counts()` returns `dict[str, int]`; `CANONICAL_COUNT_TABLES` is `tuple[str, ...]`; `result.table_counts` and restored count mappings are converted or compared as `dict[str, int]` using the same table-key names.
