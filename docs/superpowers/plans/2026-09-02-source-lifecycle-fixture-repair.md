# Source Lifecycle Fixture Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the reproducible source-lifecycle integration fixtures while preserving canonical runtime behavior.

**Architecture:** The lifecycle store remains the authority for locked-policy
re-evaluation and the database remains the authority for referential integrity.
The change is restricted to integration harnesses/tests: publish a real signed
deny rule for tests asserting delete intents, and pass canonical create-event
identities to projection-intent fixtures.

**Tech Stack:** Python 3.14, pytest-asyncio, SQLAlchemy, PostgreSQL disposable CI stack, signed-policy test seeding.

**Spec:** `docs/superpowers/specs/2026-09-02-source-lifecycle-fixture-repair-spec.md`

## Global Constraints

- Use a disposable project matching `knowledge-ci-*` and start it only with
  `CI=true bash .local/serve-live-ci.sh up <knowledge-ci-*>`; clean it with
  `bash .local/serve-live-ci.sh down`.
- Do not read, print, copy, or commit secret values.
- Do not run Desktop/Mobile live journeys or alter plugin/device-sync code.
- Production lifecycle, policy, backup, schema, and migration code is read-only
  for this plan.
- Remove a BACKLOG row only after its focused verification is green.

---

### Task 1: Make denied lifecycle assertions use the locked policy

**Files:**

- Modify: `tests/integration/source_lifecycle/conftest.py`
- Modify: `tests/integration/source_lifecycle/test_lifecycle_transactions.py`
- Modify: `tests/integration/source_lifecycle/test_projection_dispatch.py`

**Interfaces:**

- Produces `LifecycleHarness.seed_signed_policy(workspace, rules)` which calls
  `tools.signed_policy_seed.seed_signed_policy()` with the workspace and owner
  identities and returns its `SeededSignedPolicy` result.
- Consumes `RuleKind` from `personal_os.exclusion_policy.contracts` and
  `normalize_rule` from `personal_os.exclusion_policy.normalization`. The
  definite-denial rule is `normalize_rule(uuid4(), RuleKind.EXACT_SOURCE_ID,
  source_id_operand=source_id)`.

- [ ] **Step 1: Reproduce the existing assertion drift**

  Start a disposable stack and run:

  ```powershell
  bash -lc 'CI=true bash .local/serve-live-ci.sh up knowledge-ci-lifecycle-fixtures-20260902'
  bash -lc 'CI=true uv run pytest tests/integration/source_lifecycle/test_lifecycle_transactions.py tests/integration/source_lifecycle/test_projection_dispatch.py -m local_stack -q'
  ```

  Expected: the denied/indeterminate assertions fail with actual projection
  operation `upsert`, because `LifecycleHarness.seed_workspace()` publishes an
  empty signed allow-all policy.

- [ ] **Step 2: Add the failing locked-policy regression coverage**

  Add a harness method with this boundary:

  ```python
  async def seed_signed_policy(
      self, workspace: SeededWorkspace, rules: tuple[ExclusionRule, ...]
  ) -> SeededSignedPolicy:
      return await seed_signed_policy(
          self._engine,
          workspace_id=workspace.workspace_id,
          published_by_user_id=workspace.owner_user_id,
          rules=rules,
      )
  ```

  Replace the current denied/indeterminate parametrization with a denied-only
  case, because this fixture hydrates complete canonical source evidence and
  cannot legitimately assert an indeterminate locked verdict. In each denied
  lifecycle test, call it before `commit()` with
  `normalize_rule(uuid4(), RuleKind.EXACT_SOURCE_ID, source_id_operand=source_id)`.
  Assert the returned revision number is greater than the empty seed revision
  and keep the existing `delete` intent assertion. Rename the test/docstring to
  say `denied`, then run the selected test nodes and confirm they initially fail
  because the helper/rule construction is absent or does not affect the locked
  verdict.

- [ ] **Step 3: Implement the minimal fixture policy seed**

  Construct the exact `ExclusionRule` with `normalize_rule()` as specified in
  Step 2. Use `lifecycle_harness.seed_signed_policy()`; do not monkeypatch
  `_evaluate_locked_policy` and do not alter `PostgresqlSourceLifecycleStore`.
  Keep allowed lifecycle tests on the empty allow-all seed.

- [ ] **Step 4: Verify the lifecycle policy cases**

  Run:

  ```powershell
  bash -lc 'CI=true uv run pytest tests/integration/source_lifecycle/test_lifecycle_transactions.py tests/integration/source_lifecycle/test_projection_dispatch.py -m local_stack -q'
  uv run ruff format --check tests/integration/source_lifecycle/conftest.py tests/integration/source_lifecycle/test_lifecycle_transactions.py tests/integration/source_lifecycle/test_projection_dispatch.py
  uv run ruff check tests/integration/source_lifecycle/conftest.py tests/integration/source_lifecycle/test_lifecycle_transactions.py tests/integration/source_lifecycle/test_projection_dispatch.py
  ```

