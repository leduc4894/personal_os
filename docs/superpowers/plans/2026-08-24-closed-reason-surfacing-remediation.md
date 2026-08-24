# Closed-reason surfacing remediation plan

Spec: `docs/superpowers/specs/2026-08-24-closed-reason-surfacing-remediation-design.md`.
Branch: work directly on `master` per user instruction (single-developer
flow; each task lands green).

## Global Constraints

- Surfacing only: no sync/journal/auth/worker semantic changes (W3 is a
  read-only staleness computation).
- Closed vocabularies everywhere; new tokens limited to those named in the
  spec (C1: `startup_failure` stage tokens, `pass_wrapper_failed`,
  `status_read_failed`, `note_status_read_failed`; C4:
  `preview_dispatch_unavailable`, `reconciliation_dispatch_unavailable`;
  C5: `worker_stale_running`). Type-level enforcement for TS surfaces;
  closed StrEnum/validation in Python.
- Reuse established surfaces: the plugin diagnostics trail, settings
  snapshot fields, admin-route pattern, structured log events. No new
  surface types, no Phase-10 infra.
- Trail appends fire-and-forget with the never-blocks guarantee; all new
  snapshot fields null-safe.
- Privacy: no paths/hostnames/digests/credentials/raw bodies/exception
  text; forbidden-substrate tests extended to new fields/routes.
- API additions (Task 3) follow repo contract: OpenAPI, generated client,
  contract tests.

## File structure

- Plugin: `apps/obsidian-plugin/src/plugin.ts`, `src/authentication/*.ts`,
  `src/authentication/settings-tab.ts`, `src/journal/sync-diagnostics-trail.ts`
  (vocabulary extension only) + tests.
- Server: `apps/api/src/api_runtime/` (lifecycle diagnostics route,
  composition, OpenAPI export), `src/personal_os/source_lifecycle/metrics.py`
  (reader contract if needed), `src/personal_os/api_contracts/` (route
  vocabulary) + contract tests.
- Workers: `apps/worker/src/workflow_worker/policy_workflow_runtime.py` +
  sink injection wiring; `apps/api/src/api_runtime/exclusion_policy_composition.py`
  (W2 summary field).
- Docs: runbook `docs/operations/sync-error-tracing.md` (or a sibling) +
  one handoff.

## Task 1: Plugin composition surfacing (spec C1: P1–P5)

**Files:** `plugin.ts`, `sync-diagnostics-trail.ts` + tests.

- [ ] RED: startup catch currently discards the stage — drive a scripted
  engine/recovery failure and assert no trail entry, no snapshot field
  exists today; then implement the `startup_failure` trail kind (stage
  token + store reason token when applicable), the `lastStartupFailureTokens`
  snapshot field, and the self-check journal verdict rendering the tokens.
- [ ] RED: wrapper catch reports `completed` — assert the dishonest summary
  first; then implement the `pass_wrapper_failed` pass outcome token
  (trail + honest summary) and keep the genuinely-idle path unchanged.
- [ ] RED: `policy_integrity_failed` absent from the snapshot — add the
  closed `policyState` to the snapshot + one fixed guidance line per closed
  value in the settings tab.
- [ ] P4/P5: route the two startup-chain exceptional throws into the
  `startup_failure` path; the two read-swallows record their bounded
  once-per-session tokens.
- [ ] Gates: `pnpm --dir apps/obsidian-plugin exec vitest run && pnpm --dir
  apps/obsidian-plugin exec tsc --noEmit && pnpm --dir apps/obsidian-plugin
  run build`.
- [ ] Commit: `feat: surface plugin startup and pass failures`.

## Task 2: Plugin auth detail tokens (spec C2: A1–A5)

**Files:** `src/authentication/device-authorization.ts`,
`token-session.ts`, `plugin.ts` (snapshot field), `settings-tab.ts` + tests.

- [ ] RED per site A1/A2/A4/A5: scripted failures currently emit
  `onStateChange(state, null)` while holding a closed code — assert the
  null detail first; then pass the closed token as `detail`.
- [ ] A3: tombstone `ClearedReason` joins the settings snapshot and renders
  beside the terminal state.
- [ ] Vocabulary: tokens come from existing closed enums; extend the
  forbidden-substrate tests to the rendered detail lines.
- [ ] Gates as Task 1. Commit: `feat: carry closed auth reasons to the settings surface`.

## Task 3: Lifecycle admin route parity (spec C3: L1–L2)

**Files:** `apps/api/src/api_runtime/` new lifecycle diagnostics route +
composition + OpenAPI export; `api_contracts/` route vocabulary; contract
tests under `tests/contract/`.

- [ ] RED: contract test for the missing route; then implement the
  authenticated read-only admin route mirroring the sync-rejections route
  (commit counters + bounded recent-rejection ring with closed tokens).
- [ ] OpenAPI snapshot + generated client + `poe api-contract-check`.
- [ ] Gates: focused pytest suites + `uv run poe python-type-check` + ruff.
- [ ] Commit: `feat: expose lifecycle rejection diagnostics to the web admin`.

## Task 4: Worker dispatch sinks and reconciliation reasons (spec C4: W1–W2)

**Files:** `apps/worker/src/workflow_worker/policy_workflow_runtime.py`,
worker composition/wiring; `exclusion_policy_composition.py`; tests.

- [ ] RED W1: the two `except Exception: return` sites emit nothing —
  inject a diagnostic sink and emit the closed
  `preview_dispatch_unavailable` / `reconciliation_dispatch_unavailable`
  events at those catches (structured logging boundary; reachable in the
  rotating file sink).
- [ ] RED W2: admin reconciliation summary lacks the reason — select and
  render `safe_error_code` (null-safe closed token).
- [ ] Gates: worker/API focused suites + type-check + ruff.
- [ ] Commit: `feat: surface worker dispatch and reconciliation failures`.

## Task 5: Worker staleness surface (spec C5: W3)

**Files:** `exclusion_policy_composition.py` (or sibling admin read) + tests.

- [ ] RED: rows sitting `running` beyond the staleness bound are reported
  nowhere — implement the read-only staleness computation
  (`worker_stale_running` closed token + age) in the admin policy summary;
  no daemon, no auto-restart.
- [ ] Gates + commit: `feat: report stale running policy work`.

## Task 6: Documentation, smoke round, closure

- [ ] Extend the error-tracing runbook with the new surfaces (settings
  detail tokens, lifecycle admin route, worker events, staleness line) —
  sanitized examples only.
- [ ] Live smoke round with the user (stack up; trigger: wrong-origin auth
  failure → A tokens; stop a worker → staleness line; read back via Copy
  sync diagnostics / Web Admin). Record sanitized evidence.
- [ ] One handoff; retire or add BACKLOG rows for anything deferred with
  verifiable Implement-by triggers.
