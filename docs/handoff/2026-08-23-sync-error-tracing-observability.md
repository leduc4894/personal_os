# Sync Error Tracing and Observability Handoff

**Date:** 2026-08-23
**Plan:** `docs/superpowers/plans/2026-08-23-sync-error-tracing-observability.md` (untracked working doc)
**Spec:** `docs/superpowers/specs/2026-08-23-sync-error-tracing-observability-design.md` (untracked working doc, reconciled in Task 5)
**Branch:** `codex/automatic-vault-convergence`. Base `cfbfced`; implementation range
`cfbfced..69ea532` (Tasks 1–4); **last implementation SHA before Task 5
`69ea532`** (`feat: expose sync rejection diagnostics to the web admin`).
Task 5 (operations documentation and closure) lands as three commits —
`test: allow the diagnostics notice surface`, `docs: record the rejection
ring operation label ruling`, `docs: record sync error tracing operations`
— whose SHAs and the final re-run evidence are recorded in
`.superpowers/sdd/2026-08-23-sync-error-tracing-observability/task-5-report.md`.

Living operational status: `docs/operations/sync-error-tracing.md` (the
runbook this plan set out to produce). Ledger with per-task evidence:
`.superpowers/sdd/2026-08-23-sync-error-tracing-observability/progress.md`.

**Status: automated gates GREEN (Tasks 1–5). The supervised live diagnosis
loop (reload → self-check → copy diagnostics → request_id join → admin
route) has NOT yet been observed against the convergence plan's park
mystery M1 — it is the one open item (§6, one BACKLOG row).**

## 1. What was built (Tasks 1–4, one paragraph per task)

- **Task 1 (`cfe8006`)** — durable closed-token diagnostics trail
  (`apps/obsidian-plugin/src/journal/sync-diagnostics-trail.ts`): 128-entry
  ring persisted as one JSON sidecar (`sync-diagnostics-trail.json`) through
  the vault plugin-directory store; `trail_reset` on corruption; bounded
  append-failure counter (999); taps for `wire_failure` (with the envelope
  `request_id` threaded out of `parseEnvelope`, UUID-gated),
  `journal_failure`, `publish_failure`, `pass_outcome`; all appends
  fire-and-forget, proven never to block sync.
- **Task 2 (`c61bdd0`)** — sanitized export
  (`sync-diagnostics-export.ts`): `Copy sync diagnostics` command
  (clipboard with preformatted-modal fallback, registered at `onload`),
  settings `Sync diagnostics trail` section (derived stop reasons, counts,
  last-5 tail), ISO-8601 **UTC** trail timestamps.
- **Task 3 (`3afa961`)** — bounded self-check (`sync-self-check.ts`):
  three ordered closed-verdict steps (trail persist probe, credential
  presence, origin reachability via the liveness route under a 5 s bound,
  no retry), Notice summary, capability-isolated so it cannot mutate sync
  state.
- **Task 4 (`69ea532`)** — server side: bounded-50 rejection ring in
  `InMemorySmallFileSyncMetrics` (closed `error_code`, `at_epoch_ms`,
  `operation`), read-only session-gated
  `GET /api/admin/sync/rejections`, OpenAPI snapshot + generated client +
  contract tests, no new dependencies.
- **Task 5 (this handoff)** — operations runbook, spec reconciliation,
  contract alignment for the `Notice` import, this handoff and BACKLOG
  rows.

## 2. Gate evidence (final runs, Task 5 close-out)

- RED (deliberate, captured before the fix):
  `uv run pytest tests/contract/api/test_plugin_authentication_bundle.py::test_plugin_sources_import_only_the_closed_obsidian_surface -q`
  → 1 failed at base `69ea532` (`plugin.ts: imports obsidian symbol 'Notice'`).
- GREEN (same node, after the `ALLOWED_OBSIDIAN_IMPORT_NAMES` addition):
  1 passed; full file `uv run pytest tests/contract/api/test_plugin_authentication_bundle.py -q`
  → 9 passed.
