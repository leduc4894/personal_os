# Closed-Reason Surfacing Remediation Handoff

**Date:** 2026-08-24
**Plan:** `docs/superpowers/plans/2026-08-24-closed-reason-surfacing-remediation.md`
**Spec:** `docs/superpowers/specs/2026-08-24-closed-reason-surfacing-remediation-design.md`
**Branch:** `closed-reason-surfacing-remediation`. Implementation range
`1daf32a..e33c24a` (Tasks 1–5); implementation head `e33c24a`
(`test: pin the serve staleness read sql`). **Final commit of the plan:
the docs commit that carries this handoff** (`docs: extend error tracing
runbook and hand off remediation`, `da1bf8d`) — same convention as the
2026-08-23 handoff. The whole-branch review fix round then landed one
documentation-only commit, `docs: align remediation handoff with review
ledger` (§5 ledger alignment, two more BACKLOG rows, two runbook
caveats); no code changed after `da1bf8d`.

Living operator surface: `docs/operations/sync-error-tracing.md` (extended
by Task 6). Per-task RED/GREEN evidence lives in the SDD reports under
`.superpowers/sdd/2026-08-24-closed-reason-surfacing-remediation/`.

**Status: all offline gates GREEN (Tasks 1–6). The plan's live smoke
round (spec acceptance criterion 4) is PENDING — it requires the user and
the local stack and MUST run before any completion claim (§4).**

## 1. What was built (one paragraph per task)

- **Task 1 (`1daf32a`) — plugin composition surfacing (C1 P1–P5).** New
  trail kind `startup_failure` (stage token `engine_load`/`wasm_read`/
  `journal_recovery`/`other` + the closed `JournalStoreErrorReason` when
  the throw is a store error), snapshot field `lastStartupFailureTokens`
  rendered in the settings Sync status line and in the self-check's
  "journal not running" notice; honest `pass_wrapper_failed` pass outcome
  (trail + summary, never a fake `completed`); `policyState` snapshot
  field with one fixed guidance line per closed value (including
  `policy_integrity_failed`); P4 chain throws buffered (max 8) and
  flushed into the `startup_failure` trail path; P5 read-swallows record
  `status_read_failed` / `note_status_read_failed` once per session.
- **Task 2 (`1e4031e`) — plugin auth detail tokens (C2 A1–A5).** Every
  failure `onStateChange` transition passes the closed token it already
  holds as `detail` (transport codes, server registry codes via
  `resolveDeviceAuthClosedCode`, `policy_*` verification reasons); the
  terminal tombstone `ClearedReason` joins the snapshot and renders as
  `Last cleared reason: <token>` beside the terminal state.
- **Task 3 (`87c7941`) — lifecycle admin route parity (C3 L1–L2).**
  `GET /api/admin/source-lifecycle/rejections` behind the strict Web
  Admin session gate: `commit_counters` rows (`operation` ∈ rename/move/
  delete/restore × `outcome` ∈ committed/rejected/replayed) plus the
  bounded recent-rejection ring (`error_code` from the closed
  `SourceLifecycleErrorCode` registry, `at_epoch_ms`, `operation`).
  OpenAPI snapshot, generated client and contract tests updated.
- **Task 4 (`b3d00b2`) — worker dispatch sinks and reconciliation
  reasons (C4 W1–W2).** Both dispatch runtimes accept an injected
  diagnostic sink and emit `preview_dispatch_unavailable` /
  `reconciliation_dispatch_unavailable` at the two unexpected-start
  catches (fields: the opaque row id, `attempt_count`, closed
  `exception_type` + `stack_fingerprint` reductions); the composition
  roots inject the validating diagnostics logger. The Admin
  reconciliation summary selects and renders the durable
  `safe_error_code` (null-safe).
- **Task 5 (`882547a` + `e33c24a`) — worker staleness surface (C5 W3).**
  Read-only staleness read in the Admin policy status
  (`GET /api/admin/exclusion-policy`): `stale_running_previews` is null
  while nothing is stale, else one row per preview in an executable state
  (`pending`/`leased`/`running`) older than
  `PREVIEW_EXECUTION_DEADLINE_SECONDS` (15 min) — each
  `{policy_preview_id, reason: "worker_stale_running", age_seconds}`,
  oldest first, page of 16, computed on read against the database clock.
  No daemon, no auto-restart.
