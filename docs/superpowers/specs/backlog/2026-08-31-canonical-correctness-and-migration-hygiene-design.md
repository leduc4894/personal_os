# Canonical correctness and migration hygiene — design spec

Date: 2026-08-31. Domains: runtime-configuration tests, small-file sync
(metrics, operations, migrations), source lifecycle metrics, object-storage
spool, device-sync schema. Governing docs: the 2026-08-14
object-storage handoff §15, the 2026-08-23 sync-error-tracing handoff §5,
the 2026-08-24 closed-reason-surfacing handoff §5.4, the 2026-08-30
child-nine hygiene retirement handoff (deferred items 3–5), and the
2026-08-26 device-cursor handoff backlog verdicts.

## Purpose and scope

Retire seven indexed BACKLOG rows that are small, failing-test-first
correctness or hygiene fixes on already-shipped code — no Child 8/9 work,
no mobile, no live round:

1. 2026-08-14 infra (pre-existing) — circular import in
   `tests/unit/runtime_configuration/test_secret_files.py` breaks
   directory-scoped pytest collection; the full-suite gate is unaffected.
2. 2026-08-23 sync-error-tracing §5 — `_validate_epoch_ms` masks a
   non-int clock value as the generic `ValueError` (theoretical; the wall
   clock cannot produce it).
3. 2026-08-24 source-lifecycle §5.4 — the write side records only
   `replayed` outcomes and rejections; a fresh successful commit never
   calls `record_commit(COMMITTED)`, so the lifecycle route's
   `commit_counters` can never show a `committed` row.
