# Explicit restore target reservation — implementation plan

**Date:** 2026-08-25
**Spec:** `docs/superpowers/specs/2026-08-25-explicit-restore-target-reservation-design.md`
**Scope:** `apps/obsidian-plugin` only (journal schema v6, lifecycle
repository/capture, content-capture deferral, restore command, diagnostics
tokens, WDIO journey restage, living docs). No server, wire-corpus, OpenAPI,
generated-client or Python change.
**Risk:** medium — the restore command's operator procedure changes (reserve
at prompt, stage between prompt and confirm, cancel-only release); the
journal schema bumps v5 → v6. Containment: strict TDD per module, full plugin
gate after every task, `poe verify` at close, the Desktop WDIO journey and
the physical mobile matrix are the live gates.

## Tasks

### Task 1 — journal schema v6 (`restore_prior_path`)

- RED: `sqlite-database.test.ts` — a v5 database upgrades to v6 losslessly;
  v6 `local_files` reads back `restore_prior_path = null`; fresh creation
  carries the column; version constant is 6.
- GREEN: bump `JOURNAL_SCHEMA_VERSION`, add the column to DDL + the v5→v6
  upgrade path, extend `LOCAL_FILE_COLUMNS`.
- Gate: `pnpm --dir apps/obsidian-plugin exec vitest run src/journal/sqlite-database.test.ts`.

### Task 2 — lifecycle repository reservation, release, rebind, listing

- RED (`lifecycle-repository.test.ts`):
  - `reserveRestoreTarget` happy path: tombstoned row → rebound to target,
    state `restore_pending`, `restore_prior_path` = prior path, tombstone
    retained; re-reservation from `restore_pending` preserves the original
    prior path.
  - refusals: occupied tracked row (`restore_target_occupied`), in-flight
    phantom (`restore_target_busy`), non-terminal restore event
    (`restore_already_pending`), unrestorable row (closed
    `journal_mutation_failed`).
  - queued-phantom release: phantom row's `queued` create frozen
    `deferred_lifecycle` and the row released inside the same transaction.
  - `releaseRestoreTarget`: atomic return to prior path + `tombstoned`,
    prior cleared.
  - `readRestorableLocalFileIds`: lists `tombstoned` and `restore_pending`
    rows with an open tombstone (rename from
    `readTombstonedLocalFileIds`).
  - `recordLifecycleEventInSession` restore: record-time state
    `restore_pending` (not `restored`).
  - `recordLifecycleCommittedReceipt` restore: rebinds `normalized_path` to
    the operands' `target_locator` in the same transaction and clears
    `restore_prior_path`.
- GREEN: implement in `lifecycle-repository.ts`.
- Gate: focused vitest file green.

### Task 3 — lifecycle capture + content-capture deferral

- RED (`lifecycle-capture.test.ts`):
  - `reserveRestoreTarget` delegates and returns the prior path;
    `requestRestore` refuses an unreserved row (closed
    `journal_mutation_failed`) and confirms on a reserved one without eager
    tombstone consumption;
  - `detectAutomaticRestore`: refuses a `restore_pending` row; no eager
    `consumeRestoreSuccessor` (state advances only through the committed
    receipt);
  - `captureDelete` / `captureRename` on a `restore_pending` row: quiet
    `null`.
- RED (`capture.test.ts`): `#admitNormalizedPath` defers a
  `restore_pending` row on both the settle path and the automatic snapshot
  (no create minted, no auto-restore attempted); `#isLifecycleDeferredPath`
  covers it.
- GREEN: implement in `lifecycle-capture.ts`, `capture.ts`; add
  `SyncRestoreRefusalToken` to `lifecycle-contracts.ts`.
- Gate: focused vitest files green.

### Task 4 — command flow + diagnostics surface (mandatory diagnostics task)

- RED (`plugin.test.ts`): command order — prompt accept reserves before any
  staging; confirm records + requests one bounded pass; refusals surface the
  closed Notice text and one `journal_failure` trail entry with the refusal
  token; Cancel releases; dismissal retains; a failed reservation
  persistence appends `restore_reservation_persist_failed`.
- RED (`sync-diagnostics-trail.test.ts`): the three refusal tokens and the
  persist token type-check into the closed union and render in the export.
- GREEN: wire `plugin.ts` (reserve → stage → confirm → pass; Notice texts
  closed and path-free), extend `SYNC_COMPOSITION_READ_FAILURE_TOKENS`,
  mirror the refusal list into `SyncDiagnosticClosedToken`.
- Gate: `pnpm --dir apps/obsidian-plugin exec vitest run` (full plugin
  suite), `tsc --noEmit`, `run lint`, `run build`.

### Task 5 — WDIO journey restage, docs, backlog, handoff

- Restage `test/specs/source-lifecycle.e2e.ts`: create the restored file
  between the prompt accept and the confirm click via `browser.execute`;
  drop the `disablePlugin`/`enablePlugin` dance; canonical assertions
  unchanged.
- Update `docs/operations/source-locator-tombstone-lifecycle.md`: the
  explicit-restore procedure (reserve → stage → confirm), the
  `restore_pending` row wording, the new refusal tokens, the mobile staging
  procedure; update `plugin-journal-small-file-sync.md` only if the
  convergence deferral wording needs it.
- Run `uv run poe verify` (offline gate). Retire the 2026-08-25
  convergence/lifecycle-lane-race BACKLOG row only after the live gates
  pass (Task 6); write the handoff
  `docs/handoff/2026-08-25-explicit-restore-target-reservation.md`.

### Task 6 — live gates (user-participation rounds)

1. Restore the ordinary `knowledge-local` stack per `.local/RESTART.md`
   (`uv run poe stack-status` first; stand down any leftover
   `knowledge-ci-*` project).
2. Guarded Desktop WDIO run through
   `tools/obsidian_live_acceptance_bootstrap.py` on a disposable
   `knowledge-ci-*` project → `obsidian_live_acceptance_passed`.
3. Physical mobile matrix with the operator (iPhone): the eight scenarios
   with the new explicit-restore staging procedure; record sanitized
   evidence in the living doc; flip the Mobile record to PASS and retire
   the `source-lifecycle-mobile-acceptance` BACKLOG row.
4. Clean shutdown of every spawned process; no leftover CI project.

## Verification commands (offline)

```
pnpm --dir apps/obsidian-plugin exec vitest run
pnpm --dir apps/obsidian-plugin exec tsc --noEmit
pnpm --dir apps/obsidian-plugin run lint
pnpm --dir apps/obsidian-plugin run build
uv run poe verify
```
