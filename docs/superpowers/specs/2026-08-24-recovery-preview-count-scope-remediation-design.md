# Recovery Preview Count Scope Remediation Design

**Status:** Proposed
**Date:** 2026-08-24
**Scope owner:** Canonical recovery live acceptance
**Depends on:** `docs/superpowers/specs/phase 1/canonical-core-acceptance-and-recovery-design.md`

## 1. Objective

Align the live restore acceptance assertion with the established recovery
contract: restore verifies every canonical table, while ephemeral policy-preview
tables are deliberately excluded from backup and restore.

## 2. Evidence and root cause

`test_restore_matches_source_bundle_and_post_restore_read` compares the
complete `SOURCE_STORE_TABLES` count map before backup with the restored
canonical-count map. The former includes `policy_previews` and
`policy_preview_results`; the latter rightly excludes them.

The snapshot adapter documents preview tables as reconstructible, and
`SNAPSHOT_LOCK_ORDER` and `CANONICAL_COUNT_TABLES` intentionally omit both
tables. The failure therefore arises from an over-broad test oracle, not a
lossy recovery implementation. Adding preview tables to the backup would
contradict the current recovery contract.

## 3. Scope

In scope:

- Narrow only the failing live-test baseline to the version-selected canonical
  count set.
- Make the test explicitly prove that the restore receipt and the restore
  target contain exactly `CANONICAL_COUNT_TABLES`.
- Preserve the existing post-restore object and canonical-read assertions.

Out of scope:

- Backing up, restoring, migrating or locking `policy_previews` or
  `policy_preview_results`.
- Changing `SNAPSHOT_LOCK_ORDER`, recovery manifest contracts, the schema
  revision, `pg_dump` behavior or production policy-preview lifecycle.

## 4. Contract

For a current recovery manifest, `canonical_counts` and post-restore database
counts cover exactly `CANONICAL_COUNT_TABLES`. The test's expected baseline
must be computed by filtering `harness.table_counts()` to that closed set.

`policy_previews` and `policy_preview_results` are not proof of canonical
state. They may be absent after restore even if present at backup time. Their
absence must not cause a recovery failure, and their inclusion must not be
silently accepted as a manifest-contract widening.

The live test must retain an explicit assertion that its expected-map keys,
the restore result keys and the restore-target keys are all precisely the
current canonical set. This prevents future accidental use of an all-table
oracle.

## 5. Required changes

1. In `tests/integration/canonical_core/test_live_r2_acceptance.py`, build
   `counts_at_backup` by filtering the harness's full count map through
   `CANONICAL_COUNT_TABLES`.
2. Import the closed count-set constant from the recovery contract; do not
   duplicate table names in the test.
3. Add or extend a focused regression test that seeds a preview row before
   backup, confirms it is present in the full source-store count map, and
   proves the restore assertions use only the canonical count set.

No recovery code or database state changes are authorized by this spec.

## 6. Acceptance criteria

- The regression test fails with the previous all-table comparison and passes
  with the canonical-set comparison.
- The live restore drill passes while `policy_previews` exists before backup.
- The resulting recovery manifest and restore receipt contain no preview-table
  count keys.
- Existing offline recovery-manifest and restore compatibility tests remain
  green.
- The protected canonical-core acceptance workflow passes on the same commit.