- **Task 6 (the docs commit carrying this handoff)** — runbook extension
  (`docs/operations/sync-error-tracing.md`: settings detail lines,
  lifecycle route, worker dispatch events, reconciliation reason,
  staleness block, smoke-round procedure), this handoff, four BACKLOG
  rows (the whole-branch review fixes later added two more — §5.4–5.5).

## 2. Gate evidence (final runs, Task 6 close-out)

Plugin gates (exact commands, 2026-08-24):

- `pnpm --dir apps/obsidian-plugin exec vitest run` →
  `Test Files  41 passed (41)` / `Tests  689 passed (689)`, exit 0.
- `pnpm --dir apps/obsidian-plugin exec tsc --noEmit` → no output, exit 0.
- `pnpm --dir apps/obsidian-plugin run build` →
  `node scripts/build-plugin.mjs` completed, exit 0.
- `pnpm --dir apps/obsidian-plugin run lint` (`eslint . --max-warnings=0`)
  → no findings, exit 0.

Canonical full gate (the repo's composed verify task,
`pyproject.toml [tool.poe.tasks.verify]` = format-check, lint,
type-check, boundary-check, test, build):

- `uv run poe verify` → **exit 0** end-to-end (the sequence aborts on any
  member failure); final build phase built all six workspace packages
  (`Successfully built dist\…` × 12 sdists/wheels) and the web/plugin/
  api-client builds all reported `Done`.

Granular re-runs for per-gate result lines:

- `uv run mypy src apps/api/src apps/mcp/src apps/worker/src
  packages/r2-object-storage/src packages/postgresql-source-store/src
  tools` → `Success: no issues found in 180 source files`, exit 0.
- `uv run ruff check src apps packages/r2-object-storage/src
  packages/postgresql-source-store/src tests tools` →
  `All checks passed!`, exit 0.
- `uv run ruff format --check …` → `458 files already formatted`, exit 0.
- `uv run poe api-contract-check` → snapshot byte-identity +
  `openapi-typescript … --check` passed, exit 0.
- `uv run lint-imports` → `Contracts: 5 kept, 0 broken.`, exit 0.
- Focused remediation suites
  (`uv run pytest tests/contract/api/test_source_lifecycle_diagnostics_routes.py
  tests/unit/source_lifecycle/test_metrics.py
  tests/unit/api_runtime/test_exclusion_policy_composition.py
  tests/unit/api_runtime/test_exclusion_policy_routes.py
  tests/unit/workflow_worker tests/unit/diagnostics -q`) →
  `267 passed, 1 warning in 4.79s`, exit 0.

## 3. Interpretive decisions (with reasons)

1. **W3 executable-set staleness (Task 5).** The staleness read covers
   exactly `{pending, leased, running}` — the executable set — not the
   literal `running` state. Grounding: the worker's execution-deadline
   sweep (`expire_overdue_previews_statements`) fails every
   still-executable row whose `created_at` exceeds the 15-minute bound,
   and it runs every dispatch cycle, so a row older than the bound in ANY
   executable state proves no worker is sweeping. A literal-`running`
   read would detect almost nothing: a dead worker's rows rest in
   `pending` (never leased) or `leased` (lease held, outcome unknown) —
   `running` is a transient state a live execution occupies briefly. All
   three states render "Preview running" in the Admin UI, so the verdict
   also matches what the operator sees. The read mirrors the sweep's
   anchor (`created_at` vs the database clock) and its bound
   (`PREVIEW_EXECUTION_DEADLINE_SECONDS`) exactly.
2. **P4 chain failures append the trail but do not set
   `lastStartupFailureTokens` (Task 1).** The two fire-and-forget startup
   chains (grant resume, credential refresh + policy refresh) run AFTER
   the journal stack may already be healthy — a chain throw does not mean
   capture failed closed, so pinning it into the snapshot field that the
   "Journal startup failed:" settings line renders would misattribute a
   recoverable background failure to a dead journal. The throw still
   cannot vanish: it is buffered (max 8 token lists, flushed after the
   sidecar loads) into a `startup_failure` trail entry with the `other`
   stage token. Only `#startJournalCapture`'s own catch sets the snapshot
   field.