- Docs contracts (focused): `uv run pytest tests/contract/api/test_api_documentation.py tests/contract/test_bootstrap_documentation.py -q`
  → 42 passed. No contract pins the new operations runbook; the greps that
  found these suites are recorded in the task-5 report.
- Full contract suite: `uv run pytest tests/contract -q` →
  **636 passed, 5 skipped, 6 deselected** (first run after the models.py
  docstring edit showed the 5 OpenAPI snapshot byte-identity failures —
  the docstring flows into the schema — resolved by regenerating the
  snapshot and the generated client; see §4).
- `uv run poe api-contract-check` → passing (snapshot byte-identity +
  `generate:check`).
- `uv run mypy src apps/api/src` → Success, 125 source files.
- `uv run ruff check` + `ruff format --check` on the two touched Python
  files → clean.
- Plugin-side gates (Tasks 1–3, reviewer-reproduced; see the SDD ledger):
  vitest 644/644, `tsc`, `eslint`, production build. The plugin tree was
  not modified by Task 5.

## 3. Interpretive decisions (with reasons)

1. **`route_template` → `operation` ring label (Task 4, upheld in Task 5).**
   Route templates legally live only in the ASGI scope
   (`request.scope["route_template"]`) and in post-exchange access
   observations; the metrics sink is called from the domain service whose
   only request-context input is the frozen `DiagnosticContext` (no route).
   Carrying a route token down would require extending `DiagnosticContext`
   and the correlation middleware — plumbing the brief rules out. The
   closed `operation` label (`create`/`update`) preserves the diagnostic
   value with zero plumbing. The design spec §Server diagnostics is now
   reconciled to the shipped shape with a one-line ruling note.
2. **UTC trail/export timestamps (Task 2).** The export block is a
   shareable paste; local-time offsets would leak coarse location. Spec
   wording "local timestamp" tension was resolved in favor of ISO-8601 UTC
   and recorded in the ledger at review time.
3. **`Notice` closed-surface addition (Tasks 2/3, aligned repo-side in
   Task 5).** The self-check summary and copy confirmation need Obsidian's
   notice UI. This is a deliberate spec-19 closed-surface addition,
   mirrored in the plugin-side import scan (`src/plugin.test.ts`) since
   Task 2/3 and now in the repo-side
   `ALLOWED_OBSIDIAN_IMPORT_NAMES` with a provenance comment. The spec-19
   categorical surface line in
   `docs/superpowers/specs/2026-08-16-web-auth-and-device-authorization-design.md`
   was extended consistently (notice UI — and modal UI, already in the
   enforced set since the source-lifecycle plan but missing from that
   sentence).
4. **Success `request_id` sampling onto `pass_outcome` (Task 1).** The
   trail vocabulary has no success kind, and per-request success entries
   would churn the 128-entry cap; the pass entry samples the observed
   envelope `request_id` instead. Reviewer accepted.
5. **Docstring edits ripple into the OpenAPI snapshot (Task 5 lesson).**
   Pydantic model docstrings are schema descriptions, so even a
   wording-only model change requires `uv run poe api-contract-export` +
   `pnpm --filter @workspace/api-client run generate` before the contract
   suite is green again.

## 4. Task 5 deliverables and file map

- Runbook: `docs/operations/sync-error-tracing.md` (new) — trail kinds and
  closed vocabularies, self-check verdicts, sanitized export shape,
  settings section, the `request_id` join with API structured logs, the
  admin route shapes, privacy invariants, live verification procedure.
  Sanitized examples only (closed tokens, counts, ISO timestamps); no
  paths/hostnames/credentials.
- Spec reconciliation: spec §Server diagnostics `route_template` →
  `operation` with the ruling note; §Self-check notes the deliberate
  `Notice` closed-surface addition (spec file itself stays an untracked
  working doc per plan convention).
