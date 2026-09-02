# Source Conflict Capture and Resolution — Handoff

- **Plan:** `docs/superpowers/plans/2026-09-02-source-conflict-capture-and-resolution.md`
- **Spec:** `docs/superpowers/specs/2026-09-02-source-conflict-capture-and-resolution-design.md`
- **Branch:** `source-conflict-capture-and-resolution` (from `master` @ `937508b`)
- **Final code SHA:** `65e7714` (+ docs-only follow-ups through the merge head)
- **Status:** COMPLETE for all automated gates — Tasks 1-10 done, every
  non-mobile gate PASS. The operator-backed manual Desktop Conflict Inbox
  journey was **deferred to BACKLOG by operator decision (2026-09-02)**;
  the branch merged to master without it. The journey remains the
  outstanding live-evidence obligation before the Conflict Inbox is
  considered live-proven (BACKLOG row with milestone).

## Gate status (evidence)

| Gate | Result | Evidence |
|---|---|---|
| `uv run poe verify` | PASS (exit 0) | python 4605 passed / 21 skipped; obsidian-plugin 1441 passed (64 files, incl. the 8 conflict E2E journeys); web 163; all builds green. Full log tails in `.superpowers/sdd/2026-09-02-source-conflict-capture-and-resolution/task-10-report.md` §7 |
| `uv run poe api-contract-check` | PASS (exit 0) | `api_contract_current`; OpenAPI snapshot + generated client regenerated for the two new routes (`uploadSmallFileConflictContent`, `uploadSourceConflictResolutionCandidate`) |
| `CI=true bash .local/serve-live-ci.sh up knowledge-ci-source-conflicts-20260902` | PASS (exit 0) | api ready, web-admin ready (38000), tunnel `knowledge-api-verify` ready. Two bootstrap branches were diagnosed first — see Decisions #4 |
| `CI=true LOCAL_STACK_TEST_PROJECT=knowledge-ci-source-conflicts-20260902 uv run poe device-sync-test` | PASS (exit 0) | 1859 passed / 2 skipped in 17m11s |
| Conflict live-stack suites (`tests/integration/source_conflicts -m local_stack`) | PASS (exit 0) | 26 passed incl. the 7 race tests (`test_resolution_races.py`) |
| Plugin `vitest run` / `type-check` / `lint` / `build` | PASS (all exit 0) | 1441 tests; `dist/main.js` built |
| Manual Desktop Conflict Inbox journey | **DEFERRED by operator decision (2026-09-02)** | Runbook section ready (`docs/operations/source-conflict-resolution.md`); BACKLOG row added with `Before Child 9 operations acceptance`; no manual gate claimed |
| Physical Mobile matrix | Not attempted by design | Existing Child 9 backlog rows own it (see BACKLOG mobile-live rows) |

Pre-existing master integration failures did not surface in any selected
gate run and were not touched. Verified by the controller at the branch
base `937508b` (spot-check runs on fresh disposable `knowledge-ci-*`
projects, before any branch commit) with identical failure modes and
counts, so they are master defects, not branch regressions. Two classes:

1. **Lifecycle policy-expectation drift** —
   `tests/integration/source_lifecycle/test_lifecycle_transactions.py`
   (5 failed: `assert 'upsert' == 'delete'` — the store's locked-policy
   re-evaluation over the seeded allow-all policy overrides the tests'
   denied decisions) plus one `test_projection_dispatch.py` failure.
   Owning domain: source-lifecycle integration suites.
2. **Outdated integration seed helpers** —
   `tests/integration/source_lifecycle/test_backup_restore.py`
   (5 failed: seeds `projection_intents` rows without their
   `sync_events` parents → FK violation) and `test_query_plans.py`
   (6 setup errors of the same class). Owning domain: lifecycle/backup
   integration test fixtures.

Both are deferral category (a) — owned by their domains, outside this
plan's file scope; BACKLOG rows added with milestones.

## What landed (Task 10; Tasks 1-9 in their task reports)

1. `982816b` — A1+A3: `PUT /api/uploads/{operation_id}/conflict-content`
   (capture over HTTP, publication route untouched) + `edit_remote_delete`
   capture for the missing-source preflight branch with capture-time
   deletion re-validation; wire-golden `conflict_capture` corpus entry.
2. `55cc8c4` — A2: `PUT /api/sync/conflicts/{conflict_id}/candidate`
   (digest/media-type headers, policy recheck before bytes, verified
   admission, content-addressed) — the `save_merged` upload half.
3. `504cbc5` — plugin bindings: conflict grant upload in the journal lane
   (`blocked_conflict` only after the capture settles), conflict outcome
   grant/identity parsing, `uploadResolutionCandidate` client, the REAL
   verified-candidate uploader replacing the Task 9 interim.
4. `91f2208` — `edit_remote_delete` offers only `keep_remote` (never an
   unappliable publishing choice onto a deleted source).
5. `71b5324` — races / privacy contract / table metadata / plugin E2E spec
   (`test/specs/source-conflict-resolution.e2e.ts` in the vitest include).
6. `7535887` — operations runbook `docs/operations/source-conflict-resolution.md`
   + tombstone-runbook cross-link + `docs/README.md` operations section.
7. `f1bf44f` — sanctioned the Child 8 surfaces in the no-public-API and
   no-policy-bypass guards (pre-existing failures since Task 6/9, found by
   the final `poe verify`).
8. `d83209e` + `65e7714` — test stabilization (real-onload deadline) and
   race-fixture fixes (unique salts, seeded version counts).

