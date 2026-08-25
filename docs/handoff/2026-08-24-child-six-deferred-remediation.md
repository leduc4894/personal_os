# Child Six Deferred Remediation Handoff

**Date:** 2026-08-25 (plan opened 2026-08-24)
**Plan:** `docs/superpowers/plans/2026-08-24-child-six-deferred-remediation.md`
**Spec:** `docs/superpowers/specs/2026-08-24-child-six-deferred-remediation-design.md`
**Branch:** `child-six-deferred-remediation`, base `2001e05`. Implementation
range `2001e05..HEAD`:

- `49527c7` `fix: terminalize typed upload rejections` (Task 1)
- `523d0c1` `test: pin typed locator conflict wire landing` (Task 2)
- `bb80d1c` `fix: surface closed sync diagnostics tokens` (Task 3)
- `ecdb611` `docs: accept pinned ruff exception style` (Task 4)
- `f9d5ac3` `docs: record mobile lifecycle physical evidence` (Task 5)
- `6db2d9b` `test: pin typed rejection terminalization at the policy boundary` (Task 6 fix round)
- `5028233` `docs: hand off child six deferred remediation` (Task 6 closure)
- `354bf34` `fix: re-claim failed upload operations on the durable adapter` (final-review fix wave: the durable `_reserve_operation_once` now rotates `failed` rows exactly like expired-pending, converging with the offline composition and the pinned integration contract; committed rows still refuse)
- `e153458` `docs: reconcile deferred hygiene row and verdict line` (final-review fix wave; branch head at plan close-out; full `poe verify` re-run green at this head)

**Status: all ten final gates GREEN (§2). Mobile physical matrix executed
2026-08-25: 7/8 PASS on the physical device; explicit restore FAILS on the
pre-existing convergence/lifecycle lane race — the Mobile acceptance row
stays DEFERRED by explicit user ruling, and the race carries its own new
BACKLOG row (`Before next plugin release`).**

Living operational status: `docs/operations/sync-error-tracing.md` (Task 3
runbook) and `docs/operations/source-locator-tombstone-lifecycle.md` (Mobile
deferral record + sanitized physical observation). Ledger with per-task
evidence: `.superpowers/sdd/2026-08-24-child-six-deferred-remediation/progress.md`.

## 1. What was built (one paragraph per task)

- **Task 1 — typed-rejection terminalization (`49527c7`).** A typed
  non-retryable 4xx raised after the receive claim no longer strands the
  canonical operation row at `receiving`: `record_bound_terminal_failure`
  (port + durable adapter behind the operation advisory lock + offline
  implementer) writes the guarded `receiving -> failed` transition with only
  the registry token in `safe_error_code`, idempotently per bound/code pair,
  re-raising the identical error object. RED/GREEN: adapter collection error
  + service `'receiving' != 'failed'` at RED; 190 passed GREEN; full unit
  sweep 2772 passed. Physical-device observation: the mobile explicit-restore
  upload landed as `failed`/`safe_error_code=source_locator_conflict` and the
  untitled-transit 409s terminalized the same way — the closed state is
  readable on the server surface exactly as designed.
- **Task 2 — narrowed classification + shared wire corpus (`523d0c1`).**
  `classify_database_failure` gained the closed `INTEGRITY` kind for `23xxx`
  SQLSTATEs (class prefix + exception shape only, never message or constraint
  name); `map_database_failure` maps it to redacted non-retryable
  `internal_error`, so foreign integrity violations are never the retryable
  `source_commit_outcome_unknown`. RED pinned 23505 → retryable (the removed
  branch); GREEN 42 passed across error-mapping + wire contract. The corpus
  (`wire-golden.json`) gained `content_source_locator_conflict` (409,
  `source_locator_conflict`, non-retryable, plugin landing `blocked_conflict`)
  with the registry hash recomputed; TS replay pins the client mapping and
  the queue-driver trail entry is exactly
  `wire_failure · blocked_conflict · source_locator_conflict · request_id=<canonical UUID>`.
  Physical-device observation: that exact trail triple with a canonical-UUID
  request id was observed on the phone (explicit-restore and untitled-transit
  scenarios).
