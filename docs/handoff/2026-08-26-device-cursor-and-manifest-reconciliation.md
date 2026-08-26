# Handoff — Device Cursor and Manifest Reconciliation (Child 6, Tasks 1-11)

Mid-plan handoff: Tasks 1-11 of
`docs/superpowers/plans/2026-08-26-device-cursor-and-manifest-reconciliation.md`
are implemented, reviewed and committed. Tasks 12-15 remain. The session stopped
here on user instruction ("dừng sau Task 11"), not on a blocker.

## Commit accounting

Branch `device-cursor-manifest-reconciliation` (created from `master` @
`41ab718`, per user instruction: new branch, no worktree — this overrides the
plan's Execution Discipline #2 worktree mandate).

Final implementation commit: `05a1c54`. The commit that carries this handoff is
its immediate successor; this file therefore records `05a1c54` as the last
implementation SHA by convention (the plan's Task 15 Step 7 uses the same
convention for its own final handoff).

Task commits (18 total, each task's brief at
`.superpowers/sdd/2026-08-26-device-cursor-and-manifest-reconciliation/task-N-brief.md`,
full reports as `task-N-report.md` alongside):

| Task | Commits | Review outcome |
|---|---|---|
| 1 domain/registry/diagnostics | `4bf367b` | clean (3 minors deferred) |
| 2 schema/migration | `a16c3d1`, `4f787be` | 1 fix round (state-shape constraint) |
| 3 event pull + cursors | `be3239d`, `ddf77e0` | 1 fix round (gap witness on total history loss) |
| 4 manifest planning/completion | `b344c7a`, `02b838a`, `03dd7e4` | 2 fix rounds (bind-param ceiling; active locator; golden digests) |
| 5 verified content | `a9fb340`, `172a56c` | 1 fix round (ruff format artifacts) |
| 6 HTTP API + correlation | `abb87d5` | clean |
| 7 trail v2 + diagnostics surface | `8f46dc1` | clean |
| 8 journal v7 + repository | `d602cf2` | clean (routing carry-forward) |
| 9 wire client + binary transport | `16ea66e` | clean |
| 10 remote apply + echo suppression | `3c0d528`, `29135f0` | 1 fix round (1 Critical + 2 Important) |
| 11 capture/barrier/reconciliation | `c463867`, `05a1c54` | 1 fix round (3 Important) |

## Gate status (offline evidence at `05a1c54`)

- Python: focused unit/contract suites green per task; `python-lint` and
  `python-type-check` (mypy strict) green through Task 11 (196 files at Task 6).
- Live PostgreSQL integration (disposable `knowledge-ci-*` projects, runbook
  `.local/RESTART.md` followed, `knowledge-local` stood down and restored each
  time): migration upgrade/downgrade, cursor/event transactions, manifest
  transactions, query plans, verified content — all PASS (Task 5 content used a
  verification-faithful double plus the scripted real-adapter contract suite;
  live-R2 bucket proof is allocated to Task 13 Step 6 `object-storage-test-live`).
- OpenAPI snapshot + generated workspace client regenerated at Task 6; the 7
  pre-existing snapshot failures from Task 1's registry growth are cleared;
  `api-contract-check` green; client `generate` + `type-check` green.
- Plugin: full vitest suite 1072/1072 (52 files) at `05a1c54`; `tsc --noEmit`,
  lint (`--max-warnings=0`), build all exit 0.
- Known red: repo-wide `poe python-format-check` fails on 9 files (Task 1-4
  drift; see deferred items). `poe python-lint` is green; no task gate between
  now and Task 13 Step 7 runs format-check, and Task 13 must clean it first.
- NOT run yet by design: Desktop WDIO journey, physical Mobile matrix,
  `object-storage-test-live`, `poe verify` full pass — owned by Tasks 13-15.
  Child 6 is NOT complete; no completion claim is made by this handoff.

## Spec-interpretation decisions (with rationale)

1. **Canonical-only download placement — the load-bearing open decision.**
   Spec §12.3/§12.4 require canonical-only `download` actions to deliver bytes
   through the remote-apply state machine (new-device onboarding and
   SQLite-loss-with-missing-files converge only through this channel, since
   post-completion pulls start after checkpoint C). The Task 6 action wire
   carries only `source_locator_id` (a locator row UUID, per spec §6.5) — no
   locator text — and `localEntryId` is null for canonical-only actions, so the
   plugin cannot know where to place bytes. Task 11 therefore settles
   canonical-only downloads as durable `device_manifest_identity_ambiguous`
   conflicts, surfaced through one `reconcileFailure("actions", reason)`
   observation that survives completion (fail-closed, readable, no wrong write).
   Ruling: the spec's behavior contract governs over the interface sketch; the
   wire must be extended. Fix routed as a dedicated dispatch on the Task 6
   surface BEFORE Task 13: add checkpoint locator text to the canonical-only
   download action payload (precedent: §7.1 events carry locator text on
   operational wires; §6.4 forbids it only in diagnostics/telemetry), fix
   `src/personal_os/device_sync/planning.py:268` so the per-entry catch-up
   download echoes its `local_entry_id`, regenerate OpenAPI/client, update the
   plugin mirror + reconciler, and amend spec §6.5/§7.3 in Task 14's docs pass.
   BACKLOG row added with `Before Child 6 Task 13`.
2. **Worktree override** — user directed branch-only; noted in SDD ledger line 2.
3. **OpenAPI snapshot sequencing** — Task 1 grew the central `ErrorCode` enum;
   snapshot regeneration deferred to Task 6 (repo precedent `2cbcd6b`→`b41f812`),
   leaving 7 known-red snapshot tests between Tasks 1-6 (cleared at `abb87d5`).
4. **`ManifestEntryResolution`** deliberately absent from Task 1 (shape depends
   on Task 4's `ManifestMatchKind`); added in Task 4's commit to `contracts.py`.
5. **Task 2 forced scope** — 12 files beyond the brief's git-add list were
   mechanically forced (head pins in six test files, `CANONICAL_POSTGRESQL_SCHEMA_REVISION`
   bump, recovery manifest v3→v4 with the v3 count set frozen); reviewer
   verified file-by-file. The `ck_manifest_runs__state_shape` constraint was
   restructured after review so `failed`/`expired` runs are writable in honest
   shapes (small-file-sync precedent grouping).
6. **Task 4 wire grammar** — canonical-JSON final digest
   `{"pages":[{"digest","entries","page"}…],"version":1}` (RFC 8785) is pinned
   by golden vectors on both server and plugin; the canonical-only downloads
   exclusion binds as ONE `unnest(:ids)` array parameter (65,535 bind ceiling);
   rule-2 (historical locator) actions carry the checkpoint-ACTIVE locator id.
7. **Task 5 policy denial** — surfaces as `ExclusionPolicyError(EXCLUSION_POLICY_DENIED)`
   (registered closed code) and passes unmetered through device-sync metrics
   (the metric label vocabulary is `DeviceSyncErrorCode`-only; metering it needs
   a Task 1 registry change). Reviewer accepted: the token reaches a readable
   surface at the route boundary.
8. **Task 6 correlation** — `diagnostics/events.py` deliberately unchanged:
   `request_id` reaches failed-request lines via the bound diagnostic context;
   the registry stays closed. The 2026-08-23 `request_id` BACKLOG row is NOT
   retired (waits for Task 15 live evidence) even though the remediation is
   implemented and pinned.
9. **Task 8→11 `reconcile_required` clearing** — Task 8's `completeRepair`
   could not clear `journal_meta.is_reconcile_required` (persistence sticky
   merge would re-clobber). Task 11 added `markReconcileComplete()` +
   `onDeviceSyncRepairComplete` (`journal/persistence.ts`, approved forced
   file), proven durable across close/reopen with a regression control.
10. **Task 9 transport** — dedicated `DeviceSyncHttpTransport` port instead of
    widening `SyncHttpResponse` (exact-shape assertions in `sync-api.test.ts`
    are outside the task's file list); `createObsidianDeviceSyncHttpTransport`
    wiring is owned by Task 12 (`plugin.ts`).
11. **Task 10 recovery classification** — created/restored with absent target
    after crash-at-prepared recovers `clean` (absent target is the verified
    pre-mutation expectation); updated-with-absent-target stays `blocked`
    (genuine divergence).
12. **Task 11 barrier semantics** — upload admission refusal →
    `blocked("actions", "journal_mutation_failed")`; synthetic-sequence lattice
    guard → `blocked("actions", "device_cursor_gap")`; barrier-paused outbound
    passes report `completed`; every settle-with-reason emits one closed
    observation so blockers stay readable after completion discards progress
    rows; echo markers swept at/below `acknowledgedSequence` at completion (no
    time-based expiry anywhere).

## Deferred items (each has exactly one BACKLOG row)

1. **Canonical-only locator wire gap** — see decision 1. `Before Child 6 Task 13`.
2. **Policy-rule EXCLUDED for unowned uploads** — locator-class
   (`folder_prefix`/`path_glob`/`extension`), `exact_source_id` and
   `source_type` rules cannot be evaluated at finalize for entries with no
   bound canonical source (raw locators never persist), so unowned uploads
   plan `EXCLUDED` (fail-closed; apply-time recheck and publication
   enforcement backstop). Durable fix needs a `device_sync` schema column for
   the append-time decision. `Before Child 8 conflict merge`.
3. **Format drift, 9 files** — Tasks 1-4 left `poe python-format-check` red
   (`device_event_store.py`, `device_manifest_store.py`, `planning.py`, six
   device-sync test files); Task 13 Step 7 runs format-check and must clean it.
   `Before Child 6 Task 13`.
4. **Per-task review minors (Tasks 1-11)** — parked in the SDD ledger for the
   final whole-branch review to triage; representative items: duplicate
   `DeviceEventType` `__all__` entry; dead fakes helpers; unbounded
   `ManifestAction.local_entry_id`; auth-gate parametrize 5/8 routes;
   mid-stream-failure-after-200 logs COMPLETED (pre-existing semantics);
   client-disconnect generator `aclose` relies on asyncgen finalization;
   double download per download action; Task 8 echo-conflict barrier parity
   and digest-after-validation ordering. `Before Child 6 whole-branch review`.
5. **Index candidates** — `(workspace_id, event_sequence)` pull index and
   `source_tombstones.restore_event_id` index; query-plan gates pass at pinned
   fixture size; sparsity matters at multi-workspace scale. `Before production
   activation`.

Rows 53/54/60/67 of the existing BACKLOG (the three triggered Child-6 hygiene
rows + their residual row) are remediated in code with test evidence (Tasks 6-7)
but stay until Task 15's live evidence, per the plan. The two metrics rows
(`_validate_epoch_ms`, `record_commit(COMMITTED)`) remain untouched per the
plan's global constraints.

## Next actions (in order)

1. Dedicated dispatch on the Task 6 surface for the canonical-only locator
   wire gap (decision 1) — includes OpenAPI/client regen, plugin mirror,
   reconciler placement, and removes the interim identity_ambiguous settle.
2. Task 12 (coordinator, cadence, repair command, status; wires
   `createObsidianDeviceSyncHttpTransport` and the vault seam in `plugin.ts`;
   add the settings/status composition surfaces).
3. Task 13 (cross-boundary journeys, privacy, performance; run
   `object-storage-test-live`; fix the 9-file format drift before its Step 7).
4. Task 14 (canonical docs incl. spec §6.5/§7.3 amendment from decision 1,
   runbook, release candidate 0.2.0, full offline verification).
5. Task 15 (Desktop WDIO + physical Mobile gates, BACKLOG retirement with
   evidence, rewrite this handoff as the final one).
6. Final whole-branch review (most capable model; triages deferred minors in
   the SDD ledger), then `finishing-a-development-branch`.

SDD workspace (ledger, briefs, reports, review packages):
`.superpowers/sdd/2026-08-26-device-cursor-and-manifest-reconciliation/` —
git-ignored scratch; `progress.md` is the authoritative per-task record.