- [ ] **Step 5: Commit the isolated fixture repair**

  ```powershell
  git add tests/integration/source_lifecycle/conftest.py tests/integration/source_lifecycle/test_lifecycle_transactions.py tests/integration/source_lifecycle/test_projection_dispatch.py
  git commit -m "test: seed locked lifecycle policy outcomes"
  ```

### Task 2: Restore valid event-parent fixture ordering

**Files:**

- Modify: `tests/integration/source_lifecycle/conftest.py`
- Modify: `tests/integration/source_lifecycle/test_backup_restore.py`
- Modify: `tests/integration/source_lifecycle/test_query_plans.py`

**Interfaces:**

- Extend `SeededSourceLocator` with `create_event_id: UUID` populated from the
  inserted canonical create `sync_events` row.
- Every direct `projection_intents` insert uses the corresponding event UUID,
  never `current_version_id`.

- [ ] **Step 1: Reproduce the foreign-key failure**

  Run:

  ```powershell
  bash -lc 'CI=true uv run pytest tests/integration/source_lifecycle/test_backup_restore.py tests/integration/source_lifecycle/test_query_plans.py -m local_stack -q'
  ```

  Expected: fixture setup fails with a `projection_intents.event_id` foreign-key
  violation where a source-version UUID was supplied instead of a `sync_events`
  parent identity.

- [ ] **Step 2: Add a red identity assertion**

  In the harness seed path, expose the create event identity on
  `SeededSourceLocator`. Add an assertion in the backup fixture that the event
  used for the direct intent insert equals `seeded.create_event_id`; add the
  same assertion to the query-plan row construction before its batch insert.
  Run the focused nodes and confirm the assertions/fixture fail on the old
  source-version-as-event wiring.

- [ ] **Step 3: Implement the minimal parent wiring**

  Capture `event_id` already generated by
  `seed_active_source_with_locator()` in `SeededSourceLocator.create_event_id`.
  Set `test_backup_restore.py`'s direct intent `event_id` from that field. Keep
  `test_query_plans.py`'s `event_rows` before `intent_rows` and ensure each
  intent uses the same-index `event_ids[index]`; do not loosen the foreign key
  or insert synthetic parents outside the canonical batch ordering.

- [ ] **Step 4: Verify the fixture suites**

  Run:

  ```powershell
  bash -lc 'CI=true uv run pytest tests/integration/source_lifecycle/test_backup_restore.py tests/integration/source_lifecycle/test_query_plans.py -m local_stack -q'
  uv run ruff format --check tests/integration/source_lifecycle/conftest.py tests/integration/source_lifecycle/test_backup_restore.py tests/integration/source_lifecycle/test_query_plans.py
  uv run ruff check tests/integration/source_lifecycle/conftest.py tests/integration/source_lifecycle/test_backup_restore.py tests/integration/source_lifecycle/test_query_plans.py
  ```

- [ ] **Step 5: Commit the fixture parent repair**

  ```powershell
  git add tests/integration/source_lifecycle/conftest.py tests/integration/source_lifecycle/test_backup_restore.py tests/integration/source_lifecycle/test_query_plans.py
  git commit -m "test: seed projection intent event parents"
  ```

### Task 3: Close only verified backlog rows

**Files:**

- Modify: `docs/handoff/BACKLOG.md`
- Create: `docs/handoff/2026-09-02-source-lifecycle-fixture-repair.md`

- [ ] **Step 1: Run the combined acceptance command**

  ```powershell
  bash -lc 'CI=true uv run pytest tests/integration/source_lifecycle/test_lifecycle_transactions.py tests/integration/source_lifecycle/test_projection_dispatch.py tests/integration/source_lifecycle/test_backup_restore.py tests/integration/source_lifecycle/test_query_plans.py -m local_stack -q'
  git diff --check
  ```

  Expected: exit code 0 with no failed tests and no whitespace errors.

- [ ] **Step 2: Remove the two resolved index rows**

  Remove only the `source-lifecycle` policy-expectation row and the
  `lifecycle/backup fixtures` foreign-key row dated 2026-09-02. Keep all
  Desktop/Mobile, device-sync, web-admin, future-trigger, refactor, and
  backup-manifest rows unchanged.

- [ ] **Step 3: Write the single final handoff**

  Record final commit SHA, commands and exit codes, the locked-policy and
  event-parent decisions, and an explicit statement that no live
  Desktop/Mobile gate ran. Do not copy secrets, raw paths, content, identifiers,
  or diagnostics payloads.

- [ ] **Step 4: Tear down the disposable stack**

  ```powershell
  bash .local/serve-live-ci.sh down
  ```

- [ ] **Step 5: Commit the closure documentation**

  ```powershell
  git add docs/handoff/BACKLOG.md docs/handoff/2026-09-02-source-lifecycle-fixture-repair.md
  git commit -m "docs: close lifecycle fixture backlog repairs"
  ```

## Execution discipline

Before execution, use `superpowers:using-git-worktrees` unless the user again
explicitly requests the current checkout. Execute Tasks 1–3 in order, preserve
the observed RED output before each GREEN edit, and stop if a failure proves a
production defect rather than fixture drift.
