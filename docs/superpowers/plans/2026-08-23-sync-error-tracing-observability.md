# Sync error tracing and observability plan

Spec: `docs/superpowers/specs/2026-08-23-sync-error-tracing-observability-design.md`.
Branch: continue on `codex/automatic-vault-convergence` (user-approved; the
trail also finishes the convergence plan's open park diagnosis).

## Global Constraints

- Closed vocabularies only in every trail entry, renderer, export block and
  server route: existing enums (`QueuePassOutcome`, `JournalSafeErrorLabel`,
  `JournalStoreErrorReason`, sync failure kinds, status kinds, lifecycle run
  outcomes, fixed self-check verdicts) plus opaque `request_id` tokens. A
  free-form string must not be able to enter the trail at the type level.
- No paths, digests, content, credentials, tokens beyond opaque request ids,
  hostnames or raw error messages on any surface. Source-contract tests pin
  this per surfaced module.
- The trail observes only: no sync/journal semantic changes, no daemons, no
  periodic timers. Persistence is event-driven, serialized, bounded (128
  entries).
- No third-party egress. Everything stays on-device or on the local API.
- Server API additions follow the repo contract: OpenAPI, contract tests,
  generated client freshness, docs.
- The six pre-existing uncommitted WIP files of the convergence plan stay
  untouched and unstaged.

## File structure

- New: `apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts` (+ test)
- Modify: `apps/obsidian-plugin/src/journal/queue-driver.ts` (trail taps),
  `src/journal/persistence.ts` (publish-failure tap), `src/journal/sync-api.ts`
  (request_id exposure), `src/plugin.ts` (composition, commands, snapshot),
  `src/authentication/settings-tab.ts` (trail tail rendering), plus their tests.
- Server (Task 4): `apps/api/src/api_runtime/` route + composition,
  `src/personal_os/small_file_sync/metrics.py` (bounded rejection ring),
  OpenAPI export, contract tests under `tests/contract/`.
- Docs (Task 5): `docs/operations/` tracing runbook; handoff closure.

## Task 1: Durable closed-token diagnostic trail, wired into the live seams

**Files:** new `src/journal/sync-diagnostics-trail.ts` + test; modify
`queue-driver.ts`, `persistence.ts`, `sync-api.ts`, `plugin.ts` + tests.

- [ ] Step 1 — RED: trail module tests: append/evict at 128; sidecar persist
  via a fake file store (write → reload → entries survive); corrupt sidecar
  → reset + `trail_reset` entry; closed-vocabulary type test (a free-form
  string fails tsc); append-failure swallow + bounded failure counter.
- [ ] Step 2 — RED: seam-wiring tests: one queue pass with a scripted 403
  HTML transport records `wire_failure` (kind label) + `pass_outcome`
  (`retry_scheduled`); a store-error mid-pass records `journal_failure`
  (closed reason); a publish-failure injection records `publish_failure`;
  the wire entry carries the envelope `request_id` when the server sent one
  (success path too, sampled: one entry per request outcome).
- [ ] Step 3 — Implement: the trail module; wire taps at `#handleFailure`
  classification, `#runPass` outcome, persistence publish catch, sync-api
  envelope `request_id` propagation; compose in `#startJournalCapture`
  (sidecar name `sync-diagnostics-trail.json` via the vault adapter). The
  round-5 rings remain; the trail mirrors them.
- [ ] Step 4 — Gates: `pnpm exec vitest run && pnpm exec tsc --noEmit && pnpm run build`.
- [ ] Step 5 — Commit: `feat: record a durable closed-token sync diagnostics trail`.

## Task 2: Copy-sync-diagnostics command and settings surface

**Files:** `plugin.ts`, `settings-tab.ts` + tests (new command test file if
the house style requires source contracts).

- [ ] Step 1 — RED: export-builder tests produce the sanitized block (status
  line, diagnostics line, counts, trail tail) and forbidden-substrate tests
  pin no path/credential shapes; clipboard-failure fallback modal.
- [ ] Step 2 — Implement: `Copy sync diagnostics` command (clipboard, modal
  fallback); settings section renders last 5 trail entries + total count +
  trail-append failure counter; generalized stop-reason tokens derived from
  the trail in the snapshot.
- [ ] Step 3 — Gates + commit: `feat: export sanitized sync diagnostics`.

## Task 3: Run-sync-self-check command

**Files:** `plugin.ts`, new `src/journal/sync-self-check.ts` + tests.

- [ ] Step 1 — RED: self-check tests — trail persist probe verdict,
  credential-presence verdict, origin-reachability closed verdict; each
  appends a `self_check` trail entry; never mutates sync state; no retry
  loop.
- [ ] Step 2 — Implement + wire the command with a summary notice.
- [ ] Step 3 — Gates + commit: `feat: add a bounded sync self-check command`.

## Task 4: Server admin sync-diagnostics route

**Files:** `apps/api/src/api_runtime/` (route + composition),
`src/personal_os/small_file_sync/metrics.py`, OpenAPI export, contract tests.

- [ ] Step 1 — RED: metrics ring tests (bounded 50 rejection records with
  closed `error_code` + `at_epoch_ms` + route template token); route contract
  test (authenticated admin session required; closed shape only).
- [ ] Step 2 — Implement: ring in `InMemorySmallFileSyncMetrics`, read-only
  `GET` admin route, OpenAPI export update, generated-client check per repo
  contract.
- [ ] Step 3 — Gates: `uv run pytest tests/contract -q` (+ focused suites) and
  plugin gates untouched; commit `feat: expose sync rejection diagnostics to
  the web admin`.

## Task 5: Operations documentation and closure

- [ ] Update `docs/operations/` with the error-tracing runbook (how to read
  the trail, run the self-check, export, and read the admin route — sanitized
  examples only).
- [ ] Verify the live diagnosis loop once with the user (reload, self-check,
  copy diagnostics) to close the convergence plan's open park mystery or
  escalate with the captured closed reason.
- [ ] One handoff for this plan; BACKLOG rows for anything deferred with a
  verifiable Implement-by.