- Wording minor: `SmallFileRejectionRecordData` docstring no longer
  claims route equivalence ("the two sync routes derive their operation
  label from the same request" removed; the label is a stand-in that
  localizes the rejecting operation). Because docstrings are schema
  descriptions, `packages/api-client/openapi.json` and
  `packages/api-client/src/generated/schema.ts` were regenerated
  (one description line each).
- Contract alignment: `tests/contract/api/test_plugin_authentication_bundle.py`
  `ALLOWED_OBSIDIAN_IMPORT_NAMES` += `Notice` (RED → GREEN).
- BACKLOG: five rows added (see §5/§6).

## 5. Deferred minors (rulings; one BACKLOG line each)

- **Trail-module hygiene batch (Task 1 review; deferred, minor):**
  `envelopeRequestId` constructor lacks UUID validation (producers gate;
  the load path validates); queue-driver comment overclaims "every failed
  wire request outcome" (refresh-recovered 401s and resumed
  `operation_retry_required` never reach `handleFailure`); `syncFailureKind`
  duck-type vs `instanceof` asymmetry; `envelopeRequestId` name collision
  across modules; one dead bind in a test; `login_required`-without-wire-
  contact taxonomy imprecision. Ruling: defer — none affects recorded
  evidence; close before the next plugin release.
- **Export/settings typing batch (Task 2 review; deferred, minor):**
  `stopReasonTokens` typed `string[]` one layer short of the closed token
  type (house-consistent); fire-and-forget copy command theoretical
  unhandled rejection; tail element order cosmetic. Ruling: defer to the
  next diagnostics cleanup.
- **Self-check hygiene batch (Task 3 review; deferred, minor):**
  counter-saturation conservative-pass edge at the 999 append-failure cap
  undocumented; flat closed-token union crosses vocabularies without
  per-vocabulary narrowing. Ruling: defer to the next diagnostics cleanup.
  (The "probe read-back is in-memory (sound)" and "fire-and-forget without
  catch (house-consistent)" notes were informational only — no BACKLOG
  row.)
- **`_validate_epoch_ms` masking (Task 4 review; deferred, theoretical):**
  a non-int clock value would surface as the generic `ValueError`; the
  wall clock cannot produce it. Ruling: defer to the next small-file-sync
  metrics change.
- Completed in Task 5 (no BACKLOG row): the spec `route_template`
  reconciliation and the models.py route-equivalence wording minor.

## 6. Next actions

1. **Run the supervised live diagnosis loop once with the user** — reload
   the plugin (trail must survive), `Run sync self-check`, `Copy sync
   diagnostics`, join any `wire_failure · request_id` with the API log
   stream, read `GET /api/admin/sync/rejections`. Goal: close the
   convergence plan's park mystery M1 or escalate with the captured closed
   reason. One BACKLOG row with a verifiable implement-by exists; do not
   mark it passed without the observed round (see the runbook's live
   verification procedure).
2. **Convergence-plan linkage:** its Tasks 6/7 remain open pending this
   plan's trail evidence; its own ledger and handoff
   (`docs/handoff/2026-08-22-automatic-vault-convergence.md`, untracked)
   stay authoritative for that plan. Nothing in this plan closes them.
3. **Uncommitted workspace (not this plan's):** the six convergence-plan
   journey/bootstrap WIP files and the untracked convergence plan/spec/
   handoff docs plus this plan's plan/spec docs remain unstaged by design —
   the convergence plan owns them.
4. When the plugin next ships, pick up the three plugin-side BACKLOG rows
   (§5) in one pass.

## 7. Linked living documents

- Operations runbook: `docs/operations/sync-error-tracing.md`
- Envelope/logging contract: `docs/operations/api-runtime-contract.md`
- Lifecycle operator surface:
  `docs/operations/source-locator-tombstone-lifecycle.md`
- Deferred-minor index: `docs/handoff/BACKLOG.md`
