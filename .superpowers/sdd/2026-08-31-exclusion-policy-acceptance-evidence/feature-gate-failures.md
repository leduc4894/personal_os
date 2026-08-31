# Exclusion-policy feature-gate failure diagnosis

Date: 2026-08-31
GitHub Actions run: `33386382203`
Failing gate: `uv run --all-packages --frozen poe exclusion-policy-test`

## Outcome

All five reported test outcomes came from three fixture defects. The production
secret-file loader and recovery service were enforcing their documented
fail-closed contracts correctly, so no production code or public contract was
changed.

The final local acceptance run completed with `2087 passed, 2 skipped, 1
deselected` in 1156.09 seconds. The two Windows skips are the POSIX permission
and symlink tests; a CPython 3.14.6 Linux container ran the complete settings
test file with `35 passed`.

The CI-observation BACKLOG row was intentionally left in place. This repair
provides local evidence only; the observation row still requires a green real
GitHub runner execution of the final commit.

## Root causes

### 1. The insecure-permission fixture set a secure mode

`test_load_signer_rejects_insecure_file_permissions` used mode `0o640`.
That mode grants group read permission but not group write permission. The
canonical contract rejects `stat.S_IWGRP` or `stat.S_IWOTH`; it explicitly
allows read-only group/world bits. Linux therefore correctly returned the
signer instead of raising `SecretFileError`.

The fixture now uses `0o660`, which sets the group-write bit and exercises the
closed `SECRET_FILE_INSECURE_PERMISSIONS` refusal. Windows skipped the original
test by its explicit `os.name != "posix"` marker, which is why the defect first
surfaced on the Linux runner.

### 2. The alleged escaping symlink did not escape

The test configured `tmp_path` as the secret root, then placed the target at
`tmp_path / "outside" / "real.pem"`. The resolved target was still beneath the
resolved root, so the loader correctly accepted it.

The fixture now creates a nested `secrets` directory as the configured root,
places the target as a sibling outside that directory, and places the symlink
inside `secrets`. The resolved target is now genuinely outside the root and the
loader raises the closed `SECRET_FILE_OUTSIDE_ROOT` failure. On this Windows
host symlink creation was unavailable and the original test skipped; Linux
created the symlink and exposed the fixture error.

### 3. The backup fake omitted migration-seeded canonical bytes

The migration fixture seeds two `content_objects` and two referenced source
versions before the exclusion-policy schema upgrade. A later migration
correctly backfills both references. The recovery snapshot therefore includes
both object digests, but `RecordingObjectStore` held only objects published by
the policy backup graph. `RecoveryService` correctly attempted to copy every
referenced canonical object and the fake raised two `KeyError` exceptions.

The same seed fixture also hardcoded `byte_size=42`, while its digest input had
a different length. The fix retains the exact seed payloads on
`PolicyMigrationStack`, persists their actual byte lengths, and seeds those
exact bytes into the module-scoped fake object store. Recovery still verifies
the digest and size while copying, so the fixture cannot pass with arbitrary
placeholder bytes.

## TDD and reproduction evidence

### RED

- The exact GitHub log was read with `gh run view 33386382203 --log-failed`.
  It reported two `DID NOT RAISE SecretFileError` failures and three backup
  setup errors caused by the same two missing object digests.
- Windows focused settings run before the fix:
  `uv run pytest tests/unit/api_runtime/test_exclusion_policy_settings.py -k
  'insecure_file_permissions or symlink_escaping' -q` -> `2 skipped`.
- Focused disposable-stack backup run before the fix, with PostgreSQL 18.4
  client tools on `PATH`:
  `uv run pytest
  tests/integration/exclusion_policy/test_policy_backup_restore.py::test_policy_keyset_and_evaluation_state_survives_restore
  -m local_stack -q` -> two missing-digest `KeyError` sub-exceptions. On
  Windows, staging cleanup then emitted an additional open-file
  `PermissionError`; the first exception group matched CI and identified the
  fixture boundary.

### GREEN

- Linux CPython 3.14.6 container, complete settings test file:
  `35 passed in 1.53s`.
- Focused repaired backup/restore case: `1 passed in 70.42s`.
- Complete backup/restore integration file: `3 passed in 76.30s`.
- Windows settings test file: `33 passed, 2 skipped in 0.33s`; the skipped
  behaviors are covered by the Linux run above.
- Changed-file Ruff format check: pass.
- Changed-file Ruff lint: pass.
- `git diff --check`: pass.
- Full feature gate on disposable project `knowledge-ci-gatefail-full`:
  `2087 passed, 2 skipped, 1 deselected, 1 warning in 1156.09s` (exit 0).
  The warning is the pre-existing Starlette `httpx` deprecation warning.

## Safety and cleanup

- Secret values, file contents, configured secret paths and raw provider
  errors were not added to diagnostics or assertions.
- The existing closed reason/error-code assertions remain the observable
  failure surface; production error mapping was not weakened.
- No source, recovery, database schema, API or generated-client contract was
  changed.
- The disposable project, its volumes and its network were absent after the
  acceptance run. The operator `knowledge-local` project remained down.
- Unrelated untracked plan/spec files were not modified or staged.