- **Task 3 — mandatory diagnostics surfaces (`bb80d1c`).** Constructor UUID
  gate on the opaque envelope `request_id` (only canonical lowercase-hex UUIDs
  become tokens; rejects occur before any entry or persist); the settings/export
  inputs closed to the existing closed-token unions (no `string[]` escape
  hatch remains on the diagnostics path); duck-typed `syncFailureKind` replaced
  by `instanceof` narrowing; the queue-driver `#handleFailure` comment no
  longer overclaims which failures reach the hook; 999-append-failure
  saturation documented (constant, probe, runbook); one bounded
  `self_check · trail_persist_failed` marker per persist-failure episode rides
  the next successful persist; the `Copy sync diagnostics` command carries a
  closed-token rejection handler. RED across trail/export/self-check/queue +
  copy handler (stash-proven); GREEN 120 passed focused, 715 passed full
  plugin suite, tsc + eslint clean. Physical-device observation:
  Copy-sync-diagnostics exports on the phone showed closed tokens, counts,
  UUID request ids and 0 append failures (scenario 4).
- **Task 4 — PEP 758 ruling (`ecdb611`).** `uv run poe python-format-check`
  is authoritative: the pinned py314 Ruff output (`except A, B:` unparenthesized
  where legal) is the accepted style; the check passes at 465 files formatted
  with zero formatter diff. The 2026-08-23 `tooling-style` BACKLOG row was
  retired in that commit; `docs/operations/sync-error-tracing.md` needed no
  change (grep-proven: no style discussion exists there).
- **Task 5 — Mobile physical matrix (`f9d5ac3`).** Executed 2026-08-25 with
  the user on iPhone against the disposable `knowledge-ci-child-six-mobile`
  stack (7 vaults attempted, 5 used). 7/8 scenarios PASS with canonical
  evidence; explicit restore FAILS on the pre-existing convergence/lifecycle
  race (§3). Deliverables: the living doc's Mobile deferral Reason rewritten
  naming the race, a sanitized physical-observation subsection
  (`operator-record:mobile-live-20260825`), and the new 2026-08-25 BACKLOG
  race row linking here. Contract:
  `tests/contract/source_lifecycle/test_reference_device_records.py`
  (5 passed, `-m device_records`).

## 2. Final gates (Task 6, fix round — all exit 0)

| # | Command | Exit | Summary |
|---|---------|------|---------|
| 1 | `uv run pytest tests/unit/postgresql_source_store/test_small_file_sync_operations.py tests/unit/postgresql_source_store/test_error_mapping.py tests/unit/postgresql_source_store/test_publication_store.py tests/contract/small_file_sync/test_wire_contract.py tests/contract/source_lifecycle/test_reference_device_records.py -q` | 0 | 89 passed, 5 deselected (device-records marker set), 1 warning |
| 2 | `pnpm --dir apps/obsidian-plugin exec vitest run` | 0 | 42 files, 715 tests passed |
| 3 | `pnpm --dir apps/obsidian-plugin exec tsc --noEmit` | 0 | no errors |
| 4 | `pnpm --dir apps/obsidian-plugin run build` | 0 | dist rebuilt (ignored artifacts, unstaged) |
| 5 | `pnpm --dir apps/obsidian-plugin run lint` | 0 | eslint `--max-warnings=0` clean |
| 6 | `uv run poe python-format-check` | 0 | 465 files already formatted |
| 7 | `uv run ruff check src apps tests packages` | 0 | All checks passed |
| 8 | `uv run mypy src apps/api/src packages/postgresql-source-store/src` | 0 | Success: no issues in 154 files |
| 9 | `uv run poe verify` | 0 | full offline gate green: format/lint/type/boundary checks, `1 failed, 3483 passed` → fixed → `3484 passed, 21 skipped, 398 deselected`, then python+TS builds |
| 10 | `git diff --check` + `git status --short` | 0 / 0 | no whitespace errors; only the intended staged files |