## Decisions (interpretations of spec/brief, with reasons)

1. **Capture upload as a sibling route** — surfacing a capture receipt on
   the frozen `ApiEnvelope[SmallFileTerminalResultData]` upload route
   would change a frozen contract (Task 6 ruling); the sibling
   `/conflict-content` route keeps both wire shapes closed.
2. **A2 as a direct conflict-keyed upload with declared headers** — the
   carry hint's "new resolution-candidate operation" would need a
   synthetic journal-event preflight identity (a resolution candidate is
   not a journal event); the direct route follows the same verified-object
   discipline (bounded ceiling, digest verification, policy recheck before
   bytes, hash-keyed admission). Orphan admitted objects are
   content-addressed and GC stays excluded by the plan.
3. **Two-resolvers mandated snippet adapted** — with the Task 1-3 store a
   two-fresh-identity gather always ends with the loser reading a terminal
   row (typed `source_conflict_state_invalid`, spec §7 row 4 "no second
   winner"), so `any(STALE_SUCCESSOR)` cannot hold there; the mandated
   test name/gather/"at most one winning version" are kept, the
   same-identity replay race and the remote-advance stale-successor race
   are pinned separately in the same file.
4. **Fresh-CI bootstrap branches (environment)** — (a) a stale
   `.local/run-serve.py` held port 8000 (RESTART.md's stop-old-first rule);
   terminated. (b) a fresh CI database has no identity/policy keyset, so
   the API refuses startup (`exclusion_policy_not_initialized`); seeded
   the DB-level half (`canonical_core_operations.py bootstrap-identity`,
   `personal-api policy-key initialize`) per the acceptance-bootstrap
   chain, after which `serve-live-ci.sh up` passed. The operator round
   still runs the HTTP half (TOTP + policy publish) via
   `tools/obsidian_live_acceptance_bootstrap.py`.
5. **Binary safe-info panel (spec §5.2.2) — code stands without it.** The
   Task 6 wire contract renders only opaque identifiers and closed labels;
   adding size/hash members to the detail did not meet the Task 10 bar.
   Deviation ruling recorded in the runbook; the binary journey shows the
   two whole-object choices with no editor (E2E-pinned).
6. **Resolve-path policy recheck at the service boundary (spec §5.2.4).**
   The spec's canonical transaction lists the active policy among what the
   resolver validates "in one canonical transaction"; the implementation
   rechecks the reviewed remote version and the current source state inside
   the store transaction, while the exclusion-policy recheck runs at the
   service boundary over the row-locked conflict read (the store port
   carries no policy evidence). Reason: widening the Task 1/2 store ports
   to carry in-transaction policy evidence was ruled out to keep the store
   port closed. Residual risk: a sub-second window where a policy revision
   published between the guard's allow and the store commit lets one
   resolution through, caught fail-closed at the next boundary that
   re-evaluates policy (the evidence read, the next capture, the next
   resolution). The tradeoff is documented honestly in
   `src/personal_os/source_conflicts/service.py` (module docstring); the
   lifecycle domain does recheck policy in-transaction, and optional parity
   hardening with its approach is a future task.

## Deferred items (verdicts)

| Item | Verdict |
|---|---|
| Manual Desktop Conflict Inbox journey | Deferred by operator decision (2026-09-02); BACKLOG row with milestone added — see the runbook for the full procedure and the BACKLOG row for the six extra final-review exercises |
| Physical Mobile acceptance matrix | Out of scope by plan (Child 9 gate); existing BACKLOG mobile-live rows own it — no new row |
| Candidate GC / Web conflict UI / cursor-gap remediation | Out of scope by plan self-review; no row (owned by their own future plans) |
| Sibling-orphan cleanup sweep (failed-apply staging siblings) | Out of Task 10's tested behavior; BACKLOG row added (maintenance) |
| Shared stage/verify/replace core extraction (device-sync writer vs conflict applier) | Refactor-only; BACKLOG row added (maintenance) |
| Pre-existing master failures: lifecycle policy-expectation drift (`test_lifecycle_transactions` 5 + one `test_projection_dispatch`) | Out of scope category (a) — source-lifecycle domain owns it; verified failing identically at base `937508b`; BACKLOG row added |
| Pre-existing master failures: outdated seed helpers (`test_backup_restore` 5 + `test_query_plans` 6 setup errors) | Out of scope category (a) — lifecycle/backup integration fixtures own it; verified failing identically at base `937508b`; BACKLOG row added |
| `knowledge.source_conflicts` absent from the backup manifest's `SNAPSHOT_LOCK_ORDER` (35 tables, v4) | Out of Task 2's file scope (backup domain v4 pinned by its own tests); BACKLOG row with milestone |

## Next actions

1. Operator runs the Desktop Conflict Inbox journey per the BACKLOG row
   and `docs/operations/source-conflict-resolution.md` against a live CI
   project (re-stand with `CI=true bash .local/serve-live-ci.sh up
   knowledge-ci-source-conflicts-20260902`; run
   `CI=true uv run python tools/obsidian_live_acceptance_bootstrap.py
   --project-name knowledge-ci-source-conflicts-20260902 ...` first if the
   round wants TOTP+policy seeded — or just its bootstrap phases).
   Record sanitized evidence (outcome, reason token, count, timestamp);
   include the six final-review exercises listed in the BACKLOG row.
2. After the journey: Codex verifies the API checkpoints (conflict
   resolved, exactly one winning version, zero open conflicts), then
   `bash .local/serve-live-ci.sh down` (leaves `knowledge-local` down),
   and removes the BACKLOG row.
