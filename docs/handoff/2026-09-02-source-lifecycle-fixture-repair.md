# Source Lifecycle Fixture Repair Handoff

**Date:** 2026-09-02 · **Branch:** `source-lifecycle-fixture-repair`

This is the plan's single handoff file. It supersedes the planning-phase revision
of this file (retained in git history); this revision records completion.

## Commit SHAs

- Code state: `c890677` `test: seed locked lifecycle policy outcomes` (Task 1),
  then `13bd2a9` `test: seed projection intent event parents` (Task 2) — the
  branch head at the time this handoff was written.
- The closure commit carrying this handoff and the two
  [`BACKLOG.md`](BACKLOG.md) row removals lands directly on top of `13bd2a9`
  and is the branch head thereafter; its SHA could not be known at write time
  because this file is part of that commit. Read it with `git log -1` on the
  branch.

## Gate status

| Gate | Result | Evidence |
|---|---|---|
| Combined acceptance (four lifecycle files, live CI stack) | PASS | exit code 0, `35 passed in 417.89s` at 2026-09-02 ~05:29Z |
| `git diff --check` | PASS | exit code 0, no whitespace errors (checked after the code commits and again after the BACKLOG edit) |
| Ruff (Tasks 1–2, changed test files) | PASS | `uv run ruff format --check` → `3 files already formatted`; `uv run ruff check` → `All checks passed!` — per task reports (see below) |
| Live Desktop/Mobile gate | NOT RUN | No live Obsidian journey, desktop or mobile, ran at any point in this plan |

Combined acceptance command (run 2026-09-02; note the harness requires BOTH
environment variables — the plan brief's line showed only `CI=true`, and
`tests/integration/source_publication/conftest.py` fails setup with a redacted
project-name error without `LOCAL_STACK_TEST_PROJECT`):

```
CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-lifecycle-fixtures-20260902 uv run pytest \
  tests/integration/source_lifecycle/test_lifecycle_transactions.py \
  tests/integration/source_lifecycle/test_projection_dispatch.py \
  tests/integration/source_lifecycle/test_backup_restore.py \
  tests/integration/source_lifecycle/test_query_plans.py -m local_stack -q
```

Output verbatim tail: `35 passed in 417.89s (0:06:57)` — 21 from
`test_lifecycle_transactions.py` + `test_projection_dispatch.py` (Task 1 scope),
14 from `test_backup_restore.py` + `test_query_plans.py` (Task 2 scope). Each
pair had also been verified green in isolation during its own task.

The disposable stack `knowledge-ci-lifecycle-fixtures-20260902` (via
`.local/serve-live-ci.sh`) hosted the run and was torn down with
`.local/serve-live-ci.sh down` after acceptance.

Detailed per-task TDD evidence (RED → GREEN transcripts, intermediate review
rounds) lives in the untracked session artifacts
`.superpowers/sdd/2026-09-02-source-lifecycle-fixture-repair/task-1-report.md`
and `task-2-report.md` (the `.superpowers/` tree is gitignored); the counts and
outcomes cited above are inlined here so the committed record is self-sufficient.

## Decisions

### Locked-policy decision (Task 1, `c890677`)

Denied lifecycle tests now seed a real signed deny revision through
`LifecycleHarness.seed_signed_policy()` plus a per-file
`_seed_denied_source_policy()` helper called immediately before the denied
`commit()`, so the store's locked-policy re-evaluation — not a caller-supplied
decision object — produces the asserted `delete` intents.

- The former `[DENIED, INDETERMINATE]` parametrization became denied-only: the
  fixture hydrates complete canonical source evidence, so an indeterminate
  locked verdict cannot be legitimately asserted.
- Revision floor asserted as `> 1`: the empty allow-all seed is revision 1
  (workspace state starts at `active_revision_number=0`, the seeder adds 1), so
  the deny revision legitimately lands beyond the empty seed.
- No monkeypatching of `_evaluate_locked_policy`; allowed-path tests remain on
  the empty allow-all seed; no production code changed.
- The pre-repair indeterminate-restore `InternalApplicationError` was
  diagnosed as fixed-event-ID cross-variant contamination (the two
  parametrized variants shared event UUIDs, so the second hit an idempotency
  conflict) — fixture drift, not a production defect; it disappears with the
  parametrization change.

### Event-parent decision (Task 2, `13bd2a9`)

`SeededSourceLocator.create_event_id` is exposed and every direct
`projection_intents` insert parents on the canonical create-event UUID (never
the source-version UUID), guarded by identity assertions that also trip if
version-as-event wiring is reintroduced.

- PREMISE DEVIATION: `test_query_plans`' six setup errors were actually
  `fk_sources__current_version` (the fixture batch-inserted `sources` rows
  carrying `current_version_id` before the `source_versions` rows they
  reference), not the `projection_intents` FK the plan indexed. Repaired with
  the sibling device-sync fixture's pending→activate two-phase idiom — the same
  order the canonical writer produces; no FK loosening, no synthetic parents.
- `_seed_lifecycle_evidence` now commits one canonical DELETE through
  `lifecycle_store.commit`, so the tombstone-count test counts a real
  canonically-parented tombstone instead of asserting over a docstring promise
  the old seed never fulfilled.
- `_EXPECTED_INDEX_BY_QUERY["replay_by_event"]` re-pinned to `pk_sync_events`
  after read-only verification that the production
  `sync_event_lookup_by_event_statement` filters only `event_id` — a global
  by-event lookup is served by the PK, while the composite unique indexes lead
  with columns the statement never filters. No production index is missing; no
  production code changed.
- `test_snapshot_open_tombstone_count_matches_postgres_state` re-scoped to its
  own `source_id` for the direct probe, with an inclusive `>=` global snapshot
  cross-check (the sibling open-locator idiom) — the snapshot's global count is
  correct in a shared disposable database.

## BACKLOG closure

Exactly two rows removed from [`BACKLOG.md`](BACKLOG.md) after the combined
acceptance above passed (remove-only-after-green rule): the
`source-lifecycle (pre-existing)` row (test_lifecycle_transactions 5/19 plus
one test_projection_dispatch failure) and the `lifecycle/backup fixtures
(pre-existing)` row (test_backup_restore 5 fails + test_query_plans 6 setup
errors), both dated 2026-09-02 and originally indexed by the
[source-conflicts handoff](2026-09-02-source-conflict-capture-and-resolution.md).
Every other row is unchanged. No new row was added.

## Deferred items

None from this plan's scope — all in-scope findings were fixed in-session
(including both residual failures surfaced mid-plan, ruled in-plan by the
controller and fixed as recorded above).

Non-blocking observations recorded for future maintenance only (deliberately
NOT BACKLOG rows):

- No equality cross-check remains for snapshot tombstone counts — the snapshot
  API exposes a global count, so the test asserts inclusively (`>=`); an
  equality check would need a per-scope count API.

## Next actions

1. Merge branch `source-lifecycle-fixture-repair` per the usual
   branch-finishing flow.
