# Canonical PostgreSQL Baseline Repin Handoff

**Date:** 2026-09-03 · **Branch:** `canonical-postgresql-baseline-repin`

This is the plan's single handoff file, recording completion. The plan
(`docs/superpowers/plans/2026-09-02-canonical-postgresql-baseline-repin.md`)
repinned `tests/integration/test_canonical_postgresql_baseline.py` from the
stale head `20260818_01` (30 tables) to the current head `20260902_02`
(exact 40-table catalog, including the reduced `ck_user_credentials__timestamps`
clause), fixed three pre-existing mypy errors in the suite, and retired the
`canonical-postgresql-baseline` row from [`BACKLOG.md`](BACKLOG.md).

## Commit SHAs

- Code commits:
  - `0f656cd` `test: repin canonical PostgreSQL baseline catalog` (Task 2:
    `_HEAD_REVISION` repin, eight grouped table constants totaling 40,
    40-entry row-count vector, 14 new indexes, credentials CHECK pin,
    negative-case repair; 1 file, +96/−8)
  - `85d3d38` `test: fix pre-existing typing errors in baseline suite`
    (three pure typing guards at the former lines 914/2356/2409, +11/−3,
    zero behavior change)
  - `d446f85` `docs: retire canonical baseline backlog item` (exactly one
    row removed from [`BACKLOG.md`](BACKLOG.md), no other row touched)
- Docs commits from the closing fix wave:
  - `78c0497` `docs: add canonical baseline repin plan and spec` (the plan
    and spec markdowns, previously untracked carryover, committed per repo
    convention)
  - The commit carrying this handoff lands directly on top; its SHA could
    not be known at write time because this file is part of that commit.
    Read it with `git log -1` on the branch.

## Gate status

| Gate | Result | Evidence |
|---|---|---|
| Stale-oracle red run (Task 1, pre-repin) | RED as expected | `7 failed, 1 passed in 621.33s` on disposable `knowledge-ci-baseline-repin-20260902`; 3 tests failed `assert '20260902_02' == '20260818_01'`, 3 failed the exact-object-set assert with the 10-table symmetric difference (source-locator lifecycle, device-sync manifests, multipart uploads, source conflicts), 1 failed the `_baseline_revision_applied` probe; the single pass accepts base-or-phase-1 by design; no repository file changed, no commit |
| Repinned suite green (Task 2) | PASS | `8 passed in 582.14s (0:09:42)`, exit 0, same project `knowledge-ci-baseline-repin-20260902` via `CI=true LOCAL_STACK_TEST_PROJECT=... uv run pytest tests/integration/test_canonical_postgresql_baseline.py -m local_stack -q` |
| Fresh-project re-verify (Task 3) | PASS | `8 passed in 621.31s (0:10:21)`, exit 0, on second disposable `knowledge-ci-baseline-repin-verify-20260902`, after commits `0f656cd`+`85d3d38` |
| Static gates | PASS | `ruff format --check`, `ruff check`, `mypy`, `git diff --check` all exit 0 (run after the typing fix and again after the BACKLOG edit, identical results); mypy/ruff independently re-confirmed at HEAD by the final whole-branch reviewer |
| Stack down | PASS | `serve-live-ci.sh down` → `result_code: stack_down_complete`, `state: absent` for `knowledge-ci-baseline-repin-verify-20260902` (and the first project torn down before it); `knowledge-local` left DOWN; `docker ps -a` filtered by compose project label shows `NO_KNOWLEDGE_CONTAINERS` |
| BACKLOG retirement | DONE | The `canonical-postgresql-baseline (pre-existing)` row removed in `d446f85` only after the fresh-project green run (remove-only-after-green rule); post-edit grep for the domain string returns nothing |

Leak checks: full pytest outputs contain zero occurrences of `postgres://`,
`postgresql://`, `password`, `secret`; assertions name only revisions, table
names, and counts.

## Interpretive decisions (sanctioned deviations)

Five deviations from the plan's letter, each adjudicated in review:

1. **Eighth table group.** The plan's Step 1 enumerates seven groups totalling
   38 tables; the two `20260820_01` tables (`source_locators`,
   `source_tombstones`) belong to none of them, so the plan's own
   `assert len(_TABLES_IN_COUNT_ORDER) == 40` is unreachable. Added
   `_SOURCE_LOCATOR_TABLES_IN_COUNT_ORDER` between the small-file and
   device-sync groups, preserving migration order in the splat. The count
   assert is the governing invariant.
