# Performance fixture repair report

## Diagnosis

CI run `33392146949` completed its migration and feature gates, then the
performance gate failed during setup for all five tests.  The failure was a
`TypeError` from `_seed_performance_workspace`: its `PolicyMigrationStack`
constructor still supplied the former eight-field contract.

`PolicyMigrationStack` in `tests/integration/exclusion_policy/conftest.py`
now requires seven additional migration-evidence values, introduced across
`36ef06f`, `5eb9a06`, and `b4b63f9`:

- event-bound source-version ID and current source-version ID;
- immutable event payload and current payload;
- the closed unbackfillable-upgrade return code, result code, and revision.

The integration migration fixture already supplies all fifteen values.  The
performance fixture seeds one committed version per source and does not stage
the deliberately unbackfillable migration-negative row.  Therefore its event
and current version IDs both truthfully reference `perf-version-0`; its empty
payload and closed-upgrade fields record that those migration-only branches are
not seeded by this post-head performance fixture.

## Red and green evidence

- **Red:** CI run `33392146949` reported five performance setup errors at the
  omitted `PolicyMigrationStack` constructor fields.
- **Green:** the focused real-stack performance gate completed with five tests,
  zero errors and zero failures in `109.824s`; its JUnit record is
  `.local/test-results/exclusion-policy-performance-fixture-repair.xml`.

## Changed files

- `tests/performance/test_exclusion_policy_performance.py`: populate the seven
  required migration-evidence fields when constructing `PolicyMigrationStack`.

## Commands and results

1. Inspected the performance fixture, the parallel integration fixture, and
   commits `36ef06f`, `5eb9a06`, and `b4b63f9`: confirmed the stale constructor
   call as the sole difference.
2. Attempted the required launcher:
   `bash .local/serve-live-ci.sh up knowledge-ci-perf-fixture-20260831`.
   Bash could not resolve the Windows-installed `uv.exe`; after preserving that
   executable through a Bash wrapper, the launcher reached its migration phase
   but returned `database_migration_configuration_invalid`.  No runtime or
   configuration files were changed.
3. Provisioned the same disposable project through the repository-owned stack
   tool, which reported `stack_ready`, then ran:

   ```powershell
   $env:CI='true'
   $env:LOCAL_STACK_TEST_PROJECT='knowledge-ci-perf-fixture-20260831'
   & 'D:\App\codex-usage\.venv\Scripts\uv.exe' run pytest `
     tests/performance/test_exclusion_policy_performance.py -m local_stack -q `
     --junitxml=.local/test-results/exclusion-policy-performance-fixture-repair.xml
   ```

   Result: `5 passed`, `0 errors`, `0 failures`, `109.824s`.
4. Ran `uv run ruff check tests/performance/test_exclusion_policy_performance.py`:
   passed.
5. Ran `uv run mypy --strict tests/performance/test_exclusion_policy_performance.py`:
   blocked by 12 pre-existing strict errors in the performance and integration
   test harnesses, including pre-existing errors at performance lines 232-233
   and 491-492; this repair introduces none.
6. Ran `git diff --check`: passed before commit.

## Commit

- `4e8d768a405f8e158e436bd1ea5ca8496e64f057` — `test: repair performance migration fixture`

## Self-review

### Spec compliance

The repair supplies precisely the missing contract fields and does not alter
the migration fixture, migration behavior, local-stack configuration, or
performance budgets.  It preserves the immutable-event-versus-current
distinction explicitly: the two IDs are equal only because this fixture has
one committed version rather than a V1-to-V2 migration scenario.

### Code quality

The change is limited to eleven targeted lines.  The accompanying comment
explains why equality is valid and why the negative migration evidence is
absent.  Focused integration-style execution proves the exact fixture setup
path now completes before the five performance assertions run.

## Concerns

- The all-in-one live launcher remains unavailable for this invocation because
  its Bash-to-Windows `uv.exe` bridge reaches
  `database_migration_configuration_invalid`; this is an environment/launcher
  boundary, not a reason to alter runtime configuration in this task.
- Repository-wide strict mypy is already non-green in nearby fixture code;
  the focused performance gate and ruff check are green.