Gate history (recorded per the fix-round ruling): the FIRST `poe verify` run
failed in its `test` subtask — `tests/integration/small_file_sync/test_policy_and_device_boundaries.py::test_irrelevant_locator_revision_reauthorizes_claimed_exact_token_once`
pinned the superseded same-token rebind contract (409
`small_file_upload_state_invalid` + synchronous rebind of the claimed token).
Bisect attribution: passes at base `2001e05`, fails from `49527c7` — Task 1's
deliberate terminalization. Controller ruling: spec §D1 governs ("a typed
rejection must never leave its operation row at receiving"); the integration
test was updated in the fix-round commit to pin the terminal contract (403 →
row `failed` with `safe_error_code=exclusion_policy_indeterminate` → fresh
claim at the changed revision → fresh token commits exactly once). Final run
green as recorded above.

## 3. Mobile status

- **7/8 physical PASS** (canonical + device evidence, sanitized log
  `operator-record:mobile-live-20260825`): tracked rename; tracked move;
  delete (exactly one new open tombstone); **proven automatic restore — the
  environment's first committed restore event** (device Status Ready,
  `pass_outcome · completed` trail entries with UUID request ids, 0 append
  failures; open-tombstone count 4→3); offline capture/reconnect
  (`Offline — queued (1)` → committed immediately after WiFi); unload/reload
  (queued event survived app swipe-kill and drained on reload with a clean
  post-reset startup snapshot); policy-denied transition (plugin-side
  enforcement — no upload attempted — with the closed `Policy blocked`
  status line and 304-cached policy snapshot, queue unaffected).
- **Explicit restore FAIL** — the pre-existing convergence/lifecycle lane
  race: the content/convergence lane ships the restored bytes as a NEW source
  before the restore lifecycle event lands, so the server rejects the restore
  with the closed conflict family. Closed-token evidence on both surfaces:
  device trail `wire_failure · blocked_conflict · source_locator_conflict ·
  request_id=<canonical UUID>` then the documented `Reconcile required (3)`
  hard stop; server 401 (auto-refreshed) then 409 closed conflicts with the
  upload terminalized `failed`/`source_locator_conflict`. No delete commit,
  no tombstone. This is also, per controller-established forensics, what
  breaks the mandatory Desktop WDIO journey after delete: the 2026-08-24/25
  bootstrap runs failed `obsidian_wdio_failed_after_delete` twice, with DB
  forensics showing the restored-bytes create committing ~200 ms BEFORE the
  restore event POST that is then rejected 409 (3 journeys → 0 restore events
  committed, 3 fresh sources at restore paths, 6 uploads terminalized
  `failed/source_locator_conflict`). That forensics is the provenance for
  the living doc's claim that the same race fails the Desktop journey.
  Not caused by Tasks 1–3 (no lane/modal/convergence code touched; identical
  failure on pre-Task-3 and fresh dist).
- **User ruling:** option (b) — run the matrix now, record real outcomes,
  keep the Mobile row DEFERRED, no PASS claim. The 2026-08-21
  `source-lifecycle-mobile-acceptance` row is retained unchanged
  (`Before Child 6 acceptance closure`); the race carries the new 2026-08-25
  `small-file-sync` row with `Implement by: Before next plugin release`.

## 4. Operational findings from the mobile session

- **Untitled-name transit race:** Obsidian Mobile creates new notes under the
  locale-default untitled name before the user names them; the convergence
  lane ships that default name, and when the locator is already taken the
  upload 409s and the journal hard-stops. Workaround that produced scenarios
  6–8: pre-name the file via the Files app, reset plugin data, rely on the
  startup snapshot.
- **One vault must live on exactly one device:** an iCloud-replicated vault
  opened on desktop + phone double-admits every file; the second journal
  always conflicts (fourth manifestation of the ship-before-settled race
  family).
- **iOS onboarding requires returning to the app right after browser
  approval** (background poll suspension).
- **An empty note queues until content is typed.**

## 5. Decisions and interpretations

- `receive_content()` in the Task 1 brief maps to the repository's existing
  `SmallFileSyncService.receive()`; no parallel method was invented.
- The offline composition's `OfflineSmallFileUploadOperationStore` (not in
  the brief's file list) had to implement the new port method or every
  offline-stack typed rejection would answer 500 — included in `49527c7`.