2. **Credentials CHECK form.** Pinned to the actual pretty-printed
   `pg_get_constraintdef(oid, true)` form
   `CHECK (updated_at >= created_at AND password_changed_at >= created_at)` —
   the exact representation the catalog fingerprint hashes — not the plan's
   illustrative triple-parenthesis deparse. Exact full-string match; strength
   unchanged. The plan's own "use the actual normalization form emitted by
   the test's existing fingerprint helper" instruction takes precedence.
3. **Cross-source negative case.** The intent-belongs-to-another-source case
   previously inserted `source_version_id IS NULL` and expected FK 23503;
   `20260820_01` strengthened `ck_projection_intents__operation_version` so
   PostgreSQL rejects that row with 23514 before the FK can fire. The case now
   binds the claimed source's own version (setup rows via the file's existing
   `_INSERT_SETUP_SOURCE_*` pattern) so every CHECK passes and the composite
   FK `fk_projection_intents__event_source` is again the rejecting
   constraint — original guarantee strength and SQLSTATE preserved.
4. **Pytest invocation.** `-m local_stack -q`, because the plan's bare `-q`
   deselects the entire suite under the repo's default addopts
   (`not local_stack and not r2_live and not device_records`; observed as
   `8 deselected`, exit 5). The dedicated CI workflow
   (`.github/workflows/canonical-postgresql-baseline.yml`) selects the same
   marker.
5. **Pre-existing mypy errors fixed on-branch.** The three errors at the
   former lines 914/2356/2409 exist on parent `5e8269e` (verified
   independently by two reviewers), but Task 3's gate demands mypy exit 0 and
   AGENTS.md requires mypy-strict compatibility. Fixed with minimal typing
   guards only (+11/−3): two `assert ... is not None` narrowing guards over
   queries that provably always return a row, and one annotated intermediate
   variable instead of importing `cast`. No assertion or lifecycle behavior
   changed.

## Plan defects (recorded for the next repin)

Documented by the final whole-branch reviewer as plan-text defects, not
implementation problems, so the next repin doesn't re-derive them:

- The plan omits migration `20260820_01` from its table-group enumeration,
  while its own `len == 40` assert requires that migration's 2 tables.
- The plan's CHECK sketch (triple-parenthesis deparse) contradicts its own
  instruction to use the actual normalization form of the fingerprint helper.
- The plan's bare `-q` pytest invocation deselects the suite under the repo's
  addopts; the sanctioned form is `-m local_stack -q`.

## Deferred items

Exactly one, with a closed verdict (no BACKLOG row is created — repo rules
forbid deferring in-scope items; this is a "code stands" ruling, not a
deferral):

- `_constraint_definition` f-string interpolation of `constraint_name` —
  **code stands**: single literal call site; the `_rows` helper takes no
  parameters, so threading a bound param would widen blast radius beyond the
  single use; the file already has the interpolation idiom in
  `_relation_exists` and `_row_counts`; test-only code with no untrusted
  input.

## Known pre-existing condition (for the next operator)

`serve-live-ci.sh up` on a fresh CI project exits 1 at the API-readiness
sub-gate with reason token `exclusion_policy_not_initialized` (no published
policy keyset on a fresh CI database; the verifier fails closed before socket
bind). This is documented pre-existing behavior on every fresh CI project
since 2026-08-27 — see
[the 2026-09-01 canonical-correctness handoff](2026-09-01-canonical-correctness-and-migration-hygiene.md).
It is irrelevant to this suite, which needs only the database (provisioned
and migrated to head `20260902_02` before the gate). Observed on both
disposable projects during this plan; no action taken.

## Next actions

1. Branch `canonical-postgresql-baseline-repin` is ready for merge review;
   nothing else is pending.
2. Living references for operators: the repinned suite
   `tests/integration/test_canonical_postgresql_baseline.py`, the CI workflow
   `.github/workflows/canonical-postgresql-baseline.yml`, and
   [`BACKLOG.md`](BACKLOG.md) (row now retired).