4. 2026-08-30 object-storage — `spool.py:148-149` `_run_shielded_cleanup`
   retains the cleanup-raises-masks-cancellation pattern (the 2026-08-30
   fix touched only the adapter's `_run_shielded`).
5. 2026-08-30 small-file — raw locator never cleared on terminal
   transition (`small_file_sync_operations.py` clear-statement ordering;
   privacy invariant; defect since 6b9fab7; test
   `test_terminal_transition_clears_raw_locator_and_keeps_digest` RED).
6. 2026-08-30 migrations/small-file — in-process downgrade partial-commit
   gap: `transaction_per_migration` commits `20260820_01` column drops
   before `20260818_01` refuses the row gate, leaving a half-way schema;
   test `test_gated_downgrade_drops_the_operation_table_and_reapplies_head`
   RED.
7. 2026-08-26 device-sync — per-workspace pull index
   `(workspace_id, event_sequence)` and `source_tombstones.restore_event_id`
   index; query-plan gates pass at the pinned fixture size, sparsity
   degrades at multi-workspace scale. Gate: before production activation.

Out of scope: the sensitive-value-object redacted-repr row (conditional),
every "when next touched" batch not listed above, and any behavior change
beyond the named defects.

## Problem

Two privacy/correctness defects have committed RED tests (rows 5 and 6)
sitting in the integration suites as documented expectations — the
2026-08-30 final gate ran with exactly those two failures. Three smaller
gaps (rows 2–4) are known theoretical or accounting defects with rulings
already recorded. Row 1 is a pre-existing test-infrastructure defect that
breaks directory-scoped collection. Row 7 is a scale hazard: the pull and
tombstone-restore queries are full-scan-shaped once a second workspace
exists, gated only by fixture size.

## Compatibility contract

- Rows 1–6 change no public contract, no schema and no wire behavior.
  Row 5 changes stored data at terminal transitions (the raw locator
  column is cleared as its own docstring already claimed) — the digest
  and every derived surface stay identical.
- Row 7 adds two indexes through one Alembic revision with
  upgrade/downgrade tests; no query semantics change, no column changes.
- No new dependency. No cross-domain abstraction: each fix lands in its
  owning module (the repetition-over-abstraction precedents stand).

## Contracts

### C1 Directory-scoped collection restored

The import cycle in `tests/unit/runtime_configuration/test_secret_files.py`
is broken so `pytest tests/unit/runtime_configuration` collects and runs
clean, matching the full-suite gate's behavior. The fix is test-side
(import ordering/local import); it must not weaken what the test pins.

### C2 `_validate_epoch_ms` distinguishes non-int input

A non-int clock value surfaces as a distinguishable, typed error carrying
a closed reason (mirroring the sibling validators' shape) instead of the
generic `ValueError`. The path is theoretical — the test injects the
value directly. No production caller changes.

### C3 Fresh commits record `committed`

`SourceLifecycleService.commit` calls `record_commit(COMMITTED)` on a
fresh successful commit (the metrics call sites in
`src/personal_os/source_lifecycle/service.py`), while an exact replay
keeps recording `replayed` only. The lifecycle admin route's
`commit_counters` can then show a `committed` row; existing
`replayed`/`rejected` semantics are untouched.

### C4 Spool shielded cleanup stops masking cancellation

`_run_shielded_cleanup` (spool.py) follows the invariant the adapter's
`_run_shielded` already enforces: `CancelledError` propagates when the
task is cancelled; a cleanup that itself raises surfaces its error
without swallowing the cancellation. Test pins both orderings
(cleanup-raises-while-cancelled, cleanup-raises-alone).

### C5 Terminal transitions clear the raw locator

The clear-statement ordering in `small_file_sync_operations.py` is fixed
so every terminal transition clears the raw locator while keeping the
digest — making the code match its docstring and the already-RED test.
The clear applies to terminal transitions only; non-terminal updates
keep today's behavior.

### C6 Gated downgrades leave no half-applied schema

The in-process downgrade path no longer commits `20260820_01`'s column
drops before `20260818_01`'s row gate can refuse: a downgrade refused by
a row gate leaves the database at the pre-downgrade state (or a state the
existing reapply-head recovery handles), never a half-way schema. The
mechanism (pre-flight the gate, reorder, or bracket the steps) is a plan
decision bounded by: legitimate full downgrades keep
`transaction_per_migration` semantics, and the RED test turns green.

### C7 Device-sync scale indexes

One Alembic revision adds the per-workspace pull index
`(workspace_id, event_sequence)` and the `source_tombstones
(restore_event_id)` index. Existing query-plan gates keep passing at the
pinned fixture size; a multi-workspace fixture (second workspace seeded)
pins the plan shape the row names. Upgrade/downgrade and empty-database
gates run per repo rules.

## Privacy invariants (acceptance-critical)

- C5 is itself the privacy fix: raw note paths must not outlive a
  terminal transition anywhere readable.
- No new surface: C2's typed error and C3's counter row carry closed
  tokens/labels only; no paths, digests or content.

## Acceptance criteria

1. The two committed RED tests (C5, C6) turn green in the same run that
   lands their fixes; the 2026-08-30 documented-failure expectation is
   retired from the handoff narrative by a green
   `tests/integration/source_publication` run.
2. C1 verified by a clean directory-scoped `pytest
   tests/unit/runtime_configuration -q` run recorded in the plan report.
3. C3: a fresh-commit integration/unit test asserts a `committed` counter
   row appears exactly once; replay does not add one.
4. C7: migration gates plus the multi-workspace query-plan pin green.
5. Full offline gates green: `uv run poe verify`, focused domain suites
   (`poe` tasks for the touched domains), `uv run poe
   api-contract-check` (unchanged snapshot).
6. Each of the seven BACKLOG rows is removed in the diff that closes it.

## Error cases

- C6 fix cannot preserve per-migration transactions for legitimate
  downgrades: choose the mechanism that keeps both properties; if truly
  mutually exclusive, the refusal path wins (no half-way schema) and the
  deviation is recorded with evidence.
- C7 index creation against existing multi-workspace data: the migration
  is online-safe (CONCURRENTLY or the repo's established equivalent
  pattern); failure mid-migration must not leave a partial index per the
  migration test gates.
- C2's typed error collides with an existing caller's except-clause:
  widen only that caller's tuple; no bare re-mapping.