- Corpus entry added in the established schema (`name`/`surface`/`status`/
  `body_text`/`plugin_expectation`) carrying the brief's pinned values; both
  consumers are typed against that schema.
- The Python replay of the new corpus entry is corpus-only (route-unreachable
  in the offline harness: its publication double models no locator unique
  index); the served 409 envelope is pinned cross-language by the corpus
  bytes + TS replay, and the 409 status mapping by `test_http_errors.py`.
- Task 3's four extra files (sync-api.ts rename, settings-tab.ts closed
  types, plugin.ts/plugin.test.ts copy handler) are forced by the brief's own
  Steps 3–4; `trail_persist_failed` reuse for the copy rejection is
  plan-sanctioned with no consumer collision.
- Task 5 user ruling: option (b) above; row 53 (Mobile acceptance) retained.
- **Fix-round ruling (Task 6):** spec §D1 governs — the 403
  `exclusion_policy_indeterminate` is a typed non-retryable 4xx, Task 1's
  terminalization is correct, the integration test pinned a superseded
  contract and was updated to the terminal contract; the "terminal
  STATE_FAILED" BACKLOG retirement stands (see §2 gate history).

## 6. Deferred items and verdicts (indexed in `docs/handoff/BACKLOG.md`)

BACKLOG rows retired by this plan (evidence-backed): the three 2026-08-23
`sync-error-tracing` hygiene batches (Task 3), the 2026-08-23 `tooling-style`
row (Task 4, `ecdb611`), the 2026-08-24 `small-file-sync` terminal
`STATE_FAILED` row (Task 1), the 2026-08-24 `small-file-sync` `error_mapping`
23xxx row (Task 2), the 2026-08-24 `wire-contract` corpus row (Task 2).

Retained rows (each exactly once, unchanged):

- **Convergence/lifecycle lane race** — NEW 2026-08-25 `small-file-sync` row,
  `Implement by: Before next plugin release`. Blocks both the Desktop WDIO
  gate and Mobile explicit restore.
- **`source-lifecycle-mobile-acceptance`** — 2026-08-21 row retained
  DEFERRED, `Before Child 6 acceptance closure`; pinned by
  `tests/contract/source_lifecycle/test_reference_device_records.py`.

Ledger deferred minors (one line each; no BACKLOG rows — controller ruling):

- Task 1: fake-store identity fence checks only workspace/device (test-double
  fidelity); refused-write branch of `_persist_typed_rejection` lacks a
  focused unit test; failure write keeps `normalized_locator` (failed rows
  never hydrate into binding).
- Task 2: `test_policy_drafts.py` retry test name now stale (behavior
  correct); TS replay pins `failureKind` landing but no trail assertion
  (harness returns only status/bodyText).
- Task 3: runbook "Reading the verdicts" stale after token widening (copy
  rejection can emit the token with counter 0 — one-line fix);
  `#diagnosticTrail === null` copy-failure branch swallows silently (no
  surface exists); `trail_persist_failed` has three sources distinguished
  only by runbook context; copy-handler test is source-contract matching;
  Step 4 queue test adds `settleTrailPersist()` to the brief's verbatim body.
- Task 4: the PEP 758 ruling's durable record was git history + commit
  message only — now also §5 of this handoff.
- Task 5: the living doc's WDIO-desktop-fails provenance lands here (§3 —
  resolves the ledger minor); "listed by the restore picker" phrasing
  slightly overstates the observed device evidence.

## 7. Next actions

1. **Fix the convergence/lifecycle lane race** (BACKLOG 2026-08-25 row,
   `Before next plugin release`) — it blocks both the mandatory Desktop WDIO
   journey and the Mobile explicit-restore scenario.
2. After the race fix, **re-run the physical mobile matrix** to convert the
   Mobile DEFERRAL to PASS and retire the `source-lifecycle-mobile-acceptance`
   row (following the documented operator procedure the living doc links).
3. **Restore the ordinary `knowledge-local` stack** if not already done — the
   disposable `knowledge-ci-child-six-mobile` stack may still be running; the
   next session should follow `.local/RESTART.md` (`uv run poe stack-status`
   first) to stand down the CI project and bring back the standard stack.