3. **P5 read tokens ride the existing `journal_failure` kind (Task 1).**
   The two composition read-swallows (pending-count, note-status) are
   journal reads failing, and the trail vocabulary already has a journal
   failure kind; adding a dedicated kind for two once-per-session tokens
   would grow the closed kind vocabulary for near-zero diagnostic gain.
   The tokens themselves (`status_read_failed`,
   `note_status_read_failed`) are new and closed, once-per-session per
   site (bounded by `#hasRecorded…` flags — no per-render spam).
4. **A4 mapped version-bounds branch keeps its null detail (Task 2).**
   `#surfaceCreationFailure`'s mapped branch (`configuration_invalid`
   for `plugin_version_unsupported`/`api_request_validation_failed`/
   `api_request_malformed`) pre-dates the remediation and renders the
   approved-version bounds text when present; when
   `approvedVersionBounds` is null the detail stays null — it does NOT
   fall through to `resolveDeviceAuthClosedCode`. Reason: that branch
   already surfaces its reason as a bounded state + bounds text; routing
   it through the closed-code resolver would change a mapped,
   deliberate surface the spec did not audit. Only the unmapped fallback
   (`offline`) changed: null → the closed code the transport already
   produced. A foreign throw still yields null (no raw text ever).
5. **Worker file-sink capture is env-driven and NOT wired into
   `.local/run-worker.sh` (Task 4).** The rotating file sink activates
   per process via `KNOWLEDGE_DIAGNOSTICS_LOG_DIR` (blank/unset =
   disabled). The worker launchers do not set it: the sink is a
   `RotatingFileHandler`, whose rotation renames the live file — on
   Windows, two processes sharing one diagnostics directory contend on
   that rename (exclusive open), and the two workers + API all launched
   by the runbook would trip it. Operators who want durable worker
   dispatch events give EACH worker process its own diagnostics
   directory via the env var; the events always ride the structured
   stdout stream regardless. Wiring this into the runbook's launchers
   stays a BACKLOG row (§5).
6. **Lifecycle route mirrors the sync-rejections route shape (Task 3).**
   `commit_counters` + bounded ring, same envelope, same session gate —
   L2 needed no separate change: the ring is the operator surface, and
   the envelope already carries the code to the client/trail.

## 4. PENDING: the live smoke round (spec acceptance criterion 4)

Not run — it requires the user's participation and the local stack. This
is the plan's one open gate; **the plan cannot be reported complete and
no live claim of completion may be made until it runs.** Nothing in this
round may be simulated, mocked or substituted (AGENTS.md live-test
rules).

Prerequisites (in order):

1. Follow `.local/RESTART.md` exactly: `uv run poe stack-status`, then
   `.local/serve-local.sh` (with `.local/run-serve.py`), Web Admin on
   port 38000, the two policy workers via `.local/run-worker.sh`, the
   existing `knowledge-api-verify` tunnel. Do not start services any
   other way.
2. The smoke round reads existing `knowledge-local` surfaces; it creates
   no disposable project. If any step needs a WRITE journey against a
   live API, switch to a `knowledge-ci-*` disposable project first
   (hazard `knowledge-local` per the local-stack contract).
3. For the W1 readback, decide the worker file-sink question first
   (BACKLOG row: wire `KNOWLEDGE_DIAGNOSTICS_LOG_DIR` per worker, or
   read the stdout stream instead).

Trigger/readback checklist per failure class:

- **A-class tokens (wrong-origin auth failure).** Point the plugin at an
  origin that rejects the credential; let one refresh or grant poll
  fail. Readback: settings Sync status connection detail line names the
  closed token (`network_unavailable` / closed server code), and the
  terminal case renders `Last cleared reason: <token>`. Record the
  sanitized settings line.
- **Startup/policy-state lines (P1/P3).** (Only if cheap to trigger —
  not a smoke-round requirement.) Readback: `Copy sync diagnostics`
  trail tail shows `startup_failure · <stage>`; the settings Policy
  state line renders the fixed guidance.
