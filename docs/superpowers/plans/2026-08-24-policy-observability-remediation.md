# Policy observability remediation plan

Spec: `docs/superpowers/specs/2026-08-24-policy-observability-remediation-design.md`.
Branch: master (single-developer flow; each task lands green).

## Global Constraints

- Denial semantics unchanged (allowed/denied/indeterminate tests verbatim).
- Closed vocabularies only; new tokens limited to: `failed` evaluation
  outcome member, `spool_free_space` / `spool_admission_window_expired` /
  `spool_permits_exhausted`, plus any registry codes C1 must classify.
- Reuse existing surfaces: rejection-diagnostics ring, admin diagnostics
  route family, structured log events, publication event builders.
- API additions (Task 2) follow repo contract: OpenAPI, generated client,
  contract tests, `poe api-contract-check`.
- No new dependencies; no Phase-10 exporters.

## File structure

- `src/personal_os/exclusion_policy/` (errors classification, enforcement
  fail-closed recording, metrics outcome member)
- `src/personal_os/small_file_sync/service.py` (preflight catch routing +
  rejection diagnostics fields)
- `apps/api/src/api_runtime/` (metrics binding, new admin route + models,
  OpenAPI export) + `src/personal_os/api_contracts/` route vocabulary
- `packages/r2-object-storage/src/r2_object_storage/spool.py` (busy reason
  tokens)
- `apps/api/src/api_runtime/authentication_commands.py` (CLI exception
  class token)
- Contract/unit tests under `tests/`; plugin wire-table test only if C1's
  mapping verification requires it.

## Task 1: Classify and surface policy system failures (spec C1: G1)

- [ ] RED: preflight with a not-initialized / signing-unavailable policy
  currently returns 200 `excluded` and records only EXCLUDED — assert the
  collapse first; then implement the SYSTEM vs DENIAL closed split, route
  SYSTEM codes out of both catch sites as typed errors (409/503 envelopes
  per existing mapping), and record the closed `error_code` into the
  rejection diagnostics ring in ALL cases.
- [ ] RED: fail-closed raises record nothing — add the closed `failed`
  member to `EvaluationMetricOutcome` and record it with the code.
- [ ] Pin the plugin-visible mapping end-to-end (409/503 → park/pending
  behavior) per the wire table; add the plugin test only if the mapping is
  not already pinned.
- [ ] Gates: focused pytest (exclusion_policy, small_file_sync, contract
  scanners) + mypy + ruff.
- [ ] Commit: `fix: separate policy system failures from exclusions`.

## Task 2: Bind and expose policy metrics (spec C2: G2)

- [ ] RED: serve graph binds no policy metrics — bind one shared
  `InMemoryExclusionPolicyMetrics` at both composition sites; no-op
  fallback documented.
- [ ] RED: no readable surface — add the authenticated read-only admin
  route (evaluation counters by boundary/decision incl. `failed`,
  publication outcome counters, bounded recent-failure ring with closed
  codes + timestamps) in the diagnostics route family; OpenAPI snapshot +
  generated client + `poe api-contract-check`.
- [ ] Gates: focused suites + contract tests for the route.
- [ ] Commit: `feat: expose policy evaluation diagnostics to the web admin`.

## Task 3: Emit policy-guard publication failures (spec C3: G3)

- [ ] RED: `ExclusionPolicyError` during `_publish` produces no event or
  metric — route it through the existing FAILED event builder + metric
  outcome with the closed code, re-raise unchanged.
- [ ] Gates + commit: `fix: record policy guard failures during publication`.

## Task 4: Spool busy reason tokens + CLI class token (spec C4/C5: G4/G5)

- [ ] RED/GREEN: the three `object_storage_busy` sites carry their distinct
  closed reason tokens in safe_details (registry-validated).
- [ ] RED/GREEN: `_run_protected_command` failure line includes the
  exception class token via the emergency path.
- [ ] Gates + commit: `feat: distinguish busy and CLI failure reasons`.

## Task 5: Documentation and closure

- [ ] Extend the error-tracing runbook (policy system-failure outcomes,
  admin policy route, busy reasons) — sanitized examples only.
- [ ] One live smoke round with the user (broken signer or stopped worker →
  read the trail from the admin route + rotating log).
- [ ] One handoff; BACKLOG rows for anything deferred with verifiable
  Implement-by triggers (e.g., Prometheus sink remains a documented
  boundary TODO).