- **W3 staleness line (stop a worker).** With the stack up and a preview
  dispatched, stop the preview worker; wait past the 15-minute bound.
  Readback: `GET /api/admin/exclusion-policy` (Web Admin session)
  `stale_running_previews` carries
  `{"reason": "worker_stale_running", "age_seconds": N}`. Restart the
  worker; rows converge or fail closed on their own. Record the
  sanitized staleness row.
- **Lifecycle rejection ring (L1).** Trigger one typed lifecycle 4xx (a
  locator conflict on restore is the cheap trigger). Readback:
  `GET /api/admin/source-lifecycle/rejections` ring names the closed
  `error_code`; match it against the plugin trail's parked outcome.
- **W1 dispatch events (only if a dispatch sink is enabled).** With a
  worker diagnostics sink active, a dispatch failure emits
  `preview_dispatch_unavailable`/`reconciliation_dispatch_unavailable`
  with the closed fields. Optional this round unless the sink row is
  done.

All recorded evidence must stay sanitized exactly like the runbook's
examples (closed tokens, counts, timestamps; no paths/hostnames/
credentials/raw content).

## 5. Deferred items (verdicts; one BACKLOG row each)

1. **Worker rotating-file capture not wired in `.local/run-worker.sh`**
   — env `KNOWLEDGE_DIAGNOSTICS_LOG_DIR`, one worker-specific directory
   per process (shared directories hit the Windows rotation rename
   contention, §3.5). Verdict: defer; the events ride stdout today.
   **Implement by: before the live W1 smoke round** (it is the W1
   readback's durable capture).
2. **Web Admin UI rendering decision for `worker_stale_running` and the
   lifecycle rejections route** — both are wire-only today (API route
   fields + generated client); no Admin UI line renders them. The spec's
   acceptance criterion 4 reads back "from Web Admin" — decide whether
   that means a UI line or an authenticated endpoint read through the
   Web Admin session. Verdict: defer the decision to the smoke round
   operator. **Implement by: before the live smoke readback** (the
   round's readback step depends on the answer).
3. **Reconciliation intent stuck `leased` has no staleness verdict** —
   grounding: reconciliation leases cover only the workflow start call
   (`POLICY_RECONCILIATION_LEASE_SECONDS = 60`, reclaimed by any live
   worker's next cycle), and `dispatched` is the RESTING state of a
   healthy intent while its Temporal batches run — an age bound on
   `dispatched` would false-positive, and no domain execution-deadline
   constant exists for reconciliation to mirror (unlike W3's preview
   bound). An honest verdict needs a domain-defined bound (or heartbeat)
   introduced with scheduling hardening, not an invented constant. A
   dead worker is ALREADY detected by the preview staleness surface
   (same sweep class), so this is completeness, not a blind spot.
   Verdict: defer. **Implement by: before production activation** (the
   milestone where worker liveness guarantees get their acceptance
   pass).
4. **The write side never records the `committed` outcome** —
   `SourceLifecycleService.commit` records only the `replayed` outcome
   (`_record_replay`) and typed rejections (`_record_rejection`);
   a fresh successful commit returns without
   `record_commit(COMMITTED)`, so the lifecycle route's
   `commit_counters` can only ever show `replayed` and `rejected` rows.
   Pre-existing write-side gap outside the remediation diff, surfaced by
   Task 3's review. Verdict: defer — the remediation is surfacing-only;
   the route reads whatever the write side records. **Implement by: at
   next source-lifecycle metrics change** — the gap lives in the metrics
   call sites of `src/personal_os/source_lifecycle/service.py`, so any
   change to those metrics is the natural moment to close it (same
   conditional style as the `At next small-file-sync metrics change`
   rows).
5. **P5 read tokens can occupy the derived settings "Stop reasons"
   line** — `status_read_failed`/`note_status_read_failed` ride the
   `journal_failure` trail kind (§3.3), and the stop-reason derivation
   takes the newest token per failure kind, so a swallowed settings
   read can render as the current stop reason while sync itself runs
   fine. Verdict: defer — the tokens are honest trail signal; only
   their derived placement misleads, and the live smoke round does not
   read that line (its readbacks are the settings detail lines, the
   trail tail and the admin routes). **Implement by: at next plugin
   diagnostics-trail vocabulary change** — excluding read tokens from
   the derivation or giving them their own kind is a trail-vocabulary
   decision, best made the next time that closed vocabulary changes.

Trivial polish that died in review stays as prose here (verdict:
accept as-is, no row). The ledger
(`.superpowers/sdd/2026-08-24-closed-reason-surfacing-remediation/progress.md`)
records twenty per-task review minors across Tasks 1–6 (one from Task
5's re-review); every one is accounted for here or in the rows above.
The fourteen ride items:

- Task 1: stage-assignment ordering asserted only for
  trail-before-wasm (no indexOf-style assertions for
  journal_recovery-before-open and engine_load-before-load) — accept
  as-is, test-coverage polish.
- Task 1: P5 once-flags set before the trail null-check, asymmetric
  with buffering — accept as-is, unreachable today (the ledger's own
  verdict).
- Task 1: notice duration changed to 10_000ms — accept as-is, benign
  unrequested extra (the ledger's own verdict).
- Task 2: A4 mapped branch with null version bounds still emits null
  detail while holding a closed code — accept as-is, deliberate per
  §3.4 (the audit scoped to the non-mapped collapse;
  `configuration_invalid` is the diagnosis).
- Task 2: `DeviceAuthError.code` stays typed `string` (compile-time
  union only for `clearedReason`) — accept as-is, pre-existing
  contract; the tokens are closed at runtime and widening would breach
  the surfacing-only scope.
- Task 2: two overlapping source windows pin the same snapshot builder
  in `plugin.test.ts` — accept as-is, test-structure polish.
- Task 3: the contract test seeds its rejection via the recorder
  directly instead of driving a typed rejection through the lifecycle
  POST route — accept as-is, test polish.
- Task 4: `RecordingDiagnosticSink` duplicated verbatim in two worker
  test files — accept as-is, test-support polish.
- Task 4: string-matching source-contract tests pin spelling not
  behavior — accept as-is, pre-existing house pattern.
- Task 5: no test for the staleness-read-fails → closed
  dependency-error case — accept as-is, inherited from the shared
  retry policy ordering.
- Task 5: `get_policy_status` docstring lags the new staleness block —
  accept as-is, docstring drift; the runbook documents the block.
- Task 5: `STALE_RUNNING_PAGE_MAXIMUM = 16` caps silently with no
  truncation marker — the bound is now stated in the runbook (this fix
  round); the marker itself stays unimplemented, accept as-is.
- Task 6: the final commit SHA named by subject only, not written in
  prose — accept as-is, the header's `git log -1` note covers it.
- Task 6: one gate-evidence line abbreviates the ruff format path set
  with an ellipsis — accept as-is, prose abbreviation only.

The remaining six ledger lines are not ride items: Task 5's re-review
LIMIT-pinning minor was addressed inside the branch (`e33c24a` pinned
the interpolated bound); Task 5's wire-only-rendering and
reconciliation-leased minors are rows 2–3 above; Task 3's pre-existing
COMMITTED minor and Task 1's P5-read-token minor are rows 4–5 above;
and Task 6's runbook-page-bound minor is fixed by this same docs
commit.

## 6. Next actions

1. Run the live smoke round (§4) with the user; record sanitized
   evidence in the operator record; then retire the smoke-round BACKLOG
   row. Until then the remediation plan stays open.
2. Decide and implement the Web Admin rendering question (§5.2) — one
   UI line vs endpoint read — before the readback.
3. Wire the worker diagnostics directories (§5.1) if durable W1 capture
   is wanted for the round.
4. The two review-fix BACKLOG rows (§5.4 write-side `committed`
   recording, §5.5 read tokens in the derived stop reasons) wait on
   their own triggers; nothing in this plan blocks on them.
5. Nothing else is queued for this plan; the 2026-08-23 sync-error-
   tracing BACKLOG rows (plugin release batches) are untouched and
   remain owned by that handoff.

## 7. Linked living documents

- Operations runbook: `docs/operations/sync-error-tracing.md`
- Local restart runbook: `.local/RESTART.md` (never copy its details)
- API runtime/logging contract: `docs/operations/api-runtime-contract.md`
- Deferred-work index: `docs/handoff/BACKLOG.md`
- Spec: `docs/superpowers/specs/2026-08-24-closed-reason-surfacing-remediation-design.md`
